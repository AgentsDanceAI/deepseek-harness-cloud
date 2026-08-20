#!/usr/bin/env bash
# Restore a migrate-out.sh bundle onto a fresh host.
#
#   R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=... \
#   BACKUP_PASSPHRASE=... ./migrate-in.sh dhc-migrate-<stamp>.tar.gz.enc
#
# Everything keyed to the DOMAIN survives a move untouched — Waffo's webhook,
# the OAuth redirect URIs, the R2 download links, the session cookie domain.
# Only what is keyed to the MACHINE has to be redone, and this script does the
# parts it can: volumes, database, .env, networks. The rest is printed at the
# end because it lives outside this host (DNS, the shared Caddy, cron).
set -euo pipefail
cd "$(dirname "$0")"

bundle="${1:?usage: migrate-in.sh <bundle.tar.gz.enc>}"
: "${R2_ACCOUNT_ID:?}"; : "${R2_ACCESS_KEY_ID:?}"; : "${R2_SECRET_ACCESS_KEY:?}"
: "${R2_BUCKET:?}"; : "${BACKUP_PASSPHRASE:?}"

[ -f .env ] && { echo ".env already exists here — refusing to overwrite a live config" >&2; exit 1; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

echo "==> fetch and decrypt"
rclone copyto "R2:$R2_BUCKET/migrations/$bundle" "$tmp/$bundle"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in "$tmp/$bundle" -out "$tmp/bundle.tar.gz" -pass env:BACKUP_PASSPHRASE
tar -C "$tmp" -xzf "$tmp/bundle.tar.gz"
stage="$tmp/bundle"
cat "$stage/MANIFEST"

echo "==> .env"
cp "$stage/env" .env
chmod 600 .env
# The one value that is genuinely per-machine. A wrong path here does not fail
# anything — it just makes 個人成品 empty for every stopped workspace — so it is
# derived rather than carried over.
root="$(docker info --format '{{.DockerRootDir}}')/volumes"
if grep -q '^DOCKER_VOLUME_ROOT=' .env; then
  sed -i "s#^DOCKER_VOLUME_ROOT=.*#DOCKER_VOLUME_ROOT=${root}#" .env
else
  printf '\nDOCKER_VOLUME_ROOT=%s\n' "$root" >> .env
fi
echo "    DOCKER_VOLUME_ROOT=$root"

echo "==> networks"
docker network create dhc-net 2>/dev/null || true
docker network create dshwork-net 2>/dev/null || true

# compose 里这两个卷声明为 external, 和上面的网络同一个理由: 名字要与目录名、
# 项目名完全无关, 免得改个目录就让 compose 去建一个空卷、Postgres 空库启动。
echo "==> data volumes"
docker volume create dhc-data >/dev/null 2>&1 || true
docker volume create dhc-pgdata >/dev/null 2>&1 || true

echo "==> workspace volumes"
for tarball in "$stage"/volumes/*.tar.gz; do
  [ -e "$tarball" ] || break
  vol="$(basename "$tarball" .tar.gz)"
  docker volume create "$vol" >/dev/null
  # Unpacked by a throwaway container so the restore does not depend on
  # knowing this host's volume path layout.
  docker run --rm -v "$vol:/dst" -v "$tarball:/src.tar.gz:ro" alpine \
    sh -c 'tar -C /dst -xzf /src.tar.gz' >/dev/null
  echo "    $vol"
done

echo "==> postgres"
docker compose -f compose.yml --env-file .env up -d dhc-postgres
for i in $(seq 30); do
  docker exec dhc-postgres pg_isready -q && break
  sleep 2
done
POSTGRES_USER="$(grep -m1 '^POSTGRES_USER=' .env | cut -d= -f2-)"
POSTGRES_DB="$(grep -m1 '^POSTGRES_DB=' .env | cut -d= -f2-)"
gunzip -c "$stage/db.sql.gz" | docker exec -i dhc-postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q >/dev/null
n=$(docker exec dhc-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc 'SELECT COUNT(*) FROM users')
echo "    restored, $n users"

echo "==> build and start"
docker compose -f compose.yml --env-file .env up -d --build

echo "==> health"
for i in $(seq 30); do
  docker exec dhc-server python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/api/health')" 2>/dev/null && break
  sleep 2
done
docker logs dhc-server --tail 20 2>&1 | grep -i "work_volume_root" && echo "    ^ fix DOCKER_VOLUME_ROOT before you cut over" || echo "    volume root OK"

cat <<'MSG'

Restored. What is left is off this machine:

  1. DNS — point dshcloud.online, work.dshcloud.online and (if PREVIEW_DOMAIN
     is set) preview.dshcloud.online at this host's IP (Cloudflare,
     proxied/orange). Everything else keyed to the domain — the Waffo webhook,
     the Google and GitHub redirect URIs, dl.dshcloud.online — needs no change
     precisely because the domain did not.
  2. Caddy — 80/443 here must terminate TLS and route those hostnames to
     dhc-server:8100. On the old host that was a shared caddy container; see
     cutover.sh, which injects the site blocks idempotently (pass
     PREVIEW_DOMAIN= to get the preview block too).
  3. Backup cron — BOTH, and both need the `cd`: a relative path runs from
     $HOME under cron and silently never fires.
       15 4 * * *  cd <repo>/deploy/prod && ./backup-db.sh
       45 4 * * *  cd <repo>/deploy/prod && ./backup-workspaces.sh
     The second one is the only offsite copy of what users actually made; the
     database backup does not cover it.
  4. Workspace runtime — depends on WORK_BACKEND in .env:
       docker — docker build the dsh image tagged as WORK_IMAGE
                (deploy/prod/Dockerfile.dsh), or the workspace cannot start.
       eci    — nothing to build here (the image is pulled from WORK_IMAGE_REF),
                but two things ARE machine-local:
                  a) NAS mount. Add to /etc/fstab with nofail,_netdev — this
                     host runs other production services and must not hang at
                     boot when the NAS is unreachable:
                       <nas>:/ /mnt/dshwork-nas nfs vers=3,nolock,proto=tcp,\
                         hard,timeo=600,retrans=2,noresvport,nofail,_netdev 0 0
                     Needs nfs-common. Without it 個人成品 is empty for every
                     user whose workspace is asleep — and nothing errors.
                  b) Check the region's image cache still matches WORK_IMAGE_REF:
                       docker exec -w /srv/dhc dhc-server \
                         python3 -m scripts.eci_image_cache check
                     A miss is not an error, just ~25s -> ~50s on every cold
                     start, for everyone, silently.
  5. Old host — leave it stopped, not deleted, until a real user has signed in
     and paid something here.

Verify before DNS: curl -H 'Host: dshcloud.online' http://127.0.0.1:8100/api/health
MSG
