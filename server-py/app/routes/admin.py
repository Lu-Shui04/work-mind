# server-py/app/routes/admin.py
# 测试与运维用的一键重置：把进程内的所有状态清空。
#
# 知识库、会话/画像、用量统计都已迁到 PostgreSQL（持久化），审批与挂起的工作流仍在进程内。
# 反复联调时旧数据会干扰判断（"这条记录是哪次测试留下的？"），
# 提供一个显式的重置入口，比让人手动删几十条记录靠谱。
#
# 注意：这是演示/测试接口，**上生产必须加鉴权**（或直接删掉这个路由）。
import os

from fastapi import APIRouter, Depends

from app.services.cache import cache
from app.services.chat import memory as chat_memory
from app.services.identity import User, current_user
from app.services.rag.registry import registry
from app.utils.logger import logger

router = APIRouter()


@router.post("/reset")
async def reset_all(user: User = Depends(current_user)):
    """清空：会话/画像、缓存、知识库文档与向量、审批申请、用量统计、挂起的工作流。"""
    # knowledge 清空 = TRUNCATE documents + chunks（chunks 有外键级联，一次清干净）
    knowledge = await registry.clear()
    result = {
        # 会话记忆现在落在 PostgreSQL 上，重置要真的删库里的行（不再是清 dict）
        "chat": await chat_memory.clear_all(user.tenant_id),
        "cache": cache.clear(),
        "knowledge": knowledge,
        "vectors": {"clearedChunks": knowledge.get("chunks", 0)},
    }

    # ERP 申请记录
    from app.routes.erp import _applications
    result["erp"] = {"clearedApplications": len(_applications)}
    _applications.clear()

    # 挂起中的工作流实例
    from app.routes.workflow import _active_workflows
    result["workflow"] = {"clearedWorkflows": len(_active_workflows)}
    _active_workflows.clear()

    # 用量统计与缓存命中统计（现在落在 usage_calls 表上，重置要真的删库里的行）
    from app.routes.monitor import reset_usage
    result["monitor"] = await reset_usage()

    # 源文件清理：注册表被清空后，uploads 里的文件就成了孤儿（占空间也容易误以为还有数据）
    uploads_dir = "./uploads"
    removed_files = 0
    try:
        for name in os.listdir(uploads_dir):
            if name == ".gitkeep":
                continue
            try:
                os.unlink(os.path.join(uploads_dir, name))
                removed_files += 1
            except OSError:
                pass
    except FileNotFoundError:
        pass
    result["files"] = {"removed": removed_files}

    logger.info("admin: all in-memory state reset", result)
    return {"success": True, "detail": result}
