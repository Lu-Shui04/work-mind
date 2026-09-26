# server-py/app/services/agent/agent.py
# 任务 Agent：LangGraph 意图分类路由 + ReAct 工具循环 + 知识库问答分支
#
#   1) 先按 rag/intent.py 的"召回优先"判定要不要查库（与智能对话共用同一判定）
#        ├── 需要 → knowledge → 检索节点 → 生成带引用的回答
#        └── 不需要 → 2) 才让模型在下面两条路里二选一
#                      ├── tool → ReAct：模型自主选工具 ⇄ 执行 → 最终回答
#                      └── chat → 直接回答（通用写作/闲聊）
#
# 为什么要做意图分类而不是"一律走 ReAct"：
# - ReAct 每一轮都要重新发一次完整上下文，纯问答场景多花 2~4 倍 token
# - 知识与工具是两类不同的失败模式：知识分支要的是"检索准 + 敢说没有"，
#   工具分支要的是"多步规划 + 参数正确"，混在一起提示词会互相干扰
# - 分类结果随 SSE 下发，用户能看到"这次为什么去查文档/为什么调工具"（可解释）
import json
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from app.services.agent.tools import all_tools
from app.services.db import StorageUnavailable
from app.services.identity import User, get_current_user, reset_current_user, set_current_user
from app.services.model import create_chat_model
from app.services.rag.intent import classify_intent
from app.services.rag.query import (
    SearchFilters, build_context, no_knowledge_reply, retrieve_with_meta,
)
from app.utils.logger import logger

AGENT_SYSTEM = """你是 WorkMind AI 任务助手，专门处理办公场景的复杂任务。

可用工具：
- read_doc：从公司知识库检索文档（只能检索当前用户有权限的内容）
- calculate：数学计算（金额、工期等）
- get_date：日期查询和计算

工作原则：
1. 先理解任务的完整需求，想好需要哪些步骤
2. 按最少工具调用完成任务，避免重复查询
3. 获取到足够信息后立刻生成最终回答，不要继续无谓的工具调用
4. 工具返回内容不足时如实说明，不要编造
5. 每次只调用一个工具，等结果回来再决定下一步；最多 8 步"""

CHAT_SYSTEM = "你是 WorkMind AI 助手，回答简洁专业。不知道的信息如实说明，不要编造公司内部规定。"

KNOWLEDGE_SYSTEM = """你是 WorkMind AI 知识库助手。

规则：
1. 只根据下方提供的参考文档回答问题，不使用文档之外的知识
2. 如果文档中没有相关内容，明确说"知识库中未找到相关内容"
3. 回答准确简洁，引用处标注【来源：文档标题 · 第N页】"""


class AgentState(TypedDict):
    task: str
    route: str
    messages: Annotated[list, add_messages]
    steps: int
    sources: list
    # 召回诊断：没命中时说明原因（库空 / 被权限过滤 / 分数低于阈值），随 SSE 下发
    recall: dict
    # 是否已在入口预检索过（预检索是为了"没命中就还给工具分支"，见 run_agent）
    prefetched: bool


# ── 意图分类 ──────────────────────────────────────────────────────────
class _RouteDecision(BaseModel):
    route: Literal["knowledge", "tool", "chat"] = Field(
        description="knowledge=需要查公司内部文档(制度/规范/流程/政策)；"
                    "tool=需要计算/日期/多步工具调用；chat=通用问答、写作、闲聊")
    reason: str = Field(description="判断依据，一句话")
    search_query: str = Field(default="", description="route=knowledge 时，用于检索的查询语句")


_router_model = create_chat_model(temperature=0, streaming=False)
_agent_model = create_chat_model(temperature=0, streaming=True)
_model_with_tools = _agent_model.bind_tools(all_tools)
_tool_node = ToolNode(all_tools)


async def classify_task(task: str) -> _RouteDecision:
    try:
        return await _router_model.with_structured_output(_RouteDecision, method="function_calling").ainvoke([
            {"role": "system", "content": "你是任务路由器，判断任务应该走哪条执行路径。"},
            {"role": "user", "content": task},
        ])
    except Exception as err:
        logger.warn("agent: classify failed, fallback to tool", {"error": str(err)})
        return _RouteDecision(route="tool", reason="分类失败，回退到工具执行路径", search_query=task)


# ── 分支一：知识库检索 + 引用回答 ─────────────────────────────────────
async def knowledge_retrieve(state: AgentState):
    # 入口处已经预检索过就把结果直接用掉，避免同一个任务检索两次
    if state.get("prefetched"):
        return {"sources": state.get("sources") or [], "recall": state.get("recall") or {}}

    user = get_current_user()
    decision_query = state.get("search_query") or state["task"]
    # 部门提示只做提示，不做硬过滤（见 rag/intent.py 的说明）
    filters = SearchFilters(department_hint=state.get("department_hint"))
    recall: dict = {}
    try:
        sources, recall = await retrieve_with_meta(decision_query, user, filters=filters)
    except StorageUnavailable as err:
        logger.error("agent: knowledge storage unavailable", {"error": str(err)})
        sources, recall = [], {"hit": 0, "reason": "storage_unavailable", "explain": str(err)}
    except ValueError as err:
        logger.warn("agent: knowledge unavailable", {"error": str(err)})
        sources, recall = [], {"hit": 0, "reason": "embedding_unavailable", "explain": str(err)}
    logger.info("agent: knowledge retrieve", {"hits": len(sources), "user": user.user_id,
                                              "reason": recall.get("reason")})
    return {"sources": sources, "recall": recall}


async def knowledge_answer(state: AgentState):
    sources = state.get("sources") or []
    if not sources:
        # 与智能对话共用同一套"未命中"答复：说清楚为什么没找到 + 下一步怎么办
        return {"messages": [AIMessage(content=no_knowledge_reply(state.get("recall")))]}

    context = build_context(sources)
    prompt = [
        SystemMessage(content=KNOWLEDGE_SYSTEM),
        HumanMessage(content=f"参考文档：\n{context}\n\n问题：{state['task']}"),
    ]
    full = ""
    async for chunk in _agent_model.astream(prompt):
        full += chunk.content or ""
    return {"messages": [AIMessage(content=full)]}


# ── 分支二：直接回答 ──────────────────────────────────────────────────
async def direct_answer(state: AgentState):
    full = ""
    async for chunk in _agent_model.astream([
        SystemMessage(content=CHAT_SYSTEM), HumanMessage(content=state["task"]),
    ]):
        full += chunk.content or ""
    return {"messages": [AIMessage(content=full)]}


# ── 分支三：ReAct 工具循环 ────────────────────────────────────────────
def _should_continue(state: AgentState):
    last = state["messages"][-1]
    if state.get("steps", 0) >= 8:
        logger.warn("agent: max steps reached", {"steps": state.get("steps")})
        return "__end__"
    return "tools" if getattr(last, "tool_calls", None) else "__end__"


async def _agent_node(state: AgentState):
    response = await _model_with_tools.ainvoke(
        [SystemMessage(content=AGENT_SYSTEM), HumanMessage(content=state["task"]), *state["messages"]]
    )
    return {"messages": [response], "steps": state.get("steps", 0) + 1}


_builder = StateGraph(AgentState)
_builder.add_node("knowledge_retrieve", knowledge_retrieve)
_builder.add_node("knowledge_answer", knowledge_answer)
_builder.add_node("direct_answer", direct_answer)
_builder.add_node("agent", _agent_node)
_builder.add_node("tools", _tool_node)

_builder.add_conditional_edges(START, lambda s: s.get("route", "tool"), {
    "knowledge": "knowledge_retrieve",
    "chat": "direct_answer",
    "tool": "agent",
})
_builder.add_edge("knowledge_retrieve", "knowledge_answer")
_builder.add_edge("knowledge_answer", END)
_builder.add_edge("direct_answer", END)
_builder.add_conditional_edges("agent", _should_continue, {"tools": "tools", "__end__": END})
_builder.add_edge("tools", "agent")
agent_graph = _builder.compile()


_TOOL_LABELS = {
    "read_doc": "检索知识库",
    "calculate": "数学计算",
    "get_date": "日期查询",
}
_ROUTE_LABELS = {"knowledge": "知识库问答", "tool": "工具执行", "chat": "直接回答"}


def _get_tool_label(tool_name: str) -> str:
    return _TOOL_LABELS.get(tool_name, tool_name)


async def run_agent(task: str, on_event, user: User | None = None,
                    use_knowledge: bool | None = None):
    """流式执行：推送 意图 → 工具调用 → token → done。

    身份通过 ContextVar 传递，工具在调用链深处也能做权限过滤。

    路由顺序（与"智能对话"共用同一个召回判定，避免两条链路行为不一致）：
      1. 先按 rag/intent.py 的"召回优先"判定要不要查库 —— 需要查库就一定走知识分支，
         不再把这个决定权交给模型路由器（模型不知道公司库里有什么，
         它会说"这属于通用技术问题、无需检索"，用户看到的就是"Agent 不去查知识库"）；
      2. 只有确定不查库时，才让模型在 tool / chat 之间二选一。
    """
    token = set_current_user(user or User())
    logger.info("agent: start", {"task": task[:60], "user": (user or User()).user_id})

    streamed = False
    step_count = 0
    input_tokens = 0
    output_tokens = 0
    try:
        # 1) 要不要查知识库：与智能对话共用 rag/intent.py 的判定
        intent = await classify_intent(task)
        if use_knowledge is True:
            need, source, intent_reason = True, "forced", "用户选择了「强制检索知识库」"
        elif use_knowledge is False:
            need, source, intent_reason = False, "disabled", "用户选择了「关闭知识库检索」"
        else:
            need, source, intent_reason = (intent.need_knowledge, intent.decision_source,
                                           intent.reason)

        # 2) 先真的检索一次，再决定路由。
        #    为什么不是"需要查库就直接进知识分支"：Agent 还有计算/日期/通知等工具，
        #    一句"算一下 1500+800*0.8"如果被塞进知识分支，会因为没有命中而直接结束，
        #    工具根本没机会执行。所以规则是：
        #      检索有命中 → 走知识分支（带引用回答）
        #      检索没命中 → 交还给模型路由器（tool / chat），但把"没命中"的原因告诉用户
        user_obj = user or User()
        pre_sources: list = []
        pre_recall: dict = {}
        if need:
            filters = SearchFilters(department_hint=intent.department_hint)
            try:
                pre_sources, pre_recall = await retrieve_with_meta(
                    intent.query or task, user_obj, filters=filters)
            except (StorageUnavailable, ValueError) as err:
                logger.warn("agent: knowledge unavailable", {"error": str(err)})
                pre_recall = {"hit": 0, "reason": "unavailable", "explain": str(err)}

        if need and pre_sources:
            route = "knowledge"
            route_reason = f"{intent_reason}：命中 {len(pre_sources)} 条"
            search_query = intent.query or task
        else:
            decision = await classify_task(task)
            # 用户明确关掉了知识库时，模型也不许再选 knowledge（否则等于没关掉）
            route = "chat" if decision.route == "knowledge" else decision.route
            if need:
                why = (pre_recall or {}).get("explain") or "没有可用内容"
                route_reason = f"已按召回优先检索知识库：{why} → {decision.reason}"
            else:
                route_reason = f"{intent_reason} → {decision.reason}"
            search_query = decision.search_query or task
            # 知识分支不会执行，这里把检索诊断直接推给前端（否则用户不知道为什么没走知识库）
            if need:
                await on_event("sources", {"sources": [], "from": "agent-precheck",
                                            "recall": pre_recall})
            # 没命中就走别的分支，别让预检索结果影响后面的节点
            pre_sources, pre_recall = [], {}

        await on_event("intent", {
            "route": route,
            "routeLabel": _ROUTE_LABELS.get(route, route),
            "reason": route_reason,
            "searchQuery": search_query,
            "needKnowledge": need,
            "decisionSource": source,
            "departmentHint": intent.department_hint,
            "ruleHit": intent.rule_hit,
        })

        final_content = ""
        async for event in agent_graph.astream_events(
            {"task": task, "route": route, "messages": [], "steps": 0,
             "sources": pre_sources, "recall": pre_recall,
             "prefetched": route == "knowledge" and bool(pre_sources),
             "department_hint": intent.department_hint,
             "search_query": search_query},
            version="v2",
        ):
            event_type = event["event"]
            name = event["name"]
            data = event.get("data", {}) or {}

            # 检索完成 → 推引用来源（前端在回答之前就能看到"参考了哪些文档"）
            if event_type == "on_chain_end" and name == "knowledge_retrieve":
                output = data.get("output") or {}
                sources = output.get("sources") if isinstance(output, dict) else None
                if sources is not None:
                    await on_event("sources", {
                        "sources": sources, "from": "agent",
                        # 没有命中时前端能直接说明原因（库空 / 被过滤 / 分数低于阈值）
                        "recall": output.get("recall") if isinstance(output, dict) else None,
                    })

            if event_type == "on_tool_start":
                step_count += 1
                await on_event("tool_call", {
                    "step": step_count, "toolName": name,
                    "args": data.get("input"), "label": _get_tool_label(name),
                })

            if event_type == "on_tool_end":
                output = data.get("output")
                result = getattr(output, "content", output)
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        pass
                await on_event("tool_result", {
                    "toolName": name, "result": result,
                    "resultText": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                })

            if event_type == "on_chat_model_stream":
                chunk = data.get("chunk")
                content = getattr(chunk, "content", None) if chunk else None
                tool_chunks = getattr(chunk, "tool_call_chunks", None) if chunk else None
                if content and not tool_chunks:
                    streamed = True
                    final_content += content
                    await on_event("token", {"token": content})
                # 用量统计：把每一步模型调用的 token 累加起来，看板才有真实成本
                usage = getattr(chunk, "usage_metadata", None) if chunk else None
                if usage:
                    input_tokens += usage.get("input_tokens", 0) or 0
                    output_tokens += usage.get("output_tokens", 0) or 0

            # 兜底：某些分支（未命中知识库、非流式节点）没有 token 事件，用最终消息补一次
            if event_type == "on_chain_end" and name == "LangGraph":
                output = data.get("output")
                if isinstance(output, dict):
                    msgs = output.get("messages") or []
                    if msgs:
                        last = msgs[-1]
                        text = getattr(last, "content", "") or ""
                        if text and not streamed:
                            final_content = text
                            await on_event("token", {"token": text})

        await on_event("done", {"steps": step_count, "route": route,
                                "contentLength": len(final_content),
                                "inputTokens": input_tokens, "outputTokens": output_tokens})
        logger.info("agent: done", {"steps": step_count, "route": route})
    except Exception as err:
        logger.error("agent: error", {"error": str(err)})
        await on_event("error", {"message": str(err) or "Agent 执行出错"})
    finally:
        reset_current_user(token)


def get_tool_list() -> list[dict]:
    return [{"name": t.name, "label": _get_tool_label(t.name), "description": t.description} for t in all_tools]
