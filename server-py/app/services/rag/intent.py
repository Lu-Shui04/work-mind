# server-py/app/services/rag/intent.py
"""
意图识别：判断一条用户消息是否需要检索知识库，以及要检索什么。

## 默认策略：召回优先（recall_first）

旧策略是"规则先判 + 模型兜底"：只有出现问号/疑问词/领域关键词才去查库。实测的代价：

- 用户输入「rag召回失败」，而库里正好有一篇讲 RAG 的资料 → 规则判"不需要检索"，
  连模型都没问，直接走通用问答。用户看到的就是"我问了它，它却不去查库"。
- 就算交给模型兜底，模型也会说"这属于通用技术问答，无需检索公司内部文档"——
  **它根本不知道公司库里有什么**，却在凭常识判断"该不该查"。在知识库产品里，
  这个判断方向本身就是错的。

结论：**"要不要查库"不该由常识判断，而应该默认去查。**
- 库里没有相关内容 → 检索返回 0 条，代价只是一次 embedding + 一次本地 SQL（几百毫秒），
  而且界面上会明确写出"为什么没命中"（reason=below_threshold / kb_empty）；
- 库里正好有 → 用户拿到带引用的答案，这才是产品的价值。

所以现在只有"明显与知识库无关"的消息才跳过检索：打招呼、客套话、自我介绍、
讲笑话/写诗、纯算式。其余一律检索。要恢复旧的严格模式：RAG_INTENT_MODE=strict。

## 部门提示只是提示

department_hint（年假→hr、报销→finance…）**不参与候选集裁剪**，
只用于解释与日志：模型猜错一次（把 general 文档猜成 tech）会让整份文档召不回，
这个代价远大于"检索范围没被收窄"。
"""
import os
import re

from pydantic import BaseModel, Field

# recall_first（默认，召回优先） / strict（旧的"规则+模型"判定）
RAG_INTENT_MODE = (os.getenv("RAG_INTENT_MODE", "recall_first") or "recall_first").lower()

# 领域关键词 → 部门线索（仅用于提示与解释，不做硬过滤）
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "hr": ["年假", "请假", "调休", "考勤", "绩效", "招聘", "入职", "转正", "福利", "薪酬", "职级", "试用期", "婚假", "产假", "病假", "事假"],
    "finance": ["报销", "发票", "差旅", "预算", "费用", "付款", "借款", "补贴", "出差", "打车", "住宿标准"],
    "tech": ["代码", "规范", "接口", "部署", "上线", "架构", "技术选型", "组件", "版本发布", "数据库"],
    "legal": ["合同", "法务", "合规", "知识产权", "保密", "违约", "条款", "授权"],
    "product": ["需求", "原型", "产品", "PRD", "交互", "用户故事", "埋点"],
}
# 通用知识型词（不指向具体部门）
_GENERAL_KEYWORDS = ["公司规定", "制度", "政策", "流程", "标准", "规范", "手册", "指南", "条例", "办法", "SOP", "文档", "知识库"]
_QUESTION_MARKERS = ["吗", "?", "？", "什么", "如何", "怎么", "多少", "几天", "哪些", "是否", "能不能", "可不可以", "怎样"]

# 明确"不需要查库"的信号 —— 只有命中这些才跳过检索
_SKIP_RULES: list[tuple[str, str]] = [
    (r"^\s*(你好|您好|hi|hello|嗨|哈喽|在吗|早上好|下午好|晚上好|早|晚安)[!！。~～\s]*$", "打招呼"),
    (r"^\s*(谢谢|多谢|感谢|好的|好嘞|ok|OK|收到|明白了|知道了|再见|拜拜|晚安)[!！。~～\s]*$", "客套话"),
    (r"(你是谁|你叫什么|你能做什么|你会什么|介绍一下你自己|自我介绍)", "询问助手本身"),
    (r"(讲个笑话|讲个故事|写一首诗|写首诗|写首词|编个故事|来个段子)", "闲聊创作"),
    (r"^\s*(帮我)?(翻译|润色|改写|缩写|扩写)(一下)?", "纯语言任务"),
    # 写作/生成类任务：这类要的是"写"，不是"查"，否则会被知识优先策略拒答
    (r"^\s*(帮我|请|麻烦)?\s*(写|起草|拟|生成|来一?[份个篇段])", "写作/生成类任务"),
    # 通用工具类问题：把日期/天气也排除掉，否则"今天几号"会被"知识优先"策略拒答
    (r"^\s*(今天|现在|当前|此刻)?\s*(几号|几点了|几点|星期几|礼拜几|周几|什么日子|多少号)", "询问日期时间"),
    (r"(今天|明天|昨天|这周|下周).{0,6}(天气|气温|下雨|下雪)", "询问天气"),
    (r"^[\d\s+\-*/().,%^=]+$", "纯算式"),
]


class IntentDecision(BaseModel):
    need_knowledge: bool = Field(description="是否需要检索企业知识库")
    query: str = Field(default="", description="用于检索的查询语句（可对原问题做改写）")
    department_hint: str | None = Field(default=None, description="推测的部门范围，如 hr/finance")
    rule_hit: list[str] = Field(default_factory=list, description="命中的规则关键词，便于审计")
    decision_source: str = Field(default="recall-first", description="recall-first / skip-* / strict-* / forced")
    reason: str = Field(default="", description="人话解释，随 SSE 下发到前端")


class _ModelIntent(BaseModel):
    need_knowledge: bool = Field(description="这个问题是否需要查阅公司内部文档才能回答")
    department_hint: str | None = Field(default=None, description="若需要，最可能相关的部门：hr/finance/tech/legal/product/general 之一")
    rewritten_query: str = Field(default="", description="改写后的检索查询，去掉客套话，保留关键实体")


def _department_hint(msg: str) -> tuple[str | None, list[str]]:
    hits: list[str] = []
    department = None
    for dept, words in _DOMAIN_KEYWORDS.items():
        matched = [w for w in words if w in msg]
        if matched:
            hits.extend(matched)
            department = department or dept
    hits.extend([w for w in _GENERAL_KEYWORDS if w in msg])
    return department, hits


def _skip_reason(msg: str) -> str | None:
    for pattern, label in _SKIP_RULES:
        if re.search(pattern, msg):
            return label
    return None


def _rule_classify(message: str) -> IntentDecision:
    """召回优先的默认判定：只有明确"与知识库无关"的消息才跳过。"""
    msg = message.strip()
    department, hits = _department_hint(msg)

    if not msg:
        return IntentDecision(need_knowledge=False, query=msg, decision_source="skip-empty",
                              reason="空消息")
    if len(msg) <= 1:
        return IntentDecision(need_knowledge=False, query=msg, rule_hit=hits,
                              department_hint=department, decision_source="skip-too-short",
                              reason="消息过短，没有可检索的内容")

    skipped = _skip_reason(msg)
    if skipped:
        return IntentDecision(need_knowledge=False, query=msg, rule_hit=hits,
                              department_hint=department, decision_source="skip-" + skipped,
                              reason=f"识别为「{skipped}」，与知识库无关")

    return IntentDecision(
        need_knowledge=True, query=msg, department_hint=department, rule_hit=hits,
        decision_source="recall-first",
        reason="默认检索知识库（库里有相关内容就带引用回答；没有则明确说明未命中原因）"
               + (f"；推断部门：{department}" if department else ""),
    )


def _strict_classify(message: str) -> IntentDecision:
    """旧的严格模式：命中关键词或疑问句才检索，模糊时交给模型判断。"""
    msg = message.strip()
    department, hits = _department_hint(msg)
    if hits:
        return IntentDecision(need_knowledge=True, query=msg, department_hint=department,
                              rule_hit=hits, decision_source="strict-rule",
                              reason="命中关键词：" + "、".join(hits[:4]))

    is_question = any(m in msg for m in _QUESTION_MARKERS)
    if is_question and len(msg) >= 6:
        return IntentDecision(need_knowledge=False, query=msg, decision_source="uncertain",
                              reason="规则不确定，交给模型判断")
    return IntentDecision(need_knowledge=False, query=msg, decision_source="strict-rule",
                          reason="未命中知识型关键词")


async def classify_intent(message: str) -> IntentDecision:
    if RAG_INTENT_MODE == "strict":
        decision = _strict_classify(message)
        if decision.decision_source != "uncertain":
            return decision
        if os.getenv("RAG_INTENT_LLM_FALLBACK", "true").lower() != "true":
            decision.decision_source = "strict-rule"
            return decision
        return await _model_fallback(message)

    return _rule_classify(message)


async def _model_fallback(message: str) -> IntentDecision:
    """仅在 strict 模式下使用：一次极小的结构化输出调用（失败按"不检索"处理）。"""
    try:
        from app.services.model import create_chat_model
        model = create_chat_model(temperature=0, streaming=False)
        result = await model.with_structured_output(_ModelIntent, method="function_calling").ainvoke([
            {"role": "system", "content": (
                "判断用户问题是否需要查阅公司内部文档（制度/流程/规范/规定）才能回答。\n"
                "只要可能与企业自有资料相关就判需要；通用常识、闲聊、写作、计算类问题不需要。\n"
                "部门只能取 hr/finance/tech/legal/product/general。"
            )},
            {"role": "user", "content": message},
        ])
        dept = result.department_hint if result.department_hint in _DOMAIN_KEYWORDS else None
        return IntentDecision(
            need_knowledge=bool(result.need_knowledge),
            query=result.rewritten_query or message,
            department_hint=dept,
            decision_source="strict-model",
            reason="模型判定" + ("需要" if result.need_knowledge else "不需要") + "检索知识库",
        )
    except Exception:
        return IntentDecision(need_knowledge=False, query=message,
                              decision_source="strict-model-failed",
                              reason="意图分类失败，按不检索处理")
