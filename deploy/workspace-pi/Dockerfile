# pi 那一格: 老板 2026-09-02 拍板用社区的 pi-web-ui 当前端 ("pi-web-ui 这个吧"),
# 顶掉 OpenHands (5.81GB 单镜像、第四个写代码的 agent、最难部署的一个)。
#
# pi 本身没有网页界面 (只有 TUI / --mode json / RPC / SDK); pi-web-ui 是社区里
# 功能最全的一个: 对话 + 思考块 + 工具调用 + 文件树 + 内置终端 + Git 面板 +
# 在界面里改 models.json, 中英双语, 装完 368MB。它把 pi 当 SDK 在进程内跑,
# 所以一个镜像一个进程, 没有边车。
#
# 实测 (2026-09-02, 探针容器): npm 装 18 秒; 起到能答 2 秒; /api/health 是真接口
# (答 JSON), /health /healthz 是 SPA 兜底 (什么路径都 200 —— 探针别打那两个)。
FROM node:22-bookworm-slim

# node-pty (内置终端) 是原生模块, 没有对上的预编译包时回落到 node-gyp —— 工具链
# 得在。ca-certificates 必须显式装 (slim 镜像不带系统根证书, 见 agentui 那边的
# 教训)。git 给它的 Git 面板和 /workspace 的仓库用。
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      python3 make g++ ca-certificates git curl procps \
 && rm -rf /var/lib/apt/lists/*

# 版本钉死: "镜像变了就重建工作台"依赖镜像身份稳定; 而这个包一天一版
# (0.59.1, 8 月才建), 升级前先 spike。pi 本体 (@earendil-works/pi-coding-agent)
# 作为它的依赖被一起装进来, 不另装一份 —— 两份版本对不上时哪份在跑说不清。
# npm 12+ 默认拦依赖的安装脚本, node-pty 的必须放行 (另两个是无害空操作)。
ARG PI_WEB_UI_VERSION=0.59.1
RUN npm i -g --no-fund --no-audit --allow-scripts=node-pty,@google/genai,protobufjs \
      "pi-web-ui@${PI_WEB_UI_VERSION}" \
 && npm cache clean --force \
 && node -e "console.log('[build] pi SDK', require('/usr/local/lib/node_modules/pi-web-ui/node_modules/@earendil-works/pi-coding-agent/package.json').version)"

# 状态全落 NAS: /root (pi 的配置、会话、pi-web-ui 的数据目录) 与 /workspace。
# 镜像里不预置任何配置 —— models.json 里有该用户的网关令牌, 每次开机由启动脚本写。
RUN mkdir -p /workspace
EXPOSE 8787
ENV PI_WEB_HOST=0.0.0.0 PI_WEB_PORT=8787 PI_WEB_CWD=/workspace

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
      org.opencontainers.image.title="AI Store pi workspace (pi-web-ui)" \
      org.opencontainers.image.revision="${REVISION}"
