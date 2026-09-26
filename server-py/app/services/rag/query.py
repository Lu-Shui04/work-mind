# server-py/app/services/rag/query.py
"""
RAG 检索：带权限 / 版本 / 范围过滤的检索 + 引用溯源 + **未命中原因诊断**。

过滤策略：**先过滤（下推）再算相似度**
1. 权限/版本/范围条件全部写进 SQL 的 WHERE（见 identity.doc_visibility_sql），
   数据库在算余弦距离之前就把不可见切片剔掉 —— 既安全，也不会"TopK 被过滤完导致无结果"
2. 命中后再用 Python 的 can_view / version_visible 复核一遍（最终裁决，安全底线不依赖单点）

这次的三个召回修复（都是实证出来的，别再改回去）：
A. **阈值必须按 embedding 模型标定**：智谱 embedding-3 上，同一段文字近乎逐字重复
   也只到 0.57，语义相关换个说法只有 0.42~0.45。原来写死 0.5 的结果是
   "库里明明有、权限也没问题，却一条都召不回"，界面上还显示成"当前身份下未命中内容"，
   把排查方向带偏到权限上。现在默认 0.35，且用环境变量暴露出来。
B. **意图模型猜的部门不能当硬过滤**：'rag召回失败怎么办' 会被猜成 tech，
   而文档是 general，硬过滤直接 0 条。部门提示现在只做"提示/解释"，不参与候选集裁剪；
   只有用户在前端明确选了部门才是硬过滤。
C. **未命中必须能自证原因**：返回 kb_empty / all_filtered / below_threshold 与
   最高分、候选数、阈值，前端分开显示，不用再翻代码猜。
"""
import os

from langchain_core.prompts import ChatPromptTemplate

from app.schemas.document import DocumentRecord
from app.services.identity import SqlParams, User, can_view, doc_visibility_sql, version_visible
from app.services.model import chat_model, embeddings
from app.services.rag import rerank
from app.services.rag.registry import registry
from app.services.rag.vectorstore import get_vector_store
from app.utils.logger import logger

# 相似度阈值（绝对下限）：低于此值的切片不纳入参考。
# 必须随 embedding 模型重新标定（不同模型的相似度分布不同），所以走环境变量。
SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.35"))
# 相对阈值：只保留 ≥ (最高分 - MARGIN) 的切片，用于"库里同一话题文档很多"时收敛噪音。
# 默认 0 = 关闭（关闭时只看绝对阈值，行为最可预测）。
SIMILARITY_MARGIN = float(os.getenv("RAG_SIMILARITY_MARGIN", "0"))
# 返回条数：4 条对"制度问答"偏少，容易刚好漏掉答案所在的那片
DEFAULT_TOP_K = int(os.getenv("RAG_TOP_K", "6"))

# 检索不到内容时怎么回答：
#   grounded（默认，知识优先）—— 明确说"知识库中未找到相关内容"，**不调用模型**，
#                                也就不会拿模型自身知识去顶，避免与公司资料口径不一致；
#   fallback（旧的）           —— 没有资料就交给模型凭自身知识正常回答。
# 想要"先查库、命中就用、没命中照常答"就设成 fallback。
ANSWER_POLICY = (os.getenv("RAG_ANSWER_POLICY", "grounded") or "grounded").lower()


def no_knowledge_reply(recall: dict | None = None) -> str:
    """知识优先策略下的固定答复：把"没找到"说清楚，并给出可执行的下一步。"""
    r = recall or {}
    reason = r.get("reason")
    lines = ["知识库中未找到相关内容。"]

    # 用户自己收窄了检索范围时，先把这个原因说清楚 ——
    # 否则"没找到"会被误读成"库里没有"，而实际是自己把范围选窄了
    flt = r.get("appliedFilters") or {}
    explicit = []
    if flt.get("department"):
        explicit.append("归属部门=" + flt["department"])
    if flt.get("docType"):
        explicit.append("文档类型=" + flt["docType"])
    if flt.get("version"):
        explicit.append("版本=" + flt["version"])

    if explicit:
        lines.append("检索情况：当前检索范围被限制在「" + "、".join(explicit) + "」，"
                     "这个范围内没有匹配到内容。把上方的「检索范围」切回「全部可见」再试一次。")
    elif reason == "below_threshold":
        best, cutoff = r.get("bestScore"), r.get("threshold")
        lines.append(f"检索情况：有 {r.get('candidates', 0)} 条候选片段，但最高相似度 "
                     f"{best} 低于阈值 {cutoff}，没有足够的依据作答。")
    elif reason == "all_filtered":
        lines.append("检索情况：知识库里确实有内容，但都被权限或版本条件过滤掉了"
                     "（可在右上角切换身份验证，或检查文档的归属部门 / 密级 / 生效日期）。")
    elif reason == "kb_empty":
        lines.append("检索情况：知识库当前没有任何切片 —— 文档可能还没入库，"
                     "或换库之后没有重新入库。")
    elif reason in ("storage_unavailable", "embedding_unavailable", "unavailable"):
        lines.append(f"检索情况：知识库暂时不可用 —— {r.get('explain') or '请稍后重试'}。")
    elif r.get("explain"):
        lines.append(f"检索情况：{r['explain']}。")

    lines.append("可以试试：\n"
                 "· 换个更具体的说法再问一次（尽量用文档里出现过的词）\n"
                 "· 到「知识库」页确认这份资料已入库、并且已生效\n"
                 "· 如果这个问题本来就不需要公司资料，把输入框下方的「知识库」切到「关闭」，"
                 "我就用通用知识回答；想让「查不到也照常答」成为默认，"
                 "把后端 RAG_ANSWER_POLICY 设为 fallback")
    return "\n\n".join(lines)


class SearchFilters:
    """检索过滤条件（来自前端筛选器或意图理解结果）。

    department      —— 用户在前端明确选的部门：**硬过滤**（尊重用户选择）
    department_hint —— 意图模型猜的部门：**只做提示，不裁剪候选集**
                        （猜错一次就会让整份 general/公开文档召不回，代价太大）
    """

    def __init__(self, department: str | None = None, department_hint: str | None = None,
                 version: str | None = None, doc_type: str | None = None,
                 doc_ids: list[str] | None = None, include_superseded: bool = False):
        self.department = department
        self.department_hint = department_hint
        self.version = version
        self.doc_type = doc_type
        self.doc_ids = doc_ids
        self.include_superseded = include_superseded

    def describe(self) -> dict:
        return {
            "department": self.department, "departmentHint": self.department_hint,
            "version": self.version, "docType": self.doc_type, "docIds": self.doc_ids,
            "includeSuperseded": self.include_superseded,
        }


def _visible(doc: DocumentRecord | None, user: User, filters: SearchFilters) -> bool:
    if doc is None:
        return False
    if not can_view(doc, user):
        return False
    if filters.doc_ids and doc.doc_id not in filters.doc_ids:
        return False
    if filters.department and doc.department != filters.department:
        return False
    if filters.doc_type and doc.doc_type != filters.doc_type:
        return False
    return version_visible(doc, version=filters.version, include_superseded=filters.include_superseded)


def _cutoff(best: float | None) -> float:
    """实际生效的分数门槛。"""
    if SIMILARITY_MARGIN > 0 and best is not None:
        return max(SIMILARITY_THRESHOLD, best - SIMILARITY_MARGIN)
    return SIMILARITY_THRESHOLD


async def retrieve_with_meta(question: str, user: User, k: int | None = None,
                             filters: SearchFilters | None = None) -> tuple[list[dict], dict]:
    """检索并返回 (命中切片, 诊断信息)。诊断信息用于回答"为什么没命中"。"""
    filters = filters or SearchFilters()
    k = k or DEFAULT_TOP_K
    if not embeddings:
        raise ValueError("未配置 ZHIPU_API_KEY / OPENAI_API_KEY，无法使用知识库检索")

    store = get_vector_store()
    total = await store.count(user.tenant_id)          # 当前租户共有多少切片
    query_vec = await embeddings.aembed_query(question)

    # 过滤条件下推：SQL 先剔除不可见/非生效版本，再算相似度
    p = SqlParams()
    where_sql, _ = doc_visibility_sql(
        user, department=filters.department, version=filters.version,
        doc_type=filters.doc_type, doc_ids=filters.doc_ids,
        include_superseded=filters.include_superseded, params=p,
    )
    # 开启重排时放宽向量召回池（宽召回 → 精排 → 取前 k）：
    # 向量只负责"别漏"，排序交给重排模型，这是重排能提效果的前提
    pool_k = rerank.pool_size(k)
    results, sql_meta = await store.search(query_vec, where_sql=where_sql, params=p.values, k=pool_k)

    # 最终裁决：命中后再用 Python 的 can_view / version_visible 复核一遍（安全底线不依赖 SQL 单点）
    docs_meta = await registry.get_many([c.doc_id for c, _ in results])
    best = sql_meta.get("bestScore")
    cutoff = _cutoff(best)

    hits: list[dict] = []
    for chunk, score in results:
        doc = docs_meta.get(chunk.doc_id)
        if not _visible(doc, user, filters):
            continue
        if score < cutoff:
            continue
        hits.append({
            "chunkId": chunk.chunk_id,
            "docId": chunk.doc_id,
            "content": chunk.text,
            "score": round(score, 3),
            # 重排分（下面填充）：向量分只说明"整体像"，重排分才说明"能不能回答"
            "rerankScore": None,
            "title": doc.document_title if doc else "未知来源",
            # 引用溯源：页码区间 + 结构类型 + 顺位，前端可据此高亮原文
            "pageNumber": chunk.page_number,
            "pageEnd": chunk.page_end or chunk.page_number,
            "pageLabel": (f"第{chunk.page_number}-{chunk.page_end}页"
                          if chunk.page_end and chunk.page_end != chunk.page_number
                          else (f"第{chunk.page_number}页" if chunk.page_number else "无页码")),
            "elementType": chunk.element_type,
            "orderIndex": chunk.order_index,
            # 章节路径：引用时显示"出自哪一章哪一节"，比只给页码有用得多
            "headingPath": list(chunk.heading_path or []),
            "department": doc.department if doc else chunk.department,
            "version": doc.version if doc else chunk.version,
            "docType": doc.doc_type if doc else chunk.doc_type,
            "securityLevel": doc.security_level if doc else chunk.security_level,
            "fileName": doc.file_name if doc else None,
            "preview": chunk.text[:80].replace("\n", " ") + "...",
        })

    # ── 重排：cross-encoder / LLM 逐条读"查询+片段"，把真正能回答的排到前面 ──
    # 实测向量分对"同主题但不回答该问题"的切片几乎没有区分度（差 0.02~0.05），
    # 这一步是 Top1 准确率的主要来源。失败就保留向量序（fail-open），不影响可用性。
    rerank_meta: dict = {"applied": False, "provider": rerank.PROVIDER, "pool": pool_k}
    if rerank.is_enabled() and len(hits) > 1:
        scores = await rerank.rerank(question, [h["content"] for h in hits])
        if scores:
            for h, s in zip(hits, scores):
                h["rerankScore"] = round(s, 3)
            hits.sort(key=lambda h: (-(h.get("rerankScore") if h.get("rerankScore") is not None else -1),
                                     -h["score"]))
            rerank_meta.update({"applied": True, "candidates": len(scores)})
    hits = hits[:k]

    # ── 未命中原因诊断：把"没查到"翻译成人能直接行动的三类原因 ──────────
    candidates = sql_meta.get("candidates", 0)
    if hits:
        reason, explain = "ok", f"命中 {len(hits)} 条"
    elif total == 0:
        reason = "kb_empty"
        explain = "知识库当前没有任何切片（未入库，或换库/重启后没有重新入库）"
    elif candidates == 0:
        reason = "all_filtered"
        explain = "库里有切片，但都被权限/版本/显式筛选条件过滤掉了"
    else:
        reason = "below_threshold"
        explain = (f"检索到 {candidates} 条候选，但最高相似度 {best} 低于阈值 {cutoff}"
                   "（阈值没按当前 embedding 模型标定）")

    hint = filters.department_hint
    hint_matched = bool(hint) and any(h["department"] == hint for h in hits)
    meta = {
        "hit": len(hits),
        "reason": reason,
        "explain": explain,
        "candidates": candidates,
        "totalChunks": total,
        "bestScore": best,
        "threshold": round(cutoff, 4),
        "absoluteThreshold": SIMILARITY_THRESHOLD,
        "margin": SIMILARITY_MARGIN,
        "k": k,
        "departmentHint": hint,
        "departmentHintMatched": hint_matched,
        "appliedFilters": filters.describe(),
        "rerank": rerank_meta,
    }

    logger.info("rag: retrieve", {
        "question": question[:40], "hit": len(hits), "reason": reason,
        "best": best, "candidates": candidates, "total": total,
        "user": user.user_id, "filters": filters.describe(),
    })
    return hits, meta


async def retrieve(question: str, user: User, k: int = 4,
                   filters: SearchFilters | None = None) -> list[dict]:
    """返回命中切片（含引用定位信息），已按权限与版本过滤。"""
    docs, _ = await retrieve_with_meta(question, user, k=k, filters=filters)
    return docs


# 兼容旧调用（Agent 的 read_doc 工具用）
async def retrieve_docs(question: str, category: str | None = None, k: int = 4,
                        user: User | None = None) -> list[dict]:
    user = user or User()
    filters = SearchFilters(department=category) if category else None
    return await retrieve(question, user, k=k, filters=filters)


RAG_SYSTEM = """你是 WorkMind AI 知识库助手。

规则：
1. 只根据下方提供的参考文档回答问题，不使用文档之外的知识
2. 如果文档中没有相关内容，明确说"知识库中未找到相关内容"
3. 回答要准确、简洁，必要时列出要点
4. 引用时标注来源，格式：【来源：文档标题 · 第N页】"""


def build_context(docs: list[dict]) -> str:
    parts = []
    for i, d in enumerate(docs):
        loc = d.get("pageLabel") or (f"第{d['pageNumber']}页" if d.get("pageNumber") else "无页码")
        parts.append(
            f"[参考{i + 1}] 《{d['title']}》 {loc} 部门:{d['department']} 版本:{d['version']}\n{d['content']}"
        )
    return "\n\n---\n\n".join(parts)


def build_prompt():
    return ChatPromptTemplate.from_messages([
        ("system", RAG_SYSTEM),
        ("human", "参考文档：\n{context}\n\n问题：{question}"),
    ])


async def rag_answer_stream(question: str, docs: list[dict]):
    """给定检索结果，流式生成带引用的回答。"""
    context = build_context(docs)
    chain = build_prompt() | chat_model
    async for chunk in chain.astream({"context": context, "question": question}):
        if chunk.content:
            yield chunk.content
