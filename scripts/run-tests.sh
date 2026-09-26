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
  TARGETS=(tests/test_parser_cross_page.py tests/test_agent_tools.py tests/test_memory.py)
fi

for target in "${TARGETS[@]}"; do
  echo "=== $target ==="
  docker run --rm -v "$ROOT/server-py:/app" -w /app "${ENV_ARGS[@]}" "$IMAGE" python "$target"
done
