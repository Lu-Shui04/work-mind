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
import os
import time
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from app.services.agent.tools import (
    all_tools, guarded_tools, reset_tool_call_log, reset_tool_sources,
    set_tool_call_log, set_tool_sources,
)
from app.services.chat.memory import append_turn, memory_block
from app.core.db import StorageUnavailable
from app.core.identity import User, get_current_user, reset_current_user, set_current_user
from app.models import pricing
from app.models.llm import create_chat_model, primary_model_name
from app.services.rag.intent import classify_intent, looks_like_knowledge_need
from app.infra.trace import trace_step
from app.services.rag.query import SearchFilters, build_context, retrieve_with_meta
from app.prompts.chat import MISS_FIRST_RULE, miss_notice as miss_notice_text, miss_reply
from app.prompts.agent import (
    CHAT_SYSTEM, KNOWLEDGE_SYSTEM, ROUTE_SYSTEM, agent_system, finalize_system,
    FINALIZE_RETRY_SYSTEM,
)
from app.core.logger import logger
from app.infra.tokens import cache_read_of


def _tool_catalog() -> str:
    """把注册在案的工具全部写进提示词。

    为什么由 all_tools 生成而不是手写：手写清单会跟代码漂移 —— 之前这里只写了
    read_doc / calculate / get_date，web_search / write_report / send_notify
    虽然在 bind_tools 里，提示词却没提，模型基本不会去用，
    示例任务「技术调研」也就只会去查知识库然后回一句"没找到"。
    """
    return "\n".join(f"- {t.name}：{t.description}" for t in all_tools)


# 工具循环的最大步数：一步 = 一次模型调用（模型可以在一步里并行调多个工具）。
#
# 这个值不是拍的，是实测标定出来的（把上限临时放到 20，让 10 次真实任务自然收敛）：
#   知识库问答类      0 步（直接走知识分支）
#   单工具类任务      3~4 步（费用计算 / 知识查询 / 简单搜索）
#   搜索+报告类任务   5~6 步（技术调研、能查到答案的资讯查询）
#   查不到答案的任务  9~10 步（模型反复换关键词，最后自己收敛并如实说"没查到"）
# 分布：中位数 5，最大 10。
#
# 取值 14 = 观测最大值 + 40% 余量：
#   - 不裁掉任何"能自然收敛"的任务（观测到的自然收敛最长 10 步）
#   - 又能拦住真正失控的循环（早期版本 8 步里发了 14 次搜索还停不下来）
# 撞上限时会走 finalize 强制收尾（用不带工具的模型基于已有信息给总结），
# 所以即便真的撞上，用户拿到的也还是一份完整回答，而不是半截话。
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "14"))

# LangGraph 自己的递归上限（超级步）必须跟着 MAX_STEPS 一起放大。
# 每个 ReAct 循环占 2 个超级步（agent 一步、tools 一步），默认上限 25
# 只够跑到 12 步左右 —— 超过就会在图里抛
# "Recursion limit of 25 reached without hitting a stop condition"，
# 而且是在我们自己的 max steps 判断之前抛，用户看到的是一个英文报错。
# 实测把 MAX_STEPS 从 10 调到 14 时正好踩到这一点。
RECURSION_LIMIT = MAX_STEPS * 2 + 5

# 提示词本身在 app/prompts/agent.py（提示词层），这里只把"运行时要插的值"喂进去
AGENT_SYSTEM = agent_system(_tool_catalog(), MAX_STEPS)


class AgentState(TypedDict):
    task: str
    route: str
    # 知识库查过但没查到时的开场白（""=不需要说明）：
    # 用户要求"库里没有的内容必须先说明"，这个字段一路带到回答节点，见 miss_notice_text()
    miss_notice: str
    messages: Annotated[list, add_messages]
    steps: int
    sources: list
    # 召回诊断：没命中时说明原因（库空 / 被权限过滤 / 分数低于阈值），随 SSE 下发
    recall: dict
    # 是否已在入口预检索过（预检索是为了"没命中就还给工具分支"，见 run_agent）
    prefetched: bool
    # 会话记忆：【此前对话摘要】+ 最近 N 轮原文（见 chat/memory.py）。
    # 由 run_agent 注入，各分支把它拼进自己的 system prompt ——
    # 这样"上一个任务说过什么"在工具循环、直接回答、收尾里都看得见。
    memory: str
    # 意图判定给出的检索词与部门提示（部门提示只做提示/解释，不做硬过滤，
    # 见 rag/query.py 的 SearchFilters）。
    #
    # ⚠️ 这两个字段**必须声明在这里**：StateGraph 的状态通道就是按本 TypedDict 的注解建的，
    #    run_agent 往 astream_events 里多传的键会被 LangGraph **静默丢掉** ——
    #    不报错、不警告，节点里 state.get() 直接是 None（实测确认）。
    #    之前它们漏在注解外，于是 knowledge_retrieve 里的
    #    state.get("department_hint") / state.get("search_query") 永远是 None：
    #    一旦路由改动让"入口不预检索"那条兜底路径真的跑起来，部门提示与改写后的检索词
    #    就会静默失效（退回用原句检索），而且没有任何报错可查。
    department_hint: str
    search_query: str


# ── 意图分类 ──────────────────────────────────────────────────────────
class _RouteDecision(BaseModel):
    route: Literal["knowledge", "tool", "chat"] = Field(
        description="knowledge=需要查公司内部文档(制度/规范/流程/政策)；"
                    "tool=需要调用工具才能完成：联网查最新信息/版本、数学计算、日期计算、"
                    "生成并保存报告、发送通知、以及需要多步工具协作的任务；"
                    "chat=不需要任何工具、也不需要公司文档的纯问答或闲聊")
    reason: str = Field(description="判断依据，一句话")
    search_query: str = Field(default="", description="route=knowledge 时，用于检索的查询语句")


_router_model = create_chat_model(temperature=0, streaming=False)
_agent_model = create_chat_model(temperature=0, streaming=True)
# 图里绑定带"重复调用防护"的工具（见 tools.py 的 _repeat_guard），
# 提示词和 /api/agent/tools 仍然用原始的 all_tools
_model_with_tools = _agent_model.bind_tools(guarded_tools)
_tool_node = ToolNode(guarded_tools)


# 知识分支（knowledge_answer）只会"读文档 + 回答"：它**不会算数、也不会发通知/生成报告**。
# 所以任务里一旦出现这类诉求，路由判成 knowledge 就等于把这些步骤**整段丢掉** ——
# 实测就发生过：任务「查年假政策 + 按15天算我剩余天数 + 通知HR」被路由到知识分支，
# 结果既没算数也没发通知，用户只拿到一段政策文字（评测集 agent-004 反复复现，
# 三次运行里挂了两次）。这里给它一个**确定性**的硬信号，不再只靠模型判一次。
#
# 关键词取得偏保守（要"动作"而不是"名词"）：
#   「报销制度里餐费标准怎么计算？」不该被当成计算任务，所以用「计算一下」而不是「计算」；
#   「告诉我年假有几天」是纯提问，所以 notify 里不放「告诉」。
_TOOL_CAPABILITY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("计算", ("算一下", "帮我算", "计算一下", "等于多少", "还剩多少", "还剩几",
              "已用", "扣除", "合计", "总计", "换算成", "乘以", "除以")),
    ("发送通知", ("通知", "发给", "发送给", "发一下", "发下", "发送", "发到", "推送", "提醒", "告知")),
    ("生成报告", ("生成报告", "写一份报告", "出一份", "生成一份", "整理成报告",
                  "写周报", "会议纪要")),
)


def _tool_capability_need(task: str) -> str | None:
    """任务是否要求"知识分支做不到"的能力；返回能力名（用于解释路由），否则 None。"""
    text = task or ""
    for capability, hints in _TOOL_CAPABILITY_HINTS:
        if any(h in text for h in hints):
            return capability
    return None


async def classify_task(task: str) -> _RouteDecision:
    try:
        return await _router_model.with_structured_output(_RouteDecision, method="function_calling").ainvoke([
            {"role": "system", "content": ROUTE_SYSTEM},
            {"role": "user", "content": task},
        ])
    except Exception as err:
        # 这里**静默**回退成 tool：模型路由器挂了不会让任务失败，但会让"纯知识问答走知识分支"
        # 这条策略悄悄失效。实测就吃过一次亏 —— 模型外壳的参数名冲突导致路由 100% 失败，
        # 表现只是"回答质量好像变差了"，没人发现。所以除了日志，还要在**全链路追踪**里留痕。
        logger.warn("agent: classify failed, fallback to tool", {"error": str(err)})
        trace_step("route", "任务路由失败，回退到工具分支", status="error",
                   detail={"error": f"{type(err).__name__}: {err}"[:300],
                           "fallbackRoute": "tool",
                           "impact": "本应走知识分支的任务会改走工具分支，回答质量可能下降"})
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
        # 与智能对话共用同一句"未命中"答复（query.py 的 miss_reply）：
        # 说清没查到 + 引导用户把问题问具体，不调用模型、不拿通用知识顶
        return {"messages": [AIMessage(content=miss_reply())]}

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
    # 知识库查过但没查到：提示词要求模型第一句先说明（见 MISS_FIRST_RULE）。
    # 不在这里往 content 里拼说明 —— 前端看的是**流式 token**，拼在最终消息上不会出现在流里，
    # 那句说明由路由层作为第一个 token 发出去（见 run_agent 的 kb_missed 分支）。
    miss = (state.get("miss_notice") or "").strip()
    system = _system_with_memory(CHAT_SYSTEM + (("\n\n" + MISS_FIRST_RULE) if miss else ""), state)
    full = ""
    async for chunk in _agent_model.astream([
        SystemMessage(content=system),
        HumanMessage(content=state["task"]),
    ]):
        full += chunk.content or ""
    return {"messages": [AIMessage(content=full)]}


# ── 分支三：ReAct 工具循环 ────────────────────────────────────────────
def _should_continue(state: AgentState):
    last = state["messages"][-1]
    if state.get("steps", 0) >= MAX_STEPS:
        # 步数用尽时有个坑：如果这时模型正打算调工具，直接结束图的话，
        # 用户拿到的"最终回答"其实是模型调工具前的一句过程说明
        # （实测出现过 "I have enough information now. Let me ... produce the report."
        #   然后什么都没有了）。所以这种情况要强制走 finalize，让模型交一份真正的总结。
        if getattr(last, "tool_calls", None):
            logger.warn("agent: max steps reached, force finalize", {"steps": state.get("steps")})
            return "finalize"
        logger.warn("agent: max steps reached", {"steps": state.get("steps")})
        return "__end__"
    return "tools" if getattr(last, "tool_calls", None) else "__end__"


def _system_with_memory(base: str, state: AgentState) -> str:
    """把会话记忆拼到 system prompt 后面（没有记忆就原样返回）。"""
    memory = (state.get("memory") or "").strip()
    if not memory:
        return base
    return (base + "\n\n【会话记忆】以下是本次会话之前的内容，回答时可以直接引用，"
            "不要重复询问用户已经说过的事情：\n" + memory)


async def _agent_node(state: AgentState):
    # 走工具分支也可能是因为"知识库没查到"：这种情况下最终回答同样要先说明（见 direct_answer）
    base = AGENT_SYSTEM + ("\n\n" + MISS_FIRST_RULE if (state.get("miss_notice") or "").strip() else "")
    response = await _model_with_tools.ainvoke(
        [_system_with_memory(base, state), HumanMessage(content=state["task"]),
         *state["messages"]]
    )
    return {"messages": [response], "steps": state.get("steps", 0) + 1}


# ── 分支四：步数用尽后的强制收尾 ──────────────────────────────────────
# 用不带工具的模型再问一次，这样它无法再调工具，只能把已有信息整理成回答。

# DeepSeek 的工具调用标记（两对全角竖线夹着 DSML）。
# 收尾分支已经禁用了工具，但模型偶尔还是按这个格式"假装调用"，
# 标记和参数会一路流进用户回答里（实测看到 <｜｜DSML｜｜ invoke name="web_search"> 这种东西）。
# 所以除了在提示词里禁止，输出侧还要真的过滤一遍。
_DSML_MARK = "｜｜DSML｜｜"
# 实际泄漏出来的形态是 "<" + 标记（"<｜｜DSML｜｜ invoke ..."）。
# 判断"尾巴要不要留一手"时按这个完整前缀来看，否则单独一个 "<" 会被提前吐出去，
# 标记被切成多个 chunk 时就漏过了（真实踩过）。
_LEAK_PROBE = "<" + _DSML_MARK


class _StreamSanitizer:
    """增量过滤"被当成文本吐出来的工具调用标记"。

    为什么必须增量做：内容是边生成边推给前端的（token 事件），
    等生成完再过滤，脏内容早就已经显示在用户屏幕上了。
    命中标记后直接丢弃其后所有内容 —— 标记后面跟的都是被误当正文的调用参数。
    """

    def __init__(self):
        self._tail = ""
        self._cut = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        if self._cut:
            return ""
        buf = self._tail + text
        idx = buf.find(_DSML_MARK)
        if idx >= 0:
            self._cut = True
            self._tail = ""
            return buf[:idx].rstrip().rstrip("<")
        # 尾巴可能是泄漏串的前缀（"<" / "<｜" / "<｜｜DS"），留到下一个 chunk 再判断
        keep = 0
        for n in range(min(len(_LEAK_PROBE) - 1, len(buf)), 0, -1):
            if buf.endswith(_LEAK_PROBE[:n]):
                keep = n
                break
        self._tail = buf[-keep:] if keep else ""
        return buf[:-keep] if keep else buf

    def flush(self) -> str:
        """一次模型调用结束时调用：把滞留的尾巴吐出来，并重置状态。"""
        tail, self._tail, self._cut = self._tail, "", False
        return "" if not tail else tail


FINALIZE_SYSTEM = finalize_system(_DSML_MARK)


def _summarize_tool_history(messages: list) -> str:
    """把"工具调用 + 结果"压成纯文本，供收尾分支使用。

    为什么不直接把原始消息历史丢给收尾模型（第一版就是这么写的，踩了两个坑）：
    1. 历史里最后一条是"想调工具但没调"的 assistant 消息（带着 tool_calls 却没人回应），
       DeepSeek 直接 400：An assistant message with 'tool_calls' must be followed by tool messages；
    2. 就算修掉悬空调用，满屏的 assistant(tool_calls)/tool 消息等于在提示模型"继续调工具"，
       它会继续按工具调用格式输出 —— 那些标记会被过滤器清掉，用户最后拿到一个空回答。
    所以这里只喂"人类可读的调用记录"，彻底断掉工具调用的模式。
    """
    lines: list[str] = []
    for m in messages:
        calls = getattr(m, "tool_calls", None)
        if calls:
            for c in calls:
                name = c.get("name") if isinstance(c, dict) else getattr(c, "name", "?")
                args = c.get("args") if isinstance(c, dict) else getattr(c, "args", {})
                lines.append(f"[调用] {name} {json.dumps(args, ensure_ascii=False, default=str)}")
        elif isinstance(m, ToolMessage):
            text = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)
            lines.append(f"[结果] {text[:1500]}")
        elif isinstance(m, AIMessage) and m.content:
            lines.append(f"[过程] {str(m.content)[:200]}")
    return "\n".join(lines)


async def _finalize_node(state: AgentState):
    history = _summarize_tool_history(state["messages"])
    prompt = (f"任务：{state['task']}\n\n"
              f"已获取的信息（工具调用记录，共 {len(history.splitlines())} 条）：\n"
              f"{history or '（本次没有成功获取到任何工具结果）'}")
    response = await _agent_model.ainvoke([
        SystemMessage(content=FINALIZE_SYSTEM), HumanMessage(content=prompt),
    ])

    # 模型还是想调工具时会整段输出调用标记（会被过滤器清空）→ 再问一次，这次只讲人话
    text = response.content if isinstance(response.content, str) else ""
    if _DSML_MARK in text or not text.strip():
        logger.warn("agent: finalize produced no usable text, retrying")
        response = await _agent_model.ainvoke([
            SystemMessage(content=FINALIZE_RETRY_SYSTEM), HumanMessage(content=prompt),
        ])
    return {"messages": [response]}


_builder = StateGraph(AgentState)
_builder.add_node("knowledge_retrieve", knowledge_retrieve)
_builder.add_node("knowledge_answer", knowledge_answer)
_builder.add_node("direct_answer", direct_answer)
_builder.add_node("agent", _agent_node)
_builder.add_node("tools", _tool_node)
_builder.add_node("finalize", _finalize_node)

_builder.add_conditional_edges(START, lambda s: s.get("route", "tool"), {
    "knowledge": "knowledge_retrieve",
    "chat": "direct_answer",
    "tool": "agent",
})
_builder.add_edge("knowledge_retrieve", "knowledge_answer")
_builder.add_edge("knowledge_answer", END)
_builder.add_edge("direct_answer", END)
_builder.add_conditional_edges("agent", _should_continue,
                               {"tools": "tools", "finalize": "finalize", "__end__": END})
_builder.add_edge("tools", "agent")
_builder.add_edge("finalize", END)
agent_graph = _builder.compile()


# 工具中文名：前端左侧「可用工具」和步骤卡片都靠它显示。
# 必须覆盖 all_tools 里的每一个 —— 漏掉的会直接显示英文原名（web_search 之类），
# 看起来就像"工具被改掉了"。
_TOOL_LABELS = {
    "web_search": "联网搜索",
    "read_doc": "检索知识库",
    "calculate": "数学计算",
    "get_date": "日期查询",
    "write_report": "生成报告",
    "send_notify": "发送通知",
}
_ROUTE_LABELS = {"knowledge": "知识库问答", "tool": "工具执行", "chat": "直接回答"}


def _get_tool_label(tool_name: str) -> str:
    return _TOOL_LABELS.get(tool_name, tool_name)


async def run_agent(task: str, on_event, user: User | None = None,
                    use_knowledge: bool | None = None, session_id: str = "agent-default"):
    """流式执行：推送 意图 → 工具调用 → token → done。

    身份通过 ContextVar 传递，工具在调用链深处也能做权限过滤。

    路由顺序（与"智能对话"共用同一个召回判定，避免两条链路行为不一致）：
      1. 先按 rag/intent.py 的"召回优先"判定要不要查库 —— 需要查库就一定走知识分支，
         不再把这个决定权交给模型路由器（模型不知道公司库里有什么，
         它会说"这属于通用技术问题、无需检索"，用户看到的就是"Agent 不去查知识库"）；
      2. 只有确定不查库时，才让模型在 tool / chat 之间二选一。
    """
    token = set_current_user(user or User())
    # 重复调用防护：同一个"工具+参数"在本次任务里只真正执行一次，见 tools.py 的 _repeat_guard
    log_token = set_tool_call_log({})
    # 工具检索到的引用来源（read_doc）：与 _tool_call_log 同一套路 —— 父任务放一个
    # 可变列表，工具往里 append，这里取走并发 sources 事件（见 tools.record_tool_sources）
    tool_sources: list = []
    sources_token = set_tool_sources(tool_sources)
    # 前端对 sources 事件是"替换"语义，所以这里累计去重后再下发：
    # 一次任务里 read_doc 可能被调用多次（换个关键词再查一轮），
    # 只发最后一次的话，引用来源面板会丢掉前面查到的文档
    emitted_sources: list = []
    emitted_keys: set = set()
    # 会话记忆：上次任务说过什么，这次要记得（摘要 + 最近 N 轮）
    _user = user or User()
    memory = await memory_block(session_id, _user.tenant_id)
    logger.info("agent: start", {"task": task[:60], "user": (user or User()).user_id,
                                 "sessionId": session_id, "memoryChars": len(memory)})

    # route 必须**先给默认值**：下面的 except 要用它写追踪（"这次挂在哪条路上"）。
    # 如果在 try 里第一次赋值，那么"预检索阶段抛出非预期异常"时就会在 except 里
    # 读一个还没赋值的名字 —— 例如 embedding 上游 5xx / 401 抛的 APIStatusError，
    # 既不是 StorageUnavailable 也不是 ValueError，try 里的 except 接不住，于是：
    #   抛 UnboundLocalError（真实错误被顶掉）→ error 事件发不出去 →
    #   追踪里也没有 error 步骤 → 用户只看到路由看门狗那句"任务执行中断了"。
    # 默认值与 classify_task 失败时的回退保持一致（tool）。
    route = "tool"
    streamed = False
    sanitizer = _StreamSanitizer()
    model_step_seq = 1    # 第几次模型调用（只用于追踪里的编号）
    _llm_t0 = time.time()
    _llm_chars = 0
    step_count = 0        # 工具调用次数（前端步骤卡片数）
    model_steps = 0       # 模型调用次数 —— MAX_STEPS 限制的是这个，别和上面混
    max_steps_hit = False
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0     # 输入里命中提示缓存的 token（单价只有 1/50，计费要单独算）
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
        #      检索没命中 → 交还给模型路由器（tool / chat），但把"没命中"的原因告诉用户
        #      检索有命中 → 再问一次路由器：任务是纯查文档就走知识分支（带引用回答）；
        #                   任务同时还要计算/日期/通知/报告就走工具分支 ——
        #                   否则「查年假政策 + 算剩余天数 + 通知 HR」这类预设任务
        #                   只会答出政策那一段，计算和通知都不会发生，
        #                   工具分支里的 read_doc 一样能把文档取回来。
        #    （多一次分类调用 ≈ 几百 token，换"任务不做一半"，值。）
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

        # 用户选了「强制检索」而库里没有：不能像"自动"那样把问题交还给通用知识，
        # 强制就是"只依据知识库作答"，查不到就说查不到（与智能对话的三态语义保持一致）。
        forced_miss = bool(need and source == "forced" and not pre_sources)
        # ⚠️ 预检索到底有没有命中，必须在下面"清空 pre_sources"**之前**记下来。
        #    之前在清空之后才算 kb_missed，于是"走了工具分支但库里其实有命中"的情况
        #    会被误判成"库里没有"，回答以「知识库中没有查到相关内容」开头再自我纠正 ——
        #    评测集用 agent-005 的 forbid_answer_phrases 抓到了这个潜在缺陷。
        pre_hit = bool(pre_sources)

        if forced_miss:
            route = "knowledge"
            route_reason = (f"用户选择了「强制检索知识库」："
                            f"{(pre_recall or {}).get('explain') or '没有可用内容'}"
                            "（强制模式只依据知识库作答，不用通用知识补充）")
            search_query = intent.query or task
            await on_event("sources", {"sources": [], "from": "agent-precheck",
                                        "recall": pre_recall})
        elif need and pre_sources:
            decision = await classify_task(task)
            capability = _tool_capability_need(task)
            if decision.route == "tool" or capability:
                route = "tool"
                why = (f"任务还需要工具（{decision.reason}）" if decision.route == "tool"
                       else f"任务需要「{capability}」，知识分支只会读文档回答、做不到")
                route_reason = (f"{intent_reason}：命中 {len(pre_sources)} 条，"
                                f"但{why} → 走工具分支")
                search_query = decision.search_query or task
                # 不把预检索结果当引用下发：这一分支由 read_doc 工具自己再检索一次，
                # 否则前端会出现"引用了文档、最终回答却没用上"的错位
                pre_sources, pre_recall = [], {}
            else:
                route = "knowledge"
                route_reason = f"{intent_reason}：命中 {len(pre_sources)} 条"
                search_query = intent.query or task
        else:
            decision = await classify_task(task)
            # 用户明确关掉了知识库时，模型也不许再选 knowledge（否则等于没关掉）
            route = "chat" if decision.route == "knowledge" else decision.route
            if need:
                if looks_like_knowledge_need(task):
                    why = (pre_recall or {}).get("explain") or "没有可用内容"
                    route_reason = f"已按召回优先检索知识库：{why} → {decision.reason}"
                else:
                    # 不是知识型消息（确认/续聊/起名这类）：检索 miss 是 recall_first 的
                    # 例行成本，别把"召回 N 条候选但重排…"的诊断写进用户看到的路由说明
                    route_reason = f"{intent_reason} → {decision.reason}"
            else:
                route_reason = f"{intent_reason} → {decision.reason}"
            # 同上：任务要求"计算/通知/报告"这类知识分支做不到的能力时，硬性走工具分支
            capability = _tool_capability_need(task)
            if capability and route != "tool":
                route = "tool"
                route_reason += f"；任务需要「{capability}」，强制走工具分支"
            search_query = decision.search_query or task
            # 知识分支不会执行，这里把检索诊断直接推给前端（否则用户不知道为什么没走知识库）
            # 但只在"消息确实带着知识诉求"时才推：确认/续聊类消息被塞一个
            # "知识库未命中"提示框，只会让用户困惑"为什么每句都检索"（2026-10-04 实测）
            if need and looks_like_knowledge_need(task):
                await on_event("sources", {"sources": [], "from": "agent-precheck",
                                            "recall": pre_recall})
            # 没命中就走别的分支，别让预检索结果影响后面的节点
            pre_sources, pre_recall = [], {}

        # 查了库但没查到（且不是"强制"分支，强制分支会直接回固定答复）：
        # 把开场白带进图，保证最终回答第一句就是"知识库中没有查到相关内容"。
        # ⚠️ 必须在下面 trace_step / intent 事件之前算出来 —— 之前把它放在后面，
        #    结果 tracer 里引用它直接 UnboundLocalError，整个 Agent 任务当场挂掉。
        kb_missed = bool(need) and not pre_hit and not forced_miss
        if kb_missed and not looks_like_knowledge_need(task):
            # 消息本来就不是在问知识库：recall_first 的例行检索 miss 了，也不该用
            # "知识库中没有查到相关内容"开场（同一次实测：用户让"发一下直属主管"，
            # 回答却被强制带上知识库未命中声明，读起来自相矛盾）
            kb_missed = False
        miss_text = miss_notice_text() if kb_missed else ""

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
        # 全链路追踪：这次任务被路由到了哪条路、为什么（"为什么没查知识库"最常见的答案就在这）
        trace_step("route", "任务路由：" + _ROUTE_LABELS.get(route, route), detail={
            "route": route, "reason": route_reason, "searchQuery": search_query,
            "needKnowledge": need, "decisionSource": source,
            "departmentHint": intent.department_hint, "ruleHit": intent.rule_hit,
            "prefetchHits": len(pre_sources), "prefetchRecall": pre_recall,
            "kbMissed": kb_missed,
        })

        # 【库里没有 → 回答必须先说明】route=chat（直接回答）时，由后端把这句话作为
        # **第一个 token** 发出去：模型再怎么发挥，用户看到的第一句也是它。
        # 为什么不放在 direct_answer 里拼字符串：前端渲染的是流式 token，拼在最终消息上
        # 不会进流。工具分支（route=tool）的最终回答交给提示词规则，不在这里抢答 ——
        # 那一路可能出现"过程说明被 answer_reset 挪走"，硬塞一句反而会被挪到过程区。
        final_content = ""
        if kb_missed and route == "chat":
            final_content = miss_text
            await on_event("token", {"token": miss_text})

        async for event in agent_graph.astream_events(
            # ⚠️ 这里出现的每个键都必须在 AgentState 里声明过，否则会被 LangGraph 静默丢掉
            #    （见 AgentState 里 department_hint / search_query 的说明；
            #     tests/test_agent_graph.py 有一条用例专门守这个契约）
            {"task": task, "route": route, "messages": [], "steps": 0,
             "sources": pre_sources, "recall": pre_recall,
             "miss_notice": miss_text,
             # 入口已经检索过一次（命中或"强制模式下没命中"），知识分支直接用结果，
             # 别再花一次 embedding + 重排的钱
             "prefetched": route == "knowledge" and bool(need),
             "memory": memory,
             "department_hint": intent.department_hint,
             "search_query": search_query},
            # 不放大这个上限，图会先于 MAX_STEPS 抛递归错误（见 RECURSION_LIMIT 的说明）
            config={"recursion_limit": RECURSION_LIMIT},
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
                # 工具入参：排查"参数为什么传错了"就靠这一条
                trace_step("tool_call", "调用工具：" + _get_tool_label(name), detail={
                    "step": step_count, "toolName": name, "args": data.get("input"),
                })
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
                # 工具出参：和入参成对出现，直接能看出"工具返回了什么、模型据此说了什么"
                trace_step("tool_result", "工具返回：" + _get_tool_label(name), detail={
                    "toolName": name, "result": result,
                })
                await on_event("tool_result", {
                    "toolName": name, "result": result,
                    "resultText": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                })
                # read_doc 命中（或没命中）的切片也要下发 sources：Agent 页的「引用来源」
                # 面板以前只有知识分支会亮，走工具分支时用户看不到答案引了哪几份文档
                # （2026-10-04 用户反馈）。FIFO 取：并发调工具时与调用顺序一致。
                if name == "read_doc" and tool_sources:
                    latest = tool_sources.pop(0)
                    for s in latest.get("sources") or []:
                        key = s.get("chunkId") or (s.get("docId"), s.get("pageNumber"),
                                                  (s.get("content") or "")[:40])
                        if key not in emitted_keys:
                            emitted_keys.add(key)
                            emitted_sources.append(s)
                    await on_event("sources", {
                        "sources": emitted_sources, "from": "read_doc",
                        "recall": latest.get("recall") or {},
                    })

            # 每次模型调用开始时换一个干净的过滤器（一次调用内命中标记后就不再输出）
            if event_type == "on_chat_model_start":
                sanitizer = _StreamSanitizer()
                _llm_t0 = time.time()
                _llm_chars = 0

            if event_type == "on_chat_model_stream":
                chunk = data.get("chunk")
                content = getattr(chunk, "content", None) if chunk else None
                tool_chunks = getattr(chunk, "tool_call_chunks", None) if chunk else None
                if content and not tool_chunks:
                    streamed = True
                    piece = sanitizer.feed(content)
                    if piece:
                        final_content += piece
                        _llm_chars += len(piece)
                        await on_event("token", {"token": piece})
                # 用量统计：把每一步模型调用的 token 累加起来，看板才有真实成本
                usage = getattr(chunk, "usage_metadata", None) if chunk else None
                if usage:
                    input_tokens += usage.get("input_tokens", 0) or 0
                    output_tokens += usage.get("output_tokens", 0) or 0
                    cached_tokens += cache_read_of(usage)

            # 一次模型调用结束：把过滤器里滞留的尾巴吐出来，并重置状态
            if event_type == "on_chat_model_end":
                tail = sanitizer.flush()
                if tail:
                    final_content += tail
                    _llm_chars += len(tail)
                    await on_event("token", {"token": tail})
                # 全链路追踪：Agent 的第 N 次模型调用（含它产出了多少字）
                trace_step("llm", f"模型调用 #{model_step_seq}", status="ok",
                           duration_ms=round((time.time() - _llm_t0) * 1000),
                           detail={"chars": _llm_chars, "streamedTail": bool(tail)})
                model_step_seq += 1

            # 模型这一步产出了 tool_calls：说明它刚刚流式吐出来的那些字只是"过程说明"
            # （"我先查一下…"），不是最终回答。发一个 answer_reset 让前端把它挪到过程说明区。
            # 为什么不能只靠 tool_call 事件清理：步数用尽时会强制走 finalize，
            # 这一步的 tool_calls 不会被执行，也就不会有 tool_call 事件。
            if event_type == "on_chain_end" and name == "agent":
                output = data.get("output") or {}
                msgs = output.get("messages") if isinstance(output, dict) else None
                if msgs and getattr(msgs[-1], "tool_calls", None):
                    await on_event("answer_reset", {"reason": "tool_call"})

            # 步数用尽 → 走了强制收尾分支，告诉前端这次回答是"基于已有信息的总结"
            if event_type == "on_chain_start" and name == "finalize":
                max_steps_hit = True

            # 兜底：某些分支（未命中知识库、非流式节点）没有 token 事件，用最终消息补一次
            if event_type == "on_chain_end" and name == "LangGraph":
                output = data.get("output")
                if isinstance(output, dict):
                    # 图里的 steps 才是模型步数（_agent_node 每跑一次 +1），MAX_STEPS 比的就是它
                    model_steps = output.get("steps") or model_steps
                    msgs = output.get("messages") or []
                    if msgs:
                        last = msgs[-1]
                        text = getattr(last, "content", "") or ""
                        if text and not streamed:
                            final_content = text
                            await on_event("token", {"token": text})

        if not final_content.strip():
            # 兜底：所有分支都没产出可用文本（例如收尾模型又把回答写成工具调用、
            # 被过滤器清空了）。宁可给一句明确的说明，也不要让用户对着空白页面。
            final_content = ("（本次没有生成可用的回答文字）可能是模型把回答写成了工具调用格式而被过滤。"
                             "已经执行过的步骤和结果可以在上面的卡片里看到；可以换个说法再试一次。")
            await on_event("token", {"token": final_content})

        # 记住这一轮：下次任务就能引用这次说过的话。
        # 超过阈值会自动异步压缩更早的对话（append_turn 内部处理，不阻塞本次回答）
        await append_turn(session_id, task, final_content, _user.tenant_id, _user.user_id)

        trace_step("response", "任务完成", detail={
            "route": route, "toolCalls": step_count, "modelSteps": model_steps,
            "maxStepsReached": max_steps_hit, "inputTokens": input_tokens,
            "outputTokens": output_tokens, "cachedInputTokens": cached_tokens,
            "costCNY": pricing.cost_cny(primary_model_name(), input_tokens, output_tokens,
                                        cached_tokens),
            "answer": final_content,
        })
        await on_event("done", {"steps": step_count, "modelSteps": model_steps, "route": route,
                                "contentLength": len(final_content),
                                "maxStepsReached": max_steps_hit,
                                "maxSteps": MAX_STEPS,
                                "inputTokens": input_tokens, "outputTokens": output_tokens,
                                "cachedInputTokens": cached_tokens,
                                "model": primary_model_name()})
        logger.info("agent: done", {"toolCalls": step_count, "modelSteps": model_steps,
                                    "route": route, "maxStepsReached": max_steps_hit})
    except Exception as err:
        logger.error("agent: error", {"error": str(err)})
        trace_step("error", "Agent 执行出错", status="error",
                   detail={"error": f"{type(err).__name__}: {err}", "route": route,
                           "toolCalls": step_count})
        message = str(err)
        # 把基础设施类的英文报错翻译成用户能看懂、能行动的话
        if "Recursion limit" in message:
            message = "这次任务步骤太多被中断了。可以缩小任务范围，或者把问题拆成几步再问。"
        elif "tool_calls" in message and "followed by tool messages" in message:
            message = "任务执行到一半消息结构出错被中断了，请重试一次。"
        await on_event("error", {"message": message or "Agent 执行出错"})
    finally:
        reset_tool_call_log(log_token)
        reset_tool_sources(sources_token)
        reset_current_user(token)


def get_tool_list() -> list[dict]:
    return [{"name": t.name, "label": _get_tool_label(t.name), "description": t.description} for t in all_tools]
