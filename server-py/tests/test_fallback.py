# server-py/tests/test_fallback.py
"""故障注入测试：主模型不可用时，真的会降级到备用模型吗？

（本文件只保留**黄金用例**：核心路径 + 真实踩过的边界，每条都能讲清「防的是什么坑」；零碎用例已精简。）

运行方式（容器内，需要网络与 ZHIPU_API_KEY）：
    docker exec workmind-server python /app/tests/test_fallback.py

为什么单独一个文件：单元测试（test_resilience.py）用假函数覆盖状态机，
这里要打**真实上游**，验证的是"配置对不对"——
备用模型的 base_url / 模型名写错时，单元测试会全绿，线上降级却依然失败。

注入方式：把主模型的 base_url 指到一个**必然连不上**的地址（127.0.0.1:9），
其余参数与线上一致。这样不依赖"能不能真把 DeepSeek 弄挂"，结果可重复。

⚠️ 所有用例跑在**同一个事件循环**里（最后统一 asyncio.run）：
   openai 的 httpx 客户端会绑定到"首次使用它的那个事件循环"，
   每个用例各起一个 loop 的话，第二个用例就会报 RuntimeError: Event loop is closed。
   线上是一个常驻 loop，所以这种隔离方式也更贴近真实运行环境。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.environ.get("APP_DIR", "/app"))

from langchain_openai import ChatOpenAI  # noqa: E402

from app.core.config import config  # noqa: E402
from app.models import llm as M  # noqa: E402
from app.infra import resilience as R  # noqa: E402


def _broken_primary(temperature=0):
    """一个必然失败的主模型：端口 9（discard）没有任何服务在听。"""
    return ChatOpenAI(model="deepseek-chat", api_key="sk-broken",
                      base_url="http://127.0.0.1:9/v1", temperature=temperature,
                      timeout=2, max_retries=0)


def _real_fallback(temperature=0):
    if not M.FALLBACK_API_KEY:
        return None
    return ChatOpenAI(model=M.FALLBACK_MODEL, api_key=M.FALLBACK_API_KEY,
                      base_url=M.FALLBACK_BASE_URL, temperature=temperature,
                      timeout=M.LLM_TIMEOUT - 2, max_retries=0)


def _resilient(primary, fallback, label, timeout=12.0):
    """构造被测模型。超时给 12s：**真实 LLM 调用 2 秒是不够的** ——
    一开始为了"跑得快"设成 2s，结果降级调用被自己的超时掐断，
    报出来还是一个空消息的错误（asyncio 超时的 str() 为空），排查了半天。
    只有"主模型必然连不上"的用例才把超时压到 2s（连接被拒是立刻返回的）。"""
    return M.ResilientModel(
        primary=primary, fallback=fallback,
        primary_label=label, fallback_label=M.FALLBACK_MODEL,
        timeout_seconds=timeout, max_retries=0, model_name="deepseek-chat",
    )


def test_fallback_model_is_configured():
    """先确认配置在：没有备用模型的话，"降级"就只是一个词。"""
    print(f"   主模型   : {config.ai.primary_model} @ {config.ai.base_url}")
    print(f"   备用模型 : {M.FALLBACK_MODEL} @ {M.FALLBACK_BASE_URL}"
          f"（enabled={M.FALLBACK_ENABLED}）")
    assert M.FALLBACK_ENABLED, "未启用备用模型：检查 ZHIPU_API_KEY / MODEL_FALLBACK"
    assert M.FALLBACK_API_KEY, "备用模型没有 API Key"


async def test_fallback_model_actually_answers():
    """备用模型本身可用（base_url / 模型名没写错）—— 配错了这里立刻暴露。"""
    msg = await _real_fallback().ainvoke([{"role": "user", "content": "只回复两个字：收到"}])
    assert msg.content, "备用模型返回了空内容"
    print(f"   备用模型直连回复: {msg.content[:40]!r}")


async def test_primary_down_falls_back():
    """主模型连不上 → 自动降级到备用模型，用户仍然拿到回答。"""
    R.reset_breakers()
    m = _resilient(_broken_primary(), _real_fallback(), "deepseek-broken")
    t0 = time.time()
    msg = await m.ainvoke([{"role": "user", "content": "用一句话说明什么是熔断"}])
    elapsed = time.time() - t0
    assert msg.content, "降级后仍然没有回答"
    print(f"   降级成功，耗时 {elapsed:.2f}s，回答开头: {msg.content[:60]!r}")
    assert R.stats()["fallbacks"] >= 1, "降级计数没有增长"


async def test_stream_falls_back_before_first_token():
    """流式：还没吐出任何 token 时失败 → 允许降级（吐过就不能换，否则内容会串）。

    这条同时守着 _astream 的**返回类型**：子模型 astream 产出 AIMessageChunk，
    而 BaseChatModel 要求 ChatGenerationChunk。类型错了非流式测试全绿，
    只有这条会炸 —— 实测真的这么炸过一次（整个对话流式全挂）。
    """
    R.reset_breakers()
    m = _resilient(_broken_primary(), _real_fallback(), "deepseek-broken2")
    out = ""
    async for chunk in m.astream([{"role": "user", "content": "只回复两个字：收到"}]):
        out += chunk.content or ""
    assert out.strip(), "流式降级后没有拿到内容"
    print(f"   流式降级成功，收到 {len(out)} 字: {out[:40]!r}")


async def test_stream_normal_path_works():
    """正常路径的流式也必须通（回归：曾因 chunk 类型错误整个流式全挂）。"""
    R.reset_breakers()
    m = _resilient(_real_fallback(), None, M.FALLBACK_MODEL)
    out = ""
    async for chunk in m.astream([{"role": "user", "content": "只回复两个字：收到"}]):
        out += chunk.content or ""
    assert out.strip(), "正常流式没有拿到内容"
    print(f"   正常流式 OK，收到 {len(out)} 字: {out[:40]!r}")


async def test_breaker_short_circuits_after_repeated_failures():
    """连续失败到阈值 → 熔断打开 → 后续请求**不再等超时**（这是熔断省下的时间）。"""
    R.reset_breakers()
    m = _resilient(_broken_primary(), None, "deepseek-broken3", timeout=2.0)  # 不给备胎，看跳闸
    threshold = R.BREAKER_FAIL_THRESHOLD
    for _ in range(threshold):
        try:
            await m.ainvoke([{"role": "user", "content": "hi"}])
        except Exception:  # noqa: BLE001 - 预期失败
            pass
    breaker = R.get_breaker("model:deepseek-broken3")
    assert breaker.state == "open", f"连续 {threshold} 次失败后应跳闸，实际 {breaker.state}"

    t0 = time.time()
    try:
        await m.ainvoke([{"role": "user", "content": "hi"}])
    except Exception:  # noqa: BLE001
        pass
    fast = time.time() - t0
    print(f"   跳闸后响应耗时 {fast * 1000:.0f}ms（跳闸前每次要等满超时 2.0s）")
    assert fast < 1.0, f"跳闸后仍然很慢（{fast:.2f}s），说明还是打到了上游"
    assert breaker.total_rejected >= 1


SYNC_TESTS = [test_fallback_model_is_configured]
ASYNC_TESTS = [test_fallback_model_actually_answers, test_primary_down_falls_back,
               test_stream_falls_back_before_first_token, test_stream_normal_path_works,
               test_breaker_short_circuits_after_repeated_failures]


async def _main():
    failed = []
    print()
    for t in SYNC_TESTS:
        print(f"  RUN   {t.__name__}")
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as err:
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__} -> {err}")
        except Exception as err:  # noqa: BLE001
            failed.append(t.__name__)
            print(f"  ERROR {t.__name__} -> {type(err).__name__}: {err}")

    for t in ASYNC_TESTS:
        print(f"  RUN   {t.__name__}")
        try:
            await t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as err:
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__} -> {err}")
        except Exception as err:  # noqa: BLE001
            failed.append(t.__name__)
            print(f"  ERROR {t.__name__} -> {type(err).__name__}: {err}")

    total = len(SYNC_TESTS) + len(ASYNC_TESTS)
    print()
    if failed:
        print(f"结果: {len(failed)} 个失败 ❌ -> {', '.join(failed)}")
        return 1
    print(f"结果: 全部通过 ✅（{total} 个用例，含真实上游调用）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
