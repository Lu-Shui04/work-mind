# server-py/tests/test_turn_context.py
"""本轮依据块（资料 / 未命中规则）的位置与文案 —— 不联网、不调模型。

（本文件只保留**黄金用例**：核心路径 + 真实踩过的边界，每条都能讲清「防的是什么坑」；零碎用例已精简。）

运行方式（容器内）：
    docker exec workmind-server python /app/tests/test_turn_context.py
    # 或： bash scripts/run-tests.sh tests/test_turn_context.py

固化的是 2026-09-27 那次真实事故的修复：
检索**确实**命中了 2 条《年假与休假政策》切片（0.84 / 0.33，都过了重排下限），
模型却回答"知识库中没有查到年假制度的相关内容"。原因是资料被拼在 system prompt 顶部，
而上一轮"没命中"时助手刚说过的那句"知识库中没有查到相关内容"排在它后面 ——
模型把**最近一条助手消息**当成了本轮结论，顶部那两段资料等于没给。
（复现：同一会话先问"没有年假等？"（未命中）再问"我想请年假"（命中 2 条）。）

所以这里钉住两条，任一条被改坏都会让这个 bug 静默复发：
1. build_turn_messages 的顺序必须是「人设 → 历史 → 本轮依据块 → 用户提问」；
2. 依据块必须显式声明"历史里的没查到是上一轮的结论、对本轮无效"。
"""
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from app.api.chat import build_turn_messages  # noqa: E402
from app.services.rag.query import (  # noqa: E402
    MISS_LEAD, build_turn_context, resolve_knowledge_mode,
)

SOURCES = [{
    "chunkId": "c1", "docId": "d1", "title": "年假与休假政策", "pageNumber": 1,
    "pageLabel": "第1页", "department": "general", "version": "2026-09",
    "elementType": "list", "score": 0.492, "rerankScore": 0.842,
    "headingPath": ["年假与休假政策", "三、年假的申请与审批"],
    "content": "1. 员工须提前 **3 个工作日**在 HR 系统提交年假申请。",
}]
HISTORY = [HumanMessage(content="没有年假等？"),
           AIMessage(content="（" + MISS_LEAD + "，以下内容来自通用知识，不代表公司口径。）")]


# ── 顺序：依据块必须在历史之后、提问之前 ────────────────────────
def test_turn_context_comes_after_history():
    messages = build_turn_messages("人设", HISTORY, "【本轮资料】…", "我想请年假")
    kinds = [type(m).__name__ for m in messages]
    assert kinds == ["SystemMessage", "HumanMessage", "AIMessage",
                     "SystemMessage", "HumanMessage"], kinds
    assert messages[0].content == "人设"
    assert messages[-2].content == "【本轮资料】…", "依据块必须紧挨着用户提问"
    assert messages[-1].content == "我想请年假"


def test_turn_context_not_added_when_empty():
    """关闭检索、且历史为空时不该多发一条空消息（空 SystemMessage 会干扰模型）。"""
    messages = build_turn_messages("人设", [], "", "现在几点")
    assert [m.content for m in messages] == ["人设", "现在几点"]
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[-1], HumanMessage)


# ── 内容：命中资料时必须压住历史里的"没查到" ────────────────────
def test_grounded_block_carries_sources_and_history_rule():
    block = build_turn_context(SOURCES)
    assert "年假与休假政策" in block, "资料要真的在块里"
    assert "3 个工作日" in block, "资料正文要真的在块里（不是只给标题）"
    assert "[1]" in block, "编号必须与前端引用角标一致"
    assert "不要被上面的对话历史带偏" in block
    assert "严禁把上文那句" in block and "复述成本轮结论" in block


def test_no_block_when_no_sources():
    """没命中不发依据块 —— 那条路根本不调用模型，回的是固定答复 miss_reply()。"""
    assert build_turn_context([]) == ""


# ── 三态与依据块的配合（回归：资料在就绝不能走"未命中"话术）────────
def test_mode_and_block_stay_consistent():
    assert resolve_knowledge_mode(None, True, SOURCES) == "grounded_answer"
    assert build_turn_context(SOURCES).startswith("【本轮检索到的企业知识库资料】")
    # 没命中（自动/强制都一样）→ 固定答复、不发资料块、不调用模型
    assert resolve_knowledge_mode(True, True, []) == "miss"
    assert resolve_knowledge_mode(None, True, []) == "miss"
    assert build_turn_context([]) == ""
    # 关闭检索 → 既不发块、也不出现任何"未找到"话术
    assert resolve_knowledge_mode(False, False, []) == "no_retrieval"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("✅", fn.__name__)
    print(f"\n全部通过：{len(tests)} 个用例")
