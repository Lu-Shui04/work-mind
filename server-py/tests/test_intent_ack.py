# server-py/tests/test_intent_ack.py
"""确认类会话延续的意图判定 —— 不联网、不调模型。

固化的是 2026-10-04 那次真实事故：
用户上一轮让 Agent 报销 2000 元住宿费，这一轮说「好的，发一下直属主管」——
意图层没有任何跳过规则命中，recall_first 照常检索（召回 12 条候选、重排全判死），
回答被强制带上「知识库中没有查到相关内容」，用户困惑："为什么每一句都要检索？"

钉住三条，任一条被改坏都会让这个 bug 静默复发：
1. 确认词开头、且后半句没有知识诉求的会话延续必须跳过检索（skip-确认类会话延续）；
2. 「好的，帮我查一下年假政策」这类**仍然带着知识诉求**的必须照常检索；
3. looks_like_knowledge_need 决定"没查到"声明该不该说：检索 miss 与
   "说没查到"是两件事（recall_first 的例行检索 miss 不等于该对用户说没查到）。
"""
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services.rag.intent import (  # noqa: E402
    _rule_classify, looks_like_knowledge_need,
)


# ── 确认类会话延续：跳过检索 ───────────────────────────────────
def test_ack_continuation_skips_retrieval():
    d = _rule_classify("好的，发一下直属主管")
    assert d.need_knowledge is False, d
    assert d.decision_source == "skip-确认类会话延续", d.decision_source


def test_ack_with_action_skips_retrieval():
    d = _rule_classify("行，就按600一晚算")
    assert d.need_knowledge is False, d


def test_bare_ack_is_pleasantry():
    for msg in ["好", "行", "可以", "好的", "嗯"]:
        d = _rule_classify(msg)
        assert d.need_knowledge is False, msg
        assert d.decision_source.startswith("skip-"), msg


# ── 仍带知识诉求的：必须照常检索 ──────────────────────────────
def test_ack_with_knowledge_keyword_still_retrieves():
    d = _rule_classify("好的，帮我查一下年假政策")
    assert d.need_knowledge is True, d


def test_ack_with_lookup_verb_still_retrieves():
    # 用户明确说"查一下"，即便没带制度关键词也不能跳 —— 库里可能真有相关内容
    d = _rule_classify("好的，帮我查一下rag召回失败怎么办")
    assert d.need_knowledge is True, d


def test_ack_with_question_still_retrieves():
    d = _rule_classify("好的，为什么我的年假只剩5天？")
    assert d.need_knowledge is True, d


# ── looks_like_knowledge_need：决定"没查到"该不该说 ──────────────
# ── 对话记忆 / 自我介绍：去画像，不去知识库 ─────────────────────
def test_self_intro_skips_retrieval():
    """「我叫小米」的去处是用户画像，库里不可能有答案；查库只会白跑一次。"""
    d = _rule_classify("我叫小米")
    assert d.need_knowledge is False, d
    assert d.decision_source == "skip-自我介绍（存画像，不查库）", d.decision_source


def test_memory_question_skips_retrieval():
    """「记住了吗」带问号，但不是知识型提问 —— 2026-10-04 实测它被送去检索、
    未命中后用户收到一句"知识库中没有查到相关内容"。"""
    for msg in ["记住了吗", "我叫什么", "你还记得我刚才说的吗", "我们聊到哪了"]:
        d = _rule_classify(msg)
        assert d.need_knowledge is False, (msg, d)


def test_memory_question_with_knowledge_keyword_still_retrieves():
    """带知识关键词的照常检索：「帮我记住差旅报销标准」问的其实是制度内容。"""
    d = _rule_classify("帮我记住差旅报销标准")
    assert d.need_knowledge is True, d
    d2 = _rule_classify("我记得年假是15天，对吗？")
    assert d2.need_knowledge is True, d2


def test_looks_like_knowledge_need():
    assert looks_like_knowledge_need("好的，发一下直属主管") is False
    assert looks_like_knowledge_need("帮我想个团队名") is False
    assert looks_like_knowledge_need("公司的年假有多少天？") is True
    assert looks_like_knowledge_need("帮我查一下公司每月允许补卡几次？") is True
    assert looks_like_knowledge_need("今年的年会在哪里举办？") is True
    assert looks_like_knowledge_need("rag召回失败怎么办") is True


if __name__ == "__main__":
    import traceback

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as err:
                print("FAIL", name, err)
                traceback.print_exc()
