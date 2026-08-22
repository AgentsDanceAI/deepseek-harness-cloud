#!/usr/bin/env bash
# 安全地重新部署 dhc-server。
#
#   ./scripts/safe_deploy.sh            # 重建并重启 server
#   SKIP_SMOKE=1 ./scripts/safe_deploy.sh   # 应急: 跳过部署后冒烟
#
# 仅有容器启动和匿名健康检查不足以覆盖鉴权后的业务链路。本脚本在重启前后设置
# 明确门禁，并用不消耗上游额度的鉴权请求检查关键前置路径。
#
# 闸门顺序（任一不过就停止继续变更）:
#   1. 工作树干净 —— 不把别条线的半成品一起部署上去
#   2. 部署前基线 —— 区分既有故障与本次发布回归
#   3. 构建 + 重启
#   4. 等健康
#   5. **带鉴权的深链路冒烟** (见 smoke 函数)
#   6. 工作台镜像缓存是否跟上 WORK_IMAGE_REF (只告警, 不拦部署)
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
# 镜像从工作树而非 HEAD 构建，因此默认拒绝未提交的受控文件改动。
dirty="$(git status --porcelain 2>/dev/null | grep -v '^?? ' || true)"
if [ -n "$dirty" ] && [ "${ALLOW_DIRTY:-0}" != "1" ]; then
  echo "!! 工作树有未提交改动, 拒绝部署 (构建取的是工作树, 会把半成品打进镜像):" >&2
  echo "$dirty" | head -10 >&2
  echo "   确知要带上可 ALLOW_DIRTY=1 覆盖。" >&2
  exit 1
fi
revision="$(git rev-parse --verify HEAD^{commit})"

# --- 闸门 2: 部署前基线 ------------------------------------------------------
# 记录部署前健康状态，避免将既有故障误判为发布回归。
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
REVISION="$revision" docker compose -f "$COMPOSE" --env-file "$ENVFILE" up -d --build

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
# 期望 404；5xx 或连接失败表示鉴权后的链路不健康。
smoke() {
  [ "${SKIP_SMOKE:-0}" = "1" ] && { echo "⚠️  SKIP_SMOKE=1 — 跳过冒烟" >&2; return 0; }
  local code
  code="$(docker exec "$CONTAINER" python -c "
import json, urllib.request, urllib.error
from app import db, security
row = db.query_one(\"SELECT id, session_epoch FROM users WHERE status='active' ORDER BY created LIMIT 1\")
if not row:
    print('nouser'); raise SystemExit
# epoch 必须带上: try_resolve_user 会逐位比对它, 而 sign_token 默认给 0。
# 那个用户一旦改过密码 (或任何让 session_epoch 自增的操作), 不带 epoch 的
# token 就会被拒 -> 401，因此冒烟令牌必须使用当前 epoch。
token = security.sign_token(row['id'], epoch=int(row['session_epoch']))
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
    5*|000) echo "✗ 冒烟失败: 返回 $code — 服务链路不健康" >&2
            echo "  详查: docker logs $CONTAINER --since 5m 2>&1 | grep -A 20 Traceback" >&2
            [ "$before" = "sick" ] && echo "  注意: 部署前基线已不健康，请先排除既有故障。" >&2
            return 1 ;;
    *) echo "✗ 冒烟失败: 意外状态 $code" >&2; return 1 ;;
  esac
}
smoke

# --- 闸门 6: 工作台镜像缓存有没有跟上 ------------------------------------
# 只在 ECI 后端下有意义。缓存按镜像引用创建；镜像更新后应同步刷新缓存，
# 否则工作区冷启动会出现静默性能退化。
# 不因此让部署失败: 那是性能退化而不是故障, 拦下一次正常发版不成比例。
if docker exec "$CONTAINER" sh -c '[ "$WORK_BACKEND" = eci ]' 2>/dev/null; then
  if ! docker exec -w /srv/dhc "$CONTAINER" \
        python3 -m scripts.eci_image_cache check >/dev/null 2>&1; then
    echo
    echo "⚠️  工作台镜像缓存与 WORK_IMAGE_REF 不一致，冷启动性能可能退化。" >&2
    docker exec -w /srv/dhc "$CONTAINER" \
      python3 -m scripts.eci_image_cache check 2>&1 | sed 's/^/    /' >&2
  fi
fi

echo
echo "✓ 部署完成并通过冒烟。"
