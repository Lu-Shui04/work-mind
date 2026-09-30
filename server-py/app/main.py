# server-py/app/main.py
# 服务端入口：注册中间件、路由、启动服务
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import config, validate_config
from app.core.middleware import RequestLoggerMiddleware
from app.core.db import StorageUnavailable, close_db, init_db
from app.api.admin import router as admin_router
from app.api.agent import router as agent_router
from app.api.chat import router as chat_router
from app.api.erp import router as erp_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.monitor import router as monitor_router
from app.api.prompt import router as prompt_router
from app.api.trace import router as trace_router
from app.api.workflow import router as workflow_router
from app.core.errors import AppError, app_error_handler
from app.core.logger import logger

# 启动前校验配置
validate_config()

app = FastAPI(title="WorkMind Server (FastAPI)")


# ── 基础中间件 ─────────────────────────────────────────────────
# 基础安全响应头（CSP 关闭）
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-DNS-Prefetch-Control"] = "off"
        response.headers["X-Download-Options"] = "noopen"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(GZipMiddleware)  # compression
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.app.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggerMiddleware)


# ── 错误处理 ───────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    message = first.get("msg", "请求参数不合法")
    return JSONResponse(status_code=400, content={"error": {"message": message}})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # detail 已经是路由里构造好的 {"error": {...}} 结构，直接作为响应体返回
    if isinstance(exc.detail, dict):
        content = exc.detail
    elif exc.status_code == 404:
        content = {"error": {"message": "接口不存在"}}
    else:
        content = {"error": {"message": str(exc.detail)}}
    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(AppError)
async def app_error_exception_handler(request: Request, exc: AppError):
    return await app_error_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return await app_error_handler(request, exc)


# ── 路由注册 ───────────────────────────────────────────────────
app.include_router(health_router, prefix="/health")
app.include_router(chat_router, prefix="/api/chat")
app.include_router(knowledge_router, prefix="/api/knowledge")
app.include_router(agent_router, prefix="/api/agent")
app.include_router(workflow_router, prefix="/api/workflow")
app.include_router(erp_router, prefix="/api/erp")
app.include_router(prompt_router, prefix="/api/prompt")
app.include_router(monitor_router, prefix="/api/monitor")
# 全链路追踪（开发/运维用，与 admin 一样：生产环境应加鉴权或移除）
app.include_router(trace_router, prefix="/api/trace")
# 测试/运维用的一键重置（生产环境应加鉴权或移除）
app.include_router(admin_router, prefix="/api/admin")


@app.exception_handler(StorageUnavailable)
async def storage_unavailable_handler(request: Request, exc: StorageUnavailable):
    """知识库数据库不可用时统一返回 503，而不是让每个接口各自 500。"""
    return JSONResponse(status_code=503, content={"error": {"message": str(exc), "code": "STORAGE_UNAVAILABLE"}})


@app.on_event("startup")
async def on_startup():
    # 连接 PostgreSQL + pgvector 并建表（幂等）。失败不阻止启动，
    # 但知识库接口会明确报"数据库未连接"，不会静默返回空结果。
    await init_db()
    # 日预算存在 app_settings 里，启动时读回来（否则重建容器后悄悄变回默认值）
    from app.api.monitor import load_budget
    await load_budget()
    # 缓存：**主动探一次 Redis**，把"用的是 redis+l1 还是降级成 memory-only"在启动日志里说清楚。
    # 缓存挂了不该阻止启动，但必须让人一眼看到（而不是等用户抱怨命中率掉了才发现）。
    from app.infra.cache import cache
    from app.infra.resilience import health_snapshot
    await cache._get_redis()          # noqa: SLF001 - 启动自检，故意提前触发连接
    snapshot = health_snapshot()
    logger.info("resilience: ready", {
        "cache": cache.get_stats()["backend"],
        "breakers": len(snapshot["breakers"]),
        "config": snapshot["config"],
    })
    print(f"   缓存: {cache.get_stats()['backend']}"
          f"（L1 进程内 + L2 Redis，Redis 不可用时自动降级）")
    logger.info("server started", {"port": config.app.port, "env": config.app.env})
    print("\n🚀 WorkMind Server (FastAPI) 已启动")
    print(f"   地址: http://localhost:{config.app.port}")
    print(f"   健康检查: http://localhost:{config.app.port}/health\n")


@app.on_event("shutdown")
async def on_shutdown():
    from app.infra.cache import cache
    await cache.close()      # 关掉 Redis 连接，别让连接池悬着
    await close_db()
