#!/usr/bin/env bash
# =============================================================================
#  DSH Cloud — one-command self-host bootstrap
#  一键自部署：拷贝 .env → 生成密钥 → 起服务 → 健康检查
#
#    ./scripts/quickstart.sh                          # interactive
#    ./scripts/quickstart.sh --domain localhost -y    # local trial, no TLS
#    ./scripts/quickstart.sh --domain dsh.example.com \
#        --admin-email you@example.com --upstream-key sk-xxx -y
#
#  Safe to re-run: an existing deploy/selfhost/.env is never overwritten, only
#  the values you pass as flags are updated. 可重复执行，不会覆盖已有 .env。
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_DIR="$REPO/deploy/selfhost"
ENV_FILE="$STACK_DIR/.env"
ENV_TEMPLATE="$STACK_DIR/.env.example"

DOMAIN_ARG=""
ADMIN_ARG=""
UPSTREAM_KEY_ARG=""
UPSTREAM_BASE_ARG=""
ENABLE_WORK=0
ASSUME_YES=0

# --- tiny helpers ------------------------------------------------------------
c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_off=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_bold" "$c_off" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s  !!%s %s\n' "$c_ylw" "$c_off" "$*"; }
die()  { printf '%s error:%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: scripts/quickstart.sh [options]

  --domain <host>         Public hostname, or "localhost" for a local no-TLS
                          trial run. 公网域名，或 localhost 本地体验。
  --admin-email <email>   Account registered with this address becomes admin.
  --upstream-key <key>    OpenAI-compatible upstream API key (required for the
                          model gateway to answer anything).
  --upstream-base <url>   Upstream base URL including the version path
                          (default https://api.deepseek.com/v1).
  --work                  Enable cloud workspaces (needs a real domain, the
                          host docker engine and the dsh image).
  -y, --yes               Never prompt; use defaults for anything not passed.
  -h, --help              This text.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)        DOMAIN_ARG="${2:-}"; shift 2 ;;
    --admin-email)   ADMIN_ARG="${2:-}"; shift 2 ;;
    --upstream-key)  UPSTREAM_KEY_ARG="${2:-}"; shift 2 ;;
    --upstream-base) UPSTREAM_BASE_ARG="${2:-}"; shift 2 ;;
    --work)          ENABLE_WORK=1; shift ;;
    -y|--yes)        ASSUME_YES=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *)               usage; die "unknown option: $1" ;;
  esac
done

set_kv() { # set_kv <file> <KEY> <VALUE>  — replace in place or append
  local file="$1" tmp
  tmp="$(mktemp)"
  QS_KEY="$2" QS_VALUE="$3" awk '
    BEGIN { k = ENVIRON["QS_KEY"]; v = ENVIRON["QS_VALUE"]; hit = 0 }
    !hit && index($0, k "=") == 1 { print k "=" v; hit = 1; next }
    { print }
    END { if (!hit) print k "=" v }
  ' "$file" >"$tmp" && mv "$tmp" "$file"
}

get_kv() { # get_kv <file> <KEY>
  QS_KEY="$2" awk 'BEGIN { k = ENVIRON["QS_KEY"] }
    index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1"
}

ask() { # ask <prompt> <default>
  local prompt="$1" def="${2:-}" ans=""
  if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then printf '%s' "$def"; return; fi
  if [ -n "$def" ]; then printf '%s [%s]: ' "$prompt" "$def" >&2
  else printf '%s: ' "$prompt" >&2; fi
  read -r ans || ans=""
  printf '%s' "${ans:-$def}"
}

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then python3 -c 'import secrets; print(secrets.token_hex(32))'
  else od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; echo; fi
}

# --- 1/5 prerequisites -------------------------------------------------------
info "1/5 checking prerequisites"
command -v docker >/dev/null 2>&1 || die "docker not found — install Docker Engine or Docker Desktop first."
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found (this stack needs 'docker compose', not 'docker-compose')."
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon — is it running, and is your user in the docker group?"
[ -f "$ENV_TEMPLATE" ] || die "missing $ENV_TEMPLATE — run this from a full checkout of the repo."
ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'), compose $(docker compose version --short 2>/dev/null || echo '?')"

# --- 2/5 configuration -------------------------------------------------------
info "2/5 preparing $ENV_FILE"
FIRST_RUN=0
if [ ! -f "$ENV_FILE" ]; then
  FIRST_RUN=1
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "created from .env.example"
else
  ok "already exists — keeping it (flags you pass are still applied)"
fi

# AUTH_SECRET: generate once, never regenerate (that would log everyone out).
if [ -z "$(get_kv "$ENV_FILE" AUTH_SECRET)" ]; then
  set_kv "$ENV_FILE" AUTH_SECRET "$(rand_hex)"
  ok "generated AUTH_SECRET (32 random bytes)"
fi

DOMAIN="$DOMAIN_ARG"
if [ -z "$DOMAIN" ] && [ "$FIRST_RUN" = "1" ]; then
  DOMAIN="$(ask 'Domain (a hostname pointing at this machine, or "localhost")' localhost)"
fi
if [ -n "$DOMAIN" ]; then
  set_kv "$ENV_FILE" DOMAIN "$DOMAIN"
  if [ "$DOMAIN" = "localhost" ] || [ "${DOMAIN#localhost:}" != "$DOMAIN" ]; then
    # Local mode: plain HTTP, and DHC_DEV=1 so login codes are printed to the
    # log and the session cookie is not marked Secure.
    set_kv "$ENV_FILE" SITE_SCHEME "http"
    set_kv "$ENV_FILE" DHC_DEV "1"
    set_kv "$ENV_FILE" WORK_ENABLED "0"
    set_kv "$ENV_FILE" COMPOSE_PROFILES ""
    ENABLE_WORK=0
    ok "local mode: http://$DOMAIN, DHC_DEV=1 (login codes go to the log)"
  else
    set_kv "$ENV_FILE" SITE_SCHEME "https"
    set_kv "$ENV_FILE" DHC_DEV "0"
    ok "public mode: https://$DOMAIN (Caddy will request a certificate)"
  fi
fi

ADMIN="$ADMIN_ARG"
if [ -z "$ADMIN" ] && [ "$FIRST_RUN" = "1" ]; then
  ADMIN="$(ask 'Admin e-mail (the account you register with it becomes admin)' '')"
fi
[ -n "$ADMIN" ] && set_kv "$ENV_FILE" ADMIN_EMAILS "$ADMIN"

UPSTREAM_BASE="$UPSTREAM_BASE_ARG"
if [ -z "$UPSTREAM_BASE" ] && [ "$FIRST_RUN" = "1" ]; then
  UPSTREAM_BASE="$(ask 'Model upstream base URL (OpenAI-compatible, include /v1)' 'https://api.deepseek.com/v1')"
fi
[ -n "$UPSTREAM_BASE" ] && set_kv "$ENV_FILE" UPSTREAM_BASE_URL "$UPSTREAM_BASE"

UPSTREAM_KEY="$UPSTREAM_KEY_ARG"
if [ -z "$UPSTREAM_KEY" ] && [ "$FIRST_RUN" = "1" ]; then
  UPSTREAM_KEY="$(ask 'Upstream API key (leave empty to fill in later)' '')"
fi
[ -n "$UPSTREAM_KEY" ] && set_kv "$ENV_FILE" UPSTREAM_API_KEY "$UPSTREAM_KEY"

CUR_DOMAIN="$(get_kv "$ENV_FILE" DOMAIN)"
if [ "$ENABLE_WORK" = "1" ]; then
  if [ "$CUR_DOMAIN" = "localhost" ]; then
    warn "cloud workspaces need a real HTTPS domain — skipping --work"
  else
    set_kv "$ENV_FILE" WORK_ENABLED "1"
    set_kv "$ENV_FILE" COMPOSE_PROFILES "work"
    set_kv "$ENV_FILE" WORK_DOMAIN "work.$CUR_DOMAIN"
    set_kv "$ENV_FILE" COOKIE_DOMAIN ".$CUR_DOMAIN"
    ok "cloud workspaces on: work.$CUR_DOMAIN (needs its own DNS record)"
  fi
fi

# --- 3/5 build + start -------------------------------------------------------
info "3/5 building and starting the stack (first build takes a few minutes)"
cd "$STACK_DIR"
docker compose --env-file .env up -d --build --remove-orphans

# --- 4/5 health --------------------------------------------------------------
info "4/5 waiting for the app to answer /api/health"
HEALTHY=0
for _ in $(seq 1 45); do
  if docker compose --env-file .env exec -T dhc-server \
      python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/api/health')" >/dev/null 2>&1; then
    HEALTHY=1; break
  fi
  sleep 2
done
if [ "$HEALTHY" = "1" ]; then
  ok "dhc-server is healthy"
else
  docker compose --env-file .env logs --tail 40 dhc-server || true
  die "dhc-server did not become healthy — see the log above (most often: AUTH_SECRET empty, or port 80/443 already in use)."
fi

PUBLIC_BASE="$(get_kv "$ENV_FILE" PUBLIC_BASE)"
if [ -z "$PUBLIC_BASE" ]; then
  PUBLIC_BASE="$(get_kv "$ENV_FILE" SITE_SCHEME)://$(get_kv "$ENV_FILE" DOMAIN)"
fi
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 20 "$PUBLIC_BASE/api/health" >/dev/null 2>&1; then
    ok "$PUBLIC_BASE/api/health answers"
  else
    warn "$PUBLIC_BASE/api/health not reachable yet — normal for a fresh domain:"
    warn "  DNS must point here, and the first certificate takes ~10-30s."
    warn "  Watch it with: docker compose -f deploy/selfhost/docker-compose.yml logs -f dhc-caddy"
  fi
fi

# --- 5/5 summary -------------------------------------------------------------
info "5/5 done"
echo
echo "  Console        $PUBLIC_BASE"
echo "  Admin          $(get_kv "$ENV_FILE" ADMIN_EMAILS)  (register with this address, then open /console)"
echo "  Config         $ENV_FILE"
echo "  Logs           docker compose -f deploy/selfhost/docker-compose.yml logs -f dhc-server"
echo "  Stop / start   docker compose -f deploy/selfhost/docker-compose.yml down | up -d"
echo

CUR_ADMIN="$(get_kv "$ENV_FILE" ADMIN_EMAILS)"
if [ -z "$CUR_ADMIN" ] || [ "$CUR_ADMIN" = "you@example.com" ]; then
  warn "ADMIN_EMAILS is still the placeholder: nobody can reach /api/admin/*."
  warn "  Set it in $ENV_FILE and re-run, then register an account with that address."
fi
if [ -z "$(get_kv "$ENV_FILE" UPSTREAM_API_KEY)" ]; then
  warn "UPSTREAM_API_KEY is empty: every model request will answer 503."
  warn "  Fill it in $ENV_FILE, then: docker compose -f deploy/selfhost/docker-compose.yml up -d"
fi
if [ -z "$(get_kv "$ENV_FILE" ZHIPU_SEARCH_API_KEY)" ] && [ "$(get_kv "$ENV_FILE" SEARCH_PROVIDER)" = "zhipu" ]; then
  warn "ZHIPU_SEARCH_API_KEY is empty: the agent can chat and code, but not search the web."
fi
if [ "$(get_kv "$ENV_FILE" DHC_DEV)" = "1" ]; then
  warn "DHC_DEV=1 (local mode): login codes are printed to the log, cookies are not Secure."
  warn "  Read a code with: docker compose -f deploy/selfhost/docker-compose.yml logs dhc-server | grep dev-mail"
fi
if [ "$(get_kv "$ENV_FILE" WORK_ENABLED)" = "1" ]; then
  WORK_IMAGE="$(get_kv "$ENV_FILE" WORK_IMAGE)"
  if ! docker image inspect "$WORK_IMAGE" >/dev/null 2>&1; then
    warn "cloud workspaces are on but the image '$WORK_IMAGE' is not on this host."
    warn "  Build it — see deploy/selfhost/README.md, section 'Cloud workspaces'."
  fi
fi
