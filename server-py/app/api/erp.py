# server-py/app/api/erp.py
# ERP 路由：智能填单（自然语言→结构化表单）+ Multi-Agent 审批（结构化裁决，必要时打断人工）
#
# 审批流程（可解释、可打断、可补料）：
#   submit/stream   创建申请 → 规划审批链 → 逐节点评审（approved/rejected/need_info）
#   resume/stream   当某节点 need_info 时流程暂停 → 人工补充说明（并可修改申请数据）→ 继续评审
# 关键约束：**need_info 绝不算通过**；同一节点最多追问 2 轮，之后必须给出明确结论。
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.core.middleware import rate_limiter
from app.services.erp.approval import (
    APPROVAL_ROLES, plan_approval_flow, plan_reasons, review_application,
)
from app.services.erp.parser import check_compliance, parse_expense_form, parse_leave_form
from app.infra.trace import start_trace
from app.core.logger import logger
from app.core.sse import sse_stream

router = APIRouter()

# 申请记录（生产换数据库；结构已按可落库设计：chain 是子表、messages 是明细表）
_applications: dict[str, dict] = {}

MAX_ASK_ROUNDS = 1          # 同一节点最多追问 1 轮：人补完材料后必须给出明确结论，不许来回打转
_EXPENSE_PATCH_KEYS = {"type", "reason", "dept", "items", "totalAmount"}
_LEAVE_PATCH_KEYS = {"type", "startDate", "endDate", "days", "workdays", "reason", "emergencyContact"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_app(app_id: str) -> dict:
    app = _applications.get(app_id)
    if not app:
        raise HTTPException(status_code=404, detail={"error": {"message": "申请不存在"}})
    return app


def _build_chain(form_data: dict, form_type: str) -> list[dict]:
    chain = []
    for idx, role_id in enumerate(plan_approval_flow(form_data, form_type)):
        chain.append({
            "stepId": f"s{idx + 1}",
            "order": idx + 1,
            "roleId": role_id,
            "role": APPROVAL_ROLES[role_id],
            "status": "pending",     # pending → reviewing → approved / rejected / waiting_info
            "decision": None,        # approve / reject / need_info
            "reason": "",
            "checklist": [],
            "questions": [],
            "messages": [],
            "askRounds": 0,
            "durationMs": None,
            "reviewedAt": None,
        })
    return chain


def _find_step(app: dict, step_id: str) -> dict:
    for s in app["chain"]:
        if s["stepId"] == step_id:
            return s
    raise HTTPException(status_code=404, detail={"error": {"message": f"审批节点不存在：{step_id}"}})


def _current_step(app: dict) -> dict | None:
    for s in app["chain"]:
        if s["status"] not in ("approved",):
            return s
    return None


def _prior_messages(app: dict, upto_step: dict) -> list[dict]:
    out = []
    for s in app["chain"]:
        if s["order"] > upto_step["order"]:
            break
        out.extend(s["messages"])
    return out


def _apply_form_patch(app: dict, patch: dict | None):
    """人工补料时可以顺手修改申请数据；只接受白名单字段，并重算派生字段。"""
    if not patch:
        return
    allowed = _EXPENSE_PATCH_KEYS if app["formType"] == "expense" else _LEAVE_PATCH_KEYS
    for k, v in patch.items():
        if k in allowed:
            app["formData"][k] = v

    # 派生字段一律服务端重算，不信任前端（金额合计/工作日天数算错会直接影响审批结论）
    if app["formType"] == "expense":
        items = app["formData"].get("items") or []
        try:
            app["formData"]["totalAmount"] = round(sum(float(i.get("amount") or 0) for i in items), 2)
        except (TypeError, ValueError):
            pass
        app["formData"]["warnings"] = check_compliance(app["formData"])
    else:
        start, end = app["formData"].get("startDate"), app["formData"].get("endDate")
        if start and end:
            try:
                from app.services.erp.parser import _count_workdays
                app["formData"]["workdays"] = _count_workdays(start, end)
                d1 = datetime.fromisoformat(start)
                d2 = datetime.fromisoformat(end)
                app["formData"]["days"] = (d2 - d1).days + 1
            except (ValueError, TypeError):
                pass
    logger.info("erp: form patched by human", {"appId": app["id"], "keys": list(patch.keys())})


async def _run_chain(app: dict, on_event, start_index: int = 0) -> dict:
    """从 start_index 开始逐节点评审，遇到 need_info 就暂停并返回。"""
    for step in app["chain"][start_index:]:
        role = step["role"]
        step["status"] = "reviewing"
        await on_event("approver_start", {"roleId": step["roleId"], "role": role, "order": step["order"]})

        result = await review_application(
            step["roleId"], app["formData"], app["formType"],
            _prior_messages(app, step), app["answers"],
            force_decision=step["askRounds"] >= MAX_ASK_ROUNDS,
        )

        step["decision"] = result["decision"]
        step["reason"] = result["reason"]
        step["checklist"] = result["checklist"]
        step["questions"] = result["questions"]
        step["durationMs"] = result["durationMs"]
        step["reviewedAt"] = _now_iso()

        # 1) 需要补料：先抛问题气泡，再抛结论气泡（need_info），然后暂停
        if result["questions"]:
            step["askRounds"] += 1
            q_msg = {"from": step["roleId"], "role": role, "type": "question",
                     "content": "；".join(result["questions"]), "at": _now_iso()}
            step["messages"].append(q_msg)
            app["messages"].append(q_msg)
            await on_event("message", q_msg)

        d_msg = {"from": step["roleId"], "role": role, "type": "decision",
                 "content": result["reason"], "decision": result["decision"],
                 "checklist": result["checklist"], "at": _now_iso()}
        step["messages"].append(d_msg)
        app["messages"].append(d_msg)
        await on_event("message", d_msg)

        if result["decision"] == "need_info":
            step["status"] = "waiting_info"
            app["status"] = "waiting_info"
            app["pendingStepId"] = step["stepId"]
            app["updatedAt"] = _now_iso()
            await on_event("need_info", {
                "appId": app["id"], "stepId": step["stepId"], "roleId": step["roleId"], "role": role,
                "questions": result["questions"], "reason": result["reason"],
                "checklist": result["checklist"], "askRounds": step["askRounds"],
                "formData": app["formData"],
            })
            logger.info("erp: flow paused for human input", {"appId": app["id"], "stepId": step["stepId"]})
            return {"status": "waiting_info", "stepId": step["stepId"]}

        if result["decision"] == "reject":
            step["status"] = "rejected"
            app["status"] = "rejected"
            app["pendingStepId"] = None
            final = {
                "approved": False, "status": "rejected",
                "comment": f"被{role['name']}驳回：{result['reason']}",
                "rejectedAtStep": step["stepId"], "rejectedBy": role["name"],
                "reason": result["reason"], "checklist": result["checklist"],
                "approvedBy": [], "completedAt": _now_iso(),
            }
            app["result"] = final
            app["updatedAt"] = _now_iso()
            await on_event("approver_done", {"roleId": step["roleId"], "role": role,
                                             "decision": "reject", "reason": result["reason"],
                                             "checklist": result["checklist"]})
            await on_event("final", final)
            return {"status": "rejected"}

        # 2) 通过
        step["status"] = "approved"
        await on_event("approver_done", {"roleId": step["roleId"], "role": role,
                                         "decision": "approve", "reason": result["reason"],
                                         "checklist": result["checklist"]})

    app["status"] = "approved"
    app["pendingStepId"] = None
    final = {
        "approved": True, "status": "approved",
        "comment": app["chain"][-1]["reason"] if app["chain"] else "",
        "approvedBy": [s["role"]["name"] for s in app["chain"]],
        "completedAt": _now_iso(),
    }
    app["result"] = final
    app["updatedAt"] = _now_iso()
    await on_event("final", final)
    return {"status": "approved"}


# ── 智能填单：自然语言 → 结构化表单 ──────────────────────────────────
@router.post("/parse", dependencies=[Depends(rate_limiter)])
async def parse(body: dict):
    text = (body.get("text") or "").strip()
    form_type = body.get("formType")

    if not text:
        raise HTTPException(status_code=400, detail={"error": {"message": "描述不能为空"}})
    if form_type not in ("expense", "leave"):
        raise HTTPException(status_code=400, detail={"error": {"message": "formType 必须是 expense 或 leave"}})

    # 全链路追踪：自然语言 → 结构化表单（提示词与模型返回都留档，便于查"为什么解析错了"）
    trace = start_trace("erp", question=text, meta={"kind": "智能填单", "formType": form_type})
    trace.step("request", "智能填单", detail={"text": text, "formType": form_type})

    try:
        if form_type == "expense":
            form = await parse_expense_form(text)
            compliance_alerts = check_compliance(form)
            form["warnings"] = [*(form.get("warnings") or []), *compliance_alerts]
        else:
            form = await parse_leave_form(text)
        # 用量由 parse_expense_form / parse_leave_form 内部按真实 token 记账
        # （这里原来记的是 0/0：调用次数对、费用永远是 ¥0）
        trace.step("response", "解析结果", detail={"form": form})
        await trace.finish(summary={"formType": form_type,
                                    "items": len(form.get("items") or []),
                                    "warnings": len(form.get("warnings") or [])})
        return {"success": True, "form": form, "formType": form_type, "runId": trace.run_id}
    except Exception as err:
        logger.error("erp: parse error", {"error": str(err)})
        trace.fail("解析失败", err)
        await trace.finish(status="error")
        raise HTTPException(status_code=500, detail={"error": {"message": "解析失败，请检查输入内容"}})


@router.get("/roles")
async def roles():
    return {"roles": list(APPROVAL_ROLES.values())}


# ── 提交申请并开始审批（遇到 need_info 会自动暂停）───────────────────
@router.post("/submit/stream", dependencies=[Depends(rate_limiter)])
async def submit_stream(body: dict):
    form_data = body.get("formData")
    form_type = body.get("formType")
    applicant_name = body.get("applicantName")

    if not form_data or form_type not in ("expense", "leave"):
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少表单数据或类型不正确"}})

    app_id = f"APP{int(time.time() * 1000)}"
    application = {
        "id": app_id,
        "formType": form_type,
        "formData": {**form_data, "applicantName": applicant_name or "申请人"},
        "status": "in_progress",
        "chain": _build_chain(form_data, form_type),
        "chainReason": plan_reasons(form_data, form_type),
        "messages": [],
        "answers": [],
        "pendingStepId": None,
        "result": None,
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
    }
    _applications[app_id] = application

    async def generator():
        # 全链路追踪：审批链每个节点的角色、结论、耗时都在里面
        trace = start_trace("erp", question=(application["formData"].get("applicantName") or "") + " 的"
                            + ("报销" if form_type == "expense" else "请假") + "申请",
                            meta={"kind": "审批流", "appId": app_id, "formType": form_type})
        trace.step("request", "提交申请", detail={
            "appId": app_id, "formType": form_type, "formData": application["formData"],
            "chain": [{"stepId": s["stepId"], "order": s["order"], "role": s["role"]}
                      for s in application["chain"]],
            "chainReason": application["chainReason"],
        })
        yield "start", {"appId": app_id, "formType": form_type,
                        "chainReason": application["chainReason"], "runId": trace.run_id}
        yield "plan", {
            "approvers": [{"stepId": s["stepId"], "order": s["order"], "roleId": s["roleId"],
                           "role": s["role"]} for s in application["chain"]],
            "totalSteps": len(application["chain"]),
            "chainReason": application["chainReason"],
        }

        queue: list = []

        async def on_event(event_type, data):
            queue.append((event_type, data))

        result = await _run_chain(application, on_event)
        while queue:
            event_type, data = queue.pop(0)
            yield event_type, data

        if result["status"] == "waiting_info":
            trace.step("response", "审批暂停（等待人工补充材料）", detail={
                "pendingStepId": application["pendingStepId"],
            })
            await trace.finish(summary={"status": "paused",
                                        "pendingStepId": application["pendingStepId"]})
            yield "paused", {"appId": app_id, "stepId": application["pendingStepId"],
                             "application": application, "runId": trace.run_id}
        else:
            # 用量已由每个审批节点（review_application）按真实 token 记账，
            # 这里不再补一条"只有延迟、没有 token"的记录，避免同一次审批记两遍
            trace.step("response", "审批结束", detail={"status": application["status"],
                                                       "result": application.get("result")})
            await trace.finish(summary={"status": application["status"]})
            yield "done", {"appId": app_id, "status": application["status"],
                           "runId": trace.run_id}

    return sse_stream(generator)


# ── 人工补料后继续审批（核心：提出问题后人是可以改的）────────────────
@router.post("/applications/{app_id}/resume/stream", dependencies=[Depends(rate_limiter)])
async def resume_stream(app_id: str, body: dict):
    application = _get_app(app_id)
    if application["status"] != "waiting_info" or not application["pendingStepId"]:
        raise HTTPException(status_code=400, detail={"error": {"message": "该申请当前不需要补充材料"}})

    step_id = application["pendingStepId"]
    step = _find_step(application, step_id)
    answer = (body.get("answer") or "").strip()
    form_patch = body.get("formPatch")

    async def generator():
        # 全链路追踪：人工补料后继续评审，单独一条 run
        trace = start_trace("erp", question="补充材料后继续审批：" + app_id,
                            meta={"kind": "审批流（续）", "appId": app_id,
                                  "formType": application["formType"]})
        trace.step("request", "人工补充材料，继续审批", detail={
            "appId": app_id, "stepId": step_id, "answer": answer, "formPatch": form_patch,
        })
        # 1) 先落地人工补料（说明 + 修改后的申请数据）
        _apply_form_patch(application, form_patch)
        if answer or form_patch:
            content = answer or "（已修改申请数据，未附加说明）"
            human_msg = {"from": "applicant", "role": APPROVAL_ROLES["applicant"], "type": "answer",
                         "content": content, "patched": bool(form_patch), "at": _now_iso()}
            step["messages"].append(human_msg)
            application["messages"].append(human_msg)
            application["answers"].append({
                "stepId": step_id, "answer": answer, "formPatch": form_patch or {},
                "at": human_msg["at"],
            })
            yield "message", human_msg

        yield "resumed", {"appId": app_id, "stepId": step_id,
                          "formData": application["formData"], "askRounds": step["askRounds"],
                          "runId": trace.run_id}

        # 2) 重新评审当前节点，并继续往后跑
        queue: list = []

        async def on_event(event_type, data):
            queue.append((event_type, data))

        index = [s["stepId"] for s in application["chain"]].index(step_id)
        result = await _run_chain(application, on_event, start_index=index)
        while queue:
            event_type, data = queue.pop(0)
            yield event_type, data

        if result["status"] == "waiting_info":
            trace.step("response", "审批暂停（等待人工补充材料）", detail={
                "pendingStepId": application["pendingStepId"],
            })
            await trace.finish(summary={"status": "paused",
                                        "pendingStepId": application["pendingStepId"]})
            yield "paused", {"appId": app_id, "stepId": application["pendingStepId"],
                             "application": application, "runId": trace.run_id}
        else:
            # 用量已由每个审批节点（review_application）按真实 token 记账，
            # 这里不再补一条"只有延迟、没有 token"的记录，避免同一次审批记两遍
            trace.step("response", "审批结束", detail={"status": application["status"],
                                                       "result": application.get("result")})
            await trace.finish(summary={"status": application["status"]})
            yield "done", {"appId": app_id, "status": application["status"],
                           "runId": trace.run_id}

    return sse_stream(generator)


# ── 申请记录 ──────────────────────────────────────────────────────────
@router.get("/applications")
async def list_applications():
    items = sorted(_applications.values(), key=lambda a: a["createdAt"], reverse=True)
    return {"applications": [
        {
            "id": a["id"],
            "formType": a["formType"],
            "status": a["status"],
            "amount": a["formData"].get("totalAmount"),
            "reason": a["formData"].get("reason"),
            "days": a["formData"].get("workdays") or a["formData"].get("days"),
            "createdAt": a["createdAt"],
            "updatedAt": a["updatedAt"],
            "stepTotal": len(a["chain"]),
            "stepDone": len([s for s in a["chain"] if s["status"] == "approved"]),
            "currentRole": next((s["role"]["name"] for s in a["chain"] if s["status"] != "approved"), None),
        }
        for a in items
    ]}


@router.get("/applications/{app_id}")
async def get_application(app_id: str):
    return {"application": _get_app(app_id)}


@router.post("/applications/{app_id}/withdraw")
async def withdraw_application(app_id: str):
    """撤回：保留记录，状态置为 withdrawn（审计需要）。"""
    app = _get_app(app_id)
    if app["status"] == "approved":
        raise HTTPException(status_code=400, detail={"error": {"message": "已通过的申请不能撤回"}})
    app["status"] = "withdrawn"
    app["updatedAt"] = _now_iso()
    return {"success": True, "application": app}


@router.delete("/applications/{app_id}")
async def delete_application(app_id: str):
    """删除：从记录里彻底移除（测试时清理数据用）。"""
    _get_app(app_id)
    _applications.pop(app_id, None)
    logger.info("erp: application deleted", {"appId": app_id})
    return {"success": True, "deleted": app_id}


@router.delete("/applications")
async def clear_applications():
    """清空全部申请记录。"""
    n = len(_applications)
    _applications.clear()
    logger.info("erp: all applications cleared", {"count": n})
    return {"success": True, "cleared": n}
