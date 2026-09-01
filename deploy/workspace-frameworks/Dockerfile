# OpenManus 与 CrewAI 合用一个镜像 —— 两个产品, 一份 ECI 缓存。
#
# 它们都**没有界面**: OpenManus 是 `python main.py` 的命令行智能体, CrewAI 是个
# Python 库 (仓库里的 Web 端是他家的商业云 app.crewai.com, 不是开源的)。所以这
# 一格的界面是浏览器里的终端 (ttyd), 卖的是"开箱即用": 框架装好、网关配好、
# 打开就能跑, 用户不用自己折腾 pip 和 API key。
#
# ⚠️ **两个框架必须各用一个虚拟环境**: 它们要的 openai 版本不兼容 ——
# OpenManus 钉 openai~=1.66.3, 而 crewai 要更新的 (缺
# ChatCompletionMessageFunctionToolCall 就 import 失败)。装进同一个 site-packages
# 时后装的那个把先装的顶掉, 而**症状出现在用户敲第一条命令的时候**, 不是构建时。
# 合并镜像的收益 (共用一份 ECI 缓存、少一次冷启动拉取) 与隔离并不冲突。
FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      git curl ca-certificates build-essential tini \
 && rm -rf /var/lib/apt/lists/*

# ---- CrewAI 的环境 ----
# **不装 crewai-tools**: 它拖 lancedb / embedchain / pyarrow 一大串, 而那些是按
# 用途选的 —— 装全等于替用户决定他要哪套工具。要用 pip install 一句的事。
RUN python -m venv /opt/venv-crewai \
 && /opt/venv-crewai/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv-crewai/bin/pip install --no-cache-dir crewai \
 && /opt/venv-crewai/bin/python -c "import crewai; print('[build] CrewAI 可导入', crewai.__version__)"

# ---- OpenManus 的环境 ----
# datasets 与 browsergym 是做基准测试用的, 不在智能体主循环里 —— 而它俩会把
# CUDA 版 torch (nvidia 2.7G + torch 1.2G + triton 691M) 连同 transformers、
# pyarrow 一起拖进来, 光这些就 4.8GB, 而这个容器里根本没有 GPU。
#
# **必须先有 config/config.toml**: OpenManus 在 import 时就构造 Config(), 文件不在
# 就当场抛。而且 [daytona] 那一段也是必须的 —— DaytonaSettings.daytona_api_key
# 没有默认值, 缺了它同样在 import 期抛 pydantic 校验错误, 而上游自己的
# config.example.toml 里压根没有这一段 (照着样例走的人一样起不来)。
#
# **去掉依赖就得当场证明它还能起**: 下面那行 import 自检少了什么当场构建失败,
# 而不是等上线后用户敲下第一条命令才炸。
ARG OPENMANUS_REF=main
RUN git clone --depth 1 --branch ${OPENMANUS_REF} https://github.com/FoundationAgents/OpenManus /opt/openmanus \
 && grep -vE '^(datasets|browsergym)' /opt/openmanus/requirements.txt > /tmp/req.txt \
 && python -m venv /opt/venv-openmanus \
 && /opt/venv-openmanus/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv-openmanus/bin/pip install --no-cache-dir -r /tmp/req.txt \
 && mkdir -p /opt/openmanus/config \
 && printf '[llm]\nmodel = "placeholder"\nbase_url = "http://127.0.0.1"\napi_key = "placeholder"\nmax_tokens = 8192\ntemperature = 0.0\n\n[daytona]\ndaytona_api_key = "unused"\n' > /opt/openmanus/config/config.toml \
 && cd /opt/openmanus \
 && /opt/venv-openmanus/bin/python -c "import app.agent.manus, app.llm, app.tool; print('[build] OpenManus 主循环可导入')"

# 这两个框架都没有界面, 所以这一格的界面就是一个浏览器里的终端。用 ttyd
# (开源, libwebsockets + xterm.js) —— agentui 那个镜像用的是同一个, 自己写 PTY
# 的坑这个会话里已经踩过一轮。
#
# 校验 sha256: 从网上取二进制不核对等于把镜像的完整性交给别人的 CDN。
ARG TTYD_VERSION=1.7.7
ARG TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
RUN curl -fsSL -o /usr/local/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
 && echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c - \
 && chmod +x /usr/local/bin/ttyd \
 && ttyd --version

WORKDIR /workspace
EXPOSE 7681
ENTRYPOINT ["/usr/bin/tini","--"]
