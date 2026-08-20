#!/usr/bin/env bash
# 安全地重新部署 dhc-server。
#
#   ./scripts/safe_deploy.sh            # 重建并重启 server
#   SKIP_SMOKE=1 ./scripts/safe_deploy.sh   # 应急: 跳过部署后冒烟
#
# 为什么不直接 `docker compose up -d --build`: 那样没有任何闸门。镜像构建失败、
# 新代码在**鉴权之后**第一行就崩、部署时正有人在跑任务 —— 三种都没人拦。
# 姊妹产品线 2026-08-10 出过一次: 构建成功、容器起来、/health 返回 ok, 而真实
# 业务端点全量 500。雷在鉴权之后的处理器里, 不带 token 的健康探测根本够不着。
#
# 闸门顺序 (任一不过就停在部署之前, 线上维持原样):
#   1. 工作树干净 —— 不把别条线的半成品一起部署上去
#   2. 部署前基线 —— 本来就带病的话, 别把锅算到这次部署头上
#   3. 构建 + 重启
#   4. 等健康
#   5. **带鉴权的深链路冒烟** (见 smoke 函数)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
cd "$repo"

COMPOSE="${COMPOSE:-deploy/prod/compose.yml}"
ENVFILE="${ENVFILE:-deploy/prod/.env}"
CONTAINER="${CONTAINER:-dhc-server}"
PORT="${PORT:-8100}"

[ -f "$COMPOSE" ] || { echo "!! 找不到 $COMPOSE (换机时用 COMPOSE= 指定)" >&2; exit 1; }
[ -f "$ENVFILE" ] || { echo "!! 找不到 $ENVFILE" >&2; exit 1; }

# --- 闸门 1: 工作树干净 ------------------------------------------------------
# 这台机器上常有多条线并行, 工作区里可能躺着别人的在制品。构建用的是工作树而非
# HEAD, 脏工作树会把半成品一起打进镜像。
dirty="$(git status --porcelain 2>/dev/null | grep -v '^?? ' || true)"
if [ -n "$dirty" ] && [ "${ALLOW_DIRTY:-0}" != "1" ]; then
  echo "!! 工作树有未提交改动, 拒绝部署 (构建取的是工作树, 会把半成品打进镜像):" >&2
  echo "$dirty" | head -10 >&2
  echo "   确知要带上可 ALLOW_DIRTY=1 覆盖。" >&2
  exit 1
fi

# --- 闸门 2: 部署前基线 ------------------------------------------------------
# 先记下部署**之前**是否健康。若本来就带病, 冒烟失败不该算到这次部署头上 ——
# 那会把人引向错误的回滚。
before="$(docker exec "$CONTAINER" python -c "
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=5)
    print('ok')
except Exception:
    print('sick')
" 2>/dev/null || echo unknown)"
echo "==> 部署前基线: $before"

# --- 部署 --------------------------------------------------------------------
echo "==> build + up"
docker compose -f "$COMPOSE" --env-file "$ENVFILE" up -d --build

echo "==> 等待健康"
for i in $(seq 1 40); do
  if docker exec "$CONTAINER" python -c "
import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=3)" 2>/dev/null; then
    echo "    healthy"; break
  fi
  [ "$i" = 40 ] && { echo "!! 起不来" >&2; docker logs --tail 40 "$CONTAINER" >&2; exit 1; }
  sleep 3
done

# --- 闸门 5: 带鉴权的深链路冒烟 ----------------------------------------------
# /api/health 不过鉴权, 够不到真正会崩的地方。这里用容器内签的**真 token** 打
# 网关的 chat/completions, 但模型故意给一个目录里没有的 —— gateway 在
# 鉴权 → 账号 → 并发 → QPS → 积分 都过了之后才解析模型, 然后确定性 404。
# 于是这一发覆盖整条前置链路, 却不碰上游、不扣任何积分。
# 期望 404; 5xx 或连不上 = 线上带病。
smoke() {
  [ "${SKIP_SMOKE:-0}" = "1" ] && { echo "⚠️  SKIP_SMOKE=1 — 跳过冒烟" >&2; return 0; }
  local code
  code="$(docker exec "$CONTAINER" python -c "
import json, urllib.request, urllib.error
from app import db, security
row = db.query_one(\"SELECT id FROM users WHERE status='active' ORDER BY created LIMIT 1\")
if not row:
    print('nouser'); raise SystemExit
token = security.sign_token(row['id'])
req = urllib.request.Request(
    'http://127.0.0.1:$PORT/llm/v1/chat/completions',
    data=json.dumps({'model': '__deploy_smoke_not_offered__',
                     'messages': [{'role': 'user', 'content': 'x'}]}).encode(),
    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
try:
    urllib.request.urlopen(req, timeout=15); print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print('000')
" 2>/dev/null || echo 000)"
  case "$code" in
    404) echo "✓ 冒烟通过: 网关前置链路健康 (404 如预期 — 鉴权/账号/配额都过了, 只是模型不存在)"; return 0 ;;
    nouser) echo "⚠️  冒烟跳过: 库里没有 active 用户, 签不出 token" >&2; return 0 ;;
    401|403) echo "✗ 冒烟失败: 鉴权返回 $code — 签出来的 token 被拒, 会话/密钥链路有问题" >&2; return 1 ;;
    5*|000) echo "✗ 冒烟失败: 返回 $code — 线上可能带病!" >&2
            echo "  详查: docker logs $CONTAINER --since 5m 2>&1 | grep -A 20 Traceback" >&2
            [ "$before" = "sick" ] && echo "  注意: 部署**之前**就已经不健康, 未必是这次部署引入的。" >&2
            return 1 ;;
    *) echo "✗ 冒烟失败: 意外状态 $code" >&2; return 1 ;;
  esac
}
smoke

echo
echo "✓ 部署完成并通过冒烟。"
