# server-py/app/routes/knowledge.py
"""
知识库路由 —— 只做"知识资产管理"，不做对话（对话检索在 /api/chat 与 /api/agent 里）。

接口设计原则：
- 所有读写都必须带身份（Depends(current_user)），权限在服务端强制，前端传什么都无法越权
- 列表接口同样按权限过滤：看不到的文档不会出现在列表里（而不是返回后再隐藏）
- 元数据校验交给 DocumentMetadata（Pydantic），错误信息直接回给前端表单
"""
import hashlib
import json
import os
import random
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.schemas.document import (
    Department, DocStatus, DocType, DocumentMetadata, DocumentRecord, IngestStatus,
    SecurityLevel,
)
from app.services.db import StorageUnavailable, storage_info
from app.services.identity import User, can_view, current_user
from app.services.rag.ingest import (
    delete_document, ingest_document, reindex_document, sha256_of,
)
from app.services.rag.query import SearchFilters, retrieve_with_meta
from app.services.rag.registry import registry
from app.services.rag.vectorstore import get_vector_store
from app.utils.logger import logger

router = APIRouter()

UPLOAD_DIR = "./uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".markdown"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

_DEPT_LABELS = {
    "general": "全员通用", "hr": "人力资源", "tech": "技术研发", "finance": "财务",
    "legal": "法务合规", "product": "产品设计", "sales": "市场销售",
}
_TYPE_LABELS = {
    "policy": "制度规定", "manual": "手册指南", "spec": "规范标准",
    "report": "报告", "contract": "合同", "other": "其他",
}
_LEVEL_LABELS = {"public": "公开", "internal": "内部", "confidential": "机密"}
_STATUS_LABELS = {"active": "生效中", "superseded": "已被替代", "archived": "已归档"}


async def _visible_docs(user: User) -> list:
    return [d for d in await registry.list_docs(user.tenant_id) if can_view(d, user)]


def _metadata_diff(existing: DocumentRecord, metadata: DocumentMetadata) -> list[str]:
    """列出"既有文档"与"本次提交的元数据"不一致的字段。

    为什么需要它：同内容文件重复上传时，旧实现无条件复用已有记录，
    于是"把密级从 internal 改成 public"这类修改**永远不会生效**
    （用户以为改好了，其实库里还是旧记录）。
    """
    pairs = {
        "document_title": metadata.document_title,
        "department": metadata.department.value,
        "version": metadata.version,
        "effective_date": metadata.effective_date,
        "doc_type": metadata.doc_type.value,
        "security_level": metadata.security_level.value,
        "expired_date": metadata.expired_date,
        "owner": metadata.owner,
        "remark": metadata.remark,
    }
    diff = [k for k, v in pairs.items() if (getattr(existing, k) or None) != (v or None)]
    if (existing.tags or []) != (metadata.tags or []):
        diff.append("tags")
    return diff


# ── 字典：前端所有下拉选项都从这里取，避免前后端枚举各写一份 ────────────
@router.get("/options")
async def options(user: Annotated[User, Depends(current_user)]):
    versions = sorted({d.version for d in await _visible_docs(user)}, reverse=True)
    return {
        "departments": [
            {"value": k, "label": v} for k, v in _DEPT_LABELS.items()
        ],
        "docTypes": [{"value": k, "label": v} for k, v in _TYPE_LABELS.items()],
        "securityLevels": [{"value": k, "label": v} for k, v in _LEVEL_LABELS.items()],
        "docStatuses": [{"value": k, "label": v} for k, v in _STATUS_LABELS.items()],
        "versions": versions,
        "ingestStatuses": [s.value for s in IngestStatus],
        "currentUser": {
            "userId": user.user_id, "name": user.name,
            "departments": user.departments, "clearance": user.clearance,
            "tenantId": user.tenant_id,
        },
    }


# ── 检索验证（工程工具，不是聊天）：用来证明权限/版本过滤真的生效 ────────
@router.post("/search")
async def search(body: dict, user: Annotated[User, Depends(current_user)]):
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail={"error": {"message": "检索内容不能为空"}})

    filters = SearchFilters(
        department=body.get("department") or None,
        version=body.get("version") or None,
        doc_type=body.get("docType") or None,
        include_superseded=bool(body.get("includeSuperseded")),
    )
    k = min(int(body.get("k") or 5), 20)

    started = time.time()
    try:
        hits, recall = await retrieve_with_meta(question, user, k=k, filters=filters)
    except StorageUnavailable as err:
        raise HTTPException(status_code=503, detail={"error": {"message": str(err)}})
    except ValueError as err:
        raise HTTPException(status_code=400, detail={"error": {"message": str(err)}})

    return {
        "hits": hits,
        "appliedFilters": filters.describe(),
        # 未命中时这里会直接说明原因（kb_empty / all_filtered / below_threshold）
        "recall": recall,
        "diagnostics": {
            "totalChunks": recall["totalChunks"],
            "candidates": recall["candidates"],
            "bestScore": recall["bestScore"],
            "similarityThreshold": recall["threshold"],
            "reason": recall["reason"],
            "explain": recall["explain"],
            "user": {"userId": user.user_id, "departments": user.departments, "clearance": user.clearance},
            "storage": storage_info(),
            "elapsedMs": round((time.time() - started) * 1000, 1),
        },
    }


# ── AI 预填：给文件名 + 可选正文片段，返回建议的元数据（人工确认后才入库）─
@router.post("/metadata/suggest")
async def suggest_metadata(body: dict, user: Annotated[User, Depends(current_user)]):
    from pydantic import BaseModel, Field

    from app.services.model import create_chat_model

    file_name = body.get("fileName") or ""
    snippet = (body.get("snippet") or "")[:2000]
    if not file_name and not snippet:
        raise HTTPException(status_code=400, detail={"error": {"message": "请提供文件名或正文片段"}})

    # 【只让模型推断它擅长的部分】：部门/版本/生效日期/标签/标题。
    # 密级和文档类型**固定为 公开 + 其他**（下面直接赋值），不让模型猜：
    # 猜错密级会让文档直接搜不到（internal 对匿名/其他部门不可见），
    # 代价远大于"少填一个类型"，所以这两个字段交给人在表单里按需改。
    class Suggestion(BaseModel):
        document_title: str = Field(description="从文件名/正文推断的文档标题")
        department: str = Field(description="归属部门，取值：general/hr/tech/finance/legal/product/sales")
        version: str = Field(description="版本号，形如 2026-08；无法判断时用当前年月")
        effective_date: str = Field(description="生效日期 YYYY-MM-DD；无法判断时用今天")
        tags: list[str] = Field(default_factory=list, description="2-4 个标签")
        reason: str = Field(description="判断依据，一句话")

    model = create_chat_model(temperature=0, streaming=False)
    try:
        result = await model.with_structured_output(Suggestion, method="function_calling").ainvoke([
            {"role": "system", "content": (
                "你是企业文档管理员。根据文件名与正文片段推断文档元数据。\n"
                "部门只能取：general(全员通用)/hr/tech/finance/legal/product/sales；\n"
                "推断不出部门就用 general，不要编造具体数字。"
            )},
            {"role": "user", "content": f"文件名：{file_name}\n正文片段：\n{snippet}"},
        ])
    except Exception as err:
        logger.error("knowledge: metadata suggest failed", {"error": str(err)})
        raise HTTPException(status_code=500, detail={"error": {"message": "AI 预填失败，请手动填写"}})

    data = result.model_dump()
    # 归一化：模型可能返回不在枚举里的值，统一兜底，保证前端表单能直接用
    if data["department"] not in {d.value for d in Department}:
        data["department"] = Department.general.value
    # 密级 / 类型固定默认值（见 Suggestion 上的说明）：不猜，避免"猜成 internal 导致搜不到"
    data["security_level"] = SecurityLevel.public.value
    data["doc_type"] = DocType.other.value
    logger.info("knowledge: metadata suggested", {"file": file_name, "by": user.user_id})
    return {"suggestion": data, "aiGenerated": True}


# ── 上传入库 ──────────────────────────────────────────────────────────
@router.post("/documents")
async def upload_document(
    user: Annotated[User, Depends(current_user)],
    force: bool = False,          # force=true 时跳过 sha256 幂等复用，强制入库一份新的
    file: UploadFile | None = File(default=None),
    content: str | None = Form(default=None),
    document_title: str = Form(default=""),
    department: str = Form(default=""),
    version: str = Form(default=""),
    effective_date: str = Form(default=""),
    doc_type: str = Form(default=""),
    security_level: str = Form(default="internal"),
    expired_date: str | None = Form(default=None),
    owner: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    remark: str | None = Form(default=None),
):
    # 1) 元数据校验（先校验再落盘，避免产生"半成品"脏文件）
    tag_list: list[str] = []
    if tags:
        try:
            parsed_tags = json.loads(tags)
            tag_list = parsed_tags if isinstance(parsed_tags, list) else [str(parsed_tags)]
        except (json.JSONDecodeError, TypeError):
            tag_list = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]

    try:
        metadata = DocumentMetadata(
            document_title=document_title, department=department, version=version,
            effective_date=effective_date, doc_type=doc_type, security_level=security_level or "internal",
            expired_date=expired_date or None, owner=owner or None, tags=tag_list, remark=remark or None,
        )
    except ValidationError as err:
        first = err.errors()[0]
        field = ".".join(str(x) for x in first.get("loc", []))
        raise HTTPException(status_code=400, detail={
            "error": {"code": "VALIDATION", "message": f"元数据不合法（{field}）：{first.get('msg')}"}})

    # 2) 取内容：文件或粘贴文本
    if file is not None:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail={"error": {"message": f"不支持的文件格式 {ext}"}})
        data = await file.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail={"error": {"message": "文件超过 10MB 限制"}})
        file_name = file.filename or f"upload{ext}"
        file_type = ext.lstrip(".")
    elif content and content.strip():
        data = content.encode("utf-8")
        file_name = f"{metadata.document_title}.txt"
        file_type = "txt"
        ext = ".txt"
    else:
        raise HTTPException(status_code=400, detail={"error": {"message": "请上传文件或粘贴文本内容"}})

    # 3) 幂等：内容相同直接复用（不重复解析、不重复向量化、不重复计费）
    #    force=true 用于测试：同一份文件想按不同元数据（部门/版本）各存一版时使用
    file_sha256 = hashlib.sha256(data).hexdigest()
    existing = None if force else await registry.find_by_sha256(user.tenant_id, file_sha256)
    reused_note = None
    if existing:
        diff = _metadata_diff(existing, metadata)
        if existing.ingest_status == IngestStatus.indexed.value and not diff:
            logger.info("knowledge: duplicate upload", {"docId": existing.doc_id, "by": user.user_id})
            return {"success": True, "duplicated": True,
                    "message": f"该文件已入库（《{existing.document_title}》），已复用，未重复计费",
                    "document": existing}
        # 两种必须重新入库的情况：
        #   1) 元数据变了（例如把密级改成"公开"）—— 复用旧记录等于修改没生效
        #   2) 上次入库没成功（failed/pending）—— 复用会让索引永远建不起来
        reused_note = ("元数据已变更（" + "、".join(diff) + "），按新元数据重新入库"
                       if diff else
                       f"上次入库状态为 {existing.ingest_status}，重新入库")
        logger.info("knowledge: 同内容文件需要重新入库",
                    {"docId": existing.doc_id, "diff": diff, "status": existing.ingest_status})
        force = True

    # 4) 落盘保留源文件（用于查看/下载/重新解析）
    stored_name = f"{int(time.time() * 1000)}_{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=4))}{ext}"
    source_path = os.path.join(UPLOAD_DIR, stored_name)
    with open(source_path, "wb") as f:
        f.write(data)

    # 5) 入库
    try:
        doc, duplicated = await ingest_document(
            file_path=source_path, file_name=file_name, file_type=file_type,
            file_size=len(data), source_path=source_path, file_sha256=file_sha256,
            metadata=metadata, user=user, force=force,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail={"error": {"message": str(err)}})
    except Exception as err:
        logger.error("knowledge: ingest error", {"error": str(err)})
        raise HTTPException(status_code=500, detail={"error": {"message": f"入库失败：{err}"}})

    return {"success": True, "duplicated": duplicated, "note": reused_note, "document": doc}


# ── 列表：按权限 + 过滤条件 ───────────────────────────────────────────
@router.get("/documents")
async def list_documents(
    user: Annotated[User, Depends(current_user)],
    department: str | None = None,
    doc_type: str | None = None,
    status: str | None = None,
    version: str | None = None,
    keyword: str | None = None,
    include_superseded: bool = True,
):
    docs = await _visible_docs(user)
    if department:
        docs = [d for d in docs if d.department == department]
    if doc_type:
        docs = [d for d in docs if d.doc_type == doc_type]
    if status:
        docs = [d for d in docs if d.status == status]
    if version:
        docs = [d for d in docs if d.version == version]
    if keyword:
        kw = keyword.lower()
        docs = [d for d in docs if kw in d.document_title.lower() or kw in (d.file_name or "").lower()]
    if not include_superseded:
        docs = [d for d in docs if d.status == DocStatus.active.value]

    return {
        "documents": docs,
        "total": len(docs),
        "stats": await registry.stats(user.tenant_id, docs),
        "filters": {
            "department": department, "docType": doc_type, "status": status,
            "version": version, "keyword": keyword, "includeSuperseded": include_superseded,
        },
    }


# ── 详情：完整元数据 + 切片列表 + 版本链 ───────────────────────────────
@router.get("/documents/{doc_id}")
async def document_detail(
    doc_id: str,
    user: Annotated[User, Depends(current_user)],
    chunk_limit: int = Query(default=50, le=500),
    chunk_offset: int = 0,
):
    doc = await registry.get(doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail={"error": {"message": "文档不存在"}})
    if not can_view(doc, user):
        raise HTTPException(status_code=403, detail={"error": {"message": "你没有权限查看该文档"}})

    chunks = await registry.chunks(doc_id)
    page = chunks[chunk_offset:chunk_offset + chunk_limit]

    # 版本链：同租户同标题同类型的其他版本（用于展示"历史版本 / 被谁替代"）
    siblings = [
        {"docId": d.doc_id, "version": d.version, "status": d.status,
         "effectiveDate": d.effective_date, "createdAt": d.created_at}
        for d in await registry.list_docs(user.tenant_id)
        if d.doc_id != doc_id
        and d.document_title == doc.document_title
        and d.doc_type == doc.doc_type
        and d.department == doc.department
    ]

    return {
        "document": doc,
        "chunks": [c.model_dump(exclude={"embedding"}) for c in page],
        "chunkTotal": len(chunks),
        "chunkOffset": chunk_offset,
        "chunkLimit": chunk_limit,
        "versionChain": siblings,
        "elementTypeStats": _element_type_stats(chunks),
    }


def _element_type_stats(chunks) -> dict:
    stats: dict[str, int] = {}
    for c in chunks:
        stats[c.element_type] = stats.get(c.element_type, 0) + 1
    return stats


# ── 源文件下载 / 预览 ─────────────────────────────────────────────────
@router.get("/documents/{doc_id}/file")
async def download_source(doc_id: str, user: Annotated[User, Depends(current_user)]):
    doc = await registry.get(doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail={"error": {"message": "文档不存在"}})
    if not can_view(doc, user):
        raise HTTPException(status_code=403, detail={"error": {"message": "你没有权限下载该文档"}})
    if not doc.source_path or not os.path.exists(doc.source_path):
        raise HTTPException(status_code=404, detail={"error": {"message": "源文件已不存在"}})

    return FileResponse(
        doc.source_path,
        media_type="application/octet-stream",
        filename=doc.file_name,
    )


# ── 重新入库（换解析器 / 换 embedding 模型后使用）──────────────────────
@router.post("/documents/{doc_id}/reindex")
async def reindex(doc_id: str, user: Annotated[User, Depends(current_user)]):
    try:
        doc = await reindex_document(doc_id, user)
    except ValueError as err:
        raise HTTPException(status_code=400, detail={"error": {"message": str(err)}})
    return {"success": True, "document": doc}


# ── 删除 ──────────────────────────────────────────────────────────────
@router.delete("/documents/{doc_id}")
async def remove_document(doc_id: str, user: Annotated[User, Depends(current_user)]):
    doc = await registry.get(doc_id)
    if doc and not can_view(doc, user):
        raise HTTPException(status_code=403, detail={"error": {"message": "你没有权限删除该文档"}})
    try:
        await delete_document(doc_id, user)
    except ValueError as err:
        raise HTTPException(status_code=404, detail={"error": {"message": str(err)}})
    return {"success": True}


@router.delete("/documents")
async def clear_documents(user: Annotated[User, Depends(current_user)]):
    """清空当前身份可见范围内的全部文档（含源文件与向量切片）。"""
    targets = await _visible_docs(user)
    deleted, chunks = 0, 0
    for doc in targets:
        try:
            await delete_document(doc.doc_id, user)
            deleted += 1
            chunks += doc.chunk_count or 0
        except ValueError:
            continue
    logger.info("knowledge: all documents cleared", {"by": user.user_id, "deleted": deleted})
    return {"success": True, "deleted": deleted, "chunks": chunks}


# ── 概览统计 ──────────────────────────────────────────────────────────
@router.get("/stats")
async def knowledge_stats(user: Annotated[User, Depends(current_user)]):
    docs = await _visible_docs(user)
    all_docs = await registry.list_docs(user.tenant_id)
    storage = storage_info()
    return {
        "visible": await registry.stats(user.tenant_id, docs),
        "tenantTotal": len(all_docs),
        "hiddenByPermission": len(all_docs) - len(docs),
        "vectorChunks": await get_vector_store().count(user.tenant_id),
        "storage": storage,
        "currentUser": {
            "userId": user.user_id, "name": user.name,
            "departments": user.departments, "clearance": user.clearance,
        },
    }
