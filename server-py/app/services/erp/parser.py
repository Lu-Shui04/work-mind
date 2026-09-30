# server-py/app/services/erp/parser.py
# 自然语言 → 结构化表单：用结构化输出解析用户的口语化描述
import time
from datetime import date, datetime, timedelta
from typing import Literal

from langchain_core.callbacks import UsageMetadataCallbackHandler
from pydantic import BaseModel, Field

from app.models import pricing
from app.models.llm import create_chat_model, primary_model_name
from app.infra.tokens import sum_usage
from app.prompts.erp import expense_parse_system, leave_parse_system

_model = create_chat_model(temperature=0)


def _record_parse_usage(handler: UsageMetadataCallbackHandler, started: float) -> dict:
    """把"自然语言 → 表单"这次模型调用的用量记到看板（feature=erp）。

    以前 erp 的 /parse 记的是 input_tokens=0/output_tokens=0 —— 调用次数对、
    费用永远是 ¥0，看板上的"ERP 审批"因此完全没有参考价值。
    """
    from app.api.monitor import record_api_call
    from app.infra.trace import trace_step

    input_tokens, output_tokens, cached_tokens = sum_usage(handler.usage_metadata)
    latency_ms = round((time.time() - started) * 1000)
    record_api_call(feature="erp", input_tokens=input_tokens, output_tokens=output_tokens,
                    cached_input_tokens=cached_tokens, model=primary_model_name(),
                    latency_ms=latency_ms)
    trace_step("llm", "自然语言解析（结构化输出）", duration_ms=latency_ms,
               detail={"inputTokens": input_tokens, "outputTokens": output_tokens,
                       "cachedInputTokens": cached_tokens,
                       "costCNY": pricing.cost_cny(primary_model_name(), input_tokens,
                                                   output_tokens, cached_tokens)})
    return {"inputTokens": input_tokens, "outputTokens": output_tokens}


# ── 报销申请解析 ──────────────────────────────────────────────
class ExpenseItem(BaseModel):
    name: str = Field(description='费用项目名称，如"高铁票""住宿费"')
    amount: float = Field(description="金额，单位：元")
    date: str | None = Field(default=None, description="发生日期，格式 YYYY-MM-DD")
    note: str | None = Field(default=None, description="备注")


class ExpenseForm(BaseModel):
    type: Literal["travel", "meal", "office", "training", "other"] = Field(
        description="费用类型：travel=差旅, meal=餐饮, office=办公用品, training=培训, other=其他")
    items: list[ExpenseItem] = Field(description="费用明细列表")
    totalAmount: float = Field(description="总金额，单位：元")
    reason: str = Field(description="报销事由，20字以内")
    dept: str | None = Field(default=None, description="报销部门")
    warnings: list[str] = Field(default_factory=list, description="发现的异常或需要注意的地方，如金额超标、信息不全等")


async def parse_expense_form(text: str) -> dict:
    today = date.today().isoformat()

    handler = UsageMetadataCallbackHandler()
    started = time.time()
    extract_model = _model.with_structured_output(ExpenseForm, method="function_calling")
    result: ExpenseForm = await extract_model.ainvoke([
        {
            "role": "system",
            "content": expense_parse_system(today),
        },
        {"role": "user", "content": text},
    ], config={"callbacks": [handler]})

    _record_parse_usage(handler, started)
    return result.model_dump()


# ── 请假申请解析 ──────────────────────────────────────────────
class LeaveForm(BaseModel):
    type: Literal["annual", "personal", "sick", "compensatory", "marriage", "maternity"] = Field(
        description="假期类型：annual=年假, personal=事假, sick=病假, compensatory=调休, marriage=婚假, maternity=产假")
    startDate: str = Field(description="开始日期，格式 YYYY-MM-DD")
    endDate: str = Field(description="结束日期，格式 YYYY-MM-DD")
    days: float = Field(description="请假天数（自然日）")
    workdays: float = Field(description="工作日天数（排除周末）")
    reason: str = Field(description="请假原因，30字以内")
    emergencyContact: str | None = Field(default=None, description="紧急联系人（如果提到）")
    warnings: list[str] = Field(default_factory=list, description="需要注意的地方，如超过年假余额、需要双重审批等")


def _count_workdays(start_str: str, end_str: str) -> int:
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


async def parse_leave_form(text: str) -> dict:
    today = date.today().isoformat()

    handler = UsageMetadataCallbackHandler()
    started = time.time()
    extract_model = _model.with_structured_output(LeaveForm, method="function_calling")
    result: LeaveForm = await extract_model.ainvoke([
        {
            "role": "system",
            "content": leave_parse_system(today),
        },
        {"role": "user", "content": text},
    ], config={"callbacks": [handler]})

    _record_parse_usage(handler, started)
    data = result.model_dump()
    # 自动计算工作日（补充模型可能算错的情况）
    if data.get("startDate") and data.get("endDate"):
        try:
            data["workdays"] = _count_workdays(data["startDate"], data["endDate"])
        except ValueError:
            pass

    return data


# ── 报销金额合规检查 ──────────────────────────────────────────
# 公司报销标准（模拟数据，实际从数据库读取）
EXPENSE_RULES = {
    "travel": {"hotelPerNight": 800, "mealPerDay": 200, "flightEconomy": True, "maxSingleItem": 5000},
    "meal": {"maxSingleItem": 500},
    "office": {"maxSingleItem": 1000},
    "training": {"maxSingleItem": 10000},
    "other": {"maxSingleItem": 2000},
}


def check_compliance(expense_form: dict) -> list[str]:
    alerts = []
    rules = EXPENSE_RULES.get(expense_form.get("type"), EXPENSE_RULES["other"])

    for item in expense_form.get("items", []):
        if item["amount"] > rules["maxSingleItem"]:
            alerts.append(f'"{item["name"]}" ¥{item["amount"]} 超过单笔限额 ¥{rules["maxSingleItem"]}，需要额外说明')

        if expense_form.get("type") == "travel":
            if "住宿" in item["name"] and item["amount"] > rules["hotelPerNight"] * 3:
                alerts.append(f"住宿费用偏高，每晚标准为 ¥{rules['hotelPerNight']}")

    return alerts
