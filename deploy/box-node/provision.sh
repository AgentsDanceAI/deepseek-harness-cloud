#!/usr/bin/env bash
# 在一台 ascii.dev Box 上装出一个**和 248 等价的**工作台节点 (备用节点)。
# 在盒子里以 root 跑:
#
#   tar -cz deploy/k8s-node deploy/box-node | ssh user@<box ip> 'tar -xz -C /tmp'
#   ssh user@<box ip> "sudo DSH_APP_IP=... DSH_TUNNEL_NODE_IP=... DSH_TUNNEL_APP_IP=... \
#                      bash /tmp/deploy/box-node/provision.sh '<应用机 root 公钥>'"
#
# 地址一律从环境变量来, 不进 git (与 ../k8s-node/ 同规矩)。装完打印应用机要的
# token / ca.crt / .env 片段。可重复跑 —— 每一步都先看现状再动手。
#
# 装的东西 (全部可退, 见 README 的「退场」):
#   /etc/ssh/sshd_config.d/60-dsh-tunnel.conf   root 只能从应用机来、只能开隧道
#   /root/.ssh/authorized_keys                  应用机的 key, 带 restrict + 强制命令
#   /usr/local/sbin/dsh-tunnel-up               强制命令: 配 tun0, 重放防火墙, 守着
#   /etc/rancher/k3s/config.yaml + k3s          数据在 /opt/dsh-k3s (**必须**, 见 k3s-config.yaml)
#   /usr/local/bin/runsc, containerd-shim-runsc-v1, /etc/containerd/runsc.toml
set -uo pipefail

PUB="${1:-}"; [ -n "$PUB" ] || read -r PUB || true
case "$PUB" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-*) ;; *) echo "要应用机的 root 公钥作参数或 stdin" >&2; exit 2;; esac
APP_IP="${DSH_APP_IP:?要 DSH_APP_IP (应用机公网 IP, 用来把 root 登录锁死在它身上)}"
T_NODE="${DSH_TUNNEL_NODE_IP:?要 DSH_TUNNEL_NODE_IP}"
T_APP="${DSH_TUNNEL_APP_IP:?要 DSH_TUNNEL_APP_IP}"
CLUSTER_CIDR="${DSH_CLUSTER_CIDR:-10.44.0.0/16}"
SERVICE_CIDR="${DSH_SERVICE_CIDR:-10.45.0.0/16}"
CLUSTER_DNS="${DSH_CLUSTER_DNS:-10.45.0.10}"
NODE_NAME="${DSH_NODE_NAME:-dsh-box-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
K8SN="$(cd "$HERE/../k8s-node" && pwd)"   # 复用 248 那套 manifests / netpol / gvisor / 防火墙
[ -r "$K8SN/manifests.yaml" ] || { echo "找不到 $K8SN/manifests.yaml —— 两个目录要一起传上来" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "要 root (sudo bash $0 ...)" >&2; exit 2; }

rc=0
step() { printf '\n=== %s ===\n' "$*"; }
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; rc=1; }

# ── 1. 隧道入口 ───────────────────────────────────────────────────────────────
# 248 上是**另起一个 sshd** 挂在非标准端口 (那台机的 22 是公司的, 碰不得)。Box 上
# 反过来: ascii.dev 的防火墙只放行 22 (实测 09-06: 22 通, 自己起的 50005 被挡, 别的
# 端口一律不通), 所以只能走 22, 而 22 上跑的是盒子自带的 sshd。于是改成往它加一个
# drop-in, 且只对 "root 且来自应用机" 生效 —— 盒子自己的 user 账号 (ascii.dev 的
# 命令通道、桌面流都靠它) 一个字节都不动。
step "1/6 隧道入口 (sshd drop-in, 只放应用机的 root)"
install -d -m 0700 /root/.ssh
printf 'restrict,tunnel="0",command="/usr/local/sbin/dsh-tunnel-up" %s\n' "$PUB" > /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
# ⚠️ Ubuntu 的 sshd_config 在**开头**就 Include 这个目录, 所以 drop-in 里一个没关的
# Match 会把主配置剩下的全部吞进去。最后那行 `Match all` 是必须的。
cat > /etc/ssh/sshd_config.d/60-dsh-tunnel.conf <<EOF
# DSH Cloud 备用节点的隧道入口。只给 root、只给应用机、只给开隧道。
Match User root Address $APP_IP
  PermitRootLogin prohibit-password
  PermitTunnel point-to-point
  PasswordAuthentication no
  AllowTcpForwarding no
  AllowAgentForwarding no
  X11Forwarding no
  PermitTTY no
Match all
EOF
chmod 0644 /etc/ssh/sshd_config.d/60-dsh-tunnel.conf
if sshd -t; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  pass "sshd 配置合法并已 reload"
else
  fail "sshd -t 不过 —— 上面有详情; 已写的 drop-in 先删掉再排查"
fi

install -m 0755 /dev/stdin /usr/local/sbin/dsh-tunnel-up <<EOF
#!/bin/bash
# 隧道 key 的强制命令。sshd 已经为这次会话建好 tun0, 这里给它地址, 重放防火墙,
# 然后守着 —— 设备消失 (会话结束) 就退出。
DEV=tun0; LOCAL=$T_NODE; PEER=$T_APP
for _ in \$(seq 1 50); do ip link show "\$DEV" >/dev/null 2>&1 && break; sleep 0.1; done
ip link show "\$DEV" >/dev/null 2>&1 || { echo "dsh-tunnel-up: \$DEV 一直没出现" >&2; exit 1; }
ip addr flush dev "\$DEV" 2>/dev/null
ip addr add "\$LOCAL" peer "\$PEER/32" dev "\$DEV"
ip link set "\$DEV" up
[ -x /usr/local/sbin/dsh-tunnel-firewall ] && /usr/local/sbin/dsh-tunnel-firewall apply "$T_APP" >/dev/null 2>&1
logger -t dsh-tunnel "\$DEV up: \$LOCAL <-> \$PEER (from \${SSH_CLIENT%% *})"
while ip link show "\$DEV" >/dev/null 2>&1; do sleep 5; done
EOF
pass "dsh-tunnel-up 已装"

# ── 2. k3s ───────────────────────────────────────────────────────────────────
step "2/6 k3s (数据落 /opt/dsh-k3s —— /var 不进 Box 快照)"
CPUS="$(nproc)"; MEM_MB="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
# 独占机器, 只给系统留住它自己要的: 1 核 + 1.5G, 再按机器大小放宽一点。
RES_CPU="1"; RES_MEM="$(( MEM_MB > 24000 ? 3072 : 1536 ))Mi"
MAX_PODS="$(( CPUS * 10 ))"; [ "$MAX_PODS" -gt 60 ] && MAX_PODS=60
mkdir -p /etc/rancher/k3s
sed -e "s|__NODE_NAME__|$NODE_NAME|" -e "s|__TUNNEL_NODE_IP__|$T_NODE|" \
    -e "s|__CLUSTER_CIDR__|$CLUSTER_CIDR|" -e "s|__SERVICE_CIDR__|$SERVICE_CIDR|" \
    -e "s|__CLUSTER_DNS__|$CLUSTER_DNS|" -e "s|__RESERVE_CPU__|$RES_CPU|" \
    -e "s|__RESERVE_MEM__|$RES_MEM|" -e "s|__MAX_PODS__|$MAX_PODS|" \
    "$HERE/k3s-config.yaml" > /etc/rancher/k3s/config.yaml
grep -qE "__[A-Z_]+__" /etc/rancher/k3s/config.yaml && fail "config.yaml 里还有没换掉的占位符" || pass "config.yaml 已渲染 (${CPUS}C/${MEM_MB}M, 留 ${RES_CPU}C/${RES_MEM}, max-pods $MAX_PODS)"

if systemctl is-active --quiet k3s; then
  pass "k3s 已在跑, 跳过安装 (改了 config 要 systemctl restart k3s)"
else
  curl -sfL https://get.k3s.io -o /tmp/k3s-install.sh || fail "下载 k3s 安装脚本失败"
  INSTALL_K3S_CHANNEL=stable INSTALL_K3S_SYMLINK=skip sh /tmp/k3s-install.sh server \
    && pass "k3s 装好" || fail "k3s 安装失败 (journalctl -u k3s)"
fi
# /readyz 是 apiserver 的就绪, 节点自己变 Ready 还要几秒 —— 头一版只等前者,
# 于是紧接着的判定必红 (而后面每一步都好好的, 说明它红得没道理)。等到位再判。
wait_node_ready() {
  for _ in $(seq 1 80); do
    k3s kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}' | grep -qx Ready && return 0
    sleep 3
  done
  return 1
}
wait_node_ready && pass "节点 Ready" || fail "节点没到 Ready (journalctl -u k3s)"

# resume 之后 k3s 起不来的自愈闸 —— 2026-09-06 停机/开机实测踩到:
# k3s 是自解压的, 头一次跑把二进制解到 <data-dir>/data/<sha>/bin/。Box 的快照把这个
# **目录留下了、内容没留** (集群数据 agent/ server/ 都好好的, 173M 的 containerd 镜像
# 也在, 唯独这一坨解出来的二进制没了)。而 k3s 只看目录在不在就决定要不要解包 ——
# 目录在、里面空 → 直接 `exec: "k3s-server": executable file not found`, 每 5 秒重启
# 一次, 无限循环, 日志里只有这一行。手工 rm 掉那个目录再起就好了, 但备用节点的意义
# 就是不要人手救, 所以做成开机自查。
install -m 0755 /dev/stdin /usr/local/sbin/dsh-k3s-prestart <<'EOF'
#!/bin/sh
# k3s 起来之前: 解包目录在但 k3s-server 不在 = 空壳, 清掉让 k3s 重新解 (几秒钟)。
D=/opt/dsh-k3s/data
[ -x "$D/current/bin/k3s-server" ] || rm -rf "$D"/[0-9a-f]* "$D"/current
exit 0
EOF
install -d -m 0755 /etc/systemd/system/k3s.service.d
cat > /etc/systemd/system/k3s.service.d/10-dsh-box-resume.conf <<'EOF'
[Service]
ExecStartPre=/usr/local/sbin/dsh-k3s-prestart
EOF
systemctl daemon-reload
[ -x /usr/local/sbin/dsh-k3s-prestart ] && pass "resume 自愈闸已装 (k3s 解包目录被快照掏空时自动重解)" \
  || fail "自愈闸没装上"

# ── 3. gVisor ────────────────────────────────────────────────────────────────
step "3/6 gVisor (runsc)"
if [ -x /usr/local/bin/runsc ]; then
  pass "runsc 已在 ($(/usr/local/bin/runsc --version 2>/dev/null | head -1))"
else
  ( cd /tmp && rm -f runsc* containerd-shim-runsc-v1* \
    && URL=https://storage.googleapis.com/gvisor/releases/release/latest/x86_64 \
    && for f in runsc containerd-shim-runsc-v1; do curl -sfL -o "$f" "$URL/$f" && curl -sfL -o "$f.sha512" "$URL/$f.sha512"; done \
    && sha512sum -c ./*.sha512 && install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/ ) \
    && pass "runsc 装好 (校验和已核)" || fail "runsc 下载/校验失败"
fi
install -D -m 0644 "$K8SN/runsc.toml" /etc/containerd/runsc.toml
install -D -m 0644 "$K8SN/containerd-config-v3.toml.tmpl" /opt/dsh-k3s/agent/etc/containerd/config-v3.toml.tmpl
systemctl restart k3s
wait_node_ready || fail "重启 k3s 后节点没回到 Ready"
grep -qc runsc /opt/dsh-k3s/agent/etc/containerd/config.toml 2>/dev/null \
  && pass "containerd 里有 runsc 运行时" || fail "containerd 配置里没看到 runsc"

# ── 4. 集群里的东西 ──────────────────────────────────────────────────────────
step "4/6 命名空间 / 配额 / PVC / 服务账号 / 围栏 / 准入策略"
# 配额贴着这台机能给 Pod 的量放 (总内存减去 system-reserved), 不照抄 248 的 56Gi。
POD_MEM_MI=$(( MEM_MB - ${RES_MEM%Mi} - 512 )); POD_CPU=$(( CPUS - 1 ))
python3 - "$K8SN/manifests.yaml" "$CPUS" "$POD_MEM_MI" "$MAX_PODS" > /tmp/dsh-manifests.yaml <<'PY'
import re, sys
src, cpus, mem_mi, pods = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
y = open(src).read()
y = re.sub(r'(requests\.cpu:\s*)"[^"]*"', r'\g<1>"%d"' % max(1, cpus - 1), y)
y = re.sub(r'(limits\.cpu:\s*)"[^"]*"', r'\g<1>"%d"' % (cpus * 4), y)
y = re.sub(r'(requests\.memory:\s*)\S+', r'\g<1>%dMi' % mem_mi, y)
y = re.sub(r'(limits\.memory:\s*)\S+', r'\g<1>%dMi' % mem_mi, y)
y = re.sub(r'(\n\s+pods:\s*)"[^"]*"', r'\g<1>"%d"' % pods, y)
sys.stdout.write(y)
PY
k3s kubectl apply -f /tmp/dsh-manifests.yaml >/dev/null && pass "manifests 已 apply (配额 ${POD_CPU}C/${POD_MEM_MI}Mi/${MAX_PODS} pods)" || fail "manifests apply 失败"
# netpol 里的 DNS 地址跟着 service-cidr 走 —— 248 是 10.43.0.10, 这里换了段。
sed -e "s|\${DSH_TUNNEL_APP_IP}|$T_APP|g" -e "s|10\.43\.0\.10/32|$CLUSTER_DNS/32|g" \
    -e "s|10\.42\.0\.0/16|$CLUSTER_CIDR|g" -e "s|10\.43\.0\.0/16|$SERVICE_CIDR|g" \
    "$K8SN/netpol.yaml" > /tmp/dsh-netpol.yaml
k3s kubectl apply -f /tmp/dsh-netpol.yaml >/dev/null && pass "网络围栏已 apply" || fail "netpol apply 失败"
k3s kubectl apply -f "$K8SN/runtimeclass-gvisor.yaml" >/dev/null && pass "RuntimeClass gvisor 已 apply" || fail "runtimeclass apply 失败"
k3s kubectl apply -f "$K8SN/admission-gvisor.yaml" >/dev/null && pass "准入策略已 apply" || fail "准入策略 apply 失败"

# ── 5. 隧道对端防火墙 ────────────────────────────────────────────────────────
step "5/6 隧道对端防火墙"
sed -e "s|10\.42\.0\.0/16|$CLUSTER_CIDR|g" -e "s|10\.43\.0\.0/16|$SERVICE_CIDR|g" \
    "$K8SN/tunnel-firewall.sh" > /tmp/dsh-tunnel-firewall.sh
DSH_TUNNEL_APP_IP="$T_APP" bash /tmp/dsh-tunnel-firewall.sh install "$T_APP" >/dev/null 2>&1 \
  && pass "防火墙已装 (每次隧道建立时由 dsh-tunnel-up 重放)" || fail "防火墙安装失败"

# ── 6. 应用机要的东西 ────────────────────────────────────────────────────────
step "6/6 应用机那边要贴的"
TOKEN="$(k3s kubectl -n dsh get secret dhc-server-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d)"
CA="$(k3s kubectl -n dsh get secret dhc-server-token -o jsonpath='{.data.ca\.crt}' 2>/dev/null)"
if [ -n "$TOKEN" ] && [ -n "$CA" ]; then
  umask 077; printf '%s' "$TOKEN" > /root/dsh-k8s-token; printf '%s' "$CA" > /root/dsh-k8s-ca.b64
  pass "token 与 ca.crt 已落在盒子的 /root/dsh-k8s-token 与 /root/dsh-k8s-ca.b64"
  echo "  取走 (在应用机上跑):"
  echo "    ssh user@<box ip> sudo cat /root/dsh-k8s-token > /root/dsh-k8s-box/token"
  echo "    ssh user@<box ip> sudo cat /root/dsh-k8s-ca.b64 | base64 -d > /root/dsh-k8s-box/ca.crt"
else
  fail "读不到服务账号 token —— 看 k3s kubectl -n dsh get secret dhc-server-token"
fi
echo
echo "  .env 片段 (激活时才换上去, 演练不要动线上这几行):"
echo "    K8S_API_URL=https://$T_NODE:6443"
echo "    WORK_PROXY_CIDR=$T_APP/32"
echo
[ $rc -eq 0 ] && echo "全绿。" || echo "有未通过项 —— 上面 FAIL 的那几行。"
exit $rc
