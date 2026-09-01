#!/usr/bin/env bash
# 数字人的**通话**验收: 真打一通, 看画面动没动。在应用机上跑。
#
#   bash scripts/avatar_call_check.sh
#
# 与 visual_check.sh 的分工: 那个开页面查登录墙 (静态), 这个打电话查播放 (动态)。
# 数字人页在 visual_check 眼里一直是全绿的 —— 而那时它根本打不通电话。
#
# 会真烧一次 GPU 并扣 QA 账号约 10 积分。分两段跑的理由同 visual_check.sh:
# 判读要连数据库 (只有容器里有), 起浏览器要 docker (只有宿主有)。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
CONTAINER="${CONTAINER:-dhc-server}"
IMAGE="${IMAGE:-mcr.microsoft.com/playwright/python:v1.49.0-noble}"
OUT="${OUT:-/tmp/dsh-avatar-call}"
WORK="$(mktemp -d /tmp/dsh-call-run.XXXXXX)"
chmod 755 "$WORK"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

docker cp "$repo/server/scripts/avatar_call_check.py" "$CONTAINER:/srv/dhc/scripts/" >/dev/null

echo "==> 生成规格 (会话令牌)"
docker exec -w /srv/dhc "$CONTAINER" python3 -m scripts.avatar_call_check \
  --emit-spec /tmp/avcall "$@"
docker cp "$CONTAINER:/tmp/avcall/spec.json" "$WORK/spec.json" >/dev/null
docker cp "$CONTAINER:/tmp/avcall/driver.py" "$WORK/driver.py" >/dev/null

echo "==> 起浏览器打电话 (她要先想再合成, 最多等 60 秒)"
# 装**真 Chrome**: 镜像自带的 Chromium 没有 H.264 (专有编解码被编译掉了), 拿它
# 跑只会得到一个永远红的假故障。装一次约 30 秒。
docker run --rm -v "$WORK:/work" "$IMAGE" bash -c \
  "pip install -q playwright==1.49.0 >/dev/null 2>&1 && playwright install chrome >/dev/null 2>&1 && python /work/driver.py"

echo "==> 判读"
docker cp "$WORK/results.json" "$CONTAINER:/tmp/avcall/results.json" >/dev/null
mkdir -p "$OUT"
cp -r "$WORK/out/." "$OUT/" 2>/dev/null || true
docker exec -w /srv/dhc "$CONTAINER" python3 -m scripts.avatar_call_check \
  --read-results /tmp/avcall "$@"
rc=$?
echo
echo "截图在应用机的 $OUT/call.png"
exit $rc
