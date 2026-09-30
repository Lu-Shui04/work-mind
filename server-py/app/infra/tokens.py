# server-py/app/infra/tokens.py
"""Token 估算：只给"接口不回 usage"的调用兜底用。

为什么需要它：用量看板要回答"钱花在哪了"，而 embedding / rerank 这两类调用
**不回 usage**（OpenAIEmbeddings 只给向量，Cohere 兼容的 /rerank 只给分数）。
如果不估算，知识库在成本构成里就永远是 ¥0 —— 用户会以为"知识库不花钱"，
而它恰恰是 RAG 里调用量最大的一块。

估算规则（中文为主的企业文档/问题，混少量英文）：
- 中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token，混排取 0.7 token/字符的经验值。
- 宁可略微保守（估高一点点），也不要让长文档的 embedding 费用看起来是 0。

⚠️ 估算值一律用 estimated=True 记账，看板上会标出来，不跟模型返回的真实 token 混为一谈。
"""
from __future__ import annotations

import math

# 每个字符折算的 token 数（中英混排经验值，见文件头说明）
TOKENS_PER_CHAR = 0.7


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return estimate_tokens_from_chars(len(text))


def estimate_tokens_from_chars(chars: int) -> int:
    """按字符数估算。入库场景只统计总量（不必为了估算再拼一个巨大的字符串）。"""
    return max(1, math.ceil(max(0, chars) * TOKENS_PER_CHAR)) if chars else 0


def estimate_tokens_many(texts) -> int:
    return sum(estimate_tokens(t) for t in (texts or []))


def sum_usage(usage_metadata) -> tuple[int, int, int]:
    """汇总成 (输入, 输出, **缓存命中的输入**)。

    输入形如 {"deepseek-flash": {"input_tokens": 1, "output_tokens": 2,
                                "input_token_details": {"cache_read": 0}}, ...}。
    为什么用回调处理器而不是读返回值：with_structured_output(...) 返回的是**解析后的
    pydantic 对象**，里面没有 usage；挂个 handler 才能在不动业务代码的前提下拿到真实 token。
    一次链式调用里可能用到多个模型实例，所以按模型累加。

    第三个返回值是提示缓存命中的输入 token（DeepSeek 的 cache_read）：
    它单价只有未命中的 1/50，必须单独拿出来算，否则费用会明显偏高。
    """
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    for entry in (usage_metadata or {}).values():
        if not isinstance(entry, dict):
            continue
        input_tokens += int(entry.get("input_tokens") or 0)
        output_tokens += int(entry.get("output_tokens") or 0)
        details = entry.get("input_token_details") or {}
        if isinstance(details, dict):
            cached_tokens += int(details.get("cache_read") or 0)
    return input_tokens, output_tokens, cached_tokens


def cache_read_of(usage_metadata) -> int:
    """从单条 usage_metadata（例如流式的最后一个 chunk）里取缓存命中的输入 token。"""
    if not isinstance(usage_metadata, dict):
        return 0
    details = usage_metadata.get("input_token_details") or {}
    if not isinstance(details, dict):
        return 0
    return int(details.get("cache_read") or 0)
