#!/usr/bin/env bash
# 把一台机器接进应用机上的 k3s 集群当**工作节点**。在**那台机器**上以 root 跑。
#
#   K3S_URL=https://<应用机公网>:6443 K3S_TOKEN=<node-token> \
#   NODE_NAME=ecs-248 NODE_EXTERNAL_IP=<本机公网> \
#   RESERVE_CPU=6 RESERVE_MEM=64Gi DATA_DIR=/mnt/k3s \
#   bash join-as-agent.sh
#
# 248 那台是**公司的共享机**, 上面还跑着别人的 docker 服务 —— 所以:
#   · system-reserved 必须留够 (kubelet 把 Pod 总量卡在 capacity 减去它, 比命名空间
#     配额更硬)。默认 6 核 64Gi, 与它当 server 时的设置一致。
#   · data-dir 放 /mnt (系统盘小)。
#   · 只装 agent, 不碰机器上任何现有服务。
# 退场: /usr/local/bin/k3s-agent-uninstall.sh
set -uo pipefail
URL="${K3S_URL:?要 K3S_URL}"; TOK="${K3S_TOKEN:?要 K3S_TOKEN}"
NAME="${NODE_NAME:?要 NODE_NAME}"; EXT="${NODE_EXTERNAL_IP:?要 NODE_EXTERNAL_IP}"
RC="${RESERVE_CPU:-1}"; RM="${RESERVE_MEM:-2Gi}"; DD="${DATA_DIR:-/var/lib/rancher/k3s}"
# cgroup v1 的老内核要 false; v2 的机器给 true 也无妨 (默认就是 true)。
FAIL_CGROUPV1="${FAIL_CGROUPV1:-false}"
rc=0; pass(){ printf 'PASS  %s\n' "$*"; }; fail(){ printf 'FAIL  %s\n' "$*"; rc=1; }
[ "$(id -u)" = 0 ] || { echo "要 root" >&2; exit 2; }

echo "=== 1/4 先确认拨得到 server (拨不通就别拆现状) ==="
host="${URL#https://}"; port="${host##*:}"; host="${host%%:*}"
if timeout 8 bash -c "cat </dev/null >/dev/tcp/$host/$port" 2>/dev/null; then
  pass "$host:$port 可达"
else
  fail "$host:$port 不通 —— 停在这里, 什么都不动"; exit 1
fi

echo "=== 2/4 拆掉这台上原有的 k3s (如果它自己是个 server) ==="
if [ -x /usr/local/bin/k3s-uninstall.sh ]; then
  echo "  发现 server 版, 卸载 (镜像缓存会一起没, 之后要预热)"
  /usr/local/bin/k3s-uninstall.sh >/dev/null 2>&1
  pass "旧 server 已卸"
elif [ -x /usr/local/bin/k3s-agent-uninstall.sh ]; then
  /usr/local/bin/k3s-agent-uninstall.sh >/dev/null 2>&1; pass "旧 agent 已卸"
else
  pass "本来就没装"
fi

echo "=== 3/4 装 agent ==="
mkdir -p /etc/rancher/k3s
cat > /etc/rancher/k3s/config.yaml <<CFG
# 工作节点。控制面在应用机上 —— 这台只出站拨过去, 不需要任何入站端口。
node-name: $NAME
data-dir: $DD
# 跨网节点要用公网 IP 做 flannel 对等。缺了它节点照样 Ready, 但**跨节点的 Pod 网络
# 不通**, 而且没有一处会说破。
node-external-ip: $EXT
kubelet-arg:
  # 共享机上这是硬保险: 给机器上别人的服务留够。
  - "system-reserved=cpu=$RC,memory=$RM"
  - "eviction-hard=memory.available<1Gi,nodefs.available<10%"
  - "max-pods=60"
  # kubelet >= 1.36 默认拒绝 cgroup v1, 而内核 5.10 的 Alibaba Cloud Linux 3 就是 v1。
  # 少这一行 agent 起不来, 报错只在 journal 里 (systemd 只说 "Failed with result
  # 'protocol'")。这条和上面的 SKIP_SELINUX_RPM 都写在 k8s-node/README 里, 2026-09-08
  # 我照抄时两条都漏了 —— 写新配置前应该拿旧的逐行对一遍, 别凭记忆。
  - "fail-cgroupv1=$FAIL_CGROUPV1"
  # emptyDir 与容器可写层都在这下面, 别放系统盘。
  - "root-dir=$DD/kubelet"
CFG
curl -sfL https://get.k3s.io -o /tmp/k3s-install.sh || { fail "下载安装脚本失败"; exit 1; }
# INSTALL_K3S_SKIP_SELINUX_RPM: RPM 系的机器 (248 是 Alibaba Cloud Linux) 上, 安装
# 脚本会去装 k3s-selinux, 而它依赖的 container-selinux 版本装不上 —— 整个安装当场失败,
# 而**卸载已经做完了**, 那台机器就落在"两边都没有"的状态。仓库的 k8s-node/README 里
# 本来就写着这一条, 2026-09-08 我照抄时漏了, 于是 248 空了十分钟。
# 输出不吞: 安装失败的原因只在这里说一次。
INSTALL_K3S_CHANNEL=stable INSTALL_K3S_SYMLINK=skip INSTALL_K3S_SKIP_SELINUX_RPM=true \
  K3S_URL="$URL" K3S_TOKEN="$TOK" INSTALL_K3S_EXEC="agent" \
  sh /tmp/k3s-install.sh 2>&1 | tail -4 | sed 's/^/    /'
for _ in $(seq 1 40); do systemctl is-active --quiet k3s-agent && break; sleep 3; done
systemctl is-active --quiet k3s-agent && pass "k3s-agent 已起" || fail "agent 没起来 (journalctl -u k3s-agent)"

echo "=== 4/4 gVisor (工作台必须跑在沙箱里) ==="
if [ -x /usr/local/bin/runsc ]; then
  pass "runsc 已在 ($(/usr/local/bin/runsc --version 2>/dev/null | head -1))"
else
  ( cd /tmp && URLG=https://storage.googleapis.com/gvisor/releases/release/latest/x86_64 \
    && for f in runsc containerd-shim-runsc-v1; do curl -sfL -o "$f" "$URLG/$f" && curl -sfL -o "$f.sha512" "$URLG/$f.sha512"; done \
    && sha512sum -c ./runsc.sha512 ./containerd-shim-runsc-v1.sha512 >/dev/null \
    && install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/ ) \
    && pass "runsc 装好 (校验和已核)" || fail "runsc 装不上"
fi
# k3s 不认识 runsc, 要往它生成的 containerd 配置后面追加一段 (config-v3 = containerd 2.x)
install -D -m 0644 /dev/stdin /etc/containerd/runsc.toml <<'TOML'
[runsc_config]
  platform = "systrap"
  net-raw = "false"
TOML
install -D -m 0644 /dev/stdin "$DD/agent/etc/containerd/config-v3.toml.tmpl" <<'TMPL'
{{ template "base" . }}

[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
  [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runsc.options]
    TypeUrl = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
TMPL
systemctl restart k3s-agent
for _ in $(seq 1 40); do systemctl is-active --quiet k3s-agent && break; sleep 3; done
sleep 8
grep -qc runsc "$DD/agent/etc/containerd/config.toml" 2>/dev/null \
  && pass "containerd 里有 runsc 运行时" || fail "containerd 配置里没看到 runsc"

echo
[ $rc -eq 0 ] && echo "全绿。回控制面 kubectl get nodes 看这台在不在。" || echo "有未通过项。"
exit $rc
