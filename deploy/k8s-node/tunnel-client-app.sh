#!/bin/bash
# DSH Cloud tunnel — client side, runs ON the prod host as root.
# Installs:
#   /usr/local/sbin/dsh-tunnel-local-up    LocalCommand hook: addresses the tun device, adds k3s routes
#   /etc/systemd/system/dsh-tunnel.service ssh -w tunnel to <node>:<TUNNEL_PORT>, auto-restart
# and pins the node's host key for port <TUNNEL_PORT> in /root/.ssh/known_hosts (must equal the port-22 key).
# Usage:  bash tunnel-client-app.sh <node public IP>      (or DSH_NODE_IP=... in the environment)
# Uninstall: systemctl disable --now dsh-tunnel; rm -f the two files above.
set -euo pipefail
R="${1:-${DSH_NODE_IP:-}}"; P=<TUNNEL_PORT>
[ -n "$R" ] || { echo "usage: $0 <node public IP>" >&2; exit 2; }

install -m 0755 /dev/stdin /usr/local/sbin/dsh-tunnel-local-up <<'EOF'
#!/bin/bash
# Called by ssh (LocalCommand "%T") once the tunnel channel is open; $1 is the local tun device.
DEV="${1:-}"; LOCAL=<TUNNEL_APP_IP>; PEER=<TUNNEL_NODE_IP>
[ -n "$DEV" ] && [ "$DEV" != NONE ] || { echo "dsh-tunnel: no tun device" >&2; exit 1; }
ip addr flush dev "$DEV" 2>/dev/null
ip addr add "$LOCAL" peer "$PEER/32" dev "$DEV"
ip link set "$DEV" up
ip route replace 10.42.0.0/16 dev "$DEV"   # k3s pod CIDR on the node
ip route replace 10.43.0.0/16 dev "$DEV"   # k3s service CIDR on the node
logger -t dsh-tunnel "$DEV up: $LOCAL <-> $PEER, routes 10.42/16 10.43/16 via $DEV"
EOF

install -m 0644 /dev/stdin /etc/systemd/system/dsh-tunnel.service <<EOF
[Unit]
Description=DSH Cloud L3 tunnel to the k3s node over ssh :$P
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=15 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/.ssh/known_hosts -o PermitLocalCommand=yes -o LocalCommand="/usr/local/sbin/dsh-tunnel-local-up %%T" -o Tunnel=point-to-point -w any:0 -p $P -i /root/.ssh/id_ed25519 root@$R dsh-tunnel
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

# Pin the host key for [R]:P — it must be the same ed25519 key already trusted on port 22.
K22=$(ssh-keyscan -t ed25519 -p 22 $R 2>/dev/null | awk '{print $3}')
K50=$(ssh-keyscan -t ed25519 -p $P $R 2>/dev/null | awk '{print $3}')
[ -n "$K50" ] || { echo "no host key on $R:$P — is sshd-dsh-tunnel running on the node?" >&2; exit 3; }
[ "$K22" = "$K50" ] || { echo "host key on :$P differs from :22 — refusing to pin" >&2; exit 4; }
ssh-keygen -F "[$R]:$P" -f /root/.ssh/known_hosts >/dev/null || echo "[$R]:$P ssh-ed25519 $K50" >> /root/.ssh/known_hosts
echo "host key pinned for [$R]:$P"

systemctl daemon-reload
systemctl enable --now dsh-tunnel.service
for _ in $(seq 1 20); do ip -4 addr show to <TUNNEL_APP_IP> 2>/dev/null | grep -q <TUNNEL_APP_IP> && break; sleep 0.5; done
systemctl --no-pager --lines=8 status dsh-tunnel.service || true
ip -br addr | grep -E "^tun" || echo "no tun device yet"
ip route | grep -E "^10\.4[23]\." || true
ping -c 3 -W 2 <TUNNEL_NODE_IP> && echo "TUNNEL OK: <TUNNEL_APP_IP> <-> <TUNNEL_NODE_IP>"
