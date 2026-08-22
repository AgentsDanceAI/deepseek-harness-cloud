# syntax=docker/dockerfile:1.7
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.11@sha256:8101ad825250a114e7bef89eefaa73c31e34e10ffbe5aff01562740bac97553c
FROM ${UV_IMAGE} AS uv

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
WORKDIR /build
COPY server/pyproject.toml server/uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/opt/dsh-cloud/.venv \
    uv sync --frozen --no-dev --no-install-project --extra postgres --extra redis --extra stripe

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=0.2.0
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.source="https://github.com/AgentsDanceAI/deepseek-harness-cloud" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

ENV PATH=/opt/dsh-cloud/.venv/bin:$PATH \
    PYTHONPATH=/srv/dhc \
    DHC_DATA_DIR=/app/data \
    DHC_RELEASE_FILE=/usr/share/dsh-cloud/release.json \
    RELEASE_VERSION=${VERSION} \
    RELEASE_REVISION=${REVISION}

RUN groupadd --gid 10001 dsh-cloud \
 && useradd --uid 10001 --gid 10001 --home /nonexistent --shell /usr/sbin/nologin dsh-cloud \
 && install -d -o 10001 -g 10001 /app/data \
 && install -d -o 0 -g 0 -m 0755 /srv/dhc /srv/legal /opt/dsh-cloud /usr/share/dsh-cloud

COPY --from=builder --chown=0:0 /opt/dsh-cloud/.venv /opt/dsh-cloud/.venv
COPY --chown=0:0 server/app /srv/dhc/app
COPY --chown=0:0 server/config /srv/dhc/config
COPY --chown=0:0 server/scripts /srv/dhc/scripts
COPY --chown=0:0 release/release.json /usr/share/dsh-cloud/release.json
COPY --chown=0:0 server/legal/ /srv/legal/
COPY --chown=0:0 release/docker_entrypoint.py /usr/local/bin/dsh-cloud-entrypoint

WORKDIR /srv/dhc
VOLUME /app/data
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/usr/local/bin/python3", "-I", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/readyz', timeout=3)"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/python3", "-I", "/usr/local/bin/dsh-cloud-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
