# server-py/tests/test_query_rewrite.py
"""检索式改写（动作请求 → 名词短语检索式）—— 不联网、不调模型。

固化的是 2026-10-04 那次真实事故：
任务「帮我把2000元的住宿费报销一下，给领导通知一下」向量召回明明拿到了
《差旅与报销管理制度》的住宿费标准切片（0.5335 > 阈值 0.35），
但 bge 重排对祈使句没有判别力，同一片只打 0.20（下限 0.3）→ rerank_rejected →
对外答「知识库没有查到相关内容」。而把查询换成「住宿费报销」同一片是 0.97。

钉住三条，任一条被改坏都会让这个 bug 静默复发：
1. 动作请求必须被改写成名词短语检索式（金额/指代词/纯动作子句都要剥掉）；
2. 纯问句必须原样返回（评测集 rag-00x 全是问句，改写误伤会让检索质量倒退）；
3. 改写必须幂等（read_doc 工具拿到的可能已经是改写后的检索式）。
"""
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services.rag.intent import build_search_query  # noqa: E402


# ── 动作请求 → 检索式 ──────────────────────────────────────────
def test_action_request_reduced_to_noun_phrase():
    """核心事故用例：金额、祈使脚手架、通知子句全部剥掉，只留主题词。"""
    q = build_search_query("帮我把2000元的住宿费报销一下，给领导通知一下")
    assert q == "住宿费报销", q


def test_pure_action_clause_dropped():
    """「通知 HR」是工具活不是检索对象；只要还有含知识关键词的子句，就把它丢掉。"""
    assert build_search_query("帮我查一下年假政策，然后通知HR") == "年假政策"


def test_leading_action_verb_stripped():
    assert build_search_query("查一下前端代码发布的窗口期") == "前端代码发布的窗口期"


def test_money_amount_stripped():
    """金额对不上制度表里的档位数字（600/450/350），留着反而压低重排分。"""
    assert build_search_query("帮我报销5000元的发票") == "报销发票"


def test_deictic_stripped_mid_clause():
    assert build_search_query("报销一下这笔住宿费") == "报销住宿费"


def test_no_keyword_clause_fallback_keeps_content():
    """整句都没有知识关键词时不丢信息（这类消息反正会 below_threshold，走工具分支）。"""
    assert build_search_query("帮我订个会议室") == "订个会议室"


# ── 问句原样返回（防回归）─────────────────────────────────────
def test_question_passes_through_unchanged():
    for q in [
        "公司的年假有多少天？",
        "出差住宿费一线城市每晚上限是多少？",
        "单笔报销金额超过多少需要总经理审批？",
        "前端新项目默认的技术栈是什么？",
    ]:
        assert build_search_query(q) == q, q


# ── 幂等 ───────────────────────────────────────────────────────
def test_rewrite_is_idempotent():
    q = build_search_query("帮我把2000元的住宿费报销一下，给领导通知一下")
    assert build_search_query(q) == q


def test_empty_and_tiny_inputs():
    assert build_search_query("") == ""
    assert build_search_query("查") == "查"


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
