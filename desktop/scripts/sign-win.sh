#!/usr/bin/env bash
# 给 Windows 安装包做 Authenticode 签名 (Certum/SimplySign 云证书, 走 PKCS#11)。
#
#   bash desktop/scripts/sign-win.sh <exe> [更多 exe...]
#
# 前置: SimplySign Desktop 已登录 (手机 App 出 6 位 OTP)。会话由那个客户端持有,
# 本脚本只通过它挂出来的 PKCS#11 token 签名。会话过期要重登 —— Certum 的云签名
# 没有纯程序化凭据, 所以签名做不到完全无人值守。
#
# SimplySign Desktop 通过菜单栏应用维持云签名会话。签名前可用
# `pgrep -f "SimplySign Desktop"` 确认进程，再从菜单栏完成登录。
#
# 云签名会话需要在受支持的 macOS 或 Windows 桌面客户端中建立；Linux 构建机
# 只负责生成待签名产物。
set -euo pipefail

[ $# -ge 1 ] || { echo "用法: $0 <exe> [更多 exe...]" >&2; exit 1; }

# 时间戳服务器: 证书是 Certum 签发的, 用它自家的。时间戳让签名在证书过期后依然
# 有效 —— 没有它, 证书一到期所有已发布的包都会变成"签名无效"。
TS_URL="${TS_URL:-http://time.certum.pl/}"

# 签名里带的产品名和主页 —— Windows 的 UAC 提示框会把 PRODUCT_NAME 显给用户看,
# 所以换产品线签的时候必须一起换, 否则装 A 产品弹出的是 B 产品的名字。
# These values are descriptive metadata and do not affect the certificate chain.
PRODUCT_NAME="${PRODUCT_NAME:-DSH Cloud Desktop}"
PRODUCT_URL="${PRODUCT_URL:-https://dshcloud.online}"
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
    -n "$PRODUCT_NAME" -i "$PRODUCT_URL" \
    -in "$exe" -out "$out"

  # 验签: 光看 osslsigncode 退出码不够 —— 它对某些失败也返回 0。真去读回签名,
  # 确认签名存在**且带时间戳**。没时间戳的签名在证书过期那天会集体失效。
  echo "==> 复验"
  v="$(osslsigncode verify -in "$out" 2>&1 || true)"
  # ⚠️ **不要**拿 "Signature verification: failed" 当失败依据 —— 本机没有 Certum
  # 的中间证书链时它必然这么报 (unable to get local issuer certificate), 而签名
  # 本身可能仍然有效。这里验证三项可独立确认的事实：
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
echo "✓ 全部完成。签名后的安装包已准备好进入授权发布流程。"
