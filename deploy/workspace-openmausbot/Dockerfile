# OpenMausBot 那一格 (老板 2026-09-05 定: 拿掉 CrewAI, 换成这个)。
#
# 上游 milind-soni/OpenMausBot (Apache-2.0): 一个 Telegram 样式的群聊, 群里是多个
# 各有性格的机器人, 每个背后是一个命令行智能体, 能开电脑、连应用、跑工具。
# 上游自己那份 Dockerfile 建的是纯服务端 (不含 Electron), UI 与 API 合在一个端口,
# 这里照它建, 再叠三样我们必须自己加的东西:
#
#  1. **nginx 外壳**。它的服务端只监听回环, 而且判定"本机可信"看的是 **Host 头 +
#     有没有代理头** (server/request-auth.ts: isLoopbackHost + isProxied), 不是连接
#     来源。我们的反代两条都不满足 —— 实测 /api/auth/session 返回 403 "this request
#     came through a proxy (pair this device...)", 也就是配对墙。外壳把工作台代持的
#     会话注入到**上游方向**, 浏览器那边什么都不用拿; 顺带出 /__dsh_ready 就绪标记
#     (它的 /api/health 配没配对都答 200, 拿来当探针会把用户放进配对页)。
#  2. **引擎 CLI**。它的模型走 claude / codex 命令行 (自定义引擎里那条 openai-compat
#     明确不支持工具调用, 接了等于把能操作电脑的智能体降级成聊天框, 不能用)。
#     CLI 烤进镜像, 运行期由 products.py 把两个 BASE_URL 指到我们网关。
#  3. **首跑向导**。claude 的 ~/.claude.json 预置 (与 agentui 那格同一份), 否则机器人
#     第一次说话卡在选主题。这一步在启动脚本里做 —— /root 是挂进来的, 构建期写不到。
#
# 上游还在快速迭代 (package.json 0.1.56), 所以**钉死提交**, 升级前先 spike。
FROM node:24-bookworm-slim AS build
ARG OMB_COMMIT=acf88c4
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10.33.0
# Electron 只用于桌面壳, 服务端用不着 —— 不下那 100+MB 的二进制
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
WORKDIR /src
RUN git clone -q https://github.com/milind-soni/OpenMausBot . \
 && git checkout -q "${OMB_COMMIT}" \
 && echo "[build] OpenMausBot $(git rev-parse --short HEAD) $(node -p "require('./package.json').version")"
RUN pnpm install --frozen-lockfile
RUN pnpm build:server && pnpm exec vite build \
 && test -f dist-server/index.js && test -f dist/index.html

FROM node:24-bookworm-slim
ARG OMB_COMMIT=acf88c4

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      nginx ca-certificates curl git tini procps \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nginx/sites-enabled/default

# 引擎 CLI。版本与 agentui 那格保持一致 —— 同一套网关接线, 出问题好对照。
ARG CLAUDE_VERSION=2.1.251
ARG CODEX_VERSION=0.151.0
RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && claude --version && codex --version

WORKDIR /app
COPY --from=build /src/dist-server ./dist-server
COPY --from=build /src/dist ./dist

# 数据落 /root/.openmausbot —— /root 是我们挂进来的那份 (同步器把它当 home 推 OSS),
# 用户的机器人、房间、连接的应用因此跨实例保留。
ENV HOME=/root \
    OMB_DATA_DIR=/root/.openmausbot \
    OMB_STATIC_DIR=/app/dist \
    OMB_PORT=8799 \
    OMB_WEBHOOK_PORT=8800 \
    NODE_ENV=production
EXPOSE 80
ENTRYPOINT ["/usr/bin/tini","--"]

# ── 构建元数据放最后 ──────────────────────────────────────────────────────────
# **别把这两行挪回文件开头。** REVISION 的值是 git sha, 每次构建都变; BuildKit 会把
# 作用域内的 ARG 带进后面每一条 RUN 的缓存键 (层历史里能看到 `RUN |1 REVISION=...`),
# 于是 ARG 之后的所有层每次构建全部失效 —— apt、npm i -g、venv 这些几百 MB 的重活
# 层一个都复用不上。实测 (2026-09-07, 比对 registry 里相邻两版的层摘要):
# agentui 连续三版每次重下 326 MB (81%), 而真正变的只有 COPY 进去的 0.1 MB;
# comfy-local 95.8% 等于整包重拉。放到末尾之后, 前面的层跨版本全部复用。
# LABEL 不产生文件系统层, 放最后零成本。
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud OpenMausBot workspace" \
      org.opencontainers.image.revision="${REVISION}" \
      dsh.upstream="https://github.com/milind-soni/OpenMausBot@${OMB_COMMIT}"
