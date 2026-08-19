#!/usr/bin/env bash
# dshcloud.online cutover: bring up DHC and (re)write the DHC site blocks in the
# shared Caddy (primary dshcloud.online + legacy open-search.ai compat layer).
# Idempotent; safe to re-run. Run from the repo root.
#
#   bash deploy/open-search/cutover.sh
#
# Prereqs: deploy/open-search/.env filled in; the shared Caddy container ($CADDY_CTR)
# running (it owns 80/443 and the domains' TLS + Cloudflare origin).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE="$REPO/deploy/open-search/compose.yml"
ENVFILE="$REPO/deploy/open-search/.env"
# 这两个是**逐机器**的: 共享 Caddy 的配置文件路径与容器名。写死会让换机迁移时
# 传进来的值被静默忽略 —— 脚本照旧去写源机的路径, 在新机上要么文件不存在直接
# 退出, 要么(更糟)写错文件。所以两者都必须显式传入, 不给默认值 —— 写死或留默认
# 都会在换机时指向上一台机器的路径。
#   CADDYFILE=/path/to/Caddyfile CADDY_CTR=<caddy 容器名> bash deploy/open-search/cutover.sh
CADDYFILE="${CADDYFILE:?set CADDYFILE to the shared Caddy's config path}"
CADDY_CTR="${CADDY_CTR:?set CADDY_CTR to the shared Caddy's container name}"
[ -f "$CADDYFILE" ] || { echo "Caddyfile 不存在: $CADDYFILE (换机时用 CADDYFILE= 指定)"; exit 1; }
docker inspect "$CADDY_CTR" >/dev/null 2>&1 || { echo "Caddy 容器不存在: $CADDY_CTR (换机时用 CADDY_CTR= 指定)"; exit 1; }

[ -f "$ENVFILE" ] || { echo "missing $ENVFILE (copy .env.template and fill secrets)"; exit 1; }

echo "==> 1/6 ensure docker networks exist"
docker network create dhc-net 2>/dev/null || echo "    dhc-net already exists"
docker network create dshwork-net 2>/dev/null || echo "    dshwork-net already exists"

echo "==> 2/6 build + start dhc-server + docker-proxy"
docker compose -f "$COMPOSE" --env-file "$ENVFILE" up -d --build

echo "==> 3/6 attach the shared Caddy to dhc-net (+dshwork-net for work UI proxy)"
docker network connect dhc-net "$CADDY_CTR" 2>/dev/null || echo "    dhc-net already connected"
docker network connect dshwork-net "$CADDY_CTR" 2>/dev/null || echo "    dshwork-net already connected"

echo "==> 4/6 wait for dhc-server health"
for i in $(seq 1 30); do
  if docker exec dhc-server python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/api/health')" 2>/dev/null; then
    echo "    healthy"; break
  fi
  [ "$i" = 30 ] && { echo "    dhc-server did not become healthy"; docker logs --tail 40 dhc-server; exit 1; }
  sleep 2
done

echo "==> 5/6 ensure DHC site blocks in the shared Caddyfile (dshcloud-v3, backup kept)"
# Declarative + idempotent: strip every previously managed block (all
# generations), then append the current set:
#   dshcloud.online        -> dhc-server (primary console/site)
#   www.dshcloud.online    -> 308 to apex
#   work.dshcloud.online   -> dshwork-v2 routing (PWA shell + forward_auth)
# (旧域 open-search.ai 的兼容层已于 2026-08-17 撤除 —— 站主确认前期无用户。
#  LEGACY_HOST 仍保留, 因为下面第 2/3 步要用它把历史遗留的块清理干净。)
PRIMARY_HOST="${PRIMARY_DOMAIN:-dshcloud.online}"
LEGACY_HOST="${LEGACY_DOMAIN:-open-search.ai}"
WORK_HOST="${WORK_DOMAIN:-work.dshcloud.online}"
cp "$CADDYFILE" "$CADDYFILE.bak.$(date +%s 2>/dev/null || echo bak)"
python3 - "$CADDYFILE" "$PRIMARY_HOST" "$LEGACY_HOST" "$WORK_HOST" <<'PY'
import re, sys
p, primary, legacy, work = sys.argv[1:5]
s = open(p, encoding="utf-8").read()
# 1) strip the marker-wrapped v3 section from previous runs (must run first so
#    the host-pattern strips below never touch v3-managed content)
s = re.sub(r"\n?# ── DHC sites v3 BEGIN ──.*?# ── DHC sites v3 END ──\n?", "\n",
           s, flags=re.DOTALL)
# 2) strip the pre-v3 work block (comment + block)
s = re.sub(r"\n?# ── DSH Cloud workspaces[^\n]*\nwork\.[^\s{]+\s*\{.*?\n\}\n?",
           "\n", s, flags=re.DOTALL)
# 3) strip the pre-v3 legacy-domain proxy block
s = re.sub(r"(?ms)^" + re.escape(legacy) + r"\s*\{.*?\n\}\n?", "", s)
s = s.rstrip("\n") + "\n"
block = f"""
# ── DHC sites v3 BEGIN ── (managed by deepseek-harness-cloud cutover.sh; do not hand-edit)
{primary} {{
\treverse_proxy dhc-server:8100 {{
\t\tflush_interval -1
\t}}
}}
www.{primary} {{
\tredir https://{primary}{{uri}} 308
}}
# per-user dsh containers + PWA shell (dshwork-v2 routing):
#  - "/" (+PWA assets) -> dhc-server, which serves the container document with
#    the mobile/PWA layers injected (manifest, icons, service worker, CSS);
#  - everything else passes forward_auth (session -> container upstream) and is
#    reverse-proxied with a loopback Host so dsh's fence trusts it.
{work} {{
\t@pwa path / /index.html /manifest.webmanifest /sw.js /pwa/*
\thandle @pwa {{
\t\t@rootdoc path / /index.html
\t\trewrite @rootdoc /api/work/shell
\t\treverse_proxy dhc-server:8100
\t}}
\thandle {{
\t\tforward_auth dhc-server:8100 {{
\t\t\turi /api/work/route
\t\t\tcopy_headers X-Work-Upstream
\t\t\t# Strip the WS Upgrade header from the AUTH subrequest: dsh's chat
\t\t\t# uses WebSocket upgrades (/api/events.mux, /api/events.host); with
\t\t\t# Upgrade present uvicorn routes the /api/work/route subrequest as a
\t\t\t# WS handshake to an HTTP-only path -> 403 -> forward_auth fails ->
\t\t\t# the whole chat WS is killed (page loads, replies never arrive).
\t\t\theader_up -Upgrade
\t\t}}
\t\treverse_proxy {{http.request.header.X-Work-Upstream}} {{
\t\t\theader_up Host 127.0.0.1:3080
\t\t\theader_up Origin http://127.0.0.1:3080
\t\t\tflush_interval -1
\t\t}}
\t}}
}}
# 旧域 open-search.ai 兼容层已于 2026-08-17 按站主决定撤除 (前期无用户,
# 无已分发的、指向旧域的客户端需要照顾)。撤除后 open-search.ai 不再由本机
# 提供任何服务 —— Caddy 没有它的站点块, CF 回源会拿到 SNI 不匹配而握手失败。
# ⚠️ 若将来发现仍有旧客户端在打旧域, 恢复方式是把下面这段取消注释后重跑本脚本:
#   {legacy} {{
#     @passthrough path /api/* /llm/* /releases/*
#     handle @passthrough {{ reverse_proxy dhc-server:8100 {{ flush_interval -1 }} }}
#     handle {{ redir https://{primary}{{uri}} 308 }}
#   }}
#   work.{legacy} {{ redir https://{work}{{uri}} 308 }}
# ── DHC sites v3 END ──
"""
open(p, "w", encoding="utf-8").write(s + block)
print("    DHC site blocks (v3) written")
PY

echo "==> 5d/6 sync Caddyfile into the running container + reload"
# single-file bind mounts can go stale in a long-running container; push the
# authoritative host file into a container-local path and reload from it.
docker cp "$CADDYFILE" "$CADDY_CTR:/tmp/Caddyfile.dhc"
docker exec "$CADDY_CTR" caddy validate --config /tmp/Caddyfile.dhc --adapter caddyfile
docker exec "$CADDY_CTR" caddy reload --config /tmp/Caddyfile.dhc --adapter caddyfile

echo "==> 6/6 stop the old dsh container"
docker stop dsh 2>/dev/null && echo "    dsh stopped" || echo "    dsh not running"

echo
echo "cutover done. Verify:"
echo "  curl -s https://dshcloud.online/api/health"
echo "  curl -sI https://open-search.ai/ | grep -i location   # 308 -> dshcloud.online"
echo "  (dshcloud.online / www / work DNS records must point at this origin via Cloudflare)"
