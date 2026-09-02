#!/usr/bin/env bash
# 构建并发布 pi 那一格的镜像 (pi-web-ui + pi SDK)。
#
#   ./build.sh                 # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh     # 只建不推
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/pi-web-ui}"
TAG="${1:-0.59.1-r1}"
REF="$IMAGE:$TAG"

docker build --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" -t "$REF" "$here"

echo "==> 镜像内自检"
docker run --rm -u 0 --entrypoint bash "$REF" -c '
  set -e
  test -s /etc/ssl/certs/ca-certificates.crt || { echo "!! 没有系统根证书" >&2; exit 1; }
  for c in pi-web-ui git node; do command -v "$c" >/dev/null || { echo "!! $c 不在 PATH 上" >&2; exit 1; }; done
  echo "  ✓ pi-web-ui $(pi-web-ui --version 2>/dev/null | head -1 || echo 装了)"

  # 像启动脚本那样预置配置 (假令牌), 起服务, 打真接口
  mkdir -p /root/.pi/agent /workspace
  printf "{\"providers\":{\"dsh\":{\"baseUrl\":\"http://127.0.0.1:1/v1\",\"api\":\"openai-completions\",\"apiKey\":\"x\",\"models\":[{\"id\":\"m\",\"name\":\"m\",\"reasoning\":false,\"input\":[\"text\"],\"cost\":{\"input\":0,\"output\":0,\"cacheRead\":0,\"cacheWrite\":0},\"contextWindow\":128000,\"maxTokens\":8192}]}}}" > /root/.pi/agent/models.json
  printf "{\"defaultProvider\":\"dsh\",\"defaultModel\":\"m\",\"enableAnalytics\":false,\"enableInstallTelemetry\":false}" > /root/.pi/agent/settings.json
  PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 pi-web-ui --host 127.0.0.1 --port 18787 --cwd /workspace --no-browser >/tmp/srv.log 2>&1 &
  for i in $(seq 1 40); do curl -fsS -o /dev/null http://127.0.0.1:18787/api/health 2>/dev/null && break; sleep 1; done

  # /api/health 必须答 JSON —— /health、/healthz 是 SPA 兜底, 什么路径都 200,
  # 拿它们当探针等于没探。
  ct=$(curl -s -o /tmp/h.json -w "%{content_type}" http://127.0.0.1:18787/api/health)
  case "$ct" in *json*) ;; *) echo "!! /api/health 答的不是 JSON ($ct) —— 探针会被 SPA 兜底骗" >&2; cat /tmp/srv.log >&2; exit 1;; esac
  echo "  ✓ /api/health 是真接口: $(head -c 80 /tmp/h.json)"

  code=$(curl -s -o /tmp/i.html -w "%{http_code}" http://127.0.0.1:18787/)
  test "$code" = "200" || { echo "!! 首页 $code" >&2; exit 1; }
  grep -q "pi-web-ui" /tmp/i.html || { echo "!! 首页不是 pi-web-ui" >&2; exit 1; }
  echo "  ✓ 首页: $(grep -oE "<title>[^<]*" /tmp/i.html | head -1)"

  # 没有登录墙 —— 老板铁律。任何一条回 401/403 就说明有人加了账号体系
  # (它有个可选的 PI_WEB_TOKEN, 我们永远不设)。
  for p in / /api/health /api/themes; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:18787$p")
    case "$code" in 401|403) echo "!! $p 返回 $code —— 冒出了登录墙" >&2; exit 1;; esac
  done
  echo "  ✓ 无登录墙"

  # 反代下的 WebSocket 同源校验: Origin 是公网 https 域、Host 也是它 —— 这正是
  # Caddy 转进来的形状。白名单 (PI_WEB_ALLOW_ORIGINS) 由启动脚本按域名设。
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: x.example" -H "Origin: https://x.example" -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" http://127.0.0.1:18787/ws)
  echo "  · 未设白名单时反代形状的 WS 握手 -> $code (启动脚本会设 PI_WEB_ALLOW_ORIGINS)"
'
if [ "${SKIP_PUSH:-0}" = "1" ]; then echo "==> SKIP_PUSH=1, 不推"; else docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"; fi
echo "下一步: deploy/prod/.env 设 PI_IMAGE_REF=$REF PI_DOMAIN=pi.dshcloud.online, 建 ECI 缓存, Caddy 加域, 再 scripts/safe_deploy.sh"
