# claude-code / codex 两格: 社区的 pi-web-ui 当外壳, 我们给它加了第三个引擎
# (PI_WEB_ENGINE=claude|codex), 让它去驱动 Claude Code / Codex 两个 CLI。
#
# **不是从 npm 装 pi-web-ui**, 而是 build.sh 把上游钉住的提交检出来、打上
# patches/ 里三个补丁再构建 —— 引擎代码不在上游的发布包里。这一点与 pi 那格
# (deploy/workspace-pi) 不同, 那边是直接 npm i -g。
#
# 为什么不复用 pi 那格的镜像: 那边把 pi 当 SDK 在进程内跑, 我们这边是 spawn
# claude / codex 两个 CLI —— 镜像里要装的东西不一样。
FROM node:22-bookworm-slim

# node-pty (内置终端) 是原生模块, 没有对上的预编译包时回落到 node-gyp, 工具链
# 得在。**ca-certificates 必须显式装** —— slim 镜像不带系统根证书, 而 claude
# 与 codex 都是自己发 HTTPS 的 (Rust/Node 读系统根), 不装的话表现为连不上网关
# 而不是报证书错。git 给 Git 面板和 /workspace 的仓库用。
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      python3 make g++ ca-certificates git curl procps \
 && rm -rf /var/lib/apt/lists/*

# 两个 CLI 版本钉死: "镜像变了就重建工作台"依赖镜像身份稳定, 而这两个包都是
# 一天一版。适配层解析的 wire 格式是对着这两个版本实测的 (见补丁里的
# tests/unit/cli-engine-adapter.test.ts), 升版本前先跑那组测试。
ARG CLAUDE_VERSION=2.1.193
ARG CODEX_VERSION=0.151.0
RUN npm i -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && claude --version && codex --version

# 外壳本体: build.sh 已经把补丁打好, 这里只装依赖 + 构建。
WORKDIR /srv
COPY . /srv
# --fetch-retries: 这一层要拉 660 个包, 一次 ECONNRESET 就白跑二十分钟 (2026-09-12
# 的 r7 就是这么挂的)。npm 默认只重试 2 次、退避很短, 对这条链路不够。
RUN npm ci --no-audit --no-fund --ignore-scripts \
      --fetch-retries 5 --fetch-retry-mintimeout 5000 --fetch-retry-maxtimeout 120000 \
 && npm rebuild node-pty \
 && npm run build \
 && npm prune --omit=dev \
 && npm cache clean --force

# 状态全落 NAS: /root (两个 CLI 的配置与会话) 与 /workspace。
# **镜像里不预置任何配置** —— codex 的 config.toml 里有该用户的网关令牌, 每次
# 开机由引擎自己写 (server/cli/gateway.ts)。
RUN mkdir -p /workspace
EXPOSE 8787
ENV PI_WEB_HOST=0.0.0.0 PI_WEB_PORT=8787 PI_WEB_CWD=/workspace PI_WEB_MANAGED=1
CMD ["node", "dist/server/index.js"]

# ── 构建元数据放最后 ──────────────────────────────────────────────────────────
# 别挪回开头: ARG 会进后面每条 RUN 的缓存键, 于是 apt / npm 那些几百 MB 的重活层
# 每次构建全部失效 (见 workspace-pi/Dockerfile 里那段实测)。
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="AI Store CLI workspace (pi-web-ui + claude/codex engine)" \
      org.opencontainers.image.revision="${REVISION}"
