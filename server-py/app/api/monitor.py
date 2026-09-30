# server-py/app/api/monitor.py
"""用量看板：API 调用统计、Token 消耗、缓存命中率、成本。

这个模块以前是"跟整个系统不通"的典型，一次把五处断开的地方接上：

1) **统计只活在进程内存里**。而 server 容器没有挂载源码，改任何一行后端代码
   都要 docker compose up -d --build server —— 一重建内存清零，
   看板上刚跑出来的数字就没了，看起来永远像"没生效"。
   现在每次调用写一行 usage_calls（PostgreSQL），看板从库里聚合；
   数据库不可用时降级回内存统计，并在 overview.dataSource 里如实标出来。
2) **缓存命中从来没记过账**：chat 命中缓存时直接 return，没调 record_api_call，
   所以"缓存命中率"永远是 0%。现在命中/未命中都记账（命中记 saved_tokens）。
3) **知识库检索一次都不记**：看板上根本看不到"RAG 知识库"这个功能，
   而检索里的 query embedding + 重排是真实费用。现在补上。
4) **chat 的延迟写死 0**：于是"平均响应 / P50 / P90 / P99"里从来没有对话
   （最常用的功能），只剩 prompt 和 erp 的数字。现在按真实耗时记账。
5) **日预算也在内存里**：重建后悄悄变回 ¥50，"保存成功但下次又回去了"。
   现在存 app_settings 表。
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from app.models import pricing
from app.infra.cache import cache
from app.core.db import get_pool, get_setting, set_setting
from app.core.logger import logger

router = APIRouter()

_start_time = time.time()

# 价格表统一放在 app/models/pricing.py（DeepSeek 官方现价：按模型 × 缓存命中 × 峰谷时段）。
# 这里不再写死单价 —— 以前写的是 2024 年 deepseek-chat 的美元价 + 7.2 汇率，
# 既过时、也不是本项目实际在用的模型，还漏算了缓存命中与峰谷差价。

DEFAULT_DAILY_BUDGET = 50.0          # ¥50 日预算
_SETTING_BUDGET = "monitor.dailyBudget"
_MEMORY_KEEP = 500                   # 内存里只留最近 500 条，用于降级展示与即时回显

# 内存镜像：数据库写失败 / 未配置数据库时，看板还能显示当次进程内的数据
_recent: deque[dict] = deque(maxlen=_MEMORY_KEEP)
_daily_budget = DEFAULT_DAILY_BUDGET
_pending: set[asyncio.Task] = set()  # 持有引用，避免 fire-and-forget 任务被 GC 掉


def _tz():
    """统计口径的时区：和工具里的"今天"保持一致（容器默认 UTC 会比北京时间差一天）。"""
    try:
        return ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Shanghai"))
    except Exception:  # noqa: BLE001 - 时区数据缺失时退回 UTC，不影响统计
        return timezone.utc


def _cost_cny(input_tokens: int, output_tokens: int, model: str = "",
              cached_input_tokens: int = 0) -> float:
    """按当前时段与模型的实际单价算钱（详见 app/models/pricing.py）。"""
    return pricing.cost_cny(model, input_tokens, output_tokens, cached_input_tokens)


async def _insert_call(row: dict) -> None:
    pool = get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO usage_calls
                    (ts, feature, tenant_id, user_id, input_tokens, output_tokens,
                     latency_ms, cost_cny, from_cache, saved_tokens, estimated,
                     model, cached_input_tokens)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                row["ts"], row["feature"], row["tenantId"], row["userId"],
                row["inputT"], row["outputT"], row["latencyMs"], row["costCNY"],
                row["fromCache"], row["savedTokens"], row["estimated"],
                row.get("model") or "", row.get("cachedT") or 0,
            )
    except Exception as err:  # noqa: BLE001 - 记账失败不能影响业务请求
        logger.warn("monitor: 用量落库失败（本次仅内存统计）", {"error": str(err)})


def record_api_call(feature: str = "chat", input_tokens: int = 0, output_tokens: int = 0,
                    latency_ms: int = 0, from_cache: bool = False,
                    tenant_id: str = "", user_id: str = "",
                    saved_tokens: int = 0, estimated: bool = False,
                    model: str = "", cached_input_tokens: int = 0,
                    cost_cny: float | None = None) -> dict:
    """记一次模型调用。

    estimated=True 表示这个 token 数不是模型返回的，而是按字符数估算的
    （embedding 接口不回 usage，只能估），看板上会标出来，避免把估算当成账实。

    model               —— 实际调用的模型名；费用按它对应的官方单价算（见 services/pricing.py）
    cached_input_tokens —— 输入里命中提示缓存的 token（DeepSeek 的 cache_read），单价便宜 50 倍
    cost_cny            —— 显式指定费用（知识库那种"一次调用混了两个厂商"的场景自己算）

    这个函数是同步的、可在流式生成器里直接调用：先写内存镜像保证看板立刻能看到，
    再 fire-and-forget 落库；没有事件循环（同步上下文）时只留内存，不抛异常。
    """
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    cached_input_tokens = int(cached_input_tokens or 0)
    latency_ms = int(latency_ms or 0)
    if from_cache:
        cost = 0.0
    elif cost_cny is not None:
        cost = float(cost_cny)
    else:
        cost = _cost_cny(input_tokens, output_tokens, model, cached_input_tokens)

    row = {
        "time": datetime.now(timezone.utc),
        "ts": datetime.now(timezone.utc),
        "feature": feature or "chat",
        "tenantId": tenant_id or "",
        "userId": user_id or "",
        "model": model or "",
        "inputT": input_tokens,
        "outputT": output_tokens,
        "cachedT": cached_input_tokens,
        "costCNY": cost,
        "latencyMs": latency_ms,
        "fromCache": bool(from_cache),
        "savedTokens": int(saved_tokens or 0),
        "estimated": bool(estimated),
    }
    _recent.append(row)

    try:
        task = asyncio.get_running_loop().create_task(_insert_call(row))
    except RuntimeError:
        # 同步上下文（脚本、后台线程）里没有事件循环，内存统计依然可用
        return row
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return row


async def reset_usage() -> dict:
    """重置用量统计（测试用）；预算保留当前设置，缓存统计一并归零。"""
    pool = get_pool()
    cleared = len(_recent)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                cleared = int(await conn.fetchval("SELECT count(*) FROM usage_calls") or 0)
                await conn.execute("TRUNCATE usage_calls")
        except Exception as err:  # noqa: BLE001
            logger.warn("monitor: 重置用量失败", {"error": str(err)})
    _recent.clear()
    cache.reset_stats()
    return {"clearedCalls": cleared}


def _percentile(arr, p):
    if not arr:
        return 0
    s = sorted(arr)
    idx = math.ceil(len(s) * p / 100) - 1
    return int(round(s[max(0, min(idx, len(s) - 1))]))


_FEATURE_NAMES = {
    "chat": "对话助手", "knowledge": "RAG 知识库", "agent": "任务 Agent",
    "workflow": "内容工作流", "erp": "ERP 审批", "prompt": "Prompt 调试",
}


def _feature_rows(calls) -> list[dict]:
    features: dict[str, dict] = {}
    for c in calls:
        f = features.setdefault(c["feature"], {"calls": 0, "costCNY": 0.0, "tokens": 0})
        f["calls"] += 1
        f["costCNY"] += 0 if c["fromCache"] else c["costCNY"]
        f["tokens"] += c["inputT"] + c["outputT"]

    result = [
        {"feature": k, "label": _FEATURE_NAMES.get(k, k), "calls": v["calls"],
         "costCNY": round(v["costCNY"], 4), "tokens": v["tokens"]}
        for k, v in features.items()
    ]
    result.sort(key=lambda x: x["calls"], reverse=True)
    return result


def _overview(calls, total_calls, cache_hits, cost_cny, saved_tokens, budget, source) -> dict:
    return {
        "totalCallsToday": total_calls,
        "apiCallsToday": total_calls - cache_hits,
        "cacheHitsToday": cache_hits,
        "cacheHitRate": f"{cache_hits / total_calls * 100:.1f}%" if total_calls else "0%",
        "tokenInputToday": sum(c["inputT"] for c in calls),
        "tokenOutputToday": sum(c["outputT"] for c in calls),
        "costCNYToday": round(cost_cny, 4),
        "savedTokensToday": int(saved_tokens),
        "dailyBudget": budget,
        "budgetUsedPct": min(100, round(cost_cny / budget * 100, 1)) if budget else 0,
        "uptimeSeconds": int(time.time() - _start_time),
        # 数据源：postgres=已落库（重启不丢）；memory=降级，仅当前进程
        "dataSource": source,
    }


def _latency(calls) -> dict:
    latencies = [c["latencyMs"] for c in calls if not c["fromCache"] and c["latencyMs"] > 0]
    return {
        "p50": _percentile(latencies, 50),
        "p90": _percentile(latencies, 90),
        "p99": _percentile(latencies, 99),
        "avg": round(sum(latencies) / len(latencies)) if latencies else 0,
    }


def _last_7_days_from_memory(calls) -> list[dict]:
    tz = _tz()
    today = datetime.now(tz).date()
    days = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        day_calls = [c for c in calls if c["time"].astimezone(tz).date() == d]
        days.append({
            "date": d.isoformat(),
            "label": f"{d.month}/{d.day}",
            "totalCalls": len(day_calls),
            "apiCalls": len([c for c in day_calls if not c["fromCache"]]),
            "inputT": sum(c["inputT"] for c in day_calls),
            "outputT": sum(c["outputT"] for c in day_calls),
            "costCNY": round(sum(c["costCNY"] for c in day_calls if not c["fromCache"]), 4),
        })
    return days


def _stats_from_memory() -> dict:
    tz = _tz()
    today = datetime.now(tz).date()
    today_calls = [c for c in _recent if c["time"].astimezone(tz).date() == today]
    cache_hits = len([c for c in today_calls if c["fromCache"]])
    cost = sum(c["costCNY"] for c in today_calls if not c["fromCache"])
    saved = sum(c["savedTokens"] for c in today_calls if c["fromCache"])

    return {
        "overview": _overview(today_calls, len(today_calls), cache_hits, cost, saved,
                              _daily_budget, "memory"),
        "latency": _latency(today_calls),
        "byFeature": _feature_rows(today_calls),
        "last7Days": _last_7_days_from_memory(list(_recent)),
        "recentCalls": [
            {
                "time": c["time"].isoformat(), "feature": c["feature"],
                "inputT": c["inputT"], "outputT": c["outputT"],
                "cachedT": c.get("cachedT", 0), "model": c.get("model", ""),
                "costCNY": round(c["costCNY"], 5), "latencyMs": c["latencyMs"],
                "fromCache": c["fromCache"], "estimated": c["estimated"],
            }
            for c in list(reversed(_recent))[:50]
        ],
        "cacheStats": cache.get_stats(),
        "pricing": pricing.describe(),
    }


async def _stats_from_db(pool) -> dict:
    tz_name = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
    tz = _tz()
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=6)

    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT count(*)                                              AS total,
                   count(*) FILTER (WHERE from_cache)                    AS hits,
                   COALESCE(sum(input_tokens), 0)                        AS it,
                   COALESCE(sum(output_tokens), 0)                       AS ot,
                   COALESCE(sum(cost_cny) FILTER (WHERE NOT from_cache), 0) AS cost,
                   COALESCE(sum(saved_tokens) FILTER (WHERE from_cache), 0) AS saved
            FROM usage_calls WHERE ts >= $1
            """,
            today_start,
        )
        by_feature = await conn.fetch(
            """
            SELECT feature,
                   count(*)                                                AS calls,
                   COALESCE(sum(cost_cny) FILTER (WHERE NOT from_cache), 0) AS cost,
                   COALESCE(sum(input_tokens + output_tokens), 0)          AS tokens
            FROM usage_calls WHERE ts >= $1
            GROUP BY feature ORDER BY calls DESC
            """,
            today_start,
        )
        daily = await conn.fetch(
            """
            SELECT to_char((ts AT TIME ZONE $1)::date, 'YYYY-MM-DD')     AS d,
                   count(*)                                              AS total,
                   count(*) FILTER (WHERE NOT from_cache)                AS api,
                   COALESCE(sum(input_tokens), 0)                        AS it,
                   COALESCE(sum(output_tokens), 0)                       AS ot,
                   COALESCE(sum(cost_cny) FILTER (WHERE NOT from_cache), 0) AS cost,
                   -- 缓存命中省下的 token：算"成本降幅"要用（7 天窗口比单日稳，见 evals/offline_metrics.py）
                   COALESCE(sum(saved_tokens) FILTER (WHERE from_cache), 0) AS saved
            FROM usage_calls WHERE ts >= $2
            GROUP BY 1
            """,
            tz_name, week_start,
        )
        recent = await conn.fetch(
            """
            SELECT ts, feature, input_tokens, output_tokens, latency_ms,
                   cost_cny, from_cache, estimated, model, cached_input_tokens
            FROM usage_calls ORDER BY id DESC LIMIT 50
            """
        )
        lat = await conn.fetchrow(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99,
                   avg(latency_ms)                                        AS avg
            FROM usage_calls
            WHERE ts >= $1 AND NOT from_cache AND latency_ms > 0
            """,
            today_start,
        )
        budget = await conn.fetchval("SELECT value FROM app_settings WHERE key = $1", _SETTING_BUDGET)

    daily_by_day = {r["d"]: r for r in daily}
    today = datetime.now(tz).date()
    last_7_days = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        r = daily_by_day.get(d.isoformat())
        last_7_days.append({
            "date": d.isoformat(),
            "label": f"{d.month}/{d.day}",
            "totalCalls": int(r["total"]) if r else 0,
            "apiCalls": int(r["api"]) if r else 0,
            "inputT": int(r["it"]) if r else 0,
            "outputT": int(r["ot"]) if r else 0,
            "costCNY": round(float(r["cost"]), 4) if r else 0,
            # 当天缓存命中省下的 token（"成本降幅"指标的取数口）
            "savedT": int(r["saved"]) if r else 0,
        })

    daily_budget = float(budget) if isinstance(budget, (int, float)) else _daily_budget
    total = int(totals["total"])
    hits = int(totals["hits"])
    tok_in = int(totals["it"])
    tok_out = int(totals["ot"])
    cost = float(totals["cost"])

    return {
        "overview": {
            "totalCallsToday": total,
            "apiCallsToday": total - hits,
            "cacheHitsToday": hits,
            "cacheHitRate": f"{hits / total * 100:.1f}%" if total else "0%",
            "tokenInputToday": tok_in,
            "tokenOutputToday": tok_out,
            "costCNYToday": round(cost, 4),
            "savedTokensToday": int(totals["saved"]),
            "dailyBudget": daily_budget,
            "budgetUsedPct": min(100, round(cost / daily_budget * 100, 1)) if daily_budget else 0,
            "uptimeSeconds": int(time.time() - _start_time),
            "dataSource": "postgres",
        },
        "latency": {
            "p50": int(round(lat["p50"] or 0)),
            "p90": int(round(lat["p90"] or 0)),
            "p99": int(round(lat["p99"] or 0)),
            "avg": int(round(lat["avg"] or 0)),
        },
        "byFeature": [
            {"feature": r["feature"], "label": _FEATURE_NAMES.get(r["feature"], r["feature"]),
             "calls": int(r["calls"]), "costCNY": round(float(r["cost"]), 4),
             "tokens": int(r["tokens"])}
            for r in by_feature
        ],
        "last7Days": last_7_days,
        "recentCalls": [
            {
                "time": r["ts"].isoformat(), "feature": r["feature"],
                "inputT": r["input_tokens"], "outputT": r["output_tokens"],
                "cachedT": r["cached_input_tokens"], "model": r["model"],
                "costCNY": round(float(r["cost_cny"]), 5), "latencyMs": r["latency_ms"],
                "fromCache": r["from_cache"], "estimated": r["estimated"],
            }
            for r in recent
        ],
        "cacheStats": cache.get_stats(),
        # 当前生效的单价与时段（高峰/空闲）：让看板上的费用可以人工核对，
        # 而不是"一个说不清的数字"
        "pricing": pricing.describe(),
    }


@router.get("/stats")
async def stats():
    pool = get_pool()
    if pool is not None:
        try:
            return await _stats_from_db(pool)
        except Exception as err:  # noqa: BLE001 - 查询失败退回内存统计，看板不白屏
            logger.warn("monitor: 查询用量失败，退回内存统计", {"error": str(err)})
    # 库不可用或查询失败：退回内存统计，结构完全一致（overview.dataSource 会变成 memory），
    # 前端照常渲染，只是如实标注"重启会丢"
    return _stats_from_memory()


@router.post("/reset")
async def reset():
    """重置用量统计与缓存命中统计（测试用，预算保留）。"""
    result = await reset_usage()
    return {"success": True, **result}


@router.put("/budget")
async def set_budget(body: dict):
    global _daily_budget
    daily_budget = body.get("dailyBudget")
    if not isinstance(daily_budget, (int, float)) or daily_budget <= 0:
        raise HTTPException(status_code=400, detail={"error": {"message": "预算必须是正数"}})
    _daily_budget = float(daily_budget)
    # 落库：以前只改内存，容器一重建就悄悄变回 ¥50（"保存成功但下次又回去了"）
    persisted = await set_setting(_SETTING_BUDGET, float(daily_budget))
    return {"success": True, "dailyBudget": _daily_budget, "persisted": persisted}


async def load_budget() -> float:
    """启动时把日预算读回来（读不到就用默认值）。"""
    global _daily_budget
    value = await get_setting(_SETTING_BUDGET, None)
    if isinstance(value, (int, float)) and value > 0:
        _daily_budget = float(value)
    return _daily_budget
