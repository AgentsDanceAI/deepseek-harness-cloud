# CloudCLI 云工作台镜像 (社区那个 claude-code-ui 系的聊天式前端)。
#
# 与 workspace-codecli 那个 code-server 版是**两种形态**, 同一批 CLI:
#   · code-server 版: 编辑器 + 文件树, agent 开在编辑器区;
#   · 这个: 聊天式界面, 左边会话列表, 更像一个成品而不是一个 IDE。
# 老板要两版摆一起比, 之后统一成哪种由他定。
#
# 这个镜像是**伴随容器**: 它绑回环, 前面压一个 nginx 主容器负责替用户登录并把
# 会话注入页面 (见 products.py 的 _cloudcli_boot)。CloudCLI 自带账号体系
# (单用户注册 + 账号密码 + 7 天过期的 JWT), 没有关掉鉴权的开关 —— 而老板的
# 铁律是接进来的应用不留登录墙, 所以只能由工作台代持凭据。
#
# ⚠️ ca-certificates 必须显式装: node:22-slim 不带系统根证书, 而 Node 自带一份
# 副本 —— 于是 node 的 fetch 一切正常, 而 Codex (Rust, 读系统根) 一律
# `error sending request` 且服务端零日志。2026-08-31 为这个查了两小时。

FROM node:22-bookworm-slim
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud CloudCLI workspace" \
      org.opencontainers.image.revision="${REVISION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bubblewrap \
      ca-certificates \
      curl \
      git \
      ripgrep \
 && rm -rf /var/lib/apt/lists/*

# 版本全部钉死 —— "镜像变了就重建工作台"依赖镜像身份稳定
# (见 workspace._spec_fingerprint)。升级前先 spike。
ARG CLOUDCLI_VERSION=1.37.2
ARG CLAUDE_VERSION=2.1.251
ARG CODEX_VERSION=0.151.0
RUN npm i -g --no-fund --no-audit --unsafe-perm \
      "@cloudcli-ai/cloudcli@${CLOUDCLI_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

# 工作目录挂 NAS (products.py 的 mounts)。
RUN mkdir -p /workspace
WORKDIR /workspace
