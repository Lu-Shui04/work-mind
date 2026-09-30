#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkMind 关键模块评测 runner（纯标准库：urllib + json + argparse + concurrent.futures）。

设计原则
--------
1. 打真实接口，不打桩、不 mock：评测的价值在于"服务真的这么表现"，mock 掉之后
   通过率再高也不能说明线上可用。
2. 判定只认接口返回值：检索看 hits 里的文档标题与切片正文，意图看 SSE 的 intent 事件，
   回答看 token 拼出来的最终文本，Agent 看 tool_call 事件与最终回答 —— 都是前端能拿到的东西。
3. 失败原样保留：不做"重试到通过"，不为了让数字好看放宽阈值。失败明细里打印
   "期望 vs 实际"，让评测集能暴露问题（README 里维护"已知失败项 + 原因"）。

用法
----
    python run_evals.py                                  # 跑全部评测集
    python run_evals.py --suite rag_retrieval --suite intent
    python run_evals.py --suite answer_grounding --limit 3 --verbose
    python run_evals.py --base-url http://127.0.0.1:3000 --out report.json

注意：/api/chat/stream、/api/agent/run、/api/erp/parse 都挂了令牌桶限流（容量 30 / 每秒补 10），
所以并发默认 1；想跑快点用 --concurrency，但撞到 429 会被重试。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET_DIR = HERE / "datasets"
REPORT_DIR = HERE / "reports"

# 评测统一使用的身份：tech / internal —— 能看到的文档有
# 3 份 general 制度 + 前端研发规范 + 2 份公开面试资料（见 README 的可见性表）。
# 这是刻意的选择：它看不到 hr/finance/legal 的专属文档，
# 所以"问考勤/合同却答出来了"这类越权回归能被评测集抓住。
DEFAULT_IDENTITY = {
    "tenant_id": "tenant-demo",
    "user_id": "u-tech-01",
    "departments": "tech",
    "clearance": "internal",
}

# 评测集 → 数据集文件
SUITE_FILES = {
    "rag_retrieval": "rag_retrieval.json",
    "intent": "intent.json",
    "answer_grounding": "answer_grounding.json",
    "agent_tasks": "agent_tasks.json",
    "erp_parse": "erp_parse.json",
}

# 命中"库里确实没有"时，服务必须说出来的话：
#   知识库中没有查到相关内容 —— query.py::miss_reply() 的固定答复（对话与 Agent 共用），
#                               后端作为第一个 token 推出去，不调用模型
# 这几句取其一即可；编造公司规定却什么也不说，才算失败。
MISS_PHRASES = [
    "知识库中没有查到相关内容",
    "知识库中未找到相关内容",
    "知识库未命中",
]

# 本次运行的唯一标记：拼进 sessionId，保证每一轮评测都从"空会话"开始。
# 为什么必须这样：Agent / 对话的记忆是落库的（chat_sessions / chat_messages），
# 同一个 sessionId 第二次跑时，模型会直接引用上一轮的记忆（实测出现过"任务不再调用工具、
# 直接复述上次答案"），判定结果就会随"这是第几次跑"而变 —— 评测必须可重复。
RUN_TAG = time.strftime("%m%d-%H%M%S")

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ── HTTP：只依赖标准库 ────────────────────────────────────────────────
# 连接级失败（服务没起来 / 容器正在重启）打这个标记：报告里单独统计，
# 避免把基础设施抖动误读成"这个模块回归了"。
CONN_MARK = "[服务不可用] "


class Timeout(Exception):
    """请求超时/连接失败：算这条用例失败，不算 runner 崩溃。"""


def _headers(identity: dict) -> dict:
    """身份头必须带齐：漏了会落到 anonymous/public，看不到 internal 文档，
    表现是"检索全 0 条"，排查方向会被带偏到权限上。"""
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Tenant-Id": identity["tenant_id"],
        "X-User-Id": identity["user_id"],
        "X-User-Departments": identity["departments"],
        "X-User-Clearance": identity["clearance"],
    }


def post_json(base_url: str, path: str, body: dict, identity: dict, timeout: float = 120.0,
              retries: int = 4) -> dict:
    """发一个普通 JSON 请求（非流式接口用）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, headers=_headers(identity),
                                 method="POST")
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            # 429 是限流，不是用例失败：等一下重试；其它 4xx/5xx 把错误体交给判定层
            if err.code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_err = "429 限流"
                continue
            body_text = err.read().decode("utf-8", "replace")
            raise Timeout("HTTP " + str(err.code) + ": " + body_text[:300]) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            # 连接级失败（容器重启 / 端口没起来）不是被测模块的行为问题：
            # 退避重试到 ~30 秒，仍然失败才判定失败，并在报告里标成"服务不可用"，
            # 免得把一次 docker 重启记成"检索回归"。
            last_err = str(err)
            if attempt < retries:
                time.sleep(min(2 ** (attempt + 1), 16))
                continue
            raise Timeout(CONN_MARK + str(last_err)) from err
    raise Timeout(CONN_MARK + str(last_err))


def post_sse(base_url: str, path: str, body: dict, identity: dict, timeout: float = 300.0,
             stop_after=None, retries: int = 4) -> list:
    """发一个 SSE 请求，按顺序收集 (event, data)。

    stop_after：收到该事件后立即停止读取并关闭连接。
    意图集靠它省掉"判定完了还等模型把整段回答生成完"的时间 —— intent 事件在检索与模型
    调用之前就已经下发，读到它就能判定。提前断开会让服务端把这次 trace 收尾成 cancelled，
    这是预期行为（不改变任何被评测的逻辑）。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    events: list = []
    last_err = None
    for attempt in range(retries + 1):
        events = []
        req = urllib.request.Request(base_url + path, data=data, headers=_headers(identity),
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                etype = None
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event: "):
                        etype = line[len("event: "):]
                    elif line.startswith("data: "):
                        payload = line[len("data: "):]
                        try:
                            parsed = json.loads(payload)
                        except json.JSONDecodeError:
                            parsed = payload
                        events.append((etype, parsed))
                        if stop_after and etype == stop_after:
                            return events
            return events
        except urllib.error.HTTPError as err:
            body_text = err.read().decode("utf-8", "replace")
            if err.code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise Timeout("HTTP " + str(err.code) + ": " + body_text[:300]) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last_err = str(err)
            if attempt < retries:
                time.sleep(min(2 ** (attempt + 1), 16))
                continue
            raise Timeout(CONN_MARK + str(last_err)) from err
    raise Timeout(CONN_MARK + str(last_err))


def sse_tokens(events: list) -> str:
    """把 token 事件拼成最终回答。

    answer_reset 必须处理：Agent 在决定调工具之前流出去的那些字是"过程说明"
    （"我先查一下…"），前端收到 answer_reset 会把它们挪到灰色过程区。如果评测把
    过程说明也算进最终回答，关键词命中就会假通过（例如过程里提到"年假 15 天"，
    最终回答其实没写）。
    """
    buf: list = []
    for etype, data in events:
        if etype == "answer_reset":
            buf = []
        elif etype == "token" and isinstance(data, dict):
            buf.append(data.get("token") or "")
    return "".join(buf)


def sse_answer_with_process(events: list) -> str:
    """全过程文本（含被 answer_reset 挪走的过程说明），只在失败明细里展示用。"""
    return "".join(d.get("token") or "" for t, d in events
                   if t == "token" and isinstance(d, dict))


def norm(text: str) -> str:
    """关键词比对前的归一化：去掉空白、Markdown 强调符号、数字里的千分位逗号。

    为什么需要：
    1) 排版空格不稳定 —— 同一份制度里写着"600 元"，模型可能写成"600元"或"**600 元**"；
    2) **千分位**：模型很爱把金额写成"3,390 元"（中文财务书写习惯），
       而用例里的期望值是"3390"。实测就因此误报了两条失败
       （agent-002 的 3,390、agent-005 的 2,400），回答本身完全正确。
    评测判的是"这个结论有没有出现在回答里"，不是"排版是否逐字一致"，
    所以先归一化再比子串。注意只动**数字内部的**逗号（\d,\d{3}），
    不会把"1740, 1200"这种并列数字粘成一个数。
    """
    cleaned = re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    return re.sub(r"[\s*_#]+", "", cleaned)


def keyword_missing(keywords, text: str) -> list:
    """返回 text（归一化后）里缺失的关键词列表。"""
    haystack = norm(text)
    return [k for k in (keywords or []) if norm(k) not in haystack]


def adjacent_duplicate_token_ratio(events: list) -> float:
    """检测「同一个 token 被连发两遍」—— Agent 流式输出重复字符的真实故障。

    为什么要按 token 事件而不是按拼接后的文本猜：正常中文里本来就有"谢谢/刚刚"这类叠词，
    按文本算重复率会误判；而重复发送时 token 一定是成对出现的（"公司","公司","年","年"…），
    这个信号非常干净（实测故障时 ≈0.5，正常对话流 ≈0）。
    返回：非空 token 里"和上一个 token 完全相同"的比例。
    """
    tokens = [d.get("token") for t, d in events if t == "token" and isinstance(d, dict)]
    tokens = [t for t in tokens if t and t.strip()]
    if len(tokens) < 5:
        return 0.0
    same = sum(1 for a, b in zip(tokens, tokens[1:]) if a == b)
    return round(same / (len(tokens) - 1), 3)


# 判定"疑似重复发送"的阈值：正常对话流实测 0，故障时 ≈0.5，取 0.3 两边都不擦边。
DUP_TOKEN_THRESHOLD = 0.3


def p95(values: list) -> float:
    """最近秩法 P95：n 个样本取第 ceil(0.95n) 个（升序）。

    样本很少（十几条）时不做线性插值 —— 插值出来的数不是一个真实观测值，
    报告里写"P95 = 1234ms"却没有任何一次请求是这个耗时，反而误导人。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[idx]

# ── 各评测集的判定逻辑 ────────────────────────────────────────────────
def judge_rag_retrieval(case: dict, actual: dict) -> tuple:
    """RAG 检索：期望命中的文档标题 + 切片里必须出现的关键结论。

    判定三件事：
    1. Recall@k：期望文档里至少有 min_hits 份出现在 TopK 里（标题子串匹配）
    2. 关键结论：命中的切片正文里必须真的出现这些关键词
       —— 只比标题会漏掉"召回到了文档、但答案那一片没被召回"这种最坑的情况
    3. forbidden_docs：**必须不出现**的文档（越权回归 / 版本过滤失效）
    """
    hits = actual.get("hits") or []
    titles = [h.get("title") or "" for h in hits]
    texts = "\n".join((h.get("content") or "") for h in hits)
    recall = actual.get("recall") or {}

    expect_docs = case.get("expect_docs") or []
    min_hits = int(case.get("min_hits") or 1)
    matched_docs = [d for d in expect_docs if any(d in t for t in titles)]
    missing_keywords = keyword_missing(case.get("expect_keywords"), texts)
    leaked = [d for d in (case.get("forbidden_docs") or []) if any(d in t for t in titles)]

    # expect_docs 为空 = 这条用例只检查"不该出现什么"（权限用例就长这样）
    doc_ok = (not expect_docs) or len(matched_docs) >= min_hits
    ok = doc_ok and not missing_keywords and not leaked

    expect_txt = (("TopK 内需命中 " + str(expect_docs) + "（≥" + str(min_hits) + " 份）")
                  if expect_docs else "不要求命中任何文档")
    if case.get("expect_keywords"):
        expect_txt += "，正文需含 " + str(case["expect_keywords"])
    if case.get("forbidden_docs"):
        expect_txt += "，且不得出现 " + str(case["forbidden_docs"])
    actual_txt = ("命中 " + str(len(hits)) + " 条：" + str(titles)
                  + "；reason=" + str(recall.get("reason"))
                  + " bestScore=" + str(recall.get("bestScore")))
    if missing_keywords:
        actual_txt += "\n❌ 命中切片里缺少关键词 " + str(missing_keywords)
    if leaked:
        actual_txt += "\n❌ 出现了不该可见的文档 " + str(leaked)
    metrics = {
        "recall_at_k": round(len(matched_docs) / len(expect_docs), 3) if expect_docs else 1.0,
        "hits": len(hits),
        "bestScore": recall.get("bestScore"),
        "reason": recall.get("reason"),
        "top_titles": titles[:3],
    }
    return ok, "期望：" + expect_txt + "\n实际：" + actual_txt, metrics


def judge_intent(case: dict, actual: dict) -> tuple:
    """意图判定：消息 → needKnowledge 是否符合预期。

    这条集防的是"该查库却不查"和"闲聊却去查库"两类回归：
    前者让用户觉得助手没看公司资料，后者每次白花一次 embedding + 检索。
    """
    got = actual.get("needKnowledge")
    expect = bool(case.get("expect_need_knowledge"))
    ok = got is not None and bool(got) == expect
    # decisionSource 是可选断言：只用来钉住"走的是哪条规则"（如 skip-打招呼）。
    # 不强制匹配时留空，避免"规则名字改了"被误报成行为回归。
    prefix = case.get("expect_source_prefix")
    if ok and prefix and not str(actual.get("decisionSource") or "").startswith(prefix):
        ok = False
    expect_txt = "needKnowledge=" + str(expect) + (("，decisionSource 前缀 " + prefix) if prefix else "")
    actual_txt = ("needKnowledge=" + str(got)
                  + "，decisionSource=" + str(actual.get("decisionSource"))
                  + "，ruleHit=" + str(actual.get("ruleHit"))
                  + "，reason=" + str(actual.get("reason")))
    return ok, "期望：" + expect_txt + "\n实际：" + actual_txt, {
        "needKnowledge": got, "decisionSource": actual.get("decisionSource")}


CITE_RE = re.compile(r"\[(\d{1,2})\]")


def citation_metrics(answer: str, sources: list | None) -> dict:
    """引用准确率：回答里标的 [1] [2] 到底对不对得上真实来源。

    判据（**只判"错得明确"的那种**，不做语义比对，避免评测抖）：
      1. **越界引用**：回答里写了 [5]，但本轮只有 2 条来源 —— 这是硬错误（模型编了编号）；
      2. **无源引用**：本轮压根没检索到资料（sources 为空），回答里却出现 [n] —— 同样是编的；
         这两类都直接判用例失败（下面 leaked 一起返回）。
      3. citationPrecision = 有效引用数 / 总引用数（无引用时：有来源算 0，无来源算 1）；
      4. citationCoverage  = 有来源时回答是否至少引了一次。

    为什么不判"引用的那一片是不是真的支撑这句话"：模型常做同义改写，
    关键词比对会大量误判成"引用错误"，那样的指标没人敢信。语义级引用核查留给人工抽查。
    """
    sources = sources or []
    cites = [int(n) for n in CITE_RE.findall(answer or "")]
    bad = [n for n in cites if not (1 <= n <= len(sources))]
    if not cites:
        return {"citationCount": 0, "citationInvalid": [], "citationPrecision": 1.0 if not sources else 0.0,
                "citationCoverage": 0.0 if sources else 1.0, "sourceCount": len(sources)}
    return {
        "citationCount": len(cites),
        "citationInvalid": bad,
        "citationPrecision": round((len(cites) - len(bad)) / len(cites), 3),
        "citationCoverage": 1.0,
        "sourceCount": len(sources),
    }


def judge_answer_grounding(case: dict, answer: str, sources: list | None = None) -> tuple:
    """回答落地性：要点必须出现在回答里；库里没有的必须明说没查到；引用必须对得上来源。

    must_include      —— 全都要出现（数字、天数、金额这类硬事实）
    must_include_any  —— 每组里至少出现一个（同一件事的多种说法，避免判定过严）
    must_not_include  —— 一个都不能出现（"用户没问的话题不要下结论"这类断言：
                         例如目录类回答里绝不能冒出"未找到"，那是把没问的事说成库里没有）
    not_in_kb=true    —— 回答里必须出现"知识库中没有查到相关内容"之类的明示。
                         防的是"库里没有却编一条公司规定出来"（幻觉回归）。
                         注意：自动模式下后端本来就会把这句话作为第一个 token 推出去，
                         所以这条断言真正的价值是"如果有人把这句删了，评测立刻失败"。
    sources           —— 本轮检索到的切片（来自 SSE 的 sources 事件），用来算引用准确率。
    """
    answer = answer or ""
    # sources=None：这次没走检索（缓存重放 / 关闭检索）→ 引用不判（没有来源可信度可言）
    cite = citation_metrics(answer, sources) if sources is not None else {
        "citationCount": 0, "citationInvalid": [], "citationPrecision": None,
        "citationCoverage": None, "sourceCount": None, "citationSkipped": True}
    # 越界引用 / 无源引用 = 编造出处，与"库里没有却编规定"同级，直接判失败
    cite_ok = (cite.get("citationSkipped") or (
        not cite["citationInvalid"]
        and not (cite["sourceCount"] == 0 and cite["citationCount"] > 0)))
    missing = keyword_missing(case.get("must_include"), answer)
    failed_groups = [g for g in (case.get("must_include_any") or [])
                     if not any(norm(k) in norm(answer) for k in g)]
    miss_ok = True
    if case.get("not_in_kb"):
        miss_ok = any(p in answer for p in MISS_PHRASES)
    # 归一化后再比：排版空格/加粗符号不该影响"这句话有没有出现"
    leaked = [k for k in (case.get("must_not_include") or []) if norm(k) in norm(answer)]

    ok = not missing and not failed_groups and miss_ok and not leaked and cite_ok
    expect_parts = []
    if case.get("must_include"):
        expect_parts.append("必须包含 " + str(case["must_include"]))
    if case.get("must_include_any"):
        expect_parts.append("每组至少命中其一 " + str(case["must_include_any"]))
    if case.get("not_in_kb"):
        expect_parts.append("必须明示「知识库中没有查到相关内容」")
    if case.get("must_not_include"):
        expect_parts.append("不允许出现 " + str(case["must_not_include"]))
    actual_txt = "回答（" + str(len(answer)) + " 字）：" + answer[:300]
    if missing:
        actual_txt += "\n❌ 缺少要点 " + str(missing)
    if failed_groups:
        actual_txt += "\n❌ 未命中任何说法 " + str(failed_groups)
    if not miss_ok:
        actual_txt += "\n❌ 库里没有这条内容，但回答里没有明示未命中（存在编造风险）"
    if leaked:
        actual_txt += "\n❌ 出现了不该出现的内容 " + str(leaked)
    if not cite_ok:
        actual_txt += ("\n❌ 引用不成立：回答里标了 " + str(cite["citationCount"]) + " 处引用，"
                       + ("越界编号 " + str(cite["citationInvalid"]) if cite["citationInvalid"]
                          else "但本轮没有任何来源（sources 为空）"))
    return ok, "期望：" + "；".join(expect_parts) + "\n" + actual_txt, {
        "answer_chars": len(answer), **cite}


def judge_agent_task(case: dict, actual: dict) -> tuple:
    """Agent 任务：路由/工具调用 + 最终回答要点。

    expect_route  —— 期望走哪条分支（knowledge / tool / chat）
    expect_tools  —— 这些工具必须被调用过（少一个就是"任务只做了一半"）
    forbid_tools  —— 这些工具不允许被调用（防"该查库却去联网搜"这类跑偏）
    answer_keywords / answer_any —— 最终回答里必须出现（answer_any 是"其一即可"）
    forbid_answer_phrases —— 最终回答里不允许出现的话术
    """
    called = list(actual.get("tools") or [])
    answer = actual.get("answer") or ""
    route = actual.get("route")

    missing_tools = [t for t in (case.get("expect_tools") or []) if t not in called]
    forbidden_used = [t for t in (case.get("forbid_tools") or []) if t in called]
    missing_kw = keyword_missing(case.get("answer_keywords"), answer)
    any_kw = case.get("answer_any") or []
    any_kw_ok = (not any_kw) or any(norm(k) in norm(answer) for k in any_kw)
    forbidden_phrases = [p for p in (case.get("forbid_answer_phrases") or []) if norm(p) in norm(answer)]
    route_bad = bool(case.get("expect_route")) and route != case.get("expect_route")
    # 流式输出完整性：同一个 token 被连发两遍时，用户看到的是"公公司司年年假假"这种叠字
    dup_ratio = float(actual.get("tokenDuplication") or 0.0)
    dup_bad = bool(case.get("forbid_token_duplication")) and dup_ratio >= DUP_TOKEN_THRESHOLD

    ok = (not missing_tools and not forbidden_used and not missing_kw and any_kw_ok
          and not forbidden_phrases and not route_bad and not dup_bad)
    expect_txt = ("需调用 " + str(case.get("expect_tools") or [])
                  + (("，不得调用 " + str(case["forbid_tools"])) if case.get("forbid_tools") else "")
                  + (("，route=" + str(case["expect_route"])) if case.get("expect_route") else "")
                  + (("，回答需含 " + str(case["answer_keywords"])) if case.get("answer_keywords") else "")
                  + (("，回答需含其一 " + str(any_kw)) if any_kw else "")
                  + (("，回答不得出现 " + str(case["forbid_answer_phrases"]))
                     if case.get("forbid_answer_phrases") else ""))
    actual_txt = ("实际调用 " + str(called) + "，route=" + str(route)
                  + "，步数=" + str(actual.get("steps"))
                  + "，回答（" + str(len(answer)) + " 字）：" + answer[:300])
    if missing_tools:
        actual_txt += "\n❌ 缺少工具调用 " + str(missing_tools)
    if forbidden_used:
        actual_txt += "\n❌ 出现了不该调用的工具 " + str(forbidden_used)
    if missing_kw:
        actual_txt += "\n❌ 回答缺少要点 " + str(missing_kw)
    if not any_kw_ok:
        actual_txt += "\n❌ 回答未出现任何一种说法 " + str(any_kw)
    if forbidden_phrases:
        actual_txt += "\n❌ 回答里出现了不该出现的话术 " + str(forbidden_phrases)
    if route_bad:
        actual_txt += "\n❌ 路由不符：期望 " + str(case["expect_route"]) + "，实际 " + str(route)
    if dup_bad:
        actual_txt += ("\n❌ 流式输出重复：同一个 token 被连发两遍（相邻重复 token 占比 "
                       + str(dup_ratio) + "，阈值 " + str(DUP_TOKEN_THRESHOLD) + "）")
    return ok, "期望：" + expect_txt + "\n" + actual_txt, {
        "tools": called, "route": route, "steps": actual.get("steps"),
        # 这个指标每条 Agent 用例都会记录：即使本用例没断言，报告里也能看出"输出是不是被重复发送了"
        "tokenDuplication": dup_ratio}


def judge_erp_parse(case: dict, form: dict) -> tuple:
    """ERP 智能填单：结构化字段逐项比对。

    expect 里支持的断言（不写的字段不判）：
      type            —— 费用/假期类型，必须完全一致（选错类型会走错审批链）
      totalAmount     —— 总金额，按文本里的算术期望值精确比对（±0.01 容差）
      itemsMin        —— 明细条数下限（漏项是常见回归）
      itemNamesAny    —— 明细名称里至少出现一个关键词
      startDate/endDate —— 日期（只允许用绝对日期出题：相对日期会让评测随时间漂移）
      days/workdays   —— 自然日 / 工作日天数（workdays 靠 Python 排除周末，是算错的重灾区）
      reasonContains  —— 事由里必须出现的词
      warningsAny     —— 至少一条告警包含其中之一（如"医院证明""双重审批"）
    """
    problems = []
    exp = case.get("expect") or {}
    items = form.get("items") or []

    if "type" in exp and form.get("type") != exp["type"]:
        problems.append("type：期望 " + str(exp["type"]) + "，实际 " + str(form.get("type")))
    if "totalAmount" in exp:
        got = form.get("totalAmount")
        try:
            if got is None or abs(float(got) - float(exp["totalAmount"])) > 0.01:
                problems.append("totalAmount：期望 " + str(exp["totalAmount"]) + "，实际 " + str(got))
        except (TypeError, ValueError):
            problems.append("totalAmount：期望 " + str(exp["totalAmount"]) + "，实际 " + str(got) + "（无法转数字）")
    if "itemsMin" in exp and len(items) < exp["itemsMin"]:
        problems.append("items 条数：期望 ≥" + str(exp["itemsMin"]) + "，实际 " + str(len(items)))
    if "itemNamesAny" in exp:
        names = " ".join((i.get("name") or "") for i in items)
        if not any(k in names for k in exp["itemNamesAny"]):
            problems.append("明细名称：期望含 " + str(exp["itemNamesAny"]) + " 之一，实际 " + names)
    for field in ("startDate", "endDate", "days", "workdays"):
        if field in exp:
            got = form.get(field)
            if isinstance(exp[field], (int, float)) and not isinstance(exp[field], bool):
                same = got is not None and abs(float(got) - float(exp[field])) <= 0.01
            else:
                same = got == exp[field]
            if not same:
                problems.append(field + "：期望 " + str(exp[field]) + "，实际 " + str(got))
    if "reasonContains" in exp:
        reason = form.get("reason") or ""
        if not any(k in reason for k in exp["reasonContains"]):
            problems.append("reason：期望含 " + str(exp["reasonContains"]) + " 之一，实际「" + reason + "」")
    if "warningsAny" in exp:
        warnings = " ".join(form.get("warnings") or [])
        if not any(k in warnings for k in exp["warningsAny"]):
            problems.append("warnings：期望含 " + str(exp["warningsAny"]) + " 之一，实际 " + str(form.get("warnings")))

    ok = not problems
    actual_txt = json.dumps({k: v for k, v in form.items() if k != "items"}, ensure_ascii=False)
    actual_txt += "；items=" + json.dumps(
        [{"name": i.get("name"), "amount": i.get("amount")} for i in items], ensure_ascii=False)
    if problems:
        actual_txt += "\n❌ " + "；".join(problems)
    return ok, "期望：" + json.dumps(exp, ensure_ascii=False) + "\n实际：" + actual_txt, {
        "type": form.get("type"), "totalAmount": form.get("totalAmount")}


# ── 各评测集的执行函数：调用真实接口 → 判定 ───────────────────────────
def run_rag_retrieval(case: dict, base_url: str, identity: dict) -> tuple:
    """检索集打 /api/knowledge/search（检索验证接口，不消耗对话模型）。"""
    body = {"question": case["question"], "k": int(case.get("k") or 6)}
    # 显式范围收窄（部门/文档类型）也一起测：这两个是"检索范围预设"的真实入参，
    # 传错了就是"用户选了范围却没生效"，属于静默失效，最需要评测兜住。
    if case.get("department"):
        body["department"] = case["department"]
    if case.get("docType"):
        body["docType"] = case["docType"]
    if case.get("includeSuperseded"):
        body["includeSuperseded"] = True
    result = post_json(base_url, "/api/knowledge/search", body, identity)
    return judge_rag_retrieval(case, result)


def run_intent(case: dict, base_url: str, identity: dict) -> tuple:
    """意图集打 /api/chat/stream，只读到 intent 事件就断开（见 post_sse 的 stop_after）。"""
    events = post_sse(base_url, "/api/chat/stream",
                      {"message": case["message"], "noCache": True,
                       "sessionId": "eval-intent-" + case["id"] + "-" + RUN_TAG},
                      identity, stop_after="intent")
    intent = next((d for t, d in events if t == "intent"), None)
    if not isinstance(intent, dict):
        return False, "未收到 intent 事件（服务行为异常）", {}
    return judge_intent(case, intent)


def run_answer_grounding(case: dict, base_url: str, identity: dict) -> tuple:
    """回答集打 /api/chat/stream，取完整回答文本判定。

    prelude：先在**同一个会话**里问几句（结果丢弃），用来复现"上一轮的结论污染本轮"这类
    多轮问题。ag-014 就靠它：先问一句必然未命中的话，让助手把"知识库中没有查到相关内容"
    写进会话历史，再看本轮命中资料时会不会被带偏。
    """
    session_id = "eval-ground-" + case["id"] + "-" + RUN_TAG
    # noCache：评测必须打真实模型。命中缓存时后端直接重放答案，既不发 intent 也不发
    # sources，会让"引用准确率"这类指标静默失真（2026-09-30 实测踩到过）。
    for pre in case.get("prelude") or []:
        post_sse(base_url, "/api/chat/stream",
                 {"message": pre, "sessionId": session_id, "noCache": True}, identity)
    body = {"message": case["question"], "sessionId": session_id, "noCache": True}
    if case.get("useKnowledge") is not None:
        body["useKnowledge"] = case["useKnowledge"]
    events = post_sse(base_url, "/api/chat/stream", body, identity)
    answer = sse_tokens(events)
    # 引用准确率要用到"本轮到底检索到了哪几条"，所以把 sources 事件一起取出来
    sources_ev = next((d for t, d in events if t == "sources" and isinstance(d, dict)), None)
    # None = 这次压根没走检索（缓存重放 / 关闭检索）→ 不判引用，避免假失败；
    # [] = 走了检索但没命中 → 此时回答里任何 [n] 都是编的出处，必须判失败
    sources = None if sources_ev is None else (sources_ev.get("sources") or [])
    ok, detail, metrics = judge_answer_grounding(case, answer, sources)
    metrics["fromCache"] = next((d.get("fromCache") for t, d in events
                                 if t == "done" and isinstance(d, dict)), None)
    return ok, detail, metrics


def run_agent_task(case: dict, base_url: str, identity: dict) -> tuple:
    """Agent 集打 /api/agent/run（SSE），工具调用和最终回答都从这里取。"""
    body = {"task": case["task"], "sessionId": "eval-agent-" + case["id"] + "-" + RUN_TAG}
    if case.get("useKnowledge") is not None:
        body["useKnowledge"] = case["useKnowledge"]
    events = post_sse(base_url, "/api/agent/run", body, identity)
    tools = [d.get("toolName") for t, d in events if t == "tool_call" and isinstance(d, dict)]
    done = next((d for t, d in events if t == "done" and isinstance(d, dict)), {}) or {}
    error = next((d for t, d in events if t == "error" and isinstance(d, dict)), None)
    actual = {
        "tools": tools,
        "route": done.get("route"),
        "steps": done.get("steps"),
        # 只看最终回答（answer_reset 之后的部分），过程说明不算 —— 见 sse_tokens
        "answer": sse_tokens(events),
        # 顺带量一下"token 有没有被重复发送"（见 adjacent_duplicate_token_ratio）
        "tokenDuplication": adjacent_duplicate_token_ratio(events),
        "process": sse_answer_with_process(events),
    }
    ok, detail, metrics = judge_agent_task(case, actual)
    if error:
        ok = False
        detail += "\n❌ 服务返回 error 事件：" + str(error.get("message"))
    metrics["maxStepsReached"] = done.get("maxStepsReached")
    return ok, detail, metrics


def run_erp_parse(case: dict, base_url: str, identity: dict) -> tuple:
    """ERP 集打 /api/erp/parse（自然语言 → 结构化表单）。"""
    result = post_json(base_url, "/api/erp/parse",
                       {"text": case["text"], "formType": case["formType"]}, identity)
    return judge_erp_parse(case, result.get("form") or {})


RUNNERS = {
    "rag_retrieval": run_rag_retrieval,
    "intent": run_intent,
    "answer_grounding": run_answer_grounding,
    "agent_tasks": run_agent_task,
    "erp_parse": run_erp_parse,
}


def load_suite(name: str) -> dict:
    """数据集文件统一是 {suite, description, cases:[...]}；兼容直接给数组的老写法。"""
    path = DATASET_DIR / SUITE_FILES[name]
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        data = {"suite": name, "description": "", "cases": data}
    return data


def run_case(suite: str, case: dict, base_url: str, identity: dict, verbose: bool) -> dict:
    """跑一条用例；任何异常都收敛成"这条失败"，不带崩整个评测。"""
    started = time.time()
    try:
        ok, detail, metrics = RUNNERS[suite](case, base_url, identity)
        error = None
    except Timeout as err:
        ok, detail, metrics, error = False, str(err), {}, str(err)
    except Exception as err:  # noqa: BLE001 —— 单条用例异常不能带崩整个评测
        ok, detail, metrics, error = False, "用例执行异常：" + type(err).__name__ + ": " + str(err), {}, str(err)
    elapsed_ms = round((time.time() - started) * 1000, 1)
    record = {
        "id": case["id"],
        "passed": bool(ok),
        # 服务不可用（连接被拒/被重置）单独标记：这类失败要重跑，不当作模块回归
        "infra": bool(error and str(error).startswith(CONN_MARK)),
        "elapsedMs": elapsed_ms,
        # guard 是出题理由：这条用例在防什么回归。报告里原样带出来，
        # 失败时不用回去翻数据集就知道它意味着什么。
        "guard": case.get("guard", ""),
        "detail": detail,
        "metrics": metrics,
    }
    if error:
        record["error"] = error
    if verbose:
        mark = "✅ PASS" if ok else "❌ FAIL"
        log("  " + mark + " [" + case["id"] + "] " + str(round(elapsed_ms)) + "ms  " + str(case.get("guard", "")))
        if not ok:
            for line in detail.splitlines():
                log("      " + line)
    return record


def summarize(records: list) -> dict:
    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    durations = [r["elapsedMs"] for r in records]
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        # 其中有多少条是因为"服务不可用"失败的（容器重启等基础设施原因）
        "infraFailed": sum(1 for r in records if r.get("infra")),
        "passRate": round(passed / total, 4) if total else 0.0,
        "avgMs": round(sum(durations) / total, 1) if total else 0.0,
        "p95Ms": round(p95(durations), 1),
        "maxMs": round(max(durations), 1) if durations else 0.0,
    }


def write_markdown(path: Path, payload: dict) -> None:
    """人类可读的报告：总览表 + 每个集的逐条结果 + 失败明细（期望 vs 实际）。"""
    lines = []
    lines.append("# WorkMind 评测报告")
    lines.append("")
    lines.append("- 生成时间：" + payload["generatedAt"])
    lines.append("- 服务地址：" + payload["baseUrl"])
    ident = payload["identity"]
    lines.append("- 评测身份：tenant=" + ident["tenant_id"] + " / user=" + ident["user_id"]
                 + " / departments=" + ident["departments"] + " / clearance=" + ident["clearance"])
    lines.append("- 服务健康：" + payload["health"])
    lines.append("")
    overall = payload["summary"]
    lines.append("## 总览")
    lines.append("")
    lines.append("| 评测集 | 用例数 | 通过 | 失败 | 其中服务不可用 | 通过率 | 平均耗时 | P95 耗时 | 最慢 |")
    lines.append("|--------|-------|------|------|---------------|--------|---------|---------|------|")
    for name, s in payload["suites"].items():
        lines.append("| " + name + " | " + str(s["total"]) + " | " + str(s["passed"]) + " | "
                     + str(s["failed"]) + " | " + str(s.get("infraFailed", 0)) + " | "
                     + format(s["passRate"] * 100, ".1f") + "% | "
                     + format(s["avgMs"], ".0f") + " ms | " + format(s["p95Ms"], ".0f") + " ms | "
                     + format(s["maxMs"], ".0f") + " ms |")
    lines.append("| **合计** | **" + str(overall["total"]) + "** | **" + str(overall["passed"])
                 + "** | **" + str(overall["failed"]) + "** | **"
                 + str(overall.get("infraFailed", 0)) + "** | **"
                 + format(overall["passRate"] * 100, ".1f") + "%** | **"
                 + format(overall["avgMs"], ".0f") + " ms** | **"
                 + format(overall["p95Ms"], ".0f") + " ms** | **"
                 + format(overall["maxMs"], ".0f") + " ms** |")
    lines.append("")

    for name, suite in payload["suites"].items():
        lines.append("## " + name + "（" + str(suite["passed"]) + "/" + str(suite["total"]) + "，"
                     + format(suite["passRate"] * 100, ".1f") + "%）")
        lines.append("")
        lines.append(suite.get("description", ""))
        lines.append("")
        lines.append("| 用例 | 结果 | 耗时 | 这条在防什么回归 |")
        lines.append("|------|------|------|------------------|")
        for r in suite["cases"]:
            mark = "✅" if r["passed"] else "❌"
            guard = (r.get("guard") or "").replace("|", "/")
            lines.append("| " + r["id"] + " | " + mark + " | "
                         + format(r["elapsedMs"], ".0f") + " ms | " + guard + " |")
        lines.append("")
        failures = [r for r in suite["cases"] if not r["passed"]]
        if failures:
            lines.append("### 失败明细（期望 vs 实际）")
            lines.append("")
            for r in failures:
                lines.append("**" + r["id"] + "** —— " + (r.get("guard") or ""))
                lines.append("")
                lines.append("~~~")
                lines.append(r["detail"])
                lines.append("~~~")
                lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def health_check(base_url: str) -> str:
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "status=" + str(data.get("status")) + " uptime=" + str(data.get("uptime")) + "s"
    except Exception as err:  # noqa: BLE001
        return "健康检查失败：" + str(err)


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkMind 关键模块评测 runner")
    parser.add_argument("--suite", action="append", default=None,
                        help="只跑指定评测集，可重复；可选：" + ", ".join(SUITE_FILES))
    parser.add_argument("--base-url", default="http://127.0.0.1:3000", help="服务地址")
    parser.add_argument("--out", default=None, help="JSON 报告路径（Markdown 报告同名同目录）")
    parser.add_argument("--limit", type=int, default=None, help="每个评测集只跑前 N 条（调试用）")
    parser.add_argument("--top-k", type=int, default=None,
                        help="检索评测的 TopK（默认按用例里的 k，缺省 6）。"
                             "简历里那类「Top5 召回率」就用 --top-k 5 跑一次")
    parser.add_argument("--case", action="append", default=None,
                        help="只跑指定用例 id（可重复），例如 --case agent-004 —— 复盘某条失败用例用")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="并发数，默认 1（接口有令牌桶限流，并发高了会撞 429）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印通过/失败与期望 vs 实际")
    parser.add_argument("--tenant-id", default=DEFAULT_IDENTITY["tenant_id"])
    parser.add_argument("--user-id", default=DEFAULT_IDENTITY["user_id"])
    parser.add_argument("--departments", default=DEFAULT_IDENTITY["departments"])
    parser.add_argument("--clearance", default=DEFAULT_IDENTITY["clearance"])
    args = parser.parse_args()

    identity = {"tenant_id": args.tenant_id, "user_id": args.user_id,
                "departments": args.departments, "clearance": args.clearance}
    suites = args.suite or list(SUITE_FILES)
    for name in suites:
        if name not in SUITE_FILES:
            parser.error("未知评测集 " + name + "，可选：" + ", ".join(SUITE_FILES))

    base_url = args.base_url.rstrip("/")
    log("服务：" + base_url + "（" + health_check(base_url) + "）")
    log("身份：" + json.dumps(identity, ensure_ascii=False))

    payload = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseUrl": base_url,
        "identity": identity,
        "health": health_check(base_url),
        "topK": getattr(args, "top_k", None),
        "suites": {},
    }
    all_records: list = []

    for name in suites:
        dataset = load_suite(name)
        cases = dataset["cases"]
        # 复盘单条失败用例：--case 让"这条到底是稳定失败还是偶发"可以直接验证，
        # 不用为了跑一条而把整个集重跑一遍
        if args.case:
            wanted = set(args.case)
            cases = [c for c in cases if c["id"] in wanted]
            if not cases:
                log("  （" + name + " 里没有匹配 --case 的用例，跳过）")
                continue
        if args.limit:
            cases = cases[:args.limit]
        # --top-k：把检索评测的 TopK 统一改成指定值（用例自己写了 k 的以用例为准），
        # 这样"Top5 召回率"这类口径不用改数据集就能跑出来
        if getattr(args, "top_k", None) and name == "rag_retrieval":
            cases = [{**c, "k": c.get("k") or args.top_k} for c in cases]
        log("")
        log("▶ " + name + "：" + str(len(cases)) + " 条（" + str(dataset.get("description", "")) + "）")
        started = time.time()
        if args.concurrency > 1:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                records = list(pool.map(
                    lambda c, suite=name: run_case(suite, c, base_url, identity, args.verbose),
                    cases))
        else:
            records = [run_case(name, c, base_url, identity, args.verbose) for c in cases]
        summary = summarize(records)
        summary["description"] = dataset.get("description", "")
        summary["cases"] = records
        summary["wallMs"] = round((time.time() - started) * 1000, 1)
        payload["suites"][name] = summary
        all_records.extend(records)
        log("  ← " + name + ": " + str(summary["passed"]) + "/" + str(summary["total"])
            + " (" + format(summary["passRate"] * 100, ".1f") + "%) 平均 "
            + format(summary["avgMs"], ".0f") + "ms P95 " + format(summary["p95Ms"], ".0f") + "ms")

    payload["summary"] = summarize(all_records)

    if args.out:
        json_path = Path(args.out)
    else:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        json_path = REPORT_DIR / ("eval-report-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, payload)

    s = payload["summary"]
    log("")
    log("总计 " + str(s["total"]) + " 条，通过 " + str(s["passed"]) + "，失败 " + str(s["failed"])
        + "，通过率 " + format(s["passRate"] * 100, ".1f") + "%；平均 "
        + format(s["avgMs"], ".0f") + "ms，P95 " + format(s["p95Ms"], ".0f") + "ms")
    log("JSON 报告：" + str(json_path))
    log("Markdown 报告：" + str(md_path))
    # 有任何失败就以非 0 退出：接进 CI 时"评测失败"必须能拦住合并
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
