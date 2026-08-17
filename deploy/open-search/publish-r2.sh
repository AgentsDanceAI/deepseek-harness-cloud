#!/usr/bin/env bash
# Publish the desktop installers to Cloudflare R2 and point the site at them.
#
# Why R2: the installers are 604MB in total and the mac build alone is 282MB.
# Served from this machine they compete for bandwidth with the model gateway and
# the cloud workspaces — the paying surface of the product. R2 charges nothing
# for egress, so moving the bytes there costs ~$0.01/month in storage and takes
# the download traffic off this box entirely.
#
# The site keeps linking to /dl/<key>, which counts the download and then
# redirects here, so the counter survives the move and nothing in the frontend
# changes.
#
# Credentials come from the environment and are never written to disk by this
# script. Create them at:
#   Cloudflare dashboard -> R2 -> Manage R2 API Tokens -> Create API token
#   (Object Read & Write, scoped to the one bucket)
#
#   export R2_ACCOUNT_ID=...        # R2 overview page, right-hand side
#   export R2_ACCESS_KEY_ID=...
#   export R2_SECRET_ACCESS_KEY=...
#   export R2_BUCKET=dsh-releases
#   export R2_PUBLIC_BASE=https://dl.dshcloud.online   # the bucket's custom domain
#   ./publish-r2.sh
#
set -euo pipefail
cd "$(dirname "$0")"

ENVFILE=".env"

# Credentials live in .env alongside the other deployment secrets (it is
# gitignored and already holds the gateway keys). An explicit export still wins,
# so a one-off run with a rotated token needs no file edit.
if [ -f "$ENVFILE" ]; then
  while IFS= read -r line; do
    case "$line" in
      R2_*=*)
        name="${line%%=*}"
        eval "current=\${$name:-}"
        [ -n "$current" ] || export "${name}=${line#*=}"
        ;;
    esac
  done < "$ENVFILE"
fi

: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_BUCKET:?set R2_BUCKET}"
: "${R2_PUBLIC_BASE:?set R2_PUBLIC_BASE to the bucket public custom domain}"

ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
SRC_CONTAINER="dhc-server"
SRC_DIR="/app/data/releases"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

command -v rclone >/dev/null || {
  echo "rclone not found. Install it with:" >&2
  echo "  curl -fsSL https://rclone.org/install.sh | sudo bash" >&2
  exit 1
}

echo "==> pull current installers out of the data volume"
docker exec "$SRC_CONTAINER" sh -c "ls -1 $SRC_DIR" | while read -r f; do
  [ -n "$f" ] || continue
  docker cp "$SRC_CONTAINER:$SRC_DIR/$f" "$STAGE/$f"
done
ls -lh "$STAGE"

# rclone reads the remote from env vars, so no config file is created and no
# secret is left behind on this machine.
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="$ENDPOINT"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

echo "==> upload to r2://$R2_BUCKET"
rclone copy "$STAGE" "R2:$R2_BUCKET" --progress --s3-chunk-size 32M --transfers 2

echo "==> verify each object is publicly readable over $R2_PUBLIC_BASE"
fail=0
for f in "$STAGE"/*; do
  name="$(basename "$f")"
  url="$R2_PUBLIC_BASE/$name"
  # Range-request the first KB: proves public reachability without pulling 282MB.
  #
  # The cache buster is load-bearing. Probing the bare URL right after upload can
  # reach an edge that has not seen the object yet; Cloudflare then caches that
  # 404 for four hours (max-age=14400) and every later check — and every real
  # user on that edge — gets the cached miss. The verification step was creating
  # the very failure it reported. A unique query bypasses the cache without
  # poisoning it, and R2 ignores unknown query parameters when resolving keys.
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -r 0-1023 "$url?verify=$$-$RANDOM" || true)
  if [ "$code" = "206" ] || [ "$code" = "200" ]; then
    echo "    OK  $name"
  else
    echo "    BAD $name -> HTTP $code"
    fail=1
  fi
done
[ "$fail" -eq 0 ] || {
  echo "Some objects are not public. Check the bucket's custom domain is connected" >&2
  echo "and that public access is enabled, then re-run. NOT switching the site." >&2
  exit 1
}

echo "==> repoint DOWNLOAD_URL_* at R2"
declare -A MAP=(
  [DOWNLOAD_URL_MAC]="mac-arm64.dmg"
  [DOWNLOAD_URL_MAC_X64]="mac-x64.zip"
  [DOWNLOAD_URL_WIN]="win-x64.exe"
  [DOWNLOAD_URL_WIN_ARM]="win-arm64.exe"
  [DOWNLOAD_URL_ANDROID]="android.apk"
)
for var in "${!MAP[@]}"; do
  # Match the real filename by suffix rather than assuming a naming scheme.
  case "${MAP[$var]}" in
    mac-arm64.dmg) pat="*mac-arm64.dmg" ;;
    mac-x64.zip)   pat="*mac-x64.zip" ;;
    win-x64.exe)   pat="*-x64-Setup.exe" ;;
    win-arm64.exe) pat="*-arm64-Setup.exe" ;;
    android.apk)   pat="*.apk" ;;
  esac
  # shellcheck disable=SC2086
  found="$(cd "$STAGE" && ls -1 $pat 2>/dev/null | head -1 || true)"
  [ -n "$found" ] || { echo "    skip $var (no artifact)"; continue; }
  sed -i "/^${var}=/d" "$ENVFILE"
  echo "${var}=${R2_PUBLIC_BASE}/${found}" >> "$ENVFILE"
  echo "    $var -> $R2_PUBLIC_BASE/$found"
done

echo "==> restart the app so it picks up the new targets"
docker compose up -d dhc-server

echo
echo "Done. The site now redirects downloads to R2; /releases stays as a fallback"
echo "(and remains the only path for self-hosters). Check the counter still moves:"
echo "  curl -sI https://dshcloud.online/dl/mac-arm64 | grep -i location"
