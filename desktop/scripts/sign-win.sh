#!/usr/bin/env bash
# 给 Windows 安装包做 Authenticode 签名 (Certum/SimplySign 云证书, 走 PKCS#11)。
#
#   bash desktop/scripts/sign-win.sh <exe> [更多 exe...]
#
# 前置: SimplySign Desktop 已登录 (手机 App 出 6 位 OTP)。会话由那个客户端持有,
# 本脚本只通过它挂出来的 PKCS#11 token 签名。会话过期要重登 —— Certum 的云签名
# 没有纯程序化凭据, 所以签名做不到完全无人值守。
#
# ⚠️ "SimplySign Desktop 双击打不开"不等于它没在跑。装好后它由 LaunchAgent 拉起,
# 常驻在**菜单栏**; Gatekeeper 只拦双击那条启动路径, launchd 那条照走。2026-08-21
# 为这个假象绕了一大圈去啃 Linux 容器方案, 而进程 (PID 51171) 从头到尾好好活着。
# 先确认: pgrep -f "SimplySign Desktop" —— 有输出就直接点菜单栏图标里的
# "Connect with cloud" 登录, 别去修 dylib、别关 SIP、别重装。
#
# ⚠️ 为什么不在构建机 (Linux) 上签: Certum 确实提供 Linux 版 PKCS#11 库
# (SimplySignPKCS_64-MS-*.so, 随 Linux 版安装包发布), 而且 p11-kit 能把 token
# 从容器转出给宿主 —— 这条链 2026-08-21 实测打通到"宿主能问到库、返回 No slots"。
# 卡住的是持有会话的那个 Qt 客户端: 它在容器里必定段错误 (xcb 插件/6 个 xcb 辅助库/
# EGL/GTK/托盘/窗口管理器/四种 Qt 后端/官方配置与启动脚本全部试过, ldd 全绿,
# strace 显示崩在创建 SimplySignDesktop-Lock 之后, 空指针无任何提示)。
# 而 Ubuntu 24.04 把浏览器全 snap 化, 容器里也起不来, 所以改走 OAuth2 网页登录
# 这条同样断了。结论: 会话只能在 **macOS/Windows 桌面**上建立。
set -euo pipefail

[ $# -ge 1 ] || { echo "用法: $0 <exe> [更多 exe...]" >&2; exit 1; }

# 时间戳服务器: 证书是 Certum 签发的, 用它自家的。时间戳让签名在证书过期后依然
# 有效 —— 没有它, 证书一到期所有已发布的包都会变成"签名无效"。
TS_URL="${TS_URL:-http://time.certum.pl/}"
PKCS11_MODULE="${PKCS11_MODULE:-/usr/local/lib/SimplySignPKCS/SimplySignPKCS-MS-1.1.24.dylib}"
[ -f "$PKCS11_MODULE" ] || {
  PKCS11_MODULE="$(ls /usr/local/lib/SimplySignPKCS/*.dylib 2>/dev/null | head -1)"
  [ -n "$PKCS11_MODULE" ] || { echo "!! 找不到 SimplySign 的 PKCS#11 库" >&2; exit 1; }
}
command -v osslsigncode >/dev/null || { echo "!! 需要 osslsigncode (brew install osslsigncode)" >&2; exit 1; }

# osslsigncode 要通过 OpenSSL 的 pkcs11 engine 才能用硬件/云密钥, 而 brew 装的
# openssl@3 默认只在自己 Cellar 里找 engine —— libp11 装到 /opt/homebrew/lib,
# 两者对不上, 报 "Failed to find and load 'pkcs11' engine"。显式指过去。
if [ -z "${OPENSSL_ENGINES:-}" ] && [ -d /opt/homebrew/lib/engines-3 ]; then
  export OPENSSL_ENGINES=/opt/homebrew/lib/engines-3
  export OPENSSL_MODULES=/opt/homebrew/lib/ossl-modules
fi
[ -f "${OPENSSL_ENGINES:-/nonexistent}/pkcs11.dylib" ] || \
  echo "   (提示: 若报找不到 pkcs11 engine, 装 libp11: brew install libp11)" >&2

echo "==> 检查证书是否已挂出 (需要 SimplySign Desktop 已登录)"
slots="$(pkcs11-tool --module "$PKCS11_MODULE" -L 2>/dev/null || true)"
if ! printf '%s' "$slots" | grep -qiE "slot [0-9]|token"; then
  echo "!! 没有可用的 token —— SimplySign Desktop 还没登录, 或会话已过期。" >&2
  echo "   打开 SimplySign Desktop, 用手机 App 的 6 位 OTP 登录后重跑。" >&2
  exit 1
fi
printf '%s\n' "$slots" | grep -iE "slot|token|label" | head -4

for exe in "$@"; do
  [ -f "$exe" ] || { echo "!! 找不到 $exe" >&2; exit 1; }
  out="${exe%.exe}-signed.exe"
  echo "==> 签名 $(basename "$exe")"
  osslsigncode sign \
    -pkcs11module "$PKCS11_MODULE" \
    -pkcs11cert 'pkcs11:model=SimplySign%20C' \
    -key 'pkcs11:model=SimplySign%20C' \
    -h sha256 -ts "$TS_URL" \
    -n "DSH Cloud Desktop" -i "https://dshcloud.online" \
    -in "$exe" -out "$out"

  # 验签: 光看 osslsigncode 退出码不够 —— 它对某些失败也返回 0。真去读回签名,
  # 确认签名存在**且带时间戳**。没时间戳的签名在证书过期那天会集体失效。
  echo "==> 复验"
  v="$(osslsigncode verify -in "$out" 2>&1 || true)"
  # ⚠️ **不要**拿 "Signature verification: failed" 当失败依据 —— 本机没有 Certum
  # 的中间证书链时它必然这么报 (unable to get local issuer certificate), 而签名
  # 本身完全有效。2026-08-21 第一版就是这么误判的, 差点把签好的包判成坏包。
  # 真正该验的是这三件事实:
  #   1. 摘要匹配 (Current == Calculated) —— 签名覆盖的确实是这个文件
  #   2. 签发者是我们的 CA
  #   3. 带 RFC3161 时间戳 —— 没有它, 证书到期那天所有已发布的包集体失效
  cur="$(printf '%s' "$v" | grep -oE 'Current message digest *: *[0-9A-F]+' | grep -oE '[0-9A-F]{40,}' | head -1)"
  cal="$(printf '%s' "$v" | grep -oE 'Calculated message digest *: *[0-9A-F]+' | grep -oE '[0-9A-F]{40,}' | head -1)"
  [ -n "$cur" ] && [ "$cur" = "$cal" ] || {
    echo "!! 摘要不匹配 —— 签名没有覆盖这个文件" >&2; printf '%s\n' "$v" | tail -6 >&2; exit 1; }
  printf '%s' "$v" | grep -qiE "Issuer *:.*Certum Code Signing" || {
    echo "!! 签发者不是预期的 Certum Code Signing CA" >&2; exit 1; }
  printf '%s' "$v" | grep -qiE "Timestamp time *:" || {
    echo "!! 签名里没有 RFC3161 时间戳 —— 证书过期后签名会失效, 拒绝交付。" >&2; exit 1; }
  mv -f "$out" "$exe"
  echo "    ✓ $(basename "$exe") 已签名并带时间戳"
done

echo
echo "✓ 全部完成。发布前记得把签名后的 exe 送回构建机的发布目录。"
