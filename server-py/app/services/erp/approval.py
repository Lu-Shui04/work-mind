# server-py/app/services/erp/approval.py
"""
Multi-Agent 审批：结构化裁决 + 只在"要材料"时打断人工。

【这一版修掉的四个硬伤】（对应之前那版被吐槽"看不懂"的流程）
1. 旧版用关键词判断是否通过：_is_approved 只要文本里没有"驳回/不批/拒绝"就算通过，
   于是"请补充结婚证及说明是否申请延长"这种**疑问句被直接判成"已通过"**。
   → 现在改成结构化输出：decision ∈ {approve, reject, need_info}，由模型显式表态，不再猜。
2. 旧版只在消息里出现"？"才生成申请人答复，导致疑问挂空、无人回答。
   → 现在 decision=need_info 时**主动中断流程**，把问题抛给真人回答，
     并允许同时修改申请数据（比如把"婚假 1 天"改成"事假 1 天"或改天数）。
3. 旧版把所有字段无差别投喂给每个角色，主管会替 HR 操心结婚证，几个角色发言高度重复。
   → 现在按角色裁剪字段视图（主管只看业务必要性相关信息，HR 只看假期与材料，财务只看金额票据）。
4. 旧版只输出一段自然语言，前端无从来解释"凭什么"。
   → 现在每个节点产出 decision + reason + checklist（逐条检查项与结论），前端直接渲染。
"""
import json
import time
from datetime import datetime, timezone
from typing import Literal

from langchain_core.callbacks import UsageMetadataCallbackHandler
from pydantic import BaseModel, Field

from app.services import pricing
from app.services.model import create_chat_model, primary_model_name
from app.utils.logger import logger
from app.utils.tokens import sum_usage

_review_model = create_chat_model(temperature=0)

APPROVAL_ROLES = {
    "applicant": {"id": "applicant", "name": "申请人", "icon": "👤", "color": "#4f46e5", "desc": "提交申请，回答审批人的问题"},
    "manager": {"id": "manager", "name": "直属主管", "icon": "👔", "color": "#0891b2", "desc": "只看业务必要性与团队影响"},
    "finance": {"id": "finance", "name": "财务专员", "icon": "💰", "color": "#059669", "desc": "只看费用合规性与票据"},
    "hr": {"id": "hr", "name": "HR 专员", "icon": "📋", "color": "#d97706", "desc": "只看假期类型、天数与证明材料"},
    "director": {"id": "director", "name": "部门总监", "icon": "🏢", "color": "#dc2626", "desc": "大额/长假终审，关注成本与风险"},
}

LEAVE_LABELS = {"annual": "年假", "personal": "事假", "sick": "病假",
                "compensatory": "调休", "marriage": "婚假", "maternity": "产假"}


# ── 角色职责与字段裁剪（避免"主管替 HR 操心结婚证"这类串味）──────────
_ROLE_SPEC = {
    "manager": {
        "duty": "你只负责判断这次请假的**业务必要性**：工作是否可以延后、是否影响团队交付、是否有替代安排。"
                "假期类型是否符合规定、需要什么证明材料**不是你的职责**，不要评价。",
        "checks": ["请假时段是否与关键交付/会议冲突", "请假时长对团队排期的影响是否可控", "是否给出了可交接的安排"],
    },
    "hr": {
        "duty": "你只负责判断**假期类型与证明材料**：申请的天数是否符合该类假期的规定，是否缺少必要证明。"
                "业务必要性不是你的职责。",
        "checks": ["假期类型与申请天数的匹配度", "证明材料是否齐全（病假证明/结婚证等）", "是否存在应改用其他假期类型的情况"],
    },
    "finance": {
        "duty": "你只负责判断**费用合规性**：金额是否超标准、票据是否齐全、科目是否正确。业务必要性不是你的职责。",
        "checks": ["单笔金额是否超过限额", "住宿/餐饮等是否超出标准", "是否有需要补充的发票或说明"],
    },
    "director": {
        "duty": "你是终审，只关注**成本与风险**：金额或时长是否显著偏离常规，是否有必要做例外批准。",
        "checks": ["金额/时长是否处于合理区间", "前面节点的结论是否一致", "是否需要附加条件（如分期、事后补票）"],
    },
}


def role_form_view(role_id: str, form_data: dict, form_type: str) -> dict:
    """按角色裁剪字段视图：只给这个角色该看的字段，减少串味与重复发言。"""
    label = "报销" if form_type == "expense" else "请假"
    base = {"申请类型": label, "申请人": form_data.get("applicantName")}

    if form_type == "expense":
        if role_id in ("finance", "director"):
            view = {**base, "费用类型": form_data.get("type"),
                    "明细": form_data.get("items"), "总金额": form_data.get("totalAmount"),
                    "事由": form_data.get("reason")}
        else:
            view = {**base, "事由": form_data.get("reason"), "总金额": form_data.get("totalAmount")}
    else:
        if role_id in ("hr", "director"):
            view = {**base, "假期类型": LEAVE_LABELS.get(form_data.get("type"), form_data.get("type")),
                    "开始日期": form_data.get("startDate"), "结束日期": form_data.get("endDate"),
                    "自然日": form_data.get("days"), "工作日": form_data.get("workdays"),
                    "原因": form_data.get("reason"), "紧急联系人": form_data.get("emergencyContact")}
        else:
            view = {**base, "请假时段": f"{form_data.get('startDate')} ~ {form_data.get('endDate')}",
                    "工作日": form_data.get("workdays"), "原因": form_data.get("reason")}
    return view


def plan_approval_flow(form_data: dict, form_type: str) -> list[str]:
    flow = ["manager"]
    if form_type == "expense":
        flow.append("finance")
        if (form_data.get("totalAmount") or 0) > 5000:
            flow.append("director")
    else:
        flow.append("hr")
        if (form_data.get("workdays") or 0) > 5:
            flow.append("director")
    return flow


def plan_reasons(form_data: dict, form_type: str) -> list[str]:
    reasons = []
    if form_type == "expense":
        reasons.append("报销类申请固定包含财务复核")
        if (form_data.get("totalAmount") or 0) > 5000:
            reasons.append(f"总金额 ¥{form_data.get('totalAmount')} 超过 ¥5000，需要部门总监终审")
    else:
        reasons.append("请假类申请固定包含 HR 复核")
        if (form_data.get("workdays") or 0) > 5:
            reasons.append(f"请假 {form_data.get('workdays')} 个工作日超过 5 天，需要部门总监终审")
    return reasons


# ── 结构化裁决 ────────────────────────────────────────────────────────
class CheckItem(BaseModel):
    item: str = Field(description="检查项名称")
    result: Literal["pass", "warn", "fail"] = Field(description="pass=符合, warn=需要说明, fail=不符合")
    note: str = Field(description="判断依据，不超过 30 字")


class ReviewDecision(BaseModel):
    decision: Literal["approve", "reject", "need_info"] = Field(
        description="approve=同意通过；reject=明确不同意；need_info=信息不足或存在疑点，需要申请人补充说明/修改申请")
    reason: str = Field(description="结论理由，不超过 60 字")
    checklist: list[CheckItem] = Field(description="2-4 条检查项，逐条给出结论")
    questions: list[str] = Field(default_factory=list,
                                 description="decision=need_info 时必填：要问申请人什么，1-3 条，具体可回答")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_review_usage(handler: UsageMetadataCallbackHandler, started: float,
                         role_id: str = "", decision: str = "") -> dict:
    """把这一次审批节点的模型用量记到看板（feature=erp），并记一步全链路追踪。"""
    from app.routes.monitor import record_api_call
    from app.services.trace import trace_step

    input_tokens, output_tokens, cached_tokens = sum_usage(handler.usage_metadata)
    latency_ms = round((time.time() - started) * 1000)
    record_api_call(feature="erp", input_tokens=input_tokens, output_tokens=output_tokens,
                    cached_input_tokens=cached_tokens, model=primary_model_name(),
                    latency_ms=latency_ms)
    # 追踪里带上"这个节点是谁、结论是什么" —— 审批链出问题时要看的就是它
    trace_step("llm", "审批节点评审", duration_ms=latency_ms,
               detail={"roleId": role_id, "inputTokens": input_tokens,
                       "outputTokens": output_tokens, "cachedInputTokens": cached_tokens,
                       "costCNY": pricing.cost_cny(primary_model_name(), input_tokens,
                                                   output_tokens, cached_tokens),
                       "decision": decision})
    return {"inputTokens": input_tokens, "outputTokens": output_tokens}


async def review_application(role_id: str, form_data: dict, form_type: str,
                             prior_messages: list[dict], applicant_answers: list[dict],
                             force_decision: bool = False) -> dict:
    """对一个审批节点做一次结构化评审。

    prior_messages：前面节点已经产生的消息（让当前角色知道上文结论）
    applicant_answers：真人补充过的说明（decision=need_info 被打断后用户填的）
    """
    spec = _ROLE_SPEC.get(role_id, _ROLE_SPEC["manager"])
    view = role_form_view(role_id, form_data, form_type)

    context_lines = []
    for m in prior_messages[-6:]:
        who = APPROVAL_ROLES.get(m.get("from"), {}).get("name", m.get("from"))
        context_lines.append(f"[{who}] {m.get('content', '')[:120]}")
    for a in applicant_answers:
        context_lines.append(f"[申请人补充说明] {a.get('answer', '')}")

    system = (
        f"你是{APPROVAL_ROLES[role_id]['name']}。{spec['duty']}\n"
        f"请逐条检查：{'；'.join(spec['checks'])}。\n"
        "要求：信息足够就给出 approve/reject；只要存在**必须由申请人补充或修改**才能判断的疑点，"
        "就返回 need_info 并列出具体问题，不要自己替他假设。宁可 need_info，也不要含糊通过。"
    )
    if force_decision:
        # 已经补充过一轮材料：不再允许继续追问，必须给出明确结论（避免流程无限打转）
        system += "\n【重要】申请人已经补充过说明，本轮**必须**给出 approve 或 reject，不允许再返回 need_info。"
    user = (
        f"申请内容（你被允许看到的字段）：\n{json.dumps(view, ensure_ascii=False, indent=2)}\n\n"
        f"已有上下文：\n" + ("\n".join(context_lines) if context_lines else "（无）")
    )

    # 用量记账：with_structured_output 返回的是解析后的对象，**拿不到 usage**，
    # 所以挂一个回调处理器收集真实 token —— 以前这里记的是 0，看板上 ERP 永远是 ¥0。
    handler = UsageMetadataCallbackHandler()
    started = time.time()
    result: ReviewDecision = await _review_model.with_structured_output(
        ReviewDecision, method="function_calling"
    ).ainvoke([{"role": "system", "content": system}, {"role": "user", "content": user}],
              config={"callbacks": [handler]})

    data = result.model_dump()
    if data["decision"] == "need_info" and not data["questions"]:
        data["questions"] = [data["reason"]]
    data["durationMs"] = round((time.time() - started) * 1000)
    data["usage"] = _record_review_usage(handler, started, role_id, data["decision"])
    data["roleId"] = role_id
    data["formView"] = view
    logger.info("erp: review done", {
        "roleId": role_id, "decision": data["decision"],
        "checks": len(data["checklist"]), "ms": data["durationMs"],
    })
    return data
