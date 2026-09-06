#!/usr/bin/env bash
# 在一台 ascii.dev Box 上装出一个**和 248 等价的**工作台节点 (备用节点)。
# 在盒子里以 root 跑:
#
#   # 先在应用机上 `bash tunnel-wg-144.sh init`, 拿它打印的公钥
#   tar -cz deploy/k8s-node deploy/box-node | ssh user@<box> 'tar -xz -C /tmp'
#   ssh user@<box> "sudo DSH_WG_SERVER_PUBKEY=<应用机公钥> DSH_WG_ENDPOINT=<应用机IP:51820> \
#                   DSH_TUNNEL_NODE_IP=10.99.1.2 DSH_TUNNEL_APP_IP=10.99.1.1 \
#                   bash /tmp/deploy/box-node/provision.sh"
#   # 装完它会打印盒子的公钥, 回应用机 `bash tunnel-wg-144.sh peer <盒子公钥>`
#
# 地址一律从环境变量来, 不进 git (与 ../k8s-node/ 同规矩)。装完打印应用机要的
# token / ca.crt / .env 片段。可重复跑 —— 每一步都先看现状再动手。
#
# 装的东西 (全部可退, 见 README 的「退场」):
#   /etc/wireguard/dsh0.conf + wg-quick@dsh0    出站拨到应用机的 WireGuard (systemd 使能
#                                               → resume 后自动回来; /etc 进快照 → 密钥不变)
#   /usr/local/sbin/dsh-tunnel-firewall         隧道这一侧的围栏, 由 wg-quick 的 PostUp 挂上
#   /etc/rancher/k3s/config.yaml + k3s          数据在 /opt/dsh-k3s (**必须**, 见 k3s-config.yaml)
#   /usr/local/bin/runsc, containerd-shim-runsc-v1, /etc/containerd/runsc.toml
set -uo pipefail

WG_SPUB="${DSH_WG_SERVER_PUBKEY:?要 DSH_WG_SERVER_PUBKEY (应用机上 tunnel-wg-144.sh init 打印的那个)}"
WG_ENDPOINT="${DSH_WG_ENDPOINT:?要 DSH_WG_ENDPOINT (应用机公网IP:UDP端口, 例如 1.2.3.4:51820)}"
WG_IF="${DSH_WG_IF:-dsh0}"
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

# ── 1. 隧道 (WireGuard) ──────────────────────────────────────────────────────
# 为什么不是 248 那种 ssh -w: Box 每次 resume 都可能换到另一台机器, 落到 baremetal 时
# sshd 就开不出 tun 了 (2026-09-06 实测, -w any:any 也不行), 而落点不由我们定。改成
# **盒子出站**拨 WireGuard: 盒子地址怎么变都无所谓, 应用机只认公钥; 而且出站永远通,
# 不像入站那样只剩一个 22。
#
# 密钥放 /etc/wireguard (进快照) 且**只在没有时才生成** —— 重跑本脚本不换密钥, 否则
# 应用机那边登记的对端当场失效, 而症状是"隧道就是不通", 没有任何一处会说是为什么。
step "1/6 隧道 (WireGuard: 盒子 → 应用机 $WG_ENDPOINT)"
# 先清掉 ssh 隧道那一版留下的东西 —— 换了传输就不该再让一台公网机器开着 root 登录。
# (2026-09-06 上午先按 ssh -w 做过一版, 落到 baremetal 时 sshd 开不出 tun 才改的 wg。)
if [ -f /etc/ssh/sshd_config.d/60-dsh-tunnel.conf ] || [ -f /usr/local/sbin/dsh-tunnel-up ]; then
  rm -f /etc/ssh/sshd_config.d/60-dsh-tunnel.conf /usr/local/sbin/dsh-tunnel-up /root/.ssh/authorized_keys
  sshd -t && { systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true; }
  pass "ssh 隧道那一版的残留已清 (root 登录关回去了)"
fi
command -v wg >/dev/null || { apt-get update -qq && apt-get install -y -qq wireguard-tools; }
install -d -m 0700 /etc/wireguard
[ -s /etc/wireguard/$WG_IF.key ] || (umask 077; wg genkey > /etc/wireguard/$WG_IF.key)
wg pubkey < /etc/wireguard/$WG_IF.key > /etc/wireguard/$WG_IF.pub

# 隧道这一侧的围栏先装好 —— 它是下面 wg-quick 的 PostUp, 顺序反了第一次起不来。
sed -e "s|10\.42\.0\.0/16|$CLUSTER_CIDR|g" -e "s|10\.43\.0\.0/16|$SERVICE_CIDR|g" \
    -e "s|^DEV=tun0|DEV=$WG_IF|" "$K8SN/tunnel-firewall.sh" > /tmp/dsh-tunnel-firewall.sh
DSH_TUNNEL_APP_IP="$T_APP" bash /tmp/dsh-tunnel-firewall.sh install "$T_APP" >/dev/null 2>&1 \
  && pass "节点侧围栏已装 (只放行到 6443 与转发到 $CLUSTER_CIDR/$SERVICE_CIDR)" || fail "围栏安装失败"

umask 077
cat > /etc/wireguard/$WG_IF.conf <<EOF
[Interface]
Address = $T_NODE/32
PostUp = /usr/local/sbin/dsh-tunnel-firewall apply $T_APP
PostDown = /usr/local/sbin/dsh-tunnel-firewall remove
EOF
printf 'PrivateKey = %s\n' "$(cat /etc/wireguard/$WG_IF.key)" >> /etc/wireguard/$WG_IF.conf
cat >> /etc/wireguard/$WG_IF.conf <<EOF

[Peer]
# 应用机。AllowedIPs 只有它的隧道地址一个 —— 节点不需要经隧道去别处, 而 AllowedIPs
# 同时是"只接受这个对端发来的这些源地址"的白名单, 写宽了等于把围栏拆了。
PublicKey = $WG_SPUB
Endpoint = $WG_ENDPOINT
AllowedIPs = $T_APP/32
# 盒子多半在 NAT 后面 (baremetal 落点是私网 IPv4), 靠它把回程路径撑着。
PersistentKeepalive = 25
EOF
chmod 0600 /etc/wireguard/$WG_IF.conf
umask 022
systemctl enable --now "wg-quick@$WG_IF" >/dev/null 2>&1 || systemctl restart "wg-quick@$WG_IF"
systemctl is-active --quiet "wg-quick@$WG_IF" && pass "wg-quick@$WG_IF 已起并 enable (resume 后自动回来)" \
  || fail "wg-quick@$WG_IF 没起来 (journalctl -u wg-quick@$WG_IF)"

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

# ── 5. 隧道自检 ─────────────────────────────────────────────────────────────
step "5/6 隧道自检"
if wg show "$WG_IF" >/dev/null 2>&1; then
  hs="$(wg show "$WG_IF" latest-handshakes | awk '{print $2}' | head -1)"
  if [ "${hs:-0}" -gt 0 ] 2>/dev/null; then
    pass "已与应用机握手 (最近一次 $(( $(date +%s) - hs )) 秒前)"
  else
    # 安全组没放行 UDP 就是这个样子: 接口起着、一次握手也没有。
    fail "接口在但从未握手 —— 多半是应用机的入站 UDP 没放行, 或对端公钥还没登记"
    echo "      应用机上: bash deploy/box-node/tunnel-wg-144.sh peer $(cat /etc/wireguard/$WG_IF.pub)"
  fi
else
  fail "wg show $WG_IF 失败"
fi

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
echo "  这台盒子的 WireGuard 公钥 (回应用机跑 tunnel-wg-144.sh peer <它>):"
echo "    $(cat /etc/wireguard/$WG_IF.pub)"
echo
echo "  .env 片段 (激活时才换上去, 演练不要动线上这几行):"
echo "    K8S_API_URL=https://$T_NODE:6443"
echo "    WORK_PROXY_CIDR=$T_APP/32"
echo
[ $rc -eq 0 ] && echo "全绿。" || echo "有未通过项 —— 上面 FAIL 的那几行。"
exit $rc
