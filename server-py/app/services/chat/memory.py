# server-py/app/services/chat/memory.py
# 会话记忆：短期（最近若干轮原文）+ 中期（超长对话的滚动摘要）+ 用户画像（跨会话）
#
# 为什么不只在内存里存：
#   以前三种记忆都是进程内的 dict —— 后端一重启（改一行代码、docker compose up --build、
#   机器重启）就全没了。用户上一句刚说过"我叫李雷"，下一句就不认识了，
#   而且没有任何提示，看起来像模型变笨了。现在落 PostgreSQL，重启照旧记得。
#
# 为什么还要保留内存兜底：
#   数据库连不上时，"对话"不应该跟着一起挂掉 —— 退回进程内存继续能用，
#   同时在日志里明确记一条降级，避免又变成"重启就丢、却没人知道为什么"。
import asyncio
import json
import os
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.services.db import get_pool
from app.services.model import chat_model
from app.utils.logger import logger

# 超过多少轮开始压缩（一轮 = 一问一答）
SUMMARY_AFTER_ROUNDS = int(os.getenv("MEMORY_SUMMARY_AFTER_ROUNDS", "10"))
# 每次请求带最近多少轮原文
KEEP_RECENT_ROUNDS = int(os.getenv("MEMORY_KEEP_ROUNDS", "10"))
# 最近原文再长也不超过这么多 token（极端长回复的兜底）
RECENT_MAX_TOKENS = int(os.getenv("MEMORY_RECENT_MAX_TOKENS", "4000"))

DEFAULT_TENANT = "tenant-demo"
DEFAULT_SESSION = "default"


# ── Token 估算（不调 API，本地估算）────────────────────────────
def _est_tokens(text: str = "") -> int:
    cn = len(re.findall(r"[一-鿿]", text or ""))
    return int(cn * 0.6 + (len(text or "") - cn) * 0.25 + 0.999999)


# ── 内存兜底（数据库不可用时使用）──────────────────────────────
_mem_sessions: dict[str, dict] = {}   # "tenant|session" → {"summary": str, "messages": [dict]}
_mem_profiles: dict[str, dict] = {}   # "tenant|user"    → profile dict
# 正在跑摘要任务的会话，避免同一会话并发压缩（会互相覆盖摘要）
_summarizing: set[str] = set()
_degraded_notice_sent = False


def _mem_key(tenant_id: str | None, key: str) -> str:
    return f"{tenant_id or DEFAULT_TENANT}|{key}"


def _note_degraded(reason: str) -> None:
    """数据库不可用只提醒一次，避免每条消息刷屏。"""
    global _degraded_notice_sent
    if _degraded_notice_sent:
        return
    _degraded_notice_sent = True
    logger.warn("memory: 数据库不可用，会话记忆退回进程内存（重启会丢）", {"reason": reason[:160]})


def using_database() -> bool:
    """当前记忆是否落在数据库上（看板/自检用）。"""
    return get_pool() is not None


# ── 会话：读写 ──────────────────────────────────────────────────
async def _ensure_session(conn, tenant_id: str, session_id: str, user_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO chat_sessions (tenant_id, session_id, user_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id, session_id)
        DO UPDATE SET updated_at = now(), user_id = EXCLUDED.user_id
        """,
        tenant_id, session_id, user_id or "anonymous",
    )


async def get_history(session_id: str, tenant_id: str | None = None) -> list[dict]:
    """整段历史（摘要压缩过的那部分已经不在里面了）。"""
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, content FROM chat_messages "
                    "WHERE tenant_id = $1 AND session_id = $2 ORDER BY id",
                    tenant, session_id,
                )
            return [{"role": r["role"], "content": r["content"]} for r in rows]
        except Exception as err:  # noqa: BLE001 - 存储异常不能让对话挂掉
            _note_degraded(str(err))
    return list(_mem_sessions.get(_mem_key(tenant, session_id), {}).get("messages", []))


async def get_summary(session_id: str, tenant_id: str | None = None) -> str:
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                value = await conn.fetchval(
                    "SELECT summary FROM chat_sessions WHERE tenant_id = $1 AND session_id = $2",
                    tenant, session_id,
                )
            return value or ""
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))
    return _mem_sessions.get(_mem_key(tenant, session_id), {}).get("summary", "")


async def rounds_of(session_id: str, tenant_id: str | None = None) -> int:
    """已经发生了多少轮对话（一问一答算一轮）。"""
    return len(await get_history(session_id, tenant_id)) // 2


# ── Token 感知截取：从最新消息往前，塞满为止 ────────────────────
def trim_history(history: list, max_tokens: int = 2000) -> list:
    result: list = []
    total = 0
    for msg in reversed(history):
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        t = _est_tokens(content or "")
        if total + t > max_tokens and result:
            break
        result.insert(0, msg)
        total += t
    return result


# ── 供模型使用的上下文 ──────────────────────────────────────────
async def memory_messages(session_id: str, tenant_id: str | None = None,
                          keep_rounds: int | None = None) -> list:
    """组装本轮要带上的记忆：[SystemMessage(摘要)] + 最近 N 轮原文。"""
    keep = (keep_rounds or KEEP_RECENT_ROUNDS) * 2
    history = await get_history(session_id, tenant_id)
    recent = history[-keep:] if keep else []
    recent = trim_history(recent, RECENT_MAX_TOKENS) if recent else []

    out: list = []
    summary = await get_summary(session_id, tenant_id)
    if summary:
        out.append(SystemMessage(content=(
            "【此前对话的摘要】（更早的对话已经压缩成下面这段，请把它当作已经发生过的事实）\n"
            + summary
        )))
    for item in recent:
        content = item.get("content", "")
        if item.get("role") == "user":
            out.append(HumanMessage(content=content))
        else:
            out.append(AIMessage(content=content))
    return out


async def memory_block(session_id: str, tenant_id: str | None = None) -> str:
    """给 system prompt 用的纯文本记忆块（Agent 的工具循环走这条）。"""
    parts: list[str] = []
    summary = await get_summary(session_id, tenant_id)
    if summary:
        parts.append("【此前对话的摘要】\n" + summary)

    history = await get_history(session_id, tenant_id)
    recent = history[-KEEP_RECENT_ROUNDS * 2:]
    if recent:
        lines = []
        for item in recent:
            who = "用户" if item.get("role") == "user" else "助手"
            text = re.sub(r"\s+", " ", item.get("content", ""))[:400]
            lines.append(f"{who}：{text}")
        parts.append("【最近对话】\n" + "\n".join(lines))
    return ("\n\n".join(parts)).strip()


# ── 滚动摘要 ────────────────────────────────────────────────────
class _Summary(BaseModel):
    summary: str = Field(description="合并后的对话摘要，不超过 400 字")


_SUMMARY_SYSTEM = """你在维护一段多轮对话的长期记忆：把「已有摘要」和「新增对话」合并成一份新摘要。

要求：
1. 保留：用户身份与偏好、已经确认的事实与结论、未完成的事项、关键数字与专有名词
2. 丢弃：寒暄客套、重复内容、已经被推翻的说法
3. 用第三人称陈述（"用户…"、"助手…"），条目化，不超过 400 字
4. 只输出摘要正文，不要任何解释或前后缀"""


async def summarize_session(session_id: str, tenant_id: str | None = None) -> str | None:
    """把"除最近 N 轮之外"的对话压缩进摘要。失败不影响对话，静默返回 None。"""
    tenant = tenant_id or DEFAULT_TENANT
    # 同一进程内按会话去重；多进程部署时数据库侧的删除是幂等的，不会重复压缩
    key = _mem_key(tenant, session_id)
    if key in _summarizing:
        return None

    history = await get_history(session_id, tenant)
    keep = KEEP_RECENT_ROUNDS * 2
    if len(history) <= keep:
        return None

    older = history[:len(history) - keep]
    _summarizing.add(key)
    try:
        transcript = "\n".join(
            f"{'用户' if m.get('role') == 'user' else '助手'}：{m.get('content', '')[:800]}"
            for m in older
        )
        previous = (await get_summary(session_id, tenant)) or "（还没有摘要）"
        result: _Summary = await chat_model.with_structured_output(
            _Summary, method="function_calling").ainvoke([
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": f"已有摘要：\n{previous}\n\n新增对话：\n{transcript}"},
            ])
        summary = (result.summary or "").strip()
        if not summary:
            return None

        # 先写摘要再删历史：万一中间出错，宁可多留一份原文，也不要丢记忆
        pool = get_pool()
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE chat_sessions SET summary = $3, summary_at = now(), updated_at = now() "
                            "WHERE tenant_id = $1 AND session_id = $2",
                            tenant, session_id, summary,
                        )
                        # 按"要压缩的条数"删除（id 升序，删最老的 len(older) 条），
                        # 并发追加的新消息 id 更大，不会被误删
                        await conn.execute(
                            "DELETE FROM chat_messages WHERE id IN ("
                            "  SELECT id FROM chat_messages WHERE tenant_id = $1 AND session_id = $2 "
                            "  ORDER BY id LIMIT $3)",
                            tenant, session_id, len(older),
                        )
                logger.info("memory: 摘要已更新（db）", {"sessionId": session_id,
                                                        "compressed": len(older)})
                return summary
            except Exception as err:  # noqa: BLE001
                _note_degraded(str(err))

        state = _mem_sessions.setdefault(key, {"summary": "", "messages": []})
        state["summary"] = summary
        del state["messages"][:len(older)]
        logger.info("memory: 摘要已更新（内存兜底）", {"sessionId": session_id,
                                                     "compressed": len(older)})
        return summary
    except Exception as err:  # noqa: BLE001 - 摘要失败绝不能影响正常对话
        logger.warn("memory: 摘要失败，保留原文", {"sessionId": session_id, "error": str(err)[:160]})
        return None
    finally:
        _summarizing.discard(key)


def schedule_summary(session_id: str, tenant_id: str | None = None) -> bool:
    """对话超长时**异步**触发摘要，不阻塞本轮回答。返回是否触发。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环（同步调用/单元测试）：跳过摘要，绝不能因此把对话搞崩
        return False
    loop.create_task(summarize_session(session_id, tenant_id))
    return True


async def append_turn(session_id: str, user_text: str, assistant_text: str,
                      tenant_id: str | None = None, user_id: str = "anonymous") -> None:
    """记录一轮对话，并在超过阈值时异步压缩更早的部分。"""
    tenant = tenant_id or DEFAULT_TENANT
    session_id = session_id or DEFAULT_SESSION
    stored = False

    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _ensure_session(conn, tenant, session_id, user_id)
                    await conn.executemany(
                        "INSERT INTO chat_messages (tenant_id, session_id, role, content) "
                        "VALUES ($1, $2, $3, $4)",
                        [(tenant, session_id, "user", user_text),
                         (tenant, session_id, "assistant", assistant_text)],
                    )
            stored = True
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))

    if not stored:
        state = _mem_sessions.setdefault(_mem_key(tenant, session_id),
                                         {"summary": "", "messages": [], "user_id": user_id})
        state["messages"].append({"role": "user", "content": user_text})
        state["messages"].append({"role": "assistant", "content": assistant_text})

    if await rounds_of(session_id, tenant) > SUMMARY_AFTER_ROUNDS:
        schedule_summary(session_id, tenant)


async def clear_history(session_id: str, tenant_id: str | None = None) -> None:
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                # messages 上有 ON DELETE CASCADE，删会话即删消息
                await conn.execute(
                    "DELETE FROM chat_sessions WHERE tenant_id = $1 AND session_id = $2",
                    tenant, session_id,
                )
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))
    _mem_sessions.pop(_mem_key(tenant, session_id), None)
    _summarizing.discard(_mem_key(tenant, session_id))


async def list_sessions(tenant_id: str | None = None) -> list[dict]:
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT s.session_id, s.summary, count(m.id) AS messages "
                    "FROM chat_sessions s LEFT JOIN chat_messages m "
                    "  ON m.tenant_id = s.tenant_id AND m.session_id = s.session_id "
                    "WHERE s.tenant_id = $1 GROUP BY s.session_id, s.summary "
                    "ORDER BY max(s.updated_at) DESC",
                    tenant,
                )
            return [{"id": r["session_id"], "messageCount": r["messages"],
                     "rounds": r["messages"] // 2, "hasSummary": bool(r["summary"])}
                    for r in rows]
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))
    prefix = f"{tenant}|"
    return [{"id": k[len(prefix):], "messageCount": len(v["messages"]),
             "rounds": len(v["messages"]) // 2, "hasSummary": bool(v.get("summary"))}
            for k, v in _mem_sessions.items() if k.startswith(prefix)]


async def clear_all(tenant_id: str | None = None) -> dict:
    """清空会话历史与用户画像（测试/重置用）。"""
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    cleared = {"sessions": 0, "profiles": 0}
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                cleared["sessions"] = await conn.fetchval(
                    "SELECT count(*) FROM chat_sessions WHERE tenant_id = $1", tenant) or 0
                cleared["profiles"] = await conn.fetchval(
                    "SELECT count(*) FROM user_profiles WHERE tenant_id = $1", tenant) or 0
                await conn.execute("DELETE FROM chat_sessions WHERE tenant_id = $1", tenant)
                await conn.execute("DELETE FROM user_profiles WHERE tenant_id = $1", tenant)
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))
    prefix = f"{tenant}|"
    for store, field in ((_mem_sessions, "sessions"), (_mem_profiles, "profiles")):
        for k in [k for k in store if k.startswith(prefix)]:
            store.pop(k, None)
            cleared[field] += 1
    return cleared


# ── 用户画像（跨会话记忆）──────────────────────────────────────
async def get_profile(user_id: str, tenant_id: str | None = None) -> dict:
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                raw = await conn.fetchval(
                    "SELECT profile FROM user_profiles WHERE tenant_id = $1 AND user_id = $2",
                    tenant, user_id,
                )
            if raw is None:
                return {}
            return json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))
    return dict(_mem_profiles.get(_mem_key(tenant, user_id), {}))


async def _save_profile(user_id: str, profile: dict, tenant_id: str | None = None) -> None:
    tenant = tenant_id or DEFAULT_TENANT
    pool = get_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO user_profiles (tenant_id, user_id, profile) VALUES ($1, $2, $3::jsonb) "
                    "ON CONFLICT (tenant_id, user_id) DO UPDATE "
                    "SET profile = EXCLUDED.profile, updated_at = now()",
                    tenant, user_id, json.dumps(profile, ensure_ascii=False),
                )
            return
        except Exception as err:  # noqa: BLE001
            _note_degraded(str(err))
    _mem_profiles[_mem_key(tenant, user_id)] = profile


def profile_to_context(profile: dict) -> str:
    if not profile:
        return ""

    parts = []
    if profile.get("name"):
        parts.append(f"用户姓名：{profile['name']}")
    if profile.get("dept"):
        parts.append(f"部门：{profile['dept']}")
    if profile.get("techLevel"):
        parts.append(f"技术水平：{profile['techLevel']}")
    if profile.get("primaryStack"):
        parts.append(f"技术栈：{', '.join(profile['primaryStack'])}")
    if profile.get("currentGoal"):
        parts.append(f"当前目标：{profile['currentGoal']}")
    if profile.get("prefersShort"):
        parts.append("偏好简短回答")
    if profile.get("prefersCode"):
        parts.append("偏好带代码示例的回答")

    if not parts:
        return ""
    bullet = "\n".join(f"- {p}" for p in parts)
    return f"\n\n用户背景：\n{bullet}"


class _ProfileExtraction(BaseModel):
    hasInfo: bool = Field(description="是否提取到新信息")
    name: str | None = Field(default=None)
    dept: str | None = Field(default=None)
    techLevel: str | None = Field(default=None, description="初级/中级/高级/架构师")
    primaryStack: list[str] | None = Field(default=None)
    currentGoal: str | None = Field(default=None)
    prefersShort: bool | None = Field(default=None)
    prefersCode: bool | None = Field(default=None)


async def extract_and_update_profile(user_id: str, user_msg: str, ai_reply: str,
                                     tenant_id: str | None = None) -> None:
    """从对话中异步提取用户信息，更新画像（失败静默，不影响主流程）。"""
    try:
        current = await get_profile(user_id, tenant_id)
        extract_model = chat_model.with_structured_output(_ProfileExtraction, method="function_calling")

        result: _ProfileExtraction = await extract_model.ainvoke([
            {
                "role": "system",
                "content": (
                    "从对话中提取用户信息，只填写有明确依据的字段。\n"
                    f"当前已知画像：{current}\n"
                    "如果没有新信息，hasInfo 返回 false。"
                ),
            },
            {"role": "user", "content": f"用户说：{user_msg}\nAI回复：{ai_reply[:200]}"},
        ])

        if not result.hasInfo:
            return

        updated = {**current}
        if result.name:
            updated["name"] = result.name
        if result.dept:
            updated["dept"] = result.dept
        if result.techLevel:
            updated["techLevel"] = result.techLevel
        if result.currentGoal:
            updated["currentGoal"] = result.currentGoal
        if result.prefersShort is not None:
            updated["prefersShort"] = result.prefersShort
        if result.prefersCode is not None:
            updated["prefersCode"] = result.prefersCode
        if result.primaryStack:
            updated["primaryStack"] = list(dict.fromkeys(
                [*current.get("primaryStack", []), *result.primaryStack]))

        await _save_profile(user_id, updated, tenant_id)
    except Exception:
        # 画像提取失败不影响主流程，静默处理
        pass
