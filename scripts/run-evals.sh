#!/usr/bin/env bash
# scripts/run-evals.sh —— 一键跑 WorkMind 关键模块评测集（server-py/evals）
#
# 为什么默认在 WSL 里用 python3 直接跑：
#   后端把 3000 端口发布到宿主机，评测脚本和它在同一个网络命名空间视角下（127.0.0.1:3000 直通），
#   不需要进容器。容器里只有 /app 下的后端代码，evals 目录不在镜像里，
#   硬要在容器里跑还得先 docker cp 进去 —— 那是没有 python3 时的兜底路径（见文件末尾）。
#
# 用法：
#   bash scripts/run-evals.sh                      # 跑全部评测集
#   bash scripts/run-evals.sh --suite intent --verbose
#   bash scripts/run-evals.sh --limit 3 --suite rag_retrieval
#   API_BASE=http://127.0.0.1:3000 bash scripts/run-evals.sh
#
# 退出码沿用 runner 的约定：有失败用例 → 非 0（方便接进 CI / 合并前拦一道）。
set -uo pipefail

# 仓库根目录 = 本脚本所在目录的上一级（脚本放在 scripts/ 下）
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVALS_DIR="$ROOT/server-py/evals"
RUNNER="$EVALS_DIR/run_evals.py"
API_BASE="${API_BASE:-http://127.0.0.1:3000}"

if [ ! -f "$RUNNER" ]; then
  echo "❌ 找不到评测 runner：$RUNNER" >&2
  exit 2
fi

# 先探一下服务，避免因为"服务没起来"跑出一片红（那种红没有任何诊断价值）
if ! curl -sf -m 5 "$API_BASE/health/live" >/dev/null 2>&1; then
  echo "❌ 后端不可用：$API_BASE/health/live（先 docker compose up -d，再跑评测）" >&2
  exit 2
fi

# 再确认"模型链路真的能用"：/health/live 只说明进程活着 —— 依赖升级、模型 Key 失效、
# 结构化输出改动都会让进程健康但对话/填单直接 500，那样跑出来的一整片红没有任何诊断价值。
SMOKE_BODY='{"message":"公司年假有多少天？","sessionId":"eval-smoke"}'
SMOKE_OUT="$(curl -s -N -m 90 -X POST "$API_BASE/api/chat/stream" \
  -H "Content-Type: application/json" -H "X-Tenant-Id: tenant-demo" \
  -H "X-User-Id: u-tech-01" -H "X-User-Departments: tech" -H "X-User-Clearance: internal" \
  -d "$SMOKE_BODY" 2>/dev/null || true)"
if ! printf '%s' "$SMOKE_OUT" | grep -q '"token"'; then
  echo "❌ 模型链路不可用：/api/chat/stream 没有吐出任何 token（进程健康但对话链路坏了）" >&2
  echo "   先确认 DEEPSEEK_API_KEY 有效、依赖版本没被改动；确认无关时用 FORCE=1 跳过本检查" >&2
  [ "${FORCE:-0}" = "1" ] || exit 2
fi

echo "▶ 评测目标：$API_BASE"
echo "▶ 用例目录：$EVALS_DIR/datasets"

if command -v python3 >/dev/null 2>&1; then
  python3 "$RUNNER" --base-url "$API_BASE" "$@"
  exit $?
fi

# ── 兜底：宿主机没有 python3 时，把 evals 目录复制进后端容器里跑 ──────────────
# 容器内部访问自己用 localhost:3000（容器里的服务监听 0.0.0.0:3000）。
echo "⚠ 宿主机没有 python3，改为在后端容器 workmind-server 里执行"
docker cp "$EVALS_DIR" workmind-server:/tmp/evals >/dev/null
docker exec workmind-server python /tmp/evals/run_evals.py --base-url http://localhost:3000 "$@"
code=$?
# 报告是在容器里生成的，复制回来才能在仓库里看到
mkdir -p "$EVALS_DIR/reports"
docker cp workmind-server:/tmp/evals/reports/. "$EVALS_DIR/reports/" >/dev/null 2>&1 || true
exit $code
