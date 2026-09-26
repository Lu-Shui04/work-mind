# server-py/app/services/rag/vectorstore.py
"""
向量库 —— PostgreSQL + pgvector 实现（chunks 表既是切片存储，也是向量索引）。

关键设计（别改）：**过滤下推**
搜索时 SQL 把权限/版本条件写进 WHERE，由数据库先剔掉不可见的切片，
再按 pgvector 的余弦距离排序取 TopK，而不是"先向量检索 TopK，再在结果里过滤"
—— 后者会出现"TopK 全被过滤掉 → 明明库里有权限内文档却答不出来"的经典问题。

切片写入也在这里（registry 只读）：一张表只由一处写，避免双写不一致。
"""
from __future__ import annotations

from app.schemas.document import ChunkRecord
from app.services.db import require_pool
from app.services.trace import trace_step
from app.utils.logger import logger

_INSERT_COLUMNS = ("chunk_id", "doc_id", "tenant_id", "order_index", "department", "version",
                   "doc_type", "security_level", "page_number", "page_end",
                   "element_type", "element_count", "heading_path", "text", "char_count",
                   "embedding")

_SELECT_COLUMNS = ("c.chunk_id, c.doc_id, c.tenant_id, c.order_index, c.department, c.version, "
                   "c.doc_type, c.security_level, c.page_number, c.page_end, "
                   "c.element_type, c.element_count, c.heading_path, c.text, c.char_count")


def vector_literal(vec: list[float]) -> str:
    """pgvector 的文本字面量：'[0.1,0.2,...]'（配合 $n::vector 使用）。"""
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"


def _row_to_chunk(row) -> ChunkRecord:
    data = dict(row)
    data.pop("score", None)
    return ChunkRecord(**data)


class PgVectorStore:
    async def add(self, chunks: list[ChunkRecord]) -> None:
        """写入/覆盖一批切片。同一 doc_id 先删后插，保证重建索引时不残留旧切片。"""
        if not chunks:
            return
        pool = require_pool()
        doc_ids = sorted({c.doc_id for c in chunks})
        cols = ", ".join(_INSERT_COLUMNS)
        marks = ", ".join(f"${i + 1}" for i in range(len(_INSERT_COLUMNS) - 1)) + f", ${len(_INSERT_COLUMNS)}::vector"
        sql = f"INSERT INTO chunks ({cols}) VALUES ({marks})"
        rows = [
            (c.chunk_id, c.doc_id, c.tenant_id, c.order_index, c.department, c.version,
             c.doc_type, c.security_level, c.page_number, c.page_end,
             c.element_type, c.element_count, list(c.heading_path or []), c.text, c.char_count,
             vector_literal(c.embedding) if c.embedding else None)
            for c in chunks
        ]
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM chunks WHERE doc_id = ANY($1::text[])", doc_ids)
                await conn.executemany(sql, rows)

    async def remove_by_doc(self, doc_id: str) -> int:
        result = await require_pool().execute("DELETE FROM chunks WHERE doc_id = $1", doc_id)
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return 0

    async def count(self, tenant_id: str | None = None) -> int:
        if tenant_id:
            return await require_pool().fetchval(
                "SELECT count(*) FROM chunks WHERE tenant_id = $1", tenant_id)
        return await require_pool().fetchval("SELECT count(*) FROM chunks")

    async def clear(self) -> int:
        before = await self.count()
        await require_pool().execute("TRUNCATE chunks")
        return before

    async def search(self, query_vec: list[float], *, where_sql: str, params: list,
                     k: int = 4) -> tuple[list[tuple[ChunkRecord, float]], dict]:
        """带过滤的向量检索。

        params 是 where_sql 里依次用到的参数（$1..$n），
        本函数把"查询向量"和 k 追加在后面，因此调用方只需拼 where，不用管序号。
        """
        pool = require_pool()
        vec_ref = f"${len(params) + 1}"
        p_vec = list(params) + [vector_literal(query_vec)]
        k_ref = f"${len(p_vec) + 1}"
        p_all = p_vec + [k]

        join = "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id"
        diag_sql = (f"SELECT count(*) AS candidates, max(1 - (c.embedding <=> {vec_ref}::vector)) AS best "
                    f"{join} WHERE {where_sql}")
        hit_sql = (f"SELECT {_SELECT_COLUMNS}, 1 - (c.embedding <=> {vec_ref}::vector) AS score "
                   f"{join} WHERE {where_sql} ORDER BY c.embedding <=> {vec_ref}::vector LIMIT {k_ref}")

        async with pool.acquire() as conn:
            diag = await conn.fetchrow(diag_sql, *p_vec)
            rows = await conn.fetch(hit_sql, *p_all)

        results = [(_row_to_chunk(r), float(r["score"])) for r in rows]
        meta = {
            "candidates": int(diag["candidates"] or 0),
            "bestScore": round(float(diag["best"]), 4) if diag["best"] is not None else None,
        }
        # 全链路追踪：把"过了过滤还剩多少条、最高分多少、取回的 TopK 是谁"记下来。
        # 排查"明明库里有却答不出来"时，就是靠这几个数区分
        # "候选被过滤没了" / "分数不够" / "取回来的不是那一条"。
        trace_step("vector_search", "向量检索（pgvector）", detail={
            "where": where_sql,
            "k": k,
            "candidates": meta["candidates"],
            "bestScore": meta["bestScore"],
            "topHits": [
                {"chunkId": c.chunk_id, "score": round(s, 4), "docId": c.doc_id,
                 "chars": len(c.text or ""), "preview": (c.text or "")[:120]}
                for c, s in results
            ],
        })
        logger.info("rag: vector search", {"candidates": meta["candidates"],
                                           "best": meta["bestScore"], "k": k})
        return results, meta


_store: PgVectorStore | None = None


def get_vector_store() -> PgVectorStore:
    """全局单例（换成别的向量库时只改这里）。"""
    global _store
    if _store is None:
        _store = PgVectorStore()
    return _store
