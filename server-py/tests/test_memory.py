# server-py/tests/test_memory.py
"""会话记忆与知识库三态语义的回归测试（不联网、不调模型）。

（本文件只保留**黄金用例**：核心路径 + 真实踩过的边界，每条都能讲清「防的是什么坑」；零碎用例已精简。）

运行方式（容器内）：
    docker exec workmind-server python /app/tests/test_memory.py

固化的是这几条规则（都是产品语义，改坏了用户立刻能感知）：
1. 超过 N 轮触发**异步**摘要，每次请求只带「摘要 + 最近 N 轮原文」
2. 摘要失败时必须保留原文（宁可多留一份，也不能丢记忆）
3. 数据库不可用时退回进程内存，对话照样能用（但会明确记一条降级日志）
4. 知识库三态：自动=查到用资料/查不到说明后照常回答；强制=只依据知识库；关闭=压根不查
5. 答案缓存的域：三态与检索范围必须各算各的键（否则换个范围/换个模式问同一句，
   拿回来的还是上一种设置的答案 —— 甚至"强制检索"会被缓存静默绕过）

注：这个测试容器里没有数据库连接，跑的是**内存兜底**分支；
   数据库分支由 scripts/smoke-test.sh（打真实服务）和
   scripts/check-memory-persistence.sh（重启后仍在）覆盖。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.api.chat import ChatStreamRequest, _cache_scope  # noqa: E402
from app.services.chat import memory as M  # noqa: E402
from app.core.identity import User  # noqa: E402
from app.services.rag.query import MISS_LEAD, miss_reply, resolve_knowledge_mode  # noqa: E402

TENANT = "tenant-test"


def run(coro):
    return asyncio.run(coro)


class _NoAutoSummary:
    """测试里关掉"自动触发摘要"：append_turn 会 create_task，真跑起来会调模型。"""

    def __enter__(self):
        self.old = M.SUMMARY_AFTER_ROUNDS
        M.SUMMARY_AFTER_ROUNDS = 10 ** 6

    def __exit__(self, *_exc):
        M.SUMMARY_AFTER_ROUNDS = self.old


async def _reset():
    await M.clear_all(TENANT)


# ── 记忆：记录与轮数 ─────────────────────────────────────────
def test_append_turn_records_round():
    async def body():
        await _reset()
        await M.append_turn("s1", "问题一", "回答一", TENANT, "u1")
        assert await M.rounds_of("s1", TENANT) == 1
        await M.append_turn("s1", "问题二", "回答二", TENANT, "u1")
        assert await M.rounds_of("s1", TENANT) == 2
        assert len(await M.get_history("s1", TENANT)) == 4

    run(body())


def test_sessions_are_isolated_per_tenant():
    """会话按租户隔离：只凭 session_id 猜不到别人的对话。"""
    async def body():
        await _reset()
        await M.append_turn("shared-id", "A 租户说的话", "好的", "tenant-a", "u1")
        assert await M.get_history("shared-id", "tenant-b") == []
        assert len(await M.get_history("shared-id", "tenant-a")) == 2
        await M.clear_all("tenant-a")

    run(body())


def test_memory_messages_keeps_only_recent_rounds():
    """最近 N 轮之外的原文不进 prompt（它们应该已经被摘要取代）。"""
    async def body():
        await _reset()
        with _NoAutoSummary():
            for i in range(15):
                await M.append_turn("s2", f"问题{i}", f"回答{i}", TENANT, "u1")
        messages = await M.memory_messages("s2", TENANT)
        texts = [m.content for m in messages]
        assert len(messages) == M.KEEP_RECENT_ROUNDS * 2, len(messages)
        assert "问题14" in texts[-2] and "回答14" in texts[-1]
        assert all("问题0" != t for t in texts), "最早那轮不该再以原文出现"

    run(body())


def test_summarize_compresses_and_keeps_recent():
    class _FakeStructured:
        async def ainvoke(self, _messages):
            class _Out:
                summary = "用户问过 q0~q4，助手都答了。"
            return _Out()

    class _FakeModel:
        def with_structured_output(self, *_a, **_k):
            return _FakeStructured()

    async def body():
        await _reset()
        with _NoAutoSummary():
            for i in range(15):
                await M.append_turn("s6", f"q{i}", f"a{i}", TENANT, "u1")

        original = M.chat_model
        M.chat_model = _FakeModel()
        try:
            summary = await M.summarize_session("s6", TENANT)
        finally:
            M.chat_model = original

        assert summary and "q0" in summary
        assert await M.get_summary("s6", TENANT) == summary
        history = await M.get_history("s6", TENANT)
        assert len(history) == M.KEEP_RECENT_ROUNDS * 2, len(history)
        assert history[-1]["content"] == "a14"

    run(body())


def test_summarize_failure_keeps_original_history():
    """摘要失败绝不能丢记忆：宁可多留原文，也不能把对话弄没。"""
    class _Boom:
        def with_structured_output(self, *_a, **_k):
            raise RuntimeError("模型挂了")

    async def body():
        await _reset()
        with _NoAutoSummary():
            for i in range(15):
                await M.append_turn("s7", f"q{i}", f"a{i}", TENANT, "u1")

        original = M.chat_model
        M.chat_model = _Boom()
        try:
            result = await M.summarize_session("s7", TENANT)
        finally:
            M.chat_model = original

        assert result is None
        assert len(await M.get_history("s7", TENANT)) == 30, "失败时必须保留全部原文"
        assert await M.get_summary("s7", TENANT) == ""

    run(body())


def test_mode_disabled_never_uses_knowledge():
    """关闭：压根没检索，不可能出现"未找到"话术。"""
    assert resolve_knowledge_mode(False, False, []) == "no_retrieval"
    assert resolve_knowledge_mode(False, False, [{"x": 1}]) == "no_retrieval"


def test_mode_hit_answers_from_knowledge():
    for forced in (None, True):
        assert resolve_knowledge_mode(forced, True, [{"x": 1}]) == "grounded_answer"


def test_mode_miss_is_one_short_reply():
    """没命中：自动与强制**同一句引导**，且不调用模型（2026-09-27 按要求统一）。

    以前两套行为（自动=通用知识兜底、强制=一长段检索诊断）都被取消了：
    没有依据就不让模型发挥，也不在聊天里堆"阈值/候选数"这类工程细节。
    """
    assert resolve_knowledge_mode(None, True, []) == "miss"
    assert resolve_knowledge_mode(True, True, []) == "miss"
    reply = miss_reply()
    assert MISS_LEAD in reply, "必须说清没查到"
    assert "你想问什么，可以直接问我" in reply, "不能只说一句没查到就把人堵死"
    assert "阈值" not in reply and "候选" not in reply, "聊天里不出现检索诊断"

# ── 答案缓存的域：三态 + 检索范围都要算进 key ────────────────
_CACHE_USER = User(tenant_id="tenant-test", user_id="u-cache",
                   departments=["tech"], clearance="internal")


def _scope(**overrides) -> str:
    """按请求体算一次缓存域（路由里用的就是同一个函数）。"""
    return _cache_scope(_CACHE_USER,
                        ChatStreamRequest(message="公司的年假有几天？", **overrides))


def test_cache_scope_separates_retrieval_range():
    """「全部可见」与「只看财务部 / 只看政策 / 只看某版本」的答案可以完全不同。

    反例：选「本部门=财务」→ 这个范围内确实没有内容 → "未命中"的回答进了缓存；
    切回「全部可见」重问 → 命中缓存，回的还是"知识库中未找到相关内容"，
    而库里其实有内容（用户会得出"库里没有"的错误结论）。
    """
    base = _scope()
    assert _scope(knowledgeDepartment="finance") != base
    assert _scope(knowledgeDocType="policy") != base
    assert _scope(knowledgeVersion="2026-08") != base
    assert _scope(includeSuperseded=True) != base


def test_cache_scope_still_separates_permission_domain():
    """权限域没有因为加了检索域而丢：换租户/部门/密级都不能复用别人的答案。"""
    others = [
        User(tenant_id="tenant-other", user_id="u1", departments=["tech"], clearance="internal"),
        User(tenant_id="tenant-test", user_id="u2", departments=["hr"], clearance="internal"),
        User(tenant_id="tenant-test", user_id="u3", departments=["tech"], clearance="public"),
    ]
    body = ChatStreamRequest(message="公司的年假有几天？")
    mine = _cache_scope(_CACHE_USER, body)
    assert all(_cache_scope(other, body) != mine for other in others)


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
    print(f"共 {len(tests)} 个用例，", "全部通过 ✅" if not failed else f"{len(failed)} 个失败 ❌ {failed}")
    sys.exit(1 if failed else 0)
