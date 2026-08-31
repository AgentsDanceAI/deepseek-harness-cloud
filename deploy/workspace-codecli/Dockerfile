# 编码智能体云工作台镜像 (Claude Code / Codex / Gemini 三个坑位共用)。
#
# 为什么是 code-server 而不是某个专用聊天前端:
#   老板的铁律是"接进来的应用一律不留登录墙"。社区那几个 CLI 前端 (CloudCLI /
#   claude-code-ui 系) 都自带账号体系 —— 7 天过期的 JWT + 单用户注册, 要靠我们
#   替用户登一次再注入会话才能进去。这条路我在 Dify/Hermes/OpenClaw 上走过三次,
#   每次都出过"会话过期就把人永久锁在登录页"的事故。code-server 有原生的
#   `--auth none`, 没有这回事; 而且它一个镜像同时装得下四个 CLI, 用户还多拿一个
#   文件树和编辑器 —— 这几个 CLI 本来就是改代码用的。
#
# 每个产品跑的是**同一个镜像**, 区别只在启动脚本 (products.py 的 _codecli_boot)
# 写进去的终端配置和网关 env。
#
# ⚠️ ca-certificates 必须显式装。2026-08-31 踩过: node:22-slim 不带系统根证书,
# 而 Node 自带一份副本 —— 于是 node 的 fetch 一切正常, Codex (Rust, 读系统根)
# 却一律 `error sending request`, 服务端**一条日志都没有**。查了两小时。
# 自检里那条 curl 就是钉这个的 (curl 同样读系统根)。

ARG CODE_SERVER_VERSION=4.135.0
ARG NODE_VERSION=22-bookworm-slim

FROM node:${NODE_VERSION} AS node

FROM codercom/code-server:${CODE_SERVER_VERSION}
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud coding-agent workspace" \
      org.opencontainers.image.revision="${REVISION}"

USER root

# code-server 官方镜像**不带 node/npm** (它把 node 藏在自己的包里, 不上 PATH)。
# 从官方 node 镜像整段搬过来, 而不是 apt 装 —— Debian 的 nodejs 是 18,
# 这三个 CLI 都要 20+。
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      ripgrep \
 && rm -rf /var/lib/apt/lists/*

# 三个 CLI 的版本钉死 —— "镜像变了就重建工作台"依赖镜像身份稳定 (见
# workspace._spec_fingerprint)。升级前先 spike。
ARG CLAUDE_VERSION=2.1.251
ARG CODEX_VERSION=0.151.0
ARG GEMINI_VERSION=0.57.0
RUN npm i -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
      "@google/gemini-cli@${GEMINI_VERSION}" \
 && npm cache clean --force

# 删掉自带的 Copilot 扩展。它给右侧那个 "Build with Agent" 面板供电, 而那个
# 面板一点就要 GitHub OAuth (日志里明写 authorization_servers:
# github.com/login/oauth) —— 等于我们自己的产品里又藏了一道登录墙, 正是老板
# 明令不要的东西。而且它是多余的: 这个产品的 agent 就是终端里那个 CLI。
RUN rm -rf /usr/lib/code-server/lib/vscode/extensions/copilot

# 开箱即用的那一下: 启动就把 agent 开在终端里。
#
# 不用 VS Code 的 folderOpen 自动任务 —— 实测它不跑, 而且它的前置条件 (工作区
# 信任、task.allowAutomaticTasks、窗口是不是"新开的") 每一条都可能让它静默不
# 执行, 用户看到的就是一个空编辑器。内置扩展是确定的: 扩展宿主一起来就执行。
RUN set -e; \
    D=/usr/lib/code-server/lib/vscode/extensions/dsh-agent; \
    mkdir -p "$D"; \
    printf '%s\n' \
      '{' \
      '  "name": "dsh-agent",' \
      '  "displayName": "DSH Cloud Agent",' \
      '  "publisher": "dshcloud",' \
      '  "version": "1.0.0",' \
      '  "engines": { "vscode": "^1.60.0" },' \
      '  "main": "./extension.js",' \
      '  "activationEvents": ["onStartupFinished"],' \
      '  "contributes": {}' \
      '}' > "$D/package.json"; \
    printf '%s\n' \
      'const vscode = require("vscode");' \
      'function activate() {' \
      '  if (!process.env.DSH_AGENT_CMD) return;' \
      '  // 只叫 VS Code "把终端面板开出来" —— 默认终端配置文件已经是 agent 本身' \
      '  // (products.py 的 _codecli_boot 写的), 所以它建出来的就是我们要的那个。' \
      '  //' \
      '  // 不自己 createTerminal: 面板被打开时 VS Code **还会按默认配置文件再建' \
      '  // 一个**, 于是两个终端并排、两个 agent 进程各烧各的积分 (实测)。' \
      '  // 也不能只 t.show(): 终端建出来了、agent 也在跑, 而面板仍然是收起的,' \
      '  // 用户看到的还是一个空编辑器 (也实测过)。' \
      '  //' \
      '  // 已有终端时这条命令是聚焦而不是新建 —— 重载窗口不会多起 agent。' \
      '  setTimeout(() => {' \
      '    vscode.commands.executeCommand("workbench.action.terminal.focus");' \
      '  }, 1500);' \
      '}' \
      'function deactivate() {}' \
      'module.exports = { activate, deactivate };' > "$D/extension.js"

# 工作目录挂 NAS (products.py 的 mounts)。这里先建出来, 免得 code-server
# 打开一个不存在的目录时退回欢迎页。
RUN mkdir -p /home/coder/workspace && chown -R coder:coder /home/coder

USER coder
