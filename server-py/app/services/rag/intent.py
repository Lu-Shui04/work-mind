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
import time

from pydantic import BaseModel, Field

from app.prompts.rag import INTENT_SYSTEM
from app.infra.trace import trace_step

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
    (r"^\s*(谢谢|多谢|感谢|好的|好嘞|好|行|可以|嗯|对|是的|没错|ok|OK|收到|明白了|知道了|再见|拜拜|晚安)[!！。~～\s]*$", "客套话"),
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
    if _is_ack_continuation(msg):
        return "确认类会话延续"
    if _is_conversation_meta(msg):
        return "询问对话记忆"
    if _is_self_intro(msg):
        return "自我介绍（存画像，不查库）"
    return None


# 确认/同意开头的会话延续：「好的，发一下直属主管」「行，就按 600 一晚算」。
# 这类消息是在回应上一轮、没有新的知识诉求 —— 照 recall_first 检索只会白白 miss 一次，
# 回答还会被强制带上「知识库中没有查到相关内容」（2026-10-04 实测，用户困惑
# "为什么每一句都要检索"）。留三个"还是要查"的口子：后半句还有知识型关键词 /
# 疑问词 / 明确的查、搜动作（「好的，帮我查一下年假政策」必须照常检索）。
_ACK_LEAD = re.compile(
    r"^(?:好的|好嘞|好|行|可以|嗯|OK|ok|收到|明白|明白了|知道了|对|是的|没错)"
    r"[，,。.！!~～\s、]+(.+)$")
_LOOKUP_VERBS = ("查", "搜", "检索", "帮我查", "查一下", "看一下")


def _is_ack_continuation(msg: str) -> bool:
    m = _ACK_LEAD.match(msg)
    if not m:
        return False
    rest = m.group(1)
    if any(kw in rest for kw in _QUERY_KW):
        return False
    if any(mk in rest for mk in _QUESTION_MARKERS):
        return False
    if any(v in rest for v in _LOOKUP_VERBS):
        return False
    return True


# 对话记忆 / 助手自身的元问题：「记住了吗」「我叫什么」「我们刚聊到哪」。
# 这类问题的答案在**会话记忆与用户画像**里，不在公司文档里 —— 但它们带问号，
# 靠"有没有疑问词"判不出来，必须单独列出来。否则 recall_first 会把它送去检索，
# miss 之后用户收到的是一句"知识库中没有查到相关内容"（2026-10-04 实测：
# 用户说完"我叫小米"再问"记住了吗"，得到的就是这句固定答复）。
_META_PATTERNS = ("记住", "记得", "我叫什么", "我的名字", "我刚才说", "我说过什么",
                  "我们聊", "聊到哪", "上文", "上一条", "你说过", "自我介绍", "你是谁")


def _is_conversation_meta(msg: str) -> bool:
    if not any(p in msg for p in _META_PATTERNS):
        return False
    # 带知识型关键词的照常检索（"帮我记住差旅报销标准"仍然要查库）
    return not any(kw in msg for kw in _QUERY_KW)


# 自我介绍这类陈述：「我叫小米」「我是技术部的，叫我小李」—— 它的去处是**用户画像**，
# 库里不可能有答案；照常检索只会白跑一次，而且一旦 miss 还会把回答变成"没查到"。
_SELF_INTRO = re.compile(r"^\s*(我叫|我是|我的名字是|我的名字叫|叫我|你可以叫我)")


def _is_self_intro(msg: str) -> bool:
    if not _SELF_INTRO.match(msg):
        return False
    return not any(kw in msg for kw in _QUERY_KW)


def looks_like_knowledge_need(msg: str) -> bool:
    """消息是否带着知识诉求 —— 决定"没查到"这句声明该不该说。

    与"要不要检索"是两回事：recall_first 对很多非知识消息也照常检索（例行成本，
    见模块开头），但检索 miss 后**不该**拿「知识库中没有查到相关内容」去开场/拒答 ——
    「好的，发一下直属主管」这类会话延续被硬塞一句"没查到"只会让用户困惑。
    有知识型关键词 / 疑问词 / 明确的查、搜动作，才算知识诉求。
    """
    if any(kw in msg for kw in _QUERY_KW):
        return True
    if any(mk in msg for mk in _QUESTION_MARKERS):
        return True
    if any(v in msg for v in _LOOKUP_VERBS):
        return True
    return False


# ── 检索式改写：动作请求 → 名词短语检索式 ─────────────────────────────
# 为什么必须改：cross-encoder 重排模型（bge-reranker 等）是按「问句-答案段」训练的，
# 对「帮我报销一下」这类祈使/动作请求几乎没有判别力 —— 实测同一批候选：
#   查询「帮我把2000元的住宿费报销一下，给领导通知一下」→《差旅与报销管理制度》
#   的住宿费标准切片只得 0.20，而查询「住宿费报销」→ 同一片 0.97。
# 向量召回对两种说法都稳（0.53 照样把制度文档召回来），于是坏链是：
#   「重排全判死 → rerank_rejected → 对外说知识库没查到」——制度明明就在库里。
# 所以检索前先把动作请求改写成检索式（问句原样返回），规则全部确定性、零模型调用
# （recall_first 模式的成本承诺不变）。应用位置在 rag/query.py 的检索入口，
# 因此对话 / 知识库检索页 / Agent 预检索 / read_doc 工具四条链路全部生效。

# 触发条件（保守）：带祈使前缀（帮我/请/麻烦/我想…）或带「一下」；
# 纯问句（"年假有多少天？"）不触发，原样返回 —— 评测集里的问句式用例不受影响。
_QUERY_LEAD = re.compile(
    r"^(?:我想了解一下|我想了解|请帮我|帮我|请你|麻烦你|麻烦|请|烦请|"
    r"替我|给我|帮忙|我想|我要)\s*")
# 只删「动作动词」开头（查/看/搜/找/算/讲…）。单个"查"字会不会误删复合词？
# "调查/审查"是"调/审"打头、不受影响；"查看"配"查"删掉后剩"看…"仍可检索。
_QUERY_VERBS = re.compile(
    r"^(?:查一下|查查|查看|看一下|看看|看下|搜一下|搜搜|搜索|找一下|找找|"
    r"算一下|算算|确认一下|了解一下|了解|介绍一下|介绍|讲一下|讲讲|"
    r"说一下|说说|解释一下|查|看|搜|找|算|讲)\s*")
_QUERY_TAIL = re.compile(r"[。！!？?\s]*$")
# 金额数字（5000元/2000块…）：cross-encoder 拿"2000"对不上制度表里的"600/450/350"，
# 反而压低分数（实测 0.97 → 0.63），而金额本来就在用户原话里、回答时模型看得到，删掉无损。
_QUERY_AMOUNT = re.compile(r"\d+(?:\.\d+)?\s*(?:元|块|万|千|百)\s*的?")
# 指代词/所有格：不影响检索主题，纯稀释信号。分两档：
# - 子句开头剥「该」（"该"在词中间会误伤"应该"，所以只锚定开头）
# - 指示词/所有格（这笔/这些/这个/我的/公司的…）随处可剥：它们不可能成为检索主题
_QUERY_DEMO_HEAD = re.compile(r"^该\s*")
_QUERY_DEMO_ANY = re.compile(
    r"(?:这笔|这些|这个|那个|本次|这次|我们的|咱们的|我的|公司的|部门的)\s*")

# 哪些子句值得进检索式：含知识型关键词的子句。动作子句（"给领导通知一下"）不含
# 关键词，会被丢掉 —— 通知是工具活，不是知识库的检索对象。
_QUERY_KW = [w for words in _DOMAIN_KEYWORDS.values() for w in words] + _GENERAL_KEYWORDS


def build_search_query(message: str) -> str:
    """把动作请求改写成检索式；问句原样返回。幂等：结果不会再触发改写。"""
    msg = (message or "").strip()
    if not msg or len(msg) <= 1:
        return msg

    # 触发条件：祈使前缀 或 「一下」。纯问句不触发（这是防回归的关键）。
    if not _QUERY_LEAD.match(msg) and "一下" not in msg:
        return msg

    text = msg.replace("一下", "")
    # 祈使脚手架可能叠着（"请帮我查…"），循环剥到剥不动为止
    for _ in range(3):
        stripped = _QUERY_LEAD.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"^把\s*", "", text)
    text = _QUERY_VERBS.sub("", text).strip()

    # 拆子句：只要「含知识型关键词」的子句；一个都没有就全保留（别把信息丢光）
    clauses = [c.strip() for c in re.split(r"[，,；;。\s]+", text) if c.strip()]
    if any(any(kw in c for kw in _QUERY_KW) for c in clauses):
        clauses = [c for c in clauses if any(kw in c for kw in _QUERY_KW)]

    parts = []
    for c in clauses:
        c = _QUERY_DEMO_HEAD.sub("", c).strip()
        c = _QUERY_DEMO_ANY.sub("", c).strip()
        c = _QUERY_AMOUNT.sub("", c).strip()
        c = _QUERY_TAIL.sub("", c).strip()
        if c:
            parts.append(c)
    result = " ".join(parts).strip()
    return result or msg


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
    """对外唯一入口：判定"这次要不要查知识库"，并记一步追踪。

    "为什么这次没查知识库"是最常被问的问题，所以判定结果、依据、改写后的
    检索词都要落到 trace 里（前端全链路页一眼能看到）。
    """
    started = time.time()
    decision = await _classify_intent(message)
    trace_step("intent", "意图判定", duration_ms=round((time.time() - started) * 1000), detail={
        "needKnowledge": decision.need_knowledge,
        "decisionSource": decision.decision_source,
        "reason": decision.reason,
        "searchQuery": decision.query,
        "departmentHint": decision.department_hint,
        "ruleHit": decision.rule_hit,
    })
    return decision


async def _classify_intent(message: str) -> IntentDecision:
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
        from app.models.llm import create_chat_model
        model = create_chat_model(temperature=0, streaming=False)
        result = await model.with_structured_output(_ModelIntent, method="function_calling").ainvoke([
            {"role": "system", "content": INTENT_SYSTEM},
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
