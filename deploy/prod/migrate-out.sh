#!/usr/bin/env bash
# Package everything this deployment cannot rebuild, for a move to another host.
#
#   ./migrate-out.sh            # rehearsal: bundle while the site keeps serving
#   ./migrate-out.sh --freeze   # the real one: stop writes first, then bundle
#
# What is in the bundle, and why only this:
#
#   .env          secrets, and the only copy — it is gitignored on purpose
#   db.sql.gz     accounts, orders, credits. The money. 18KB compressed.
#   volumes/      per-user workspaces: /workspace files and ~/.dsh history
#                 (仅 WORK_BACKEND=docker; ECI 后端下这些在 NAS 上, 不随包走)
#
# What is deliberately NOT in it:
#
#   releases/     installers are served from R2 (/dl/<key> counts, then 302s
#                 there); the local copy has been a leftover since that change
#   ~/.npm, ~/.cache   261MB of rebuildable package cache against 3.6MB of
#                 actual session history — carrying it would make the bundle
#                 35x bigger for nothing
#   images        dsh-local:rc8 rebuilds from the dsh repo; dhc-server builds
#                 from this one
#
# Transport is R2 because the two hosts have no direct link. That bucket is
# PUBLIC (it fronts dl.dshcloud.online), so the bundle — which contains every
# secret this service has — is encrypted before it leaves the host and marked
# uncacheable so deleting it takes effect immediately.
set -euo pipefail
cd "$(dirname "$0")"

ENVFILE=".env"
PREFIX="migrations"
[ -f "$ENVFILE" ] || { echo "no .env beside this script" >&2; exit 1; }

while IFS= read -r line; do
  case "$line" in
    R2_*=*|POSTGRES_*=*|BACKUP_PASSPHRASE=*)
      name="${line%%=*}"
      eval "current=\${$name:-}"
      [ -n "$current" ] || export "${name}=${line#*=}"
      ;;
  esac
done < "$ENVFILE"

: "${R2_ACCOUNT_ID:?}"; : "${R2_ACCESS_KEY_ID:?}"; : "${R2_SECRET_ACCESS_KEY:?}"
: "${R2_BUCKET:?}"; : "${POSTGRES_USER:?}"; : "${POSTGRES_DB:?}"
: "${BACKUP_PASSPHRASE:?set BACKUP_PASSPHRASE in .env — this bundle holds every secret and the destination bucket is public}"

FREEZE=0
[ "${1:-}" = "--freeze" ] && FREEZE=1

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
bundle="dhc-migrate-${stamp}.tar.gz.enc"
tmp="$(mktemp -d)"
stage="$tmp/bundle"
mkdir -p "$stage/volumes"
trap 'rm -rf "$tmp"' EXIT

if [ "$FREEZE" -eq 1 ]; then
  # Order matters: the workspaces write to the volumes and dhc-server writes to
  # the database, so both stop BEFORE anything is read. A dump taken while
  # orders are still landing is a dump that loses them.
  echo "==> freeze: stopping dhc-server and every workspace"
  docker stop dhc-server >/dev/null 2>&1 || true
  for c in $(docker ps -q --filter "name=dshwork-"); do docker stop "$c" >/dev/null; done
  echo "    stopped (postgres stays up; it is the thing being read)"
else
  echo "==> rehearsal: the site keeps serving, so this bundle is a point-in-time"
  echo "    approximation. Re-run with --freeze for the cutover."
fi

echo "==> database"
docker exec dhc-postgres pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --clean --if-exists --no-owner --no-privileges | gzip -9 > "$stage/db.sql.gz"
gzip -t "$stage/db.sql.gz"
# Decompress once and grep the FILE. `gunzip -c | grep -q` closes the pipe on
# the first match, gunzip takes SIGPIPE, and pipefail reports that as a failed
# check — so the tables that appear earliest are the ones that "fail". This is
# written up in backup-db.sh and reintroduced here anyway; hence the comment.
gunzip -c "$stage/db.sql.gz" > "$tmp/plain.sql"
for table in users orders credit_grants subscriptions; do
  grep -q "COPY public.${table} " "$tmp/plain.sql" \
    || { echo "dump has no data section for '$table'" >&2; exit 1; }
done
rm -f "$tmp/plain.sql"
echo "    $(stat -c%s "$stage/db.sql.gz") bytes, tables present"

# 工作台文件在哪, 取决于后端 —— 而两种情况下这个循环都"成功"地跑完:
# ECI 后端下本机根本没有 dshwork-* 卷, 循环转一圈什么都不打包, 包里安安静静地
# 少了用户的全部产出, 而头注释还写着 volumes/ 里有它们。所以这里必须出声。
work_backend="$(grep -E '^WORK_BACKEND=' "$ENVFILE" 2>/dev/null | cut -d= -f2- | tr -d ' \r')"
if [ "${work_backend:-docker}" = "eci" ]; then
  cat <<'EOWS'
==> workspace volumes: 跳过 (WORK_BACKEND=eci)
    用户的 /workspace 与 ~/.dsh 在 **NAS** 上, 不在本机, 因此不随这个包走。
    换机只要新机能挂上同一个 NAS 就什么都不用做; 但**换地域**要先把 NAS 数据
    复制过去 (NAS 是地域内资源), 否则新机起来是空的。
    另有一份加密副本在 R2: backups/workspaces (见 backup-workspaces.sh)。
EOWS
else
echo "==> workspace volumes"
found=0
for vol in $(docker volume ls --format '{{.Name}}' | grep -E '^dshwork-(ws|home)-'); do
  found=1
  src="$(docker volume inspect "$vol" --format '{{.Mountpoint}}')"
  [ -d "$src" ] || { echo "    skip $vol (no mountpoint)"; continue; }
  # ~/.npm and ~/.cache are package caches that rebuild on first use; ~/.dsh is
  # the session history, which does not.
  tar -C "$src" -czf "$stage/volumes/${vol}.tar.gz" \
      --exclude='./.npm' --exclude='./.cache' --exclude='./node_modules' . 2>/dev/null
  echo "    $vol  $(du -h "$stage/volumes/${vol}.tar.gz" | cut -f1)"
done
# docker 后端却一个卷都没有, 多半是名字变了或引擎不对 —— 安静地打个空包比报错糟。
[ "$found" = 1 ] || echo "    !! 一个 dshwork-* 卷都没找到 —— 包里不会有任何用户文件, 先查清楚再迁"
fi

cp "$ENVFILE" "$stage/env"
{
  echo "source_host=$(hostname)"
  echo "created=$stamp"
  echo "git_commit=$(git -C ../.. rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "frozen=$FREEZE"
  echo "docker_root=$(docker info --format '{{.DockerRootDir}}')"
  echo "volumes=$(ls "$stage/volumes" | tr '\n' ' ')"
} > "$stage/MANIFEST"

echo "==> pack and encrypt"
tar -C "$tmp" -czf "$tmp/bundle.tar.gz" bundle
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -in "$tmp/bundle.tar.gz" -out "$tmp/$bundle" -pass env:BACKUP_PASSPHRASE
head -c 8 "$tmp/$bundle" | grep -q "Salted__" || { echo "encryption failed" >&2; exit 1; }
echo "    $(du -h "$tmp/$bundle" | cut -f1) encrypted"

export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

echo "==> upload"
rclone copyto "$tmp/$bundle" "R2:$R2_BUCKET/$PREFIX/$bundle" --s3-chunk-size 16M \
  --header-upload "Cache-Control: no-store, private"
rclone lsl "R2:$R2_BUCKET/$PREFIX/$bundle" >/dev/null

if [ "$FREEZE" -eq 1 ]; then
  echo
  echo "The old host is now STOPPED. It stays that way until you either restore"
  echo "on the new host or run:  docker start dhc-server"
fi

cat <<MSG

Done. $PREFIX/$bundle

On the new host:
  git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud
  cd deepseek-harness-cloud/deploy/prod
  R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=$R2_BUCKET \\
  BACKUP_PASSPHRASE=... ./migrate-in.sh $bundle

The passphrase is not in the bundle and not in git. Carry it yourself.
MSG
