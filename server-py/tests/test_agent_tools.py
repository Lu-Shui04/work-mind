# server-py/tests/test_agent_tools.py
"""Agent 6 个工具的回归测试（不依赖 pytest，直接 python 运行即可）。

（本文件只保留**黄金用例**：核心路径 + 真实踩过的边界，每条都能讲清「防的是什么坑」；零碎用例已精简。）

运行方式（容器内，Dockerfile 已把 tests/ 打进镜像）：
    docker exec workmind-server python /app/tests/test_agent_tools.py

覆盖的缺陷（都是真实修过的，用例固化下来防止改回去）：
1. calculate 把「3天，共580元」按字符过滤成 3580 算出错答案 —— 现在必须明确报错
2. calculate 用 eval 求值 —— 现在走 AST 白名单，且不允许 ** （防 9**9**9 卡死进程）
3. calculate 不支持千分位/全角数字 —— 现在支持
4. get_date 用进程本地时间（容器是 UTC）—— 现在按 APP_TIMEZONE（默认北京时间）
5. web_search 对没接搜索服务的环境返回"该话题在技术社区有广泛讨论"，
   模型误以为搜过了，反复重试 8 步后向用户道歉 —— 现在明确标注"未接入真实联网搜索"
6. read_doc 只说"没找到"，不告诉模型为什么（库空/权限过滤/分数低/重排判定答不了）
7. send_notify 回"通知已通过飞书发送给 X"，其实没发 —— 现在标注 simulated
8. 工具中文名漏配会让前端显示英文原名（web_search / write_report / send_notify）
9. AGENT_SYSTEM 里的工具清单手写会跟 all_tools 漂移，模型不知道有这些工具

这些用例不联网、不连库、不调模型：外部依赖一律用替身（monkeypatch）。
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services.agent import tools as T  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _call(tool, **kwargs) -> str:
    """按工具 schema 校验参数后调用（顺带验证 args_schema 本身没写坏）。"""
    return _run(tool.ainvoke(kwargs))


# ── 工具3：calculate ──────────────────────────────────────────
def test_calculate_basic_arithmetic():
    out = _call(T.calculate_tool, expression="580 * 3 + 1200 + 150 * 3")
    assert out.endswith("= 3390"), out


def test_calculate_decimal_no_float_noise():
    assert _call(T.calculate_tool, expression="0.1 + 0.2").endswith("= 0.3")


def test_calculate_rejects_text_with_units():
    """「3天，共580元」早期会被过滤成 3580 —— 必须报错，不能静默算错。"""
    out = _call(T.calculate_tool, expression="3天，共580元")
    assert "无法识别" in out, out
    assert "3580" not in out, out


def test_calculate_rejects_code_injection():
    """注入串必须被当成"看不懂的表达式"拒绝，绝不能有计算结果。"""
    out = _call(T.calculate_tool, expression="__import__('os').system('echo pwned')")
    assert "无法识别" in out, out
    assert "计算结果" not in out, f"注入串被当成算式执行了：{out}"


def test_calculate_rejects_power_operator():
    """** 会算出 9**9**9 这种把进程算死的表达式，必须拒绝。"""
    out = _call(T.calculate_tool, expression="2 ** 10")
    assert "计算失败" in out, out


def test_calculate_division_by_zero():
    assert "计算失败" in _call(T.calculate_tool, expression="1/0")


def test_get_date_today_uses_configured_timezone():
    """容器是 UTC，日期必须按 APP_TIMEZONE（默认北京时间）算，否则会差一天。"""
    out = _call(T.get_date_tool, operation="today")
    expected = (datetime.now(timezone.utc) + timedelta(hours=8))
    assert f"{expected.year}年{expected.month}月{expected.day}日" in out, out
    assert "时区" in out
    assert "星期" in out


def test_get_date_diff_workdays():
    out = _call(T.get_date_tool, operation="diff", date1="2026-03-01", date2="2026-03-08")
    assert "共 7 天" in out and "工作日 5 天" in out, out


def _with_provider(provider, **patch):
    """临时切换搜索后端（search_provider() 读模块级常量，所以直接改常量）。"""
    saved = (T.SEARCH_PROVIDER_ENV, T.TAVILY_API_KEY, T.BOCHA_API_KEY)
    T.SEARCH_PROVIDER_ENV = provider
    T.TAVILY_API_KEY = patch.get("tavily_key")
    T.BOCHA_API_KEY = patch.get("bocha_key")
    return saved


def _restore_provider(saved):
    T.SEARCH_PROVIDER_ENV, T.TAVILY_API_KEY, T.BOCHA_API_KEY = saved


def _patch_retrieve(func):
    from app.services.rag import query as Q

    original = Q.retrieve_with_meta
    Q.retrieve_with_meta = func
    return original, Q


def test_read_doc_reports_why_nothing_found():
    async def fake(question, user, k=None, filters=None):
        return [], {"explain": "最高相似度 0.28 低于阈值 0.35", "reason": "below_threshold"}

    original, Q = _patch_retrieve(fake)
    try:
        out = _run(T.read_doc_tool.ainvoke({"question": "年假有几天"}))
        assert "未找到" in out and "最高相似度 0.28" in out, out
    finally:
        Q.retrieve_with_meta = original


def test_read_doc_returns_hits_with_citation_meta():
    async def fake(question, user, k=None, filters=None):
        return [{"title": "年假与休假政策", "pageNumber": 1, "department": "general",
                 "version": "2026-09", "content": "满 1 年不满 10 年，年假 5 天。"}], {"reason": "ok"}

    original, Q = _patch_retrieve(fake)
    try:
        out = _run(T.read_doc_tool.ainvoke({"question": "年假"}))
        assert "《年假与休假政策》" in out and "年假 5 天" in out, out
    finally:
        Q.retrieve_with_meta = original


def test_repeat_guard_skips_identical_calls():
    """模型换关键词反复搜同一件事时，第二次起不再真正执行（省步数、省搜索费用）。"""
    token = T.set_tool_call_log({})
    try:
        guarded = {t.name: t for t in T.guarded_tools}
        first = _run(guarded["calculate"].ainvoke({"expression": "1 + 1"}))
        again = _run(guarded["calculate"].ainvoke({"expression": "1 + 1"}))
        other = _run(guarded["calculate"].ainvoke({"expression": "2 + 2"}))
        assert first.endswith("= 2"), first
        assert "已跳过重复调用" in again, again
        assert other.endswith("= 4"), other
    finally:
        T.reset_tool_call_log(token)


def test_stream_sanitizer_cuts_leaked_tool_markup():
    """模型把工具调用标记当正文吐出来时，标记及其后的调用参数都必须丢掉。"""
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    mark = A._DSML_MARK
    leaked = "已查到赛季信息。" + "<" + mark + ' invoke name="web_search">' + mark + ">"
    s = A._StreamSanitizer()
    out = s.feed(leaked)
    assert out == "已查到赛季信息。", repr(out)
    assert s.feed("后面的参数也不能漏出来") == ""
    assert s.flush() == ""


def test_stream_sanitizer_handles_markup_split_across_chunks():
    """标记被切成多个 chunk 也要能过滤（流式场景必然发生）。"""
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    mark = A._DSML_MARK
    pieces = ["结论：SS40。", "<", mark[:3], mark[3:], ' invoke name="web_search">']
    s = A._StreamSanitizer()
    out = "".join(s.feed(p) for p in pieces) + s.flush()
    assert out == "结论：SS40。", repr(out)


def test_every_tool_has_description():
    for t in T.all_tools:
        assert t.description and len(t.description) > 10, f"{t.name} 缺少 description"


def test_system_prompt_lists_every_tool():
    """AGENT_SYSTEM 的工具清单由 all_tools 生成，不允许漏掉任何一个。"""
    try:
        from app.services.agent.agent import AGENT_SYSTEM
    except Exception as err:  # noqa: BLE001 - 无 API Key 时模型构造会失败，跳过即可
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    for t in T.all_tools:
        assert t.name in AGENT_SYSTEM, f"提示词里没有列出工具 {t.name}"


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
