#!/usr/bin/env bash
# 备用节点 (Box) 的隧道 —— 客户端一侧, 在**应用机**上以 root 跑。
#
# 与 ../k8s-node/tunnel-client-app.sh (通往 248 的那条) 并存, 名字全部带 -box, 地址段
# 也另起一段 —— 目的就是能在**不停 248** 的前提下把备用节点拉起来对着打一遍。两条
# 隧道同时在的时候: 248 是 10.99.0.x + 路由 10.42/16 10.43/16, Box 是 10.99.1.x +
# 路由 10.44/16 10.45/16, 互不相干。
#
#   DSH_BOX_IP=<box 公网 IP> bash tunnel-client-box.sh install
#   bash tunnel-client-box.sh remove
#
# ⚠️ **这条 ssh -w 隧道只在 Box 落到 hetzner 的 VM 上时能用。** 2026-09-06 实测: 同一台
# Box resume 之后落到 baremetal, sshd 就开不出 tun 了 (-w any:0 和 -w any:any 都是
# channel 0: open failed), 而落在哪不由我们定。落点会变 = 这条传输靠不住 —— 正式方案
# 见 README 的「未决: 隧道怎么走」(WireGuard 或 sshuttle), 定了就把这个文件换掉,
# activate.sh 其余六步不动。
#
# 端口默认 22 (ascii.dev 只放行 22), baremetal 落点要用 sshEndpoint 给的高端口 —— 由
# activate.sh 经 DSH_BOX_PORT 传进来。
#
# ⚠️ 主机密钥每次 resume 都会变 —— Box 的快照不含机器身份 (hostname / ssh host key)。
# 所以这里**不预置 known_hosts**, 由 activate.sh 在每次激活时从 ascii.dev 的 API
# 通道 (HTTPS + 密钥认证) 取回指纹再钉。那是比 TOFU 更硬的信任根, 别改回 TOFU。
set -euo pipefail

UNIT=/etc/systemd/system/dsh-tunnel-box.service
HOOK=/usr/local/sbin/dsh-tunnel-box-local-up
KNOWN=/root/.ssh/known_hosts_dsh_box
LOCAL="${DSH_BOX_TUNNEL_APP_IP:-10.99.1.1}"
PEER="${DSH_BOX_TUNNEL_NODE_IP:-10.99.1.2}"
CLUSTER_CIDR="${DSH_BOX_CLUSTER_CIDR:-10.44.0.0/16}"
SERVICE_CIDR="${DSH_BOX_SERVICE_CIDR:-10.45.0.0/16}"

case "${1:-}" in
install)
  R="${DSH_BOX_IP:?要 DSH_BOX_IP (每次 resume 都会变, 用 box.sh ssh 现取)}"
  PORT="${DSH_BOX_PORT:-22}"
  install -m 0755 /dev/stdin "$HOOK" <<EOF
#!/bin/bash
# ssh 在隧道通道建立后调用 (LocalCommand "%T"), \$1 是本地 tun 设备。
DEV="\${1:-}"; LOCAL=$LOCAL; PEER=$PEER
[ -n "\$DEV" ] && [ "\$DEV" != NONE ] || { echo "dsh-tunnel-box: 没拿到 tun 设备" >&2; exit 1; }
ip addr flush dev "\$DEV" 2>/dev/null
ip addr add "\$LOCAL" peer "\$PEER/32" dev "\$DEV"
ip link set "\$DEV" up
ip route replace $CLUSTER_CIDR dev "\$DEV"   # Box 节点的 pod 网段
ip route replace $SERVICE_CIDR dev "\$DEV"   # Box 节点的 service 网段
logger -t dsh-tunnel-box "\$DEV up: \$LOCAL <-> \$PEER"
EOF
  install -m 0644 /dev/stdin "$UNIT" <<EOF
[Unit]
Description=DSH Cloud L3 tunnel to the standby Box node (ssh :22)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \\
  -o ServerAliveCountMax=3 -o ConnectTimeout=15 -o StrictHostKeyChecking=yes \\
  -o UserKnownHostsFile=$KNOWN -o PermitLocalCommand=yes -o LocalCommand="$HOOK %%T" \\
  -o Tunnel=point-to-point -w any:0 -p $PORT -i /root/.ssh/id_ed25519 root@$R dsh-tunnel-box
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  echo "已装。known_hosts ($KNOWN) 由 activate.sh 写, 写好再 systemctl enable --now dsh-tunnel-box"
  ;;
remove)
  systemctl disable --now dsh-tunnel-box 2>/dev/null || true
  rm -f "$UNIT" "$HOOK" "$KNOWN"
  systemctl daemon-reload
  echo "已退场 (通往 248 的 dsh-tunnel 未受影响: $(systemctl is-active dsh-tunnel))"
  ;;
*) sed -n '2,18p' "$0" >&2; exit 2 ;;
esac
