# server-py/app/routes/agent.py
# Agent 路由：流式执行任务，实时推送每一步状态
import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.middleware import rate_limiter, security_check
from app.services.agent.agent import get_tool_list, run_agent
from app.services.identity import User, current_user
from app.services.trace import start_trace
from app.utils.logger import logger
from app.utils.sse import sse_stream

router = APIRouter()


@router.post("/run", dependencies=[Depends(rate_limiter)])
async def run(body: dict, user: User = Depends(current_user)):
    task = (body.get("task") or "").strip()
    security_check(task)

    if not task:
        raise HTTPException(status_code=400, detail={"error": {"message": "任务不能为空"}})
    if len(task) > 2000:
        raise HTTPException(status_code=400, detail={"error": {"message": "任务描述过长，请简洁描述"}})

    # None=自动（默认"召回优先"），True=强制检索知识库，False=关闭知识库检索
    use_knowledge = body.get("useKnowledge")
    if use_knowledge is not None:
        use_knowledge = bool(use_knowledge)

    # 会话 id：Agent 要靠它记住"上一个任务说了什么"（摘要 + 最近 N 轮，见 chat/memory.py）
    session_id = (body.get("sessionId") or "agent-default").strip() or "agent-default"

    async def generator():
        queue: asyncio.Queue = asyncio.Queue()
        usage: dict = {}
        started = time.time()

        # 全链路追踪：必须在 create_task 之前 start —— asyncio 任务创建时会**复制**
        # 当前上下文，晚一步的话 run_agent 里的 trace_step 全都看不到这条 trace。
        trace = start_trace("agent", question=task, tenant_id=user.tenant_id,
                            user_id=user.user_id, user_name=user.name,
                            meta={"sessionId": session_id, "useKnowledge": use_knowledge})
        trace.step("request", "收到任务", detail={
            "task": task, "sessionId": session_id,
            "knowledgeMode": ("强制检索" if use_knowledge is True else
                              "关闭检索" if use_knowledge is False else "自动"),
            "identity": {"userId": user.user_id, "name": user.name,
                         "departments": user.departments, "clearance": user.clearance},
        })

        async def on_event(event_type, data):
            # 捕获 done 事件里的用量，任务结束后统一记录到看板
            if event_type == "done":
                usage.update(data or {})
            await queue.put((event_type, data))

        yield "start", {"task": task, "sessionId": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "runId": trace.run_id}

        # run_agent 内部总会以 'done' 或 'error' 事件结束
        run_task = asyncio.create_task(
            run_agent(task, on_event, user, use_knowledge, session_id=session_id))

        final_event = "done"
        try:
            while True:
                # run_agent 万一在发终点事件之前就挂了（比如它自己初始化阶段抛异常），
                # 前端会一直转圈、什么也看不到 —— 这正是"要靠全链路追踪查问题"的场景之一，
                # 所以这里主动兜底：任务结束却没有事件时，补一条 error 并收尾。
                if run_task.done() and queue.empty():
                    exc = run_task.exception() if not run_task.cancelled() else None
                    detail = f"{type(exc).__name__}: {exc}" if exc else "任务在没有发出终点事件的情况下结束"
                    trace.step("error", "任务异常结束（未发出终点事件）", status="error",
                               detail={"error": detail})
                    final_event = "error"
                    yield "error", {"message": "任务执行中断了，请重试一次（详细过程可在「全链路追踪」里查看）"}
                    break

                getter = asyncio.ensure_future(queue.get())
                done, _pending = await asyncio.wait(
                    {getter, run_task}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    # 事件还没来、但任务已经结束 → 下一轮循环会走上面的兜底分支
                    getter.cancel()
                    continue
                event_type, data = getter.result()
                yield event_type, data
                if event_type in ("done", "error"):
                    final_event = event_type
                    await run_task
                    from app.routes.monitor import record_api_call
                    record_api_call(
                        feature="agent",
                        input_tokens=usage.get("inputTokens", 0),
                        output_tokens=usage.get("outputTokens", 0),
                        latency_ms=round((time.time() - started) * 1000),
                        from_cache=False,
                    )
                    break
        finally:
            await trace.finish(status="ok" if final_event == "done" else "error",
                               summary={"toolCalls": usage.get("steps"),
                                        "modelSteps": usage.get("modelSteps"),
                                        "route": usage.get("route"),
                                        "inputTokens": usage.get("inputTokens"),
                                        "outputTokens": usage.get("outputTokens"),
                                        "answerChars": usage.get("contentLength")})

    return sse_stream(generator)


@router.get("/tools")
async def tools():
    return {"tools": get_tool_list()}


@router.get("/examples")
async def examples():
    return {
        "examples": [
            # 排第一：一次任务同时用到「计算 + 联网搜索 + 生成报告」三个工具，
            # 用来快速验证多工具协作（数猫要算数、猫吃什么要联网、最后要出报告）
            {"title": "养猫多工具测试", "icon": "🐱",
             "task": "我很喜欢猫。我家有3只猫，现在生了7个小猫。今天早上朋友问我你家有几只猫我不知道怎么说，"
                     "后面我送给朋友3只，现在家里有几只也不知道。我不知道猫喜欢吃什么所以想上网查一下，"
                     "但是我想给我的猫做一个养猫计划报告。"},
            {"title": "技术调研", "task": "对比 Vue3 和 React 2024年的最新状态，分别查询它们的最新版本和主要特性，生成一份技术选型报告", "icon": "🔍"},
            {"title": "费用计算", "task": "我出差3天，酒店每晚580元，机票往返1200元，餐费每天150元，帮我计算总报销金额，并查询一下公司差旅报销标准", "icon": "💰"},
            {"title": "工期计算", "task": "项目计划从2024年3月1日开始，需要45个工作日完成，帮我计算预计完成日期，并生成一份项目时间轴摘要", "icon": "📅"},
            {"title": "知识查询", "task": "从知识库查询公司的年假政策，计算一下我今年还剩多少年假（假设今年已用6天，总共15天），并发送结果通知给HR", "icon": "📚"},
        ],
    }
