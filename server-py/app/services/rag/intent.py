# server-py/app/services/rag/intent.py
"""
意图识别：判断一条用户消息是否需要检索知识库，以及要检索什么。

## 三种模式（RAG_INTENT_MODE）

### strict（历史模式，别再用来当默认）
"规则先判 + 模型兜底"：只有出现问号/疑问词/领域关键词才查库。实测代价：
- 用户输入「rag召回失败」，库里正好有那篇资料 → 规则判"不需要检索"，连模型都没问；
- 就算交给模型兜底，模型也会说"这属于通用技术问答，无需检索公司内部文档" ——
  **它根本不知道公司库里有什么**，却在凭常识判断"该不该查"。

### recall_first（纯规则，召回优先）
只有在"明显与知识库无关"时才跳过（打招呼/客套/写作/日期/纯算式），其余一律查库。
代价只是一次 embedding + 一次 SQL（几百毫秒，¥0.00006）。
但它的短板在 2026-10-04 暴露得很清楚：**规则是个跑步机** —— 一个 session 里就得补
三条（"好的，发一下直属主管" / "记住了吗" / "我叫小米"），每来一种新说法都要再加一条。

### hybrid（默认）
分三层，各干各擅长的事：
1. **规则快速通道**：打招呼/客套/纯算式/日期这类高置信、判错代价极低的 → 跳过检索（0 成本）；
2. **知识信号直通**：消息里有制度关键词或"查/搜"动作 → 直接查库，**不问模型**
   （省掉实测 ≈742ms / ¥0.0003 的那次调用）；
3. **其余交给模型判"这条消息属于哪一类"**（chitchat/memory/self_intro/action/company/general）——
   注意问的是**类别**，不是"该不该查库"：类别 → 是否检索由 _CATEGORY_POLICY 做确定性映射。
   这样既拿到模型的泛化能力（"上次我们定的那个方案是什么来着"这种规则永远追不上的说法），
   又不会让模型凭常识替我们决定"库里有没有"（那就是 strict 的老坑）。
   三类结论一律当 company → 查库：模型超时/报错、返回了不认识的类别、拿不准。

  分类结果进程内缓存 1 小时（同一条消息第二次 0 成本）。

## 部门提示只是提示

department_hint（年假→hr、报销→finance…）**不参与候选集裁剪**，
只用于解释与日志：模型猜错一次（把 general 文档猜成 tech）会让整份文档召不回，
这个代价远大于"检索范围没被收窄"。
"""
import os
import re
import time

from pydantic import BaseModel, Field

from app.prompts.rag import INTENT_CATEGORY_SYSTEM, INTENT_SYSTEM
from app.infra.trace import trace_step
from app.core.logger import logger

# recall_first（纯规则，召回优先）
# hybrid     （默认：规则快速通道 + 知识信号直通 + 其余交给模型判"消息类别"）
# strict     （旧的"规则+模型"判定）
RAG_INTENT_MODE = (os.getenv("RAG_INTENT_MODE", "hybrid") or "hybrid").lower()

# 模型判出的类别 → 要不要查库。**这个映射是代码写死的**：
# 模型只负责"这句话是哪一类"，"哪一类要查库"由这里决定 ——
# 不让模型判"该不该查库"，因为它不知道公司库里有什么（见模块开头的历史坑）。
_CATEGORY_POLICY: dict[str, bool] = {
    "chitchat": False,     # 闲聊寒暄
    "memory": False,       # 问对话记忆 / 助手自身
    "self_intro": False,   # 自我介绍（存画像）
    "action": True,        # 要执行动作 → 可能要先查制度
    "company": True,       # 可能涉及公司内部资料
    "general": True,       # 通用知识：也查（拿不准就查，召回优先的本意）
}
# 分类结果缓存：同一条消息第二次不再调模型（进程内即可，分类很便宜、也不怕各实例不一致）
_CATEGORY_CACHE: dict[str, tuple[float, str, str]] = {}
_CATEGORY_TTL = float(os.getenv("INTENT_CATEGORY_TTL", "3600"))
_CATEGORY_CACHE_MAX = int(os.getenv("INTENT_CATEGORY_CACHE_MAX", "500"))

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
    decision_source: str = Field(default="recall-first", description="recall-first / skip-* / hybrid-* / strict-* / forced")
    reason: str = Field(default="", description="人话解释，随 SSE 下发到前端")
    # hybrid 模式专用：模型判出的消息类别（chitchat/memory/...）与判定耗时，
    # 都进全链路追踪 —— "这次为什么（没）查库"要能看到是哪一层做的决定
    category: str | None = Field(default=None, description="hybrid：模型判出的消息类别")
    classify_ms: int = Field(default=0, description="hybrid：模型分类耗时（0=走了规则快速通道/缓存）")


class _MessageCategory(BaseModel):
    category: str = Field(description="chitchat / memory / self_intro / action / company / general 之一")
    reason: str = Field(default="", description="一句话依据")


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


def _has_knowledge_signal(msg: str) -> bool:
    """消息里有没有"该查库"的强信号（知识型关键词 / 查、搜动作）。

    有 → 直接查，**不花模型的钱**（这也是召回优先的本意）；
    没有 → 才交给模型判类别（hybrid 模式）。
    """
    if any(kw in msg for kw in _QUERY_KW):
        return True
    return any(v in msg for v in _LOOKUP_VERBS)


async def _model_category(msg: str) -> tuple[str, str, int, bool]:
    """让模型判"这条消息属于哪一类"，返回 (类别, 依据, 耗时ms, 是否命中缓存)。

    失败 / 超时 / 认不出的类别 → 一律当 company（查库）：
    宁可多查一次库（¥0.00006、几百毫秒），也不能因为分类器挂了就漏答。
    """
    now = time.time()
    hit = _CATEGORY_CACHE.get(msg)
    if hit and now - hit[0] < _CATEGORY_TTL:
        return hit[1], hit[2], 0, True

    started = time.time()
    try:
        from app.models.llm import create_chat_model
        model = create_chat_model(temperature=0, streaming=False)
        result = await model.with_structured_output(
            _MessageCategory, method="function_calling").ainvoke([
                {"role": "system", "content": INTENT_CATEGORY_SYSTEM},
                {"role": "user", "content": msg},
            ])
        category = (result.category or "").strip().lower()
        reason = (result.reason or "").strip()[:120]
        if category not in _CATEGORY_POLICY:
            logger.warn("intent: 模型返回了未知类别，按 knowledge 处理",
                        {"category": category[:40], "msg": msg[:40]})
            category, reason = "company", f"类别不可识别（{category[:20]}），按召回优先处理"
    except Exception as err:  # noqa: BLE001 - 分类失败绝不能拦住对话
        logger.warn("intent: 消息分类失败，按召回优先处理", {"error": str(err)[:160]})
        return "company", "分类失败，按召回优先处理", int((time.time() - started) * 1000), False

    elapsed = int((time.time() - started) * 1000)
    # 简单的容量控制：满了清一半（分类很便宜，重建代价可忽略）
    if len(_CATEGORY_CACHE) >= _CATEGORY_CACHE_MAX:
        for k in list(_CATEGORY_CACHE)[: _CATEGORY_CACHE_MAX // 2]:
            _CATEGORY_CACHE.pop(k, None)
    _CATEGORY_CACHE[msg] = (now, category, reason)
    return category, reason, elapsed, False


async def _hybrid_classify(message: str) -> IntentDecision:
    """混合判定：规则快速通道 → 知识信号直通 → 其余交给模型判类别。

    为什么要分三层（而不是直接让模型判"该不该查库"）：
    模型不知道公司库里有什么，凭常识判"要不要检索"必然出错（见模块开头的历史坑）。
    这里让模型只回答"这句话是哪一类"，类别 → 检索与否由 _CATEGORY_POLICY 映射 ——
    泛化能力用上了，判断权还在代码手里。而"有知识关键词/查搜动作"的消息压根不问模型，
    省掉那次调用（实测模型分类 ≈742ms / ¥0.0003）。
    """
    msg = message.strip()
    department, hits = _department_hint(msg)

    if not msg:
        return IntentDecision(need_knowledge=False, query=msg,
                              decision_source="skip-empty", reason="空消息")
    if len(msg) <= 1:
        return IntentDecision(need_knowledge=False, query=msg, rule_hit=hits,
                              department_hint=department, decision_source="skip-too-short",
                              reason="消息过短，没有可检索的内容")

    # 第一层：高置信快速通道（零成本，判错代价极低）
    skipped = _skip_reason(msg)
    if skipped:
        return IntentDecision(need_knowledge=False, query=msg, rule_hit=hits,
                              department_hint=department, decision_source="skip-" + skipped,
                              reason=f"识别为「{skipped}」，与知识库无关")

    # 第二层：有知识信号 → 直接查，不问模型
    if _has_knowledge_signal(msg):
        return IntentDecision(
            need_knowledge=True, query=msg, department_hint=department, rule_hit=hits,
            decision_source="hybrid-signal",
            reason="命中知识信号（制度关键词/查搜动作），直接检索知识库"
                   + (f"；推断部门：{department}" if department else ""))

    # 第三层：没有信号 → 模型判类别，拿不准就查（见 _CATEGORY_POLICY）
    category, why, elapsed, cached = await _model_category(msg)
    need = _CATEGORY_POLICY[category]
    return IntentDecision(
        need_knowledge=need, query=msg, department_hint=department, rule_hit=hits,
        decision_source=f"hybrid-model-{category}", category=category, classify_ms=elapsed,
        reason=(f"模型判定类别「{category}」"
                + ("（命中缓存）" if cached else f"（耗时 {elapsed}ms）")
                + f"：{why} → " + ("检索知识库" if need else "不需要查公司文档")),
    )


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
        # hybrid 专用：这次是规则快速通道 / 知识信号 / 还是模型判的类别（+耗时）
        "mode": RAG_INTENT_MODE,
        "category": decision.category,
        "classifyMs": decision.classify_ms,
    })
    return decision


async def _classify_intent(message: str) -> IntentDecision:
    if RAG_INTENT_MODE == "hybrid":
        return await _hybrid_classify(message)
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
