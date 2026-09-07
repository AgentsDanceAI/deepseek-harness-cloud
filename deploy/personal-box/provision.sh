#!/usr/bin/env bash
# 在一台 ascii.dev Box 上装出「一个用户的云电脑」—— 在盒子里以 root 跑。
#
#   sudo DSH_WG_SERVER_PUBKEY=... DSH_WG_ENDPOINT=<应用机IP:51820> \
#        DSH_TUNNEL_IP=10.99.1.N DSH_TUNNEL_APP_IP=10.99.1.1 \
#        REG_USER=... REG_PASS=... bash provision.sh [产品镜像...]
#
# 与备用节点那份 (deploy/box-node/provision.sh) 的关系: 隧道那一段是同一套做法, 但这里
# **不装 k3s** —— 一个用户的电脑就是一台跑 docker 的机器, 产品是盒内的容器。
#
# 装的东西 (全部可退):
#   /opt/dsh-wg/dsh0.{conf,key,pub} + dsh-wg.service   出站拨到应用机的 WireGuard
#   /home/user/dsh/<产品>/{home,workspace} + shared     用户数据 (**必须在 /home/user 下**)
#   docker 里预拉好的产品镜像
set -uo pipefail
WG_SPUB="${DSH_WG_SERVER_PUBKEY:?要应用机公钥}"
WG_ENDPOINT="${DSH_WG_ENDPOINT:?要应用机 IP:端口}"
T_SELF="${DSH_TUNNEL_IP:?要这台盒子的隧道地址}"
T_APP="${DSH_TUNNEL_APP_IP:-10.99.1.1}"
WG_IF="${DSH_WG_IF:-dsh0}"
WG_DIR=/opt/dsh-wg
USER_HOME=/home/user
rc=0; step(){ printf '\n=== %s ===\n' "$*"; }; pass(){ printf 'PASS  %s\n' "$*"; }; fail(){ printf 'FAIL  %s\n' "$*"; rc=1; }
[ "$(id -u)" = 0 ] || { echo "要 root" >&2; exit 2; }

step "1/4 隧道 (WireGuard, 出站拨到 $WG_ENDPOINT)"
command -v wg >/dev/null || { apt-get update -qq && apt-get install -y -qq wireguard-tools; }
# ⚠️ **不能放 /etc/wireguard** —— 那是 ascii.dev 自己的目录, 每次开机被它重置成只剩
# box.key + wg0.conf, 我们的配置和私钥一起消失, 而 wg-quick@ 还留在 enabled 状态。
# 放 /opt (进快照) + 自己的 unit; 私钥只在没有时才生成, 于是 resume 后公钥不变,
# 应用机那边登记的对端一直有效。
install -d -m 0700 "$WG_DIR"
[ -s "$WG_DIR/$WG_IF.key" ] || (umask 077; wg genkey > "$WG_DIR/$WG_IF.key")
wg pubkey < "$WG_DIR/$WG_IF.key" > "$WG_DIR/$WG_IF.pub"
umask 077
{ echo "[Interface]"; echo "Address = $T_SELF/32"
  printf 'PrivateKey = %s\n' "$(cat "$WG_DIR/$WG_IF.key")"
  echo; echo "[Peer]"
  echo "# 应用机。AllowedIPs 只有它一个地址 —— 这台电脑不需要经隧道去别处, 而"
  echo "# AllowedIPs 同时是「只接受这个对端发来的这些源地址」的白名单, 写宽了等于拆围栏。"
  echo "PublicKey = $WG_SPUB"; echo "Endpoint = $WG_ENDPOINT"
  echo "AllowedIPs = $T_APP/32"; echo "PersistentKeepalive = 25"; } > "$WG_DIR/$WG_IF.conf"
chmod 0600 "$WG_DIR/$WG_IF.conf"; umask 022
cat > /etc/systemd/system/dsh-wg.service <<UNIT
[Unit]
Description=DSH personal box tunnel (WireGuard $WG_IF)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/wg-quick up $WG_DIR/$WG_IF.conf
ExecStop=/usr/bin/wg-quick down $WG_DIR/$WG_IF.conf
Restart=on-failure
RestartSec=10s
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable dsh-wg >/dev/null 2>&1
systemctl restart dsh-wg 2>/dev/null || systemctl start dsh-wg
systemctl is-active --quiet dsh-wg && pass "隧道已起并 enable ($T_SELF -> $T_APP)" || fail "隧道没起来 (journalctl -u dsh-wg)"
echo "  这台盒子的公钥: $(cat "$WG_DIR/$WG_IF.pub")"

step "2/4 用户目录 (**必须在 /home/user 下** —— /data 不进快照, 实测 2026-09-07)"
install -d -o user -g user "$USER_HOME/dsh" "$USER_HOME/dsh/shared"
pass "已建 $USER_HOME/dsh 与 shared"

step "3/4 预拉产品镜像"
if [ -n "${REG_USER:-}" ] && [ -n "${REG_PASS:-}" ]; then
  echo "$REG_PASS" | docker login ghcr.io -u "$REG_USER" --password-stdin >/dev/null 2>&1 \
    && pass "registry 已登录" || fail "registry 登录失败"
fi
n=0
for img in "$@"; do
  if docker image inspect "$img" >/dev/null 2>&1; then echo "  已有: $img"; n=$((n+1)); continue; fi
  if docker pull -q "$img" >/dev/null 2>&1; then echo "  拉好: $img"; n=$((n+1)); else fail "拉不动: $img"; fi
done
[ $# -eq 0 ] || { [ $n -eq $# ] && pass "$n/$# 个镜像就位" || fail "只拿到 $n/$# 个"; }
# 登录凭据不留在盘上 —— 它进快照, 而快照是可以被导出的。
rm -f /root/.docker/config.json

step "4/4 自检"
ip -br addr show "$WG_IF" 2>/dev/null | grep -q "$T_SELF" && pass "隧道地址已配上" || fail "隧道地址没配上"
df -h /home | tail -1 | awk '{print "  盘: 已用 "$3" / 共 "$2" (剩 "$4")"}'
echo
[ $rc -eq 0 ] && echo "全绿。" || echo "有未通过项。"
exit $rc
