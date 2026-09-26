# server-py/app/routes/chat.py
# 对话路由：意图判断 → （按权限）检索知识库 → 带引用流式回答 + 缓存 + 会话管理 + 画像
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.middleware import rate_limiter, security_check
from app.services.cache import cache
from app.services.chat.memory import (
    clear_all, clear_history, extract_and_update_profile, get_history, get_profile,
    list_sessions, profile_to_context, trim_history,
)
from app.services.db import StorageUnavailable
from app.services.identity import User, current_user
from app.services.model import chat_model
from app.services.rag.intent import classify_intent
from app.services.rag.query import (
    ANSWER_POLICY, SearchFilters, build_context, no_knowledge_reply, retrieve_with_meta,
)
from app.utils.logger import logger
from app.utils.sse import sse_stream

router = APIRouter()

# 唯一的助手人设。
#
# 为什么把原来的四个角色（通用/技术/HR/法务）合并掉：
# - 它们在代码里只做一件事 —— 换一句 system prompt，**不影响检索到哪些文档**，
#   对用户能感知的结果没有差异；
# - 更要命的是它们和"知识优先"冲突：技术顾问那句"回答要有代码示例、说明清楚原理"、
#   HR 那句"回答要有温度"，都会诱导模型在资料之外发挥，而现在的规则是
#   "资料不足就明确说没找到，不许补充、推测、举一反三"。
# - 真正能改变结果的是"查哪些文档"，所以那一行 UI 换成了**检索范围预设**
#   （部门 / 文档类型 / 是否含历史版本），见前端的 ScopeSelector.vue。
#
# role 字段仍然接收（老客户端不报错），但不再改变行为。
ASSISTANT_SYSTEM = (
    "你是 WorkMind AI，一个智能办公助手。"
    "回答简洁、专业、有条理，必要时代码、步骤、表格该给就给。"
    "当上方提供了企业知识库资料时，资料是本轮回答的唯一依据。"
)


class ChatStreamRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    sessionId: str | None = "default"
    systemPrompt: str | None = Field(default=None, max_length=2000)
    role: str | None = "default"
    userId: str | None = "anonymous"
    # 知识库检索：None=自动判断（默认），True=强制检索，False=关闭
    useKnowledge: bool | None = None
    # 检索范围收窄（部门/版本/类型），为空时按自动判断结果与用户权限
    knowledgeDepartment: str | None = None
    knowledgeVersion: str | None = None
    knowledgeDocType: str | None = None
    includeSuperseded: bool = False


def _cache_scope(user: User, sources: list[dict]) -> str:
    """缓存隔离域：不同租户/部门/密级，以及命中的不同切片集合，不能互相复用答案。"""
    deps = ",".join(sorted(user.departments))
    chunks = ",".join(sorted(s["chunkId"] for s in sources))
    return f"{user.tenant_id}|{deps}|{user.clearance}|{chunks}"


@router.post("/stream", dependencies=[Depends(rate_limiter)])
async def chat_stream(body: ChatStreamRequest, user: User = Depends(current_user)):
    security_check(body.message)

    session_id = body.sessionId or "default"
    # role 已废弃：四个角色预设已合并为下面唯一的 ASSISTANT_SYSTEM
    if body.role and body.role != "default":
        logger.info("chat: role 参数已废弃（角色预设已合并）", {"role": body.role})
    user_id = body.userId or user.user_id
    message = body.message

    async def generator():
        try:
            base_system = ASSISTANT_SYSTEM
            profile = get_profile(user_id)
            profile_ctx = profile_to_context(profile)

            # ── 1) 意图判断：这次要不要查知识库 ──────────────────────
            # 默认"召回优先"：只有明显与知识库无关的消息（打招呼/闲聊/纯算式）才不查，
            # 因为"该不该查库"不该由常识判断 —— 模型根本不知道公司库里有什么。
            # 用户可以在输入框旁显式选择 强制检索 / 关闭检索 来覆盖。
            sources: list[dict] = []
            decision = await classify_intent(message)
            if body.useKnowledge is True:
                need, source, reason = True, "forced", "用户选择了「强制检索知识库」"
            elif body.useKnowledge is False:
                need, source, reason = False, "disabled", "用户选择了「关闭知识库检索」"
            else:
                need = decision.need_knowledge
                source, reason = decision.decision_source, decision.reason

            yield "intent", {
                "needKnowledge": need,
                "decisionSource": source,
                "reason": reason,
                "ruleHit": decision.rule_hit,
                "departmentHint": decision.department_hint,
                "searchQuery": decision.query if need else None,
                "forced": body.useKnowledge is True,
                "disabled": body.useKnowledge is False,
            }

            if need:
                # ⚠️ 关键区分（别再合并）：
                #   body.knowledgeDepartment  = 用户在前端明确选的部门 → 硬过滤，尊重用户
                #   decision.department_hint  = 意图模型猜的部门     → 只做提示，不裁剪候选集
                # 之前两者共用一个硬过滤，模型把"rag召回失败怎么办"猜成 tech、
                # 而文档是 general，结果整份文档都召不回。
                filters = SearchFilters(
                    department=body.knowledgeDepartment or None,
                    department_hint=decision.department_hint,
                    version=body.knowledgeVersion or None,
                    doc_type=body.knowledgeDocType or None,
                    include_superseded=body.includeSuperseded,
                )
                recall = None
                try:
                    sources, recall = await retrieve_with_meta(
                        decision.query or message, user, filters=filters)
                except StorageUnavailable as err:
                    # 数据库不可用：明确告诉用户是"知识库连不上"，不能说成"没命中"
                    logger.error("chat: knowledge storage unavailable", {"error": str(err)})
                    recall = {"hit": 0, "reason": "storage_unavailable", "explain": str(err),
                              "candidates": 0, "totalChunks": 0, "bestScore": None,
                              "threshold": None, "k": 0, "departmentHint": None,
                              "departmentHintMatched": False,
                              "appliedFilters": filters.describe()}
                except ValueError as err:
                    # 知识库不可用（例如未配置 embedding）不能打断对话，降级为普通问答
                    logger.warn("chat: knowledge unavailable, degrade", {"error": str(err)})
                    recall = {"hit": 0, "reason": "embedding_unavailable", "explain": str(err),
                              "candidates": 0, "totalChunks": 0, "bestScore": None,
                              "threshold": None, "k": 0, "departmentHint": None,
                              "departmentHintMatched": False,
                              "appliedFilters": filters.describe()}

                yield "sources", {
                    "sources": sources,
                    "recall": recall,
                    "filters": filters.describe(),
                    "identity": {"userId": user.user_id, "departments": user.departments,
                                 "clearance": user.clearance},
                }

                # ── 知识优先：检索不到就不作答 ──────────────────────
                # 不调用模型 = 不可能拿模型自身的知识去顶，也就不会和公司资料口径打架。
                # 答复里已经把"为什么没找到 + 下一步怎么办"讲清楚（见 no_knowledge_reply）。
                if not sources and ANSWER_POLICY == "grounded":
                    text = no_knowledge_reply(recall)
                    yield "start", {"sessionId": session_id}
                    for i in range(0, len(text), 3):
                        yield "token", {"token": text[i:i + 3]}
                        await asyncio.sleep(0.006)
                    hist = get_history(session_id)
                    hist.append(HumanMessage(content=message))
                    hist.append(AIMessage(content=text))
                    if len(hist) > 20:
                        del hist[:2]
                    yield "done", {"fromCache": False, "inputTokens": 0, "outputTokens": 0,
                                   "sourceCount": 0, "grounded": True,
                                   "reason": (recall or {}).get("reason")}
                    logger.info("chat: 知识库未命中，按知识优先策略直接答复", {
                        "reason": (recall or {}).get("reason"), "user": user.user_id,
                        "best": (recall or {}).get("bestScore"),
                    })
                    return

            # ── 3) 组装 system：角色 + 画像 + （检索到的）参考资料 ────
            context_block = ""
            if sources:
                context_block = (
                    "\n\n【企业知识库资料】以下内容已按当前用户权限过滤，是本轮回答的**唯一依据**：\n"
                    "1. 只依据这些资料回答，并在引用处标注【来源：文档标题 · 第N页】\n"
                    "2. 资料不足以回答的部分，明确说\"知识库中未找到相关内容\"，"
                    "**不要用你自己的知识补充、推测或举一反三**\n"
                    "3. 资料之间如果有冲突，指出冲突并标注各自来源\n"
                    + build_context(sources)
                )
            system_prompt = (body.systemPrompt or base_system) + profile_ctx + context_block

            # ── 4) 缓存（必须带权限隔离域，否则会把 A 权限的答案发给 B）──
            scope = _cache_scope(user, sources)
            cached = cache.get(system_prompt, message, scope)
            if cached:
                logger.info("cache hit", {"sessionId": session_id, "scope": scope[:48]})
                yield "cache_hit", {}
                text = cached["content"]
                for i in range(0, len(text), 3):
                    yield "token", {"token": text[i:i + 3]}
                    await asyncio.sleep(0.006)
                yield "done", {"fromCache": True, "sourceCount": len(sources)}
                return

            history = get_history(session_id)
            trimmed = trim_history(history, 2000)
            messages = [SystemMessage(content=system_prompt), *trimmed, HumanMessage(content=message)]

            yield "start", {"sessionId": session_id}

            full_reply = ""
            input_tokens = 0
            output_tokens = 0

            async for chunk in chat_model.astream(messages):
                if chunk.content:
                    full_reply += chunk.content
                    yield "token", {"token": chunk.content}
                if getattr(chunk, "usage_metadata", None):
                    input_tokens = chunk.usage_metadata.get("input_tokens", 0)
                    output_tokens = chunk.usage_metadata.get("output_tokens", 0)

            history.append(HumanMessage(content=message))
            history.append(AIMessage(content=full_reply))
            if len(history) > 20:
                del history[:2]

            cache.set(system_prompt, message, full_reply, input_tokens + output_tokens, scope)

            asyncio.create_task(_safe_extract_profile(user_id, message, full_reply))

            # 用量统计：落库后才能在看板上看到真实数字（此前 record_api_call 从未被调用）
            from app.routes.monitor import record_api_call
            record_api_call(feature="chat", input_tokens=input_tokens, output_tokens=output_tokens,
                            latency_ms=0, from_cache=False)

            yield "done", {
                "fromCache": False, "inputTokens": input_tokens, "outputTokens": output_tokens,
                "sourceCount": len(sources),
            }
            logger.info("chat done", {
                "sessionId": session_id, "inputTokens": input_tokens,
                "outputTokens": output_tokens, "replyLen": len(full_reply),
                "sources": len(sources), "user": user.user_id,
            })
        except Exception as err:
            logger.error("chat error", {"error": str(err)})
            raise

    return sse_stream(generator)


async def _safe_extract_profile(user_id: str, message: str, reply: str):
    try:
        await extract_and_update_profile(user_id, message, reply)
    except Exception:
        pass


@router.get("/sessions")
async def sessions():
    return {"sessions": list_sessions()}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    clear_history(session_id)
    return {"success": True}


@router.delete("/sessions")
async def clear_sessions():
    """清空全部会话（测试时清理上下文用）。"""
    from app.services.chat.memory import list_sessions
    n = len(list_sessions())
    clear_all()
    logger.info("chat: all sessions cleared", {"count": n})
    return {"success": True, "cleared": n}


@router.get("/profile/{user_id}")
async def profile(user_id: str):
    return get_profile(user_id)


@router.get("/roles")
async def roles():
    """已废弃：四个角色预设（通用/技术/HR/法务）已合并为一个助手。

    它们原来只换一句 system prompt、不影响检索到哪些文档，却和"知识优先"
    （资料不足就说没找到、不许补充）相冲突。保留这个路由只为老客户端不报错；
    "查哪些文档"的差异改由 ChatStreamRequest 的 knowledgeDepartment /
    knowledgeDocType / knowledgeVersion / includeSuperseded 表达。
    """
    return {
        "roles": [
            {"id": "default", "label": "智能助手", "icon": "🤖",
             "desc": "知识库优先：先查企业资料，查不到会明确说明"},
        ],
        "deprecated": True,
        "scopeFields": ["knowledgeDepartment", "knowledgeDocType",
                        "knowledgeVersion", "includeSuperseded"],
    }
