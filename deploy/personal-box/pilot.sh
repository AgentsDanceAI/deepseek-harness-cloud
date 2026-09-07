#!/usr/bin/env bash
# 「每人一台云电脑」的端到端试点 —— 在**应用机**上以 root 跑。
#
#   bash deploy/personal-box/pilot.sh <用户名> [产品id]      # 建 + 装 + 验
#   bash deploy/personal-box/pilot.sh <用户名> --verify      # 只重验 (盒子已存在)
#   bash deploy/personal-box/pilot.sh <用户名> --rm          # 拆掉
#
# 证的是这一条链能不能走通, 用的是**真实产品镜像和真实启动脚本** (从跑着的 dhc-server
# 里取 products.boot_script / env_for, 不手写), 所以过了就说明 BoxBackend 照着做即可:
#
#   浏览器 → Caddy → <隧道IP>:<端口> → 盒子里的产品容器
#            ↑ 与今天 X-Work-Upstream 的契约一模一样
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BOXSH="$HERE/../box-node/box.sh"
WGSH="$HERE/../box-node/tunnel-wg-144.sh"
NAME="${1:?要一个用户名 (只用来命名对端和状态文件)}"
ARG2="${2:-claude-code}"
STATE="/root/dsh-k8s-box/pilot-$NAME.env"
T_APP="${DSH_BOX_TUNNEL_APP_IP:-10.99.1.1}"
rc=0; pass(){ printf 'PASS  %s\n' "$*"; }; fail(){ printf 'FAIL  %s\n' "$*"; rc=1; }
box(){ bash "$BOXSH" "$@"; }
st(){ box get "$1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["box"]["state"])'; }
wait_up(){ for _ in $(seq 1 60); do case "$(st $1)" in idle|ready|running) return 0;; esac; sleep 3; done; return 1; }

if [ "$ARG2" = --rm ]; then
  [ -f "$STATE" ] && . "$STATE"
  bash "$WGSH" peer-rm "$NAME" >/dev/null 2>&1
  [ -n "${BOX:-}" ] && { box stop "$BOX" >/dev/null 2>&1; box rm "$BOX"; }
  rm -f "$STATE"; echo "已拆"; exit 0
fi

if [ "$ARG2" = --verify ]; then
  [ -f "$STATE" ] || { echo "没有 $STATE, 先跑一次建好" >&2; exit 2; }
  . "$STATE"
else
  PRODUCT="$ARG2"
  echo "=== 1/6 建一台盒子 ==="
  BOX=$(box new default 7200) || exit 1
  wait_up "$BOX" || { echo "开不了机"; exit 1; }
  # 隧道地址: 10.99.1.1 是应用机, .2 是备用节点, 用户从 .10 起
  N=10; while grep -q "AllowedIPs = 10.99.1.$N/32" /etc/wireguard/dshbox0.conf 2>/dev/null; do N=$((N+1)); done
  T_SELF="10.99.1.$N"
  printf 'BOX=%s\nPRODUCT=%s\nT_SELF=%s\n' "$BOX" "$PRODUCT" "$T_SELF" > "$STATE"
  pass "盒子 $BOX, 隧道地址 $T_SELF"

  echo "=== 2/6 装机 (WireGuard + 目录 + 预拉镜像) ==="
  box key "$BOX" /root/.ssh/id_ed25519.pub >/dev/null
  read -r H P <<<"$(box ssh "$BOX")"
  ssh-keygen -qf /root/.ssh/known_hosts -R "$H" 2>/dev/null; ssh-keygen -qf /root/.ssh/known_hosts -R "[$H]:$P" 2>/dev/null
  IMG=$(docker exec dhc-server python3 -c "from app import products;p=products.registry()['$PRODUCT'];print(p.image_ref or p.image)")
  RU=$(grep -m1 '^WORK_REGISTRY_USERNAME=' /data/workspace/deepseek-harness-cloud/deploy/prod/.env | cut -d= -f2-)
  RP=$(grep -m1 '^WORK_REGISTRY_PASSWORD=' /data/workspace/deepseek-harness-cloud/deploy/prod/.env | cut -d= -f2-)
  tar czf - -C "$HERE/.." personal-box | ssh -o StrictHostKeyChecking=no -p "$P" -i /root/.ssh/id_ed25519 "user@$H" "rm -rf /tmp/personal-box && tar xzf - -C /tmp"
  ssh -o StrictHostKeyChecking=no -p "$P" -i /root/.ssh/id_ed25519 "user@$H" \
    "sudo DSH_WG_SERVER_PUBKEY=$(cat /etc/wireguard/dshbox0.pub) DSH_WG_ENDPOINT=$(curl -s --max-time 10 https://ipinfo.io/ip):51820 \
        DSH_TUNNEL_IP=$T_SELF DSH_TUNNEL_APP_IP=$T_APP REG_USER='$RU' REG_PASS='$RP' \
        bash /tmp/personal-box/provision.sh '$IMG'" 2>&1 | grep -E '^(PASS|FAIL|===|  这台盒子的公钥|  盘:)'
  # sudo: box run 是以 user 身份跑的, 而 /opt/dsh-wg 是 root 0700 —— 不加 sudo 读不到,
  # 而且它只会静默地给一个空串, 一路传到 peer-add 才报"要公钥"。
  PUB=$(box run "$BOX" 'sudo cat /opt/dsh-wg/dsh0.pub' 2>/dev/null | tail -1)
  case "$PUB" in *=) ;; *) fail "读不到盒子的公钥 (拿到: '$PUB')"; exit 1;; esac
  echo "PUB=$PUB" >> "$STATE"; echo "IMG=$IMG" >> "$STATE"

  echo "=== 3/6 登记对端 ==="
  bash "$WGSH" peer-add "$NAME" "$PUB" "$T_SELF" | head -2
fi

echo "=== 4/6 等握手 ==="
hs=0; for _ in $(seq 1 30); do
  hs=$(wg show dshbox0 latest-handshakes 2>/dev/null | awk -v k="$PUB" '$1==k{print $2}')
  [ "${hs:-0}" -gt 0 ] 2>/dev/null && break; sleep 2
done
[ "${hs:-0}" -gt 0 ] 2>/dev/null && pass "已握手 ($(( $(date +%s) - hs ))s 前)" || fail "没握上手"

echo "=== 5/6 起产品容器 (用线上真实的 boot 脚本与 env) ==="
PORT=$(docker exec dhc-server python3 -c "from app import products;print(products.registry()['$PRODUCT'].port)")
docker exec dhc-server python3 -c "
from app import products
import json
print(json.dumps({'boot': products.boot_script('$PRODUCT'),
                  'env': products.env_for('$PRODUCT','pilot-token','pilot-secret')}))" > /tmp/pilot-spec.json
python3 - <<'PY' > /tmp/pilot-run.sh
import json, shlex
s = json.load(open("/tmp/pilot-spec.json"))
env = " ".join("-e %s" % shlex.quote(f"{k}={v}") for k, v in s["env"].items())
print("sudo docker rm -f dsh-product >/dev/null 2>&1")
print("sudo install -d -o root -g root /home/user/dsh/PRODUCT/home /home/user/dsh/PRODUCT/workspace")
print("sudo docker run -d --name dsh-product --restart=always "
      "-p TUNIP:PORT:PORT " + env +
      " -v /home/user/dsh/PRODUCT/home:/root -v /home/user/dsh/PRODUCT/workspace:/workspace"
      " -v /home/user/dsh/shared:/shared IMAGE sh -c " + shlex.quote(s["boot"]))
print("sleep 3; sudo docker ps --format '{{.Names}} {{.Status}}' | head -2")
PY
sed -i "s|PRODUCT|$PRODUCT|g; s|TUNIP|$T_SELF|g; s|PORT|$PORT|g; s|IMAGE|$IMG|g" /tmp/pilot-run.sh
box run "$BOX" "$(cat /tmp/pilot-run.sh)" 2>&1 | tail -2

echo "=== 6/6 从应用机反代过去 (这就是 Caddy 走的路) ==="
code=""; for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://$T_SELF:$PORT/" 2>/dev/null)
  [ "$code" = 200 ] && break; sleep 3
done
[ "$code" = 200 ] && pass "宿主 http://$T_SELF:$PORT/ → 200" || fail "宿主拿到 ${code:-无响应}"
c2=$(docker exec dhc-server python3 -c "import urllib.request as u;print(u.urlopen('http://$T_SELF:$PORT/',timeout=10).status)" 2>/dev/null)
[ "$c2" = 200 ] && pass "dhc-server 容器里 → 200 (Caddy 就是从这里出去的)" || fail "容器里拿到 ${c2:-无响应}"
rp=$(docker exec dhc-server python3 -c "
from app import products
import urllib.request as u
p=products.registry()['$PRODUCT']
print(u.urlopen('http://$T_SELF:$PORT'+p.ready_path,timeout=10).status)" 2>/dev/null)
[ "$rp" = 200 ] && pass "产品自己的就绪探针 → 200" || fail "就绪探针拿到 ${rp:-无响应}"

echo
[ $rc -eq 0 ] && echo "全绿 —— 链路通。盒子 $BOX, 隧道 $T_SELF:$PORT" || echo "有未通过项。"
echo "  重验: bash $0 $NAME --verify    拆掉: bash $0 $NAME --rm"
exit $rc
