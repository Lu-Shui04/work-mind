# server-py/evals/offline_metrics.py
"""离线指标汇总：没有线上流量，也能把七个指标算成一张能拿出手的表。

为什么单独一个脚本：评测集回答的是"这条用例过没过"，而对外（简历 / 汇报 / GitHub）
要的是"召回率多少、准确率多少、P95 多少、省了多少钱"。两者取数口径不同，
混在一起讲容易被追问穿帮，所以把口径写死在一个地方，谁都能复算。

七个指标的口径（每条都写清楚数据来源，被追问时对得上）：

| 指标 | 口径 | 数据来源 |
|------|------|---------|
| Top5 召回率 | 每条检索用例 recall_at_k 的平均（命中期望文档数 / 期望文档数） | 评测报告（用 --top-k 5 跑） |
| 答案准确率 | 「回答落地性」评测通过率：要点必须出现、库里没有的必须明说没查到 | 评测报告 |
| 引用准确率 | 回答里的 [n] 与真实来源对得上的比例；越界编号 / 无来源却标引用 → 该用例直接失败 | 评测报告（citationPrecision） |
| P95 延迟 | 评测逐条耗时排序后的 P95；分集与整体各给一份 | 评测报告（p95Ms）+ 监控看板 p99 |
| Token 成本降幅 | 缓存命中省下的 token ÷（实际消耗 + 省下） | 监控看板 /api/monitor/stats |
| 任务成功率 | Agent 任务评测通过率（路由/工具/要点三项断言）；另给运行期 trace 成功率 | 评测报告 + /api/trace/stats |
| 人工介入率 | ERP 审批里被 need_info 打断（要申请人补材料）的申请占比 | /api/erp/applications |

用法（后端在跑就行，脚本只用标准库）：
    python run_evals.py --top-k 5                    # 先出一份 Top5 口径的评测报告
    python offline_metrics.py                        # 默认取 reports/ 里最新那份报告
    python offline_metrics.py --report <path.json> --out reports/offline-metrics.md
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time
import urllib.error
import urllib.request

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def _get_json(base_url: str, path: str, timeout: float = 15.0):
    """取一个 JSON 接口；失败返回 None（缺数据不该让整张表报错）。"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def latest_report() -> str | None:
    files = sorted(glob.glob(os.path.join(REPORTS_DIR, "eval-report-*.json")))
    return files[-1] if files else None


def _suite(report: dict, name: str) -> dict:
    return (report.get("suites") or {}).get(name) or {}


def _mean(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _pct(x: float | None) -> str:
    return "—" if x is None else format(x * 100, ".1f") + "%"


def collect(report: dict, base_url: str) -> list[dict]:
    """把七个指标算成 [{指标, 数值, 口径, 样本}] 的行。"""
    rows: list[dict] = []

    # ── 1. Top5 召回率 ────────────────────────────────────────────
    rag = _suite(report, "rag_retrieval")
    rag_cases = rag.get("cases") or []
    recall = _mean([(c.get("metrics") or {}).get("recall_at_k") for c in rag_cases])
    topk = report.get("topK") or 6
    rows.append({
        "name": f"Top{topk} 召回率",
        "value": _pct(recall),
        "how": f"每条用例 recall_at_k（命中期望文档数 ÷ 期望文档数）取平均；本次按 k={topk} 跑",
        "sample": f"{len(rag_cases)} 条检索用例（含权限过滤 / 范围收窄）",
    })

    # ── 2. 答案准确率 ─────────────────────────────────────────────
    ground = _suite(report, "answer_grounding")
    g_cases = ground.get("cases") or []
    rows.append({
        "name": "答案准确率",
        "value": _pct(ground.get("passRate")),
        "how": "回答要点必须真的出现在回答里（关键词级判定），库里没有的必须明说没查到",
        "sample": f"{ground.get('passed', 0)}/{ground.get('total', 0)} 条回答评测",
    })

    # ── 3. 引用准确率 ─────────────────────────────────────────────
    prec = _mean([(c.get("metrics") or {}).get("citationPrecision") for c in g_cases])
    cited = sum(int((c.get("metrics") or {}).get("citationCount") or 0) for c in g_cases)
    bad = sum(len((c.get("metrics") or {}).get("citationInvalid") or []) for c in g_cases)
    no_source_cited = sum(1 for c in g_cases
                          if (c.get("metrics") or {}).get("sourceCount") == 0
                          and (c.get("metrics") or {}).get("citationCount"))
    rows.append({
        "name": "引用准确率",
        "value": _pct(prec),
        "how": ("回答里的 [n] 与 sources 对得上的比例；越界编号 / 本轮无来源却标引用都判失败"
                f"（本次共 {cited} 处引用，越界 {bad} 处，无源引用 {no_source_cited} 条用例）"),
        "sample": f"{len(g_cases)} 条回答评测",
    })

    # ── 4. P95 延迟 ───────────────────────────────────────────────
    summary = report.get("summary") or {}
    per_suite = "；".join(
        f"{n} {format(float((s.get('p95Ms') or 0)), '.0f')}ms"
        for n, s in (report.get("suites") or {}).items())
    rows.append({
        "name": "P95 延迟",
        "value": format(float(summary.get("p95Ms") or 0), ".0f") + " ms",
        "how": "评测逐条耗时排序取 P95（含真实模型调用）；分集：" + per_suite,
        "sample": f"{summary.get('total', 0)} 条用例",
    })
    stats = _get_json(base_url, "/api/monitor/stats") or {}
    lat = (stats.get("latency") or {})
    if lat.get("p99") is not None:
        rows.append({
            "name": "运行期延迟分位（监控看板）",
            "value": (f"P50 {lat.get('p50')}ms / P90 {lat.get('p90')}ms / P99 {lat.get('p99')}ms"
                      f"（平均 {lat.get('avg')}ms）"),
            "how": "PostgreSQL percentile_cont 按功能统计，**排除缓存命中**（否则会虚低）",
            "sample": f"看板今日 {((stats.get('overview') or {}).get('apiCallsToday', '—'))} 次真实调用",
        })

    # ── 5. Token 成本降幅 ─────────────────────────────────────────
    ov = (stats.get("overview") or {})
    # 用 **7 天窗口**而不是单日：单日的构成会被"跑评测（noCache，不进缓存）"带偏，
    # 7 天里既有真实对话的命中，也有评测的冷启动，更接近稳态
    week = stats.get("last7Days") or []
    used = sum(int(d.get("inputT") or 0) + int(d.get("outputT") or 0) for d in week) \
        or int(ov.get("tokenInputToday") or 0) + int(ov.get("tokenOutputToday") or 0)
    saved = sum(int(d.get("savedT") or 0) for d in week) or int(ov.get("savedTokensToday") or 0)
    ratio = (saved / (used + saved)) if (used + saved) else None
    money = ""
    try:  # 有依赖时顺手折算成钱（脚本本身只用标准库，所以这里允许失败）
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.models.pricing import cost_cny  # type: ignore
        money = f"；折算约 ¥{cost_cny('deepseek-chat', saved, 0):.2f}"
    except Exception:  # noqa: BLE001
        pass
    rows.append({
        "name": "Token 成本降幅（缓存贡献）",
        "value": _pct(ratio),
        "how": (f"缓存命中省下的 token ÷（实际消耗 + 省下），近 7 天口径：省下 {saved}，"
                f"实际消耗 {used}；今日命中率 {ov.get('cacheHitRate', '—')}{money}"),
        "sample": (f"近 7 天 {sum(int(d.get('totalCalls') or 0) for d in week) or '—'} 次调用"
                   "（落 PostgreSQL 的 usage_calls）"),
    })

    # ── 6. 任务成功率 ─────────────────────────────────────────────
    agent = _suite(report, "agent_tasks")
    trace = _get_json(base_url, "/api/trace/stats") or {}
    today = trace.get("today") or []
    total_runs = sum(int(t.get("total") or 0) for t in today)
    failed_runs = sum(int(t.get("failed") or 0) for t in today)
    trace_rate = (1 - failed_runs / total_runs) if total_runs else None
    rows.append({
        "name": "任务成功率",
        "value": _pct(agent.get("passRate")),
        "how": (f"Agent 任务评测通过率（路由 / 工具调用 / 最终回答要点三项断言）；"
                f"运行期 trace 成功率 {_pct(trace_rate)}（今日 {total_runs} 次，失败 {failed_runs} 次）"),
        "sample": f"{agent.get('passed', 0)}/{agent.get('total', 0)} 条任务用例",
    })

    # ── 7. 人工介入率 ─────────────────────────────────────────────
    apps = (_get_json(base_url, "/api/erp/applications") or {}).get("applications") or []
    need_human = 0
    for a in apps:
        detail = (_get_json(base_url, f"/api/erp/applications/{a['id']}") or {}).get("application") or {}
        chain = detail.get("chain") or []
        if any(step.get("questions") or step.get("status") == "waiting_info" for step in chain):
            need_human += 1
    rate = (need_human / len(apps)) if apps else None
    rows.append({
        "name": "人工介入率",
        "value": _pct(rate) if apps else "无数据",
        "how": ("审批链里被 need_info 打断（要申请人补材料才继续）的申请占比；"
                "其余为模型按角色链自动裁决"),
        "sample": (f"{need_human}/{len(apps)} 份申请" if apps
                   else "0 份申请（审批记录在内存里，进程重启即清空；跑一次审批流即可产生样本）"),
    })
    return rows


def render(rows: list[dict], report_path: str, base_url: str) -> str:
    lines = [
        "# WorkMind 离线指标",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 服务地址：{base_url}",
        f"- 评测报告：{os.path.basename(report_path) if report_path else '（未找到，指标 1/2/3/4/6 缺数据）'}",
        "",
        "| 指标 | 数值 | 口径与数据来源 | 样本量 |",
        "|------|------|---------------|--------|",
    ]
    for r in rows:
        lines.append(f"| **{r['name']}** | {r['value']} | {r['how']} | {r['sample']} |")
    lines += [
        "",
        "> 全部为**离线指标**：来自评测集（真实模型跑真实接口）+ 落库的用量/追踪数据，",
        "> 不依赖线上流量。复算方式见 server-py/evals/README.md。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="离线指标汇总（七个指标一张表）")
    ap.add_argument("--base-url", default="http://127.0.0.1:3000", help="后端地址")
    ap.add_argument("--report", default=None, help="评测报告 JSON；缺省取 reports/ 里最新一份")
    ap.add_argument("--out", default=None, help="Markdown 输出路径（缺省写 reports/offline-metrics-<时间>.md）")
    args = ap.parse_args()

    report_path = args.report or latest_report()
    report = json.load(open(report_path, encoding="utf-8")) if report_path else {}
    rows = collect(report, args.base_url)
    text = render(rows, report_path, args.base_url)

    out = args.out or os.path.join(REPORTS_DIR, "offline-metrics-" + time.strftime("%Y%m%d-%H%M%S") + ".md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)

    print(text)
    print("报告已写入：" + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
