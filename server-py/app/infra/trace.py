# server-py/app/infra/trace.py
"""全链路追踪：把一个请求内部到底发生了什么，按时间顺序记下来。

为什么需要它
- 现在排查问题靠 `docker logs | grep`：一次对话几十行、跑完就翻不到，
  也没法对着"某一条聊天记录"回看它当时到底走了哪条路。
- 用户能看到的只有最后那句回答；"为什么这次没查知识库 / 工具参数为什么是错的 /
  这 8 秒花在哪"这些问题，日志里全是碎片。
所以给每个请求起一条 trace：run（一次请求）+ step（一个步骤），落 PostgreSQL，
前端「全链路追踪」页按时间线展开，每一步的入参/出参都能点开看 ——
相当于把开发者盯终端的那套流程，变成可回看、可搜索、能对着某条消息打开的东西。

设计要点（都是踩过坑才这么写的）
1. **ContextVar 传递**：和 current_user 一个路子。入口 start_trace() 之后，
   调用链深处（意图、检索、重排、工具、模型）直接 trace_step(...) 即可，
   不用把 trace 对象一层层塞进函数签名。
2. **缓冲 + 一次落库**：步骤先存内存，finish() 时一个事务写 run + 全部 step。
   追踪绝不能拖慢正常请求（尤其 SSE 流式），更不能因为写库失败把业务带崩。
3. **截断**：detail 里的长文本（回答全文、原文切片、system prompt）按上限截断，
   避免一条 trace 几百 KB 把库撑爆；截断处会留下说明，不会假装数据是完整的。
4. **没有追踪时零成本**：current_trace() 为 None 时所有 trace_step 都是空操作。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextvars import ContextVar

from app.core.db import get_pool
from app.core.logger import logger

# 单个字符串字段的字符上限（回答/prompt/切片原文都会走这里）
MAX_STR = int(os.getenv("TRACE_MAX_STR", "2000"))
# 单步 detail 序列化后的字节上限，超了就只留预览
MAX_DETAIL_BYTES = int(os.getenv("TRACE_MAX_DETAIL", "16000"))
# 单个 run 最多记多少步（跑飞的循环不至于把内存和库撑爆）
MAX_STEPS = int(os.getenv("TRACE_MAX_STEPS", "300"))
# 保留天数：超期的 trace 在 finish 时顺手清掉（索引在 ts 上，代价很低）
RETENTION_DAYS = int(os.getenv("TRACE_RETENTION_DAYS", "7"))


def _clip(value, depth: int = 0):
    """递归裁剪：长字符串截断、超长列表/字典只留前若干项。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_STR:
            return value
        return value[:MAX_STR] + f"…（已截断，原文共 {len(value)} 字）"
    if depth >= 6:
        return "…（层级过深，已省略）"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 60:
                out["…"] = f"另有 {len(value) - 60} 个字段"
                break
            out[str(k)] = _clip(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [_clip(v, depth + 1) for v in items[:60]]
        if len(items) > 60:
            out.append(f"…（另有 {len(items) - 60} 项）")
        return out
    return _clip(str(value), depth + 1)


def _pack_detail(detail) -> dict:
    """裁剪 + 兜底：detail 必须是能存进 JSONB 的字典。"""
    if detail is None:
        return {}
    if not isinstance(detail, dict):
        detail = {"value": detail}
    clipped = _clip(detail)
    try:
        text = json.dumps(clipped, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 出现无法序列化的对象时不要让追踪本身报错
        return {"note": "detail 无法序列化", "repr": _clip(str(detail))}
    if len(text.encode("utf-8")) > MAX_DETAIL_BYTES:
        return {
            "truncated": True,
            "bytes": len(text.encode("utf-8")),
            "preview": text[: MAX_DETAIL_BYTES // 2],
        }
    return clipped


class Trace:
    """一次请求的追踪上下文。"""

    def __init__(self, feature: str, question: str = "", tenant_id: str = "",
                 user_id: str = "", user_name: str = "", meta: dict | None = None):
        self.run_id = "tr_" + uuid.uuid4().hex[:12]
        self.feature = feature
        self.question = question or ""
        self.tenant_id = tenant_id or ""
        self.user_id = user_id or ""
        self.user_name = user_name or ""
        self.started = time.time()
        self.steps: list[dict] = []
        self.status = "running"
        self.error = ""
        self.summary: dict = dict(meta or {})
        self._finished = False
        self._dropped = 0

    # ── 记步骤 ────────────────────────────────────────────────────
    def step(self, kind: str, name: str, *, status: str = "ok",
             detail=None, duration_ms: int = 0) -> None:
        if self._finished:
            return
        if len(self.steps) >= MAX_STEPS:
            self._dropped += 1
            return
        now = time.time()
        self.steps.append({
            "idx": len(self.steps),
            "offset_ms": int((now - self.started) * 1000),
            "kind": kind,
            "name": name,
            "status": status,
            "duration_ms": int(duration_ms or 0),
            "detail": _pack_detail(detail),
        })

    def span(self, kind: str, name: str, detail=None):
        """with / async with 都能用的计时片段：退出时自动记录耗时与异常。"""
        return _Span(self, kind, name, detail)

    def fail(self, name: str, err: BaseException, detail=None) -> None:
        self.error = f"{type(err).__name__}: {err}"
        self.step("error", name, status="error", detail={"error": self.error, **(detail or {})})

    # ── 收尾落库 ──────────────────────────────────────────────────
    async def finish(self, *, status: str = "ok", summary: dict | None = None,
                     error: str | None = None) -> str:
        if self._finished:
            return self.run_id
        self._finished = True
        self.status = status
        if error:
            self.error = error
        if summary:
            self.summary.update(summary)
        if self._dropped:
            self.summary["droppedSteps"] = self._dropped
        duration_ms = int((time.time() - self.started) * 1000)

        pool = get_pool()
        if pool is None:
            # 没库（或库挂了）时追踪静默失效：不能因为"记日志"把业务带崩
            logger.warn("trace: 数据库不可用，本次追踪未落库", {"runId": self.run_id})
            return self.run_id
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO trace_runs
                            (run_id, ts, feature, question, tenant_id, user_id, user_name,
                             status, duration_ms, step_count, error, summary)
                        VALUES ($1, to_timestamp($2), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        self.run_id, self.started, self.feature, _clip(self.question),
                        self.tenant_id, self.user_id, self.user_name,
                        self.status, duration_ms, len(self.steps), self.error or None,
                        json.dumps(_clip(self.summary), ensure_ascii=False, default=str),
                    )
                    if self.steps:
                        await conn.executemany(
                            """
                            INSERT INTO trace_steps
                                (run_id, idx, ts, kind, name, status, duration_ms, detail)
                            VALUES ($1, $2, to_timestamp($3), $4, $5, $6, $7, $8::jsonb)
                            """,
                            [(self.run_id, s["idx"],
                              self.started + s["offset_ms"] / 1000.0,
                              s["kind"], s["name"], s["status"], s["duration_ms"],
                              json.dumps(s["detail"], ensure_ascii=False, default=str))
                             for s in self.steps],
                        )
                    # 保留策略：顺手清掉过期的（trace_runs.ts 上有索引）
                    await conn.execute(
                        "DELETE FROM trace_runs WHERE ts < now() - ($1 || ' days')::interval",
                        str(RETENTION_DAYS),
                    )
        except Exception as err:  # noqa: BLE001
            logger.warn("trace: 落库失败", {"runId": self.run_id, "error": str(err)})
        return self.run_id


class _Span:
    """计时片段：with / async with 都能用。

    detail 可以在片段内部继续补充（比如检索完把命中数写进去），
    退出时统一记录；片段内抛异常则自动记成 error 步骤。
    """

    def __init__(self, trace: Trace, kind: str, name: str, detail=None):
        self.trace = trace
        self.kind = kind
        self.name = name
        self.detail = dict(detail or {})
        self.t0 = time.time()
        self._done = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._close(exc)
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._close(exc)
        return False

    def _close(self, exc) -> None:
        if self._done:
            return
        self._done = True
        duration = int((time.time() - self.t0) * 1000)
        if exc is not None:
            self.detail["error"] = f"{type(exc).__name__}: {exc}"
            self.trace.step(self.kind, self.name, status="error",
                            detail=self.detail, duration_ms=duration)
        else:
            self.trace.step(self.kind, self.name, detail=self.detail, duration_ms=duration)


class _NullSpan:
    """没有追踪上下文时的占位片段：能当 with / async with 用，什么都不记。"""

    def __init__(self, kind, name, detail=None):
        self.kind, self.name = kind, name
        self.detail = dict(detail or {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_current: ContextVar[Trace | None] = ContextVar("workmind_trace", default=None)


def start_trace(feature: str, question: str = "", tenant_id: str = "",
                user_id: str = "", user_name: str = "", meta: dict | None = None) -> Trace:
    """开一条追踪并设为当前上下文（在入口处调用一次）。"""
    trace = Trace(feature, question, tenant_id, user_id, user_name, meta)
    _current.set(trace)
    return trace


def current_trace() -> Trace | None:
    return _current.get()


def trace_step(kind: str, name: str, *, status: str = "ok",
               detail=None, duration_ms: int = 0) -> None:
    """在任意调用链深处记一步（没有追踪上下文时是空操作）。"""
    trace = _current.get()
    if trace is not None:
        trace.step(kind, name, status=status, detail=detail, duration_ms=duration_ms)



