#!/usr/bin/env bash
# 给已构建的 macOS .app 做 Developer ID 签名 + 公证 + staple, 全程在 Linux 上跑。
#
#   ./desktop/scripts/sign-mac.sh <mac-arm64|mac> [...]
#
# 凭据通过 SIGN_DIR 提供，包括 Developer ID 证书、密码文件和公证 API 密钥。
#
# ── 关键: 用 --for-notarization, 别手工枚举嵌套实体 ──────────────────────────
# 该标志对**所有 Mach-O 二进制**统一开 hardened runtime (help 原文: "equivalent to
# --code-signature-flags runtime for all signed paths"), 并强制 Developer ID 证书
# + 时间戳服务器。
#
# 手工为嵌套二进制逐个指定 runtime 标志容易遗漏或受路径语法限制；
# `--for-notarization` 统一处理所有 Mach-O 文件。
#
# ── 验证纪律: 本地读签名标志, 别拿公证当测试 ────────────────────────────────
# verify-mac-signature.mjs 在提交公证前直接检查每个 Mach-O 的 CodeDirectory flags。
#
# ⚠️ 从 macOS 传输 .app 时，使用 COPYFILE_DISABLE=1，避免 bsdtar 生成
#    AppleDouble 文件。这些文件会污染签名清单并干扰二进制检测：
#      COPYFILE_DISABLE=1 tar -czf app.tar.gz -C dist/mac-arm64 "DSH Cloud Desktop.app"
#    传到之后先 `find <app> -name "._*" -delete` 复查一遍再签, 便宜且能兜住。
#
# ⚠️ zip 不能承载公证票据 (票据写进 bundle 的 CodeResources), 所以顺序必须是
#    签名 → 公证 → staple 到 .app 本体 → 最后才打 zip。
set -euo pipefail

SIGN_DIR="${SIGN_DIR:-/root/.agentsdance-signing}"
P12="$SIGN_DIR/devid-application.p12"
P12PW="$SIGN_DIR/p12-password"
NOTARY="$SIGN_DIR/notary-key.json"
here="$(cd "$(dirname "$0")" && pwd)"
desktop_dir="$(dirname "$here")"
DIST="$desktop_dir/build/upstream/dsh-plugin-desktop/dist"
ENT="$desktop_dir/entitlements.plist"
RC="${RCODESIGN:-rcodesign}"

for f in "$P12" "$P12PW" "$NOTARY" "$ENT"; do
  [ -f "$f" ] || { echo "!! 缺少 $f" >&2; exit 1; }
done
command -v "$RC" >/dev/null || { echo "!! rcodesign 未安装" >&2; exit 1; }

for dir in "$@"; do
  app="$DIST/$dir/DSH Cloud Desktop.app"
  [ -d "$app" ] || { echo "!! 找不到 $app" >&2; exit 1; }

  # ⚠️ --for-notarization 只统一处理 hardened runtime **标志**; entitlements 仍然
  # 按 rcodesign 的通用规则**只作用于主实体**。Electron 的渲染进程跑在
  # Helper (Renderer).app 里, 它开了 hardened runtime 却拿不到 allow-jit 的话,
  # V8 申请不到 JIT 内存，渲染进程会退出。Apple 公证只校验 runtime 标志，
  # 不验证应用运行所需的 entitlements 是否完整。
  # 所以每个 Helper.app 必须用 "<相对路径>:<文件>" 显式再给一遍 entitlements。
  # (这些路径不含 @, scope 能正常表达; 含 @ 的 ripgrep 不需要 entitlements。)
  ent_args=(--entitlements-xml-file "$ENT")
  for nested in "$app"/Contents/Frameworks/*.app; do
    [ -e "$nested" ] || continue
    rel="Contents/Frameworks/$(basename "$nested")"
    ent_args+=(--entitlements-xml-file "$rel:$ENT")
  done

  echo "==> [$dir] 签名 (--for-notarization + $(( ${#ent_args[@]} / 2 )) 组 entitlements)"
  "$RC" sign --for-notarization \
    --p12-file "$P12" --p12-password-file "$P12PW" \
    "${ent_args[@]}" "$app" >/dev/null

  echo "==> [$dir] 本地校验 hardened runtime (公证前先在本地拦一道)"
  node "$here/verify-mac-signature.mjs" "$app"

  echo "==> [$dir] 公证 + staple"
  "$RC" notary-submit --api-key-file "$NOTARY" --staple "$app" 2>&1 \
    | grep -E 'poll state|Accepted|Invalid|writing notarization ticket' || true

  echo "    ✅ $dir 完成"
done

echo
echo "下一步 (必须在 macOS 上做, 本机是 Linux 就把 .app 传过去):"
echo "  bash desktop/scripts/wrap-signed-dmg.sh <signed.app|signed.zip> <输出目录>"
echo "发布必须是 DMG 而非 zip —— zip 会让用户吃 App Translocation, 且应用内更新"
echo "读安装包尾部比对 'koly' (UDIF 标记), zip 过不了, 更新下载完必定失败。"
echo "发布流程还应拒绝 zip 或尾部缺少 koly 标记的 macOS 产物。"
