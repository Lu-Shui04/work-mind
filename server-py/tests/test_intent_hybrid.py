# server-py/tests/test_intent_hybrid.py
"""混合意图判定（recall_first 的升级版）—— 不联网、不调模型（模型层用替身）。

为什么要有这一层：纯规则会变成"规则跑步机" —— 2026-10-04 一个 session 里就补了
三条规则（会话延续 / 对话记忆 / 自我介绍），每来一种新说法都得再加一条。
但也不能反过来"让模型直接判该不该查库"：它不知道公司库里有什么
（历史坑：它说「rag召回失败」属于通用技术问答、无需检索，而库里正好有那篇资料）。

所以分工是：
  第一层 规则快速通道（打招呼/纯算式/日期…）—— 零成本、高置信，判错代价极低；
  第二层 有知识信号（制度关键词 / 查搜动作）—— 直接查，不问模型（省掉 ≈742ms/¥0.0003）；
  第三层 其余交给模型判"这句话是哪一类"—— 类别 → 是否检索由 _CATEGORY_POLICY
        做确定性映射，拿不准（模型挂了/类别不认识）一律当 company → 查库。

钉住的就是这三层各自不能被改坏。
"""
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services.rag import intent as I  # noqa: E402


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ── 第一层：规则快速通道 ────────────────────────────────────────
def test_fast_path_needs_no_model(monkeypatch=None):
    calls: list[str] = []
    orig = I._model_category

    async def spy(msg):
        calls.append(msg)
        return "company", "spy", 0, False

    I._model_category = spy
    try:
        for msg in ["你好", "今天几号？", "讲个笑话吧", "记住了吗", "我叫小米"]:
            d = _run(I._hybrid_classify(msg))
            assert d.need_knowledge is False, (msg, d)
            assert d.decision_source.startswith("skip-"), (msg, d.decision_source)
    finally:
        I._model_category = orig
    assert calls == [], f"快速通道不该调用模型：{calls}"


# ── 第二层：知识信号直通（不调模型）────────────────────────────
def test_knowledge_signal_skips_model():
    calls: list[str] = []
    orig = I._model_category

    async def spy(msg):
        calls.append(msg)
        return "company", "spy", 0, False

    I._model_category = spy
    try:
        for msg in ["公司的年假有多少天", "差旅报销标准", "帮我查一下前端规范"]:
            d = _run(I._hybrid_classify(msg))
            assert d.need_knowledge is True, (msg, d)
            assert d.decision_source == "hybrid-signal", (msg, d.decision_source)
    finally:
        I._model_category = orig
    assert calls == [], f"有知识信号时不该调用模型：{calls}"


# ── 第三层：模型判类别 → 确定性映射 ────────────────────────────
def test_model_category_drives_policy():
    orig = I._model_category
    try:
        for category, need in [("memory", False), ("chitchat", False),
                               ("self_intro", False), ("company", True),
                               ("general", True), ("action", True)]:
            async def fake(msg, _c=category):
                return _c, "fake", 12, False

            I._model_category = fake
            d = _run(I._hybrid_classify("上次那个方案是什么来着"))
            assert d.need_knowledge is need, (category, d)
            assert d.decision_source == f"hybrid-model-{category}", d.decision_source
            assert d.category == category, d
    finally:
        I._model_category = orig


def test_category_policy_covers_every_category():
    """提示词里承诺的六类必须都在映射表里 —— 漏一类就会 KeyError 把对话打挂。"""
    for c in ["chitchat", "memory", "self_intro", "action", "company", "general"]:
        assert c in I._CATEGORY_POLICY, c


def test_unknown_or_failed_category_falls_back_to_retrieval():
    """模型挂了 / 返回不认识的类别 → 查库（宁可多查一次，也不能漏答）。"""
    orig = I._model_category
    try:
        async def unknown(msg):
            return "company", "类别不可识别，按召回优先处理", 5, False

        I._model_category = unknown
        d = _run(I._hybrid_classify("这句话很模糊"))
        assert d.need_knowledge is True, d
    finally:
        I._model_category = orig


def _patch_model_factory(behaviour):
    """把 create_chat_model 换成替身，用来测 _model_category 本身（缓存 / 失败兜底）。

    _model_category 内部是**函数内**导入 create_chat_model 的，所以替换模块属性即可生效。
    """
    import app.models.llm as llm

    class _Structured:
        async def ainvoke(self, messages):
            return behaviour()

    class _Model:
        def with_structured_output(self, *a, **k):
            return _Structured()

    original = llm.create_chat_model
    llm.create_chat_model = lambda **kwargs: _Model()
    return llm, original


def test_category_cache_avoids_second_model_call():
    """同一条消息第二次不再调模型（进程内缓存，省掉那次 ≈742ms/¥0.0003）。"""
    calls = {"n": 0}

    def behaviour():
        calls["n"] += 1
        return I._MessageCategory(category="company", reason="fake")

    llm, original = _patch_model_factory(behaviour)
    I._CATEGORY_CACHE.clear()
    try:
        c1, _, _, cached1 = _run(I._model_category("随便一句话测试缓存"))
        c2, _, _, cached2 = _run(I._model_category("随便一句话测试缓存"))
        assert c1 == c2 == "company", (c1, c2)
        assert calls["n"] == 1, f"第二次应命中缓存，实际调用了 {calls['n']} 次"
        assert cached1 is False and cached2 is True
    finally:
        llm.create_chat_model = original
        I._CATEGORY_CACHE.clear()


def test_model_exception_falls_back_to_retrieval():
    """分类模型挂了（上游 5xx / 超时）→ 当 company 处理 = 查库，绝不拦住对话。"""

    def behaviour():
        raise RuntimeError("upstream 502")

    llm, original = _patch_model_factory(behaviour)
    I._CATEGORY_CACHE.clear()
    try:
        category, reason, _, cached = _run(I._model_category("分类会失败的一句话"))
        assert category == "company", category
        assert "分类失败" in reason, reason
        assert cached is False
    finally:
        llm.create_chat_model = original
        I._CATEGORY_CACHE.clear()


def test_unknown_category_from_model_treated_as_company():
    """模型返回了提示词之外的类别（幻觉）→ 当 company 处理，不 KeyError。"""
    llm, original = _patch_model_factory(
        lambda: I._MessageCategory(category="weather", reason="瞎编的类别"))
    I._CATEGORY_CACHE.clear()
    try:
        category, reason, _, _ = _run(I._model_category("明天天气怎么样"))
        assert category == "company", category
        assert "不可识别" in reason, reason
    finally:
        llm.create_chat_model = original
        I._CATEGORY_CACHE.clear()


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
