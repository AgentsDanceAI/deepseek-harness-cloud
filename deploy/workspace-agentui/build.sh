#!/usr/bin/env bash
# 构建并发布自研智能体工作台镜像。
#
#   ./build.sh                 # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh     # 只建不推
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.251}"
CODEX_VERSION="${CODEX_VERSION:-0.151.0}"
GEMINI_VERSION="${GEMINI_VERSION:-0.57.0}"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/agentui}"
TAG="${1:-0.1.0}"
REF="$IMAGE:$TAG"

docker build \
  --build-arg CLAUDE_VERSION="$CLAUDE_VERSION" \
  --build-arg CODEX_VERSION="$CODEX_VERSION" \
  --build-arg GEMINI_VERSION="$GEMINI_VERSION" \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" \
  -t "$REF" .

echo "==> 镜像内自检"
docker run --rm -u 0 --entrypoint bash "$REF" -c '
  set -e

  # 1. 系统根证书。缺了它 node 照样一切正常 (自带副本), 而 Codex (Rust) 一律
  #    握手失败且**服务端零日志**。curl 与 Codex 读同一份, 用它钉。
  test -s /etc/ssl/certs/ca-certificates.crt || { echo "!! 没有系统根证书" >&2; exit 1; }
  curl -fsS -o /dev/null --max-time 20 https://registry.npmjs.org/ \
    || { echo "!! 根证书装了但 TLS 走不通 (Codex 会死在这, 且没有日志)" >&2; exit 1; }
  echo "  ✓ 系统根证书: curl 走得通 TLS"

  for c in claude codex gemini git rg bwrap python3 uvicorn; do
    command -v "$c" >/dev/null || { echo "!! $c 不在 PATH 上" >&2; exit 1; }
  done
  echo "  ✓ claude: $(claude --version 2>&1 | head -1)"
  echo "  ✓ codex:  $(codex --version 2>&1 | head -1)"

  # 2. 前端依赖必须**烤在镜像里**。运行时去 CDN 拿的话, 实例出网抖一下界面就
  #    白屏, 而且等于把用户的浏览器指向第三方。
  test -s /srv/web/vendor/marked.js \
    || { echo "!! marked 不在镜像里 —— 正文渲染会坏" >&2; exit 1; }
  echo "  ✓ 正文渲染依赖已内置 (marked $(wc -c < /srv/web/vendor/marked.js) 字节)"

  # 终端交给 ttyd —— 自己写的那套 PTY 反复出转义序列乱码, 换掉了。
  command -v ttyd >/dev/null || { echo "!! ttyd 不在镜像里 —— 终端整块坏掉" >&2; exit 1; }
  echo "  ✓ ttyd: $(ttyd --version 2>&1 | head -1)"

  # 3. 服务起得来, 且**四条关键接口都在**。少任何一条都是某个标签页整块坏掉,
  #    而用户看到的只是"点了没反应"。
  uvicorn app.main:app --host 127.0.0.1 --port 18080 > /tmp/srv.log 2>&1 &
  for _ in $(seq 1 40); do
    curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:18080/api/health 2>/dev/null && break
    sleep 1
  done
  for p in /api/health /api/config /api/sessions /api/files /api/git/status; do
    code=$(curl -s -o /tmp/r.json -w "%{http_code}" --max-time 10 "http://127.0.0.1:18080$p")
    test "$code" = "200" || { echo "!! $p 返回 $code" >&2; tail -20 /tmp/srv.log >&2; exit 1; }
  done
  echo "  ✓ 接口齐: health/config/sessions/files/git"

  # 4. 终端反代能出内容。ttyd 是按需起的, 所以这一下也顺带验了"按需拉起"这条路。
  code=$(curl -s -o /tmp/t.html -w "%{http_code}" --max-time 25 http://127.0.0.1:18080/terminal/)
  test "$code" = "200" || { echo "!! /terminal/ 返回 $code —— 终端整块坏掉" >&2; tail -20 /tmp/srv.log >&2; exit 1; }
  grep -qi "ttyd\|terminal" /tmp/t.html || { echo "!! /terminal/ 返回的不是 ttyd 的页面" >&2; exit 1; }
  echo "  ✓ 终端反代: ttyd 页面出得来"

  # 5. 首页真的能出来 (静态挂载顺序错了的话这里会 404 —— 它挂在 / 上, 很容易
  #    被别的路由吃掉)。
  code=$(curl -s -o /tmp/idx.html -w "%{http_code}" --max-time 10 http://127.0.0.1:18080/)
  test "$code" = "200" || { echo "!! 首页 $code" >&2; exit 1; }
  grep -q "DSH Cloud" /tmp/idx.html || { echo "!! 首页内容不对" >&2; exit 1; }
  for a in /static/app.js /static/style.css /static/vendor/xterm.js; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:18080$a")
    test "$code" = "200" || { echo "!! 静态资源 $a 返回 $code" >&2; exit 1; }
  done
  echo "  ✓ 首页与静态资源都在"

  # 6. 没有登录墙 —— 这是老板的铁律, 也是我们自研的前提之一。任何一条接口回
  #    401/403 就说明有人加了账号体系。
  for p in / /api/sessions /api/config; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:18080$p")
    case "$code" in 401|403) echo "!! $p 返回 $code —— 冒出了登录墙" >&2; exit 1;; esac
  done
  echo "  ✓ 无登录墙"
'

if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
else
  docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"
fi
echo "下一步: deploy/prod/.env 设 AGENTUI_IMAGE_REF=$REF, 跑 scripts/safe_deploy.sh"
