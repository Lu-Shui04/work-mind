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

import httpx
from pydantic import BaseModel, Field

from app.services.model import create_chat_model
from app.utils.logger import logger

PROVIDER = (os.getenv("RERANK_PROVIDER", "llm") or "llm").lower()
# 一次最多重排多少条候选（LLM 重排的 prompt 长度与费用由它决定）
MAX_CANDIDATES = int(os.getenv("RERANK_MAX_CANDIDATES", "12"))
# 每条候选送给重排模型的字符数上限（head 部分信息量最大）
MAX_CHARS = int(os.getenv("RERANK_MAX_CHARS", "600"))
# 向量召回池 = k * POOL_MULT，宽召回 + 精排
POOL_MULT = int(os.getenv("RERANK_POOL_MULT", "3"))
POOL_MAX = int(os.getenv("RERANK_POOL_MAX", "20"))
TIMEOUT = float(os.getenv("RERANK_TIMEOUT", "20"))

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


async def _rerank_llm(query: str, docs: list[str]) -> list[float] | None:
    model = create_chat_model(temperature=0, streaming=False)
    listing = "\n\n".join(f"[{i + 1}] {_clip(d)}" for i, d in enumerate(docs))
    result = await model.with_structured_output(_Scores, method="function_calling").ainvoke([
        {"role": "system", "content": _LLM_SYSTEM},
        {"role": "user", "content": f"查询：{query}\n\n候选片段：\n{listing}"},
    ])
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


async def rerank(query: str, docs: list[str]) -> list[float] | None:
    """给每条候选打相关性分（与 docs 顺序一一对应）。失败返回 None（调用方保持原序）。"""
    if not is_enabled() or len(docs) < 2:
        return None
    docs = docs[:MAX_CANDIDATES]
    try:
        if PROVIDER == "bge":
            scores = await _rerank_bge(query, docs)
            model_name = RERANK_MODEL
        else:
            scores = await _rerank_llm(query, docs)
            model_name = "deepseek-chat(listwise)"
        if scores is None:
            return None
        # 被截断的候选给最低分，保证它们排在重排过的那些后面
        scores = scores + [-1.0] * (len(docs) - len(scores))
        logger.info("rag: rerank done", {"provider": PROVIDER, "model": model_name,
                                         "docs": len(docs),
                                         "top": round(max(scores), 2) if scores else None})
        return scores
    except Exception as err:
        logger.warn("rag: rerank failed, keep vector order",
                    {"provider": PROVIDER, "error": str(err)[:160]})
        return None
