# server-py/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 依赖源：默认仍是官方 PyPI（本地/海外构建行为不变）。
# 国内服务器直连 pypi.org 实测只有几十 KB/s，装完这些依赖要几十分钟；
# 部署时用 --build-arg 指到就近镜像（腾讯云内网镜像实测 150KB/0.12s）：
#   docker compose -f docker-compose.prod.yml build \
#     --build-arg PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple \
#     --build-arg PIP_TRUSTED_HOST=mirrors.tencentyun.com
# （HTTP 源必须同时给 PIP_TRUSTED_HOST，否则 pip 会拒绝不安全的源）
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=

COPY requirements.txt ./
RUN set -eux; \
    if [ -n "${PIP_TRUSTED_HOST}" ]; then \
      pip install --no-cache-dir -i "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" -r requirements.txt; \
    else \
      pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt; \
    fi

COPY app/ ./app/
# 回归测试一起打进镜像，容器内可直接跑：
#   docker exec workmind-server python /app/tests/test_parser_cross_page.py
COPY tests/ ./tests/

ENV NODE_ENV=production
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health/live')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
