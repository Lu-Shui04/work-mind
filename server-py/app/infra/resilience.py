# server-py/app/infra/resilience.py
"""生产级韧性组件：超时 / 指数退避重试 / 熔断 / 降级。

## 为什么需要（每一条都是线上会真实发生的事）

1. **超时**：任何一次外部调用都必须有上限。没有超时的重试不是"更可靠"，
   而是把一次抖动放大成雪崩（请求堆积 → 线程/连接池耗尽 → 整个服务不可用）。
2. **指数退避重试 + 抖动**：模型接口 429 / 502 / 连接被重置是常态，
   隔几十毫秒重试一次通常就成功了。退避是为了给上游喘息；**抖动**是为了避免
   所有请求在同一时刻齐步重试（惊群），把刚恢复的上游再打挂一次。
3. **熔断**：上游真挂了的时候，重试只会让每个用户都卡满超时。
   连续失败到阈值就"跳闸"——后续请求**立刻**失败并走降级，既省用户时间，
   也给上游恢复窗口；过了冷却期进入"半开"，放少量探测流量试探恢复没有。
4. **降级**：主模型（DeepSeek）不可用 → 备用模型（智谱 GLM）顶一会儿；
   都不可用 → **明确报错**，绝不返回"看起来成功"的假结果。

## 为什么不用现成的库
- `tenacity` 只有重试，没有熔断；`pybreaker` 没有 async；两者都没有"降级到另一个模型"。
- 自己实现只有 200 行，还能把"第几次重试 / 为什么降级 / 熔断跳闸"直接写进全链路追踪
  （排查时最想知道的就是这几个事实），比黑盒依赖划算。

## 设计要点
- 纯标准库，async 优先。
- 熔断器按**资源名**注册单例（`model:deepseek` / `tool:web_search`），状态可观测。
- 只对**可重试**的错误重试：超时 / 连接错误 / 429 / 5xx；
  参数错误（4xx）重试一百次也还是错，只会浪费用户的钱和时间。
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.logger import logger

# ── 默认参数（都可用环境变量覆盖，便于压测时调小）──────────────────
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "30"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "0.5"))
LLM_MAX_DELAY = float(os.getenv("LLM_RETRY_MAX_DELAY", "8"))

TOOL_TIMEOUT = float(os.getenv("TOOL_TIMEOUT", "20"))
TOOL_MAX_RETRIES = int(os.getenv("TOOL_MAX_RETRIES", "1"))

BREAKER_FAIL_THRESHOLD = int(os.getenv("BREAKER_FAIL_THRESHOLD", "5"))
BREAKER_RECOVERY_SECONDS = float(os.getenv("BREAKER_RECOVERY_SECONDS", "30"))
BREAKER_HALF_OPEN_CALLS = int(os.getenv("BREAKER_HALF_OPEN_CALLS", "1"))

# 只看这几个状态码判断"该不该重试"
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_RETRYABLE_HINTS = ("timeout", "timed out", "connection", "temporarily", "overloaded",
                    "rate limit", "too many requests", "reset by peer", "unavailable")
# 从错误消息里捞 HTTP 状态码（"Error code: 503" / "502 Bad Gateway" / "HTTP 429"）
_STATUS_IN_MSG = re.compile(r"\b(\d{3})\b")


class BreakerOpenError(RuntimeError):
    """熔断器处于打开状态：这次调用被直接拒绝（没打到上游）。"""


class RetryExhaustedError(RuntimeError):
    """重试次数用尽，仍然失败（原始异常挂在 __cause__ 上）。"""


class CallTimeoutError(RuntimeError):
    """调用超时。"""


def is_retryable(err: BaseException) -> bool:
    """判断这个异常值不值得重试。

    不值得的：参数错误、鉴权失败、内容审核不通过 —— 重试只是重复烧钱。
    值得的：超时、连接层错误、429/5xx（上游抖动或过载）。
    """
    if isinstance(err, (asyncio.TimeoutError, TimeoutError, ConnectionError,
                        BreakerOpenError, CallTimeoutError)):
        # BreakerOpenError 单看是"不该重试"的，但调用方（降级逻辑）需要知道它；
        # 这里返回 True 只会让它被立即重试，所以显式排除。
        return not isinstance(err, BreakerOpenError)
    status = getattr(err, "status_code", None)
    if status is None:
        resp = getattr(err, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        if status in _RETRYABLE_STATUS:
            return True
        if 400 <= status < 500:
            return False

    msg = str(err).lower()
    # 很多上游库不挂 status_code，只把状态码写进消息（"Error code: 429"、"502 Bad Gateway"）。
    # 实测踩过：只按关键词判断时，"502 Bad Gateway" 会被当成不可重试 ——
    # 于是网关抖一下就直接把错误抛给用户，而重试一次通常就好了。
    m = _STATUS_IN_MSG.search(msg)
    if m:
        code = int(m.group(1))
        if code in _RETRYABLE_STATUS:
            return True
        if 400 <= code < 500:
            return False
    return any(hint in msg for hint in _RETRYABLE_HINTS)


def backoff_delay(attempt: int, base: float = LLM_BASE_DELAY,
                  max_delay: float = LLM_MAX_DELAY) -> float:
    """第 attempt 次重试前要等多久（指数退避 + 抖动）。

    公式：`min(max_delay, base * 2**attempt)` 再叠加抖动 ——
    取一半固定、一半随机（equal jitter）：既保留退避的节奏，又不会让一批请求
    在同一毫秒齐步重试。
    """
    ceiling = min(max_delay, base * (2 ** max(0, attempt)))
    return round(ceiling / 2 + random.uniform(0, ceiling / 2), 3)


# ── 熔断器 ──────────────────────────────────────────────────────
@dataclass
class CircuitBreaker:
    """按资源维度的熔断器（closed → open → half_open → closed）。

    状态机：
      closed    正常放行；连续失败达到阈值 → open
      open      直接拒绝（不碰上游）；冷却时间到 → half_open
      half_open 只放行少量探测请求：成功 → closed；失败 → 立刻回到 open
    """

    name: str
    fail_threshold: int = BREAKER_FAIL_THRESHOLD
    recovery_seconds: float = BREAKER_RECOVERY_SECONDS
    half_open_calls: int = BREAKER_HALF_OPEN_CALLS

    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_inflight: int = 0
    # 观测指标（看板/健康检查会读）
    total_calls: int = 0
    total_failures: int = 0
    total_rejected: int = 0
    total_opened: int = 0
    last_error: str = ""
    last_state_change: float = field(default_factory=time.time)

    def allow(self) -> bool:
        """这次调用放不放行（放行不代表一定成功，只是"可以试")。"""
        now = time.time()
        if self.state == "open":
            if now - self.opened_at >= self.recovery_seconds:
                self._to_half_open(now)
            else:
                self.total_rejected += 1
                return False
        if self.state == "half_open":
            if self.half_open_inflight >= self.half_open_calls:
                self.total_rejected += 1
                return False
            self.half_open_inflight += 1
        return True

    def on_success(self) -> None:
        self.total_calls += 1
        self.consecutive_failures = 0
        self.half_open_inflight = max(0, self.half_open_inflight - 1)
        if self.state != "closed":
            logger.info("breaker: closed (上游恢复)", {"breaker": self.name})
            self.state = "closed"
            self.last_state_change = time.time()

    def on_failure(self, err: BaseException) -> None:
        self.total_calls += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.last_error = f"{type(err).__name__}: {err}"[:300]
        self.half_open_inflight = max(0, self.half_open_inflight - 1)
        # 半开状态下探测失败 → 立刻重新跳闸（不用再等攒够阈值）
        if self.state == "half_open" or self.consecutive_failures >= self.fail_threshold:
            self._to_open()

    def _to_open(self) -> None:
        self.state = "open"
        self.opened_at = time.time()
        self.total_opened += 1
        self.last_state_change = self.opened_at
        logger.warn("breaker: OPEN（后续请求直接降级）", {
            "breaker": self.name, "failures": self.consecutive_failures,
            "recoverySec": self.recovery_seconds, "lastError": self.last_error,
        })

    def _to_half_open(self, now: float) -> None:
        self.state = "half_open"
        self.half_open_inflight = 0
        self.last_state_change = now
        logger.info("breaker: half-open（放探测流量）", {"breaker": self.name})

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "consecutiveFailures": self.consecutive_failures,
            "totalCalls": self.total_calls,
            "totalFailures": self.total_failures,
            "totalRejected": self.total_rejected,
            "totalOpened": self.total_opened,
            "lastError": self.last_error,
            "stateChangedAt": self.last_state_change,
            "recoverInSec": (round(max(0.0, self.recovery_seconds - (time.time() - self.opened_at)), 1)
                             if self.state == "open" else 0.0),
        }


_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **overrides) -> CircuitBreaker:
    """按名字拿（或建）熔断器单例 —— 同一个资源全局共用一个状态。"""
    breaker = _BREAKERS.get(name)
    if breaker is None:
        breaker = CircuitBreaker(name=name, **overrides)
        _BREAKERS[name] = breaker
    return breaker


def breaker_snapshots() -> list[dict]:
    return [b.snapshot() for b in _BREAKERS.values()]


def reset_breakers() -> None:
    """测试用：清空所有熔断器状态。"""
    _BREAKERS.clear()


# ── 全局计数（观测用：重试了几次、超时几次、熔断挡了多少）──────────
_stats = {
    "calls": 0, "success": 0, "failures": 0, "retries": 0,
    "timeouts": 0, "breakerRejected": 0, "fallbacks": 0,
}


def stats() -> dict:
    return dict(_stats)


def bump_metric(key: str, n: int = 1) -> None:
    _stats[key] = _stats.get(key, 0) + n


async def call_with_resilience(
    fn: Callable[[], Awaitable[Any]],
    *,
    name: str,
    timeout: float | None = None,
    retries: int = 0,
    base_delay: float = LLM_BASE_DELAY,
    max_delay: float = LLM_MAX_DELAY,
    breaker_name: str | None = None,
    retry_if: Callable[[BaseException], bool] = is_retryable,
    on_event: Callable[[str, dict], None] | None = None,
) -> Any:
    """带超时 + 指数退避重试 + 熔断的调用。

    参数：
      fn            无参 async 工厂（每次重试都会重新调用它，拿一个新的 awaitable）
      name          资源名，日志/追踪里用
      timeout       单次尝试的超时（秒）；None = 不额外加超时（由底层客户端自己控）
      retries       最多重试次数（总尝试 = retries + 1）
      breaker_name  熔断器名；None = 本次不参与熔断（比如探测流量）
      on_event      回调 (kind, detail)：retry / timeout / breaker_open / gave_up，
                    调用方可以据此写进全链路追踪
    """
    _stats["calls"] += 1
    breaker = get_breaker(breaker_name) if breaker_name else None

    if breaker is not None and not breaker.allow():
        _stats["breakerRejected"] += 1
        reason = f"熔断器 {breaker_name} 处于打开状态（连续失败 {breaker.consecutive_failures} 次）"
        if on_event:
            on_event("breaker_open", {"name": name, "breaker": breaker_name,
                                      "detail": breaker.snapshot()})
        raise BreakerOpenError(reason)

    attempt = 0
    last_err: BaseException | None = None
    while attempt <= retries:
        try:
            coro = fn()
            result = await (asyncio.wait_for(coro, timeout=timeout) if timeout else coro)
            if breaker is not None:
                breaker.on_success()
            _stats["success"] += 1
            return result
        except asyncio.CancelledError:
            # 调用方主动取消（比如用户点了停止）：不算失败，也不该触发熔断
            if breaker is not None:
                breaker.half_open_inflight = max(0, breaker.half_open_inflight - 1)
            raise
        except Exception as err:  # noqa: BLE001 - 这里必须接住所有异常做分类
            last_err = err
            timed_out = isinstance(err, (asyncio.TimeoutError, TimeoutError))
            if timed_out:
                _stats["timeouts"] += 1
            if breaker is not None:
                breaker.on_failure(err)
            will_retry = attempt < retries and retry_if(err)
            if on_event:
                on_event("timeout" if timed_out else "error", {
                    "name": name, "attempt": attempt + 1, "willRetry": will_retry,
                    "error": f"{type(err).__name__}: {err}"[:200],
                })
            if not will_retry:
                break
            delay = backoff_delay(attempt, base_delay, max_delay)
            _stats["retries"] += 1
            logger.warn("resilience: 重试", {"name": name, "attempt": attempt + 1,
                                             "delaySec": delay, "error": str(err)[:160]})
            if on_event:
                on_event("retry", {"name": name, "attempt": attempt + 1, "delaySec": delay})
            await asyncio.sleep(delay)
            attempt += 1

    _stats["failures"] += 1
    if on_event:
        on_event("gave_up", {"name": name, "attempts": attempt + 1,
                             "error": f"{type(last_err).__name__}: {last_err}"[:200]})
    raise RetryExhaustedError(f"{name} 调用失败（尝试 {attempt + 1} 次）：{last_err}") from last_err


def health_snapshot() -> dict:
    """给 /health 用的可观测快照。"""
    return {
        "counters": stats(),
        "breakers": breaker_snapshots(),
        "config": {
            "llmTimeout": LLM_TIMEOUT, "llmRetries": LLM_MAX_RETRIES,
            "toolTimeout": TOOL_TIMEOUT, "toolRetries": TOOL_MAX_RETRIES,
            "breakerFailThreshold": BREAKER_FAIL_THRESHOLD,
            "breakerRecoverySeconds": BREAKER_RECOVERY_SECONDS,
        },
    }
