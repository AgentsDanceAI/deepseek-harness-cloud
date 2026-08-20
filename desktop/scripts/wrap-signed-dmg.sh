#!/usr/bin/env bash
# 把**已签名+已公证**的 .app 封装成带拖拽引导的 DMG。
#
#   bash desktop/scripts/wrap-signed-dmg.sh <signed.app|signed.zip> [输出目录]
#
# 为什么需要这一步 (2026-08-19):
# 签名/公证在 Linux 构建机上做 (rcodesign 能签 mac 二进制), 但 Linux 打不出
# UDIF 格式的 DMG —— 当时的结论是"那就发 zip", 代价是用户解压后就地双击运行,
# 触发 macOS App Translocation: 系统把 App 挂到 /private/var/.../AppTranslocation
# 的只读随机目录里跑, 自动更新一类的功能随之失效, 而且**没有任何提示**告诉
# 用户应该先拖进「应用程序」。
#
# 正确的结论不是放弃 DMG, 而是把最后这层封装放回 macOS 做。已验证 (2026-08-19,
# 线上 2.0.0 arm64 包实测): 重新封装**不损公证** —— 票据 staple 在 .app 本体上,
# 外层容器换了照样 `stapler validate` 通过、spctl 判 Notarized Developer ID。
#
# 本脚本只做封装, 不做签名: 传进来的 .app 必须已经签好名并公证过, 否则直接拒绝
# (未公证的包封进 DMG 只会把 Gatekeeper 问题原样带给用户)。
set -euo pipefail

[ "$(uname -s)" = "Darwin" ] || { echo "本脚本必须在 macOS 上运行 (hdiutil 打 UDIF)" >&2; exit 1; }

SRC="${1:?usage: wrap-signed-dmg.sh <signed.app|signed.zip> [outdir]}"
OUTDIR="${2:-$(pwd)}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# 入参可以是 .app 目录, 也可以是发布用的 zip
if [ -d "$SRC" ]; then
  APP="$SRC"
else
  echo "==> 解开 $SRC"
  ditto -x -k "$SRC" "$WORK/x"
  APP=$(find "$WORK/x" -maxdepth 2 -name "*.app" -type d | head -1)
  [ -n "$APP" ] || { echo "zip 里没有 .app" >&2; exit 1; }
fi
echo "==> app: $APP"

# 封装前先卡住: 没签名或没公证的包不许进 DMG
echo "==> 校验签名与公证"
codesign --verify --deep --strict "$APP" 2>&1 | tail -2 || { echo "签名校验失败" >&2; exit 1; }
xcrun stapler validate "$APP" >/dev/null 2>&1 || { echo "公证票据缺失/无效 —— 先公证再封 DMG" >&2; exit 1; }

VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" 2>/dev/null || echo "0.0.0")
ARCH_LABEL="${ARCH_LABEL:-$(uname -m)}"
VOLNAME="${VOLNAME:-DSH Cloud Desktop}"
DMG="$OUTDIR/DSH-Cloud-Desktop-${VERSION}-mac-${ARCH_LABEL}.dmg"

echo "==> 组装 DMG 内容 (app + /Applications 拖拽目标)"
STAGE="$WORK/stage"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
# 注意: 已公证的包**不要**再放「请先阅读/如何绕过 Gatekeeper」之类的说明 ——
# 那是未签名时期的产物, 现在放只会让用户以为这包有问题。

mkdir -p "$OUTDIR"
hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG" >/dev/null

echo "==> 复验: 挂载后确认拖拽目标在、公证仍有效"
MP=$(hdiutil attach "$DMG" -nobrowse -readonly | tail -1 | awk -F'\t' '{print $NF}')
trap 'hdiutil detach "$MP" -quiet 2>/dev/null || true; rm -rf "$WORK"' EXIT
[ -L "$MP/Applications" ] || { echo "DMG 里缺少 /Applications 软链" >&2; exit 1; }
xcrun stapler validate "$MP/$(basename "$APP")" >/dev/null 2>&1 || { echo "封装后公证票据失效" >&2; exit 1; }
spctl -a -t exec "$MP/$(basename "$APP")" >/dev/null 2>&1 || { echo "封装后 Gatekeeper 不放行" >&2; exit 1; }

echo
echo "✓ $DMG"
echo "  拖拽目标 ✓  公证票据 ✓  Gatekeeper: Notarized Developer ID ✓"
