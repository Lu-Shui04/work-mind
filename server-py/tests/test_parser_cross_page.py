# server-py/tests/test_parser_cross_page.py
"""跨页续写合并的回归测试（不依赖 pytest，直接 python 运行即可）。

运行方式（容器内）：
    docker exec workmind-server python /app/tests/test_parser_cross_page.py

覆盖的 5 个场景：
1. 中文段落被页码截断 → 合并成一个元素，页码区间 1-2
2. 上一页结尾是完整句子 → 不合并（避免把不相干内容粘起来）
3. 表格跨页续行 → 合并，且重复表头只保留一次
4. 切片层：跨页元素产生"带页码区间"的单个切片，文本不被切断
5. 真实 PDF 端到端（pypdf 路径）：页尾半句话与下一页开头接回

这些用例来自一次真实缺陷：早期版本"按页切块"，导致一段话/一张表被页码切成两半，
引用也只能标到半页。修完后把这几个场景固化下来，避免以后改解析器时又退回去。
"""
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.schemas.document import DocumentRecord
from app.services.rag.ingest import build_chunks
from app.services.rag.parser import parse_document, parse_pages


def test_cross_page_paragraph_merge():
    p1 = "员工差旅报销标准\n\n住宿费用标准为每晚不超过"
    p2 = "800 元，超出部分需要部门总监审批。\n\n第二条 交通费按经济舱标准报销。"
    doc = parse_pages([p1, p2], parser_name="test", plain_mode=True)

    merged = [e for e in doc.elements if e.text.startswith("住宿费用标准")]
    assert len(merged) == 1, "被截断的段落应当合并成一个元素"
    assert "800 元" in merged[0].text, "合并后应当包含下一页的开头"
    assert (merged[0].page_number, merged[0].page_end) == (1, 2), "页码区间应为 1-2"
    assert any(e.text.startswith("第二条") for e in doc.elements), "下一段不应被误并"


def test_complete_sentence_not_merged():
    doc = parse_pages(
        ["第一条 本规定适用于全体正式员工。", "第二条 年假天数见下表。"],
        parser_name="test", plain_mode=True,
    )
    assert len(doc.elements) == 2, "两页各自成段"
    assert all(e.page_number == e.page_end for e in doc.elements), "不应出现跨页区间"


def test_table_continuation_dedup_header():
    t1 = "| 天数 | 审批人 |\n| --- | --- |\n| 1天 | 主管 |"
    t2 = "| 天数 | 审批人 |\n| --- | --- |\n| 3天 | 主管+HR |"
    doc = parse_pages([t1, t2], parser_name="test", plain_mode=True)

    assert len(doc.elements) == 1, "跨页表格应合并为一个元素"
    tbl = doc.elements[0]
    assert tbl.element_type.value == "table"
    assert (tbl.page_number, tbl.page_end) == (1, 2)
    assert tbl.text.count("| 天数 | 审批人 |") == 1, "重复表头应被去掉"
    assert "3天" in tbl.text and "1天" in tbl.text, "两页数据行都要在"


def test_chunk_carries_page_range():
    p1 = "员工差旅报销标准\n\n住宿费用标准为每晚不超过"
    p2 = "800 元，超出部分需要部门总监审批。"
    doc = parse_pages([p1, p2], parser_name="test", plain_mode=True)

    rec = DocumentRecord(
        doc_id="doc_test", tenant_id="t", document_title="差旅标准", department="finance",
        version="2026-08", effective_date="2026-08-01", doc_type="policy",
        security_level="internal", file_name="a.pdf", file_type="pdf", file_size=1,
        source_path="/tmp/a.pdf", file_sha256="x",
    )
    chunks = build_chunks(rec, doc.elements)

    assert any(c.page_end and c.page_end != c.page_number for c in chunks), "应存在带页码区间的切片"
    assert any("800 元" in c.text for c in chunks), "跨页切片文本应完整"


def test_plain_text_without_blank_lines_is_chunked():
    """回归：没有空行的纯文本（整篇被解析成 1 个巨大段落）必须切成多个切片。

    真实故障：一份 11661 字节的 .txt（无空行）入库后 chunks=1、单片 4396 字。
    两个 bug 叠加：① .txt 被当成 Markdown，没有空行 → 整篇 = 1 个元素；
                  ② 切分只对标题/表格/代码做超长判断，正文分支放过超长元素。
    """
    from app.services.rag.ingest import MAX_CHUNK_CHARS, TARGET_CHUNK_CHARS

    sentence = "第三条 员工请假需提前向直属主管报备，并在系统中提交申请，超过三天需部门总监审批。"
    text = sentence * 60            # ≈ 2700 字，且完全没有空行
    doc = parse_pages([text], parser_name="test", plain_mode=True)

    assert len(doc.elements) > 1, "无空行的纯文本应被按句末标点断成多个元素"

    rec = DocumentRecord(
        doc_id="doc_txt", tenant_id="t", document_title="请假制度", department="hr",
        version="2026-08", effective_date="2026-08-01", doc_type="policy",
        security_level="internal", file_name="a.txt", file_type="txt", file_size=len(text),
        source_path="/tmp/a.txt", file_sha256="y",
    )
    chunks = build_chunks(rec, doc.elements)

    assert len(chunks) > 1, f"应切成多个切片，实际 {len(chunks)} 个"
    assert max(c.char_count for c in chunks) <= MAX_CHUNK_CHARS, "任何切片都不能超过上限"
    assert sum(c.char_count for c in chunks) >= len(text) * 0.9, "切分不应丢内容"
    # 拼回去要能覆盖原文（去掉空白差异后不应该少字）
    joined = "".join(c.text for c in chunks).replace("\n", "")
    assert sentence[:20] in joined


def test_chunks_have_overlap():
    """切片之间必须有重叠：答案被切在边界上时，两片都能独立召回。"""
    from app.services.rag.ingest import MAX_CHUNK_CHARS, OVERLAP_CHARS

    sentence = "第十二条 员工出差住宿标准为每晚不超过八百元，超出部分需部门总监审批。"
    text = sentence * 120          # ≈ 4500 字，确保能切出多片
    doc = parse_pages([text], parser_name="test", plain_mode=True)
    rec = DocumentRecord(
        doc_id="doc_ov", tenant_id="t", document_title="差旅标准", department="finance",
        version="2026-08", effective_date="2026-08-01", doc_type="policy",
        security_level="internal", file_name="b.txt", file_type="txt", file_size=len(text),
        source_path="/tmp/b.txt", file_sha256="z",
    )
    chunks = build_chunks(rec, doc.elements)

    assert OVERLAP_CHARS > 0, "重叠长度应大于 0（曾经因为重写切分逻辑把重叠弄丢过）"
    assert len(chunks) > 2, f"应切成多片，实际 {len(chunks)}"

    # 相邻两片：后一片的开头应当出现在前一片的结尾里（说明确实重叠了）
    overlapped = 0
    for prev, cur in zip(chunks, chunks[1:]):
        head = cur.text.strip()[:12]
        if head and head in prev.text:
            overlapped += 1
    assert overlapped >= len(chunks) - 1, f"相邻切片应普遍存在重叠，实际 {overlapped}/{len(chunks) - 1}"

    # 重叠会带来内容重复：总字数应明显多于原文
    assert sum(c.char_count for c in chunks) > len(text), "存在重叠时总字数应大于原文"
    assert max(c.char_count for c in chunks) <= MAX_CHUNK_CHARS, "带重叠也不能超过上限"


def _build_pdf(path: str, pages: list[list[str]]) -> None:
    """手写一个最小 PDF（只依赖标准库），用于端到端验证 pypdf 解析路径。"""
    n = len(pages)
    page_nums = [4 + 2 * i for i in range(n)]
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [" + kids + "] /Count " + str(n) + " >>").encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, lines in enumerate(pages):
        content = "BT /F1 12 Tf 72 720 Td 16 TL\n"
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content += "(" + esc + ") Tj T*\n"
        content += "ET"
        objs[page_nums[i] + 1] = ("<< /Length " + str(len(content)) + " >>\nstream\n"
                                  + content + "\nendstream").encode()
        objs[page_nums[i]] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                              "/Resources << /Font << /F1 3 0 R >> >> /Contents "
                              + str(page_nums[i] + 1) + " 0 R >>").encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += str(num).encode() + b" 0 obj\n" + objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    maxnum = max(objs)
    out += b"xref\n0 " + str(maxnum + 1).encode() + b"\n0000000000 65535 f \n"
    for num in range(1, maxnum + 1):
        out += ("%010d 00000 n \n" % offsets[num]).encode() if num in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size " + str(maxnum + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    with open(path, "wb") as f:
        f.write(bytes(out))


def test_real_pdf_end_to_end(tmp_dir: str = "/tmp"):
    path = os.path.join(tmp_dir, "workmind_page_test.pdf")
    _build_pdf(path, [
        ["Reimbursement Standard", "The hotel rate must not exceed"],
        ["800 CNY per night.", "Approval Process"],
    ])
    try:
        doc = parse_document(path, "pdf")
        assert doc.parser_name.endswith("v2")
        assert any("must not exceed" in e.text and "800 CNY per night." in e.text
                   for e in doc.elements), "PDF 页尾半句话应被接回"
        assert any(e.page_number == 1 and e.page_end == 2 for e in doc.elements)
    finally:
        os.path.exists(path) and os.remove(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as err:
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__} -> {err}")
        except Exception as err:  # noqa: BLE001
            failed.append(t.__name__)
            print(f"  ERROR {t.__name__} -> {type(err).__name__}: {err}")
    print()
    print("结果:", "全部通过 ✅" if not failed else f"{len(failed)} 个失败 ❌ {failed}")
    sys.exit(1 if failed else 0)
