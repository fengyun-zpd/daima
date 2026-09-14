# CodePilot Dashboard 镜像：Node 构建静态产物 -> nginx 托管并反向代理后端。
#
# 构建：
#   docker build -f docker/web/Dockerfile -t codepilot-web:local .
# 运行：
#   docker compose up -d web        # 见 docker-compose.yml
#
# 说明：前端只使用相对路径（/api、/internal、/healthz、/readyz），
# 后端地址由 docker/web/nginx.conf 中的 upstream 决定，无需重新构建前端。

FROM node:20-alpine AS build
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY docker/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
