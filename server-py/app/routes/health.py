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
