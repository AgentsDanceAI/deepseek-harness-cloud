#!/usr/bin/env bash
# 云产品的**动手**验收: 真敲命令 / 真发消息, 看有没有真回应。在应用机上跑。
#
#   bash scripts/product_use_check.sh                    # 全部会用的产品
#   bash scripts/product_use_check.sh openmanus crewai   # 只试这几个
#
# 与 visual_check.sh 的分工: 那个开页面查登录墙 (首屏), 这个动手查能不能用。
# 2026-09-01 老板逐个点开新接的四个产品, 四个全废, 而 visual_check 对它们全报 ✓
# —— 因为毛病全都发生在用户动手之后。
#
# 会真起容器、真调模型 (扣 QA 账号一点积分)。分两段跑的理由同 visual_check.sh:
# 判读要连数据库 (只有容器里有), 起浏览器要 docker (只有宿主有)。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
CONTAINER="${CONTAINER:-dhc-server}"
IMAGE="${IMAGE:-mcr.microsoft.com/playwright/python:v1.49.0-noble}"
OUT="${OUT:-/tmp/dsh-use}"
WORK="$(mktemp -d /tmp/dsh-call-run.XXXXXX)"
chmod 755 "$WORK"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

docker cp "$repo/server/scripts/product_use_check.py" "$CONTAINER:/srv/dhc/scripts/" >/dev/null

echo "==> 生成规格 (会话令牌 + 每个产品怎么动手)"
docker exec -w /srv/dhc "$CONTAINER" python3 -m scripts.product_use_check \
  --emit-spec /tmp/usecheck "$@"
docker cp "$CONTAINER:/tmp/usecheck/spec.json" "$WORK/spec.json" >/dev/null
docker cp "$CONTAINER:/tmp/usecheck/driver.py" "$WORK/driver.py" >/dev/null

echo "==> 起浏览器逐个动手 (冷启动 + 真调模型, 每个最多两分钟)"
# 装**真 Chrome**: 镜像自带的 Chromium 没有 H.264 (专有编解码被编译掉了), 拿它
# 跑只会得到一个永远红的假故障。装一次约 30 秒。
docker run --rm -v "$WORK:/work" "$IMAGE" bash -c \
  "pip install -q playwright==1.49.0 >/dev/null 2>&1 && playwright install chrome >/dev/null 2>&1 && python /work/driver.py"

echo "==> 判读"
docker cp "$WORK/results.json" "$CONTAINER:/tmp/usecheck/results.json" >/dev/null
mkdir -p "$OUT"
cp -r "$WORK/out/." "$OUT/" 2>/dev/null || true
docker exec -w /srv/dhc "$CONTAINER" python3 -m scripts.product_use_check \
  --read-results /tmp/usecheck "$@"
rc=$?
echo
echo "截图在应用机的 $OUT/ —— **一定要看图**"
exit $rc
