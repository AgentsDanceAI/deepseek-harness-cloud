#!/usr/bin/env bash
# 把备用节点拉起来 —— 在**应用机**上以 root 跑。演练和真出事用的是同一条命令。
#
#   BOX_ENVFILE=/path/to/.env bash deploy/box-node/activate.sh <box id>     # 唤醒一台已有的
#   BOX_ENVFILE=... DSH_FROM_SNAPSHOT=dsh-node bash deploy/box-node/activate.sh   # 从底片新开一台
#
# 做完这六步, 节点就绪、隧道在、烟测过。**它不改任何线上配置** —— 最后那一步
# (.env 换 K8S_API_URL 并重启 api) 打印出来给人自己贴, 因为那一下是真切流量。
#
# 隧道是 WireGuard, 由**盒子出站**拨过来 (见 tunnel-wg-144.sh 里的原委), 所以这里不用
# 关心盒子这次落在哪、地址是什么 —— 那是 ssh 隧道时代才要操心的事。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BOX="${1:-}"; SNAP="${DSH_FROM_SNAPSHOT:-}"
T_APP="${DSH_BOX_TUNNEL_APP_IP:-10.99.1.1}"; T_NODE="${DSH_BOX_TUNNEL_NODE_IP:-10.99.1.2}"
WG_IF="${DSH_WG_IF:-dshbox0}"
CREDS=/root/dsh-k8s-box
rc=0; pass(){ printf 'PASS  %s\n' "$*"; }; fail(){ printf 'FAIL  %s\n' "$*"; rc=1; }
box(){ bash "$HERE/box.sh" "$@"; }

echo "=== 1/6 拿到一台开着的 Box ==="
if [ -z "$BOX" ]; then
  [ -n "$SNAP" ] || { echo "要 box id, 或用 DSH_FROM_SNAPSHOT=<底片名>" >&2; exit 2; }
  BOX="$(box from "$SNAP")" || exit 1; echo "从底片 $SNAP 开出 $BOX"
else
  box resume "$BOX" >/dev/null 2>&1 || true
fi
state=""
for _ in $(seq 1 60); do
  state="$(box get "$BOX" | python3 -c 'import json,sys;print(json.load(sys.stdin)["box"]["state"])')"
  case "$state" in ready|idle|running|error|deleted) break;; esac; sleep 5
done
case "$state" in ready|idle|running) pass "Box $BOX 状态 $state";; *) fail "Box 没起来 (状态 $state)"; exit 1;; esac

echo "=== 2/6 隧道 (WireGuard) ==="
bash "$HERE/tunnel-wg-144.sh" up >/dev/null 2>&1 && pass "应用机侧 wg-quick@$WG_IF 已起" || fail "应用机侧 wg 起不来"
# 盒子那侧是 systemd 使能的, resume 之后自己回来; 这里等握手, 不催它。
hs=0
for _ in $(seq 1 30); do
  hs="$(wg show "$WG_IF" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -rn | head -1)"
  [ "${hs:-0}" -gt 0 ] 2>/dev/null && break; sleep 2
done
if [ "${hs:-0}" -gt 0 ] 2>/dev/null; then
  pass "已握手 ($(( $(date +%s) - hs )) 秒前), 隧道 $T_APP <-> $T_NODE"
else
  fail "一直没握手 —— 查: 安全组入站 UDP 放行了吗 / 对端公钥登记了吗 / 盒子里 wg-quick@dsh0 起没起"
  echo "      盒子里看: bash $HERE/box.sh run $BOX 'sudo wg show; systemctl is-active wg-quick@dsh0'"
fi
# 通往 248 的那条必须毫发无损 —— 演练的全部意义就在这里。
echo "  通往 248 的 dsh-tunnel: $(systemctl is-active dsh-tunnel 2>/dev/null || echo '(未安装)')"

echo "=== 3/6 凭据 ==="
install -d -m 0755 "$CREDS"
box run "$BOX" 'sudo cat /root/dsh-k8s-token' > "$CREDS/token" 2>/dev/null
box run "$BOX" 'sudo cat /root/dsh-k8s-ca.b64' 2>/dev/null | base64 -d > "$CREDS/ca.crt"
# 容器里跑服务的是 uid 10001 (Dockerfile 的 dsh-cloud), 属主不对就是产品域名 500 —
# 2026-09-03 在 248 上栽过一次, 这里直接照做。
chown 10001:10001 "$CREDS/token" "$CREDS/ca.crt" 2>/dev/null || true
chmod 0600 "$CREDS/token" "$CREDS/ca.crt"
[ -s "$CREDS/token" ] && [ -s "$CREDS/ca.crt" ] && pass "token / ca.crt 已就位 ($CREDS)" || fail "凭据没取到"

echo "=== 4/6 k8s API 走隧道通不通 ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --cacert "$CREDS/ca.crt" \
        -H "Authorization: Bearer $(cat "$CREDS/token" 2>/dev/null)" \
        "https://$T_NODE:6443/api/v1/namespaces/dsh/pods" 2>/dev/null)"
[ "$code" = 200 ] && pass "API 200 (namespace dsh 可列)" || fail "API 返回 ${code:-无响应}"

echo "=== 5/6 烟测: 真起一个 Pod, 且必须落在 gVisor 里 ==="
box run "$BOX" 'sudo k3s kubectl -n dsh delete pod dsh-smoke --ignore-not-found >/dev/null 2>&1
cat <<Y | sudo k3s kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: dsh-smoke, namespace: dsh}
spec:
  runtimeClassName: gvisor
  containers:
  - name: c
    image: busybox:1.36
    command: ["sh","-c","echo ok > /tmp/x; httpd -f -p 8080 -h /tmp"]
    resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 200m, memory: 128Mi}}
Y
for i in $(seq 1 40); do
  p=$(sudo k3s kubectl -n dsh get pod dsh-smoke -o jsonpath="{.status.phase}" 2>/dev/null)
  [ "$p" = Running ] && break; sleep 3
done
echo "phase=$p"
echo "kernel=$(sudo k3s kubectl -n dsh exec dsh-smoke -- uname -r 2>/dev/null)"
echo "podip=$(sudo k3s kubectl -n dsh get pod dsh-smoke -o jsonpath="{.status.podIP}" 2>/dev/null)"' > /tmp/dsh-smoke.out 2>&1
sed 's/^/  /' /tmp/dsh-smoke.out
grep -q "phase=Running" /tmp/dsh-smoke.out && pass "Pod 起来了" || fail "Pod 没到 Running"
# uname 必须是 gVisor 的内核号 —— 只看"起没起"证明不了它在沙箱里 (248 上的老教训)。
grep -qE "kernel=4\.19\.[0-9]+-gvisor" /tmp/dsh-smoke.out && pass "确认在 gVisor 内核里" || fail "内核不是 gvisor 的 —— 沙箱没生效"

echo "=== 6/6 应用机 → Pod 直连 (Caddy 就是这么反代的) ==="
POD_IP="$(sed -n 's/^podip=//p' /tmp/dsh-smoke.out | tr -d '[:space:]')"
if [ -n "$POD_IP" ]; then
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://$POD_IP:8080/x")"
  [ "$c" = 200 ] && pass "宿主直连 Pod $POD_IP:8080 → 200" || fail "宿主连 Pod 得到 ${c:-无响应}"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^dhc-server$'; then
    # 镜像里没有 curl (2026-09-06 演练撞上), 但一定有 python3 —— 服务本身就是它跑的。
    c2="$(docker exec dhc-server python3 -c "import urllib.request as u;print(u.urlopen('http://$POD_IP:8080/x',timeout=15).status)" 2>/dev/null)"
    [ "$c2" = 200 ] && pass "dhc-server 容器里直连 Pod → 200" || fail "容器里连 Pod 得到 ${c2:-无响应}"
  fi
else
  fail "没拿到 Pod IP"
fi
box run "$BOX" 'sudo k3s kubectl -n dsh delete pod dsh-smoke --ignore-not-found' >/dev/null 2>&1

echo
if [ $rc -eq 0 ]; then
  cat <<TXT
全绿 —— 备用节点可用。**切流量是单独一步, 上面没有做**:

  1) deploy/prod/.env 改两行:
       K8S_API_URL=https://$T_NODE:6443
       WORK_PROXY_CIDR=$T_APP/32
  2) token/ca 换成这台的: compose 里挂的是 /root/dsh-k8s, 把 $CREDS/* 覆盖过去
     (先备份 248 那份), 属主 10001:10001, 0600。
  3) docker compose restart api, 然后开一个工作台真点一遍。
  4) 镜像是冷的: 第一个开 Coze/Dify 的人要等拉镜像。切之前最好先预热 (见 README)。

演练完不切的话, 收工: bash $HERE/tunnel-wg-144.sh down; bash $HERE/box.sh stop $BOX
TXT
else
  echo "有未通过项 —— 别切流量。"
fi
exit $rc
