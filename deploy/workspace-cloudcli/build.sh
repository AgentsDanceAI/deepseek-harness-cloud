#!/usr/bin/env bash
# 构建并发布 CloudCLI 工作台镜像 (伴随容器; 主容器是 stock nginx)。
#
#   ./build.sh                 # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh     # 只建不推
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

CLOUDCLI_VERSION="${CLOUDCLI_VERSION:-1.37.2}"
CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.251}"
CODEX_VERSION="${CODEX_VERSION:-0.151.0}"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/cloudcli-local}"
TAG="${1:-${CLOUDCLI_VERSION}-r1}"
REF="$IMAGE:$TAG"

docker build \
  --build-arg CLOUDCLI_VERSION="$CLOUDCLI_VERSION" \
  --build-arg CLAUDE_VERSION="$CLAUDE_VERSION" \
  --build-arg CODEX_VERSION="$CODEX_VERSION" \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" \
  -t "$REF" .

echo "==> 镜像内自检"
docker run --rm -u 0 --entrypoint bash "$REF" -c '
  set -e

  # 1. 系统根证书 —— 缺了它 node 照样一切正常 (自带副本), 而 Codex (Rust) 一律
  #    握手失败且服务端零日志。用 curl 钉 (它与 Codex 读同一份)。
  test -s /etc/ssl/certs/ca-certificates.crt || { echo "!! 没有系统根证书" >&2; exit 1; }
  curl -fsS -o /dev/null --max-time 20 https://registry.npmjs.org/ \
    || { echo "!! 根证书装了但 TLS 走不通 (Codex 会死在这, 且没有日志)" >&2; exit 1; }
  echo "  ✓ 系统根证书: curl 走得通 TLS"

  for c in cloudcli claude codex node npm git rg bwrap; do
    command -v "$c" >/dev/null || { echo "!! $c 不在 PATH 上" >&2; exit 1; }
  done
  echo "  ✓ claude: $(claude --version 2>&1 | head -1)"
  echo "  ✓ codex:  $(codex --version 2>&1 | head -1)"

  # 2. 服务起得来, 且**鉴权接口是我们预期的那三个** —— 主容器的免登录流程完全
  #    依赖它们 (register/login/status)。上游哪天改路由, 这里要红, 而不是让
  #    用户去撞一堵登录墙。
  cloudcli start > /tmp/cc.log 2>&1 &
  for _ in $(seq 1 60); do
    curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:3001/ 2>/dev/null && break
    sleep 1
  done
  curl -fsS -o /dev/null --max-time 10 http://127.0.0.1:3001/ \
    || { echo "!! CloudCLI 首页起不来:" >&2; tail -20 /tmp/cc.log >&2; exit 1; }
  code=$(curl -s -o /tmp/st.json -w "%{http_code}" --max-time 10 http://127.0.0.1:3001/api/auth/status)
  test "$code" = "200" || { echo "!! /api/auth/status 返回 $code — 免登录流程靠它判断要不要注册" >&2; exit 1; }
  echo "  ✓ CloudCLI $(cloudcli --version 2>/dev/null | tail -1): 首页与 /api/auth/status 都在 ($(cat /tmp/st.json | head -c 80))"
'

if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
else
  docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"
fi
echo "下一步: deploy/prod/.env 设 CLOUDCLI_IMAGE_REF=$REF, 跑 scripts/safe_deploy.sh"
