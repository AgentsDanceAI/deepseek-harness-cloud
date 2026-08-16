#!/usr/bin/env bash
# Publish desktop installers from a green desktop-installers workflow run:
#   bash deploy/open-search/publish-installers.sh <run-id>
# Downloads the artifacts, drops them into dhc-server's data volume (served at
# /releases), points DOWNLOAD_URL_MAC/WIN at them, and restarts the app.
set -euo pipefail

RUN_ID="${1:?usage: publish-installers.sh <workflow-run-id>}"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENVFILE="$REPO_DIR/deploy/open-search/.env"
BASE="https://open-search.ai"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "==> download artifacts from run $RUN_ID"
gh run download "$RUN_ID" --dir "$WORK" -R AgentsDanceAI/deepseek-harness-cloud
find "$WORK" -type f \( -name "*.dmg" -o -name "*.exe" \) -exec ls -lh {} \;

DMG=$(find "$WORK" -name "*.dmg" | head -1)
EXE=$(find "$WORK" -name "*.exe" | head -1)
[ -n "$DMG" ] || { echo "no dmg artifact"; exit 1; }
[ -n "$EXE" ] || { echo "no exe artifact"; exit 1; }

echo "==> copy into dhc-server data volume (/app/data/releases)"
docker exec dhc-server mkdir -p /app/data/releases
docker cp "$DMG" dhc-server:/app/data/releases/
docker cp "$EXE" dhc-server:/app/data/releases/

DMG_NAME=$(basename "$DMG")
EXE_NAME=$(basename "$EXE")

echo "==> point DOWNLOAD_URL_* at the hosted files"
sed -i '/^DOWNLOAD_URL_MAC=/d;/^DOWNLOAD_URL_WIN=/d' "$ENVFILE"
{
  echo "DOWNLOAD_URL_MAC=$BASE/releases/$DMG_NAME"
  echo "DOWNLOAD_URL_WIN=$BASE/releases/$EXE_NAME"
} >> "$ENVFILE"

echo "==> restart app with new env"
docker compose -f "$REPO_DIR/deploy/open-search/compose.yml" --env-file "$ENVFILE" up -d

echo "==> verify"
for i in $(seq 1 20); do
  curl -sf --max-time 10 "$BASE/api/health" >/dev/null && break; sleep 2
done
for f in "$DMG_NAME" "$EXE_NAME"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -r 0-1023 "$BASE/releases/$f")
  size=$(curl -s -o /dev/null -w "%{size_download}" --max-time 120 "$BASE/releases/$f" -r 0-0 2>/dev/null || true)
  echo "  /releases/$f -> HTTP $code"
done
echo "done. Download page: $BASE/download"
