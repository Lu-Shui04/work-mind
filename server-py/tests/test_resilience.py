# server-py/tests/test_resilience.py
"""韧性组件的回归测试（不联网、不调真实模型，全部用假函数注入故障）。

运行方式（容器内）：
    docker exec workmind-server python /app/tests/test_resilience.py

固化的是这几条"线上真的会疼"的规则：
1. **指数退避 + 抖动**：延迟随重试次数增长，但不是一个固定值（避免惊群齐步重试）
2. **只重试值得重试的错误**：超时 / 连接 / 429 / 5xx 才重试；
   参数错误（400/401）重试一百次也还是错，只会白烧钱
3. **超时必须生效**：卡住不返回的调用会被掐断并计入超时指标
4. **熔断状态机**：连续失败到阈值 → open（后续调用**不碰上游**直接拒绝）
   → 冷却期到 → half_open（放探测流量）→ 成功则 closed / 失败则立刻重新 open
5. **不吞掉取消**：用户点"停止"抛出的 CancelledError 不能被当成失败（更不该触发熔断）
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from app.services import resilience as R  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class _Flaky:
    """前 fail_times 次抛错，之后成功 —— 用来验证重试确实发生了。"""

    def __init__(self, fail_times: int, err: Exception | None = None):
        self.calls = 0
        self.fail_times = fail_times
        self.err = err or ConnectionError("connection reset by peer")

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.err
        return "ok"


class _Slow:
    """永远不返回 —— 用来验证超时。"""

    def __init__(self, delay: float = 5.0):
        self.delay = delay
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return "late"


def test_retryable_classification():
    """只有"上游抖动类"错误才值得重试。"""
    assert R.is_retryable(asyncio.TimeoutError()) is True
    assert R.is_retryable(ConnectionError("connection reset")) is True
    assert R.is_retryable(RuntimeError("Error code: 429 - rate limit")) is True
    assert R.is_retryable(RuntimeError("502 Bad Gateway")) is True

    class _Err(Exception):
        def __init__(self, status):
            super().__init__(f"http {status}")
            self.status_code = status

    assert R.is_retryable(_Err(400)) is False      # 参数错误：重试没有意义
    assert R.is_retryable(_Err(401)) is False      # 鉴权失败：重试更没意义
    assert R.is_retryable(_Err(503)) is True


def test_backoff_grows_and_jitters():
    """指数退避：上限随次数翻倍；抖动：同一 attempt 多次采样不应完全相同。"""
    caps = [max(R.backoff_delay(i, base=0.5, max_delay=8) for _ in range(50)) for i in range(4)]
    for i in range(3):
        assert caps[i + 1] >= caps[i], f"退避没有增长：{caps}"
    assert caps[0] <= 0.5 + 1e-6 and caps[3] <= 8.0, f"退避超过上限：{caps}"
    samples = {R.backoff_delay(3, base=0.5, max_delay=8) for _ in range(20)}
    assert len(samples) > 1, "退避没有抖动（会惊群）"


def test_retry_succeeds_after_transient_failures():
    fn = _Flaky(fail_times=2)
    result = run(R.call_with_resilience(fn, name="t", timeout=1, retries=2, base_delay=0.01,
                                        max_delay=0.02))
    assert result == "ok"
    assert fn.calls == 3, f"期望 2 次重试后成功，实际调用 {fn.calls} 次"


def test_retry_gives_up_and_wraps_error():
    fn = _Flaky(fail_times=99)
    try:
        run(R.call_with_resilience(fn, name="t", timeout=1, retries=2, base_delay=0.01,
                                   max_delay=0.02))
        raise AssertionError("应当抛出 RetryExhaustedError")
    except R.RetryExhaustedError as err:
        assert isinstance(err.__cause__, ConnectionError), "原始异常要挂在 __cause__ 上"
    assert fn.calls == 3, f"重试次数应为 retries+1=3，实际 {fn.calls}"


def test_non_retryable_error_is_not_retried():
    class _Err(Exception):
        status_code = 400

    fn = _Flaky(fail_times=99, err=_Err("bad request"))
    try:
        run(R.call_with_resilience(fn, name="t", timeout=1, retries=3, base_delay=0.01))
    except R.RetryExhaustedError:
        pass
    assert fn.calls == 1, f"400 不该重试，实际调用 {fn.calls} 次"


def test_timeout_is_enforced():
    fn = _Slow(delay=2.0)
    t0 = time.time()
    try:
        run(R.call_with_resilience(fn, name="t", timeout=0.15, retries=0))
        raise AssertionError("应当超时")
    except R.RetryExhaustedError:
        pass
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"超时没有生效，等了 {elapsed:.2f}s"


def test_circuit_breaker_opens_after_threshold():
    """连续失败到阈值 → 跳闸；跳闸后**不再调用上游**（这是熔断省下的时间）。"""
    R.reset_breakers()
    breaker = R.get_breaker("unit:opens", fail_threshold=3, recovery_seconds=60)
    fn = _Flaky(fail_times=99)

    for _ in range(3):
        try:
            run(R.call_with_resilience(fn, name="t", timeout=1, retries=0,
                                       breaker_name="unit:opens"))
        except R.RetryExhaustedError:
            pass
    assert breaker.state == "open", f"连续 3 次失败后应跳闸，实际 {breaker.state}"
    calls_before = fn.calls

    try:
        run(R.call_with_resilience(fn, name="t", timeout=1, retries=0, breaker_name="unit:opens"))
        raise AssertionError("跳闸后应当直接拒绝")
    except R.BreakerOpenError:
        pass
    assert fn.calls == calls_before, "熔断打开后不应再调用上游"
    assert breaker.total_rejected >= 1


def test_circuit_breaker_half_open_then_closed():
    """冷却期后放探测流量：成功 → 合闸；失败 → 立刻重新跳闸。"""
    R.reset_breakers()
    breaker = R.get_breaker("unit:half", fail_threshold=2, recovery_seconds=0.2)
    bad = _Flaky(fail_times=99)
    for _ in range(2):
        try:
            run(R.call_with_resilience(bad, name="t", timeout=1, retries=0, breaker_name="unit:half"))
        except R.RetryExhaustedError:
            pass
    assert breaker.state == "open"

    time.sleep(0.25)   # 等过冷却期
    good = _Flaky(fail_times=0)
    assert run(R.call_with_resilience(good, name="t", timeout=1, retries=0,
                                      breaker_name="unit:half")) == "ok"
    assert breaker.state == "closed", f"半开探测成功后应合闸，实际 {breaker.state}"

    # 再次失败到阈值 → 重新跳闸
    for _ in range(2):
        try:
            run(R.call_with_resilience(bad, name="t", timeout=1, retries=0, breaker_name="unit:half"))
        except R.RetryExhaustedError:
            pass
    assert breaker.state == "open"


def test_half_open_failure_reopens_immediately():
    """半开状态下探测失败 → 立刻回到 open（不用再攒够阈值）。"""
    R.reset_breakers()
    breaker = R.get_breaker("unit:half2", fail_threshold=5, recovery_seconds=0.15)
    bad = _Flaky(fail_times=99)
    for _ in range(5):
        try:
            run(R.call_with_resilience(bad, name="t", timeout=1, retries=0, breaker_name="unit:half2"))
        except R.RetryExhaustedError:
            pass
    assert breaker.state == "open"
    time.sleep(0.2)
    assert breaker.allow() is True          # 触发 half_open
    assert breaker.state == "half_open"
    breaker.on_failure(RuntimeError("probe failed"))
    assert breaker.state == "open", "半开探测失败必须立刻重新跳闸"


def test_cancellation_is_not_a_failure():
    """用户点"停止"→ CancelledError：不算失败，也不该触发熔断。"""
    R.reset_breakers()
    breaker = R.get_breaker("unit:cancel", fail_threshold=2, recovery_seconds=60)

    async def _cancelled():
        raise asyncio.CancelledError()

    try:
        run(R.call_with_resilience(_cancelled, name="t", timeout=1, retries=2,
                                   breaker_name="unit:cancel"))
    except asyncio.CancelledError:
        pass
    assert breaker.consecutive_failures == 0, "取消不该计入失败"
    assert breaker.state == "closed"


def test_metrics_are_recorded():
    R.reset_breakers()
    before = R.stats()
    fn = _Flaky(fail_times=1)
    run(R.call_with_resilience(fn, name="t", timeout=1, retries=1, base_delay=0.01))
    after = R.stats()
    assert after["calls"] == before["calls"] + 1
    assert after["retries"] >= before["retries"] + 1
    assert after["success"] >= before["success"] + 1


def test_snapshot_shape():
    """可观测快照的字段是给 /health/resilience 用的，改坏了运维页面会空。"""
    snap = R.get_breaker("unit:snap").snapshot()
    for key in ("name", "state", "totalCalls", "totalFailures", "totalRejected",
                "totalOpened", "lastError", "recoverInSec"):
        assert key in snap, f"快照缺少字段 {key}"


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
    if failed:
        print(f"结果: {len(failed)} 个失败 ❌ -> {', '.join(failed)}")
        sys.exit(1)
    print(f"结果: 全部通过 ✅（{len(tests)} 个用例）")
