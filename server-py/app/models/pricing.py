# server-py/app/models/pricing.py
"""价格表与费用计算（元 / 百万 tokens）。

## 数据来源（不要凭记忆改，改之前先看页面）
https://api-docs.deepseek.com/zh-cn/quick_start/pricing
核对时间：2026-09

| 项目                    | deepseek-flash | deepseek-v4-pro |
|-------------------------|----------------|-----------------|
| 输入（缓存命中）空闲     | 0.02 元        | 0.15 元         |
| 输入（缓存命中）高峰     | 0.04 元        | 0.30 元         |
| 输入（缓存未命中）空闲   | 1 元           | 4.5 元          |
| 输入（缓存未命中）高峰   | 2 元           | 9.0 元          |
| 输出 空闲               | 4 元           | 13.5 元         |
| 输出 高峰               | 8 元           | 27.0 元         |

- 高峰时段：**北京时间**周一至周五（不含法定节假日）9:00-12:00、14:00-18:00；
  其余时段（含周末与法定节假日全天）为空闲。空闲价 = 高峰价 ÷ 2。
- 模型名映射（官方说明 + 实测）：`deepseek-flash` / `deepseek-v4-flash` /
  `deepseek-v4-flash-vision-exp` → Flash；`deepseek-v4-pro` → Pro。
  老名字 `deepseek-chat`（本项目 .env 里用的就是它）实际由 DeepSeek-V4.1-Flash 提供服务，
  按 Flash 计费 —— 这一点用 response_metadata.model_name 实测确认过（返回 deepseek-flash）。

## 为什么重写
以前写死的是 2024 年的 deepseek-chat 美元价（输入 $0.27/M、输出 $1.10/M，再乘 7.2 汇率），
既不是现在的价格、也不是这个项目实际在用的模型，而且完全没算：
缓存命中的输入（便宜 50 倍）和峰谷差价（差 2 倍）—— 费用会明显偏高。

## 其它厂商（非 DeepSeek）
- 智谱 embedding-3：0.5 元 / 百万 tokens（来源：智谱开放文档 embedding-3 页，核对于 2026-09）
- bge 重排（SiliconFlow 等第三方 rerank 接口）：**官方没有可引用的公开单价**，
  所以默认不计价（0 元），要计就自己配 PRICE_RERANK_PER_M —— 宁可显示 0，也不编一个数字。
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone

# 北京时间（峰谷时段是按北京时间定义的，容器里通常是 UTC，必须显式换算）
BEIJING = timezone(timedelta(hours=8))

# 元 / 百万 tokens（**空闲时段**单价）
DEEPSEEK_TIERS: dict[str, dict[str, float]] = {
    "deepseek-flash": {"cache_hit": 0.02, "cache_miss": 1.0, "output": 4.0},
    "deepseek-v4-pro": {"cache_hit": 0.15, "cache_miss": 4.5, "output": 13.5},
}
# 高峰时段：价格 = 空闲 × 2
PEAK_MULTIPLIER = 2.0
PEAK_WINDOWS: tuple[tuple[time, time], ...] = ((time(9, 0), time(12, 0)),
                                               (time(14, 0), time(18, 0)))

# 非 DeepSeek 的单价（只有输入价，且没有峰谷）
EXTERNAL_TIERS: dict[str, dict[str, float]] = {
    # 智谱 embedding-3（知识库入库 / 问句向量化）
    "zhipu-embedding-3": {"input": 0.5, "output": 0.0},
    # bge 重排：单价可由环境变量配置，默认 0（不猜价格）
    "siliconflow-bge-reranker": {"input": float(os.getenv("PRICE_RERANK_PER_M", "0") or 0),
                                 "output": 0.0},
}

# 模型名 → 计费档位（前缀匹配，兼容各家的写法：deepseek-chat / deepseek-v4-flash ...）
_MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    ("deepseek-pro", "deepseek-v4-pro"),
    ("deepseek-reasoner", "deepseek-v4-pro"),   # 旧的推理模型名，按 Pro 档保守计
    ("deepseek-flash", "deepseek-flash"),
    ("deepseek-v4-flash", "deepseek-flash"),
    ("deepseek-chat", "deepseek-flash"),        # 老名字，实际由 V4.1-Flash 提供服务
    ("deepseek", "deepseek-flash"),             # 兜底：DeepSeek 系默认按 Flash
    ("embedding", "zhipu-embedding-3"),
    ("zhipu", "zhipu-embedding-3"),
    ("bge", "siliconflow-bge-reranker"),
    ("rerank", "siliconflow-bge-reranker"),
)

DEFAULT_TIER = "deepseek-flash"


def tier_for(model: str | None) -> str:
    """把模型名映射到计费档位（认不出来就按本项目默认在用的 Flash 计，并保持可追溯）。"""
    name = (model or "").strip().lower()
    if not name:
        return DEFAULT_TIER
    for alias, tier in _MODEL_ALIASES:
        if alias in name:
            return tier
    return DEFAULT_TIER


def is_peak(at: datetime | None = None) -> bool:
    """是否处于高峰时段（北京时间，周一至周五 9:00-12:00 / 14:00-18:00）。

    注：官方规则里"不含中国法定节假日"，这里没有节假日日历，只按周一至周五判断 ——
    节假日会被算成高峰（价格偏高一点点），属于保守估计，不会把费用算少。
    """
    now = (at or datetime.now(timezone.utc)).astimezone(BEIJING)
    if now.weekday() >= 5:      # 周六 / 周日
        return False
    t = now.time()
    return any(start <= t < end for start, end in PEAK_WINDOWS)


def tier_prices(model: str | None, at: datetime | None = None) -> tuple[str, dict[str, float]]:
    """返回 (档位名, 该时刻实际生效的单价表)。"""
    tier = tier_for(model)
    if tier in DEEPSEEK_TIERS:
        base = DEEPSEEK_TIERS[tier]
        mult = PEAK_MULTIPLIER if is_peak(at) else 1.0
        return tier, {k: v * mult for k, v in base.items()}
    return tier, dict(EXTERNAL_TIERS.get(tier, {"input": 0.0, "output": 0.0}))


def cost_cny(model: str | None, input_tokens: int = 0, output_tokens: int = 0,
             cached_input_tokens: int = 0, at: datetime | None = None) -> float:
    """算一次调用的费用（元）。

    cached_input_tokens 是**输入里命中提示缓存的那些 token**（DeepSeek 返回的
    usage.input_token_details.cache_read），它们单价便宜 50 倍，必须从全价输入里扣掉。
    """
    tier, prices = tier_prices(model, at)
    if "cache_miss" in prices:
        cached = max(0, min(int(cached_input_tokens or 0), int(input_tokens or 0)))
        miss = max(0, int(input_tokens or 0) - cached)
        total = (miss * prices["cache_miss"] + cached * prices["cache_hit"]
                 + int(output_tokens or 0) * prices["output"])
    else:
        total = (int(input_tokens or 0) * prices.get("input", 0.0)
                 + int(output_tokens or 0) * prices.get("output", 0.0))
    return round(total / 1e6, 8)


def cost_for_tier(tier: str, input_tokens: int = 0, output_tokens: int = 0,
                  cached_input_tokens: int = 0, at: datetime | None = None) -> float:
    """按**指定档位**计价（知识库那种"一次调用里混了两个厂商"的场景用）。"""
    return cost_cny(tier, input_tokens, output_tokens, cached_input_tokens, at)


def unpriced_tiers() -> list[str]:
    """哪些档位没配单价（当前一律按 0 计）—— 看板要如实说明，不能让人以为"免费"。"""
    out = []
    for name, table in EXTERNAL_TIERS.items():
        if all(float(v or 0) == 0 for v in table.values()):
            out.append(name)
    return out


def describe(model: str | None = None, at: datetime | None = None) -> dict:
    """给看板/接口用的"当前单价与时段"说明，让费用数字可核对。"""
    tier, prices = tier_prices(model, at)
    now = (at or datetime.now(timezone.utc)).astimezone(BEIJING)
    return {
        "tier": tier,
        "model": model or "",
        "peak": is_peak(at),
        "period": "高峰时段" if is_peak(at) else "空闲时段",
        "beijingTime": now.strftime("%Y-%m-%d %H:%M"),
        "prices": {k: round(v, 4) for k, v in prices.items()},
        "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        "checkedAt": "2026-09",
        "note": "空闲价为高峰价的一半；高峰=北京时间周一至周五 9:00-12:00 / 14:00-18:00",
        # 没配单价的档位：这些调用只记 token、费用按 0 计（不编价格），看板上会标出来
        "unpriced": unpriced_tiers(),
    }
