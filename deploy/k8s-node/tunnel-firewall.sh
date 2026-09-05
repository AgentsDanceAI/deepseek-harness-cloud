#!/usr/bin/env bash
# 隧道对端的防火墙 (在 k3s 节点上跑, root)。
#
# 为什么: 隧道把生产机 (<TUNNEL_APP_IP>) 直接接到节点上, 没有这道墙时它能碰到节点上
# 所有监听 0.0.0.0 的东西 —— 22、免密 redis 6379、kubelet 10250、frps 7000、别的
# 组员的 python 服务 —— 而且节点开着 ip_forward, 生产机加一条路由就能经节点进公司
# 内网 192.168.0.0/24。生产机是对公网开的 web 服务器, 假设它有一天被打穿。
#
# 放行的只有两样: 到本机 6443 (k8s API), 转发到 Pod/Service 网段 (10.42/16, 10.43/16)。
# 只碰 `-i tun0` 的包, 不影响节点上任何别的流量; 所有规则在自己的两条链里, 可整体退场。
#
#   bash tunnel-firewall.sh apply      # 装规则 (幂等, 可重复跑)
#   bash tunnel-firewall.sh remove     # 退场
#   bash tunnel-firewall.sh install    # 装到 /usr/local/sbin 并挂进 dsh-tunnel-up, 每次隧道建立都重放
#
# 规则不持久 (节点没有 iptables-persistent), 靠 dsh-tunnel-up 在每次隧道会话开始时重放。
set -euo pipefail
DEV=tun0; PEER=<TUNNEL_APP_IP>; API_PORT=6443
POD_CIDR=10.42.0.0/16; SVC_CIDR=10.43.0.0/16
IN=DSH-TUN-IN; FWD=DSH-TUN-FWD

apply() {
  iptables -w -N "$IN" 2>/dev/null || iptables -w -F "$IN"
  iptables -w -A "$IN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -w -A "$IN" -s "$PEER" -p tcp --dport "$API_PORT" -j ACCEPT
  iptables -w -A "$IN" -s "$PEER" -p icmp -j ACCEPT
  iptables -w -A "$IN" -m limit --limit 6/min -j LOG --log-prefix "dsh-tun-drop-in: "
  iptables -w -A "$IN" -j DROP
  iptables -w -C INPUT -i "$DEV" -j "$IN" 2>/dev/null || iptables -w -I INPUT 1 -i "$DEV" -j "$IN"

  iptables -w -N "$FWD" 2>/dev/null || iptables -w -F "$FWD"
  iptables -w -A "$FWD" -d "$POD_CIDR" -j RETURN
  iptables -w -A "$FWD" -d "$SVC_CIDR" -j RETURN
  iptables -w -A "$FWD" -m limit --limit 6/min -j LOG --log-prefix "dsh-tun-drop-fwd: "
  iptables -w -A "$FWD" -j DROP
  iptables -w -C FORWARD -i "$DEV" -j "$FWD" 2>/dev/null || iptables -w -I FORWARD 1 -i "$DEV" -j "$FWD"
  logger -t dsh-tunnel "firewall applied on $DEV (in: $API_PORT only; fwd: $POD_CIDR $SVC_CIDR only)"
}

remove() {
  iptables -w -D INPUT -i "$DEV" -j "$IN" 2>/dev/null || true
  iptables -w -D FORWARD -i "$DEV" -j "$FWD" 2>/dev/null || true
  for c in "$IN" "$FWD"; do iptables -w -F "$c" 2>/dev/null || true; iptables -w -X "$c" 2>/dev/null || true; done
}

install_self() {
  install -m 0755 "$0" /usr/local/sbin/dsh-tunnel-firewall
  local up=/usr/local/sbin/dsh-tunnel-up
  if [ -f "$up" ] && ! grep -q dsh-tunnel-firewall "$up"; then
    # 在 "ip link set up" 之后、进入守候循环之前重放规则
    sed -i 's|^ip link set "\$DEV" up$|ip link set "$DEV" up\n[ -x /usr/local/sbin/dsh-tunnel-firewall ] \&\& /usr/local/sbin/dsh-tunnel-firewall apply \|\| logger -t dsh-tunnel "firewall apply FAILED"|' "$up"
    grep -q dsh-tunnel-firewall "$up" || { echo "could not hook into $up" >&2; exit 1; }
  fi
  apply
}

case "${1:-}" in
  apply) apply ;;
  remove) remove ;;
  install) install_self ;;
  *) echo "usage: $0 apply|remove|install" >&2; exit 2 ;;
esac
