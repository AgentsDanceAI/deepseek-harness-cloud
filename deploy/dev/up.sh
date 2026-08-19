#!/usr/bin/env bash
# Bring up the development stack on this machine.
#
#   ./up.sh          # start (creates .env with fresh secrets on first run)
#   ./up.sh --fresh  # throw away the dev database and start clean
#
# Never touches a production deployment: different project name, different
# container names, different volumes, and it binds to loopback only.
set -euo pipefail
cd "$(dirname "$0")"

# This host kept the retired production volumes as a fallback. A dev stack is
# fine beside them; a container under the PRODUCTION name is not, because the
# next person to type `docker compose up` in the wrong directory gets a second
# live service on real balances. Refuse rather than race it.
if docker ps -a --format '{{.Names}}' | grep -qx "dhc-server"; then
  echo "a container named dhc-server exists on this host — that is the PRODUCTION" >&2
  echo "name. Refusing to start the dev stack beside it; sort that out first." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "==> first run: creating .env"
  cp .env.example .env
  pw="$(openssl rand -hex 16)"
  secret="$(openssl rand -base64 33 | tr -d '\n')"
  # BSD sed needs an argument to -i; GNU sed must not have one.
  if [ "$(uname -s)" = "Darwin" ]; then inplace=(-i ''); else inplace=(-i); fi
  edit() { sed "${inplace[@]}" "$1" .env; }

  if [ "$(uname -s)" = "Darwin" ]; then
    # Docker Desktop runs the engine in a VM, so DockerRootDir is a path INSIDE
    # that VM — bind-mounting it from macOS yields an empty directory. The
    # workspace features also need a locally built dsh image. Both go off here
    # rather than half-work in a way that reads like a bug in the code.
    mkdir -p .novolumes
    root="$(cd .novolumes && pwd)"
    edit "s#^WORK_ENABLED=.*#WORK_ENABLED=0#"
    echo "    macOS: cloud workspaces off (the engine runs in a VM, so its"
    echo "    volume root is not a path this host can mount)."
  else
    root="$(docker info --format '{{.DockerRootDir}}')/volumes"
  fi
  edit "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=${pw}#"
  edit "s#__PW__#${pw}#"
  edit "s#^AUTH_SECRET=.*#AUTH_SECRET=${secret}#"
  edit "s#^DOCKER_VOLUME_ROOT=.*#DOCKER_VOLUME_ROOT=${root}#"
  chmod 600 .env
  echo "    generated a password, a session secret, and DOCKER_VOLUME_ROOT=${root}"
fi

if [ "${1:-}" = "--fresh" ]; then
  echo "==> dropping the dev database"
  docker compose -f compose.yml --env-file .env down -v
fi

echo "==> build and start"
docker compose -f compose.yml --env-file .env up -d --build

echo "==> health"
for _ in $(seq 40); do
  if curl -fsS http://127.0.0.1:18100/api/health >/dev/null 2>&1; then
    echo "    $(curl -s http://127.0.0.1:18100/api/health)"
    break
  fi
  sleep 1
done

# A wrong volume root is silent — the products page just goes empty — so surface
# it here rather than let it be found by a confused test.
docker logs dhc-dev-server --tail 40 2>&1 | grep -i "WORK_VOLUME_ROOT" && \
  echo "    ^ fix DOCKER_VOLUME_ROOT in deploy/dev/.env" || true

cat <<'MSG'

Dev stack is on http://127.0.0.1:18100 (loopback only — from your Mac:
  ssh -L 18100:127.0.0.1:18100 <this-host>
then open http://127.0.0.1:18100).

  docker compose -f compose.yml --env-file .env logs -f dhc-dev-server
  docker compose -f compose.yml --env-file .env down

Payments are deliberately unconfigured here; checkout records an intent and
stops. Model calls need UPSTREAM_API_KEY in .env — dev traffic bills to the
same upstream account production does.
MSG
