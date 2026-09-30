# server-py/tests/test_agent_graph.py
"""任务 Agent 的状态契约与错误路径回归（不联网、不连库、不调模型）。

（本文件只保留**黄金用例**：核心路径 + 真实踩过的边界，每条都能讲清「防的是什么坑」；零碎用例已精简。）

运行方式（容器内，Dockerfile 已把 tests/ 打进镜像）：
    docker exec workmind-server python /app/tests/test_agent_graph.py

固化的是两条**静默故障**（都真实发生过，且都不会报错，只能靠用例守住）：

1. **state 字段契约**：LangGraph 的 StateGraph 是**按 TypedDict 的注解**建状态通道的，
   run_agent 往 astream_events 里多传的键会被它**静默丢掉**（不报错、不警告）。
   之前 department_hint / search_query 就漏在 AgentState 注解外面，节点里
   state.get() 永远是 None —— 部门提示与改写后的检索词会静默失效，退回用原句检索，
   而日志里什么都看不出来。
2. **错误路径不许自己崩**：预检索抛非预期异常时（embedding 上游 5xx/401 抛的
   APIStatusError 既不是 StorageUnavailable 也不是 ValueError，run_agent 里接不住），
   except 分支如果引用一个还没赋值的局部变量（route），就会抛 UnboundLocalError：
   真实错误被顶掉、error 事件发不出去、追踪里没有 error 步骤，
   用户只看到路由看门狗那句泛化的"任务执行中断了"。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services.agent import agent as A            # noqa: E402
from app.services.rag.intent import IntentDecision   # noqa: E402


def run(coro):
    return asyncio.run(coro)


# run_agent 里 astream_events 传进图的键（改动那一处就要同步改这里）
RUN_AGENT_STATE_KEYS = (
    "task", "route", "messages", "steps", "sources", "recall", "miss_notice",
    "prefetched", "memory", "department_hint", "search_query",
)


# ── 契约一：进图的每个键都必须在 AgentState 里声明 ────────────────
def test_every_key_passed_into_the_graph_is_declared():
    declared = set(A.AgentState.__annotations__)
    missing = [k for k in RUN_AGENT_STATE_KEYS if k not in declared]
    assert not missing, (
        "这些键没有在 AgentState 里声明，进图时会被 LangGraph 静默丢掉：" + ", ".join(missing)
    )


def test_declared_keys_survive_the_graph_boundary():
    """把"上面那条断言为什么重要"钉死：声明过的字段确实到得了节点。"""
    from langgraph.graph import END, START, StateGraph

    seen: dict = {}

    def probe(state):
        seen.update(dict(state))
        return {}

    builder = StateGraph(A.AgentState)
    builder.add_node("probe", probe)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    graph = builder.compile()

    run(graph.ainvoke({"task": "t", "messages": [], "steps": 0,
                       "department_hint": "hr", "search_query": "年假"}))
    assert seen.get("department_hint") == "hr", seen
    assert seen.get("search_query") == "年假", seen


# ── 契约二：预检索失败要如实报错，而不是自己在 except 里崩 ──────────
def test_presearch_failure_emits_error_event():
    async def body():
        events: list = []

        async def on_event(kind, data):
            events.append((kind, data))

        async def fake_intent(_task):
            return IntentDecision(need_knowledge=True, query="年假政策",
                                  decision_source="recall-first", reason="测试替身")

        async def boom(*_a, **_kw):
            raise RuntimeError("embedding upstream 502")

        original = (A.classify_intent, A.retrieve_with_meta)
        A.classify_intent, A.retrieve_with_meta = fake_intent, boom
        try:
            # 这条路径以前会抛 UnboundLocalError（route 未赋值），于是永远发不出 error 事件
            await A.run_agent("公司年假有几天？", on_event)
        finally:
            A.classify_intent, A.retrieve_with_meta = original

        kinds = [k for k, _ in events]
        assert kinds and kinds[-1] == "error", kinds
        message = events[-1][1]["message"]
        assert "embedding upstream 502" in message, message

    run(body())


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
