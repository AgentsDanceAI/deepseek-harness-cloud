#!/usr/bin/env bash
# 对端登记的桥 —— 在**应用机**上以 root 安装。
#
#   bash wg-peer-watch.sh install | remove | status
#
# 为什么要有这个: dhc-server 跑在容器里、uid 10001, 改不了宿主的 WireGuard 配置。给它
# 直接的 root 或者让它持有 wg 私钥都不行 —— 那个容器是对公网开的 web 服务。
#
# 于是走一条单向的投递: 应用只往投递目录写一个**公钥 + 隧道地址**的小文件, 宿主上的
# 监听器捡起来调 peer-add。应用拿不到私钥 (私钥只在盒子里生成、从不离开盒子), 也调不了
# wg —— 它能做的只有"提出登记一个公钥", 而公钥本身泄了没有用。
#
# 投递文件格式 (一行): <名字> <公钥> <隧道地址>
# 名字与地址都会被严格校验, 因为这行字最终会进 wg 配置。
set -euo pipefail
DROP="${DSH_WG_DROP:-/run/dsh-wg-pending}"
HERE="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
install)
  install -d -m 0733 "$DROP"      # 应用只需要能写进去, 不需要能列目录
  install -m 0755 /dev/stdin /usr/local/sbin/dsh-wg-peer-drain <<'EOF'
#!/bin/bash
# 把投递目录里的登记请求过一遍。每个文件处理完就删 —— 失败的也删, 并留一条日志:
# 留着会被无限重试, 而一个格式不对的文件永远不会自己变对。
DROP="${DSH_WG_DROP:-/run/dsh-wg-pending}"
WGSH=/usr/local/sbin/dsh-wg-peer-add
shopt -s nullglob
for f in "$DROP"/*.peer; do
  read -r name pub ip _ < "$f" || true
  rm -f "$f"
  # 严格校验: 这三个值会原样进 wg 配置文件
  case "$name" in ""|*[!a-zA-Z0-9._-]*) logger -t dsh-wg "名字不合法, 丢弃: $f"; continue;; esac
  case "$pub" in *[!A-Za-z0-9+/=]*|"") logger -t dsh-wg "公钥不合法, 丢弃: $name"; continue;; esac
  [[ "$ip" =~ ^10\.99\.1\.([0-9]{1,3})$ ]] || { logger -t dsh-wg "地址不在池里, 丢弃: $name $ip"; continue; }
  (( BASH_REMATCH[1] >= 10 && BASH_REMATCH[1] <= 249 )) || { logger -t dsh-wg "地址越界, 丢弃: $ip"; continue; }
  if [ -x "$WGSH" ] && "$WGSH" "$name" "$pub" "$ip" >/dev/null 2>&1; then
    logger -t dsh-wg "已登记对端 $name -> $ip"
  else
    logger -t dsh-wg "登记失败: $name -> $ip"
  fi
done
EOF
  # peer-add 的薄封装: 监听器只认这一个入口, 不直接调仓库里的脚本 (仓库会被 git pull
  # 换掉, 而这条链要在任何时候都可用)。
  install -m 0755 /dev/stdin /usr/local/sbin/dsh-wg-peer-add <<EOF
#!/bin/bash
exec bash "$HERE/tunnel-wg-144.sh" peer-add "\$1" "\$2" "\$3"
EOF
  install -m 0644 /dev/stdin /etc/systemd/system/dsh-wg-drain.service <<'EOF'
[Unit]
Description=Drain pending DSH WireGuard peer registrations
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/dsh-wg-peer-drain
EOF
  install -m 0644 /dev/stdin /etc/systemd/system/dsh-wg-drain.timer <<'EOF'
[Unit]
Description=Check for pending DSH WireGuard peer registrations
[Timer]
# 每 5 秒一次: 用户点开工作台时要等它, 拖太久就成了额外的等待。
OnBootSec=10s
OnUnitActiveSec=5s
AccuracySec=1s
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now dsh-wg-drain.timer
  echo "已装。投递目录 $DROP (0733, 应用只能写不能列)"
  systemctl --no-pager list-timers dsh-wg-drain.timer | head -3
  ;;
remove)
  systemctl disable --now dsh-wg-drain.timer 2>/dev/null || true
  rm -f /etc/systemd/system/dsh-wg-drain.{timer,service} /usr/local/sbin/dsh-wg-peer-{drain,add}
  systemctl daemon-reload
  echo "已退场 (已登记的对端不动)"
  ;;
status)
  systemctl is-active dsh-wg-drain.timer 2>/dev/null || echo "(没装)"
  echo "  待处理: $(ls "$DROP"/*.peer 2>/dev/null | wc -l) 个"
  journalctl -t dsh-wg --no-pager -n 5 2>/dev/null | tail -5
  ;;
*) sed -n '2,18p' "$0" >&2; exit 2 ;;
esac
