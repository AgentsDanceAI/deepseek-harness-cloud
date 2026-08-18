#!/usr/bin/env bash
# Daily PostgreSQL backup to Cloudflare R2.
#
# The database holds accounts, orders, payment records and credit balances —
# money, not cache. Until this script existed there was no backup of any kind:
# a lost volume meant every paid customer's balance was gone with no way to
# reconstruct it from anything but Waffo's side of the ledger.
#
# Offsite by design: a copy on the same host dies with the host. R2 egress is
# free and the dumps are small, so keeping 30 days costs cents.
#
#   ./backup-db.sh            # run one backup
#   ./backup-db.sh --verify   # also download it back and check it restores
#
# Credentials come from .env (same file the app and publish-r2.sh read).
set -euo pipefail
cd "$(dirname "$0")"

ENVFILE=".env"
RETAIN_DAYS=30
PREFIX="backups/postgres"

[ -f "$ENVFILE" ] || { echo "no .env beside this script" >&2; exit 1; }
while IFS= read -r line; do
  case "$line" in
    R2_*=*|POSTGRES_*=*)
      name="${line%%=*}"
      eval "current=\${$name:-}"
      [ -n "$current" ] || export "${name}=${line#*=}"
      ;;
  esac
done < "$ENVFILE"

: "${R2_ACCOUNT_ID:?}"; : "${R2_ACCESS_KEY_ID:?}"; : "${R2_SECRET_ACCESS_KEY:?}"
: "${R2_BUCKET:?}"; : "${POSTGRES_USER:?}"; : "${POSTGRES_DB:?}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="dhc-${stamp}.sql.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> dump $POSTGRES_DB"
# --clean --if-exists so the dump can be replayed onto a live database without
# hand-dropping objects first; that is the difference between a backup and a
# file you cannot actually restore under pressure.
docker exec dhc-postgres pg_dump \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --clean --if-exists --no-owner --no-privileges \
  | gzip -9 > "$tmp/$name"

size=$(stat -c%s "$tmp/$name")
[ "$size" -gt 1024 ] || { echo "dump is only ${size}B — refusing to upload a truncated backup" >&2; exit 1; }
echo "    $name  $(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B")"

# A dump that gunzips cleanly and contains the tables that carry money. Catching
# a corrupt dump at 3am beats discovering it during a restore.
echo "==> sanity-check the dump"
gzip -t "$tmp/$name"
# Decompress once and grep the file. Piping into `grep -q` closes the pipe on
# the first match, gunzip takes SIGPIPE, and `set -o pipefail` reports that as
# a failed check — so the tables appearing EARLIEST in the dump were the ones
# that "failed". A backup checker that fails on healthy backups is worse than
# no checker, because you learn to ignore it.
gunzip -c "$tmp/$name" > "$tmp/plain.sql"
for table in users orders credit_grants subscriptions; do
  grep -q "COPY public.${table} " "$tmp/plain.sql" \
    || { echo "dump has no data section for '$table' — not a usable backup" >&2; exit 1; }
done
rm -f "$tmp/plain.sql"
echo "    tables present"

export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

echo "==> upload to r2://$R2_BUCKET/$PREFIX/"
rclone copyto "$tmp/$name" "R2:$R2_BUCKET/$PREFIX/$name" --s3-chunk-size 16M

echo "==> prune backups older than ${RETAIN_DAYS}d"
rclone delete "R2:$R2_BUCKET/$PREFIX" --min-age "${RETAIN_DAYS}d" 2>/dev/null || true
kept=$(rclone lsf "R2:$R2_BUCKET/$PREFIX" 2>/dev/null | grep -c . || echo 0)
echo "    $kept backup(s) retained"

if [ "${1:-}" = "--verify" ]; then
  echo "==> verify: pull it back and restore into a scratch database"
  rclone copyto "R2:$R2_BUCKET/$PREFIX/$name" "$tmp/verify.sql.gz"
  docker exec dhc-postgres psql -U "$POSTGRES_USER" -d postgres \
    -c 'DROP DATABASE IF EXISTS dhc_restore_check' -c 'CREATE DATABASE dhc_restore_check' >/dev/null
  gunzip -c "$tmp/verify.sql.gz" | docker exec -i dhc-postgres psql -U "$POSTGRES_USER" -d dhc_restore_check -q >/dev/null 2>&1
  n=$(docker exec dhc-postgres psql -U "$POSTGRES_USER" -d dhc_restore_check -tAc 'SELECT COUNT(*) FROM users')
  docker exec dhc-postgres psql -U "$POSTGRES_USER" -d postgres -c 'DROP DATABASE dhc_restore_check' >/dev/null
  echo "    restored and read back $n users — the backup is usable"
fi

echo
echo "Done. $PREFIX/$name"
