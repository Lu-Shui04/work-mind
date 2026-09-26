# server-py/tests/test_agent_tools.py
"""Agent 6 个工具的回归测试（不依赖 pytest，直接 python 运行即可）。

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


def test_calculate_thousands_separator():
    assert _call(T.calculate_tool, expression="1,500 + 200").endswith("= 1700")


def test_calculate_percent():
    assert _call(T.calculate_tool, expression="1500 * 20%").endswith("= 300")
    assert _call(T.calculate_tool, expression="50%").endswith("= 0.5")


def test_calculate_modulo_still_works():
    # % 后面跟数字 = 取模，不能一律当成百分比（带不带空格都要能认）
    out = _call(T.calculate_tool, expression="1500 % 7")
    assert out.endswith("= 2"), out
    assert _call(T.calculate_tool, expression="1500%7").endswith("= 2")


def test_calculate_fullwidth_input():
    assert _call(T.calculate_tool, expression="１５００ ＋ ２００").endswith("= 1700")


def test_calculate_decimal_no_float_noise():
    assert _call(T.calculate_tool, expression="0.1 + 0.2").endswith("= 0.3")


def test_calculate_rejects_text_with_units():
    """「3天，共580元」早期会被过滤成 3580 —— 必须报错，不能静默算错。"""
    out = _call(T.calculate_tool, expression="3天，共580元")
    assert "无法识别" in out, out
    assert "3580" not in out, out


def test_calculate_rejects_two_numbers_separated_by_space():
    out = _call(T.calculate_tool, expression="3天 580元")
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


def test_calculate_empty_and_overlong():
    assert "内容为空" in _call(T.calculate_tool, expression="   ")
    assert "过长" in _call(T.calculate_tool, expression="1+" * 200 + "1")


def test_calculate_has_no_eval():
    """静态兜底：源码里不允许再出现 eval/exec（literal_eval 除外）。"""
    src = open(os.path.join(os.path.dirname(T.__file__), "tools.py"), encoding="utf-8").read()
    assert re.search(r"(?<!literal_)\beval\s*\(", src) is None, "tools.py 不应再使用 eval"
    assert re.search(r"\bexec\s*\(", src) is None, "tools.py 不应使用 exec"
    assert "ast.parse" in src


# ── 工具4：get_date ──────────────────────────────────────────
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


def test_get_date_diff_is_order_insensitive():
    a = _call(T.get_date_tool, operation="diff", date1="2026-03-01", date2="2026-03-08")
    b = _call(T.get_date_tool, operation="diff", date1="2026-03-08", date2="2026-03-01")
    assert a == b, (a, b)


def test_get_date_add_days():
    out = _call(T.get_date_tool, operation="add_days", date1="2026-03-01", date2="45")
    assert "2026-04-15" in out, out


def test_get_date_invalid_format():
    out = _call(T.get_date_tool, operation="diff", date1="2026/03/01", date2="2026-03-08")
    assert "格式不正确" in out, out


def test_get_date_non_integer_days():
    out = _call(T.get_date_tool, operation="add_days", date1="2026-03-01", date2="三天")
    assert "必须是整数" in out, out


# ── 工具1：web_search ────────────────────────────────────────
def _with_provider(provider, **patch):
    """临时切换搜索后端（search_provider() 读模块级常量，所以直接改常量）。"""
    saved = (T.SEARCH_PROVIDER_ENV, T.TAVILY_API_KEY, T.BOCHA_API_KEY)
    T.SEARCH_PROVIDER_ENV = provider
    T.TAVILY_API_KEY = patch.get("tavily_key")
    T.BOCHA_API_KEY = patch.get("bocha_key")
    return saved


def _restore_provider(saved):
    T.SEARCH_PROVIDER_ENV, T.TAVILY_API_KEY, T.BOCHA_API_KEY = saved


def test_search_provider_autodetect():
    saved = _with_provider("auto", tavily_key=None, bocha_key=None)
    zhipu = T.ZHIPU_API_KEY
    try:
        T.ZHIPU_API_KEY = None
        assert T.search_provider() == "demo"
        T.ZHIPU_API_KEY = "zp-x"
        assert T.search_provider() == "zhipu", "智谱 key 是知识库在用的那个，应当能直接当搜索用"
        T.TAVILY_API_KEY = "tvly-x"
        assert T.search_provider() == "tavily", "多个 key 同时存在时 tavily 优先"
        T.TAVILY_API_KEY = None
        T.BOCHA_API_KEY = "sk-x"
        assert T.search_provider() == "bocha"
        T.SEARCH_PROVIDER_ENV = "demo"
        assert T.search_provider() == "demo", "显式指定优先于自动探测"
    finally:
        T.ZHIPU_API_KEY = zhipu
        _restore_provider(saved)


def test_zhipu_items_mapping():
    """智谱返回的 link 常常是空串，要用 media 兜底，不能让引用显示"来源：未知"。"""
    payload = {"search_result": [
        {"title": "标题1", "content": "正文1", "link": "https://a.example/1",
         "publish_date": "2026-09-17", "media": "官网"},
        {"title": "标题2", "content": "正文2", "link": "", "publish_date": "", "media": "微信公众号"},
    ]}
    items = T._zhipu_items(payload)
    assert len(items) == 2
    assert items[0]["url"] == "https://a.example/1" and items[0]["published"] == "2026-09-17"
    assert items[1]["url"] == "（来源：微信公众号）", items[1]
    assert T._zhipu_items({}) == []


def test_web_search_zhipu_formats_results():
    saved = _with_provider("zhipu")

    async def fake(query, count):
        return [{"title": "和平精英 SS30 赛季", "url": "https://x.example",
                 "content": "新赛季将于 2026-10-01 上线", "published": "2026-09-20"}]

    original = T._search_zhipu
    T._search_zhipu = fake
    try:
        out = _run(T.search_tool.ainvoke({"query": "和平精英 当前赛季"}))
        assert "SS30" in out and "2026-10-01" in out and "2026-09-20" in out, out
    finally:
        T._search_zhipu = original
        _restore_provider(saved)


def test_web_search_demo_known_topic():
    saved = _with_provider("demo")
    try:
        out = _run(T.search_tool.ainvoke({"query": "Vue 3 最新版本"}))
        assert "Vue" in out, out
        assert "演示数据" in out, "演示数据必须标注出来，否则模型会当成真实搜索结果"
    finally:
        _restore_provider(saved)


def test_web_search_demo_unknown_query_is_honest():
    """没接搜索服务时，必须说"没接入"，不能只回一句含糊的"该话题有广泛讨论"。"""
    saved = _with_provider("demo")
    try:
        out = _run(T.search_tool.ainvoke({"query": "和平精英 当前赛季"}))
        assert "未接入真实联网搜索" in out, out
        assert "Tavily".lower() in out.lower() or "TAVILY_API_KEY" in out, "要告诉管理员怎么启用真实搜索"
        assert "技术社区有广泛讨论" not in out, "早期话术会误导模型反复重试"
    finally:
        _restore_provider(saved)


def test_web_search_real_provider_formats_results():
    saved = _with_provider("tavily", tavily_key="tvly-test")

    async def fake_search(query, count):
        return [{"title": "标题A", "url": "https://example.com/a", "content": "正文内容"}]

    original = T._search_tavily
    T._search_tavily = fake_search
    try:
        out = _run(T.search_tool.ainvoke({"query": "测试查询"}))
        assert "标题A" in out and "https://example.com/a" in out and "正文内容" in out, out
    finally:
        T._search_tavily = original
        _restore_provider(saved)


def test_web_search_provider_failure_is_reported():
    saved = _with_provider("tavily", tavily_key="tvly-test")

    async def boom(query, count):
        raise RuntimeError("网络不可达")

    original = T._search_tavily
    T._search_tavily = boom
    try:
        out = _run(T.search_tool.ainvoke({"query": "测试查询"}))
        assert "联网搜索失败" in out and "网络不可达" in out, out
    finally:
        T._search_tavily = original
        _restore_provider(saved)


def test_web_search_empty_results_is_reported():
    saved = _with_provider("tavily", tavily_key="tvly-test")

    async def empty(query, count):
        return []

    original = T._search_tavily
    T._search_tavily = empty
    try:
        out = _run(T.search_tool.ainvoke({"query": "测试查询"}))
        assert "没有返回" in out, out
    finally:
        T._search_tavily = original
        _restore_provider(saved)


# ── 工具2：read_doc ──────────────────────────────────────────
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


def test_read_doc_survives_backend_failure():
    async def boom(question, user, k=None, filters=None):
        raise RuntimeError("数据库连不上")

    original, Q = _patch_retrieve(boom)
    try:
        out = _run(T.read_doc_tool.ainvoke({"question": "年假"}))
        assert "知识库暂时不可用" in out, out
    finally:
        Q.retrieve_with_meta = original


# ── 工具5：write_report ──────────────────────────────────────
def test_write_report_markdown_structure():
    payload = json.loads(_call(T.write_report_tool, title="周报", content="## 本周\n完成 A"))
    assert payload["success"] is True
    assert payload["format"] == "markdown"
    assert payload["content"].startswith("# 周报")
    assert "生成时间" in payload["content"]
    assert "## 本周" in payload["content"], "markdown 模式要保留标题标记"


def test_write_report_plain_strips_markdown():
    payload = json.loads(_call(T.write_report_tool, title="周报",
                               content="## 本周\n完成 **A**", format="plain"))
    body = payload["content"]
    assert not body.startswith("# "), body
    assert "##" not in body and "**" not in body, body
    assert "完成 A" in body


# ── 工具6：send_notify ───────────────────────────────────────
def test_send_notify_marks_simulated():
    payload = json.loads(_call(T.send_notify_tool, to="HR", subject="标题", message="正文"))
    assert payload["success"] is True
    assert payload["simulated"] is True, "演示环境必须标注未真实投递"
    assert "未真实投递" in payload["message"], payload["message"]


def test_send_notify_rejects_unknown_channel():
    try:
        _call(T.send_notify_tool, to="HR", subject="s", message="m", channel="telegram")
    except Exception:
        return
    raise AssertionError("channel 只允许 email/feishu/dingtalk，非法值应当被 schema 拒绝")


# ── 重复调用防护 ─────────────────────────────────────────────
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


def test_repeat_guard_is_noop_without_active_task():
    """没有任务上下文（比如单元测试直接调工具）时不启用防护。"""
    guarded = {t.name: t for t in T.guarded_tools}
    first = _run(guarded["calculate"].ainvoke({"expression": "3 * 3"}))
    second = _run(guarded["calculate"].ainvoke({"expression": "3 * 3"}))
    assert first.endswith("= 9") and second.endswith("= 9")


def test_guarded_tool_emits_single_tool_event():
    """防护层不能引入嵌套 runnable：否则每个工具调用的 SSE 事件会发两遍（步骤卡片翻倍）。"""
    guarded = {t.name: t for t in T.guarded_tools}

    async def count_events():
        starts = 0
        async for ev in guarded["calculate"].astream_events({"expression": "5 + 5"}, version="v2"):
            if ev["event"] == "on_tool_start":
                starts += 1
        return starts

    assert _run(count_events()) == 1


def test_guarded_tools_keep_names_and_schemas():
    assert [t.name for t in T.guarded_tools] == [t.name for t in T.all_tools]
    for raw, guard in zip(T.all_tools, T.guarded_tools):
        assert raw.description == guard.description, raw.name
        assert raw.args_schema is guard.args_schema, raw.name


# ── 步数用尽时的收尾逻辑 ─────────────────────────────────────
def test_should_continue_routes_to_finalize_at_step_limit():
    """步数用尽且模型还想调工具时，必须走 finalize 强制收尾。

    真实缺陷：早期直接 __end__，用户看到的"最终回答"是模型调工具前的一句过程说明
    （"I have enough information now. Let me ... produce the report."），然后就没了。
    """
    try:
        from langchain_core.messages import AIMessage

        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return

    def state(steps, wants_tool):
        last = (AIMessage(content="我再查一下", tool_calls=[
            {"name": "web_search", "args": {"query": "x"}, "id": "call_1"}])
            if wants_tool else AIMessage(content="最终回答"))
        return {"messages": [last], "steps": steps}

    assert A._should_continue(state(1, True)) == "tools"
    assert A._should_continue(state(1, False)) == "__end__"
    assert A._should_continue(state(A.MAX_STEPS, True)) == "finalize"
    assert A._should_continue(state(A.MAX_STEPS, False)) == "__end__"


def test_recursion_limit_leaves_room_for_max_steps():
    """LangGraph 的递归上限必须比 MAX_STEPS 需要的大，否则会先抛递归错误。"""
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    # 每个 ReAct 循环 = agent + tools 两个超级步
    needed = A.MAX_STEPS * 2 + 1
    assert A.RECURSION_LIMIT > needed, f"limit={A.RECURSION_LIMIT} 不足以跑满 {A.MAX_STEPS} 步"
    assert A.RECURSION_LIMIT > 25, "默认的 25 只够 12 步左右，必须显式放大"


def test_graph_wires_finalize_node():
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    nodes = set(A.agent_graph.get_graph().nodes)
    assert "finalize" in nodes, nodes
    assert "agent" in nodes and "tools" in nodes


def test_finalize_history_is_plain_text():
    """收尾分支只能拿到"人类可读的工具记录"，不能拿到原始消息历史。

    原始历史里最后一条是带着 tool_calls 却没人回应的 assistant 消息（DeepSeek 会 400），
    而且满屏 tool_calls 会让模型继续按调用格式输出（会被过滤器清空成空回答）。
    """
    try:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return

    messages = [
        HumanMessage(content="任务：查赛季"),
        AIMessage(content="我先搜一下", tool_calls=[
            {"name": "web_search", "args": {"query": "和平精英 赛季"}, "id": "c1"}]),
        ToolMessage(content="SS40 于 2026-08 上线", tool_call_id="c1"),
        AIMessage(content="再补一次", tool_calls=[
            {"name": "get_date", "args": {"operation": "today"}, "id": "c2"}]),
    ]
    text = A._summarize_tool_history(messages)
    assert "[调用] web_search" in text and "和平精英 赛季" in text, text
    assert "[结果] SS40 于 2026-08 上线" in text, text
    assert "[调用] get_date" in text, text
    # 关键：不能残留任何 tool_calls 结构（否则又变成"提示模型继续调工具"）
    assert "tool_calls" not in text and "tool_call_id" not in text
    # 没有 tool_calls 的普通消息仍然作为"过程"带上（带 tool_calls 的那条只记调用，不重复记文字）
    assert "[过程] 收尾前的说明" in A._summarize_tool_history([AIMessage(content="收尾前的说明")])


def test_finalize_history_handles_empty():
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    assert A._summarize_tool_history([]) == ""


def test_stream_sanitizer_passes_normal_text():
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    s = A._StreamSanitizer()
    assert s.feed("查询完成，结果是 ") == "查询完成，结果是 "
    assert s.feed("3390 元。") == "3390 元。"
    assert s.flush() == ""


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


def test_stream_sanitizer_resets_between_calls():
    """一次模型调用结束后必须重置，否则后面所有调用都会被误伤。"""
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    s = A._StreamSanitizer()
    s.feed("<" + A._DSML_MARK + " 乱码")
    s.flush()
    assert s.feed("下一轮的正常内容") == "下一轮的正常内容"


def test_finalize_prompt_forbids_tools():
    try:
        from app.services.agent import agent as A
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    assert "不要再要求调用任何工具" in A.FINALIZE_SYSTEM
    assert "没查到的" in A.FINALIZE_SYSTEM
    assert A._DSML_MARK in A.FINALIZE_SYSTEM, "提示词里要点名禁止的标记长什么样"


# ── 工具注册表与提示词的一致性（防止再次漂移）──────────────────
def test_tool_count_and_names():
    names = [t.name for t in T.all_tools]
    assert names == ["web_search", "read_doc", "calculate", "get_date",
                     "write_report", "send_notify"], names


def test_every_tool_has_description():
    for t in T.all_tools:
        assert t.description and len(t.description) > 10, f"{t.name} 缺少 description"


def test_tool_labels_cover_every_tool():
    """漏配中文名会让前端"可用工具"里直接显示 web_search 这种英文原名。"""
    from app.services.agent.agent import _TOOL_LABELS

    for t in T.all_tools:
        label = _TOOL_LABELS.get(t.name)
        assert label, f"工具 {t.name} 没有中文名"
        assert label != t.name, f"工具 {t.name} 的中文名没配（当前等于英文名）"
        assert not re.search(r"[a-zA-Z_]{3,}", label), f"{t.name} 的中文名里还有英文：{label}"


def test_system_prompt_lists_every_tool():
    """AGENT_SYSTEM 的工具清单由 all_tools 生成，不允许漏掉任何一个。"""
    try:
        from app.services.agent.agent import AGENT_SYSTEM
    except Exception as err:  # noqa: BLE001 - 无 API Key 时模型构造会失败，跳过即可
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    for t in T.all_tools:
        assert t.name in AGENT_SYSTEM, f"提示词里没有列出工具 {t.name}"


def test_route_labels_match_route_literal():
    try:
        from app.services.agent.agent import _ROUTE_LABELS, _RouteDecision
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    allowed = set(_RouteDecision.model_fields["route"].annotation.__args__)
    assert allowed == set(_ROUTE_LABELS), (allowed, set(_ROUTE_LABELS))


def test_tool_list_export_shape():
    try:
        from app.services.agent.agent import get_tool_list
    except Exception as err:  # noqa: BLE001
        print(f"    （跳过：导入 agent 模块失败 {type(err).__name__}）")
        return
    items = get_tool_list()
    assert len(items) == len(T.all_tools)
    for item in items:
        assert set(item) == {"name", "label", "description"}
        assert item["label"] != item["name"]


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
