#!/usr/bin/env bash
# 工作台集群巡检 —— 在**应用机**(控制面所在的那台)上以 root 跑, cron 每 5 分钟。
#
# 为什么要有它: 这套东西的故障形态几乎都是"不报错的坏", 站点全程 200, 没有任何
# 一处会说破 ——
#
#   · 2026-09-08: flannel 的 WireGuard 撞上 Box 隧道的 51820, k3s **干净退出**后
#     被 systemd 拉起, 如此往复 **2582 次 / 8 小时**。站点一直 200, 没人知道。
#     → 查 NRestarts 的**增量**(绝对值没意义, 长了才是信号)。
#   · 同日: node-external-ip 没给, 节点 Join 成功、状态 Ready, 但**跨节点的 Pod
#     网络不通**。kubectl 全绿。→ 真去连一个运行中的工作台的 Pod 地址。
#   · 同日: netpol 白名单还指着上一代架构的来源地址 —— ICMP 通、TCP 被 REJECT。
#     → 从**应用容器里**真调一次 k8s API, 那才是应用走的路。
#
# 顺带把容量趟出来: Pod 卡 Pending = 248 满了, 该买溢出节点了 (老板要的信号)。
#
# 告警走 AgentsDance 那套 (同一台机, 凭据在 api 容器里, 宿主机不存密钥)。
# 拿不到就只写日志 —— 巡检本身不该因为发不出信而失败。
set -uo pipefail
KC=/etc/rancher/k3s/k3s.yaml
KUBECTL="k3s kubectl"
STATE=/var/tmp/dsh-cluster-watch; mkdir -p "$STATE"
ALERT=/data/workspace/AgentsDanceCloud/scripts/alert_mail.sh
APP_CONTAINER="${APP_CONTAINER:-dhc-server}"
TS="$(date '+%F %T %Z')"
PROBLEMS=()

note() { printf '%s  %s\n' "$TS" "$*"; }

# ── 1. k3s 有没有在悄悄重启 ────────────────────────────────────────────
for unit in k3s k3s-agent; do
  systemctl list-unit-files "$unit.service" >/dev/null 2>&1 || continue
  systemctl is-enabled "$unit" >/dev/null 2>&1 || continue
  now="$(systemctl show "$unit" -p NRestarts --value 2>/dev/null)"
  [ -n "$now" ] || continue
  f="$STATE/$unit.restarts"
  prev="$(cat "$f" 2>/dev/null || echo "$now")"
  echo "$now" > "$f"
  if [ "$now" -gt "$prev" ]; then
    PROBLEMS+=("$unit 在重启: 上次巡检 $prev 次 → 现在 $now 次 (+$((now - prev)))
    干净退出也会被 systemd 拉起来, 站点不会有任何异常。看 journalctl -u $unit -n 50
    历史原因: WireGuard 端口撞车 / 配置里有它不认的键")
  fi
done

# ── 2. 节点 ────────────────────────────────────────────────────────────
nodes="$($KUBECTL get nodes --no-headers 2>&1)"
if [ -z "$nodes" ] || grep -qiE 'refused|error|unable' <<<"$nodes"; then
  PROBLEMS+=("kubectl 取不到节点列表 —— 控制面可能没在跑: ${nodes:0:200}")
else
  notready="$(awk '$2 !~ /^Ready/ {print $1" ("$2")"}' <<<"$nodes" | tr '\n' ' ')"
  [ -n "$notready" ] && PROBLEMS+=("节点不 Ready: $notready")
  # 能收工作台的节点 = Ready 且未封锁且不是控制面(控制面带 NoExecute 污点)
  workers="$(awk '$2 == "Ready" && $3 !~ /control-plane/ {print $1}' <<<"$nodes" | wc -l)"
  [ "$workers" -eq 0 ] && PROBLEMS+=("**没有一个可调度的工作节点** —— 新工作台一个都开不出来")
  note "节点: $(tr '\n' ';' <<<"$nodes" | sed 's/  */ /g')"
fi

# ── 3. 容量: 有没有 Pod 卡着排不上 ─────────────────────────────────────
# 这是"该买溢出节点"的信号。5 分钟还 Pending 才算 (刚创建的不算)。
pending="$($KUBECTL get pods -A --field-selector=status.phase=Pending \
           --no-headers 2>/dev/null | awk '{print $1"/"$2" "$6}')"
if [ -n "$pending" ]; then
  stuck=""
  while read -r p age; do
    [ -n "$p" ] || continue
    # AGE 形如 7m32s / 2h13m / 3d —— 秒和分钟级的放过
    case "$age" in
      *d*|*h*) stuck+="$p($age) ";;
      *m*s|*m) [ "${age%%m*}" -ge 5 ] 2>/dev/null && stuck+="$p($age) ";;
    esac
  done <<<"$pending"
  if [ -n "$stuck" ]; then
    why="$($KUBECTL get events -A --field-selector reason=FailedScheduling \
           -o custom-columns=MSG:.message --no-headers 2>/dev/null | tail -2)"
    PROBLEMS+=("容量到顶: 这些 Pod 排不上队 $stuck
    调度器说: ${why:-<没有 FailedScheduling 事件>}
    该加节点了 —— 新开一台按 deploy/k8s-node/join-as-agent.sh 接进来即可,
    应用侧不用改任何东西 (K8sBackend 不知道集群有几个节点)。")
  fi
fi
# 容量水位每次都记一笔, 攒出趋势 (不告警)
$KUBECTL describe nodes 2>/dev/null \
  | awk '/^Name:/{n=$2} /Allocated resources/{f=1} f&&/memory/{print "    水位 "n": memory "$2" "$3; f=0}' \
  | sed "s/^/$TS/"

# ── 4. 应用真走的那条路: 容器里 → k8s API ──────────────────────────────
if docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
  # 用应用自己认的那几个变量 (K8S_CRED_DIR 是**宿主**路径, 给 compose 挂载用的,
  # 容器里并不存在 —— 拿它去读会得到一个和"网络不通"长得一模一样的假故障)。
  code="$(docker exec "$APP_CONTAINER" python3 -c '
import os, ssl, urllib.request
ctx = ssl.create_default_context(cafile=os.environ["K8S_CA_FILE"])
tok = open(os.environ["K8S_TOKEN_FILE"]).read().strip()
r = urllib.request.Request(os.environ["K8S_API_URL"] + "/version",
                           headers={"Authorization": "Bearer " + tok})
print(urllib.request.urlopen(r, context=ctx, timeout=10).status)
' 2>&1 | tail -1)"
  [ "$code" = "200" ] || PROBLEMS+=("应用容器调不通 k8s API (得到 '${code:-<异常>}')
    整条开工作台的路断了, 而站点首页照样 200。看 $APP_CONTAINER 的 .env:
    K8S_API_URL / K8S_CRED_DIR")
fi

# ── 5. 跨节点 Pod 网络: 真连一个运行中的工作台 ─────────────────────────
# 只在有工作台在跑的时候查 (没有就没什么可断的)。取一个不在本机的 Pod。
me="$($KUBECTL get nodes -l node-role.kubernetes.io/control-plane --no-headers 2>/dev/null | awk '{print $1}')"
# 命名空间取 config.py 的默认值 dsh (**不是** dshwork —— 那是安全组名, 别弄混:
# 名字写错的话这一项会永远静默跳过, 一个"永远不会红"的检查比没有检查更坏)。
read -r pip pnode <<<"$($KUBECTL get pods -n "${WORK_NS:-dsh}" \
  --field-selector=status.phase=Running -o custom-columns=IP:.status.podIP,N:.spec.nodeName \
  --no-headers 2>/dev/null | awk -v me="$me" '$2 != me {print; exit}')"
if [ -n "${pip:-}" ]; then
  if timeout 6 bash -c "cat </dev/null >/dev/tcp/$pip/8080" 2>/dev/null \
     || ping -c1 -W2 "$pip" >/dev/null 2>&1; then
    note "跨节点 Pod 网络通 ($pip @ $pnode)"
  else
    PROBLEMS+=("跨节点 Pod 网络不通: 够不着 $pip (在 $pnode 上)
    节点会一直显示 Ready, kubectl 全绿。查 flannel: 两端都要 node-external-ip,
    且 wireguard 的 UDP 端口两边安全组都得放行 (见 deploy/k8s-node/README.md)")
  fi
fi

# ── 收尾 ───────────────────────────────────────────────────────────────
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  note "全绿"
  rm -f "$STATE/alerted"
  exit 0
fi
body="工作台集群巡检发现问题 @ $TS

$(printf '%s\n\n' "${PROBLEMS[@]}")
巡检脚本: deploy 机 /data/workspace/deepseek-harness-cloud/scripts/watch_cluster.sh"
printf '%s\n' "$body" | sed "s/^/$TS  /"
# 同一批问题只发一封, 变了或恢复过再发 (别把邮箱刷爆)
sig="$(printf '%s' "${PROBLEMS[*]}" | cut -c1-400 | md5sum | cut -c1-8)"
if [ "$(cat "$STATE/alerted" 2>/dev/null)" != "$sig" ] && [ -x "$ALERT" ]; then
  printf '%s\n' "$body" | "$ALERT" "工作台集群: ${#PROBLEMS[@]} 项异常" >/dev/null 2>&1 \
    && echo "$sig" > "$STATE/alerted"
fi
exit 1
