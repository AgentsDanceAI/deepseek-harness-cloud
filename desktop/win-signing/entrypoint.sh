#!/usr/bin/env bash
# 起 Xvnc + SimplySign Desktop, 并把 PKCS#11 token 通过 p11-kit socket 转出容器。
set -euo pipefail

VNC_PASSWORD="${VNC_PASSWORD:?set VNC_PASSWORD — 这个端口会暴露一个能操作你代码签名证书的桌面}"
DISPLAY_NUM="${DISPLAY_NUM:-:1}"
export DISPLAY="$DISPLAY_NUM"

mkdir -p /root/.vnc /run/p11-kit
printf '%s\n' "$VNC_PASSWORD" | vncpasswd -f > /root/.vnc/passwd
chmod 600 /root/.vnc/passwd

# pcscd: SimplySign 把云证书模拟成智能卡, 没有它 token 出不来
pcscd --foreground --auto-exit &
sleep 1

echo "==> 启动 Xvnc on $DISPLAY_NUM"
Xvnc "$DISPLAY_NUM" -geometry 1024x768 -depth 24 -rfbauth /root/.vnc/passwd \
     -rfbport 5900 -SecurityTypes VncAuth -AlwaysShared &
sleep 2

# 随包自带 Qt 5.9, 必须让它找到自己的库和插件 —— 否则 Qt 报"找不到 xcb 平台
# 插件", 设了插件路径之后又会因为缺 xcb 辅助库直接段错误 (见 Dockerfile 注释)。
export LD_LIBRARY_PATH=/opt/SimplySignDesktop
export QT_QPA_PLATFORM_PLUGIN_PATH=/opt/SimplySignDesktop/plugins/platforms
export XDG_RUNTIME_DIR=/tmp/runtime-root
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

# 必须先有系统托盘, 否则 SimplySign 挂托盘图标时空指针崩溃 (见 Dockerfile 注释)
echo "==> 启动系统托盘 (stalonetray)"
stalonetray --geometry 1x1+0+0 --icon-size 16 >/tmp/tray.log 2>&1 &
sleep 2

echo "==> 启动 SimplySign Desktop"
# 用 _start 包装而非裸二进制: 它负责设好 LD_LIBRARY_PATH 指向随包的 Qt 库
SSD_BIN=/opt/SimplySignDesktop/SimplySignDesktop_start
[ -x "$SSD_BIN" ] || SSD_BIN=/opt/SimplySignDesktop/SimplySignDesktop
[ -n "$SSD_BIN" ] || { echo "!! 找不到 SimplySignDesktop 可执行文件:"; find /opt/SimplySignDesktop -maxdepth 3 | head -20; exit 1; }
echo "    $SSD_BIN"
OPENSSL_CONF=/etc/ssl/ "$SSD_BIN" &
sleep 3

# p11-kit server 把容器内看到的 token 转成 unix socket, 挂载出去给宿主用。
# -f 前台运行, 容器活着 socket 就活着。
echo "==> 暴露 p11-kit socket 到 /run/p11-kit/p11kit.sock"
echo
echo "下一步 (在宿主上做):"
echo "  1. VNC 连到本机 5999 端口, 用手机 SimplySign App 的 OTP 登录"
echo "  2. 登录后**必须点 close 按钮**, 否则 token 不会挂出来"
echo "  3. 然后跑 desktop/scripts/sign-win.sh"
echo
# provider 必须是 **SimplySign 自己的 PKCS#11 库** —— 云证书只有它认得,
# opensc 那个只管本地智能卡。库随安装包发布, 版本号会变, 所以现找不写死。
PKCS11_SO="$(ls /opt/SimplySignDesktop/SimplySignPKCS_64-*.so 2>/dev/null | head -1)"
[ -n "$PKCS11_SO" ] || { echo "!! 找不到 SimplySign 的 PKCS#11 库" >&2; exit 1; }
echo "    provider: $PKCS11_SO"
exec p11-kit server -f -n /run/p11-kit/p11kit.sock --provider "$PKCS11_SO" 'pkcs11:'
