# Agents Team 云工作台镜像。
#
# 是什么: 一个会**动手**的智能体 —— 在用户自己的容器里跑命令、读写文件、访问网络,
# 前端是我们自己写的聊天界面 (deploy/workspace-agents-team/web)。与 codecli 那条线的
# 区别: 那边给的是编辑器 + 几个 CLI, 用户自己敲; 这边是交代一件事, 它自己做完。
#
# 没有账号体系, 也不该有 —— 域前面压着我们的 forward_auth (见 app/main.py 开头)。
#
# ⚠️ ca-certificates 必须显式装。python:*-slim 不带系统根证书, 而 httpx 走的是
# certifi 自带的一份 —— 于是 Python 侧访问网络一切正常, 智能体用 curl / apt /
# pip 装东西却一律证书错误, 报错还在它的工具输出里, 排查方向完全指偏。
# codecli 那边被同一件事咬过 (Node 有自带副本, Rust 的 Codex 没有)。

ARG PYTHON_VERSION=3.12-slim-bookworm
FROM python:${PYTHON_VERSION}

ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.title="DSH Cloud Operator workspace" \
      org.opencontainers.image.revision="${REVISION}"

# 智能体自己会去装它需要的东西, 这里只铺**它没法自举的那几样**:
# 没有 ca-certificates 装不了任何东西; 没有 curl/git 连第一步都迈不出去;
# procps 是为了让它能 ps 查自己起的后台进程 (slim 里连 ps 都没有)。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      git \
      procps \
      ripgrep \
 && rm -rf /var/lib/apt/lists/*

# 版本钉死: "镜像变了就重建工作台"依赖镜像身份稳定 (workspace._image_is_stale)。
ARG FASTAPI_VERSION=0.121.1
ARG UVICORN_VERSION=0.42.0
ARG HTTPX_VERSION=0.28.1
ARG PLAYWRIGHT_VERSION=1.58.0
RUN pip install --no-cache-dir \
      "fastapi==${FASTAPI_VERSION}" \
      "uvicorn[standard]==${UVICORN_VERSION}" \
      "httpx==${HTTPX_VERSION}" \
      "playwright==${PLAYWRIGHT_VERSION}"

# 浏览器工具用 (见 app/browser.py)。
# **只装 chromium**: `playwright install` 不带参数会把 firefox 和 webkit 也拖下来,
# 三个加起来一 GB 多, 而我们的工具只驱动 chromium。
# `--with-deps` 装的是 Chromium 在 Debian 上的系统依赖 (字体、libnss 之类) ——
# 少了它 Chromium **能装上但起不来**, 报错是一串找不到 .so, 出现在容器 stderr 里,
# 而工具层只会显示成"浏览器启动失败"。
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agents-team
COPY app/ /opt/agents-team/app/
COPY web/ /opt/agents-team/web/
# 万相 3.0 提示词技能包 (百炼官方给的短剧/电商两套, 各十万字)。
# 整包放进来而不是把要点抄进人格: 抄会失真, 也没法随上游更新。
COPY skills/ /opt/agents-team/skills/

# 工作目录挂 NAS (products.py 的 mounts)。先建出来, 免得首轮 shell 落在一个
# 不存在的 cwd 上 —— 那会让第一条命令莫名其妙失败。
RUN mkdir -p /workspace

ENV AGENTS_TEAM_WORKDIR=/workspace \
    PYTHONUNBUFFERED=1

EXPOSE 8710
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8710"]
