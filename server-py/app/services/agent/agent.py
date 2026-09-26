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
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from app.services.agent.tools import (
    all_tools, guarded_tools, reset_tool_call_log, set_tool_call_log,
)
from app.services.chat.memory import append_turn, memory_block
from app.services.db import StorageUnavailable
from app.services.identity import User, get_current_user, reset_current_user, set_current_user
from app.services.model import create_chat_model
from app.services.rag.intent import classify_intent
from app.services.rag.query import (
    MISS_FIRST_RULE, SearchFilters, build_context, miss_notice as miss_notice_text,
    no_knowledge_reply, retrieve_with_meta,
)
from app.utils.logger import logger


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

AGENT_SYSTEM = f"""你是 WorkMind AI 任务助手，专门处理办公场景的复杂任务。

可用工具：
{_tool_catalog()}

工作原则：
1. 先理解任务的完整需求，想好需要哪些步骤
2. 按最少工具调用完成任务，避免重复查询
3. 需要外部信息（最新版本、资讯、行情等）时不凭记忆编造，用 web_search 查
4. 收集到足够信息后，用 write_report 产出结构化报告
5. 获取到足够信息后立刻生成最终回答，不要继续无谓的工具调用
6. 工具返回内容不足时如实说明，不要编造
7. 每次只调用一个工具，等结果回来再决定下一步
8. **不要在同一个问题上反复搜索**：同一个关键词不要搜第二遍，换个说法搜同一件事也算重复；
   搜了 3~4 次还没拿到关键信息，就基于已有信息给结论并说明缺什么，不要无限搜下去
9. 最多 {MAX_STEPS} 步；接近上限时先输出结论，别把步数花在收尾之外的调用上"""

CHAT_SYSTEM = "你是 WorkMind AI 助手，回答简洁专业。不知道的信息如实说明，不要编造公司内部规定。"

KNOWLEDGE_SYSTEM = """你是 WorkMind AI 知识库助手。

规则：
1. 只根据下方提供的参考文档回答问题，不使用文档之外的知识
2. 如果文档中没有相关内容，明确说"知识库中未找到相关内容"
3. 回答准确简洁；引用资料时直接在句末写方括号编号，例如 [1] 或 [1][3]
   （编号就是资料列表里的序号，不要写【来源：文档标题 · 第N页】这种长句）"""


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


async def classify_task(task: str) -> _RouteDecision:
    try:
        return await _router_model.with_structured_output(_RouteDecision, method="function_calling").ainvoke([
            {"role": "system", "content":
                "你是任务路由器，判断任务应该走哪条执行路径。"
                "只要任务需要工具（联网搜索舆情/版本/资讯、计算、日期、生成报告、发通知）才能完成，"
                "就选 tool —— 即使它同时也包含写作成分（例如'查最新版本并生成报告'仍是 tool）；"
                "只有完全不需要工具、也不需要公司文档时才选 chat。"},
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


FINALIZE_SYSTEM = f"""本轮的工具调用次数已用尽，工具现在不可用。

请直接给出最终回答，不要再要求调用任何工具：
1. 用中文回答，先把**已经拿到的信息**整理清楚（带上来源或时间，便于用户核对）
2. 明确区分「已查到的」和「没查到的」，没查到的如实说明缺什么
3. 不要再重复搜索、不要再编造；不确定的地方就说 不确定
4. 如果任务要求报告或总结，直接输出正文
5. 绝对不要输出任何工具调用格式或标记（例如 {_DSML_MARK} 这种），那会被当成乱码展示给用户"""


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


FINALIZE_RETRY_SYSTEM = """直接输出最终回答的正文文字。

要求：
- 只输出中文正文，不要输出任何调用格式、标记、参数或代码块
- 信息不足的地方如实说明，不要编造"""


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
    # 会话记忆：上次任务说过什么，这次要记得（摘要 + 最近 N 轮）
    _user = user or User()
    memory = await memory_block(session_id, _user.tenant_id)
    logger.info("agent: start", {"task": task[:60], "user": (user or User()).user_id,
                                 "sessionId": session_id, "memoryChars": len(memory)})

    streamed = False
    sanitizer = _StreamSanitizer()
    step_count = 0        # 工具调用次数（前端步骤卡片数）
    model_steps = 0       # 模型调用次数 —— MAX_STEPS 限制的是这个，别和上面混
    max_steps_hit = False
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
            if decision.route == "tool":
                route = "tool"
                route_reason = (f"{intent_reason}：命中 {len(pre_sources)} 条，"
                                f"但任务还需要工具（{decision.reason}）→ 走工具分支")
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

        # 查了库但没查到（且不是"强制"分支，强制分支会直接回固定答复）：
        # 把开场白带进图，保证最终回答第一句就是"知识库中没有查到相关内容"
        kb_missed = bool(need) and not pre_sources and not forced_miss
        miss_text = miss_notice_text() if kb_missed else ""

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

            # 每次模型调用开始时换一个干净的过滤器（一次调用内命中标记后就不再输出）
            if event_type == "on_chat_model_start":
                sanitizer = _StreamSanitizer()

            if event_type == "on_chat_model_stream":
                chunk = data.get("chunk")
                content = getattr(chunk, "content", None) if chunk else None
                tool_chunks = getattr(chunk, "tool_call_chunks", None) if chunk else None
                if content and not tool_chunks:
                    streamed = True
                    piece = sanitizer.feed(content)
                    if piece:
                        final_content += piece
                        await on_event("token", {"token": piece})
                # 用量统计：把每一步模型调用的 token 累加起来，看板才有真实成本
                usage = getattr(chunk, "usage_metadata", None) if chunk else None
                if usage:
                    input_tokens += usage.get("input_tokens", 0) or 0
                    output_tokens += usage.get("output_tokens", 0) or 0

            # 一次模型调用结束：把过滤器里滞留的尾巴吐出来，并重置状态
            if event_type == "on_chat_model_end":
                tail = sanitizer.flush()
                if tail:
                    final_content += tail
                    await on_event("token", {"token": tail})

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

        await on_event("done", {"steps": step_count, "modelSteps": model_steps, "route": route,
                                "contentLength": len(final_content),
                                "maxStepsReached": max_steps_hit,
                                "maxSteps": MAX_STEPS,
                                "inputTokens": input_tokens, "outputTokens": output_tokens})
        logger.info("agent: done", {"toolCalls": step_count, "modelSteps": model_steps,
                                    "route": route, "maxStepsReached": max_steps_hit})
    except Exception as err:
        logger.error("agent: error", {"error": str(err)})
        message = str(err)
        # 把基础设施类的英文报错翻译成用户能看懂、能行动的话
        if "Recursion limit" in message:
            message = "这次任务步骤太多被中断了。可以缩小任务范围，或者把问题拆成几步再问。"
        elif "tool_calls" in message and "followed by tool messages" in message:
            message = "任务执行到一半消息结构出错被中断了，请重试一次。"
        await on_event("error", {"message": message or "Agent 执行出错"})
    finally:
        reset_tool_call_log(log_token)
        reset_current_user(token)


def get_tool_list() -> list[dict]:
    return [{"name": t.name, "label": _get_tool_label(t.name), "description": t.description} for t in all_tools]
