#!/usr/bin/env bash
# scripts/run-tests.sh
# 跑后端回归测试。
#
# 做法：不重新构建镜像，直接把 server-py 挂进镜像里跑 —— 改完代码立刻能验证，
# 不用等 docker build。.env 通过 --env-file 传进去（模型构造需要 DEEPSEEK_API_KEY）。
#
# 用法：
#   bash scripts/run-tests.sh                              # 跑全部
#   bash scripts/run-tests.sh tests/test_agent_tools.py    # 只跑指定文件
#   bash scripts/run-tests.sh tests/test_resilience.py     # 只跑韧性单测（不联网）
#
# 注意：test_fallback.py 会打真实上游（智谱）验证降级链路，需要网络与 ZHIPU_API_KEY；
# 只想跑离线用例时显式指定其它文件即可。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-workmindagentpython-server}"
cd "$ROOT"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "找不到镜像 $IMAGE，请先执行： docker compose build server" >&2
  exit 1
fi

ENV_ARGS=()
if [ -f .env ]; then ENV_ARGS=(--env-file .env); fi

if [ "$#" -gt 0 ]; then
  TARGETS=("$@")
else
  # 默认全集：解析/工具/记忆 + 韧性（不联网）+ 故障注入（要联网，验证降级真的能用）
  TARGETS=(tests/test_parser_cross_page.py tests/test_agent_tools.py tests/test_memory.py
           tests/test_resilience.py tests/test_fallback.py)
fi

for target in "${TARGETS[@]}"; do
  echo "=== $target ==="
  docker run --rm -v "$ROOT/server-py:/app" -w /app "${ENV_ARGS[@]}" "$IMAGE" python "$target"
done
