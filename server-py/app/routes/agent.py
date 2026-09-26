# server-py/app/routes/agent.py
# Agent 路由：流式执行任务，实时推送每一步状态
import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.middleware import rate_limiter, security_check
from app.services.agent.agent import get_tool_list, run_agent
from app.services.identity import User, current_user
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

        async def on_event(event_type, data):
            # 捕获 done 事件里的用量，任务结束后统一记录到看板
            if event_type == "done":
                usage.update(data or {})
            await queue.put((event_type, data))

        yield "start", {"task": task, "sessionId": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # run_agent 内部总会以 'done' 或 'error' 事件结束
        run_task = asyncio.create_task(
            run_agent(task, on_event, user, use_knowledge, session_id=session_id))

        while True:
            event_type, data = await queue.get()
            yield event_type, data
            if event_type in ("done", "error"):
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

    return sse_stream(generator)


@router.get("/tools")
async def tools():
    return {"tools": get_tool_list()}


@router.get("/examples")
async def examples():
    return {
        "examples": [
            {"title": "技术调研", "task": "对比 Vue3 和 React 2024年的最新状态，分别查询它们的最新版本和主要特性，生成一份技术选型报告", "icon": "🔍"},
            {"title": "费用计算", "task": "我出差3天，酒店每晚580元，机票往返1200元，餐费每天150元，帮我计算总报销金额，并查询一下公司差旅报销标准", "icon": "💰"},
            {"title": "工期计算", "task": "项目计划从2024年3月1日开始，需要45个工作日完成，帮我计算预计完成日期，并生成一份项目时间轴摘要", "icon": "📅"},
            {"title": "知识查询", "task": "从知识库查询公司的年假政策，计算一下我今年还剩多少年假（假设今年已用6天，总共15天），并发送结果通知给HR", "icon": "📚"},
        ],
    }
