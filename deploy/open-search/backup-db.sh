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
# ENCRYPTED, always. The only R2 bucket this deployment has a token for is
# dsh-releases, which is PUBLISHED at https://dl.dshcloud.online — so an object
# written here is served to anyone who asks for its path. Plaintext dumps went
# there for two days: accounts, emails, password hashes, orders and balances,
# under a name derived from a fixed cron time. The dump is now encrypted before
# it leaves the host, and this script refuses to run without a passphrase
# rather than quietly fall back to plaintext.
#
#   BACKUP_PASSPHRASE — set it in .env AND keep a copy somewhere that survives
#   this machine. Lose it and every offsite backup is unreadable.
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
    R2_*=*|POSTGRES_*=*|BACKUP_PASSPHRASE=*)
      name="${line%%=*}"
      eval "current=\${$name:-}"
      [ -n "$current" ] || export "${name}=${line#*=}"
      ;;
  esac
done < "$ENVFILE"

: "${R2_ACCOUNT_ID:?}"; : "${R2_ACCESS_KEY_ID:?}"; : "${R2_SECRET_ACCESS_KEY:?}"
: "${R2_BUCKET:?}"; : "${POSTGRES_USER:?}"; : "${POSTGRES_DB:?}"
: "${BACKUP_PASSPHRASE:?set BACKUP_PASSPHRASE in .env — the destination bucket is public, so an unencrypted dump there is a public dump}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="dhc-${stamp}.sql.gz.enc"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> dump $POSTGRES_DB"
# --clean --if-exists so the dump can be replayed onto a live database without
# hand-dropping objects first; that is the difference between a backup and a
# file you cannot actually restore under pressure.
docker exec dhc-postgres pg_dump \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --clean --if-exists --no-owner --no-privileges \
  | gzip -9 > "$tmp/plain.sql.gz"

size=$(stat -c%s "$tmp/plain.sql.gz")
[ "$size" -gt 1024 ] || { echo "dump is only ${size}B — refusing to upload a truncated backup" >&2; exit 1; }
echo "    $name  $(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B")"

# A dump that gunzips cleanly and contains the tables that carry money. Catching
# a corrupt dump at 3am beats discovering it during a restore.
echo "==> sanity-check the dump"
gzip -t "$tmp/plain.sql.gz"
# Decompress once and grep the file. Piping into `grep -q` closes the pipe on
# the first match, gunzip takes SIGPIPE, and `set -o pipefail` reports that as
# a failed check — so the tables appearing EARLIEST in the dump were the ones
# that "failed". A backup checker that fails on healthy backups is worse than
# no checker, because you learn to ignore it.
gunzip -c "$tmp/plain.sql.gz" > "$tmp/plain.sql"
for table in users orders credit_grants subscriptions; do
  grep -q "COPY public.${table} " "$tmp/plain.sql" \
    || { echo "dump has no data section for '$table' — not a usable backup" >&2; exit 1; }
done
rm -f "$tmp/plain.sql"
echo "    tables present"

# AES-256 with a KDF, so the object is inert wherever it lands. Done BEFORE the
# upload, never after: the window between the two is exactly the exposure.
echo "==> encrypt"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -in "$tmp/plain.sql.gz" -out "$tmp/$name" -pass env:BACKUP_PASSPHRASE
shred -u "$tmp/plain.sql.gz" 2>/dev/null || rm -f "$tmp/plain.sql.gz"
head -c 8 "$tmp/$name" | grep -q "Salted__" || { echo "encryption produced something unexpected" >&2; exit 1; }
echo "    $(stat -c%s "$tmp/$name") bytes, AES-256"

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
  rclone copyto "R2:$R2_BUCKET/$PREFIX/$name" "$tmp/verify.enc"
  # Decrypting here is the point: it proves the passphrase in .env is the one
  # the object was written with. A backup you cannot open is not a backup.
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in "$tmp/verify.enc" -out "$tmp/verify.sql.gz" -pass env:BACKUP_PASSPHRASE
  docker exec dhc-postgres psql -U "$POSTGRES_USER" -d postgres \
    -c 'DROP DATABASE IF EXISTS dhc_restore_check' -c 'CREATE DATABASE dhc_restore_check' >/dev/null
  gunzip -c "$tmp/verify.sql.gz" | docker exec -i dhc-postgres psql -U "$POSTGRES_USER" -d dhc_restore_check -q >/dev/null 2>&1
  n=$(docker exec dhc-postgres psql -U "$POSTGRES_USER" -d dhc_restore_check -tAc 'SELECT COUNT(*) FROM users')
  docker exec dhc-postgres psql -U "$POSTGRES_USER" -d postgres -c 'DROP DATABASE dhc_restore_check' >/dev/null
  echo "    restored and read back $n users — the backup is usable"
fi

echo
echo "Done. $PREFIX/$name"
