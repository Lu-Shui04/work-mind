#!/usr/bin/env bash
# scripts/check-web-search.sh
# 自检：Agent 的联网搜索现在是真搜还是演示数据？
#
# 用法： bash scripts/check-web-search.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-workmindagentpython-server}"
cd "$ROOT"

ENV_ARGS=()
if [ -f .env ]; then ENV_ARGS=(--env-file .env); fi

docker run --rm -i -v "$ROOT/server-py:/app" -w /app "${ENV_ARGS[@]}" "$IMAGE" python - <<'PY'
import asyncio

from app.services.agent import tools as T

provider = T.search_provider()
print("当前搜索后端：", provider)
print("  ZHIPU_API_KEY :", "已配置" if T.ZHIPU_API_KEY else "未配置")
print("  TAVILY_API_KEY:", "已配置" if T.TAVILY_API_KEY else "未配置")
print("  BOCHA_API_KEY :", "已配置" if T.BOCHA_API_KEY else "未配置")
print()

if provider == "demo":
    print("结论：没有可用的搜索服务，web_search 只会返回演示数据。")
    print("      在 .env 里配 ZHIPU_API_KEY / TAVILY_API_KEY / BOCHA_API_KEY 任一，")
    print("      然后 docker compose up -d server 重启即可。")
    raise SystemExit(2)

query = "今天有什么科技新闻"
print(f"实测查询：{query}")
print("-" * 60)
print(asyncio.run(T.search_tool.ainvoke({"query": query}))[:900])
print("-" * 60)
print("结论：联网搜索可用 ✅")
PY
