#!/usr/bin/env bash
# dshcloud.online cutover: bring up DHC and (re)write the DHC site blocks in the
# shared Caddy (primary dshcloud.online + legacy open-search.ai compat layer).
# Idempotent; safe to re-run. Run from the repo root.
#
#   bash deploy/open-search/cutover.sh
#
# Prereqs: deploy/open-search/.env filled in; the the shared Caddy container
# running (it owns 80/443 and the domains' TLS + Cloudflare origin).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE="$REPO/deploy/open-search/compose.yml"
ENVFILE="$REPO/deploy/open-search/.env"
CADDYFILE="***REDACTED-PATH***/deploy/gpu-node/Caddyfile.gpu"
CADDY_CTR="the shared Caddy"

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

echo "==> 5/6 ensure DHC site blocks in Caddyfile.gpu (dshcloud-v3, backup kept)"
# Declarative + idempotent: strip every previously managed block (all
# generations), then append the current set:
#   dshcloud.online        -> dhc-server (primary console/site)
#   www.dshcloud.online    -> 308 to apex
#   work.dshcloud.online   -> dshwork-v2 routing (PWA shell + forward_auth)
#   open-search.ai         -> /api /llm /releases passthrough (published
#                             installers keep working), pages 308 to primary
#   work.open-search.ai    -> 308 to work.dshcloud.online
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
# legacy domain: APIs/downloads keep serving (already-shipped desktop builds,
# device tokens, webhooks), everything else redirects to the primary domain.
{legacy} {{
\t@passthrough path /api/* /llm/* /releases/*
\thandle @passthrough {{
\t\treverse_proxy dhc-server:8100 {{
\t\t\tflush_interval -1
\t\t}}
\t}}
\thandle {{
\t\tredir https://{primary}{{uri}} 308
\t}}
}}
work.{legacy} {{
\tredir https://{work}{{uri}} 308
}}
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
