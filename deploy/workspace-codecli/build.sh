#!/usr/bin/env bash
# 构建并发布编码智能体工作台镜像 (Claude Code / Codex / Gemini 三个坑位共用)。
#
#   ./build.sh                 # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh     # 只建不推
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

CODE_SERVER_VERSION="${CODE_SERVER_VERSION:-4.135.0}"
CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.251}"
CODEX_VERSION="${CODEX_VERSION:-0.151.0}"
GEMINI_VERSION="${GEMINI_VERSION:-0.57.0}"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/codecli-local}"
TAG="${1:-${CODE_SERVER_VERSION}-r1}"
REF="$IMAGE:$TAG"

docker build \
  --build-arg CODE_SERVER_VERSION="$CODE_SERVER_VERSION" \
  --build-arg CLAUDE_VERSION="$CLAUDE_VERSION" \
  --build-arg CODEX_VERSION="$CODEX_VERSION" \
  --build-arg GEMINI_VERSION="$GEMINI_VERSION" \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" \
  -t "$REF" .

echo "==> 镜像内自检"
docker run --rm -u 0 --entrypoint bash "$REF" -c '
  set -e

  # 1. 系统根证书。**这条是主角**: 缺了它, node 照样一切正常 (它自带一份根证书
  #    副本), 而 Codex (Rust) / 任何读系统根的东西一律握手失败, 服务端一条日志
  #    都没有。2026-08-31 为这个查了两小时。curl 与 Codex 读同一份, 所以用它钉。
  test -s /etc/ssl/certs/ca-certificates.crt || { echo "!! 没有系统根证书" >&2; exit 1; }
  curl -fsS -o /dev/null --max-time 20 https://registry.npmjs.org/ \
    || { echo "!! 系统根证书装了但 TLS 走不通 (Codex 会死在这, 且没有日志)" >&2; exit 1; }
  echo "  ✓ 系统根证书: curl 走得通 TLS"

  # 2. 三个 CLI 都在 PATH 上且能跑起来 (不是只有文件在)。
  for c in claude codex gemini code-server node npm git rg bwrap; do
    command -v "$c" >/dev/null || { echo "!! $c 不在 PATH 上" >&2; exit 1; }
  done
  echo "  ✓ claude: $(claude --version 2>&1 | head -1)"
  echo "  ✓ codex:  $(codex --version 2>&1 | head -1)"
  echo "  ✓ gemini: $(gemini --version 2>&1 | head -1)"

  # 3. 开箱即用的那一下: 扩展在, 且 Copilot 那个要登 GitHub 的面板已经拿掉。
  test -f /usr/lib/code-server/lib/vscode/extensions/dsh-agent/extension.js \
    || { echo "!! dsh-agent 扩展不在 —— 用户进去只会看到一个空编辑器" >&2; exit 1; }
  test ! -d /usr/lib/code-server/lib/vscode/extensions/copilot \
    || { echo "!! Copilot 扩展还在 —— 右侧面板一点就要 GitHub 登录" >&2; exit 1; }
  test -f /usr/lib/code-server/lib/vscode/extensions/anthropic.claude-code/package.json \
    || { echo "!! Claude Code 的 IDE 扩展没装上 —— 终端里会挂一条红字报错" >&2; exit 1; }
  echo "  ✓ dsh-agent 扩展在位, Copilot 面板已移除"

  # 4. code-server 的无鉴权模式确实生效 —— 这是我们选它的**唯一理由**
  #    (老板铁律: 接进来的应用不留登录墙)。上游哪天改了默认值, 这里要红。
  code-server --auth none --bind-addr 127.0.0.1:18080 /home/coder/workspace \
    > /tmp/cs.log 2>&1 &
  for _ in $(seq 1 40); do grep -q "HTTP server listening" /tmp/cs.log && break; sleep 1; done
  grep -q "Authentication is disabled" /tmp/cs.log \
    || { echo "!! code-server 没有关掉鉴权 —— 用户会撞上登录墙:" >&2; tail -20 /tmp/cs.log >&2; exit 1; }
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 http://127.0.0.1:18080/)
  case "$code" in 200|302) ;; *) echo "!! code-server 首页 $code" >&2; tail -20 /tmp/cs.log >&2; exit 1;; esac
  echo "  ✓ code-server: 鉴权已关, 首页 $code"
'

if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
else
  docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"
fi
echo "下一步: deploy/prod/.env 设 CODECLI_IMAGE_REF=$REF, 跑 scripts/safe_deploy.sh"
