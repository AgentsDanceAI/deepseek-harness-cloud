#!/usr/bin/env bash
# 给已构建的 macOS .app 做 Developer ID 签名 + 公证 + staple, 全程在 Linux 上跑。
#
#   ./desktop/scripts/sign-mac.sh <mac-arm64|mac> [...]
#
# 前置: desktop/build/upstream/.../dist/<dir>/DSH Cloud Desktop.app 已由
#       electron-builder 产出 (build-mac.sh 或手工 --mac dir)。
#
# 凭据 (与 AgentsDance 共用同一 Team GKT967HB5K 的证书, Developer ID 证书是
# 发给 Team 而非某个 App 的, bundle id 无关):
#   /root/.agentsdance-signing/devid-application.p12
#   /root/.agentsdance-signing/p12-password
#   /root/.agentsdance-signing/notary-key.json   (rcodesign encode-app-store-connect-api-key 产物)
#
# ⚠️ 两个已被实测钉死的坑 (见 a sibling production system/docs/code-signing.md 报告二):
#   1. rcodesign 未加 scope 的 --code-signature-flags 只作用于主实体, 嵌套的
#      Helper.app / framework 内可执行体会落 flags=0x0 → 公证必拒。必须对每个
#      嵌套实体用 "<相对路径>:<值>" 的 scoped 语法逐个指定。
#   2. zip 不能 staple 票据 (票据写在 UDIF koly trailer / bundle 里)。所以顺序是
#      签 .app → 公证 .app → staple 到 .app 本体 → 最后才打 zip。
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

sign_app() {
  local app="$1"
  # ── 含 @ 的路径要先单独签 ──────────────────────────────────────────────
  # rcodesign 的 scope 语法把 <path>@<int> 用于 fat binary 索引, 所以路径里的
  # "@vscode/..." 会被当成 @ 表达式解析失败 (2026-08-18 实测:
  # "unable to parse settings scope: @ expression not recognized"), 且无转义。
  # 对策: 这类裸 Mach-O 先以独立文件身份签好 runtime, 再在 bundle 签名时用
  # --exclude 跳过, 其既有签名得以保留。
  local at_excludes=() f rel
  while IFS= read -r f; do
    rel="${f#"$app"/}"
    echo "    单独签 (含@): $rel"
    "$RC" sign --p12-file "$P12" --p12-password-file "$P12PW" \
      --code-signature-flags runtime "$f" >/dev/null
    at_excludes+=(--exclude "$rel")
  done < <(find "$app" -type f ! -name "*.dylib" ! -name "*.node" -exec file {} + 2>/dev/null \
           | grep -E "Mach-O.*executable" | sed "s/: *Mach-O.*//" | grep "@")

  local args=(--code-signature-flags runtime --entitlements-xml-file "$ENT")
  local nested rel exe
  # Helper*.app: bundle 级 scope 覆盖其内全部内容
  for nested in "$app"/Contents/Frameworks/*.app; do
    [ -e "$nested" ] || continue
    rel="Contents/Frameworks/$(basename "$nested")"
    args+=(--code-signature-flags "$rel:runtime" --entitlements-xml-file "$rel:$ENT")
  done
  # 其余所有 Mach-O 可执行体, 逐个补 runtime。三点必须按类型而非权限位枚举:
  #   · node-pty 的 spawn-helper 权限是 0644 (没有 +x), 按 -perm -100 会漏掉,
  #     而公证照样逐二进制校验它 —— 2026-08-18 首次公证就是栽在这两个文件上
  #     ("The executable does not have the hardened runtime enabled");
  #   · ripgrep 的 rg 在 Resources/app.asar.unpacked 下, 不在 Frameworks 里;
  #   · framework 内的 crashpad_handler / ShipIt bundle 级 scope 穿透不到。
  # 主实体自身 (Contents/MacOS/<name>) 由未加 scope 的参数覆盖, 这里跳过。
  local main_exe="Contents/MacOS/$(basename "${app%.app}")"
  while IFS= read -r exe; do
    rel="${exe#"$app"/}"
    [ "$rel" = "$main_exe" ] && continue
    case "$rel" in *@*) continue ;; esac
    case "$rel" in Contents/Frameworks/*.app/*) continue ;; esac
    args+=(--code-signature-flags "$rel:runtime")
  done < <(find "$app" -type f ! -name "*.dylib" ! -name "*.node" -exec file {} + 2>/dev/null \
           | grep -E "Mach-O.*(executable|bundle)" | sed "s/: *Mach-O.*//")
  echo "    scoped 实体数: $(( ${#args[@]} / 2 ))"
  "$RC" sign --p12-file "$P12" --p12-password-file "$P12PW" \
    "${at_excludes[@]}" "${args[@]}" "$app"
}

for dir in "$@"; do
  app="$DIST/$dir/DSH Cloud Desktop.app"
  [ -d "$app" ] || { echo "!! 找不到 $app" >&2; exit 1; }
  echo "==> [$dir] 签名"
  sign_app "$app"
  echo "==> [$dir] 公证 (Apple 扫描, 通常几分钟)"
  "$RC" notary-submit --api-key-file "$NOTARY" --staple "$app"
  echo "==> [$dir] 校验"
  "$RC" print-signature-info "$app" 2>/dev/null | grep -iE 'signature_flags|team_name|authority' | head -5 | sed 's/^/    /' || true
  echo "    ✅ $dir 完成 (票据已 staple 进 .app)"
done
