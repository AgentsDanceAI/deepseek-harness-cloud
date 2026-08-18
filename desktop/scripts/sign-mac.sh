#!/usr/bin/env bash
# 给已构建的 macOS .app 做 Developer ID 签名 + 公证 + staple, 全程在 Linux 上跑。
#
#   ./desktop/scripts/sign-mac.sh <mac-arm64|mac> [...]
#
# 凭据 (与 AgentsDance 共用 Team GKT967HB5K —— Developer ID 证书发给 Team 而非
# 单个 App, bundle id 无关, 且 Apple 限额 5 张, 复用是推荐做法):
#   /root/.agentsdance-signing/{devid-application.p12,p12-password,notary-key.json}
#
# ── 关键: 用 --for-notarization, 别手工枚举嵌套实体 ──────────────────────────
# 该标志对**所有 Mach-O 二进制**统一开 hardened runtime (help 原文: "equivalent to
# --code-signature-flags runtime for all signed paths"), 并强制 Developer ID 证书
# + 时间戳服务器。
#
# 2026-08-18 曾手工枚举每个 Helper/framework 用 "<相对路径>:runtime" 的 scoped 语法
# 逐个指定, 结果撞上 rcodesign 把 <path>@<int> 用作 fat binary 索引 ——
# "@vscode/ripgrep-darwin-arm64/bin/rg" 这个路径无法写进 scope 且无转义。为绕它试过
# --exclude(该文件不进封印 → macOS 判定 bundle 被篡改 → 用户机器上启动即闪退,
# 而公证却能通过, 骗过了验收)、临时改名(封印路径对不上 → 主二进制签名无效)、
# TOML 配置(rcodesign 静默忽略不认识的字段, 三种结构都验证不了)、bundle 签完后
# 补签(改了哈希 → 封印失效)。全部白费 —— --for-notarization 一行解决。
#
# ── 验证纪律: 本地读签名标志, 别拿公证当测试 ────────────────────────────────
# 公证一轮 3-5 分钟且只回一句"拒了"。verify-mac-signature.mjs 直接解析每个 Mach-O
# 的 CodeDirectory flags, 几秒出结果, 且能指出是哪个文件没开 runtime。
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
  # V8 申请不到 JIT 内存 → "Failed to reserve virtual memory for CodeRange" →
  # 渲染进程死 → 应用启动即退出 (2026-08-18 用户实测)。
  # 而**公证照样能过** —— Apple 只校验 runtime 标志, 不校验 entitlements 够不够用。
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
