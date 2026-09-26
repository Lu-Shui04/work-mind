# server-py/app/services/rag/rerank.py
"""
重排（Rerank）：向量召回只负责"把候选捞回来"，精排交给重排模型。

为什么必须有这一步：向量相似度是"整体语义接近"，它分不清
"讲的是同一类东西"和"真的能回答这个问题"。实测同一批候选里，
真正回答问题的切片和只沾边的切片，向量分只差 0.02~0.05，排序经常是错的。
重排模型（cross-encoder / LLM）逐条读"查询 + 片段"，判别力高一个量级。

【为什么默认用 LLM 而不是专门的 rerank 模型】
实测智谱的 rerank 接口（model=rerank）在本项目的真实切片上**没有判别力**：
10 条候选全部返回 1.0000，包括明显无关的章节，排序完全不可用。
所以默认走 LLM 重排（用已经配好的 DEEPSEEK_API_KEY，零额外依赖、立即可用）；
如果配了 SiliconFlow 之类的 Cohere 兼容 rerank 接口，把 provider 换成 bge 即可 ——
那种专用 cross-encoder 质量更好、更便宜、更快（推荐生产用 BAAI/bge-reranker-v2-m3）。

任何失败都 fail-open：返回 None，调用方保留向量序，绝不让重排把检索搞挂。
"""
from __future__ import annotations

import os
import time

import httpx
from langchain_core.callbacks import UsageMetadataCallbackHandler
from pydantic import BaseModel, Field

from app.services.model import create_chat_model
from app.services.trace import trace_step
from app.utils.logger import logger
from app.utils.tokens import estimate_tokens_many, sum_usage

PROVIDER = (os.getenv("RERANK_PROVIDER", "llm") or "llm").lower()
# 一次最多重排多少条候选（LLM 重排的 prompt 长度与费用由它决定）
MAX_CANDIDATES = int(os.getenv("RERANK_MAX_CANDIDATES", "12"))
# 每条候选送给重排模型的字符数上限（head 部分信息量最大）
MAX_CHARS = int(os.getenv("RERANK_MAX_CHARS", "600"))
# 向量召回池 = k * POOL_MULT，宽召回 + 精排
POOL_MULT = int(os.getenv("RERANK_POOL_MULT", "3"))
POOL_MAX = int(os.getenv("RERANK_POOL_MAX", "20"))
TIMEOUT = float(os.getenv("RERANK_TIMEOUT", "20"))

# 重排分数下限：低于它的候选直接丢弃（不是"排后面"，是"不要"）。
#
# 为什么必须有：向量相似度阈值（RAG_SIMILARITY_THRESHOLD=0.35）只能保证"主题沾边"，
# 拦不住"讲的是同一类东西、但回答不了这个问题"的切片。实测同一套库：
#   查询「B端和C端AI产品的设计差异」→ 重排最高 1.00（真命中，应保留）
#   查询「对比 Vue3 和 React …生成技术选型报告」→ 重排最高 0.04（12 条候选全是噪音）
# 假命中留在结果里后果不只是"答得不好"：Agent 会认为"知识库命中 6 条"，
# 于是把任务锁死在知识分支，一句"未找到相关内容"就结束了，工具完全没机会执行。
#
# 标度随 provider 不同（bge 是 0~1，llm 是 0~10 的整数），所以默认值按 provider 取；
# 想关掉过滤就设 RAG_RERANK_MIN_SCORE=0。
_DEFAULT_MIN_SCORE = "0.3" if PROVIDER == "bge" else "5"
MIN_SCORE = float(os.getenv("RAG_RERANK_MIN_SCORE") or _DEFAULT_MIN_SCORE)

RERANK_BASE_URL = os.getenv("RERANK_BASE_URL", "https://api.siliconflow.cn/v1")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


def rerank_api_key() -> str | None:
    return os.getenv("RERANK_API_KEY") or os.getenv("OPENAI_API_KEY")


def is_enabled() -> bool:
    if PROVIDER in ("none", "off", "false", ""):
        return False
    if PROVIDER == "bge":
        return bool(rerank_api_key())
    if PROVIDER == "llm":
        return bool(os.getenv("DEEPSEEK_API_KEY"))
    return False


def pool_size(k: int) -> int:
    """向量召回池大小：开启重排时放宽，让重排有得挑。"""
    if not is_enabled():
        return k
    return max(k, min(k * POOL_MULT, POOL_MAX))


def _clip(text: str) -> str:
    t = (text or "").strip()
    return t if len(t) <= MAX_CHARS else t[:MAX_CHARS] + "…"


# ── provider: llm（DeepSeek listwise 打分）────────────────────────────
class _Score(BaseModel):
    index: int = Field(description="候选编号，从 1 开始")
    score: int = Field(description="这条片段对该查询的相关性，0-10 的整数，越高越相关")


class _Scores(BaseModel):
    scores: list[_Score] = Field(description="每个候选的相关性打分")


_LLM_SYSTEM = """你是检索结果重排器。给定一个查询和若干候选片段，判断**每条片段能否直接回答这个查询**。

打分标准（0-10）：
- 9-10：直接包含答案，照它就能回答
- 6-8：强相关，包含答案的大部分要素
- 3-5：主题沾边，但不回答这个具体问题
- 0-2：无关

只按"能否回答该查询"打分，不要因为片段本身写得详细就打高分。
必须给每个候选都打分，编号与输入一致。"""


async def _rerank_llm(query: str, docs: list[str],
                     handler: UsageMetadataCallbackHandler | None = None) -> list[float] | None:
    model = create_chat_model(temperature=0, streaming=False)
    listing = "\n\n".join(f"[{i + 1}] {_clip(d)}" for i, d in enumerate(docs))
    # 挂 handler 才能拿到真实 token：with_structured_output 返回的是解析后的对象，没有 usage
    config = {"callbacks": [handler]} if handler is not None else None
    result = await model.with_structured_output(_Scores, method="function_calling").ainvoke([
        {"role": "system", "content": _LLM_SYSTEM},
        {"role": "user", "content": f"查询：{query}\n\n候选片段：\n{listing}"},
    ], config=config)
    scores = [0.0] * len(docs)
    for item in result.scores:
        if 1 <= item.index <= len(docs):
            scores[item.index - 1] = max(0.0, min(10.0, float(item.score)))
    return scores


# ── provider: bge（Cohere / Jina 兼容的专用 rerank 接口）───────────────
async def _rerank_bge(query: str, docs: list[str]) -> list[float] | None:
    payload = {"model": RERANK_MODEL, "query": query,
               "documents": [_clip(d) for d in docs], "top_n": len(docs)}
    headers = {"Authorization": "Bearer " + (rerank_api_key() or ""),
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{RERANK_BASE_URL.rstrip('/')}/rerank",
                                 json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    scores = [0.0] * len(docs)
    for item in data.get("results") or []:
        idx = item.get("index")
        val = item.get("relevance_score", item.get("score"))
        if isinstance(idx, int) and 0 <= idx < len(docs) and val is not None:
            scores[idx] = float(val)
    return scores


async def rerank(query: str, docs: list[str], usage_out: dict | None = None) -> list[float] | None:
    """给每条候选打相关性分（与 docs 顺序一一对应）。失败返回 None（调用方保持原序）。

    usage_out：可选，回填这次重排的用量（input_tokens/output_tokens/estimated/latency_ms）。
    为什么由重排自己回填而不是调用方估：LLM 重排能拿到**真实 usage**（挂回调处理器），
    只有 bge 那种专用 rerank 接口不回 usage、才退化成按字符估算。
    """
    if not is_enabled() or len(docs) < 2:
        # "这次为什么没重排"也是排查时要看的信息，别让它静默消失
        trace_step("rerank", "重排跳过", detail={
            "enabled": is_enabled(), "provider": PROVIDER, "candidates": len(docs),
            "reason": "未启用重排" if not is_enabled() else "候选少于 2 条，排序没有意义",
        })
        return None
    docs = docs[:MAX_CANDIDATES]
    started = time.time()
    handler = UsageMetadataCallbackHandler() if PROVIDER != "bge" else None

    def _fill_usage(estimated_fallback: bool) -> None:
        if usage_out is None:
            return
        if handler is not None:
            it, ot = sum_usage(handler.usage_metadata)
            if it or ot:
                usage_out.update({"input_tokens": it, "output_tokens": ot, "estimated": False})
            else:
                usage_out.update({"input_tokens": estimate_tokens_many([query, *docs]),
                                  "output_tokens": 0, "estimated": True})
        else:
            usage_out.update({"input_tokens": estimate_tokens_many([query, *docs]),
                              "output_tokens": 0, "estimated": estimated_fallback})
        usage_out["latency_ms"] = round((time.time() - started) * 1000)

    try:
        if PROVIDER == "bge":
            scores = await _rerank_bge(query, docs)
            model_name = RERANK_MODEL
            _fill_usage(True)
        else:
            scores = await _rerank_llm(query, docs, handler)
            model_name = "deepseek-chat(listwise)"
            _fill_usage(False)
        if scores is None:
            return None
        # 被截断的候选给最低分，保证它们排在重排过的那些后面
        scores = scores + [-1.0] * (len(docs) - len(scores))
        logger.info("rag: rerank done", {"provider": PROVIDER, "model": model_name,
                                         "docs": len(docs),
                                         "top": round(max(scores), 2) if scores else None})
        trace_step("rerank", "重排打分", duration_ms=round((time.time() - started) * 1000), detail={
            "provider": PROVIDER, "model": model_name, "minScore": MIN_SCORE,
            "candidates": len(docs), "top": round(max(scores), 3) if scores else None,
            "scores": [round(s, 3) for s in scores],
        })
        return scores
    except Exception as err:
        # 失败也可能已经产生费用（例如打分回包解析失败），有 handler 就照实记
        _fill_usage(True)
        logger.warn("rag: rerank failed, keep vector order",
                    {"provider": PROVIDER, "error": str(err)[:160]})
        trace_step("rerank", "重排失败（保留向量序）", status="error",
                   duration_ms=round((time.time() - started) * 1000),
                   detail={"provider": PROVIDER, "error": str(err)[:300]})
        return None
