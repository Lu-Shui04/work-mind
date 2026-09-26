# frontend/Dockerfile
# 前端镜像：Vite 构建 → Nginx 托管静态资源，并把 /api 反向代理到后端容器
#
# NPM_REGISTRY 可用 --build-arg 覆盖，默认用国内镜像（registry.npmjs.org 在国内常超时）
#   docker compose build --build-arg NPM_REGISTRY=https://registry.npmjs.org frontend

# ── 构建阶段 ──────────────────────────────────────────────────
FROM node:22-alpine AS build

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

RUN npm i -g pnpm@9 --registry=$NPM_REGISTRY

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --registry=$NPM_REGISTRY

COPY . .
RUN pnpm build

# ── 运行阶段 ──────────────────────────────────────────────────
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget -qO- http://127.0.0.1/ > /dev/null 2>&1 || exit 1
