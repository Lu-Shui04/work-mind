# server-py/app/services/model.py
"""模型工厂 + 韧性外壳：超时 / 指数退避重试 / 熔断 / 降级（DeepSeek → 智谱）。

## 为什么要在模型层做这些
业务代码只需要 `create_chat_model(...)` 拿到一个"能用的模型"，不该关心：
- 这次会不会 429 / 502 / 连接被重置（要重试几次、等多久）
- DeepSeek 是不是挂了（要不要切智谱顶一会儿）
- 上游连续失败时，是不是该**立刻失败**而不是让用户干等 30 秒

所以把四件事收在模型层（而不是散落在每个调用点）：
  1. **超时**：单次尝试有上限（默认 30s，`LLM_TIMEOUT`）
  2. **指数退避重试 + 抖动**：只重试"值得重试"的错误（超时/连接/429/5xx）
  3. **熔断**：连续失败到阈值 → 后续请求直接走降级，不再打上游
  4. **降级**：DeepSeek 不可用 → 智谱 GLM 顶一会儿；智谱也不行 → 明确报错

## ⚠️ 为什么外壳是 Runnable 而不是 BaseChatModel 子类（这是踩过的坑）
一开始把外壳写成 `BaseChatModel` 子类，看起来最"正统"，结果 **Agent 的每个 token 被推了两遍**，
用户看到「公司公司年年假假规定规定…」：
LangGraph 的 `astream_events` 是靠回调事件还原流的，而"一个 chat model 里再调一个 chat model"
会让**内外两次运行都发 on_chat_model_stream** —— 于是同一个 chunk 出现两次。
对话链路不读 astream_events，所以只有 Agent 出问题，更难发现（是评测集抓出来的）。
包成普通 Runnable 之后，chat model 事件只由内层真模型发一次，正好。

（另一个坑：内层 model.astream() 产出的是 AIMessageChunk，而 `BaseChatModel._astream` 要求
 ChatGenerationChunk，两者混淆会让**流式整体报错** —— 换成 Runnable 外壳后这个坑也不存在了。）
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import config
from app.services.resilience import (
    LLM_MAX_RETRIES, LLM_TIMEOUT, BreakerOpenError, RetryExhaustedError,
    bump_metric, call_with_resilience, get_breaker,
)
from app.utils.logger import logger

# ── 降级模型（智谱 GLM，OpenAI 兼容协议）──────────────────────────
# 复用知识库那个 ZHIPU_API_KEY：国内部署不用再多申请一个服务
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "glm-4-flash")
FALLBACK_BASE_URL = os.getenv("FALLBACK_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
FALLBACK_API_KEY = os.getenv("FALLBACK_API_KEY") or config.ai.zhipu_key
FALLBACK_ENABLED = (os.getenv("MODEL_FALLBACK", "auto").lower() != "off") and bool(FALLBACK_API_KEY)


def _note(kind: str, detail: dict) -> None:
    """把韧性事件写进日志 + 全链路追踪（没有追踪上下文时是空操作）。"""
    if kind in ("retry", "breaker_open", "fallback", "gave_up"):
        logger.warn("model resilience: " + kind, detail)
    try:
        from app.services.trace import trace_step
        trace_step("resilience", f"模型韧性：{kind}",
                   status=("error" if kind == "gave_up" else "ok"), detail=detail)
    except Exception:  # noqa: BLE001 - 追踪绝不能影响模型调用
        pass


class ResilientModel(Runnable):
    """给任意"模型 Runnable"套上超时 / 重试 / 熔断 / 降级。

    对上层的接口与 BaseChatModel 对齐：`ainvoke` / `astream` / `bind_tools` /
    `with_structured_output`，所以业务代码（含 `prompt | model` 这种 LCEL 链）无感。
    """

    def __init__(self, primary: Any, fallback: Any = None, *,
                 primary_label: str = "deepseek", fallback_label: str = "zhipu",
                 timeout_seconds: float = LLM_TIMEOUT, max_retries: int = LLM_MAX_RETRIES,
                 model_name: str = ""):
        self.primary = primary
        self.fallback = fallback
        self.primary_label = primary_label
        self.fallback_label = fallback_label
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        # 计费与日志用（chat.py 会读它拿模型名）
        self.model_name = model_name or primary_label

    # ── 通用：主 →（失败/跳闸）→ 备 ────────────────────────────────
    async def _with_fallback(self, call, *, mode: str = "invoke"):
        try:
            return await call_with_resilience(
                lambda: call(self.primary), name=f"model:{self.primary_label}",
                timeout=self.timeout_seconds, retries=self.max_retries,
                breaker_name=f"model:{self.primary_label}", on_event=_note,
            )
        except (RetryExhaustedError, BreakerOpenError) as err:
            if self.fallback is None:
                raise
            bump_metric("fallbacks")
            _note("fallback", {"from": self.primary_label, "to": self.fallback_label,
                               "mode": mode, "reason": str(err)[:200]})
            # 备用上游一样会抖动，所以同样走重试/熔断（熔断器独立，互不牵连）
            return await call_with_resilience(
                lambda: call(self.fallback), name=f"model:{self.fallback_label}",
                timeout=self.timeout_seconds, retries=self.max_retries,
                breaker_name=f"model:{self.fallback_label}", on_event=_note,
            )

    # ── 非流式 ────────────────────────────────────────────────────
    async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002 - 与 LangChain 签名一致
        async def _call(model):
            return await model.ainvoke(input, config=config, **kwargs)

        return await self._with_fallback(_call, mode="invoke")

    def invoke(self, input, config=None, **kwargs):  # noqa: A002
        """同步路径：本项目全程 async，这里只保证可用（不重试，失败即降级）。"""
        try:
            return self.primary.invoke(input, config=config, **kwargs)
        except Exception as err:  # noqa: BLE001
            if self.fallback is None:
                raise
            _note("fallback", {"to": self.fallback_label, "mode": "sync",
                               "reason": f"{type(err).__name__}: {err}"[:200]})
            return self.fallback.invoke(input, config=config, **kwargs)

    # ── 流式 ──────────────────────────────────────────────────────
    async def _stream(self, model, label: str, input, config, kwargs) -> AsyncIterator[Any]:
        """从指定模型流式取 chunk：逐块超时 + 熔断（块与块之间也算超时）。"""
        breaker = get_breaker(f"model:{label}")
        if not breaker.allow():
            raise BreakerOpenError(f"模型 {label} 处于熔断状态")
        try:
            iterator = model.astream(input, config=config, **kwargs).__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(),
                                                   timeout=self.timeout_seconds)
                except StopAsyncIteration:
                    break
                yield chunk
            breaker.on_success()
        except Exception as err:  # noqa: BLE001
            if not isinstance(err, asyncio.CancelledError):
                breaker.on_failure(err)
            raise

    async def astream(self, input, config=None, **kwargs):  # noqa: A002
        """流式：**只要还没吐出任何 token** 就允许降级；吐过就不再切换（否则内容会串）。"""
        emitted = 0
        try:
            async for chunk in self._stream(self.primary, self.primary_label, input, config, kwargs):
                emitted += 1
                yield chunk
        except Exception as err:  # noqa: BLE001
            if emitted or self.fallback is None:
                raise
            bump_metric("fallbacks")
            _note("fallback", {"from": self.primary_label, "to": self.fallback_label,
                               "mode": "stream", "streamedChunks": 0,
                               "reason": f"{type(err).__name__}: {err}"[:200]})
            async for chunk in self._stream(self.fallback, self.fallback_label, input, config, kwargs):
                yield chunk

    # ── 反射式委托：bind_tools / with_structured_output ────────────
    def _delegate(self, op: str, /, *args, **kwargs) -> "ResilientModel":
        """在主/备模型上各调一次同名方法，结果继续用同一套韧性包住。

        这样外壳**不需要知道** bind_tools / with_structured_output 具体做什么
        （哪怕以后换别的模型类、多出别的方法也一样能用）。

        ⚠️ op 必须是**位置限定参数**（那个 `/`）：被委托的方法自己也可能有
        `method=` 这类关键字参数（`with_structured_output(schema, method="function_calling")`），
        如果这里也叫 `method`，就会撞成
        `_delegate() got multiple values for argument 'method'` —— 实测把
        ERP 填单、任务路由、AI 预填全打挂了（而且任务路由是**静默**回退，最难发现）。
        """
        primary = getattr(self.primary, op)(*args, **kwargs)
        fallback = (getattr(self.fallback, op)(*args, **kwargs)
                    if self.fallback is not None else None)
        return ResilientModel(primary, fallback, primary_label=self.primary_label,
                              fallback_label=self.fallback_label,
                              timeout_seconds=self.timeout_seconds,
                              max_retries=self.max_retries, model_name=self.model_name)

    def bind_tools(self, tools, **kwargs) -> "ResilientModel":
        """工具调用同样享受重试/熔断/降级（Agent 的 ReAct 循环靠它）。"""
        return self._delegate("bind_tools", tools, **kwargs)

    def with_structured_output(self, schema, **kwargs) -> "ResilientModel":
        """结构化输出（ERP 解析、审批裁决、A/B 评分、AI 预填都用它）。"""
        return self._delegate("with_structured_output", schema, **kwargs)


def create_chat_model(temperature: float = 0.7, streaming: bool = False) -> ResilientModel:
    """创建对话模型（主：DeepSeek；备：智谱）。上层无感：astream / ainvoke / bind_tools 都能用。"""
    # 客户端级超时比外层超时略短：让底层先报错，我们的重试逻辑才接得住
    primary = ChatOpenAI(
        model=config.ai.primary_model,
        api_key=config.ai.deepseek_key,
        base_url=config.ai.base_url,
        temperature=temperature,
        streaming=streaming,
        stream_usage=streaming,
        timeout=max(1.0, LLM_TIMEOUT - 2),
        max_retries=0,          # 重试统一由我们自己的指数退避做（带熔断与追踪）
    )
    fallback = None
    if FALLBACK_ENABLED:
        fallback = ChatOpenAI(
            model=FALLBACK_MODEL,
            api_key=FALLBACK_API_KEY,
            base_url=FALLBACK_BASE_URL,
            temperature=temperature,
            streaming=streaming,
            stream_usage=streaming,
            timeout=max(1.0, LLM_TIMEOUT - 2),
            max_retries=0,
        )
    else:
        logger.warn("model: 未配置备用模型（FALLBACK_API_KEY / ZHIPU_API_KEY），降级不可用", {})

    return ResilientModel(
        primary, fallback,
        primary_label=config.ai.primary_model or "deepseek",
        fallback_label=FALLBACK_MODEL if fallback is not None else "none",
        model_name=config.ai.primary_model or "deepseek-chat",
    )


class ZhipuAIEmbeddings:
    """轻量 ZhipuAI Embedding 客户端（OpenAI 兼容协议）"""

    def __init__(self, api_key: str, model: str = "embedding-3"):
        self._client = OpenAIEmbeddings(
            model=model,
            api_key=api_key,
            base_url="https://open.bigmodel.cn/api/paas/v4",
            check_embedding_ctx_length=False,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._client.aembed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await self._client.aembed_query(text)


def create_embeddings():
    """
    创建 Embedding 模型（向量化文本，RAG 必用）
    注意：DeepSeek 暂无 embedding 模型，这里用 OpenAI 兼容接口
    """
    # 优先使用智谱 AI（key 格式 xxxx.xxxx）
    if config.ai.zhipu_key:
        return ZhipuAIEmbeddings(api_key=config.ai.zhipu_key, model="embedding-3")
    # 其次使用 SiliconFlow / OpenAI 兼容接口
    if config.ai.openai_key:
        return OpenAIEmbeddings(
            model=config.ai.embed_model,
            api_key=config.ai.openai_key,
            base_url=config.ai.embed_base_url,
            check_embedding_ctx_length=False,
        )
    print("⚠️  未配置 ZHIPU_API_KEY 或 OPENAI_API_KEY，RAG 功能将不可用")
    return None


# 单例：应用启动时创建一次，全局复用
chat_model = create_chat_model(temperature=0.7, streaming=True)
embeddings = create_embeddings()


def primary_model_name() -> str:
    """当前对话模型名 —— 计费按它查单价（见 services/pricing.py）。

    取配置名即可：DeepSeek 侧的老名字（本项目用的是 deepseek-chat）实际由
    V4.1-Flash 提供服务，定价表里这些名字都映射到 Flash 档，算出来的钱一样。
    """
    return getattr(chat_model, "model_name", "") or config.ai.primary_model


def embeddings_tier_name() -> str:
    """向量模型的计费档位名（知识库入库 / 问句向量化按它计价）。"""
    return "zhipu-embedding-3" if config.ai.zhipu_key else "openai-embeddings"
