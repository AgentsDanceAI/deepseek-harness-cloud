#!/usr/bin/env bash
# ascii.dev Box 的一层薄壳 (只用 curl, 不引 SDK) —— 备用工作台节点的全部操作都从这里走。
#
#   bash deploy/box-node/box.sh <子命令> [参数]      # 密钥与 org 默认从 /root/dsh-k8s-box/box.env 读
#
#   ls                          列出账上所有 Box (id / 状态 / 规格 / 公网 IP)
#   limits                      当前档位、并发上限、还能不能开 (按 BOX_ORG 指定的钱包)
#   orgs                        列出能用的钱包 (哪个是 standard 就把它设成 BOX_ORG)
#   new [type] [ttl]            开一台 (默认 default / 7200s; 试用档强制 ttl<=7200)
#   from <名字快照> [type]      从命名快照开一台 —— 这是"激活备用节点"的正路
#   get <box>                   一台的详情
#   ip <box>                    只打印 ip 字段 (可能是 IPv6, 别拿它 ssh)
#   ssh <box>                   打印 "<host> <port>": 优先 sshEndpoint, 回落公网 IPv4
#   run <box> <命令...>         在盒子里跑一条命令, 打印 stdout/stderr/退出码
#   key <box> <公钥文件>        把一把 OpenSSH 公钥装进盒子 (之后可以 ssh user@<ip>)
#   stop <box>                  停机 (快照 + 停表, **免费**; resume 接着用)
#   resume <box>                开机
#   snap <box> <名字>           存一份命名快照 (无过期, 最多 10 份) —— 备用节点的"底片"
#   snaps                       列出命名快照
#   rm <box>                    删除 (要带确认头, 值就是 box id)
#
# 密钥: BOX_API_KEY。优先取环境变量, 否则从 BOX_ENVFILE 里读那一行。**不打印、不进日志。**
#
# ⚠️ BOX_ORG —— 付费记在哪个钱包上。2026-09-06 实测: 订阅买在 org 上, 而 API key 默认
# 走 Personal 钱包, 于是账号明明付过钱, `limits` 还报 trial、开到第 3 台就
# `limit_reached: Trial accounts can run 2 concurrent boxes`。带上 org 之后同一把 key
# 立刻是 standard/100 台 (实测连开 10 台, 每台约 1 秒)。
# org id 用 `box.sh orgs` 查; 不设就是个人钱包。
set -euo pipefail

API="${BOX_API_BASE:-https://ascii.dev/api/box/v1}"
# 默认从应用机上那份 box.env 读 (BOX_API_KEY + BOX_ORG)。**不要**放进
# deploy/prod/.env —— compose 对 dhc-server 是 `env_file: [".env"]`, 放那儿等于把
# Box 的密钥注进应用容器, 而它根本不用 Box。
ENVFILE="${BOX_ENVFILE:-/root/dsh-k8s-box/box.env}"
envget() {  # 别 source —— .env 里有带空格不加引号的值, source 会把它们当命令执行
  { grep -E "^$1=" "$ENVFILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'
}
if [ -r "$ENVFILE" ]; then
  [ -n "${BOX_API_KEY:-}" ] || BOX_API_KEY="$(envget BOX_API_KEY)"
  [ -n "${BOX_ORG:-}" ]     || BOX_ORG="$(envget BOX_ORG)"
fi
[ -n "${BOX_API_KEY:-}" ] || { echo "没有 BOX_API_KEY (设环境变量, 或让 BOX_ENVFILE 指到含它的文件; 当前找的是 $ENVFILE)" >&2; exit 2; }
# 没有 org = 这次请求记在个人钱包上, 而个人钱包多半还是 trial —— 症状是"明明付过钱
# 却只能开 2 台", 且没有任何一处会说破。所以这里必须吵。
if [ -z "${BOX_ORG:-}" ] && [ "${1:-}" != "orgs" ]; then
  echo "⚠️  没有 BOX_ORG: 这次请求算在**个人钱包**上 (多半是 trial, 2 台并发)。" >&2
  echo "    \`bash $0 orgs\` 找到 standard 那个, 写进 $ENVFILE 的 BOX_ORG=" >&2
fi

req() {  # req <方法> <路径> [json 体] [额外 header...]
  local m="$1" p="$2" body="${3:-}"; shift 3 2>/dev/null || shift 2
  local args=(-sS -X "$m" -H "authorization: Bearer $BOX_API_KEY" --max-time "${BOX_TIMEOUT:-180}")
  [ -n "${BOX_ORG:-}" ] && args+=(-H "X-Box-Org: $BOX_ORG")
  [ -n "$body" ] && args+=(-H 'content-type: application/json' -d "$body")
  for h in "$@"; do args+=(-H "$h"); done
  curl "${args[@]}" "$API$p"
}

# 出错时把 message 打出来再退出 —— 直接把整坨 JSON 甩给人看没用。
check() { python3 -c '
import json,sys
d=json.load(sys.stdin)
if not d.get("ok", True):
    e=d.get("error") or {}
    print("ascii.dev 拒绝了: %s (%s)" % (d.get("message") or e.get("message"), d.get("code") or e.get("code")), file=sys.stderr)
    sys.exit(1)
json.dump(d, sys.stdout)
'; }

jq_py() { python3 -c "import json,sys;d=json.load(sys.stdin);$1"; }

cmd="${1:-}"; shift || true
case "$cmd" in
ls)
  req GET /boxes | check | jq_py 'print("\n".join("%-14s %-11s %-8s %-16s %s" % (b["id"],b["state"],b["type"],b.get("ip") or "-",b.get("name") or "") for b in d["boxes"])) if d["boxes"] else print("(账上没有 Box)")' ;;
orgs)
  # 哪个钱包是 standard 就把它的 id 设成 BOX_ORG —— 订阅可能买在 org 上而不是个人名下。
  req GET /orgs | check | jq_py 'print("\n".join("%-42s %-10s %s" % (o["id"], o["type"], o["name"]) for o in d["orgs"]))'
  echo "(逐个查档位: BOX_ORG=<id> bash $0 limits)" ;;
limits)
  req GET /limits | check | jq_py 'c=d["currentLimits"];print("档位 %s  并发上限 %s/分钟建 %s  blocked=%s" % (d["accessTier"],c["activeBoxes"],c["creationRatePerMinute"],d.get("blockedReason")))' ;;
new)
  t="${1:-default}"; ttl="${2:-7200}"
  req POST /boxes "{\"type\":\"$t\",\"ttlSeconds\":$ttl,\"noEnv\":true}" "Idempotency-Key: $(date +%s)-$RANDOM" \
    | check | jq_py 'print(d["box"]["id"])' ;;
from)
  name="${1:?要命名快照的名字}"; t="${2:-default}"
  req POST /boxes "{\"type\":\"$t\",\"ttlSeconds\":${BOX_TTL:-7200},\"noEnv\":true,\"fromSnapshot\":\"$name\"}" "Idempotency-Key: $(date +%s)-$RANDOM" \
    | check | jq_py 'print(d["box"]["id"])' ;;
get)
  req GET "/boxes/${1:?要 box id}" | check | python3 -m json.tool ;;
ip)
  req GET "/boxes/${1:?要 box id}" | check | jq_py 'print(d["box"].get("ip") or "")' ;;
ssh)
  # 打印 "<host> <port>"。**别直接用 ip 字段**: 2026-09-06 实测, 同一台 Box
  # resume 之后从 hetzner 的 VM (公网 IPv4) 落到 baremetal, ip 变成纯 IPv6
  # (应用机没有 v6 出口, 直接不可达), 而 sshEndpoint 这时才有值, 给的是
  # IPv4:高端口。两个字段谁有值看落在哪, 所以按这个顺序取。
  req GET "/boxes/${1:?要 box id}" | check | jq_py '
b=d["box"]; e=(b.get("sshEndpoint") or "").strip()
if e: h,_,p=e.rpartition(":"); print(h, p or 22)
elif b.get("ip") and ":" not in b["ip"]: print(b["ip"], 22)
else: print("", "", end="")' ;;
run)
  b="${1:?要 box id}"; shift; c="$*"
  python3 -c 'import json,sys;print(json.dumps({"command":sys.argv[1]}))' "$c" \
    | { read -r body; req POST "/boxes/$b/commands" "$body"; } | check | jq_py '
import sys
sys.stdout.write(d.get("stdout") or "")
e=d.get("stderr") or ""
if e: sys.stderr.write(e)
sys.exit(0 if d.get("exitCode")==0 else (d.get("exitCode") or 1))' ;;
key)
  b="${1:?要 box id}"; f="${2:?要公钥文件}"
  python3 -c 'import json,sys;print(json.dumps({"key":open(sys.argv[1]).read().strip()}))' "$f" \
    | { read -r body; req POST "/boxes/$b/sshkey" "$body"; } | check >/dev/null && echo "公钥已装入 $b" ;;
stop)    req POST "/boxes/${1:?}/stop"   | check >/dev/null && echo "已停机 (免费, resume 可接着用)" ;;
resume)  req POST "/boxes/${1:?}/resume" | check >/dev/null && echo "已开机" ;;
snap)
  # 真实路径是 POST /named-snapshots {boxId,name} —— 不是 /boxes/{id}/snapshots/{name}
  # (文档正文里那句 `box snapshot <id> <name>` 是 CLI 的写法, REST 不长这样)。
  b="${1:?要 box id}"; n="${2:?要快照名字}"
  req POST /named-snapshots "{\"boxId\":\"$b\",\"name\":\"$n\"}" | check >/dev/null && echo "已存命名快照: $n" ;;
snaps)
  req GET /named-snapshots | check | jq_py 'ss=d.get("snapshots") or d.get("namedSnapshots") or [];print("\n".join("%-24s %s" % (s.get("name"),s.get("createdAt","")) for s in ss)) if ss else print("(没有命名快照)")' ;;
rm)
  # 是 DELETE /boxes/{id}, 不是文档正文写的 POST /boxes/{id}/delete (后者 404)。
  # 确认头的值必须**就是**那台的 id, 否则 409。
  b="${1:?}"; req DELETE "/boxes/$b" "" "X-Ascii-Confirm-Delete: $b" | check >/dev/null && echo "已删除 $b" ;;
*)
  sed -n '2,25p' "$0" >&2; exit 2 ;;
esac
