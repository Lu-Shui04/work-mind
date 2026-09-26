# server-py/app/routes/trace.py
# 全链路追踪：列出每次请求（run）与它的步骤（step），供"哪个环节出错"的回看。
#
# 为什么不做成日志文件而是落到库里：
#   - 要能按"某一条聊天记录"点进来（前端拿得到 run_id）；
#   - 要有筛选/搜索（按功能、状态、关键字）；
#   - 日志是给人扫的，trace 是给人**点开看入参出参**的。
#
# ⚠️ 与 /api/admin/* 一样，这是演示/运维接口：**上生产必须加鉴权**（或者按角色限可见）。
import json

from fastapi import APIRouter, HTTPException, Query

from app.services.db import get_pool
from app.utils.logger import logger

router = APIRouter()

_FEATURE_LABELS = {
    "chat": "对话助手", "knowledge": "RAG 知识库", "agent": "任务 Agent",
    "workflow": "内容工作流", "erp": "ERP 审批", "prompt": "Prompt 调试",
}


def _decode(value):
    """asyncpg 取 jsonb 回来是字符串（没注册 codec），这里统一解成对象。"""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


@router.get("/runs")
async def list_runs(limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0),
                    feature: str | None = None,
                    status: str | None = None,
                    q: str | None = None):
    """追踪列表（不含步骤，步骤由详情接口给）。"""
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "数据库不可用，追踪不可用"}})

    where = ["1=1"]
    args: list = []
    if feature:
        args.append(feature)
        where.append(f"feature = ${len(args)}")
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if q:
        args.append(f"%{q}%")
        where.append(f"(question ILIKE ${len(args)} OR run_id ILIKE ${len(args)})")
    clause = " AND ".join(where)

    args_page = args + [limit, offset]
    sql = f"""
        SELECT run_id, ts, feature, question, user_id, user_name, status,
               duration_ms, step_count, error, summary
        FROM trace_runs
        WHERE {clause}
        ORDER BY ts DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
    """
    count_sql = f"SELECT count(*) FROM trace_runs WHERE {clause}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args_page)
        total = await conn.fetchval(count_sql, *args)

    return {
        "total": int(total or 0),
        "runs": [
            {
                "runId": r["run_id"],
                "time": r["ts"].isoformat(),
                "feature": r["feature"],
                "featureLabel": _FEATURE_LABELS.get(r["feature"], r["feature"]),
                "question": r["question"],
                "userId": r["user_id"],
                "userName": r["user_name"],
                "status": r["status"],
                "durationMs": r["duration_ms"],
                "stepCount": r["step_count"],
                "error": r["error"],
                "summary": _decode(r["summary"]),
            }
            for r in rows
        ],
    }


@router.get("/stats")
async def stats():
    """页面顶部的小结：今天各功能跑了多少次、有没有失败。"""
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "数据库不可用，追踪不可用"}})
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT feature,
                   count(*)                                        AS total,
                   count(*) FILTER (WHERE status <> 'ok')          AS failed,
                   count(*) FILTER (WHERE status = 'running')      AS running
            FROM trace_runs
            WHERE ts >= date_trunc('day', now())
            GROUP BY feature ORDER BY total DESC
            """
        )
        total = await conn.fetchval("SELECT count(*) FROM trace_runs")
    return {
        "totalRuns": int(total or 0),
        "today": [
            {"feature": r["feature"], "label": _FEATURE_LABELS.get(r["feature"], r["feature"]),
             "total": int(r["total"]), "failed": int(r["failed"]), "running": int(r["running"])}
            for r in rows
        ],
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: str):
    """一条追踪的完整时间线：每一步的类型/耗时/状态/入参出参。"""
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "数据库不可用，追踪不可用"}})
    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT * FROM trace_runs WHERE run_id = $1", run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"error": {"message": "追踪记录不存在（可能已过期清理）"}})
        steps = await conn.fetch(
            "SELECT idx, ts, kind, name, status, duration_ms, detail "
            "FROM trace_steps WHERE run_id = $1 ORDER BY idx",
            run_id,
        )

    return {
        "runId": run["run_id"],
        "time": run["ts"].isoformat(),
        "feature": run["feature"],
        "featureLabel": _FEATURE_LABELS.get(run["feature"], run["feature"]),
        "question": run["question"],
        "tenantId": run["tenant_id"],
        "userId": run["user_id"],
        "userName": run["user_name"],
        "status": run["status"],
        "durationMs": run["duration_ms"],
        "stepCount": run["step_count"],
        "error": run["error"],
        "summary": _decode(run["summary"]),
        "steps": [
            {
                "idx": s["idx"],
                "time": s["ts"].isoformat(),
                "offsetMs": int((s["ts"] - run["ts"]).total_seconds() * 1000),
                "kind": s["kind"],
                "name": s["name"],
                "status": s["status"],
                "durationMs": s["duration_ms"],
                "detail": _decode(s["detail"]),
            }
            for s in steps
        ],
    }


@router.delete("/runs")
async def clear_runs():
    """清空全部追踪（联调时用，避免旧数据干扰判断）。"""
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "数据库不可用"}})
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM trace_runs")
        await conn.execute("TRUNCATE trace_runs CASCADE")
    logger.info("trace: cleared", {"count": int(count or 0)})
    return {"success": True, "cleared": int(count or 0)}
