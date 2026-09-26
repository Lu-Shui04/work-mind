# server-py/app/routes/workflow.py
# 工作流路由：启动/查询/推进/获取结果
import random
import time

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.callbacks import UsageMetadataCallbackHandler

from app.middleware import rate_limiter
from app.services.model import primary_model_name
from app.services.workflow.workflows import WORKFLOW_BUILDERS, WORKFLOW_META
from app.utils.logger import logger
from app.services.trace import start_trace, trace_step
from app.utils.sse import sse_stream
from app.utils.tokens import sum_usage

router = APIRouter()

# 存活的工作流实例：threadId → { graph, meta, config }
_active_workflows: dict[str, dict] = {}


@router.get("/templates")
async def templates():
    return {"templates": list(WORKFLOW_META.values())}


async def _translate_graph_events(graph, graph_input, config, meta):
    """把 LangGraph 的事件翻译成前端要的 SSE 事件：节点状态 + 流式 token。

    start 与 resume **共用这一段**。以前是两份实现，而且不一致：
    resume 里转发了 token，start 里没转发；再叠加"模型没开流式"，
    结果两条路径都看不到流式输出（用户看到的是最后一整段蹦出来）。

    token 事件带上 nodeId 和 isResult：
      - isResult=True  → 前端把它当"正文"追加到结果面板
      - 否则           → 贴在左侧流程图对应的节点卡片上，流程实时可见
    """
    result_node = meta.get("resultNode")
    current_node = None
    last_node = None

    async for event in graph.astream_events(graph_input, config, version="v2"):
        event_type = event["event"]
        name = event["name"]
        data = event.get("data") or {}

        if event_type == "on_chain_start" and name not in ("__start__", "LangGraph"):
            node = next((n for n in meta["nodes"] if n["id"] == name), None)
            if node and name != last_node:
                last_node = current_node = name
                # 全链路追踪：每个节点 = 一次模型调用，进出都留痕
                trace_step("node", "节点开始：" + node["label"])
                yield "node_start", {"nodeId": name, "label": node["label"]}

        elif event_type == "on_chat_model_stream":
            chunk = data.get("chunk")
            content = getattr(chunk, "content", None) if chunk else None
            if content and current_node:
                yield "token", {"nodeId": current_node, "token": content,
                                "isResult": current_node == result_node}

        elif event_type == "on_chain_end" and name not in ("__end__", "LangGraph"):
            node = next((n for n in meta["nodes"] if n["id"] == name), None)
            if node:
                output = data.get("output")
                preview = ""
                if isinstance(output, dict) and output:
                    first_val = next(iter(output.values()))
                    if isinstance(first_val, str) and first_val:
                        preview = first_val[:80] + ("..." if len(first_val) > 80 else "")
                trace_step("node", "节点完成：" + node["label"], detail={
                    "nodeId": name, "preview": preview,
                    "outputKeys": list(output.keys()) if isinstance(output, dict) else None,
                })
                yield "node_done", {"nodeId": name, "preview": preview}


@router.post("/start/stream", dependencies=[Depends(rate_limiter)])
async def start_stream(body: dict):
    workflow_id = body.get("workflowId")
    input_data = body.get("input") or {}

    if not workflow_id or workflow_id not in WORKFLOW_BUILDERS:
        raise HTTPException(status_code=400, detail={"error": {"message": f"未知工作流：{workflow_id}"}})

    rand = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
    thread_id = f"wf_{int(time.time() * 1000)}_{rand}"
    config = {"configurable": {"thread_id": thread_id}}

    async def generator():
        builder = WORKFLOW_BUILDERS[workflow_id]
        graph = builder()
        meta = WORKFLOW_META[workflow_id]

        _active_workflows[thread_id] = {"graph": graph, "meta": meta, "config": config}

        _wf_started = time.time()
        # 全链路追踪：工作流一次跑好几个节点，出问题时要能看出"卡/错在哪个节点"
        trace = start_trace("workflow", question=meta.get("title") or workflow_id,
                            meta={"workflowId": workflow_id, "threadId": thread_id})
        trace.step("request", "启动工作流：" + (meta.get("title") or workflow_id), detail={
            "workflowId": workflow_id, "threadId": thread_id, "input": input_data,
            "nodes": [n["id"] for n in meta.get("nodes", [])],
        })
        yield "start", {"threadId": thread_id, "workflowId": workflow_id,
                        "resultNode": meta.get("resultNode"), "runId": trace.run_id}
        logger.info("workflow: started", {"workflowId": workflow_id, "threadId": thread_id})

        # 用量记账：工作流的每个节点都是一次模型调用，用量挂在回调处理器上收集，
        # 一次 run 汇总成一条记录（以前这里只记了延迟，token 是 0，费用永远是 ¥0）
        handler = UsageMetadataCallbackHandler()
        async for ev in _translate_graph_events(graph, input_data,
                                                {**config, "callbacks": [handler]}, meta):
            yield ev

        from app.routes.monitor import record_api_call
        input_tokens, output_tokens, cached_tokens = sum_usage(handler.usage_metadata)
        record_api_call(feature="workflow", input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_input_tokens=cached_tokens, model=primary_model_name(),
                        latency_ms=round((time.time() - _wf_started) * 1000))
        state = await graph.aget_state(config)

        if state.next:
            trace.step("response", "工作流暂停（等待人工审核）", detail={
                "nextNode": state.next[0], "inputTokens": input_tokens,
                "outputTokens": output_tokens,
            })
            await trace.finish(summary={"status": "paused", "nextNode": state.next[0],
                                        "inputTokens": input_tokens, "outputTokens": output_tokens})
            yield "paused", {
                "threadId": thread_id,
                "nextNode": state.next[0],
                "intermediates": _get_intermediates(state.values, workflow_id),
                "runId": trace.run_id,
            }
        else:
            result = state.values.get(meta["resultKey"], "")
            trace.step("response", "工作流完成", detail={
                "inputTokens": input_tokens, "outputTokens": output_tokens,
                "resultChars": len(result or ""), "result": result,
            })
            await trace.finish(summary={"status": "completed", "inputTokens": input_tokens,
                                        "outputTokens": output_tokens})
            yield "completed", {"threadId": thread_id, "result": result,
                                "runId": trace.run_id}

    return sse_stream(generator)


@router.post("/resume/stream", dependencies=[Depends(rate_limiter)])
async def resume_stream(body: dict):
    thread_id = body.get("threadId")
    feedback = body.get("feedback")

    wf = _active_workflows.get(thread_id)
    if not wf:
        raise HTTPException(status_code=404, detail={"error": {"message": "工作流不存在或已过期，请重新启动"}})

    graph, meta, config = wf["graph"], wf["meta"], wf["config"]

    async def generator():
        if feedback and feedback.strip():
            await graph.aupdate_state(config, {"humanFeedback": feedback})

        logger.info("workflow: resumed", {"threadId": thread_id, "hasFeedback": bool(feedback)})
        # 人工审核后继续跑：单独起一条 trace（这也是一次独立的请求）
        trace = start_trace("workflow", question=(meta.get("title") or "") + "（人工补充后继续）",
                            meta={"workflowId": meta.get("id"), "threadId": thread_id,
                                  "resume": True})
        trace.step("request", "工作流继续执行", detail={
            "threadId": thread_id, "feedback": feedback,
            "nextNode": None,
        })
        yield "resumed", {"threadId": thread_id, "resultNode": meta.get("resultNode"),
                          "runId": trace.run_id}

        # 继续跑的这一段同样要记账：它是真正花钱的部分（剩下的几个节点都在这里跑完）
        started = time.time()
        handler = UsageMetadataCallbackHandler()
        async for ev in _translate_graph_events(graph, None,
                                                {**config, "callbacks": [handler]}, meta):
            yield ev

        final_state = await graph.aget_state(config)
        result = final_state.values.get(meta["resultKey"], "")

        from app.routes.monitor import record_api_call
        input_tokens, output_tokens, cached_tokens = sum_usage(handler.usage_metadata)
        record_api_call(feature="workflow", input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_input_tokens=cached_tokens, model=primary_model_name(),
                        latency_ms=round((time.time() - started) * 1000))

        trace.step("response", "工作流完成", detail={
            "inputTokens": input_tokens, "outputTokens": output_tokens,
            "resultChars": len(result or ""), "result": result,
        })
        await trace.finish(summary={"status": "completed", "inputTokens": input_tokens,
                                    "outputTokens": output_tokens})

        yield "completed", {"threadId": thread_id, "result": result, "runId": trace.run_id}

        _active_workflows.pop(thread_id, None)
        logger.info("workflow: completed", {"threadId": thread_id})

    return sse_stream(generator)


def _get_intermediates(values: dict, workflow_id: str) -> list[dict]:
    maps = {
        "weekly_report": {"highlights": "提炼的亮点", "risks": "风险/阻塞项"},
        "meeting_minutes": {"attendees": "参会人与议题", "conclusions": "会议结论", "actionItems": "Action Items"},
        "email_polish": {"purpose": "意图分析", "issues": "发现的问题"},
        "prd_skeleton": {"features": "功能点", "constraints": "约束条件"},
    }
    field_map = maps.get(workflow_id, {})
    return [{"key": k, "label": label, "value": values[k]} for k, label in field_map.items() if values.get(k)]
