# server-py/app/services/agent/tools.py
# Agent 工具集：6 个工具，每个都有清晰的 name、description、schema
#
# 工具是模型与现实之间唯一的接口，所以这里守两条底线：
#   1) **看不明白就报错，不要静默猜**。早期版本的 calculate 会把
#      「3天，共580元」这类文本按字符过滤成 "3580" 算出一个错答案，
#      用户看不出任何异常。现在归一化不掉的输入一律明确报错。
#   2) **做不到就直说**。演示数据、没配 key 的服务、模拟发送，都要写进返回值里，
#      否则模型会把"没查到"包装成结论，把"演示成功"当成真的发出去了 ——
#      「查不到和平精英赛季」那次，根因就是搜索工具假装自己搜过了。
import ast
import asyncio
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.infra.resilience import (
    TOOL_MAX_RETRIES, TOOL_TIMEOUT, BreakerOpenError, RetryExhaustedError,
    call_with_resilience,
)
from app.core.logger import logger

# ── 时区 ────────────────────────────────────────────────────────
# 容器默认跑在 UTC："今天是几号"在 UTC 与北京时间之间会差一天
# （北京时间 00:00-08:00 时，UTC 还停在前一天），请假天数、工期、报销时限这类
# 计算会整体错一天。所以日期一律按配置时区算，不用进程本地时间。
TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
# slim 镜像可能没带 tzdata，装不上时的兜底偏移（宁可固定偏移，也不要退回 UTC）
_FALLBACK_OFFSETS = {"Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "UTC": 0}


def _now() -> datetime:
    """当前时间（按 APP_TIMEZONE，默认北京时间），带 tzinfo 便于溯源。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(TIMEZONE_NAME))
    except Exception:  # noqa: BLE001 - 缺 tzdata / 时区名写错都不该让工具挂掉
        hours = _FALLBACK_OFFSETS.get(TIMEZONE_NAME)
        if hours is None:
            logger.warn("tool:get_date unknown timezone, fallback to UTC", {"tz": TIMEZONE_NAME})
            return datetime.now(timezone.utc)
        return datetime.now(timezone.utc) + timedelta(hours=hours)


# ── 工具1：联网搜索 ────────────────────────────────────────────
# 三种后端，按 .env 里配了什么自动选，也可以用 SEARCH_PROVIDER 显式指定：
#   tavily —— TAVILY_API_KEY（国际通用，注册即有免费额度）
#   bocha  —— BOCHA_API_KEY（国内可直连的中文搜索）
#   zhipu  —— ZHIPU_API_KEY（智谱 Web Search；**知识库已经在用这个 key**，
#             所以国内部署往往不需要再申请搜索服务，配好 key 就直接能搜）
#   demo   —— 都没配：返回内置演示数据，并明确标注"这不是真实搜索结果"
#
# 为什么必须区分出 demo 模式：早期版本无论查什么，都返回一句
# 「关于X的搜索结果：该话题在技术社区有广泛讨论」。模型把它当成"搜过了、没搜到"，
# 于是换个关键词反复重试（一次任务烧掉 8 步），最后向用户道歉。
# 用户看到的是"联网搜索坏了"，真实原因是搜索压根没接 —— 工具应该一开始就说清楚。
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
BOCHA_API_KEY = os.getenv("BOCHA_API_KEY")
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
# 智谱搜索的引擎档位：search_std（标准，便宜）/ search_pro（高级，更准也更贵）
ZHIPU_SEARCH_ENGINE = os.getenv("ZHIPU_SEARCH_ENGINE", "search_std")
SEARCH_PROVIDER_ENV = (os.getenv("SEARCH_PROVIDER") or "auto").lower()
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "20"))

# 演示数据：没有配搜索 key 时用的内置结果（示例任务「技术调研」依赖它）
_MOCK_RESULTS = {
    "Vue3": "Vue 3.4.21 是目前最新版本，于2024年3月发布。主要改进：defineModel() 正式稳定，响应式系统性能提升约 56%，编译器优化减少生成代码量。",
    "React": "React 18.3 是最新稳定版，引入了并发渲染、useTransition、Suspense 改进。2024年主要关注点是 React Server Components 的稳定化。",
    "Vite": "Vite 5.2 是目前最新版本，使用 Rollup 4 构建，冷启动速度提升 30%，支持 Lightning CSS。推荐用于新项目。",
    "DeepSeek": "DeepSeek-V3 于2024年12月发布，是目前最强的开源 LLM 之一，性能接近 Claude 3.5 Sonnet，中文表现优秀，API 价格仅为 GPT-4o 的1/10。",
    "TypeScript": "TypeScript 5.7 是最新版本，新增 noUncheckedSideEffectImports 选项，改进了声明文件的处理方式。",
    "微前端": "qiankun 2.x 和 wujie 是国内最流行的微前端框架。wujie 基于 WebComponent + iframe，隔离性更好；qiankun 更成熟，社区更大。",
}

# 关键词 → mock 结果 的别名表。
# 为什么不能直接拿 _MOCK_RESULTS 的 key 做包含匹配：模型爱写 "Vue 3 最新版本"（Vue 和 3 中间有空格），
# 而 key 是 "Vue3"，直接 in 判断匹配不到，只能返回兜底话术 ——
# 报告里就会出现"搜索未返回可靠的版本信息"，看起来就像"联网搜索坏了"。
# 所以先归一化（去空格/连字符/点、转小写）再匹配。
_MOCK_ALIASES: list[tuple[str, str]] = [
    ("vue", "Vue3"),
    ("react", "React"),
    ("vite", "Vite"),
    ("deepseek", "DeepSeek"),
    ("typescript", "TypeScript"),
    ("qiankun", "微前端"),
    ("wujie", "微前端"),
    ("微前端", "微前端"),
]


def _normalize_query(text: str) -> str:
    return re.sub(r"[\s\-_./\\·、,，]+", "", (text or "").lower())


def _mock_lookup(query: str) -> str | None:
    """按别名表匹配 mock 数据；匹配不到返回 None（调用方给兜底话术）。"""
    q = _normalize_query(query)
    key = next((k for alias, k in _MOCK_ALIASES if alias in q), None)
    return _MOCK_RESULTS.get(key) if key else None


def search_provider() -> str:
    """当前生效的搜索后端：tavily / bocha / zhipu / demo。"""
    if SEARCH_PROVIDER_ENV in ("tavily", "bocha", "zhipu", "demo"):
        return SEARCH_PROVIDER_ENV
    if TAVILY_API_KEY:
        return "tavily"
    if BOCHA_API_KEY:
        return "bocha"
    if ZHIPU_API_KEY:
        return "zhipu"
    return "demo"


class WebSearchArgs(BaseModel):
    query: str = Field(description='搜索关键词，尽量精确，如"Vue3最新版本"而不是"前端框架"')


async def _search_tavily(query: str, count: int) -> list[dict]:
    payload = {"api_key": TAVILY_API_KEY, "query": query,
               "max_results": count, "search_depth": "basic"}
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        resp = await client.post("https://api.tavily.com/search", json=payload)
        resp.raise_for_status()
        data = resp.json()
    return [{"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
            for r in (data.get("results") or []) if isinstance(r, dict)]


def _bocha_items(data: dict) -> list[dict]:
    """博查的返回结构在不同版本/网关下略有差异，这里按已知的几种形状取，取不到就返回空。"""
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    pages = payload.get("webPages") or payload.get("webpages") or {}
    items = pages.get("value") or pages.get("values") or []
    if not items and isinstance(payload.get("results"), list):
        items = payload["results"]
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": it.get("name") or it.get("title"),
            "url": it.get("url") or it.get("link"),
            "content": it.get("summary") or it.get("snippet") or it.get("content"),
        })
    return out


async def _search_bocha(query: str, count: int) -> list[dict]:
    payload = {"query": query, "summary": True, "count": count}
    headers = {"Authorization": "Bearer " + (BOCHA_API_KEY or ""),
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        resp = await client.post("https://api.bochaai.com/v1/web-search",
                                 json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return _bocha_items(data)


def _zhipu_items(data: dict) -> list[dict]:
    """智谱 Web Search 返回 {search_result: [{title, content, link, publish_date, media}]}。

    link 常常是空串（有些结果来自公众号/信息流，没有可点的原文地址），
    这时用 media（来源站点）兜底，别让引用里出现 "来源：未知"。
    """
    items = data.get("search_result") or data.get("data") or []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": it.get("title") or "无标题",
            "url": it.get("link") or (f"（来源：{it['media']}）" if it.get("media") else ""),
            "content": it.get("content") or "",
            "published": it.get("publish_date") or "",
        })
    return out


async def _search_zhipu(query: str, count: int) -> list[dict]:
    """智谱 Web Search（用知识库同一个 ZHIPU_API_KEY，通常不需要额外申请服务）。"""
    payload = {"search_engine": ZHIPU_SEARCH_ENGINE, "search_query": query}
    headers = {"Authorization": "Bearer " + (ZHIPU_API_KEY or ""),
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        resp = await client.post("https://open.bigmodel.cn/api/paas/v4/web_search",
                                 json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return _zhipu_items(data)[:count]


def _format_results(query: str, results: list[dict]) -> str:
    lines = [f'联网搜索"{query}"，返回 {len(results)} 条结果（按相关性排序）：']
    for i, r in enumerate(results, 1):
        text = re.sub(r"\s+", " ", (r.get("content") or "")).strip()
        when = f"（{r['published']}）" if r.get("published") else ""
        lines.append(f"[{i}] {r.get('title') or '无标题'}{when}\n"
                     f"    {text[:400]}\n    来源：{r.get('url') or '未知'}")
    lines.append("以上是实时搜索结果；回答时请注明信息的时间/来源，不要与自己的旧知识混淆。")
    return "\n".join(lines)


@tool("web_search", args_schema=WebSearchArgs)
async def search_tool(query: str) -> str:
    """搜索互联网，获取实时信息（时事、赛事、游戏版本与赛季、产品价格等）以及技术资讯、版本信息、最佳实践。当需要了解某个技术或事件的最新状态、或不确定某个信息时使用。返回结果带发布时间，回答时请注明时间。"""
    provider = search_provider()
    logger.info("tool:search", {"query": query, "provider": provider})

    if provider in ("tavily", "bocha", "zhipu"):
        backend = {"tavily": _search_tavily, "bocha": _search_bocha, "zhipu": _search_zhipu}[provider]
        try:
            results = await backend(query, SEARCH_MAX_RESULTS)
        except Exception as err:  # noqa: BLE001 - 搜索失败不该让整次任务崩掉
            logger.warn("tool:search failed", {"provider": provider, "error": str(err)[:200]})
            return (f'联网搜索失败（{provider}）：{err}。'
                    "可以稍后重试；如果这个问题不依赖实时信息，也可以直接回答。")
        if not results:
            return f'联网搜索（{provider}）没有返回"{query}"的结果，可以换更具体的关键词再试一次。'
        return _format_results(query, results)

    # ── demo 模式（没配搜索 key）──────────────────────────────
    hit = _mock_lookup(query)
    if hit:
        return f"[演示数据 · 非真实联网] {hit}"
    return (f'[演示环境 · 未接入真实联网搜索] 无法查询"{query}"的实时信息。\n'
            "这不是搜过了没搜到，而是本环境没有配置搜索服务：请如实告知用户该信息需要联网查询，"
            "不要凭记忆编造，也不要反复更换关键词重试。\n"
            "（管理员在 .env 里配 TAVILY_API_KEY / BOCHA_API_KEY / ZHIPU_API_KEY 任一即可启用真实搜索 —— "
            "ZHIPU_API_KEY 通常已经为知识库配好了，直接复用即可）")


# ── 工具2：读取知识库文档 ──────────────────────────────────────
class ReadDocArgs(BaseModel):
    question: str = Field(description="要查询的问题或关键词")


@tool("read_doc", args_schema=ReadDocArgs)
async def read_doc_tool(question: str) -> str:
    """从公司知识库检索文档内容。用于查询公司内部规定、产品手册、技术文档等。当问题涉及公司内部信息时优先使用。"""
    logger.info("tool:read_doc", {"question": question})

    try:
        # 关键：必须用"当前调用者"的身份检索，否则会绕过权限把别的部门文档喂给模型
        from app.core.identity import get_current_user
        from app.services.rag.query import SearchFilters, retrieve_with_meta

        user = get_current_user()
        docs, recall = await retrieve_with_meta(question, user, k=6, filters=SearchFilters())
        # 命中切片回传给上层 → 前端「引用来源」面板（Agent 页也能看到引了哪几份文档）
        record_tool_sources(docs, recall)

        if not docs:
            # 把"为什么没找到"如实带回去（库空 / 被权限过滤 / 分数低于阈值 / 重排判定答不了），
            # 否则模型只能笼统地说"没找到"，用户也不知道该改权限还是换个说法
            why = (recall or {}).get("explain") or "没有可用内容"
            return (f'知识库中未找到关于"{question}"的相关内容（{why}）。'
                    f'检索范围：租户 {user.tenant_id}，部门 {"/".join(user.departments)}，'
                    f'密级 {user.clearance}。可以换个更具体的说法再试，'
                    "或告知用户该信息不在其权限范围内。")

        return "\n\n".join(
            f"[文档{i + 1}]《{d['title']}》第{d['pageNumber']}页（部门:{d['department']} 版本:{d['version']}）：{d['content']}"
            for i, d in enumerate(docs)
        )
    except Exception as err:  # noqa: BLE001 - 工具失败要变成可读文本，而不是把异常抛给图
        logger.warn("tool:read_doc failed", {"error": str(err)})
        return "知识库暂时不可用，请稍后重试。"


# ── 工具3：数学计算 ────────────────────────────────────────────
class CalculateArgs(BaseModel):
    expression: str = Field(description='纯数学表达式，如 "1500 + 800 * 0.8" 或 "(200 + 350) * 3"（不要带中文说明和单位）')

# 只允许这几种运算：** 不在其中 —— 避免 "9**9**9" 这种把进程算死的表达式
_ALLOWED_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)
# 可以安全丢掉的单位/货币词（丢掉它们不会让两个数字粘在一起）
_UNIT_WORDS = ("人民币", "美元", "元", "块", "rmb", "cny", "usd", "¥", "￥", "$")
MAX_EXPRESSION_LEN = 200


def _normalize_expression(raw: str) -> str:
    """归一化成纯算式；归一化不掉的字符原样留着，交给调用方报错（绝不静默丢弃）。"""
    s = raw or ""
    # 全角 → 半角（１２３＋－×÷，（））
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = (s.replace("　", " ").replace("×", "*").replace("÷", "/")
           .replace("－", "-").replace("＋", "+").replace("，", ",").replace("。", ""))
    # 千分位：1,500 → 1500
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    for w in _UNIT_WORDS:
        s = s.replace(w, "")
    s = s.replace(",", "")
    # "%" 有两种含义：后面跟数字是取模（1500 % 7），否则是百分比（20% → 20/100）。
    # 必须先用占位符把取模的 % 保护起来 —— 否则下面统一的 "%"→"/100" 会把取模也改成除以 100，
    # "1500 % 7" 就变成 "1500 /100 7" 了。
    s = re.sub(r"%\s*(?=\d)", " __MOD__ ", s)
    s = s.replace("%", "/100")
    s = s.replace("__MOD__", "%")
    return re.sub(r"\s+", " ", s).strip()


def _eval_node(node: ast.AST) -> float:
    """在 AST 上求值：只认数字、括号、四则运算与取模。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("表达式里只能出现数字")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_OPS):
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
            raise ZeroDivisionError("除数是 0")
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left % right
    raise ValueError("包含不支持的运算（只支持 + - * / % 和括号）")


def _format_number(value: float) -> str:
    rounded = round(float(value), 6)
    return str(int(rounded)) if float(rounded).is_integer() else str(rounded)


@tool("calculate", args_schema=CalculateArgs)
async def calculate_tool(expression: str) -> str:
    """执行数学计算，支持加减乘除、取模、括号、百分比、千分位与全角数字。用于需要精确计算数值的场景，比如报销金额合计、工作日计算等。"""
    logger.info("tool:calculate", {"expression": expression})

    if not (expression or "").strip():
        return "无效的数学表达式：内容为空"
    if len(expression) > MAX_EXPRESSION_LEN:
        return f"表达式过长（超过 {MAX_EXPRESSION_LEN} 字符），请只传算式本身"

    safe_expr = _normalize_expression(expression)

    # 归一化后剩下的非法字符直接报错。早期版本在这里"过滤掉非法字符"，
    # 结果 "3天 580元" 被拼成 "3580" 算出个错数，用户完全看不出来。
    illegal = sorted({c for c in safe_expr if c not in "0123456789+-*/().% "})
    if illegal:
        return (f"无法识别的表达式：{expression}（包含无法处理的字符 {' '.join(illegal)}）。"
                '请只传纯算式，例如 "580 * 3 + 1200"；文字说明和单位请放在算式之外。')
    if re.search(r"\d\s+\d", safe_expr):
        return (f"无法识别的表达式：{expression}（数字之间只有空格，看起来是两段数字）。"
                '请只传一个算式，例如 "1500 + 800 * 0.8"。')

    try:
        tree = ast.parse(safe_expr, mode="eval")
    except SyntaxError:
        return f"无法识别的表达式：{expression}"

    try:
        result = _eval_node(tree)
    except ZeroDivisionError as err:
        return f"计算失败：{err}"
    except ValueError as err:
        return f"计算失败：{err}"
    except Exception as err:  # noqa: BLE001
        return f"计算失败：{err}"

    return f"计算结果：{expression} = {_format_number(result)}"


# ── 工具4：获取日期信息 ─────────────────────────────────────────
class GetDateArgs(BaseModel):
    operation: Literal["today", "diff", "add_days"] = Field(
        description="today=获取今天日期, diff=计算日期差, add_days=日期加减")
    date1: str | None = Field(default=None, description="开始日期，格式 YYYY-MM-DD")
    date2: str | None = Field(default=None, description="结束日期（格式 YYYY-MM-DD）；operation=add_days 时传天数（整数，可为负）")


_WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]


def _parse_date(value: str | None) -> datetime | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


@tool("get_date", args_schema=GetDateArgs)
async def get_date_tool(operation: str, date1: str | None = None, date2: str | None = None) -> str:
    """获取日期信息：查询今天日期（按东八区）、计算两个日期之间的天数和工作日数、日期加减。用于请假天数计算、项目工期估算、"还差几天"这类倒计时。"""
    logger.info("tool:get_date", {"operation": operation, "date1": date1, "date2": date2, "tz": TIMEZONE_NAME})

    now = _now()

    if operation == "today":
        return (f"今天是 {now.year}年{now.month}月{now.day}日，星期{_WEEKDAY_CN[now.weekday()]}"
                f"（时区 {TIMEZONE_NAME}）")

    if operation == "diff":
        d1, d2 = _parse_date(date1), _parse_date(date2)
        if d1 is None or d2 is None:
            return "日期格式不正确，请使用 YYYY-MM-DD 格式（例如 2026-09-26）"

        diff_days = abs((d2 - d1).days)
        start, end = (d1, d2) if d1 < d2 else (d2, d1)
        workdays = 0
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                workdays += 1
            cursor += timedelta(days=1)

        return (f"{start.strftime('%Y-%m-%d')} 到 {end.strftime('%Y-%m-%d')}："
                f"共 {diff_days} 天，其中工作日 {workdays} 天")

    if operation == "add_days":
        base = _parse_date(date1)
        if base is None:
            return "日期格式不正确，请使用 YYYY-MM-DD 格式（例如 2026-09-26）"
        try:
            days = int(str(date2).strip())
        except (TypeError, ValueError):
            return f"天数必须是整数，收到：{date2}"
        target = base + timedelta(days=days)
        return (f"{base.strftime('%Y-%m-%d')} 加 {days} 天后是 "
                f"{target.strftime('%Y-%m-%d')}，星期{_WEEKDAY_CN[target.weekday()]}")

    return f"今天是 {now.strftime('%Y-%m-%d')}"


# ── 工具5：生成并保存报告 ─────────────────────────────────────
class WriteReportArgs(BaseModel):
    title: str = Field(description="报告标题")
    content: str = Field(description="报告正文内容，使用 Markdown 格式")
    format: Literal["markdown", "plain"] | None = Field(default="markdown")


def _to_plain(text: str) -> str:
    """把 Markdown 的标题标记/强调符号去掉，产出纯文本正文。"""
    out = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        out.append(line)
    return "\n".join(out).strip()


@tool("write_report", args_schema=WriteReportArgs)
async def write_report_tool(title: str, content: str, format: str | None = "markdown") -> str:
    """将分析结果整理成结构化报告并保存。当需要输出最终分析报告时使用，确保在收集到所有信息后才调用此工具。"""
    logger.info("tool:write_report", {"title": title, "format": format})

    timestamp = _now().strftime("%Y-%m-%d %H:%M:%S")
    plain = format == "plain"
    body = _to_plain(content) if plain else (content or "").strip()
    header = title if plain else f"# {title}"
    meta = f"生成时间：{timestamp}" if plain else f"> 生成时间：{timestamp}"
    footer = "" if plain else "\n\n---\n*由 WorkMind AI Agent 自动生成*"
    report = f"{header}\n\n{meta}\n\n{body}{footer}"

    return json.dumps({
        "success": True,
        "title": title,
        "format": format or "markdown",
        "content": report,
        "savedAt": timestamp,
        "message": f"报告「{title}」已生成，共 {len(report)} 字",
    }, ensure_ascii=False)


# ── 工具6：发送通知 ────────────────────────────────────────────
class SendNotifyArgs(BaseModel):
    to: str = Field(description='接收人，如"张三"或"tech-team"')
    subject: str = Field(description="消息主题")
    message: str = Field(description="消息正文（简洁）")
    channel: Literal["email", "feishu", "dingtalk"] | None = Field(default="feishu")


@tool("send_notify", args_schema=SendNotifyArgs)
async def send_notify_tool(to: str, subject: str, message: str, channel: str | None = "feishu") -> str:
    """生成并发送消息通知（邮件/飞书/钉钉）。用于任务完成后通知相关人员，或发送报告摘要。注意：当前是演示环境，只生成通知内容并记录，不会真正投递到第三方平台。"""
    logger.info("tool:send_notify", {"to": to, "subject": subject, "channel": channel})

    await asyncio.sleep(0.2)  # 模拟网络延迟
    timestamp = _now().isoformat()

    # 如实标注"演示环境未真实投递"：早期版本直接回"通知已通过飞书发送给 X"，
    # 模型会把这句话原样写进最终回答，用户以为真的发出去了。
    return json.dumps({
        "success": True,
        "simulated": True,
        "to": to,
        "subject": subject,
        "channel": channel,
        "sentAt": timestamp,
        "message": f"已生成发给「{to}」的 {channel} 通知（演示环境：未真实投递，仅生成内容）",
        "content": message,
    }, ensure_ascii=False)


# 导出所有工具（供 AgentService 使用）
all_tools = [
    search_tool,
    read_doc_tool,
    calculate_tool,
    get_date_tool,
    write_report_tool,
    send_notify_tool,
]


# ── 重复调用防护 ────────────────────────────────────────────────
# 模型会陷进"换个说法再搜一遍"的循环：实测一次任务发了 14 次 web_search（8 步全在搜），
# 步数、搜索费用、上下文长度全烧在重复调用上，最后还是没收敛。
# 这里按「工具名 + 参数」记录本次任务内的调用，第二次起直接回一句"已查过"，
# 把模型推回收敛，而不是让它继续换关键词。
# 用 ContextVar：一次任务一份记录，并发任务之间不会串号。
_tool_call_log: ContextVar[dict | None] = ContextVar("workmind_tool_call_log", default=None)


def set_tool_call_log(log: dict | None):
    """开始一次任务时调用；返回 token 供 reset 用。"""
    return _tool_call_log.set(log)


def reset_tool_call_log(token) -> None:
    try:
        _tool_call_log.reset(token)
    except ValueError:
        pass


# 工具里检索到的引用来源：read_doc 是"工具内部的检索"，命中切片也必须能像知识分支那样
# 出现在前端「引用来源」里 —— 否则 Agent 页只有一团工具返回文本，用户根本不知道答案
# 引了哪份文档（2026-10-04 用户反馈："为什么 Agent 模块不显示文档检索来源"）。
# 与 _tool_call_log 同一套路：**父任务放一个可变列表，工具往里 append**。
# （ContextVar 在子上下文里只复制引用，重新赋值父任务看不到，改内容才看得到。）
_tool_sources: ContextVar[list | None] = ContextVar("workmind_tool_sources", default=None)


def set_tool_sources(bucket: list | None):
    """开始一次任务时调用；返回 token 供 reset 用。"""
    return _tool_sources.set(bucket)


def reset_tool_sources(token) -> None:
    try:
        _tool_sources.reset(token)
    except ValueError:
        pass


def record_tool_sources(sources: list, recall: dict | None = None) -> None:
    """工具里查到（或没查到）时调用，由上层取走并发 sources 事件。

    没查到也要记：前端靠它显示"为什么没命中"（库空/权限过滤/低于阈值/重排全弃），
    而不是让用户对着"工具返回了一句话"猜原因。
    """
    bucket = _tool_sources.get()
    if bucket is None:
        return
    bucket.append({"sources": list(sources or []), "recall": dict(recall or {})})


# 不重试的工具：**有副作用的调用重试等于重复执行**。
# send_notify 真发出去一次就够了，重试可能给同一个人发两条通知 ——
# 这类工具只做超时与熔断，不做重试（要幂等得靠调用方给幂等键，不是靠重试）。
_NON_RETRYABLE_TOOLS = {"send_notify"}


def _tool_event(kind: str, detail: dict) -> None:
    """把工具的韧性事件写进日志 + 全链路追踪（没有追踪上下文时是空操作）。"""
    logger.warn("tool resilience: " + kind, detail)
    try:
        from app.infra.trace import trace_step
        trace_step("resilience", f"工具韧性：{kind}",
                   status="error" if kind in ("gave_up", "breaker_open") else "ok",
                   detail=detail)
    except Exception:  # noqa: BLE001 - 追踪不能影响工具执行
        pass


def _guard_tool(tool_obj):
    """给工具套三层防护：重复调用拦截 → 超时 → 指数退避重试 → 熔断。

    为什么工具也要熔断：某个上游（比如搜索服务）挂了的时候，
    Agent 会连着调 5 次，每次都卡满超时 —— 一次任务光等待就烧掉 100 秒，
    用户看到的是"Agent 一直在转圈"。跳闸之后立刻返回"这个工具现在不可用"，
    模型可以改用别的工具或直接说明情况，而不是干等。
    """

    # 关键：内部必须直接调原函数，不能走 tool_obj.ainvoke()。
    # 走 ainvoke 会在 ToolNode 里再起一个嵌套 runnable，astream_events 就会把
    # on_tool_start / on_tool_end 各发两遍 —— 前端步骤卡片直接翻倍（真实踩过）。
    # 参数已经由外层（同一个 args_schema）校验过，这里直接执行是安全的。
    inner = tool_obj.coroutine or tool_obj.func
    max_retries = 0 if tool_obj.name in _NON_RETRYABLE_TOOLS else TOOL_MAX_RETRIES

    @tool(tool_obj.name, args_schema=tool_obj.args_schema, description=tool_obj.description)
    async def guarded(**kwargs) -> str:
        # ① 重复调用拦截（完全相同参数只真正执行一次）
        log = _tool_call_log.get()
        if log is not None:
            key = tool_obj.name + "|" + json.dumps(kwargs, ensure_ascii=False, sort_keys=True,
                                                   default=str)
            if key in log:
                logger.info("tool:repeat skipped", {"tool": tool_obj.name})
                return (f"（已跳过重复调用）你刚刚已经用完全相同的参数调用过 {tool_obj.name}，"
                        "结果与上次完全一样。请基于已有信息继续，或换一个真正不同的参数；"
                        "不要把步数花在重复调用上。")
            log[key] = True

        # ② 超时 + 指数退避重试 + 熔断
        async def _call():
            return await inner(**kwargs)

        try:
            return await call_with_resilience(
                _call, name=f"tool:{tool_obj.name}",
                timeout=TOOL_TIMEOUT, retries=max_retries,
                breaker_name=f"tool:{tool_obj.name}", on_event=_tool_event,
            )
        except (RetryExhaustedError, BreakerOpenError) as err:
            # 不给模型抛异常（抛了整条 Agent 就断了），而是回一句**能行动**的说明：
            # 工具失败了、失败在哪、现在该干什么。
            reason = str(err)
            if isinstance(err, BreakerOpenError):
                hint = ("该工具连续失败已暂时停用（熔断），这段时间不要再调用它；"
                        "请改用其它工具，或直接基于已获得的信息给出回答并说明哪部分没能查到。")
            else:
                hint = "请换一个参数/关键词再试，或改用其它工具；不要用完全相同的参数反复重试。"
            return (f"（工具 {tool_obj.name} 调用失败）{reason}\n{hint}")

    return guarded


# 图里用带防护的版本；all_tools 保持原始对象（工具清单 / 接口 / 测试都用它）
guarded_tools = [_guard_tool(t) for t in all_tools]
