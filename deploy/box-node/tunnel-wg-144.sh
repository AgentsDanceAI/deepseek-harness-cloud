#!/usr/bin/env bash
# 备用节点隧道 —— **应用机**这一侧 (WireGuard)。以 root 跑。
#
#   bash tunnel-wg-144.sh init            # 生成密钥 + 写配置, 打印本机公钥 (给 provision.sh 用)
#   bash tunnel-wg-144.sh peer <盒子公钥>                    # 备用节点 (= peer-add standby ...)
#   bash tunnel-wg-144.sh peer-add <名字> <公钥> <隧道地址>  # 多对端: 每人一台的用户盒子
#   bash tunnel-wg-144.sh peer-rm <名字> | peer-ls
#   bash tunnel-wg-144.sh up | down | status
#   bash tunnel-wg-144.sh remove
#
# 为什么是 WireGuard 而不是 248 那种 ssh -w: Box 每次 resume 都可能换机器, 落到
# baremetal 时 sshd 开不出 tun (2026-09-06 实测), 而落点不由我们定。WireGuard 由
# **盒子出站**拨过来 —— 盒子的地址怎么变都无所谓, 这边只认公钥。
#
# 前提: 阿里云安全组要放行入站 UDP $PORT (只有控制台能点)。WireGuard 对没带正确
# 密钥的包一个字节都不回, 端口开着不增加可被扫描的面。
#
# 方向是单向的: 应用机主动连节点 (6443 + 任意 Pod IP), 节点**不需要**反过来碰应用机
# —— 工作台 Pod 调网关走的是公网 (netpol 的出站白名单把整个 10/8 排除了)。所以这里
# 只放行"已建立"的回程包, 盒子那把私钥即使泄了也主动进不来。
set -euo pipefail

IF="${DSH_WG_IF:-dshbox0}"
CONF="/etc/wireguard/$IF.conf"
PORT="${DSH_WG_PORT:-51820}"
APP_IP="${DSH_BOX_TUNNEL_APP_IP:-10.99.1.1}"
NODE_IP="${DSH_BOX_TUNNEL_NODE_IP:-10.99.1.2}"
CLUSTER_CIDR="${DSH_BOX_CLUSTER_CIDR:-10.44.0.0/16}"
SERVICE_CIDR="${DSH_BOX_SERVICE_CIDR:-10.45.0.0/16}"

need_wg() { command -v wg >/dev/null || { echo "没有 wg —— apt install wireguard-tools" >&2; exit 2; }; }

case "${1:-}" in
init)
  need_wg
  install -d -m 0700 /etc/wireguard
  if [ -s /etc/wireguard/$IF.key ]; then
    echo "已有密钥, 不重新生成 (换密钥会让盒子那边的对端配置失效)" >&2
  else
    (umask 077; wg genkey > /etc/wireguard/$IF.key)
  fi
  wg pubkey < /etc/wireguard/$IF.key > /etc/wireguard/$IF.pub
  if [ -s "$CONF" ]; then
    echo "$CONF 已存在, 保留 (改端口/地址请自己编辑)" >&2
  else
    umask 077
    cat > "$CONF" <<EOF
[Interface]
Address = $APP_IP/24
ListenPort = $PORT
PostUp = /usr/local/sbin/dsh-box-wg-fence apply %i
PostDown = /usr/local/sbin/dsh-box-wg-fence remove %i
EOF
    # 私钥单独一行追加, 不进上面的 heredoc (少一次被日志/回显捎带的机会)
    printf 'PrivateKey = %s\n' "$(cat /etc/wireguard/$IF.key)" >> "$CONF"
    chmod 0600 "$CONF"
  fi
  install -m 0755 "$(dirname "$0")/wg-peer.py" /usr/local/sbin/dsh-wg-peer.py
  install -m 0755 /dev/stdin /usr/local/sbin/dsh-box-wg-fence <<'EOF'
#!/bin/bash
# 隧道这一侧的围栏: 只放行应用机主动发起之后的回程包, 盒子主动打过来的一律丢。
# 规则全在自己的两条链里, remove 能整体退场。FORWARD 那条是给 docker 容器的路径用的
# (dhc-server 容器直连 Pod IP 时, 回程走 FORWARD 不走 INPUT)。
DEV="${2:-dshbox0}"
case "${1:-}" in
apply)
  for c in DSH-BOX-IN DSH-BOX-FWD; do
    iptables -N $c 2>/dev/null; iptables -F $c
    iptables -A $c -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    iptables -A $c -m limit --limit 5/min -j LOG --log-prefix "dsh-box-wg drop: "
    iptables -A $c -j DROP
  done
  # RETURN 之后继续走原链 (INPUT 默认 ACCEPT, FORWARD 交给 docker 那套), 所以只需
  # 把入口挂在最前面。幂等: 已经有了就不重复插。
  iptables -C INPUT   -i "$DEV" -j DSH-BOX-IN  2>/dev/null || iptables -I INPUT   1 -i "$DEV" -j DSH-BOX-IN
  iptables -C FORWARD -i "$DEV" -j DSH-BOX-FWD 2>/dev/null || iptables -I FORWARD 1 -i "$DEV" -j DSH-BOX-FWD
  ;;
remove)
  iptables -D INPUT   -i "$DEV" -j DSH-BOX-IN  2>/dev/null
  iptables -D FORWARD -i "$DEV" -j DSH-BOX-FWD 2>/dev/null
  for c in DSH-BOX-IN DSH-BOX-FWD; do iptables -F $c 2>/dev/null; iptables -X $c 2>/dev/null; done
  ;;
esac
exit 0
EOF
  echo "应用机公钥 (喂给 provision.sh 的 DSH_WG_SERVER_PUBKEY):"
  cat /etc/wireguard/$IF.pub
  echo "安全组要放行: 入站 UDP $PORT (来源可以只写盒子的出口地址, 但它每次 resume 都变, 建议 0.0.0.0/0)"
  ;;
peer)
  # 备用节点那一个对端 (兼容原有用法)。等价于 peer-add standby <公钥> <节点隧道地址>。
  need_wg
  bash "$0" peer-add standby "${2:?要盒子的公钥}" "$NODE_IP"
  ;;

peer-add)
  # peer-add <名字> <公钥> <隧道地址>  —— 幂等: 同名的被替换而不是追加。
  #
  # 一个接口挂多个对端: 备用节点 (standby) 与将来每人一台的用户盒子共用 dshbox0 和同一
  # 个 UDP 端口 —— 阿里云安全组每开一个端口都要人去控制台点一次, 能不加就不加。每个
  # 对端的 AllowedIPs 是自己的 /32, 互不重叠。
  need_wg
  NAME="${2:?要名字}"; PUB="${3:?要公钥}"; IP="${4:?要隧道地址}"
  [ -s "$CONF" ] || { echo "先跑 init" >&2; exit 2; }
  case "$NAME" in *[!a-zA-Z0-9._-]*) echo "名字只能是字母数字点横线下划线" >&2; exit 2;; esac
  # standby 还要能路由到集群网段; 其余对端只有自己那一个地址。
  ALLOWED="$IP/32"
  [ "$NAME" = standby ] && ALLOWED="$IP/32, $CLUSTER_CIDR, $SERVICE_CIDR"
  python3 /usr/local/sbin/dsh-wg-peer.py "$CONF" set "$NAME" "$PUB" "$ALLOWED"
  chmod 0600 "$CONF"
  echo "对端 $NAME 已登记: ${PUB:0:16}… → $ALLOWED"
  systemctl is-active --quiet "wg-quick@$IF" && { wg syncconf "$IF" <(wg-quick strip "$IF"); echo "已热加载"; }
  ;;

peer-rm)
  NAME="${2:?要名字}"
  python3 /usr/local/sbin/dsh-wg-peer.py "$CONF" rm "$NAME"
  systemctl is-active --quiet "wg-quick@$IF" && wg syncconf "$IF" <(wg-quick strip "$IF")
  ;;

peer-ls)
  python3 /usr/local/sbin/dsh-wg-peer.py "$CONF" ls
  echo "  --- 握手 ---"
  wg show "$IF" latest-handshakes 2>/dev/null | while read -r k t; do
    if [ "${t:-0}" -gt 0 ] 2>/dev/null; then echo "  ${k:0:16}…  $(( $(date +%s) - t ))s 前"; else echo "  ${k:0:16}…  从未"; fi
  done
  ;;

up)
  need_wg; systemctl enable --now "wg-quick@$IF"; sleep 1; wg show "$IF" ;;
down)
  systemctl disable --now "wg-quick@$IF" ;;
status)
  wg show "$IF" 2>/dev/null || echo "$IF 没起来"
  echo "--- 路由 ---"; ip route show | grep -E "$CLUSTER_CIDR|$SERVICE_CIDR|$NODE_IP" || echo "(没有指向隧道的路由)"
  echo "--- 围栏 ---"; iptables -S DSH-BOX-IN 2>/dev/null | tail -3 || echo "(围栏没装)"
  ;;
remove)
  systemctl disable --now "wg-quick@$IF" 2>/dev/null || true
  /usr/local/sbin/dsh-box-wg-fence remove "$IF" 2>/dev/null || true
  rm -f "$CONF" /etc/wireguard/$IF.key /etc/wireguard/$IF.pub /usr/local/sbin/dsh-box-wg-fence
  echo "已退场 (通往 248 的 dsh-tunnel 未受影响: $(systemctl is-active dsh-tunnel 2>/dev/null))"
  ;;
*) sed -n '2,20p' "$0" >&2; exit 2 ;;
esac
