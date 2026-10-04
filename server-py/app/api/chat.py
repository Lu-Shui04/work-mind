# server-py/app/api/chat.py
# 对话路由：意图判断 → （按权限）检索知识库 → 带引用流式回答 + 缓存 + 会话管理 + 画像
import asyncio
import time

from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.middleware import rate_limiter, security_check
from app.prompts.chat import ASSISTANT_SYSTEM
from app.infra.cache import cache
from app.infra.tasks import spawn
from app.services.chat.memory import (
    append_turn, clear_all, clear_history, clear_profile, extract_and_update_profile,
    get_profile, list_sessions, memory_messages, profile_to_context,
)
from app.core.db import StorageUnavailable
from app.core.identity import User, current_user
from app.models import pricing
from app.models.llm import chat_model
from app.services.rag.intent import classify_intent, looks_like_knowledge_need
from app.services.rag.query import (
    SearchFilters, build_turn_context, miss_reply, resolve_knowledge_mode,
    retrieve_with_meta,
)
from app.infra.trace import start_trace, trace_step
from app.core.logger import logger
from app.core.sse import sse_stream
from app.infra.tokens import cache_read_of

router = APIRouter()

# 助手人设与"知识优先"规则都在提示词层：app/prompts/chat.py


class ChatStreamRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    sessionId: str | None = "default"
    systemPrompt: str | None = Field(default=None, max_length=2000)
    role: str | None = "default"
    # ⚠️ 缺省必须是 None，不能写 "anonymous"：下面 user_id = body.userId or user.user_id
    #    的本意是"没传就用请求头里的身份"，但默认值一填，or 就永远短路到 "anonymous" ——
    #    画像与会话归属全落到 anonymous 名下（2026-10-04 实测：不带 userId 的请求
    #    把 {"name":"小米"} 存到了 anonymous，而界面按 u-tech-01 查，用户以为没记住）。
    userId: str | None = None
    # 知识库检索：None=自动判断（默认），True=强制检索，False=关闭
    useKnowledge: bool | None = None
    # 检索范围收窄（部门/版本/类型），为空时按自动判断结果与用户权限
    knowledgeDepartment: str | None = None
    knowledgeVersion: str | None = None
    knowledgeDocType: str | None = None
    includeSuperseded: bool = False
    # 跳过答案缓存（评测 / "重新生成" 用）。
    # 为什么必须有这个开关：缓存命中时后端**直接重放答案**，既不发 intent 也不发
    # sources —— 评测会把重放当成模型输出，于是"答案里标了 [1] 却没有来源"这类
    # 假失败冒出来，指标静默失真（2026-09-30 实测：一轮评测 5 条全因此误判失败）。
    noCache: bool = False


def chat_model_name() -> str:
    """当前对话模型的真实名字（计费要按它查单价）。

    注意：这里取的是**配置的**模型名（本项目是 deepseek-chat）。
    DeepSeek 侧返回的 response_metadata.model_name 是 deepseek-flash ——
    老名字实际由 V4.1-Flash 提供服务，定价表里两者都映射到 Flash 档，
    所以用哪个名字都能算对。
    """
    return getattr(chat_model, "model_name", "") or "deepseek-chat"


# 缓存键的口径版本：改了 key 的组成就把它 +1，避免新旧条目互相串味
# v3：把"知识库模式 + 检索范围"并入缓存域（v2 只算权限域，见 _cache_scope）
# v4：目录类问题（"知识库里有哪些内容？"）一度改走确定性目录答复
# v5：目录捷径按用户要求撤掉、回到**正常检索** —— 作答语义又变了一次，
#     所以继续 +1，避免把 v3/v4 期间那批答案（含"库里只有 1 篇"那类）继续发出去
_CACHE_SCOPE_VERSION = "v5"

# 像"追问"的消息不进缓存（也不查缓存）。
# 为什么必须挡这一类：缓存键是「问题 + 权限域 + 检索域 + 画像」，**不含会话历史** ——
# "公司的年假是怎么规定的？"独立成句，复用答案没问题；
# 但"那差旅呢？"的答案强依赖上一句，一旦跨会话复用就会答非所问。
# 挡掉的代价很小（追问本来就是少数），换来的是"不会答错"。
_FOLLOWUP_PREFIXES = ("那", "这", "它", "他", "她", "还有", "再", "继续", "接着", "然后",
                      "另外", "刚才", "上面", "上述", "此", "其", "同样", "一样")


def _is_followup(message: str) -> bool:
    """看起来像"依赖上文"的追问 → 每次都走完整流程（带正确上下文），不进缓存。"""
    text = (message or "").strip()
    if len(text) < 6:                       # 太短的问题通常要靠上下文才成立
        return True
    return text.startswith(_FOLLOWUP_PREFIXES)


def build_turn_messages(system_prompt: str, history: list, turn_context: str,
                        question: str) -> list:
    """本轮消息顺序：人设/画像 → 会话历史 → 【本轮依据块】→ 用户提问。

    ⚠️ 顺序即语义，别再改回去（2026-09-27 实测的真实事故）：
    检索明明命中了 2 条《年假与休假政策》切片，模型却回答"知识库中没有查到年假制度的相关内容"。
    原因既不是检索、也不是资料没给，而是**上一轮的结论盖住了本轮的资料**：
    同一会话里用户上一句问的是"没有年假等？"，那一次恰好没命中（重排把 12 条候选全判成
    "回答不了这个问题"，最高分 0.11），后端按"自动模式未命中"先推了一句
    "（知识库中没有查到相关内容，以下内容来自通用知识…）"，助手也照此答了一遍；
    而资料原先拼在 system prompt 顶部，历史消息排在它后面 ——
    模型把**最近一条助手消息**当成了本轮结论，顶部那两段资料等于没给。

    所以这里有两条硬约束（缺一条就会复发）：
      1) 本轮依据块必须落在**会话历史之后、用户提问之前**（离问题最近，权重最高）；
      2) 块里要显式声明"历史里说过的没查到是上一轮的结论，对本轮无效"（见 build_turn_context 的规则 4）。

    单独抽成函数是为了能被单测钉住：tests/test_turn_context.py 直接断言这里的顺序。
    """
    messages = [SystemMessage(content=system_prompt), *history]
    if turn_context:
        messages.append(SystemMessage(content=turn_context))
    messages.append(HumanMessage(content=question))
    return messages


def _knowledge_scope(body: ChatStreamRequest) -> str:
    """检索域：知识库三态（自动/强制/关闭）+ 用户显式收窄的检索范围。

    为什么它必须进缓存键：这几项**都会改变答案本身**，而它们既不在 system prompt 里
    （检索上下文是检索之后才拼进去的），也不在权限域里 —— 只算权限域就会出现
    "换个范围/换个模式问同一句，拿回来的还是上一种设置的答案"。两个可复现的反例：

      1) 选「本部门 = 财务」提问 → 这个范围内确实没有内容 → 那份"未命中"的回答被写进缓存；
         切回「全部可见」重问同一句 → 直接命中缓存，回的还是"知识库中未找到相关内容"，
         可库里其实有内容 —— 用户会得出"库里没有"的错误结论。
      2) 选「关闭知识库」拿到的是通用知识回答；改成「强制检索」重问同一句 → 命中同一个键，
         "只依据知识库作答、查不到就说查不到"被静默绕过 —— 三态语义在缓存这一层失效。

    取的都是请求体里的原始值（不检索、不花时间），所以"命中直接返回"这个前提不受影响。
    """
    mode = "auto" if body.useKnowledge is None else ("force" if body.useKnowledge else "off")
    return ":".join([
        "kb", mode,
        body.knowledgeDepartment or "-",
        body.knowledgeDocType or "-",
        body.knowledgeVersion or "-",
        "sup" if body.includeSuperseded else "cur",
    ])


def _cache_scope(user: User, body: ChatStreamRequest) -> str:
    """缓存隔离域 = **权限域**（租户/部门/密级）+ **检索域**（知识库模式/检索范围）。

    两条都必须有，少一条都会"张冠李戴"：
      权限域管的是"这个人能不能看到这份答案"（换了身份绝不复用）；
      检索域管的是"这份答案是在什么检索设置下产生的"（见 _knowledge_scope）。

    为什么不再把"命中的切片 id"算进来（v1 的做法）：
    算它就必须**先检索才能算 key** —— 于是每次"命中"仍然要花 1~2 秒做
    意图判定 + 问句向量化 + 向量检索 + 重排，缓存几乎白做。
    实测：命中一次 2695ms，其中检索占 794ms、重放占 1900ms，模型生成是 0。
    现在改成"权限域 + 检索域 + 有效 system prompt（含画像）+ 问题"作 key：
    **请求一进来就能查缓存，命中直接返回**（不再走意图判定与检索）。

    代价（说清楚，不藏着）：知识库更新后，最长一个 TTL（默认 30 分钟）内可能返回旧答案。
    这是任何缓存都有的取舍；权限隔离不受影响 —— 域不同根本不会命中同一个 key。
    想更"新鲜"就把 CACHE_TTL 调小。
    """
    deps = ",".join(sorted(user.departments))
    return (f"{_CACHE_SCOPE_VERSION}|{user.tenant_id}|{deps}|{user.clearance}"
            f"|{_knowledge_scope(body)}")


@router.post("/stream", dependencies=[Depends(rate_limiter)])
async def chat_stream(body: ChatStreamRequest, user: User = Depends(current_user)):
    security_check(body.message)

    session_id = body.sessionId or "default"
    # role 已废弃：四个角色预设已合并为一个助手（人设见 app/prompts/chat.py）
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

            # ── 0) 早查缓存：命中就**直接返回**，不再走意图判定与知识库检索 ──
            # 缓存键 = 权限域（租户/部门/密级）+ 有效 system prompt（含画像）+ 问题。
            # 关键：它**不含检索结果**，所以不必先检索就能查 —— 这是"命中直接返回"的前提。
            # （老做法把"命中的切片 id"算进 key，必须先检索才能查缓存，等于每次命中
            #   仍然白花 1~2 秒做向量化+检索+重排；实测命中一次 2695ms。）
            cache_prompt = (body.systemPrompt or base_system) + profile_ctx
            # scope = 权限域 + 检索域（换身份、换知识库模式、换检索范围都不会串味）
            scope = _cache_scope(user, body)
            # noCache（评测/重新生成）：既不读缓存也不写缓存，保证这次一定打真实模型
            cacheable = (not _is_followup(message)) and not body.noCache
            _lookup_t0 = time.time()
            cached = await cache.get(cache_prompt, message, scope) if cacheable else None
            if body.noCache:
                # 评测/调试专用：缓存命中会直接重放答案（不发 intent / sources），
                # 评测会把重放当成模型输出 → 指标静默失真，所以这条路必须留痕
                trace.step("cache", "按请求跳过缓存（noCache：评测/重新生成）", detail={})
            elif not cacheable:
                trace.step("cache", "跳过缓存（像追问，答案依赖上文）", detail={"message": message})
            if cached:
                text = cached["content"]
                saved = int(cached.get("tokens") or 0)
                trace.step("cache", "命中缓存（未检索、未调用模型）", detail={
                    "scope": scope, "savedTokens": saved, "replyChars": len(text),
                    "lookupMs": round((time.time() - _lookup_t0) * 1000),
                })
                logger.info("cache hit", {"sessionId": session_id, "scope": scope[:48],
                                          "lookupMs": round((time.time() - _lookup_t0) * 1000)})
                yield "cache_hit", {}
                # 大块快速重放：以前每 3 个字 sleep 6ms，一篇 600 字的回答要 1.2 秒 ——
                # 命中本该是"秒回"，却被假打字机拖得跟真调用差不多。
                for i in range(0, len(text), 24):
                    yield "token", {"token": text[i:i + 24]}
                    await asyncio.sleep(0.002)
                # 缓存命中的回答同样要进记忆，否则下一轮会"忘记"这次说过什么
                await append_turn(session_id, message, text, user.tenant_id, user_id)
                # 缓存命中也必须记账：这是看板"缓存命中率"的唯一数据来源
                from app.api.monitor import record_api_call
                record_api_call(
                    feature="chat", from_cache=True, saved_tokens=saved,
                    latency_ms=round((time.time() - started) * 1000),
                    tenant_id=user.tenant_id, user_id=user_id,
                )
                trace.step("response", "返回缓存回答", detail={"reply": text,
                                                               "elapsedMs": round((time.time() - started) * 1000)})
                await trace.finish(summary={"cached": True, "replyChars": len(text),
                                            "savedTokens": saved})
                yield "done", {"fromCache": True, "sourceCount": 0, "savedTokens": saved,
                               "runId": trace.run_id}
                return

            trace.step("cache", "未命中缓存（继续走意图判定与检索）", detail={
                "scope": scope, "lookupMs": round((time.time() - _lookup_t0) * 1000),
            })

            # ── 1) 意图判断：这次要不要查知识库 ──────────────────────
            # 默认"召回优先"：只有明显与知识库无关的消息（打招呼/闲聊/纯算式）才不查，
            # 因为"该不该查库"不该由常识判断 —— 模型根本不知道公司库里有什么。
            # 用户可以在输入框旁显式选择 强制检索 / 关闭检索 来覆盖。
            sources: list[dict] = []
            # 本次作答的知识库三态：no_retrieval（关闭）/ grounded_answer（命中）/ miss（没命中）
            # （唯一裁决处是 resolve_knowledge_mode，下面命中检索后再赋值）
            mode = "no_retrieval"
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

                # 只有「命中资料」或「知识型消息的未命中」才推这个块：
                # 确认/续聊类消息 miss 了不该弹"知识库未命中"提示框（与 Agent 同一条规则，
                # 2026-10-04 实测：用户说"好的，发一下直属主管"却看到召回诊断，很困惑）
                if sources or body.useKnowledge is True or looks_like_knowledge_need(message):
                    yield "sources", {
                        "sources": sources,
                        "recall": recall,
                        "filters": filters.describe(),
                        "identity": {"userId": user.user_id, "departments": user.departments,
                                     "clearance": user.clearance},
                    }

                # ── 没命中时怎么办：回一句引导，**不调用模型** ──────────────
                # 三种模式的语义（唯一裁决处是 resolve_knowledge_mode）：
                #   强制（useKnowledge=true）/ 自动（undefined）没命中：同一句引导，
                #     不拿通用知识顶、也不堆检索诊断（原因在前端回复下方的提示框里）
                #   关闭（false）：压根不检索，直接回答，不会出现"未找到"话术
                # 为什么不再让模型作答：没有依据就不让它发挥 —— 省一次调用，
                # 也不会再把通用常识写成公司规定（详见 query.py 里 miss_reply 的注释）。
                mode = resolve_knowledge_mode(body.useKnowledge, need, sources)
                if (mode == "miss" and body.useKnowledge is not True
                        and not looks_like_knowledge_need(message)):
                    # 消息不是知识型提问（确认/续聊/起名这类）：recall_first 例行检索 miss 了，
                    # 不要把会话堵死在"知识库没查到"的固定答复上，正常交给模型回答
                    mode = "no_retrieval"
                if mode == "miss":
                    text = miss_reply()
                    yield "start", {"sessionId": session_id, "runId": trace.run_id}
                    for i in range(0, len(text), 3):
                        yield "token", {"token": text[i:i + 3]}
                        await asyncio.sleep(0.006)
                    await append_turn(session_id, message, text, user.tenant_id, user_id)
                    # 这条路没调模型（只回固定答复），但"为什么没查到"才是要看的东西 ——
                    # 检索那几步已经在 trace 里了，这里补一条结论
                    trace_step("response", "知识库未命中（不调用模型，只回一句引导）", detail={
                        "reason": (recall or {}).get("reason"),
                        "reply": text,
                    })
                    await trace.finish(summary={"cached": False, "groundedMiss": True,
                                                "reason": (recall or {}).get("reason")})
                    yield "done", {"fromCache": False, "inputTokens": 0, "outputTokens": 0,
                                   "sourceCount": 0, "grounded": True,
                                   "reason": (recall or {}).get("reason"),
                                   "runId": trace.run_id}
                    logger.info("chat: 知识库未命中，回一句引导（不调用模型）", {
                        "reason": (recall or {}).get("reason"), "user": user.user_id,
                        "best": (recall or {}).get("bestScore"),
                    })
                    return

            # ── 3) 组装 system：角色 + 画像；本轮资料另走"依据块"（见下） ────
            # 走到这里 = 本轮检索命中了（没命中的在上面就 return 了）。

            # 本轮依据块 = 检索到的资料 + 引用规则。
            # ⚠️ 它**不能**拼回 system prompt 顶部（以前就是那么写的，被上一轮结论盖掉了）；
            #    必须落在会话历史之后、用户提问之前，原因见 build_turn_messages 的注释。
            turn_context = build_turn_context(sources)
            system_prompt = (body.systemPrompt or base_system) + profile_ctx

            # system prompt 与"本轮依据块"是排查"回答为什么跑偏"的第一手材料，必须留档
            # （两者都是**检索之后**才有的；缓存键用的是检索之前的 cache_prompt，
            #  见上面"早查缓存"那一段 —— 不要混用）
            trace_step("prompt", "组装系统提示词", detail={
                "systemPrompt": system_prompt,
                # 本轮的资料就在这里：回答"为什么没用资料"时第一个要看的就是它
                "turnContext": turn_context,
                "chars": len(system_prompt) + len(turn_context),
                "sourcesInContext": len(sources),
                "profileInjected": bool(profile_ctx),
                "knowledgeMode": mode,
            })

            # 记忆 = 【此前对话摘要】+ 最近 N 轮原文（见 services/chat/memory.py）；
            # 本轮依据块排在记忆之后、提问之前（顺序即语义，见 build_turn_messages）
            messages = build_turn_messages(
                system_prompt, await memory_messages(session_id, user.tenant_id),
                turn_context, message)

            # runId 一并下发：前端把它挂在消息上，"查看全链路"按钮直接就能用
            yield "start", {"sessionId": session_id, "runId": trace.run_id}

            full_reply = ""
            input_tokens = 0
            output_tokens = 0
            cached_tokens = 0      # 输入里命中提示缓存的 token（单价只有 1/50）

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

            # 用**检索之前**的 cache_prompt 作键（与上面早查用的是同一个），
            # 这样下一次同样的提问一进来就能命中，不必再做检索。
            # 追问类消息不写缓存：它的答案依赖上文，复用会答非所问。
            if cacheable:
                await cache.set(cache_prompt, message, full_reply,
                                input_tokens + output_tokens, scope)

            # 走 spawn（强引用）：裸 create_task 只被事件循环弱引用，画像抽取可能
            # 在跑完前被 GC —— 表现是"画像时有时无"且日志干净（2026-10-04 实测）
            spawn(_safe_extract_profile(user_id, message, full_reply, user.tenant_id),
                  name="profile-extract")

            # 用量统计：落库后才能在看板上看到真实数字（此前 record_api_call 从未被调用）
            from app.api.monitor import record_api_call
            record_api_call(feature="chat", input_tokens=input_tokens, output_tokens=output_tokens,
                            latency_ms=round((time.time() - started) * 1000), from_cache=False,
                            tenant_id=user.tenant_id, user_id=user_id,
                            model=chat_model_name(), cached_input_tokens=cached_tokens)

            trace_step("response", "返回回答", detail={
                "reply": full_reply,
                "inputTokens": input_tokens, "outputTokens": output_tokens,
                "sources": len(sources),
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
    """画像抽取的兜底包装：绝不影响主流程，但失败要留日志（见 infra/tasks.spawn）。"""
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


@router.delete("/profile/{user_id}")
async def clear_user_profile(user_id: str, user: User = Depends(current_user)):
    """清除该用户的跨会话画像（前端「清除记忆」按钮）。

    以前前端只把本地 store 置空，刷新一下画像又全回来（库里那条还在）——
    看起来就是"记性能记住、却删不掉"。
    """
    cleared = await clear_profile(user_id, user.tenant_id)
    logger.info("chat: profile cleared", {"userId": user_id, "tenant": user.tenant_id,
                                          "cleared": cleared})
    return {"success": True, "cleared": cleared}


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
