#!/bin/bash
# DSH Cloud tunnel — server side, runs ON the k3s node as root.
# Installs a SEPARATE sshd instance on the tunnel port (the main sshd on port 22 is not touched):
#   /etc/ssh/sshd_tunnel_config            key-only, single key, tun-only, no shell/forwarding
#   /etc/ssh/dsh_tunnel_authorized_keys    the prod host's key with restrict + forced command
#   /usr/local/sbin/dsh-tunnel-up          forced command: configures tun0, lives while it exists
#   /etc/systemd/system/sshd-dsh-tunnel.service
# Port and tunnel addresses are NOT in git (they live in the prod host's deploy/prod/.env):
#   DSH_TUNNEL_PORT=<port>  DSH_TUNNEL_NODE_IP=<node tunnel ip>  DSH_TUNNEL_APP_IP=<prod host tunnel ip>
# Usage:  DSH_TUNNEL_PORT=... DSH_TUNNEL_NODE_IP=... DSH_TUNNEL_APP_IP=... bash tunnel-sshd-node.sh 'ssh-ed25519 AAAA... prod-host-key'
#    or:  echo 'ssh-ed25519 AAAA...' | DSH_TUNNEL_PORT=... bash tunnel-sshd-node.sh
# Uninstall: systemctl disable --now sshd-dsh-tunnel; rm -f the four files above.
set -euo pipefail
PUB="${1:-}"; [ -n "$PUB" ] || read -r PUB
case "$PUB" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-*) ;; *) echo "need the prod host public key as argument or stdin" >&2; exit 2;; esac
P="${2:-${DSH_TUNNEL_PORT:-}}"; LOCAL="${DSH_TUNNEL_NODE_IP:-}"; PEER="${DSH_TUNNEL_APP_IP:-}"
[ -n "$P" ] && [ -n "$LOCAL" ] && [ -n "$PEER" ] || { echo "need DSH_TUNNEL_PORT, DSH_TUNNEL_NODE_IP, DSH_TUNNEL_APP_IP in the environment" >&2; exit 2; }

if ss -tln "sport = :$P" | grep -q ":$P"; then echo "port $P already in use, aborting" >&2; ss -tlnp "sport = :$P"; exit 3; fi

install -m 0644 /dev/stdin /etc/ssh/sshd_tunnel_config <<'EOF'
# DSH Cloud tunnel sshd — separate instance from the main sshd (port 22), which it never touches.
# Purpose: one ssh -w layer-3 tunnel from the DSH prod host to the k3s node here.
# Key-only, a single authorized key (/etc/ssh/dsh_tunnel_authorized_keys), tun only, no shell/forwarding.
Port __PORT__
ListenAddress 0.0.0.0:__PORT__
PidFile /run/sshd-dsh-tunnel.pid
HostKey /etc/ssh/ssh_host_ed25519_key
AuthorizedKeysFile /etc/ssh/dsh_tunnel_authorized_keys
AllowUsers root
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
ChallengeResponseAuthentication no
UsePAM yes
PermitTunnel point-to-point
AllowTcpForwarding no
GatewayPorts no
X11Forwarding no
AllowAgentForwarding no
PermitTTY no
PermitUserRC no
MaxSessions 2
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 30
ClientAliveCountMax 3
LogLevel VERBOSE
EOF
sed -i "s/__PORT__/$P/g" /etc/ssh/sshd_tunnel_config

printf 'restrict,tunnel="0",command="/usr/local/sbin/dsh-tunnel-up" %s\n' "$PUB" > /etc/ssh/dsh_tunnel_authorized_keys
chmod 0600 /etc/ssh/dsh_tunnel_authorized_keys

install -m 0755 /dev/stdin /usr/local/sbin/dsh-tunnel-up <<'EOF'
#!/bin/bash
# Forced command for the DSH tunnel key (see /etc/ssh/sshd_tunnel_config).
# sshd has already created tun0 for this session; give it its address and stay alive
# while the device exists (sshd removes tun0 when the tunnel session ends, which ends this loop).
DEV=tun0; LOCAL=__NODE_IP__; PEER=__APP_IP__
for _ in $(seq 1 50); do ip link show "$DEV" >/dev/null 2>&1 && break; sleep 0.1; done
ip link show "$DEV" >/dev/null 2>&1 || { echo "dsh-tunnel-up: $DEV never appeared" >&2; exit 1; }
ip addr flush dev "$DEV" 2>/dev/null
ip addr add "$LOCAL" peer "$PEER/32" dev "$DEV"
ip link set "$DEV" up
logger -t dsh-tunnel "$DEV up: $LOCAL <-> $PEER (session from ${SSH_CLIENT%% *})"
while ip link show "$DEV" >/dev/null 2>&1; do sleep 5; done
logger -t dsh-tunnel "$DEV gone, session ending"
EOF
sed -i "s|__NODE_IP__|$LOCAL|; s|__APP_IP__|$PEER|" /usr/local/sbin/dsh-tunnel-up

install -m 0644 /dev/stdin /etc/systemd/system/sshd-dsh-tunnel.service <<'EOF'
[Unit]
Description=DSH Cloud tunnel sshd (tun-only, separate from sshd.service)
Documentation=file:/etc/ssh/sshd_tunnel_config
After=network.target

[Service]
Type=simple
ExecStartPre=/usr/sbin/sshd -t -f /etc/ssh/sshd_tunnel_config
ExecStart=/usr/sbin/sshd -D -e -f /etc/ssh/sshd_tunnel_config
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

/usr/sbin/sshd -t -f /etc/ssh/sshd_tunnel_config && echo "config OK"
systemctl daemon-reload
systemctl enable --now sshd-dsh-tunnel.service
sleep 1
systemctl --no-pager --lines=5 status sshd-dsh-tunnel.service || true
ss -tlnp "sport = :$P"
echo "main sshd untouched: $(systemctl is-active sshd) (pid $(systemctl show sshd -p MainPID --value))"
