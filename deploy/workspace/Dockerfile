# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
FROM ${NODE_IMAGE}
ARG HARNESS_RUNTIME
ARG VERSION

RUN test -n "${HARNESS_RUNTIME}" \
 && apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 make g++ socat ca-certificates git \
 && npm install -g "@deepseek-ai/dsh@${HARNESS_RUNTIME}" \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/* /root/.npm

RUN groupadd --gid 10001 dsh \
 && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash dsh \
 && install -d -o 10001 -g 10001 /workspace

USER 10001:10001
WORKDIR /workspace
EXPOSE 3081

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
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="LicenseRef-DSH-Cloud-Community-1.0"
