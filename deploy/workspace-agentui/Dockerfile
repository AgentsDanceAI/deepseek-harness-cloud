# DSH Cloud 自研智能体工作台。
#
# 老板 2026-08-31 拍板自研, 顶掉 CloudCLI。理由不是"别人的做得不好", 是三条我们
# 绕不过去的:
#   · 别人的界面里挂着别人的引流入口 (Star / Join Community / Report Issue),
#     而用户付的是我们的钱;
#   · **积分是我们的核心机制, 在别人的 UI 里没有位置** —— 余额、本轮消耗、剩余
#     分钟, 这些只有自己写才放得进去;
#   · 现成那个的 provider 里没有 Gemini, 四条线永远统一不了。
# 附带的好处: 为了绕开 CloudCLI 自带的账号体系, 我写过四段补丁 (登录墙/向导/
# 空项目/项目根目录), 每一段都可能被上游一次更新打掉。自己写就没有这回事。
#
# ⚠️ ca-certificates 必须显式装: node/python slim 镜像不带系统根证书, 而 Node
# 自带一份副本 —— 于是 node 的 fetch 一切正常, 而 Codex (Rust, 读系统根) 一律
# `error sending request` 且服务端零日志。2026-08-31 为这个查了两小时。

FROM node:22-bookworm-slim AS vendor
# 前端依赖在**构建期**取, 运行时不碰网络。用 CDN 的话: 实例出网抖一下界面就白屏,
# 而且等于把用户的浏览器指向第三方。
ARG XTERM_VERSION=5.5.0
ARG MARKED_VERSION=15.0.7
WORKDIR /v
RUN npm i --no-fund --no-audit "@xterm/xterm@${XTERM_VERSION}" "marked@${MARKED_VERSION}" \
 && mkdir -p /out \
 && cp node_modules/@xterm/xterm/lib/xterm.js /out/xterm.js \
 && cp node_modules/@xterm/xterm/css/xterm.css /out/xterm.css \
 && cp node_modules/marked/marked.min.js /out/marked.js \
 && test -s /out/xterm.js && test -s /out/marked.js

FROM node:22-bookworm-slim
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud agent workspace (自研)" \
      org.opencontainers.image.revision="${REVISION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bubblewrap \
      ca-certificates \
      curl \
      git \
      python3 \
      python3-pip \
      python3-venv \
      ripgrep \
 && rm -rf /var/lib/apt/lists/*

# 三个 CLI 的版本钉死 —— "镜像变了就重建工作台"依赖镜像身份稳定
# (见 workspace._spec_fingerprint)。升级前先 spike。
ARG CLAUDE_VERSION=2.1.251
ARG CODEX_VERSION=0.151.0
ARG GEMINI_VERSION=0.57.0
RUN npm i -g --no-fund --no-audit --unsafe-perm \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
      "@google/gemini-cli@${GEMINI_VERSION}" \
 && npm cache clean --force

# 后端。装进 venv —— Debian 的 python3 是 externally-managed, 直接 pip 会被拒。
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir \
      "fastapi==0.115.6" "uvicorn[standard]==0.34.0" "httpx==0.28.1" \
      "websockets==14.1"
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /srv
COPY app /srv/app
COPY web /srv/web
COPY --from=vendor /out /srv/web/vendor

# 跑 agent 的非 root 用户。
#
# **不是可选项**: Claude Code 明确拒绝以 root 跑 bypassPermissions
# ("--dangerously-skip-permissions cannot be used with root/sudo privileges"),
# 而托管环境里让用户逐条确认工具调用等于把"云端 agent"变成"你得一直盯着的
# agent"。而 NAS 挂进来的 /root 与 /workspace 属主是 root —— 所以启动脚本要先
# 把这两处 chown 给它 (见 products.py 的 _agentui_boot), 否则 agent 连自己的
# 会话文件都写不进去, 而它只会在日志里抱怨一句然后照常跑, 用户是回收之后才
# 发现东西没了。
# 终端用 **ttyd** (开源, C 写的 libwebsockets + xterm.js), 不用我们自己写的 PTY。
#
# 老板 2026-08-31: "终端还是不行啊, 不行你就植入个开源的"。他是对的 —— 我手写
# 那套 PTY 反复出转义序列的乱码 (先是一屏 `vvvv`, 修了之后又是一屏 `$$$$`),
# 而终端仿真里"尺寸协商 + 转义序列 + 键盘编码"这几件事就是有一堆边角, 值不得
# 我们自己趟。ttyd 是这件事上被用了很多年的实现。
# (他提的 ghostty 是原生 GPU 终端, 跑不进浏览器; 浏览器里对应的就是 ttyd。)
#
# 校验 sha256 —— 从网上取二进制不核对等于把镜像的完整性交给别人的 CDN。
ARG TTYD_VERSION=1.7.7
ARG TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
RUN curl -fsSL -o /usr/local/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
 && echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c - \
 && chmod +x /usr/local/bin/ttyd \
 && ttyd --version

# 直接复用 node 镜像自带的 node 用户 (uid 1000) —— 另建一个会撞 uid, 而且
# 多一个用户就多一处要同步的属主。
ARG AGENT_UID=1000
RUN getent passwd ${AGENT_UID} >/dev/null \
 && install -d -o ${AGENT_UID} -g ${AGENT_UID} /home/agent \
 && echo "agent home 归 uid ${AGENT_UID}"

RUN mkdir -p /workspace
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
