# server-py/app/main.py
# 服务端入口：注册中间件、路由、启动服务
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import config, validate_config
from app.middleware import RequestLoggerMiddleware
from app.services.db import StorageUnavailable, close_db, init_db
from app.routes.admin import router as admin_router
from app.routes.agent import router as agent_router
from app.routes.chat import router as chat_router
from app.routes.erp import router as erp_router
from app.routes.health import router as health_router
from app.routes.knowledge import router as knowledge_router
from app.routes.monitor import router as monitor_router
from app.routes.prompt import router as prompt_router
from app.routes.trace import router as trace_router
from app.routes.workflow import router as workflow_router
from app.utils.errors import AppError, app_error_handler
from app.utils.logger import logger

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
    from app.routes.monitor import load_budget
    await load_budget()
    logger.info("server started", {"port": config.app.port, "env": config.app.env})
    print("\n🚀 WorkMind Server (FastAPI) 已启动")
    print(f"   地址: http://localhost:{config.app.port}")
    print(f"   健康检查: http://localhost:{config.app.port}/health\n")


@app.on_event("shutdown")
async def on_shutdown():
    await close_db()
