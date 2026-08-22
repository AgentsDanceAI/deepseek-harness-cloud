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
STACK_FILES=(-f "$STACK_DIR/docker-compose.yml" -f "$STACK_DIR/compose.build.yml")

stack_compose() {
  docker compose --env-file "$ENV_FILE" "${STACK_FILES[@]}" "$@"
}

compose_hint() {
  local hint="docker compose --env-file deploy/selfhost/.env" index
  for ((index = 1; index < ${#STACK_FILES[@]}; index += 2)); do
    hint+=" -f deploy/selfhost/$(basename "${STACK_FILES[$index]}")"
  done
  printf '%s' "$hint"
}

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
  if [ "$DOMAIN" = "localhost" ] || [ "${DOMAIN#localhost:}" != "$DOMAIN" ]; then
    LOCAL_HTTP_PORT="8787"
    if [ "${DOMAIN#localhost:}" != "$DOMAIN" ]; then
      LOCAL_HTTP_PORT="${DOMAIN#localhost:}"
      [[ "$LOCAL_HTTP_PORT" =~ ^[0-9]+$ ]] || die "localhost port must be numeric"
      if ((10#$LOCAL_HTTP_PORT < 1 || 10#$LOCAL_HTTP_PORT > 65535)); then
        die "localhost port must be between 1 and 65535"
      fi
    fi
    # Local mode: plain HTTP, and DHC_DEV=1 so login codes are printed to the
    # log and the session cookie is not marked Secure.
    set_kv "$ENV_FILE" DOMAIN "localhost"
    set_kv "$ENV_FILE" SITE_SCHEME "http"
    set_kv "$ENV_FILE" PUBLIC_BASE "http://localhost:$LOCAL_HTTP_PORT"
    set_kv "$ENV_FILE" BIND_ADDRESS "127.0.0.1"
    set_kv "$ENV_FILE" HTTP_PORT "$LOCAL_HTTP_PORT"
    set_kv "$ENV_FILE" HTTPS_PORT "8443"
    set_kv "$ENV_FILE" DHC_DEV "1"
    set_kv "$ENV_FILE" WORK_ENABLED "0"
    set_kv "$ENV_FILE" COMPOSE_PROFILES ""
    ENABLE_WORK=0
    ok "local mode: http://localhost:$LOCAL_HTTP_PORT, loopback only, DHC_DEV=1"
  else
    set_kv "$ENV_FILE" DOMAIN "$DOMAIN"
    set_kv "$ENV_FILE" SITE_SCHEME "https"
    set_kv "$ENV_FILE" PUBLIC_BASE ""
    set_kv "$ENV_FILE" BIND_ADDRESS "0.0.0.0"
    set_kv "$ENV_FILE" HTTP_PORT "80"
    set_kv "$ENV_FILE" HTTPS_PORT "443"
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

if [ "$CUR_DOMAIN" != "localhost" ]; then
  SMTP_HOST="$(get_kv "$ENV_FILE" MAIL_SMTP_HOST)"
  SMTP_USER="$(get_kv "$ENV_FILE" MAIL_SMTP_USER)"
  SMTP_FROM="$(get_kv "$ENV_FILE" MAIL_FROM)"
  GOOGLE_ID="$(get_kv "$ENV_FILE" GOOGLE_LOGIN_CLIENT_ID)"
  GOOGLE_SECRET="$(get_kv "$ENV_FILE" GOOGLE_LOGIN_CLIENT_SECRET)"
  GITHUB_ID="$(get_kv "$ENV_FILE" GITHUB_LOGIN_CLIENT_ID)"
  GITHUB_SECRET="$(get_kv "$ENV_FILE" GITHUB_LOGIN_CLIENT_SECRET)"
  if { [ -z "$SMTP_HOST" ] || { [ -z "$SMTP_FROM" ] && [ -z "$SMTP_USER" ]; }; } \
      && { [ -z "$GOOGLE_ID" ] || [ -z "$GOOGLE_SECRET" ]; } \
      && { [ -z "$GITHUB_ID" ] || [ -z "$GITHUB_SECRET" ]; }; then
    die "public mode requires SMTP or Google/GitHub OAuth for the first verified account. Configure it in $ENV_FILE, then re-run this command."
  fi
fi

# --- 3/5 build + start -------------------------------------------------------
info "3/5 building and starting the stack (first build takes a few minutes)"

# Preserve the supported PostgreSQL overlay once an operator has enabled it.
# Omitting the overlay on a later run would recreate dhc-server with SQLite and
# make the existing data appear to have vanished. Check both the service label
# and the named volume so this remains safe after `docker compose down`.
PROJECT_NAME="$(get_kv "$ENV_FILE" COMPOSE_PROJECT_NAME)"
PROJECT_NAME="${PROJECT_NAME:-dsh-selfhost}"
POSTGRES_CONTAINER="$(docker ps -aq \
  --filter "label=com.docker.compose.project=$PROJECT_NAME" \
  --filter "label=com.docker.compose.service=postgres" | sed -n '1p')"
if [ -n "$POSTGRES_CONTAINER" ] || docker volume inspect "${PROJECT_NAME}_dhc-pgdata" >/dev/null 2>&1; then
  STACK_FILES+=("-f" "$STACK_DIR/compose.postgres.yml")
  ok "preserving the existing PostgreSQL overlay"
fi
COMPOSE_HINT="$(compose_hint)"

cd "$STACK_DIR"
stack_compose up -d --build

# --- 4/5 health --------------------------------------------------------------
info "4/5 waiting for the app to become ready"
HEALTHY=0
for _ in $(seq 1 45); do
  if stack_compose exec -T dhc-server \
      python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/readyz')" >/dev/null 2>&1; then
    HEALTHY=1; break
  fi
  sleep 2
done
if [ "$HEALTHY" = "1" ]; then
  ok "dhc-server is healthy"
else
  stack_compose logs --tail 40 dhc-server || true
  die "dhc-server did not become healthy — see the log above (most often: AUTH_SECRET empty, or port 80/443 already in use)."
fi

PUBLIC_BASE="$(get_kv "$ENV_FILE" PUBLIC_BASE)"
if [ -z "$PUBLIC_BASE" ]; then
  PUBLIC_BASE="$(get_kv "$ENV_FILE" SITE_SCHEME)://$(get_kv "$ENV_FILE" DOMAIN)"
fi
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 20 "$PUBLIC_BASE/readyz" >/dev/null 2>&1; then
    ok "$PUBLIC_BASE/readyz answers"
  else
    warn "$PUBLIC_BASE/api/health not reachable yet — normal for a fresh domain:"
    warn "  DNS must point here, and the first certificate takes ~10-30s."
    warn "  Watch it with: $COMPOSE_HINT logs -f dhc-caddy"
  fi
fi

# --- 5/5 summary -------------------------------------------------------------
info "5/5 done"
echo
echo "  Console        $PUBLIC_BASE"
echo "  Admin          $(get_kv "$ENV_FILE" ADMIN_EMAILS)  (verify this address, then open /console)"
echo "  Config         $ENV_FILE"
echo "  Logs           $COMPOSE_HINT logs -f dhc-server"
echo "  Stop / start   $COMPOSE_HINT down | up -d --build"
echo

CUR_ADMIN="$(get_kv "$ENV_FILE" ADMIN_EMAILS)"
if [ -z "$CUR_ADMIN" ] || [ "$CUR_ADMIN" = "you@example.com" ]; then
  warn "ADMIN_EMAILS is still the placeholder: nobody can reach /api/admin/*."
  warn "  Set it in $ENV_FILE and re-run, then verify an account with that address."
fi
if [ -z "$(get_kv "$ENV_FILE" UPSTREAM_API_KEY)" ]; then
  warn "UPSTREAM_API_KEY is empty: every model request will answer 503."
  warn "  Fill it in $ENV_FILE, then re-run this quickstart command."
fi
if [ -z "$(get_kv "$ENV_FILE" ZHIPU_SEARCH_API_KEY)" ] && [ "$(get_kv "$ENV_FILE" SEARCH_PROVIDER)" = "zhipu" ]; then
  warn "ZHIPU_SEARCH_API_KEY is empty: the agent can chat and code, but not search the web."
fi
if [ "$(get_kv "$ENV_FILE" DHC_DEV)" = "1" ]; then
  warn "DHC_DEV=1 (local mode): login codes are printed to the log, cookies are not Secure."
  warn "  Read a code with the Logs command above and filter for dev-mail."
fi
if [ "$(get_kv "$ENV_FILE" WORK_ENABLED)" = "1" ]; then
  WORK_IMAGE="$(get_kv "$ENV_FILE" WORK_IMAGE)"
  if ! docker image inspect "$WORK_IMAGE" >/dev/null 2>&1; then
    warn "cloud workspaces are on but the image '$WORK_IMAGE' is not on this host."
    warn "  Build it — see deploy/selfhost/README.md, section 'Cloud workspaces'."
  fi
fi
