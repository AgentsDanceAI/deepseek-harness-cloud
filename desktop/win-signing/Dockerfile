# SimplySign (Certum) 云证书会话容器 —— 给 Windows 安装包做代码签名。
#
# 为什么要容器: 证书私钥在 Certum 的 HSM 里, 只能通过 PKCS#11 访问, 而 PKCS#11
# 库需要 SimplySign Desktop 持有一个已登录的会话。那个客户端是 GUI 程序, 而构建
# 机是无头 Linux —— 所以把 GUI 关进容器 (Xvnc), 只把 p11-kit socket 暴露出来,
# 宿主上的 osslsigncode 通过它签名。生产机不用装 X11, 也不留任何图形依赖。
#
# 会话必须人工建立: Certum 的云签名靠手机 App 出 OTP, 没有纯程序化凭据。一次
# 登录后可连续签多个文件, 会话过期要再登一次 —— 所以签名做不到完全无人值守。
FROM ubuntu:24.04

ARG SSD_VERSION=2.9.14-9.4.3.0
ARG SSD_URL=https://files.certum.eu/software/SimplySignDesktop/Linux-Ubuntu/${SSD_VERSION}/SimplySignDesktop-${SSD_VERSION}-x86_64-prod-ubuntu.bin

ENV DEBIAN_FRONTEND=noninteractive

# tigervnc-standalone-server: 无头 X + VNC 一体
# p11-kit-modules + p11-kit: 把容器内的 PKCS#11 token 通过 socket 转出去
# libpcsclite1: SimplySign 把云证书模拟成智能卡, 依赖 PC/SC 栈
# 其余 libX*/libgl: GUI 客户端的最小运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
      tigervnc-standalone-server tigervnc-common tigervnc-tools \
      p11-kit p11-kit-modules opensc \
      libpcsclite1 pcscd \
      libx11-6 libxext6 libxrender1 libxi6 libxtst6 libxcomposite1 \
      libice6 libsm6 libglib2.0-0 libgl1 libfontconfig1 \
      libxslt1.1 libxml2 libnss3 libdbus-1-3 libpulse0 libpulse-mainloop-glib0 \
      # Qt5 的 xcb 平台插件需要一整套 xcb 辅助库。缺任何一个的表现都是**段错误**,
      # 而不是"缺少 xxx" —— 2026-08-21 在这上面绕了几轮才定位到。
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
      libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 \
      libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxcb-glx0 libxcb-util1 \
      # Qt 启动时会加载 xcbglintegrations 与 platformthemes 下的插件, 缺 EGL/GTK
      # 会在初始化阶段直接段错误 (不是报"缺少 xxx", 所以很难从现象反推)。
      libegl1 libgl1-mesa-dri libgbm1 \
      libgtk-3-0 libgdk-pixbuf-2.0-0 libatk1.0-0 libatk-bridge2.0-0 libcairo2 libpango-1.0-0 \
      # stalonetray: SimplySign 启动后要往系统托盘挂图标, 容器里没有托盘就
      # 空指针崩溃 —— strace 显示崩在创建 SimplySignDesktop-Lock 之后的第一步,
      # 现象是段错误而非任何提示。参考 hpvb/certum-container 也装了这个。
      stalonetray xterm procps curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 官方 Ubuntu 版安装包 (与宿主同发行版, 比 CentOS 版少一层兼容风险)。
# 安装器是自解压 shell, --target 解到临时目录后再挪到 /opt。
# 安装器自解压后得到 SSD-<ver>-dist/ 与 SS-<ver>-dist/ 两个目录, 外加一个
# zz_install_en 脚本。**不跑那个脚本**: 它要 $USER_NAME 参数、往用户桌面写
# .desktop 快捷方式、改 home 下的配置 —— 容器里没有桌面用户, 那些步骤只会失败。
# 我们要的东西全在 dist 目录里, 直接摊平使用:
#   SimplySignDesktop            GUI 主程序 (持有云端会话)
#   SimplySignDesktop_start      启动包装 (设好 LD_LIBRARY_PATH)
#   SimplySignPKCS_64-MS-*.so    **Linux 版 PKCS#11 库** —— 签名真正要用的就是它
RUN curl -fsSL "${SSD_URL}" -o /tmp/ssd.bin \
 && (yes yes | sh /tmp/ssd.bin --target /tmp/ssd-extract >/dev/null 2>&1 || true) \
 && mkdir -p /opt/SimplySignDesktop \
 && cp -a /tmp/ssd-extract/SSD-*-dist/. /opt/SimplySignDesktop/ \
 # 跑官方安装器把配置布置到 home。它坚持要交互式终端 (管道喂 yes 会被判成
 # "Installation interrupted by user"), 所以用 script(1) 造伪终端。不跑它的话
 # GUI 会在读完配置后空指针崩溃 —— strace 显示崩在建 SimplySignDesktop-Lock 之后。
 && ( cd /tmp/ssd-extract && printf 'yes\nyes\nyes\n' > /tmp/ans \
      && script -qec "./zz_install_en root" /dev/null < /tmp/ans > /tmp/install.log 2>&1 || true ) \
 && rm -rf /tmp/ssd.bin /tmp/ssd-extract /tmp/ans \
 && test -x /opt/SimplySignDesktop/SimplySignDesktop \
 && ls /opt/SimplySignDesktop/SimplySignPKCS_64-*.so \
 # 动态库缺失要在**构建期**暴露: 否则容器能起来、p11-kit 也能挂 socket, 只有 GUI
 # 悄悄没跑起来 —— 而 GUI 才是持有云端会话的那一半, 缺了签名必然失败且难查。
 && ( LD_LIBRARY_PATH=/opt/SimplySignDesktop ldd /opt/SimplySignDesktop/SimplySignDesktop \
      | grep "not found" && echo "!! 上列动态库缺失" && exit 1 || true )

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 5900
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
