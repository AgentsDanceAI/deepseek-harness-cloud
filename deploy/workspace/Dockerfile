# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
FROM ${NODE_IMAGE}
ARG HARNESS_RUNTIME
ARG VERSION
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

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
