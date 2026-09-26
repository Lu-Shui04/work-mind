# server-py/app/routes/chat.py
# 对话路由：意图判断 → （按权限）检索知识库 → 带引用流式回答 + 缓存 + 会话管理 + 画像
import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.middleware import rate_limiter, security_check
from app.services.cache import cache
from app.services.chat.memory import (
    append_turn, clear_all, clear_history, extract_and_update_profile, get_profile,
    list_sessions, memory_messages, profile_to_context,
)
from app.services.db import StorageUnavailable
from app.services.identity import User, current_user
from app.services import pricing
from app.services.model import chat_model
from app.services.rag.intent import classify_intent
from app.services.rag.query import (
    MISS_FIRST_RULE, SearchFilters, build_context, miss_notice as miss_notice_text,
    no_knowledge_reply, resolve_knowledge_mode, retrieve_with_meta,
)
from app.services.trace import start_trace, trace_step
from app.utils.logger import logger
from app.utils.sse import sse_stream
from app.utils.tokens import cache_read_of

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


def chat_model_name() -> str:
    """当前对话模型的真实名字（计费要按它查单价）。

    注意：这里取的是**配置的**模型名（本项目是 deepseek-chat）。
    DeepSeek 侧返回的 response_metadata.model_name 是 deepseek-flash ——
    老名字实际由 V4.1-Flash 提供服务，定价表里两者都映射到 Flash 档，
    所以用哪个名字都能算对。
    """
    return getattr(chat_model, "model_name", "") or "deepseek-chat"


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
        # 全链路追踪：从"收到提问"开始记，检索/缓存/模型/异常都挂在同一条 trace 上，
        # 跑完落库；前端「全链路追踪」页能按这条聊天记录点进来看完整过程。
        trace = start_trace("chat", question=message, tenant_id=user.tenant_id,
                            user_id=user_id, user_name=user.name,
                            meta={"sessionId": session_id, "useKnowledge": body.useKnowledge})
        trace.step("request", "收到提问", detail={
            "message": message,
            "sessionId": session_id,
            "knowledgeMode": ("强制检索" if body.useKnowledge is True else
                              "关闭检索" if body.useKnowledge is False else "自动"),
            "scope": {"department": body.knowledgeDepartment, "docType": body.knowledgeDocType,
                      "version": body.knowledgeVersion, "includeSuperseded": body.includeSuperseded},
            "identity": {"userId": user.user_id, "name": user.name,
                         "departments": user.departments, "clearance": user.clearance},
            "systemPromptOverride": body.systemPrompt,
        })
        # 看板上的"平均响应/P99"要包含对话（最常用的入口）。
        # 以前这里写死 latency_ms=0，于是对话永远不出现在延迟统计里。
        started = time.time()
        try:
            base_system = ASSISTANT_SYSTEM
            profile = await get_profile(user_id, user.tenant_id)
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

                # ── 没命中时怎么办：三种模式的语义必须分清（以前是混的）──────
                #   强制（useKnowledge=true）：只依据知识库，查不到就说查不到，
                #                              不许拿模型自己的知识兜底（怕和公司口径打架）
                #   自动（undefined）：先明确说明"库里没有"，**然后照常用通用知识回答**
                #                      （用户要的就是这个：别拿一句"没找到"把人堵死）
                #   关闭（false）：压根不检索，直接回答，不会出现任何"未找到"话术
                # 部署方仍可用 RAG_ANSWER_POLICY=grounded 让"自动"也走严格模式。
                if resolve_knowledge_mode(body.useKnowledge, need, sources) == "grounded_miss":
                    text = no_knowledge_reply(recall)
                    yield "start", {"sessionId": session_id, "runId": trace.run_id}
                    for i in range(0, len(text), 3):
                        yield "token", {"token": text[i:i + 3]}
                        await asyncio.sleep(0.006)
                    await append_turn(session_id, message, text, user.tenant_id, user_id)
                    # 这条路没调模型（只回固定答复），但"为什么没查到"才是要看的东西 ——
                    # 检索那几步已经在 trace 里了，这里补一条结论
                    trace_step("response", "知识库未命中（强制模式，不调用模型）", detail={
                        "reason": (recall or {}).get("reason"),
                        "reply": text,
                    })
                    await trace.finish(summary={"cached": False, "groundedMiss": True,
                                                "reason": (recall or {}).get("reason")})
                    yield "done", {"fromCache": False, "inputTokens": 0, "outputTokens": 0,
                                   "sourceCount": 0, "grounded": True,
                                   "reason": (recall or {}).get("reason"),
                                   "runId": trace.run_id}
                    logger.info("chat: 知识库未命中，按知识优先策略直接答复", {
                        "reason": (recall or {}).get("reason"), "user": user.user_id,
                        "best": (recall or {}).get("bestScore"),
                    })
                    return

            # ── 3) 组装 system：角色 + 画像 + （检索到的）参考资料 ────
            # 自动模式下"库里没有"时：先给用户一句说明，再让模型用通用知识回答。
            # 说明放在回答开头（而不是让模型自己措辞），保证每次都能说清楚、
            # 也保证它不会被误读成"公司资料"。
            miss_notice = ""
            if resolve_knowledge_mode(body.useKnowledge, need, sources) == "fallback_miss":
                # 【库里没有 → 必须先说明】两道保险，缺一不可：
                #   1) 后端先把这句话发出去（模型再怎么发挥，用户看到的第一句也是它）
                #   2) 提示词要求模型自己第一句也说一遍（这样读起来才像"回答的一部分"，
                #      而不是一句系统旁注）
                # 检索诊断（候选数/最高分/阈值）不再塞进回答正文：那个位置已经有黄色的
                # "未命中原因"提示框在展示了，正文里出现"阈值没标定"这类工程细节只会干扰阅读。
                miss_notice = miss_notice_text()
                base_system += "\n\n" + MISS_FIRST_RULE

            context_block = ""
            if sources:
                context_block = (
                    "\n\n【企业知识库资料】以下内容已按当前用户权限过滤，是本轮回答的**唯一依据**：\n"
                    "1. 只依据这些资料回答；引用时在句末直接写方括号编号，例如 [1] 或 [1][3]，"
                    "编号就是每段资料开头标注的序号。**不要写【来源：文档标题 · 第N页】这种长句**"
                    "（前端会把编号渲染成可点击的小角标，长句会打断阅读）\n"
                    "2. 资料不足以回答的部分，明确说\"知识库中未找到相关内容\"，"
                    "**不要用你自己的知识补充、推测或举一反三**\n"
                    "3. 资料之间如果有冲突，指出冲突并分别标注编号\n"
                    + build_context(sources)
                )
            system_prompt = (body.systemPrompt or base_system) + profile_ctx + context_block

            # ── 4) 缓存（必须带权限隔离域，否则会把 A 权限的答案发给 B）──
            scope = _cache_scope(user, sources)
            cached = await cache.get(system_prompt, message, scope)
            # 组装好的 system prompt 是排查"回答为什么跑偏"的第一手材料，必须留档
            trace_step("prompt", "组装系统提示词", detail={
                "systemPrompt": system_prompt,
                "chars": len(system_prompt),
                "sourcesInContext": len(sources),
                "profileInjected": bool(profile_ctx),
            })
            if not cached:
                trace.step("cache", "未命中缓存（需要调用模型）", detail={"scope": scope})
            if cached:
                logger.info("cache hit", {"sessionId": session_id, "scope": scope[:48]})
                yield "cache_hit", {}
                text = cached["content"]
                for i in range(0, len(text), 3):
                    yield "token", {"token": text[i:i + 3]}
                    await asyncio.sleep(0.006)
                # 缓存命中的回答同样要进记忆，否则下一轮会"忘记"这次说过什么
                await append_turn(session_id, message, text, user.tenant_id, user_id)
                # 缓存命中也必须记账：这是看板"缓存命中率"的唯一数据来源。
                # 以前这条分支直接 return，没调 record_api_call ——
                # 于是不管命中多少次的对话，看板上永远是 0% 命中率、¥0 省下的钱。
                from app.routes.monitor import record_api_call
                record_api_call(
                    feature="chat", from_cache=True,
                    saved_tokens=int(cached.get("tokens") or 0),
                    latency_ms=round((time.time() - started) * 1000),
                    tenant_id=user.tenant_id, user_id=user_id,
                )
                trace.step("cache", "命中缓存（未调用模型）", detail={
                    "scope": scope, "savedTokens": int(cached.get("tokens") or 0),
                    "replyChars": len(text),
                })
                trace.step("response", "返回缓存回答", detail={"reply": text})
                await trace.finish(summary={"cached": True, "sources": len(sources),
                                            "replyChars": len(text)})
                yield "done", {"fromCache": True, "sourceCount": len(sources),
                               "savedTokens": int(cached.get("tokens") or 0),
                               "runId": trace.run_id}
                return

            # 记忆 = 【此前对话摘要】+ 最近 N 轮原文（见 services/chat/memory.py）
            messages = [SystemMessage(content=system_prompt),
                        *await memory_messages(session_id, user.tenant_id),
                        HumanMessage(content=message)]

            # runId 一并下发：前端把它挂在消息上，"查看全链路"按钮直接就能用
            yield "start", {"sessionId": session_id, "runId": trace.run_id}

            full_reply = ""
            input_tokens = 0
            output_tokens = 0
            cached_tokens = 0      # 输入里命中提示缓存的 token（单价只有 1/50）

            # 先把"知识库未命中"的说明推出去，再流式输出模型回答
            if miss_notice:
                full_reply += miss_notice
                for i in range(0, len(miss_notice), 3):
                    yield "token", {"token": miss_notice[i:i + 3]}
                    await asyncio.sleep(0.004)

            _llm_t0 = time.time()
            async for chunk in chat_model.astream(messages):
                if chunk.content:
                    full_reply += chunk.content
                    yield "token", {"token": chunk.content}
                if getattr(chunk, "usage_metadata", None):
                    input_tokens = chunk.usage_metadata.get("input_tokens", 0)
                    output_tokens = chunk.usage_metadata.get("output_tokens", 0)
                    cached_tokens = cache_read_of(chunk.usage_metadata)
            trace_step("llm", "对话模型生成", duration_ms=round((time.time() - _llm_t0) * 1000),
                       detail={"model": chat_model_name(),
                               "inputTokens": input_tokens,
                               "outputTokens": output_tokens,
                               "cachedInputTokens": cached_tokens,
                               "costCNY": pricing.cost_cny(chat_model_name(), input_tokens,
                                                           output_tokens, cached_tokens),
                               "replyChars": len(full_reply),
                               "messages": len(messages), "reply": full_reply})

            # 记住这一轮；超过阈值会自动异步压缩更早的对话（不阻塞本次回答）
            await append_turn(session_id, message, full_reply, user.tenant_id, user_id)

            await cache.set(system_prompt, message, full_reply, input_tokens + output_tokens, scope)

            asyncio.create_task(
                _safe_extract_profile(user_id, message, full_reply, user.tenant_id))

            # 用量统计：落库后才能在看板上看到真实数字（此前 record_api_call 从未被调用）
            from app.routes.monitor import record_api_call
            record_api_call(feature="chat", input_tokens=input_tokens, output_tokens=output_tokens,
                            latency_ms=round((time.time() - started) * 1000), from_cache=False,
                            tenant_id=user.tenant_id, user_id=user_id,
                            model=chat_model_name(), cached_input_tokens=cached_tokens)

            trace_step("response", "返回回答", detail={
                "reply": full_reply,
                "inputTokens": input_tokens, "outputTokens": output_tokens,
                "sources": len(sources),
                "missNotice": miss_notice or None,
            })
            await trace.finish(summary={
                "cached": False, "sources": len(sources),
                "inputTokens": input_tokens, "outputTokens": output_tokens,
                "replyChars": len(full_reply),
            })

            yield "done", {
                "fromCache": False, "inputTokens": input_tokens, "outputTokens": output_tokens,
                "sourceCount": len(sources), "runId": trace.run_id,
            }
            logger.info("chat done", {
                "sessionId": session_id, "inputTokens": input_tokens,
                "outputTokens": output_tokens, "replyLen": len(full_reply),
                "sources": len(sources), "user": user.user_id,
            })
        except Exception as err:
            logger.error("chat error", {"error": str(err)})
            trace.fail("对话处理失败", err)
            await trace.finish(status="error")
            raise
        finally:
            # 客户端中途断开（GeneratorExit）等异常路径：把已经记下的步骤照样落库，
            # 否则"这次为什么没出结果"就永远查不到了。finish 幂等，已收尾时是空操作。
            try:
                await trace.finish(status="cancelled")
            except Exception:  # noqa: BLE001
                pass

    return sse_stream(generator)


async def _safe_extract_profile(user_id: str, message: str, reply: str, tenant_id: str):
    try:
        await extract_and_update_profile(user_id, message, reply, tenant_id)
    except Exception:
        pass


@router.get("/sessions")
async def sessions(user: User = Depends(current_user)):
    """列出当前租户的会话（会话落库了，重启后这里依然看得到）。"""
    return {"sessions": await list_sessions(user.tenant_id)}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: User = Depends(current_user)):
    await clear_history(session_id, user.tenant_id)
    return {"success": True}


@router.delete("/sessions")
async def clear_sessions(user: User = Depends(current_user)):
    """清空当前租户的全部会话（测试时清理上下文用）。"""
    n = len(await list_sessions(user.tenant_id))
    await clear_all(user.tenant_id)
    logger.info("chat: all sessions cleared", {"count": n})
    return {"success": True, "cleared": n}


@router.get("/profile/{user_id}")
async def profile(user_id: str, user: User = Depends(current_user)):
    return await get_profile(user_id, user.tenant_id)


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
