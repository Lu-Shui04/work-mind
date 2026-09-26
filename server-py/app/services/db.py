# server-py/app/services/db.py
"""
PostgreSQL + pgvector 连接池与建表 —— 知识库的唯一持久化层。

为什么必须换掉内存实现（这次踩到的坑）：
- 之前 documents / chunks 都放在进程内 dict 里，**服务一重启知识库就空了**。
  表现和"权限不对"一模一样：库里明明有文档，检索却 0 条，且没有任何日志能看出来。
- 上传成功后用户以为"资料已经入库了"，但只要容器重建（docker compose up --build、
  改一行代码、机器重启），索引就没了，而 uploads/ 里的源文件还在 —— 误导性极强。

设计取舍：
- 直接用 asyncpg + SQL，不引入 ORM：检索的核心是"过滤下推 + pgvector 排序"，
  用 SQL 写出来最直观，也最接近换成 pgvector 后应有的形态（见 query.py 的注释）。
- **过滤条件全部下推到 SQL 的 WHERE**，保证"先过滤再算相似度"，
  不会出现"TopK 全被权限过滤掉 → 明明有权限内文档却答不出来"。
- 连接池在 FastAPI startup 里创建（asyncpg 的池必须绑定到正在运行的事件循环）。
- 建表 DDL 幂等，启动时执行；数据库连不上时应用仍能启动，但知识库接口会
  **明确报错**（StorageUnavailable），而不是静默返回空结果。
"""
from __future__ import annotations

import os
from datetime import date, datetime

import asyncpg

from app.utils.logger import logger

# 向量维度：智谱 embedding-3 = 2048。换 embedding 模型时必须同步改这里并重新入库。
VECTOR_DIM = int(os.getenv("PGVECTOR_DIM", "2048"))


class StorageUnavailable(RuntimeError):
    """数据库不可用 —— 知识库功能整体不可用（区别于"检索没命中"）。"""


def database_url() -> str | None:
    """连接串：优先 DATABASE_URL，其次 PG* 一组环境变量；都没有则返回 None。"""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("PGHOST")
    if not host:
        return None
    user = os.getenv("PGUSER", "workmind")
    password = os.getenv("PGPASSWORD", "")
    port = os.getenv("PGPORT", "5432")
    name = os.getenv("PGDATABASE", "workmind")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{name}"


def _safe_dsn(url: str | None) -> str:
    """日志里不打密码。"""
    if not url:
        return "(未配置)"
    if "@" in url:
        head, tail = url.split("@", 1)
        scheme = head.split("://", 1)[0] if "://" in head else "postgresql"
        return f"{scheme}://***@{tail}"
    return url


# 建表语句逐条执行（而不是一整个多语句块）：
# 任何一条失败都不会把前面已经建好的表一起回滚掉。
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    document_title  TEXT NOT NULL,
    department      TEXT NOT NULL,
    version         TEXT NOT NULL,
    effective_date  DATE,
    expired_date    DATE,
    doc_type        TEXT NOT NULL,
    security_level  TEXT NOT NULL,
    owner           TEXT,
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    remark          TEXT,
    file_name       TEXT NOT NULL DEFAULT '',
    file_type       TEXT NOT NULL DEFAULT '',
    file_size       BIGINT NOT NULL DEFAULT 0,
    source_path     TEXT NOT NULL DEFAULT '',
    file_sha256     TEXT NOT NULL,
    page_count      INTEGER NOT NULL DEFAULT 0,
    language        TEXT NOT NULL DEFAULT 'unknown',
    parser_name     TEXT NOT NULL DEFAULT '',
    parse_time_ms   DOUBLE PRECISION NOT NULL DEFAULT 0,
    element_count   INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    char_count      INTEGER NOT NULL DEFAULT 0,
    chunk_target    INTEGER NOT NULL DEFAULT 0,
    chunk_max       INTEGER NOT NULL DEFAULT 0,
    chunk_overlap   INTEGER NOT NULL DEFAULT 0,
    ingest_status   TEXT NOT NULL DEFAULT 'pending',
    ingest_error    TEXT,
    ingest_time_ms  DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL DEFAULT '',
    created_by_name TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at      TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'active',
    superseded_by   TEXT,
    preview         TEXT NOT NULL DEFAULT ''
)
""",
    "CREATE INDEX IF NOT EXISTS idx_documents_tenant ON documents (tenant_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_documents_sha    ON documents (tenant_id, file_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_documents_group  ON documents (tenant_id, document_title, doc_type, department)",
    f"""
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    tenant_id      TEXT NOT NULL,
    order_index    INTEGER NOT NULL,
    department     TEXT NOT NULL,
    version        TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    security_level TEXT NOT NULL,
    page_number    INTEGER,
    page_end       INTEGER,
    element_type   TEXT NOT NULL DEFAULT 'paragraph',
    element_count  INTEGER NOT NULL DEFAULT 1,
    heading_path   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    text           TEXT NOT NULL,
    char_count     INTEGER NOT NULL DEFAULT 0,
    embedding      vector({VECTOR_DIM})
)
""",
    # 老库升级：切片新增"章节路径"列（引用可回溯到哪一章哪一节）
    "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS heading_path TEXT[] NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc    ON chunks (doc_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_tenant ON chunks (tenant_id)",
)


async def _create_vector_index(conn) -> str | None:
    """建向量索引（可选）。默认不开，原因见下。

    pgvector 对 vector 类型的 HNSW 索引**上限是 2000 维**，而智谱 embedding-3 是 2048 维，
    所以直接建索引会报 "column cannot have more than 2000 dimensions for hnsw index"，
    并且会把同一批 DDL 里的建表语句一起带崩（这也是把它拆成独立语句的原因）。
    可选方案：
      1) 不开索引：走精确检索（顺序扫描 + <=>），几万条切片以内完全够用 —— 默认
      2) 用 halfvec(2048)：支持到 4000 维，代价是半精度（2 字节/维）与查询要写成
         ORDER BY embedding::halfvec({VECTOR_DIM}) <=> $1::halfvec({VECTOR_DIM})
      3) 换用支持降维的 embedding 参数（如 1024 维）后重建列，再建索引
    需要时用 PGVECTOR_INDEX=hnsw 显式打开（只对 ≤2000 维生效）。
    """
    mode = os.getenv("PGVECTOR_INDEX", "none").lower()
    if mode != "hnsw":
        return None
    if VECTOR_DIM > 2000:
        logger.warn("db: 向量维度超过 HNSW 上限，跳过索引（走精确检索）",
                    {"dim": VECTOR_DIM, "limit": 2000,
                     "hint": "可用 halfvec 或降低 embedding 维度后再建索引"})
        return None
    sql = ("CREATE INDEX IF NOT EXISTS idx_chunks_hnsw ON chunks "
           "USING hnsw (embedding vector_cosine_ops)")
    await conn.execute(sql)
    return "hnsw"


_pool: asyncpg.Pool | None = None


async def init_db() -> bool:
    """建立连接池并建表。返回是否成功；失败只打日志，不阻止应用启动。"""
    global _pool
    url = database_url()
    if not url:
        logger.error("db: 未配置 DATABASE_URL / PGHOST，知识库将不可用", {})
        return False
    try:
        _pool = await asyncpg.create_pool(
            dsn=url,
            min_size=int(os.getenv("PG_POOL_MIN", "1")),
            max_size=int(os.getenv("PG_POOL_MAX", "10")),
            command_timeout=float(os.getenv("PG_COMMAND_TIMEOUT", "30")),
            max_inactive_connection_lifetime=300,
        )
        async with _pool.acquire() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                await conn.execute(stmt)
            index_mode = await _create_vector_index(conn)
            version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            dim = await conn.fetchval(
                "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        logger.info("db: ready", {
            "dsn": _safe_dsn(url), "pgvector": version,
            "vectorDim": dim if dim and dim > 0 else None, "configuredDim": VECTOR_DIM,
            "backend": "postgres", "vectorIndex": index_mode or "exact-scan",
        })
        return True
    except Exception as err:
        _pool = None
        logger.error("db: 初始化失败，知识库不可用", {"dsn": _safe_dsn(url), "error": str(err)})
        return False


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("db: pool closed", {})


def get_pool() -> asyncpg.Pool | None:
    return _pool


def is_ready() -> bool:
    return _pool is not None


def require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise StorageUnavailable(
            "知识库数据库未连接（PostgreSQL/pgvector 不可用）。"
            "请确认 db 服务已启动且 DATABASE_URL 正确。"
        )
    return _pool


def storage_info() -> dict:
    return {
        "backend": "postgres+pgvector" if _pool is not None else "unavailable",
        "ready": _pool is not None,
        "dsn": _safe_dsn(database_url()),
        "vectorDim": VECTOR_DIM,
    }


async def ping() -> bool:
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


# ── 类型转换工具（asyncpg 只认 Python 原生类型，不认 ISO 字符串）─────────
def to_date(value: str | None) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def to_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def date_str(value) -> str | None:
    return value.isoformat() if isinstance(value, date) else (str(value) if value else None)


def ts_str(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else "")
