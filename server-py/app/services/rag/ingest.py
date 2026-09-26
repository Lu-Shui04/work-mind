# server-py/app/services/rag/ingest.py
"""
文档入库管线：文件 → 解析 → 分片 → 向量化 → 注册索引。

状态机（每一步都写回注册表，前端可直接展示进度）：
    pending → parsing → chunking → embedding → indexed
                                      ↘ failed（记录 ingest_error）

并发/可靠性取舍（先跑通功能，后续再接企业级）：
- 当前同步执行（上传请求内完成）；因为每步状态都落库，后续把 embed 阶段搬到
  arq/Celery 队列时，前端与接口契约都不用改。
- 已做幂等：同一租户下文件内容 sha256 相同 → 直接复用已有文档，不重复向量化、不重复计费。
- 已做版本治理：新版本入库时自动把旧版本标记 superseded（见 registry.supersede_previous）。
- 源文件保留在 uploads/ 下（不再入库后删除），用于"查看源文件 / 重新解析 / 溯源下载"。
"""
import hashlib
import os
import random
import re
import time
from datetime import datetime, timezone

from app.schemas.document import (
    ChunkRecord, DocStatus, DocumentMetadata, DocumentRecord, ElementType,
    IngestStatus,
)
from app.services.db import VECTOR_DIM
from app.services.model import embeddings
from app.services.rag.parser import parse_document
from app.services.rag.registry import registry
from app.services.rag.vectorstore import get_vector_store
from app.utils.logger import logger

# 切片参数（可用环境变量覆盖；每个文档入库时会把实际参数记进元数据，便于复现与回溯）
#   TARGET  目标长度：正文按这个粒度聚合
#   MAX     硬上限：任何切片都不允许超过（防止"一篇文档一个切片"那类事故）
#   OVERLAP 重叠长度：相邻切片共享的尾部字符数，避免答案刚好被切在边界上而检索不到
TARGET_CHUNK_CHARS = int(os.getenv("RAG_CHUNK_TARGET", "500"))
MAX_CHUNK_CHARS = int(os.getenv("RAG_CHUNK_MAX", "1200"))
OVERLAP_CHARS = int(os.getenv("RAG_CHUNK_OVERLAP", "80"))
# 最小切片长度：低于这个长度的块并入相邻块。
# 为什么必须有：碎片（"**回答框架**："这种 9 字块）会挤占 TopK，
# 而且单独召回来也没有任何信息量。
MIN_CHUNK_CHARS = int(os.getenv("RAG_CHUNK_MIN", "150"))
EMBED_BATCH_SIZE = 20

# 纯装饰行（分隔线、空表格边框…）：不承载信息，直接丢弃
_NOISE_RE = re.compile(r"^[\s\-*_=~#|+.·•—…]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _split_long_text(text: str, target: int = TARGET_CHUNK_CHARS,
                     hard_limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """长单元按句末标点切分，保证不丢字，并且**绝不产生超过 hard_limit 的片段**。

    先按句末标点聚合到 ~target；如果某个"句子"本身就比 hard_limit 还长
    （例如一整段没有标点的文本、或者表格里的一行超长数据），再按字符硬切兜底。
    没有这一步，一个几万字的段落会被原样塞进一个切片（曾经真的发生过：4396 字 → 1 片）。
    """
    import re
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    pieces, cur = [], ""
    for s in sentences:
        if not s:
            continue
        if cur and len(cur) + len(s) > target:
            pieces.append(cur)
            cur = s
        else:
            cur += s
    if cur.strip():
        pieces.append(cur)

    # 兜底硬切：保证任何片段都不超过 hard_limit
    result: list[str] = []
    for p in pieces or [text]:
        while len(p) > hard_limit:
            result.append(p[:hard_limit])
            p = p[hard_limit:]
        if p:
            result.append(p)
    return result or [text[:hard_limit]]


def _overlap_tail(text: str, size: int = OVERLAP_CHARS) -> str:
    """取一片的尾部作为下一片的开头（重叠）。

    为什么要重叠：一句话被切在边界上时，"上半句"和"下半句"分别落在两个切片里，
    如果 query 命中的语义只出现在下半句，上半句那片就召不回来；重叠让边界内容在两片里都完整存在。

    细节：尽量从句末标点之后开始，保证重叠部分不是从半句话中间截断的。
    """
    if size <= 0 or not text:
        return ""
    tail = text[-size:]
    for i, ch in enumerate(tail):
        if ch in "。！？!?；;\n":
            return tail[i + 1:].strip()
    return tail.strip()


def _is_noise(text: str) -> bool:
    t = (text or "").strip()
    return len(t) < 4 or bool(_NOISE_RE.match(t))


def _el_type(el) -> str:
    return el.element_type.value if hasattr(el.element_type, "value") else str(el.element_type)


def _heading_prefix(doc: DocumentRecord, heading_path) -> str:
    """把"文档 > 章 > 节"注入切片正文开头。

    为什么值得多占这十几个字：**"这段属于哪一节"本身就是很强的检索信号**。
    问"RAG 的工作流程"时，命中切片如果开头写着
    [文档：面试手册 > 第二类：AI技术理解 > Q5：什么是RAG？]，
    向量相似度和关键词匹配都会明显更好；引用时也能直接告诉用户出自哪一节。
    """
    heads = [h for h in (heading_path or []) if h]
    # Markdown 的第一个 H1 通常就是文档标题本身，重复注入没有信息量还有噪音
    title = (doc.document_title or "").strip()
    while heads and (heads[0] == title or heads[0] in title or title in heads[0]):
        heads.pop(0)
        break
    return "[文档：" + " > ".join([title] + heads) + "]"


def _pack_section(elements: list, *, break_on_page: bool) -> list[dict]:
    """把**同一节**的元素聚合成若干 pack（每 pack 就是之后的一个切片）。

    - 单个块（表格/代码/段落）只要自身不超过 MAX_CHUNK_CHARS 就**绝不从中间切开**
      —— "不拆散"指的是不要把一个表格切成两半，而不是"表格必须独占一片"：
      表格独立成片会把"Q5 的说明文字"和"Q5 的对比表"拆到两个切片里，
      两个切片单独召回都不完整。同一节内按长度打包，块边界就是天然断点。
    - 只有长度超 TARGET 才断开，断开处按句子回填重叠（_overlap_tail 会对齐到句末）
    - 纯文本/PDF（break_on_page=True）仍在"页"边界断开：页是物理边界，
      上一页结尾和下一页开头本来就不相干（Markdown 不这样做，它的"页"只是定位锚点）
    """
    packs: list[dict] = []
    buf_texts: list[str] = []
    buf_len = 0
    buf_pages: list[int | None] = [None, None]
    buf_count = 0
    buf_type = ElementType.paragraph.value

    def note_pages(el):
        start = el.page_number
        end = getattr(el, "page_end", None) or start
        if start is None:
            return
        buf_pages[0] = start if buf_pages[0] is None else min(buf_pages[0], start)
        buf_pages[1] = end if buf_pages[1] is None else max(buf_pages[1], end)

    def flush(carry: bool = True):
        nonlocal buf_texts, buf_len, buf_pages, buf_count, buf_type
        if not buf_texts:
            return
        text = "\n\n".join(buf_texts)
        packs.append({"texts": buf_texts, "chars": len(text), "pages": tuple(buf_pages),
                      "element_count": buf_count, "element_type": buf_type})
        carry_text = _overlap_tail(text) if carry else ""
        buf_texts = [carry_text] if carry_text else []
        buf_len = len(carry_text)
        buf_pages = [None, None]
        buf_count = 0
        buf_type = ElementType.paragraph.value

    for el in elements:
        et = _el_type(el)
        # 0) 先对超长元素做句末切分，保证进入聚合的片段都不超过 MAX
        pieces = [el.text] if len(el.text) <= MAX_CHUNK_CHARS else _split_long_text(el.text)

        for piece in pieces:
            page_changed = (break_on_page and buf_texts and buf_pages[0] is not None
                            and el.page_number is not None and el.page_number != buf_pages[1])
            if buf_texts and (page_changed or buf_len + len(piece) > TARGET_CHUNK_CHARS):
                flush(carry=not page_changed)
            if not buf_count:
                buf_type = et
            buf_texts.append(piece)
            buf_len += len(piece)
            buf_count += 1
            note_pages(el)
            if buf_len >= TARGET_CHUNK_CHARS:
                flush()

    flush(carry=False)
    return packs


def _merge_small(packs: list[dict]) -> list[dict]:
    """过短的切片并入相邻切片。

    "< MIN_CHUNK_CHARS 的碎片"危害很大：它会占掉 TopK 的名额，
    而且单独召回回来没有任何信息量（实测出现过 3 字的 "---" 和 9 字的 "**回答框架**："）。
    """
    out: list[dict] = []
    pending: dict | None = None        # 开头就很短的片，攒着并入下一片
    for p in packs:
        if pending is not None:
            if pending["chars"] + p["chars"] <= MAX_CHUNK_CHARS:
                p = dict(p, texts=pending["texts"] + p["texts"],
                         chars=pending["chars"] + p["chars"],
                         element_count=pending["element_count"] + p["element_count"],
                         pages=(pending["pages"][0] or p["pages"][0],
                                max(x for x in (pending["pages"][1], p["pages"][1]) if x is not None)
                                if (pending["pages"][1] or p["pages"][1]) else None))
                pending = None
            else:
                out.append(pending)
                pending = None
        if not out and p["chars"] < MIN_CHUNK_CHARS and len(packs) > 1:
            pending = p
            continue
        if out and p["chars"] < MIN_CHUNK_CHARS and out[-1]["chars"] + p["chars"] <= MAX_CHUNK_CHARS:
            prev = out[-1]
            prev["texts"] = prev["texts"] + p["texts"]
            prev["chars"] += p["chars"]
            prev["element_count"] += p["element_count"]
            a, b = prev["pages"]
            c, d = p["pages"]
            prev["pages"] = (min(x for x in (a, c) if x is not None) if (a or c) else None,
                             max(x for x in (b, d) if x is not None) if (b or d) else None)
            continue
        out.append(dict(p))
    if pending is not None:
        out.append(pending)          # 只有一片时也得留着，别把内容弄丢
    return out


def build_chunks(doc: DocumentRecord, elements: list) -> list[ChunkRecord]:
    """结构单元 → 切片。

    顺序本身就是设计（别调换）：
      0. 丢掉装饰性/空块（"---" 曾经切出过 3 字的空切片）
      1. **按 heading_path 分组**：同一节的内容必须落在同一片里 ——
         实测一篇 Markdown 手册里 "Q5：什么是RAG？" 的 标题/段落/表格/代码
         被拆成 5 片，任何一片单独召回都不完整（标题那片只有 26 字）
      2. 节内贪心聚合到 ~TARGET，表格/代码不拆散
      3. 过短的片并入相邻片（见 _merge_small）
      4. 每片开头注入 [文档 > 章 > 节]
    """
    items = [el for el in elements if el.text and not _is_noise(el.text)]
    # Markdown 的元素带 heading_path；纯文本/PDF 不带 —— 用它判断要不要按"页"断开
    break_on_page = not any(getattr(el, "heading_path", None) for el in items)

    # 1) 按章节分组：连续的、heading_path 相同的块属于同一节
    sections: list[dict] = []
    for el in items:
        key = tuple(getattr(el, "heading_path", None) or ())
        if sections and sections[-1]["key"] == key:
            sections[-1]["elements"].append(el)
        else:
            sections.append({"key": key, "elements": [el]})

    # 2) 节内聚合 + 3) 最小块合并
    packs: list[dict] = []
    for sec in sections:
        tagged = _pack_section(sec["elements"], break_on_page=break_on_page)
        for p in tagged:
            p["heading_path"] = list(sec["key"])
        packs.extend(tagged)
    packs = _merge_small(packs)

    # 4) 生成切片：编号 + 注入标题路径
    chunks: list[ChunkRecord] = []
    for i, p in enumerate(packs):
        body = "\n\n".join(p["texts"])
        text = _heading_prefix(doc, p["heading_path"]) + "\n" + body
        chunks.append(_make_chunk(
            doc, text, i,
            {"page_number": p["pages"][0], "page_end": p["pages"][1],
             "element_type": p["element_type"], "heading_path": p["heading_path"]},
            element_count=p["element_count"],
        ))
    logger.info("rag: chunks built", {
        "docId": doc.doc_id, "elements": len(elements), "sections": len(sections),
        "chunks": len(chunks),
        "avgChars": round(sum(c.char_count for c in chunks) / len(chunks)) if chunks else 0,
        "minChars": min((c.char_count for c in chunks), default=0),
        "maxChars": max((c.char_count for c in chunks), default=0),
    })
    return chunks


def _make_chunk(doc: DocumentRecord, text: str, order_index: int, meta: dict,
                element_count: int = 1) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{doc.doc_id}_c{order_index:04d}",
        doc_id=doc.doc_id,
        tenant_id=doc.tenant_id,
        order_index=order_index,
        # 过滤字段快照：检索时不用回表就能做权限/版本下推
        department=doc.department,
        version=doc.version,
        doc_type=doc.doc_type,
        security_level=doc.security_level,
        page_number=meta.get("page_number"),
        page_end=meta.get("page_end") or meta.get("page_number"),
        element_type=meta.get("element_type", ElementType.paragraph.value),
        element_count=element_count,
        heading_path=meta.get("heading_path") or [],
        text=text,
        char_count=len(text),
    )


async def ingest_document(
    *,
    file_path: str,
    file_name: str,
    file_type: str,
    file_size: int,
    source_path: str,
    file_sha256: str,
    metadata: DocumentMetadata,
    user,
    force: bool = False,
) -> tuple[DocumentRecord, bool]:
    """执行入库。返回 (文档记录, 是否为重复文件命中幂等)。

    force=True 时跳过内容幂等复用（测试时同一份文件想按不同元数据各存一版）。
    注意：路由层也有一道同样的检查，两处都必须尊重 force，否则会"看着传了新版本、
    实际又被复用成旧文档"。
    """
    # 0) 幂等：同一租户 + 同一文件内容 → 直接复用
    # 幂等只对"真正入库成功"的文档生效：
    # 之前的实现只要 sha256 相同就复用，结果**入库失败的文档会被永久复用**，
    # 用户以为"重新传一次就好了"，其实索引一直没建起来（检索永远 0 条）。
    existing = None if force else await registry.find_by_sha256(user.tenant_id, file_sha256)
    if existing and existing.ingest_status == IngestStatus.indexed.value:
        logger.info("rag: duplicate file, reuse existing", {"docId": existing.doc_id, "sha256": file_sha256[:12]})
        return existing, True
    if existing:
        logger.info("rag: 同内容文档尚未入库成功，重新建索引",
                    {"docId": existing.doc_id, "status": existing.ingest_status})

    started = time.time()
    doc = DocumentRecord(
        doc_id=f"doc_{int(time.time() * 1000)}_{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=4))}",
        tenant_id=user.tenant_id,
        document_title=metadata.document_title,
        department=metadata.department.value,
        version=metadata.version,
        effective_date=metadata.effective_date,
        expired_date=metadata.expired_date,
        doc_type=metadata.doc_type.value,
        security_level=metadata.security_level.value,
        owner=metadata.owner,
        tags=metadata.tags,
        remark=metadata.remark,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        source_path=source_path,
        file_sha256=file_sha256,
        created_by=user.user_id,
        created_by_name=user.name,
        created_at=_now_iso(),
        ingest_status=IngestStatus.pending.value,
    )
    await registry.add(doc)

    try:
        # 1) 解析
        await registry.update(doc.doc_id, ingest_status=IngestStatus.parsing.value)
        parsed = parse_document(file_path, file_type)
        if not parsed.elements:
            raise ValueError("文档解析后没有可用内容（可能是扫描件或空文件）")

        await registry.update(
            doc.doc_id,
            page_count=parsed.page_count,
            language=parsed.language,
            parser_name=parsed.parser_name,
            parse_time_ms=parsed.parse_time_ms,
            element_count=len(parsed.elements),
            char_count=parsed.char_count,
            preview=(parsed.elements[0].text[:120].replace("\n", " ") + "..."),
            ingest_status=IngestStatus.chunking.value,
        )

        # 2) 分片
        chunks = build_chunks(doc, parsed.elements)
        if not chunks:
            raise ValueError("文档分片结果为空")

        # 3) 向量化（分批，失败可从当前批次重试）
        await registry.update(
            doc.doc_id,
            ingest_status=IngestStatus.embedding.value,
            chunk_count=len(chunks),
            chunk_target=TARGET_CHUNK_CHARS,
            chunk_max=MAX_CHUNK_CHARS,
            chunk_overlap=OVERLAP_CHARS,
        )
        logger.info("rag: chunk config", {
            "docId": doc.doc_id, "chunks": len(chunks),
            "target": TARGET_CHUNK_CHARS, "max": MAX_CHUNK_CHARS, "overlap": OVERLAP_CHARS,
        })
        if not embeddings:
            raise ValueError("未配置 ZHIPU_API_KEY / OPENAI_API_KEY，无法向量化")

        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i:i + EMBED_BATCH_SIZE]
            vectors = await embeddings.aembed_documents([c.text for c in batch])
            for chunk, vec in zip(batch, vectors):
                chunk.embedding = vec
            logger.info("rag: embedding progress", {"docId": doc.doc_id, "done": min(i + EMBED_BATCH_SIZE, len(chunks)), "total": len(chunks)})

        # 4) 维度校验：换 embedding 模型后维度会变（智谱 embedding-3 = 2048），
        #    与表的向量列不一致时给出可行动的报错，而不是让 pgvector 抛"expected N dimensions"
        dims = {len(c.embedding) for c in chunks if c.embedding}
        if dims and dims != {VECTOR_DIM}:
            raise ValueError(
                f"向量维度 {sorted(dims)} 与数据库向量列维度 {VECTOR_DIM} 不一致："
                f"换过 embedding 模型时，需要按新维度重建 chunks.embedding 列（PGVECTOR_DIM）并重新入库"
            )

        # 5) 注册 + 版本治理
        # chunks 表既是切片存储也是向量索引：只在这里写一次（registry 侧只读），避免双写
        await get_vector_store().add(chunks)
        superseded = await registry.supersede_previous(doc)
        if superseded:
            logger.info("rag: previous versions superseded", {"docId": doc.doc_id, "superseded": superseded})

        await registry.update(
            doc.doc_id,
            ingest_status=IngestStatus.indexed.value,
            indexed_at=_now_iso(),
            ingest_time_ms=round((time.time() - started) * 1000, 2),
        )
        # 重新读一次：前面所有 update 都写在数据库里，内存里的 doc 对象还是初始值
        # （不重新读的话，接口返回的 chunk_count=0 / ingest_status=pending，前端会显示"0 个切片"）
        doc = await registry.get(doc.doc_id) or doc
        logger.info("rag: ingest done", {
            "docId": doc.doc_id, "chunks": len(chunks), "pages": parsed.page_count,
            "status": doc.status, "ms": doc.ingest_time_ms,
        })
        return doc, False

    except Exception as err:
        await registry.update(
            doc.doc_id,
            ingest_status=IngestStatus.failed.value,
            ingest_error=str(err),
            ingest_time_ms=round((time.time() - started) * 1000, 2),
        )
        logger.error("rag: ingest failed", {"docId": doc.doc_id, "error": str(err)})
        raise


async def delete_document(doc_id: str, user) -> DocumentRecord:
    doc = await registry.get(doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise ValueError("文档不存在")
    # chunks 有 ON DELETE CASCADE，切片随文档一起被数据库删掉
    await registry.delete(doc_id)
    try:
        if doc.source_path and os.path.exists(doc.source_path):
            os.unlink(doc.source_path)
    except OSError:
        pass
    logger.info("rag: document deleted", {"docId": doc_id, "by": user.user_id})
    return doc


async def reindex_document(doc_id: str, user) -> DocumentRecord:
    """重新解析入库（换解析器或 embedding 模型后使用）。"""
    doc = await registry.get(doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise ValueError("文档不存在")
    if not doc.source_path or not os.path.exists(doc.source_path):
        raise ValueError("源文件已不存在，无法重建索引")

    await get_vector_store().remove_by_doc(doc_id)
    await registry.update(doc_id, ingest_status=IngestStatus.pending.value,
                          ingest_error=None, chunk_count=0)

    parsed = parse_document(doc.source_path, doc.file_type)
    await registry.update(
        doc_id, page_count=parsed.page_count, language=parsed.language,
        parser_name=parsed.parser_name, parse_time_ms=parsed.parse_time_ms,
        element_count=len(parsed.elements), char_count=parsed.char_count,
    )
    chunks = build_chunks(doc, parsed.elements)
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i:i + EMBED_BATCH_SIZE]
        vectors = await embeddings.aembed_documents([c.text for c in batch])
        for chunk, vec in zip(batch, vectors):
            chunk.embedding = vec
    await get_vector_store().add(chunks)
    await registry.update(doc_id, chunk_count=len(chunks), ingest_status=IngestStatus.indexed.value,
                    indexed_at=_now_iso(), ingest_error=None,
                    chunk_target=TARGET_CHUNK_CHARS, chunk_max=MAX_CHUNK_CHARS,
                    chunk_overlap=OVERLAP_CHARS)
    logger.info("rag: reindex done", {"docId": doc_id, "chunks": len(chunks)})
    # 同 ingest：更新都落在数据库里，返回前重新读一次，避免接口返回过期快照
    return await registry.get(doc_id) or doc
