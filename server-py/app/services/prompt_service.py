# server-py/app/services/prompt_service.py
# Prompt 调试：模板管理、A/B 测试评分
import asyncio
import time
from datetime import datetime, timezone
from typing import Literal

from langchain_core.callbacks import UsageMetadataCallbackHandler
from pydantic import BaseModel, Field

from app.models.llm import create_chat_model
from app.infra.tokens import sum_usage
from app.prompts.lab import (
    AB_COMPARE_SYSTEM,
    AB_JUDGE_SYSTEM,
    CODE_REVIEW_PROMPT,
    CONCISE_QA_PROMPT,
    FRONTEND_ASSISTANT_PROMPT,
)

# 评分模型用 temperature=0，结果稳定
_score_model = create_chat_model(temperature=0)

# ── Prompt 模板存储（生产用数据库）────────────────────────────
_template_store: dict[str, dict] = {}
_template_id_seq = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 内置示例模板
_default_templates = [
    {
        "id": "t_default_1",
        "name": "前端助手",
        "systemPrompt": FRONTEND_ASSISTANT_PROMPT,
        "description": "通用前端技术问答",
        "tags": ["前端", "技术"],
        "createdAt": _now_iso(),
        "versions": [],
    },
    {
        "id": "t_default_2",
        "name": "代码 Review",
        "systemPrompt": CODE_REVIEW_PROMPT,
        "description": "代码审查专用",
        "tags": ["代码", "审查"],
        "createdAt": _now_iso(),
        "versions": [],
    },
    {
        "id": "t_default_3",
        "name": "简洁问答",
        "systemPrompt": CONCISE_QA_PROMPT,
        "description": "简短精准的回答风格",
        "tags": ["简洁"],
        "createdAt": _now_iso(),
        "versions": [],
    },
]

for _t in _default_templates:
    _template_store[_t["id"]] = _t


# ── 模板 CRUD ─────────────────────────────────────────────────
def list_templates() -> list[dict]:
    return sorted(_template_store.values(), key=lambda t: t["createdAt"], reverse=True)


def get_template(template_id: str) -> dict | None:
    return _template_store.get(template_id)


def save_template(name: str, system_prompt: str, description: str = "", tags: list[str] | None = None,
                   existing_id: str | None = None) -> dict:
    global _template_id_seq
    tags = tags or []
    template_id = existing_id or f"t_{int(time.time() * 1000)}_{_template_id_seq}"
    _template_id_seq += 1
    existing = _template_store.get(template_id)

    versions = [*(existing.get("versions", []) if existing else []), {
        "version": len(existing.get("versions", [])) + 1 if existing else 1,
        "systemPrompt": existing["systemPrompt"] if existing else system_prompt,
        "savedAt": _now_iso(),
    }][-10:]

    template = {
        "id": template_id,
        "name": name,
        "systemPrompt": system_prompt,
        "description": description,
        "tags": tags,
        "createdAt": existing["createdAt"] if existing else _now_iso(),
        "updatedAt": _now_iso(),
        "versions": versions,
    }

    _template_store[template_id] = template
    return template


def delete_template(template_id: str):
    if template_id.startswith("t_default_"):
        raise ValueError("内置模板不能删除")
    if template_id not in _template_store:
        raise ValueError("模板不存在")
    del _template_store[template_id]


# ── A/B 测试：AI 自动评分 ──────────────────────────────────────
class _Evaluation(BaseModel):
    relevance: int = Field(ge=1, le=5, description="回答与问题的相关性")
    accuracy: int = Field(ge=1, le=5, description="内容的准确性")
    clarity: int = Field(ge=1, le=5, description="表达的清晰度")
    conciseness: int = Field(ge=1, le=5, description="是否简洁，不啰嗦")
    overall: int = Field(ge=1, le=5, description="综合评分")


class _Comparison(BaseModel):
    winner: Literal["A", "B", "tie"]
    reason: str = Field(description="对比理由，30字以内")


async def score_ab_test(question: str, answer_a: str, answer_b: str,
                     usage_out: dict | None = None) -> dict:
    """AI 评分（3 次模型调用：A/B 各评一次 + 一次对比）。

    usage_out：可选，回填这 3 次调用的真实 token（看板要按真实费用记账；
    以前 A/B 测试记的是 0/0，看板上"Prompt 调试"的调用次数对、费用永远是 ¥0）。
    """
    handler = UsageMetadataCallbackHandler()
    config = {"callbacks": [handler]}
    eval_model = _score_model.with_structured_output(_Evaluation, method="function_calling")

    eval_a, eval_b = await asyncio.gather(
        eval_model.ainvoke([
            {"role": "system", "content": AB_JUDGE_SYSTEM},
            {"role": "user", "content": f"问题：{question}\n\n回答：{answer_a}"},
        ], config=config),
        eval_model.ainvoke([
            {"role": "system", "content": AB_JUDGE_SYSTEM},
            {"role": "user", "content": f"问题：{question}\n\n回答：{answer_b}"},
        ], config=config),
    )

    compare_model = _score_model.with_structured_output(_Comparison, method="function_calling")
    comparison: _Comparison = await compare_model.ainvoke([
        {"role": "system", "content": AB_COMPARE_SYSTEM},
        {"role": "user", "content": f"""
问题：{question}

回答A：{answer_a}
A的评分：相关性{eval_a.relevance} 准确性{eval_a.accuracy} 清晰度{eval_a.clarity} 简洁性{eval_a.conciseness} 综合{eval_a.overall}

回答B：{answer_b}
B的评分：相关性{eval_b.relevance} 准确性{eval_b.accuracy} 清晰度{eval_b.clarity} 简洁性{eval_b.conciseness} 综合{eval_b.overall}

哪个回答更好？"""},
    ], config=config)

    if usage_out is not None:
        input_tokens, output_tokens, cached_tokens = sum_usage(handler.usage_metadata)
        usage_out.update({"input_tokens": input_tokens, "output_tokens": output_tokens,
                          "cached_input_tokens": cached_tokens})

    return {
        "scoreA": eval_a.model_dump(),
        "scoreB": eval_b.model_dump(),
        "winner": comparison.winner,
        "reason": comparison.reason,
    }
