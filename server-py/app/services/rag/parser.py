# server-py/app/services/rag/parser.py
"""
结构化解析器：文件 → 结构单元（ParsedElement）列表。

【为什么要"结构化"而不是按字数切】
- 按 500 字硬切会把标题和正文切在一起、把表格拦腰截断，命中后无法引用，
  也回答不了"这条规定出自哪一页/哪一节"。
- 先把文档解析成 标题 / 段落 / 列表 / 表格 / 代码块 这些**结构单元**，
  再以单元为原子单位聚合切片，才能保证：
    · 标题独立成块（章节级检索）
    · 表格整体成块（结构化问答可整表召回）
    · 每个切片都能带页码区间 / 元素类型 / 顺位（引用可定位）

【跨页续写处理】（v2 新增，解决"正文/表格被页码切断"）
按页解析会把"一段话跨到下一页""表格跨页续行"切成两个单元。所以解析分两步：
  1) 先按行切块，并记录每一块的 page_start / page_end
  2) 再做一次**续写合并**：
     · 相邻页 + 上一块结尾没有句末标点 + 下一块是正文 → 判定为被截断的同一段，合并
     · 相邻页 + 两块都是表格 → 合并，并去掉重复的表头行
     · 下一块是标题 → 不合并（标题本身就是章节边界）
  合并后 page_start/page_end 形成区间（如第 3-4 页），引用与前端都能精确标注。

解析器版本化：parser_name + parse_time_ms 写进文档元数据，
换解析器（例如接 MinerU / pdfplumber 坐标聚类）后可用同一批文档做回归对比。

【已知局限，别当成"已解决"】
- PDF 走 pypdf 的纯文本抽取，**没有版面坐标**：真正的"表格"只有文本里带分隔符
  （竖线/制表符）时才能识别出来，否则表格会被当成普通段落。
  要彻底解决需要版面感知的解析器（pdfplumber 坐标聚类 / MinerU / unstructured）。
- 跨页英文断词（word- 换行接 word）用"连字符 + 小写字母开头"的启发式处理，
  并非 100% 准确。

支持格式：.pdf（按真实页码）/ .md（Markdown 结构）/ .txt（段落结构）
"""
import re
import time

from app.schemas.document import ElementType, ParsedDocument, ParsedElement

# 非分页格式（md/txt）的"伪分页"粒度：每 2000 字算一页，
# 给无页码文档也提供稳定的定位锚点。
PSEUDO_PAGE_CHARS = 2000

# 句末标点：上一块如果以这些字符结尾，说明它是"说完了"，不是被截断
SENTENCE_END = "。！？!?；;…）)】」》”\"'"

# 单行超过这个长度就在句末标点处拆成多行再处理。
# 现实中很常见：纯文本文件整篇一行、PDF 抽取出来的长行。
# 不拆的话"按句末断段"的规则永远触发不了，整篇会变成一个巨大段落
# （真实故障：一份 .txt 因为没有空行，4396 字 → 1 个元素 → 1 个切片）。
LONG_LINE_CHARS = 400

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
# PDF 没有 Markdown 标记，只能用"看起来像标题"的强模式来识别。
# 【教训】早期版本用"短行 + 无句末标点 = 标题"，结果把"住宿费用标准为每晚不超过"
# 这种被页码截断的正文行误判成标题，既破坏了上下文，也让跨页续写无法合并。
# 所以这里只认：第X条/一、/1.2 编号/Chapter N/全大写英文标题 这些强模式。
_PDF_HEADING_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百零\d]+[条款章节]"
    r"|[一二三四五六七八九十]+、"
    r"|\d+(?:\.\d+)*[\.、]?\s+\S"
    r"|(?:Chapter|Section|Appendix|Part)\s+[\dA-Za-z]+"
    r"|[A-Z][A-Z0-9 \-]{3,40})$"
)
# PDF 无空行：用句末标点判定"一句话说完了"，行尾有这些标点就结束当前段落。
# 这样既能把不同段落分开，又不会把跨页截断的半句话切断。
_PDF_SENTENCE_END_RE = re.compile(r"[。！？!?]$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE_RE = re.compile(r"^\s*\x60\x60\x60")
# Markdown 水平分隔线（--- / *** / ___）：纯装饰，不承载信息，必须丢掉，
# 否则会切出一堆内容为 "---" 的 3 字切片（实测真实发生过）
_MD_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


# ── 语言识别（写进元数据，便于后续切换分语言检索/分词策略）──────────
def detect_language(text: str) -> str:
    if not text:
        return "unknown"
    sample = text[:4000]
    cjk = len(_CJK_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    total = cjk + latin
    if total == 0:
        return "unknown"
    ratio = cjk / total
    if ratio >= 0.3:
        return "zh"
    if ratio <= 0.05:
        return "en"
    return "mixed"


# ── 第一步：按行切块，并记录页码区间 ──────────────────────────────────
def _blocks_with_pages(entries: list[tuple[int, str]], *, plain_mode: bool = False) -> list[dict]:
    """entries: [(page_no, line)]。返回 [{element_type, text, page_start, page_end}]"""
    # 纯文本/PDF 预处理：把超长行按句子拆开，否则"句末断段"永远不会触发
    if plain_mode:
        expanded: list[tuple[int, str]] = []
        for page_no, raw in entries:
            if len(raw.strip()) > LONG_LINE_CHARS:
                for piece in _split_by_sentence(raw.strip(), LONG_LINE_CHARS):
                    expanded.append((page_no, piece))
            else:
                expanded.append((page_no, raw))
        entries = expanded

    blocks: list[dict] = []
    buf: list[tuple[int, str]] = []
    buf_type: ElementType | None = None
    in_code = False

    def flush():
        nonlocal buf, buf_type
        if buf:
            text = "\n".join(t for _, t in buf).strip()
            if text:
                blocks.append({
                    "element_type": (buf_type or ElementType.paragraph).value,
                    "text": text,
                    "page_start": buf[0][0],
                    "page_end": buf[-1][0],
                })
        buf, buf_type = [], None

    def push(element_type: ElementType, page_no: int, text: str):
        blocks.append({"element_type": element_type.value, "text": text.strip(),
                       "page_start": page_no, "page_end": page_no})

    for page_no, raw in entries:
        line = raw.rstrip()
        stripped = line.strip()

        # 【关键】缓冲区分页断开：块本身绝不跨页。
        # 否则"下一页的表格行/段落行"会被直接追加进上一页的缓冲区，
        # 导致重复表头混进表格中间、两页不相干的正文粘成一块。
        # 跨页续写一律交给 _merge_continuations 用明确的规则判断。
        if buf and buf[-1][0] != page_no:
            flush()

        # 代码围栏：围栏之间整体作为一个单元
        if _FENCE_RE.match(line):
            if in_code:
                flush()
                in_code = False
            else:
                flush()
                in_code = True
                buf_type = ElementType.code
            continue
        if in_code:
            buf.append((page_no, line))
            continue

        if not stripped:                      # 空行 → 结束当前单元
            flush()
            continue

        m = _HEADING_RE.match(line)
        if m:                                 # Markdown 标题
            flush()
            push(ElementType.title, page_no, m.group(2))
            continue

        if _TABLE_ROW_RE.match(line):         # 表格：连续行合并
            if buf_type is not ElementType.table:
                flush()
                buf_type = ElementType.table
            buf.append((page_no, stripped))
            continue

        if _LIST_RE.match(line):              # 列表：连续项合并
            if buf_type is not ElementType.list:
                flush()
                buf_type = ElementType.list
            buf.append((page_no, stripped))
            continue

        # 纯文本/PDF：只认强模式的标题（见 _PDF_HEADING_RE 注释里的教训）
        if plain_mode and len(stripped) <= 40 and _PDF_HEADING_RE.match(stripped):
            flush()
            push(ElementType.title, page_no, stripped)
            continue

        if buf_type not in (None, ElementType.paragraph):
            flush()
        buf_type = ElementType.paragraph
        buf.append((page_no, stripped))

        # 纯文本/PDF 常常没有空行分隔段落：一行以句末标点收尾就认为这一段结束。
        # （Markdown 不走这条：它靠空行分段，句末断段会把一个自然段拆成很多小块）
        # 注意：跨页被截断的半句话不会命中这里，会在 _merge_continuations 里被接回去。
        if plain_mode and _PDF_SENTENCE_END_RE.search(stripped) and len(stripped) >= 6:
            flush()

    flush()
    return blocks


def _md_blocks(pages: list[str]) -> list[dict]:
    """Markdown：按**标题树**解析，只产出内容块，标题不单独成块。

    标题只做两件事：
      1) 维护 heading_stack（H1→H6），其文本成为后续内容块的 heading_path；
      2) 作为块边界（遇到任何标题都结束当前块）。
    这样一个"## Q5：什么是RAG？"不会变成只有标题的 26 字切片
    （那种切片检索出来没有任何信息量，还会挤掉真正有内容的块），
    而它下面的段落/表格/代码会带着 [文档 > 章 > 节] 的路径聚合到一起。

    同一路径的正文块在切块阶段按 heading_path 分组聚合（见 ingest.build_chunks）。
    """
    blocks: list[dict] = []
    stack: list[tuple[int, str]] = []          # [(level, title)]
    buf: list[tuple[int, str]] = []
    buf_type: ElementType | None = None
    in_code = False

    def flush():
        nonlocal buf, buf_type
        if buf:
            text = "\n".join(t for _, t in buf).strip()
            if text:
                blocks.append({
                    "element_type": (buf_type or ElementType.paragraph).value,
                    "text": text,
                    "page_start": buf[0][0],
                    "page_end": buf[-1][0],
                    "heading_path": [h for _, h in stack],
                })
        buf, buf_type = [], None

    for page_no, text in enumerate(pages, start=1):
        for raw in (text or "").splitlines():
            line = raw.rstrip()
            stripped = line.strip()

            if buf and buf[-1][0] != page_no:
                flush()                        # 块不跨页（伪分页只作定位锚点）

            if _FENCE_RE.match(line):           # 代码围栏：整段作为一个单元
                if in_code:
                    flush()
                    in_code = False
                else:
                    flush()
                    in_code = True
                    buf_type = ElementType.code
                continue
            if in_code:
                buf.append((page_no, line))
                continue

            m = _HEADING_RE.match(line)
            if m:                               # 标题：更新标题栈，不产出块
                flush()
                level, title = len(m.group(1)), m.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                continue

            if not stripped or _MD_HR_RE.match(line):
                flush()                         # 空行 / 分隔线：结束当前块
                continue

            if _TABLE_ROW_RE.match(line):       # 表格：连续行合并，绝不拆散
                if buf_type is not ElementType.table:
                    flush()
                    buf_type = ElementType.table
                buf.append((page_no, stripped))
                continue

            if _LIST_RE.match(line):            # 列表：连续项合并
                if buf_type is not ElementType.list:
                    flush()
                    buf_type = ElementType.list
                buf.append((page_no, stripped))
                continue

            if buf_type not in (None, ElementType.paragraph):
                flush()
            buf_type = ElementType.paragraph
            buf.append((page_no, stripped))

    flush()
    return blocks


# ── 第二步：跨页续写合并 ──────────────────────────────────────────────
def _join_wrapped(a: str, b: str) -> str:
    """把被页码/换行截断的两段正文接回去，避免中英文粘连或多余空格。"""
    a = a.rstrip()
    b = b.lstrip()
    if not a:
        return b
    if not b:
        return a
    if a.endswith("-") and b[:1].islower():
        # 英文单词被跨页拆开：去掉连字符直接接
        return a[:-1] + b
    # 中文接中文：直接拼；否则补一个空格（英文句子/数字之间）
    if _CJK_RE.match(a[-1]) and _CJK_RE.match(b[0]):
        return a + b
    return a + " " + b


def _is_continuation(prev: dict, cur: dict) -> bool:
    # 只合并"物理相邻页"，避免把不相干的两页硬拼起来
    if cur["page_start"] != prev["page_end"] + 1:
        return False
    # 新标题 = 新章节，绝不合并
    if cur["element_type"] == ElementType.title.value:
        return False
    # 表格跨页续行
    if prev["element_type"] == ElementType.table.value and cur["element_type"] == ElementType.table.value:
        return True
    # 代码块被页码截断
    if prev["element_type"] == ElementType.code.value and cur["element_type"] == ElementType.code.value:
        return True
    # 正文/列表：上一块结尾没有句末标点 → 判定为被页码截断
    if prev["element_type"] in (ElementType.paragraph.value, ElementType.list.value) \
            and cur["element_type"] in (ElementType.paragraph.value, ElementType.list.value):
        tail = prev["text"].rstrip()
        return bool(tail) and tail[-1] not in SENTENCE_END
    return False


def _merge_continuations(blocks: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for cur in blocks:
        if merged and _is_continuation(merged[-1], cur):
            prev = merged[-1]
            if prev["element_type"] == ElementType.table.value:
                prev_rows = prev["text"].split("\n")
                next_rows = cur["text"].split("\n")
                # 跨页表格通常会在新页重复表头（含 | --- | 分隔行）：
                # 从新页开头逐行与旧表头比对，能对上就丢掉，避免表头出现在表格中间。
                idx = 0
                while idx < len(next_rows) and idx < len(prev_rows) \
                        and next_rows[idx].strip() == prev_rows[idx].strip():
                    idx += 1
                if idx:
                    next_rows = next_rows[idx:]
                if next_rows:
                    prev["text"] = prev["text"] + "\n" + "\n".join(next_rows)
            else:
                prev["text"] = _join_wrapped(prev["text"], cur["text"])
            prev["page_end"] = cur["page_end"]
            continue
        merged.append(cur)
    return merged


# ── 分页来源 ──────────────────────────────────────────────────────────
def _pdf_pages(file_path: str) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(file_path)
    return [(page.extract_text() or "") for page in reader.pages]


def _split_by_sentence(text: str, size: int) -> list[str]:
    """按句末标点把超长段落切成不超过 size 的片段（纯文本没有空行时的兜底）。"""
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    out, cur = [], ""
    for s in sentences:
        if not s:
            continue
        if cur and len(cur) + len(s) > size:
            out.append(cur)
            cur = s
        else:
            cur += s
    if cur.strip():
        out.append(cur)

    result = []
    for piece in out or [text]:
        while len(piece) > size:          # 连标点都没有的长串，按字符硬切
            result.append(piece[:size])
            piece = piece[size:]
        if piece:
            result.append(piece)
    return result


def _pseudo_pages(text: str) -> list[str]:
    """把无页码格式按段落边界切成 ~PSEUDO_PAGE_CHARS 的"页"，保证定位锚点稳定。

    注意：如果一个"段落"本身就超过一页（纯文本没有空行时常见），
    必须再按句子切，否则整篇文档只会得到 1 页、页码锚点也就失去意义。
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    expanded: list[str] = []
    for p in paragraphs:
        if len(p) <= PSEUDO_PAGE_CHARS:
            expanded.append(p)
        else:
            expanded.extend(_split_by_sentence(p, PSEUDO_PAGE_CHARS))
    paragraphs = expanded

    pages, cur = [], ""
    for p in paragraphs:
        if cur and len(cur) + len(p) > PSEUDO_PAGE_CHARS:
            pages.append(cur)
            cur = ""
        cur += (p + "\n\n")
    if cur.strip():
        pages.append(cur)
    return pages or [text]


# ── 可单测的核心：pages → ParsedDocument ──────────────────────────────
def parse_pages(pages: list[str], *, parser_name: str, plain_mode: bool) -> ParsedDocument:
    started = time.time()

    if plain_mode:
        entries: list[tuple[int, str]] = []
        for page_no, text in enumerate(pages, start=1):
            for line in (text or "").splitlines():
                entries.append((page_no, line))
        blocks = _merge_continuations(_blocks_with_pages(entries, plain_mode=True))
    else:
        # Markdown：走标题树解析（标题不单独成块，内容块带 heading_path）
        blocks = _md_blocks(pages)

    elements: list[ParsedElement] = []
    for order, b in enumerate(blocks):
        if not b["text"].strip():
            continue
        elements.append(ParsedElement(
            order_index=order,
            page_number=b["page_start"],
            page_end=b["page_end"],
            element_type=ElementType(b["element_type"]),
            text=b["text"],
            heading_path=b.get("heading_path") or [],
        ))

    full_text = "\n".join(e.text for e in elements)
    return ParsedDocument(
        elements=elements,
        page_count=len(pages),
        language=detect_language(full_text),
        parser_name=parser_name,
        parse_time_ms=round((time.time() - started) * 1000, 2),
        char_count=len(full_text),
    )


# ── 统一入口 ──────────────────────────────────────────────────────────
def parse_document(file_path: str, file_type: str) -> ParsedDocument:
    ext = (file_type or "").lower().lstrip(".")

    if ext == "pdf":
        return parse_pages(_pdf_pages(file_path), parser_name="pypdf+structure-v2", plain_mode=True)

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    # 只有 Markdown 有可靠的结构标记（标题/表格/列表），其余纯文本一律按"句末断段"处理。
    # 早期把 .txt 当成 Markdown 处理，一旦文件里没有空行，整篇就会变成一个巨大段落。
    if ext in ("md", "markdown"):
        return parse_pages(_pseudo_pages(text), parser_name="markdown-heading-tree-v3", plain_mode=False)
    return parse_pages(_pseudo_pages(text), parser_name="text-structure-v2", plain_mode=True)
