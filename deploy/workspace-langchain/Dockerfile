# LangChain 那一格: 官方对话前端 (agent-chat-ui) + 一个跑在 LangGraph 上的智能体。
#
# 两半都在同一个容器里: 前端默认就连 localhost:2024 (langgraph dev 的端口), 分开
# 部署反而要多一层跨域和鉴权。用户看到的是一个能聊天的智能体, 不是一张"请填写
# deployment URL"的表单 —— 那张表单正是老板铁律里要拆掉的东西, 我们用 env 预置
# 掉 (NEXT_PUBLIC_API_URL / NEXT_PUBLIC_ASSISTANT_ID)。

# ---- 前端构建 ----
FROM node:22-slim AS web
WORKDIR /src
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/langchain-ai/agent-chat-ui .
# NEXT_PUBLIC_* 是**构建期**烧进 bundle 的, 运行时再设没用 —— 这里就得给对。
# NEXT_PRIVATE_STANDALONE: 逼 Next 输出 standalone。上游的 next.config 里没开
# 这个 (他们是给人 pnpm dev 用的), 而不开的话产物没有自带 server, 只能把整个
# node_modules 一起搬进运行镜像 —— 那是 1GB 起。
# **必须是绝对 URL**: LangGraph 的 JS SDK 用 new URL() 解析这个值, 相对路径直接
# 抛 `"/langgraph/threads" cannot be parsed as a URL` —— 页面能开、一发消息就红。
# (2026-09-01 老板实测撞到。我原以为同源相对路径最省事, 那是想当然。)
# 域名要跟着部署走, 所以做成构建参数。
ARG PUBLIC_ORIGIN=https://langchain.dshcloud.online
ENV NEXT_PUBLIC_API_URL=${PUBLIC_ORIGIN}/langgraph \
    NEXT_PUBLIC_ASSISTANT_ID=agent \
    NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PRIVATE_STANDALONE=true
RUN corepack enable && pnpm install --frozen-lockfile && pnpm build

# ---- 运行 ----
FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      curl ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*
COPY agent /opt/agent
RUN pip install --no-cache-dir "langgraph-cli[inmem]" -e /opt/agent
COPY --from=web /src/.next/standalone /opt/web
COPY --from=web /src/.next/static /opt/web/.next/static
COPY front.mjs /opt/front.mjs
EXPOSE 3000
