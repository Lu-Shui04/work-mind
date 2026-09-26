# server-py/app/services/rag/registry.py
"""
文档注册表 —— PostgreSQL 实现（documents 表 + chunks 表的读取）。

为什么不把 status（是否已被新版本替代）等"会变的字段"冗余进向量切片：
- status 会随"上传新版本"改变，两份存储一定会出现双写不一致
  （旧版本的切片还带着 active）。
- 所以约定：**切片只存"不会变的过滤字段快照"（tenant/department/version/doc_type/security_level），
  会变的字段（status / superseded_by / effective_date / expired_date）检索时 JOIN documents 现算**。
- 现在两张表在同一个库里，这个 JOIN 就是一条 SQL（见 query.py / vectorstore.py），
  不再需要"内存 join"；换库前的那套双写不一致问题随之消失。

切片正文的**写入**在 vectorstore.add()（chunks 表就是向量库），这里只负责读，
避免"注册表写一遍、向量库又写一遍"的双写。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.services.db import (
    StorageUnavailable, date_str, require_pool, to_date, to_ts, ts_str,
)
from app.schemas.document import ChunkRecord, DocStatus, DocumentRecord, IngestStatus
from app.utils.logger import logger

# 版本治理的"同一份文档"判定维度：租户 + 标题 + 类型 + 部门
_GROUP_KEYS = ("document_title", "doc_type", "department")

# 字段清单直接从模型取，避免"模型加了字段、SQL 忘了改"这类漂移
_DOC_FIELDS: tuple[str, ...] = tuple(DocumentRecord.model_fields.keys())
_CHUNK_COLUMNS = ("chunk_id", "doc_id", "tenant_id", "order_index", "department", "version",
                  "doc_type", "security_level", "page_number", "page_end",
                  "element_type", "element_count", "heading_path", "text", "char_count")

# 需要转成 date / timestamptz / jsonb 的列
_DATE_FIELDS = {"effective_date", "expired_date"}
_TS_FIELDS = {"created_at", "indexed_at"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enc(field: str, value):
    """Python 值 → asyncpg 能写入的值。"""
    if field in _DATE_FIELDS:
        return to_date(value)
    if field in _TS_FIELDS:
        return to_ts(value) or datetime.now(timezone.utc)
    if field == "tags":
        return json.dumps(value or [], ensure_ascii=False)
    return value


def _row_to_doc(row) -> DocumentRecord:
    data = dict(row)
    data["effective_date"] = date_str(data.get("effective_date")) or ""
    data["expired_date"] = date_str(data.get("expired_date"))
    data["created_at"] = ts_str(data.get("created_at"))
    data["indexed_at"] = ts_str(data.get("indexed_at")) or None
    tags = data.get("tags")
    data["tags"] = json.loads(tags) if isinstance(tags, str) else (tags or [])
    return DocumentRecord(**data)


def _row_to_chunk(row) -> ChunkRecord:
    data = dict(row)
    return ChunkRecord(**data)


class DocRegistry:
    """documents 表的仓储。方法全部异步 —— 调用方必须 await。"""

    # ── 基础 CRUD ─────────────────────────────────────────────────────
    async def add(self, doc: DocumentRecord) -> DocumentRecord:
        pool = require_pool()
        cols = ", ".join(_DOC_FIELDS)
        marks = ", ".join(f"${i + 1}" for i in range(len(_DOC_FIELDS)))
        values = [_enc(f, getattr(doc, f)) for f in _DOC_FIELDS]
        await pool.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", *values)
        return doc

    async def get(self, doc_id: str) -> DocumentRecord | None:
        row = await require_pool().fetchrow("SELECT * FROM documents WHERE doc_id = $1", doc_id)
        return _row_to_doc(row) if row else None

    async def get_many(self, doc_ids) -> dict[str, DocumentRecord]:
        ids = list({i for i in doc_ids if i})
        if not ids:
            return {}
        rows = await require_pool().fetch(
            "SELECT * FROM documents WHERE doc_id = ANY($1::text[])", ids)
        return {r["doc_id"]: _row_to_doc(r) for r in rows}

    async def list_docs(self, tenant_id: str | None = None) -> list[DocumentRecord]:
        if tenant_id:
            rows = await require_pool().fetch(
                "SELECT * FROM documents WHERE tenant_id = $1 ORDER BY created_at DESC", tenant_id)
        else:
            rows = await require_pool().fetch("SELECT * FROM documents ORDER BY created_at DESC")
        return [_row_to_doc(r) for r in rows]

    async def update(self, doc_id: str, **fields) -> DocumentRecord | None:
        updates = {k: v for k, v in fields.items() if k in _DOC_FIELDS and k != "doc_id"}
        if not updates:
            return await self.get(doc_id)
        sets = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(updates))
        values = [_enc(k, v) for k, v in updates.items()]
        row = await require_pool().fetchrow(
            f"UPDATE documents SET {sets} WHERE doc_id = ${len(values) + 1} RETURNING *",
            *values, doc_id)
        return _row_to_doc(row) if row else None

    async def delete(self, doc_id: str) -> bool:
        # chunks 表有 ON DELETE CASCADE，切片随文档一起删掉
        result = await require_pool().execute("DELETE FROM documents WHERE doc_id = $1", doc_id)
        return result.endswith(" 1")

    # ── 幂等：同一份文件重复上传 ───────────────────────────────────────
    async def find_by_sha256(self, tenant_id: str, sha256: str) -> DocumentRecord | None:
        row = await require_pool().fetchrow(
            "SELECT * FROM documents WHERE tenant_id = $1 AND file_sha256 = $2 "
            "ORDER BY created_at DESC LIMIT 1", tenant_id, sha256)
        return _row_to_doc(row) if row else None

    # ── 切片（只读；写入在 vectorstore）────────────────────────────────
    async def chunks(self, doc_id: str, limit: int | None = None) -> list[ChunkRecord]:
        cols = ", ".join(_CHUNK_COLUMNS)
        sql = (f"SELECT {cols} FROM chunks WHERE doc_id = $1 ORDER BY order_index"
               + (" LIMIT $2" if limit else ""))
        rows = await (require_pool().fetch(sql, doc_id, limit) if limit
                      else require_pool().fetch(sql, doc_id))
        return [_row_to_chunk(r) for r in rows]

    async def count(self, tenant_id: str | None = None) -> int:
        if tenant_id:
            return await require_pool().fetchval(
                "SELECT count(*) FROM documents WHERE tenant_id = $1", tenant_id)
        return await require_pool().fetchval("SELECT count(*) FROM documents")

    # ── 版本治理：新版本上线时自动把旧版本置为 superseded ───────────────
    async def supersede_previous(self, new_doc: DocumentRecord) -> list[str]:
        """返回被本次上传替代的 doc_id 列表。

        若上传的是**更旧**的版本（生效日期早于已生效版本），则把新文档自己标为 superseded，
        避免"传了一份历史版本反而把现行版本顶掉"。
        """
        pool = require_pool()
        newer = await pool.fetchrow(
            "SELECT doc_id FROM documents "
            "WHERE tenant_id = $1 AND doc_id <> $2 AND status = 'active' "
            "  AND document_title = $3 AND doc_type = $4 AND department = $5 "
            "  AND COALESCE(effective_date, DATE '0001-01-01') "
            "      > COALESCE($6::date, DATE '0001-01-01') "
            "LIMIT 1",
            new_doc.tenant_id, new_doc.doc_id, new_doc.document_title,
            new_doc.doc_type, new_doc.department, to_date(new_doc.effective_date))

        if newer:
            await pool.execute(
                "UPDATE documents SET status = 'superseded', superseded_by = $1 WHERE doc_id = $2",
                newer["doc_id"], new_doc.doc_id)
            new_doc.status = DocStatus.superseded.value
            new_doc.superseded_by = newer["doc_id"]
            return []

        rows = await pool.fetch(
            "UPDATE documents SET status = 'superseded', superseded_by = $1 "
            "WHERE tenant_id = $2 AND doc_id <> $1 AND status = 'active' "
            "  AND document_title = $3 AND doc_type = $4 AND department = $5 "
            "RETURNING doc_id",
            new_doc.doc_id, new_doc.tenant_id, new_doc.document_title,
            new_doc.doc_type, new_doc.department)
        return [r["doc_id"] for r in rows]

    # ── 维护 ──────────────────────────────────────────────────────────
    async def clear(self) -> dict:
        """清空所有文档与切片（测试/运维用）。"""
        pool = require_pool()
        counts = {"documents": await pool.fetchval("SELECT count(*) FROM documents"),
                  "chunks": await pool.fetchval("SELECT count(*) FROM chunks")}
        await pool.execute("TRUNCATE chunks, documents")
        return counts

    # ── 统计（给知识库概览用）────────────────────────────────────────
    async def stats(self, tenant_id: str, docs: list[DocumentRecord] | None = None) -> dict:
        if docs is None:
            docs = await self.list_docs(tenant_id)
        by_department: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for d in docs:
            by_department[d.department] = by_department.get(d.department, 0) + 1
            by_status[d.status] = by_status.get(d.status, 0) + 1

        indexed = len([d for d in docs if d.ingest_status == IngestStatus.indexed.value])
        return {
            "documentCount": len(docs),
            "chunkCount": sum(d.chunk_count for d in docs),
            "elementCount": sum(d.element_count for d in docs),
            "charCount": sum(d.char_count for d in docs),
            "fileBytes": sum(d.file_size for d in docs),
            "pageCount": sum(d.page_count for d in docs),
            "byDepartment": by_department,
            "byStatus": by_status,
            "indexedCount": indexed,
            "failedCount": len([d for d in docs if d.ingest_status == IngestStatus.failed.value]),
        }


registry = DocRegistry()
