# server-py/app/routes/health.py
import time

from fastapi import APIRouter

from app.services.cache import cache
from app.services.db import ping, storage_info

router = APIRouter()

_start_time = time.time()


@router.get("/live")
async def live():
    return {"status": "ok", "uptime": int(time.time() - _start_time)}


@router.get("/")
async def health():
    """就绪检查：数据库连不上时 status=degraded（知识库不可用），但进程仍是健康的。"""
    db_ok = await ping()
    return {
        "status": "healthy" if db_ok else "degraded",
        "uptime": int(time.time() - _start_time),
        "cache": cache.get_stats(),
        "storage": {**storage_info(), "connected": db_ok},
        "version": "1.0.0",
    }


@router.get("/resilience")
async def resilience_health():
    """韧性组件的可观测快照：熔断器状态、重试/超时/降级计数、当前阈值配置。

    为什么单独一个接口：出故障时最需要一眼看到的是
    "哪个上游跳闸了、跳了多久、还要多久才探测恢复"，而不是翻日志。
    """
    from app.services.resilience import health_snapshot
    from app.services.cache import cache as _cache

    snapshot = health_snapshot()
    snapshot["cache"] = _cache.get_stats()
    return snapshot
