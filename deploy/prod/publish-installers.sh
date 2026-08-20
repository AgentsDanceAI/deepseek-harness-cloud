#!/usr/bin/env bash
# Publish installers from green workflow runs:
#   bash deploy/prod/publish-installers.sh <desktop-run-id> [android-run-id]
# Downloads the artifacts, drops them into dhc-server's data volume (served at
# /releases), points DOWNLOAD_URL_* at them, and restarts the app.
#
# Desktop run artifacts: mac arm64 DMG + mac x64 (Intel) DMG + windows Setup.exe
# (any subset is fine — only the found ones are published; missing ones keep
# their current DOWNLOAD_URL_* entries). Android run artifact: sideload APK.
set -euo pipefail

RUN_ID="${1:?usage: publish-installers.sh <desktop-run-id> [android-run-id]}"
ANDROID_RUN_ID="${2:-}"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENVFILE="$REPO_DIR/deploy/prod/.env"
BASE="https://dshcloud.online"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "==> download artifacts from desktop run $RUN_ID"
gh run download "$RUN_ID" --dir "$WORK/desktop" -R AgentsDanceAI/deepseek-harness-cloud
if [ -n "$ANDROID_RUN_ID" ]; then
  echo "==> download artifacts from android run $ANDROID_RUN_ID"
  gh run download "$ANDROID_RUN_ID" --dir "$WORK/android" -R AgentsDanceAI/deepseek-harness-cloud
fi
find "$WORK" -type f \( -name "*.dmg" -o -name "*.exe" -o -name "*.apk" \) -exec ls -lh {} \;

DMG_ARM=$(find "$WORK" -name "*.dmg" | grep -i "arm64" | head -1 || true)
DMG_X64=$(find "$WORK" -name "*.dmg" | grep -vi "arm64" | head -1 || true)
EXE=$(find "$WORK" -name "*.exe" | head -1 || true)
APK=$(find "$WORK" -name "*.apk" | head -1 || true)
[ -n "$DMG_ARM$DMG_X64$EXE$APK" ] || { echo "no installer artifacts found"; exit 1; }

echo "==> copy into dhc-server data volume (/app/data/releases)"
docker exec dhc-server mkdir -p /app/data/releases
PUBLISHED=()
publish_one() { # $1=file $2=env-var
  local f="$1" var="$2" name
  [ -n "$f" ] || { echo "    (no artifact for $var — keeping current)"; return 0; }
  name=$(basename "$f")
  docker cp "$f" dhc-server:/app/data/releases/
  sed -i "/^${var}=/d" "$ENVFILE"
  echo "${var}=$BASE/releases/$name" >> "$ENVFILE"
  PUBLISHED+=("$name")
  echo "    $var -> /releases/$name"
}
publish_one "$DMG_ARM" DOWNLOAD_URL_MAC
publish_one "$DMG_X64" DOWNLOAD_URL_MAC_X64
publish_one "$EXE"     DOWNLOAD_URL_WIN
publish_one "$APK"     DOWNLOAD_URL_ANDROID

echo "==> restart app with new env"
docker compose -f "$REPO_DIR/deploy/prod/compose.yml" --env-file "$ENVFILE" up -d

echo "==> verify"
for i in $(seq 1 20); do
  curl -sf --max-time 10 "$BASE/api/health" >/dev/null && break; sleep 2
done
for f in "${PUBLISHED[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -r 0-1023 "$BASE/releases/$f")
  echo "  /releases/$f -> HTTP $code"
done
echo "done. Download page: $BASE/download"
