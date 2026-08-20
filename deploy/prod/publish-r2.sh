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

# Content-addressed prefixes. Overwriting an object at the same key does NOT
# invalidate Cloudflare's cache: republishing win-x64 under its original name
# left the edge serving the previous build for the rest of its four-hour TTL,
# so users kept downloading a stale installer while R2 held the new one.
# Giving each build its own path makes every release a fresh URL that no cache
# can shadow, keeps the download filename clean, and leaves unchanged files at
# their existing path so they are neither re-uploaded nor re-downloaded.
echo "==> address each artifact by content hash"
KEYED="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$KEYED"' EXIT
declare -A KEY_OF
for f in "$STAGE"/*; do
  name="$(basename "$f")"
  sha="$(sha256sum "$f" | cut -c1-8)"
  mkdir -p "$KEYED/$sha"
  ln "$f" "$KEYED/$sha/$name" 2>/dev/null || cp "$f" "$KEYED/$sha/$name"
  KEY_OF["$name"]="$sha/$name"
  echo "    $name -> $sha/"
done

echo "==> upload to r2://$R2_BUCKET"
rclone copy "$KEYED" "R2:$R2_BUCKET" --progress --s3-chunk-size 32M --transfers 2

echo "==> verify each object is publicly readable over $R2_PUBLIC_BASE"
fail=0
for f in "$STAGE"/*; do
  name="$(basename "$f")"
  url="$R2_PUBLIC_BASE/${KEY_OF[$name]}"
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
# ⚠️ mac 当前发的是 zip, 这是个待还的债 (2026-08-19 实测确认危害):
# zip 没有拖拽引导, 用户解压后就地双击 → macOS App Translocation 把 App
# 挂到 /private/var/.../AppTranslocation 的只读随机目录里跑, 自动更新失效,
# 而且全程无提示。原来的结论是「Linux 打不出可公证的 UDIF, 那就发 zip」——
# 前半句对, 后半句选错了方向: 正确做法是把最后这层封装放回 macOS。
# 已验证 (拿线上 2.0.0 arm64 包实测): 重新封装**不损公证** —— 票据 staple 在
# .app 本体, 外层容器换了照样 stapler validate 通过、spctl 判 Notarized。
# mac 发 DMG (2026-08-20 改回)。发 zip 会同时坏掉两件事:
#   1) zip 里没有 /Applications 软链, 用户解压后就地双击 → App Translocation,
#      应用跑在只读随机目录里、自动更新失效, 且全程无提示;
#   2) **应用内更新根本走不通** —— update-download.ts 读安装包尾部比对 'koly'
#      (UDIF 结尾标记), 不匹配就抛 "The downloaded file is not a UDIF disk image."
#      zip 永远过不了这道校验, 于是"检查更新"下载完必定失败。
# 产 DMG 的方式: 签名公证后在 **macOS** 上跑 desktop/scripts/wrap-signed-dmg.sh
# (Linux 打不出 UDIF, hdiutil 只有 macOS 才有), 再把 DMG 放进发布目录。
declare -A MAP=(
  [DOWNLOAD_URL_MAC]="mac-arm64.dmg"
  [DOWNLOAD_URL_MAC_X64]="mac-x64.dmg"
  [DOWNLOAD_URL_WIN]="win-x64.exe"
  [DOWNLOAD_URL_WIN_ARM]="win-arm64.exe"
  [DOWNLOAD_URL_ANDROID]="android.apk"
)
for var in "${!MAP[@]}"; do
  # Match the real filename by suffix rather than assuming a naming scheme.
  # ⚠️ 必须有 default 分支并先清空 pat: case 落空时 bash 会**保留上一轮循环的值**,
  # 于是该变量会被指到上一个平台的产物上。2026-08-18 实测: MAP 里把 mac 从 .dmg
  # 改成 .zip 后没同步这里, DOWNLOAD_URL_MAC 被静默指向了 Android 的 .apk ——
  # 官网 macOS 下载按钮会给出一个安卓安装包, 而脚本一行报错都没有。
  pat=""
  case "${MAP[$var]}" in
    mac-arm64.dmg) pat="*mac-arm64.dmg" ;;
    mac-x64.dmg)   pat="*mac-x64.dmg" ;;
    mac-arm64.zip) pat="*mac-arm64.zip" ;;
    mac-x64.zip)   pat="*mac-x64.zip" ;;
    win-x64.exe)   pat="*-x64-Setup.exe" ;;
    win-arm64.exe) pat="*-arm64-Setup.exe" ;;
    android.apk)   pat="*.apk" ;;
    *) echo "    !! $var: MAP 值 '${MAP[$var]}' 没有对应的匹配模式" >&2; exit 1 ;;
  esac
  # shellcheck disable=SC2086
  found="$(cd "$STAGE" && ls -1 $pat 2>/dev/null | head -1 || true)"
  [ -n "$found" ] || { echo "    skip $var (no artifact)"; continue; }
  # mac 安装包必须是真 UDIF: 应用内更新会读尾部 512 字节比对 'koly', 过不了就
  # 报 "not a UDIF disk image" —— 发错格式的代价是更新功能整个失效, 而且只有
  # 用户点了更新才会暴露。宁可在这里挡下发布, 也不要发一个注定更新失败的包。
  case "$found" in
    *.dmg)
      if [ "$(tail -c 512 "$STAGE/$found" | head -c 4)" != "koly" ]; then
        echo "    !! $found 尾部没有 koly 标记 —— 不是 UDIF 磁盘映像, 拒绝发布。" >&2
        echo "       用 desktop/scripts/wrap-signed-dmg.sh 在 macOS 上重新封装。" >&2
        exit 1
      fi ;;
    *mac*.zip)
      echo "    !! $found 是 zip: 用户会吃 App Translocation, 且应用内更新过不了" >&2
      echo "       UDIF 校验。改用 wrap-signed-dmg.sh 产 DMG 后再发。" >&2
      exit 1 ;;
  esac
  sed -i "/^${var}=/d" "$ENVFILE"
  echo "${var}=${R2_PUBLIC_BASE}/${KEY_OF[$found]}" >> "$ENVFILE"
  echo "    $var -> $R2_PUBLIC_BASE/${KEY_OF[$found]}"
done

echo "==> restart the app so it picks up the new targets"
docker compose up -d dhc-server

echo
echo "Done. The site now redirects downloads to R2; /releases stays as a fallback"
echo "(and remains the only path for self-hosters). Check the counter still moves:"
echo "  curl -sI https://dshcloud.online/dl/mac-arm64 | grep -i location"
