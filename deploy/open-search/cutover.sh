#!/usr/bin/env bash
# open-search.ai cutover: bring up DHC and repoint the shared Caddy from the old
# dsh container to it. Idempotent; safe to re-run. Run from the repo root.
#
#   bash deploy/open-search/cutover.sh
#
# Prereqs: deploy/open-search/.env filled in; the the shared Caddy container
# running (it owns 80/443 and open-search.ai's TLS + Cloudflare origin).
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

echo "==> 5/6 repoint open-search.ai in Caddyfile.gpu (backup kept)"
if grep -q "reverse_proxy dhc-server:8100" "$CADDYFILE"; then
  echo "    already repointed"
else
  cp "$CADDYFILE" "$CADDYFILE.bak.$(date +%s 2>/dev/null || echo bak)"
  # Replace the whole open-search.ai { ... } block with a clean DHC proxy block.
  python3 - "$CADDYFILE" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
block = '''open-search.ai {
\treverse_proxy dhc-server:8100 {
\t\tflush_interval -1
\t}
}'''
# match "open-search.ai {" ... matching closing brace at column 0
new = re.sub(r"open-search\.ai\s*\{.*?\n\}", block, s, count=1, flags=re.DOTALL)
if new == s:
    sys.exit("could not find open-search.ai block to replace")
open(p, "w", encoding="utf-8").write(new)
print("    Caddyfile.gpu open-search.ai block replaced")
PY
fi

echo "==> 5c/6 ensure work.<domain> Caddy block (cloud workspaces, v2 PWA shell)"
WORK_HOST="${WORK_DOMAIN:-work.open-search.ai}"
if grep -q "dshwork-v2" "$CADDYFILE"; then
  echo "    work v2 block already present"
else
  # drop any previous version of the work block first
  python3 - "$CADDYFILE" "$WORK_HOST" <<'PY'
import re, sys
p, host = sys.argv[1], sys.argv[2]
s = open(p, encoding="utf-8").read()
s2 = re.sub(r"\n# ── DSH Cloud workspaces[^\n]*\n" + re.escape(host) + r"\s*\{.*?\n\}\n?",
            "\n", s, flags=re.DOTALL)
open(p, "w", encoding="utf-8").write(s2)
print("    removed old work block" if s2 != s else "    no old work block")
PY
  # Routing (dshwork-v2):
  #  - "/" (+PWA assets) go to dhc-server, which serves the container's document
  #    with mobile/PWA layers injected (manifest, icons, service worker, CSS);
  #  - everything else passes forward_auth (session -> container upstream) and
  #    is reverse-proxied with a loopback Host so dsh's fence trusts it.
  cat >> "$CADDYFILE" <<EOF

# ── DSH Cloud workspaces (dshwork-v2) — per-user dsh containers + PWA shell ──
${WORK_HOST} {
	@pwa path / /index.html /manifest.webmanifest /sw.js /pwa/*
	handle @pwa {
		@rootdoc path / /index.html
		rewrite @rootdoc /api/work/shell
		reverse_proxy dhc-server:8100
	}
	handle {
		forward_auth dhc-server:8100 {
			uri /api/work/route
			copy_headers X-Work-Upstream
		}
		reverse_proxy {http.request.header.X-Work-Upstream} {
			header_up Host 127.0.0.1:3080
			header_up Origin http://127.0.0.1:3080
			flush_interval -1
		}
	}
}
EOF
  echo "    appended ${WORK_HOST} v2 block"
fi

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
echo "  curl -s https://open-search.ai/api/health"
echo "  (work.<domain> also needs a Cloudflare DNS record → this origin)"
