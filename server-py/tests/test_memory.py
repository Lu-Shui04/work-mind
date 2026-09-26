# server-py/tests/test_memory.py
"""会话记忆与知识库三态语义的回归测试（不联网、不调模型）。

运行方式（容器内）：
    docker exec workmind-server python /app/tests/test_memory.py

固化的是这几条规则（都是产品语义，改坏了用户立刻能感知）：
1. 超过 N 轮触发**异步**摘要，每次请求只带「摘要 + 最近 N 轮原文」
2. 摘要失败时必须保留原文（宁可多留一份，也不能丢记忆）
3. 数据库不可用时退回进程内存，对话照样能用（但会明确记一条降级日志）
4. 知识库三态：自动=查到用资料/查不到说明后照常回答；强制=只依据知识库；关闭=压根不查

注：这个测试容器里没有数据库连接，跑的是**内存兜底**分支；
   数据库分支由 scripts/smoke-test.sh（打真实服务）和
   scripts/check-memory-persistence.sh（重启后仍在）覆盖。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services.chat import memory as M  # noqa: E402
from app.services.rag.query import resolve_knowledge_mode  # noqa: E402

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


def test_memory_messages_without_history_is_empty():
    async def body():
        await _reset()
        assert await M.memory_messages("empty-session", TENANT) == []

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


def test_summary_is_prepended_to_prompt():
    async def body():
        await _reset()
        await M.append_turn("s3", "我叫李雷，在技术部", "好的", TENANT, "u1")
        M._mem_sessions[f"{TENANT}|s3"]["summary"] = "用户是技术部的李雷。"
        messages = await M.memory_messages("s3", TENANT)
        assert "此前对话的摘要" in messages[0].content
        assert "李雷" in messages[0].content
        assert messages[-1].content == "好的"

    run(body())


def test_memory_block_used_by_agent():
    async def body():
        await _reset()
        await M.append_turn("s4", "帮我算差旅费", "合计 3390 元", TENANT, "u1")
        M._mem_sessions[f"{TENANT}|s4"]["summary"] = "用户之前算过差旅费。"
        block = await M.memory_block("s4", TENANT)
        assert "此前对话的摘要" in block and "最近对话" in block
        assert "3390" in block

    run(body())


# ── 记忆：异步摘要 ───────────────────────────────────────────
def test_summary_triggers_only_after_threshold():
    """没超过阈值不压缩；超过阈值时触发一次异步摘要。"""
    async def body():
        await _reset()
        fired = []

        async def fake_summarize(session_id, tenant_id=None):
            fired.append(session_id)
            return "摘要"

        original = M.summarize_session
        M.summarize_session = fake_summarize
        try:
            for i in range(M.SUMMARY_AFTER_ROUNDS):
                await M.append_turn("s5", f"q{i}", f"a{i}", TENANT, "u1")
            assert fired == [], "还没超过阈值就不该压缩"
            await M.append_turn("s5", "q-extra", "a-extra", TENANT, "u1")
            await asyncio.sleep(0)
            assert fired == ["s5"], f"超过阈值应当触发一次异步摘要，实际 {fired}"
        finally:
            M.summarize_session = original

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


def test_clear_history_resets_summary_too():
    async def body():
        await _reset()
        await M.append_turn("s8", "q", "a", TENANT, "u1")
        M._mem_sessions[f"{TENANT}|s8"]["summary"] = "摘要"
        await M.clear_history("s8", TENANT)
        assert await M.get_summary("s8", TENANT) == ""
        assert await M.get_history("s8", TENANT) == []

    run(body())


def test_profile_roundtrip_and_clear():
    async def body():
        await _reset()
        assert await M.get_profile("u1", TENANT) == {}
        await M._save_profile("u1", {"name": "李雷", "dept": "技术部"}, TENANT)
        profile = await M.get_profile("u1", TENANT)
        assert profile["name"] == "李雷"
        assert "李雷" in M.profile_to_context(profile)
        await M.clear_all(TENANT)
        assert await M.get_profile("u1", TENANT) == {}

    run(body())


def test_degraded_mode_still_works_without_database():
    """没有数据库连接时必须退回内存继续可用（只是重启会丢），而不是直接报错。"""
    async def body():
        await _reset()
        # 这个测试容器没有数据库连接
        assert M.using_database() is False
        await M.append_turn("s9", "没有库也要能聊", "好的", TENANT, "u1")
        assert await M.rounds_of("s9", TENANT) == 1
        sessions = await M.list_sessions(TENANT)
        assert any(s["id"] == "s9" for s in sessions), sessions
        await M.clear_all(TENANT)

    run(body())


# ── 知识库三态语义 ───────────────────────────────────────────
def test_mode_disabled_never_uses_knowledge():
    """关闭：压根没检索，不可能出现"未找到"话术。"""
    assert resolve_knowledge_mode(False, False, []) == "no_retrieval"
    assert resolve_knowledge_mode(False, False, [{"x": 1}]) == "no_retrieval"


def test_mode_hit_answers_from_knowledge():
    for forced in (None, True):
        assert resolve_knowledge_mode(forced, True, [{"x": 1}]) == "grounded_answer"


def test_mode_forced_miss_is_grounded_only():
    """强制：查不到就说查不到，不许用模型自己的知识补充。"""
    assert resolve_knowledge_mode(True, True, []) == "grounded_miss"


def test_mode_auto_miss_falls_back_to_model():
    """自动：先说明未命中，再照常用通用知识回答（用户要的就是这个）。"""
    assert resolve_knowledge_mode(None, True, []) == "fallback_miss"


def test_mode_policy_can_force_strict_auto():
    """部署方显式配 grounded 时，自动模式也走严格模式。"""
    assert resolve_knowledge_mode(None, True, [], policy="grounded") == "grounded_miss"
    assert resolve_knowledge_mode(None, True, [], policy="fallback") == "fallback_miss"


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
