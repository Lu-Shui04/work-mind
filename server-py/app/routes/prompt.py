# server-py/app/routes/prompt.py
# Prompt 调试路由：单次测试(流式) + A/B对比 + 模板管理
import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException

from app.middleware import rate_limiter
from app.routes.monitor import record_api_call
from app.services import pricing
from app.services.model import create_chat_model, primary_model_name
from app.services.trace import start_trace
from app.services.prompt.prompt_service import (
    delete_template, get_template, list_templates, save_template, score_ab_test,
)
from app.utils.logger import logger
from app.utils.sse import sse_stream
from app.utils.tokens import cache_read_of

router = APIRouter()


@router.post("/test/stream", dependencies=[Depends(rate_limiter)])
async def test_stream(body: dict):
    system_prompt = body.get("systemPrompt") or ""
    user_message = (body.get("userMessage") or "").strip()
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("maxTokens", 1000)

    if not user_message:
        raise HTTPException(status_code=400, detail={"error": {"message": "测试消息不能为空"}})

    async def generator():
        # 全链路追踪：Prompt 单测也是一次"提问 → 模型 → 回答"，同样留档
        # （对比不同 prompt 的效果时，trace 里能直接看到当时用的确切 system prompt）
        trace = start_trace("prompt", question=user_message, meta={"kind": "Prompt 单测"})
        trace.step("request", "Prompt 单测", detail={
            "systemPrompt": system_prompt, "userMessage": user_message,
            "temperature": temperature, "maxTokens": max_tokens,
        })

        test_model = create_chat_model(temperature=temperature, streaming=True)

        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": body.get("userMessage")})

        yield "start", {"temperature": temperature, "maxTokens": max_tokens,
                        "runId": trace.run_id}

        full_reply = ""
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        start_ms = time.time() * 1000

        async for chunk in test_model.astream(messages, max_tokens=max_tokens):
            if chunk.content:
                full_reply += chunk.content
                yield "token", {"token": chunk.content}
            if getattr(chunk, "usage_metadata", None):
                input_tokens = chunk.usage_metadata.get("input_tokens", 0)
                output_tokens = chunk.usage_metadata.get("output_tokens", 0)
                cached_tokens = cache_read_of(chunk.usage_metadata)

        latency_ms = round(time.time() * 1000 - start_ms)

        record_api_call(feature="prompt", input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_input_tokens=cached_tokens, model=primary_model_name(),
                        latency_ms=latency_ms, from_cache=False)

        trace.step("llm", "模型生成", duration_ms=latency_ms, detail={
            "model": getattr(test_model, "model_name", "") or "chat_model",
            "inputTokens": input_tokens, "outputTokens": output_tokens,
            "reply": full_reply,
        })
        trace.step("response", "返回生成结果", detail={
            "inputTokens": input_tokens, "outputTokens": output_tokens,
            "replyChars": len(full_reply),
        })
        await trace.finish(summary={"inputTokens": input_tokens, "outputTokens": output_tokens,
                                    "replyChars": len(full_reply)})

        yield "done", {
            "latencyMs": latency_ms,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
            # 费用按官方现价实算（模型 × 缓存命中 × 峰谷），不再写死 2024 年的美元单价
            "costCNY": pricing.cost_cny(primary_model_name(), input_tokens, output_tokens,
                                        cached_tokens),
            "runId": trace.run_id,
        }

        logger.info("prompt test done", {"latencyMs": latency_ms, "inputTokens": input_tokens, "outputTokens": output_tokens})

    return sse_stream(generator)


@router.post("/ab-test/stream", dependencies=[Depends(rate_limiter)])
async def ab_test_stream(body: dict):
    """A/B 对比（流式）。

    为什么要改成流式：这个接口要跑 **2 次生成 + 3 次评分共 5 次模型调用**，
    是最慢的一个入口（十几秒到几十秒）。以前是等全部跑完再一次性返回，
    用户面对的就是一个"测试中..."转圈，完全不知道进行到哪了 ——
    实测同样的毛病：单次测试是流式的，A/B 不是。

    事件：token{variant} → variant_done{variant} → scoring → done{answerA,answerB,evaluation}
    两个变体**并行**生成，token 用 variant 区分，前端各追加到自己那一列。
    """
    question = (body.get("question") or "").strip()
    system_prompt_a = body.get("systemPromptA")
    system_prompt_b = body.get("systemPromptB")
    temperature = body.get("temperature", 0)
    max_tokens = body.get("maxTokens", 800)

    if not question:
        raise HTTPException(status_code=400, detail={"error": {"message": "测试问题不能为空"}})

    async def generator():
        queue: asyncio.Queue = asyncio.Queue()
        answers = {"a": "", "b": ""}
        # 两个变体各自的真实 token：A/B 一次要跑 5 次模型调用，不记的话看板上
        # "Prompt 调试"就只有调用次数、费用永远是 ¥0（实测就是这样）
        variant_usage = {"a": (0, 0, 0), "b": (0, 0, 0)}   # (输入, 输出, 缓存命中输入)
        started = time.time()

        # 全链路追踪：A/B 一次跑 5 次模型调用（2 次生成 + 3 次评分），
        # 出问题时最需要看的就是"两个 prompt 各自是什么、评分依据是什么"
        trace = start_trace("prompt", question=question, meta={"kind": "A/B 对比"})
        trace.step("request", "A/B 对比", detail={
            "question": question, "systemPromptA": system_prompt_a,
            "systemPromptB": system_prompt_b, "temperature": temperature,
            "maxTokens": max_tokens,
        })

        async def run_variant(key: str, system_prompt: str | None):
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": question})
            model = create_chat_model(temperature=temperature, streaming=True)
            try:
                async for chunk in model.astream(messages, max_tokens=max_tokens):
                    text = chunk.content or ""
                    if text:
                        answers[key] += text
                        await queue.put(("token", {"variant": key, "token": text}))
                    usage = getattr(chunk, "usage_metadata", None)
                    if usage:
                        variant_usage[key] = (usage.get("input_tokens", 0) or 0,
                                              usage.get("output_tokens", 0) or 0,
                                              cache_read_of(usage))
            except Exception as err:  # noqa: BLE001 - 单个变体失败不该拖垮整次对比
                logger.error("ab test variant failed", {"variant": key, "error": str(err)})
                await queue.put(("variant_error", {"variant": key, "message": str(err)[:200]}))
            finally:
                await queue.put(("variant_done", {"variant": key}))

        yield "start", {"question": question, "runId": trace.run_id}
        tasks = [asyncio.create_task(run_variant("a", system_prompt_a)),
                 asyncio.create_task(run_variant("b", system_prompt_b))]

        finished = 0
        while finished < 2:
            event_type, data = await queue.get()
            yield event_type, data
            if event_type == "variant_done":
                finished += 1
        await asyncio.gather(*tasks, return_exceptions=True)

        trace.step("llm", "两个变体生成完成", duration_ms=round((time.time() - started) * 1000),
                   detail={"tokensA": variant_usage["a"], "tokensB": variant_usage["b"],
                           "answerA": answers["a"], "answerB": answers["b"]})

        # 两个变体都出完了才开始评分（评分本身要读完整答案）
        yield "scoring", {}
        score_usage: dict = {}
        _score_t0 = time.time()
        try:
            evaluation = await score_ab_test(question, answers["a"], answers["b"],
                                             usage_out=score_usage)
        except Exception as err:  # noqa: BLE001
            logger.error("ab test scoring failed", {"error": str(err)})
            evaluation = None
        trace.step("llm", "AI 评分（3 次调用）",
                   status="ok" if evaluation is not None else "error",
                   duration_ms=round((time.time() - _score_t0) * 1000),
                   detail={"usage": score_usage, "evaluation": evaluation,
                           "error": None if evaluation is not None else "评分失败（详情见服务端日志）"})

        # A/B = 2 次生成 + 3 次评分的真实用量，合成一条记录（一次用户操作 = 一行）
        input_tokens = sum(v[0] for v in variant_usage.values()) + int(score_usage.get("input_tokens", 0))
        output_tokens = sum(v[1] for v in variant_usage.values()) + int(score_usage.get("output_tokens", 0))
        cached_tokens = (sum(v[2] for v in variant_usage.values())
                         + int(score_usage.get("cached_input_tokens", 0)))
        record_api_call(feature="prompt", input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_input_tokens=cached_tokens, model=primary_model_name(),
                        latency_ms=round((time.time() - started) * 1000), from_cache=False,
                        estimated=(input_tokens == 0 and output_tokens == 0))
        logger.info("ab test done", {"latencyMs": round((time.time() - started) * 1000)})

        trace.step("response", "返回对比结果", detail={
            "winner": (evaluation or {}).get("winner"), "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        })
        await trace.finish(summary={"inputTokens": input_tokens, "outputTokens": output_tokens,
                                    "winner": (evaluation or {}).get("winner")})

        yield "done", {"answerA": answers["a"], "answerB": answers["b"],
                       "evaluation": evaluation, "runId": trace.run_id}

    return sse_stream(generator)


# ── CRUD：模板管理 ────────────────────────────────────────────

@router.get("/templates")
async def templates():
    return {"templates": list_templates()}


@router.get("/templates/{template_id}")
async def get_one_template(template_id: str):
    t = get_template(template_id)
    if not t:
        raise HTTPException(status_code=404, detail={"error": {"message": "模板不存在"}})
    return t


@router.post("/templates")
async def create_template(body: dict):
    name = (body.get("name") or "").strip()
    system_prompt = (body.get("systemPrompt") or "").strip()
    if not name or not system_prompt:
        raise HTTPException(status_code=400, detail={"error": {"message": "模板名称和内容不能为空"}})
    template = save_template(name, system_prompt, body.get("description", ""), body.get("tags", []))
    return {"success": True, "template": template}


@router.put("/templates/{template_id}")
async def update_template(template_id: str, body: dict):
    template = save_template(
        body.get("name"), body.get("systemPrompt"), body.get("description", ""), body.get("tags", []),
        existing_id=template_id,
    )
    return {"success": True, "template": template}


@router.delete("/templates/{template_id}")
async def remove_template(template_id: str):
    try:
        delete_template(template_id)
        return {"success": True}
    except ValueError as err:
        raise HTTPException(status_code=400, detail={"error": {"message": str(err)}})
