#!/usr/bin/env bash
# 在 248 与备用 Box 节点之间来回切 —— 在**应用机**上以 root 跑。
#
#   bash deploy/box-node/switch-node.sh status    # 现在指着哪台
#   bash deploy/box-node/switch-node.sh to-box    # 切到备用节点 (先自检, 不过就不切)
#   bash deploy/box-node/switch-node.sh back      # 切回去 (从切换时存下的原值恢复)
#
# 为什么要有这个脚本, 而不是"照着 README 手工改两行": 真出事那天没人想在半夜对着
# 清单敲 sed, 而演练如果不能一键退回去, 就没人敢演练。
#
# 只动 deploy/prod/.env 的三行 (改前整份备份一次):
#   K8S_API_URL     指向哪台节点的 k8s API
#   WORK_PROXY_CIDR Pod 看到的反代来源 (netpol 的入站白名单按它写)
#   K8S_CRED_DIR    compose 把哪个目录只读挂进容器当 token/ca —— 所以**不用覆盖凭据文件**,
#                   248 那份原地不动, 退回去只是把这行删掉
#
# ⚠️ 必须 `up -d` 不能 `restart`: compose 的 env_file 是**创建容器时**读的, restart
# 不会重读, 于是"改了 .env、重启了、行为没变", 而且没有任何一处会报错。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENVFILE="${ENVFILE:-$REPO/deploy/prod/.env}"
COMPOSE="${COMPOSE:-$REPO/deploy/prod/compose.yml}"
STATE=/root/dsh-k8s-box/switch-state.env
T_APP="${DSH_BOX_TUNNEL_APP_IP:-10.99.1.1}"; T_NODE="${DSH_BOX_TUNNEL_NODE_IP:-10.99.1.2}"
BOX_CREDS=/root/dsh-k8s-box
rc=0; pass(){ printf 'PASS  %s\n' "$*"; }; fail(){ printf 'FAIL  %s\n' "$*"; rc=1; }
[ -f "$ENVFILE" ] || { echo "找不到 $ENVFILE" >&2; exit 2; }

getkey() { { grep -E "^$1=" "$ENVFILE" || true; } | tail -1 | cut -d= -f2-; }
setkeys() {  # setkeys K=V ... —— 值为空串表示删掉那一行
  python3 - "$ENVFILE" "$@" <<'PY'
import sys
path, pairs = sys.argv[1], sys.argv[2:]
want = dict(p.split("=", 1) for p in pairs)
out, seen = [], set()
for line in open(path).read().splitlines(True):
    k = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
    if k in want:
        seen.add(k)
        if want[k] == "":            # 删除
            continue
        out.append("%s=%s\n" % (k, want[k]))
    else:
        out.append(line)
for k, v in want.items():
    if k not in seen and v != "":
        if out and not out[-1].endswith("\n"): out.append("\n")
        out.append("%s=%s\n" % (k, v))
open(path, "w").write("".join(out))
PY
}
recreate() {
  echo "  重建 dhc-server (env_file 只在创建时读, restart 不算)…"
  docker compose -f "$COMPOSE" --env-file "$ENVFILE" up -d dhc-server 2>&1 | tail -3
  for _ in $(seq 1 30); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' dhc-server 2>/dev/null)" = healthy ] && return 0
    sleep 2
  done
  return 1
}
show() {
  echo "  K8S_API_URL     = $(getkey K8S_API_URL)"
  echo "  WORK_PROXY_CIDR = $(getkey WORK_PROXY_CIDR)"
  cd_="$(getkey K8S_CRED_DIR)"; echo "  K8S_CRED_DIR    = ${cd_:-(未设, compose 默认 /root/dsh-k8s)}"
  echo "  容器里实际生效  = $(docker exec dhc-server sh -c 'echo $K8S_API_URL' 2>/dev/null || echo '(容器没在跑)')"
}

case "${1:-}" in
status)
  api="$(getkey K8S_API_URL)"
  case "$api" in *"$T_NODE"*) echo "现在指着: **备用 Box 节点**";; *) echo "现在指着: 248 (或其它)";; esac
  show
  # 看**最近**握手, 不是"有没有握过" —— 盒子停机之后那个时间戳还留着,
  # 只判 >0 会一直说"已握手", 而隧道其实早就没了。
  hs="$(wg show dshbox0 latest-handshakes 2>/dev/null | awk '{print $2}' | sort -rn | head -1)"
  if [ "${hs:-0}" -gt 0 ] 2>/dev/null; then
    age=$(( $(date +%s) - hs ))
    [ "$age" -lt 180 ] && echo "  wg 隧道: 通 (${age}s 前握手)" || echo "  wg 隧道: **已断** (上次握手 $((age/60)) 分钟前; 盒子多半停机了)"
  else
    echo "  wg 隧道: 从未握手 (或没装)"
  fi
  echo "  248 隧道: $(systemctl is-active dsh-tunnel 2>/dev/null || echo '(未安装)')"
  ;;

to-box)
  echo "=== 1/4 先自检: 不通就不切 ==="
  [ -s "$BOX_CREDS/token" ] && [ -s "$BOX_CREDS/ca.crt" ] || { fail "$BOX_CREDS 里没有凭据 —— 先跑 activate.sh"; exit 1; }
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 --cacert "$BOX_CREDS/ca.crt" \
          -H "Authorization: Bearer $(cat "$BOX_CREDS/token")" \
          "https://$T_NODE:6443/api/v1/namespaces/dsh/pods" 2>/dev/null)"
  [ "$code" = 200 ] && pass "备用节点 API 200" || { fail "备用节点 API $code —— 先跑 activate.sh 把它弄绿"; exit 1; }

  echo "=== 2/4 存原值 + 整份备份 ==="
  cur_api="$(getkey K8S_API_URL)"; cur_cidr="$(getkey WORK_PROXY_CIDR)"; cur_dir="$(getkey K8S_CRED_DIR)"
  case "$cur_api" in *"$T_NODE"*) echo "已经指着备用节点了, 无事可做"; exit 0;; esac
  install -d -m 0700 "$BOX_CREDS"
  bak="$ENVFILE.bak.$(date +%s)"; cp -p "$ENVFILE" "$bak"; chmod 0600 "$bak"
  { echo "PREV_K8S_API_URL=$cur_api"; echo "PREV_WORK_PROXY_CIDR=$cur_cidr"; echo "PREV_K8S_CRED_DIR=$cur_dir"; echo "PREV_ENV_BACKUP=$bak"; } > "$STATE"
  chmod 0600 "$STATE"; pass "原值已存 ($STATE); .env 整份备份在 $bak"

  echo "=== 3/4 改三行并重建 ==="
  setkeys "K8S_API_URL=https://$T_NODE:6443" "WORK_PROXY_CIDR=$T_APP/32" "K8S_CRED_DIR=$BOX_CREDS"
  recreate && pass "dhc-server healthy" || fail "容器没到 healthy (docker logs dhc-server)"

  echo "=== 4/4 复核 ==="
  inside="$(docker exec dhc-server sh -c 'echo $K8S_API_URL' 2>/dev/null)"
  [ "$inside" = "https://$T_NODE:6443" ] && pass "容器里生效的是备用节点 ($inside)" || fail "容器里还是 $inside"
  docker exec dhc-server sh -c 'test -s /run/dsh-k8s/token && test -s /run/dsh-k8s/ca.crt' \
    && pass "凭据已挂进容器" || fail "容器里读不到 /run/dsh-k8s/{token,ca.crt}"
  echo
  [ $rc -eq 0 ] && cat <<TXT
切好了。**现在去浏览器真开一格工作台** —— 这一步只有你能做 (要登录):

  · 挑「$(printf 'Hermes')」那格试: 它的镜像是 nginx:1.27-alpine, 几 MB, 拉得快。
    Coze (16G 内存) 在这台 4C/8G 上**起不来**, 别拿它试。
  · 第一次开任何一格都要现拉镜像, 慢是正常的, 不是坏了。
  · 看日志: docker logs -f dhc-server   /   盒子里: sudo k3s kubectl -n dsh get pods -w

测完退回去: bash $HERE/switch-node.sh back
TXT
  [ $rc -ne 0 ] && echo "有未通过项 —— 建议立刻 back"
  ;;

back)
  [ -f "$STATE" ] || { echo "没有 $STATE, 不知道原值是什么。手工改回 deploy/prod/.env 的那三行。" >&2; exit 2; }
  . "$STATE"
  echo "=== 恢复 ==="
  setkeys "K8S_API_URL=$PREV_K8S_API_URL" "WORK_PROXY_CIDR=$PREV_WORK_PROXY_CIDR" "K8S_CRED_DIR=$PREV_K8S_CRED_DIR"
  recreate && pass "dhc-server healthy" || fail "容器没到 healthy"
  inside="$(docker exec dhc-server sh -c 'echo $K8S_API_URL' 2>/dev/null)"
  [ "$inside" = "$PREV_K8S_API_URL" ] && pass "已切回 $inside" || fail "容器里是 $inside, 期望 $PREV_K8S_API_URL"
  echo "  (改前的 .env 整份备份还在 $PREV_ENV_BACKUP, 确认无误后可以删)"
  rm -f "$STATE"
  ;;

*) sed -n '2,20p' "$0" >&2; exit 2 ;;
esac
exit $rc
