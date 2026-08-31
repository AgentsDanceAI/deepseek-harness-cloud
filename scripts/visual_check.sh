#!/usr/bin/env bash
# 云工作台的**视觉**验收: 逐个真开页面、截图、查登录墙。在应用机上跑。
#
#   bash scripts/visual_check.sh                 # 全部已启用产品
#   bash scripts/visual_check.sh openclaw dify   # 只看这几个
#
# 为什么需要它: 我们的产品几乎都自带账号体系, 我们靠注入配置或会话把它们的登录墙
# 拆掉 —— 而这类拆除**只有真渲染出来才看得见是否成功**。登录墙、首跑向导、错误页
# 全都是 HTTP 200, SPA 的 HTML 在登录前后又长得一模一样。2026-08-31 一天之内
# 我在六个产品上重复踩同一类坑, 每次都是"接口全绿 → 老板打开一看是墙"。
#
# 分两段跑是被迫的: 判读逻辑要连数据库和配置 (只有 dhc-server 容器里有), 而起
# 浏览器要 docker (只有宿主有)。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
CONTAINER="${CONTAINER:-dhc-server}"
IMAGE="${IMAGE:-mcr.microsoft.com/playwright/python:v1.49.0-noble}"
OUT="${OUT:-/tmp/dsh-visual}"
WORK="$(mktemp -d /tmp/dsh-visual-run.XXXXXX)"
chmod 755 "$WORK"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# 让脚本的最新版进到容器里 —— 免得改了本地却跑着旧的 (踩过)
docker cp "$repo/server/scripts/workspace_visual_check.py" "$CONTAINER:/srv/dhc/scripts/" >/dev/null

echo "==> 生成规格 (会话令牌 + 产品清单)"
docker exec -w /srv/dhc "$CONTAINER" python3 -m scripts.workspace_visual_check \
  --emit-spec /tmp/visual "$@"
docker cp "$CONTAINER:/tmp/visual/spec.json" "$WORK/spec.json" >/dev/null
docker cp "$CONTAINER:/tmp/visual/driver.py" "$WORK/driver.py" >/dev/null

echo "==> 起浏览器 (冷启动的产品要等, 最多 3 分钟一个)"
# 这个镜像只带**浏览器二进制和系统依赖**(重的那部分), playwright 的 Python 包
# 要自己装 —— 官方定位是"CI 里 pip install 你自己那份"。装它只要几秒。
docker run --rm -v "$WORK:/work" "$IMAGE" bash -c \
  "pip install -q playwright==1.49.0 >/dev/null 2>&1 && python /work/driver.py"

echo "==> 判读"
docker cp "$WORK/results.json" "$CONTAINER:/tmp/visual/results.json" >/dev/null
mkdir -p "$OUT"
cp -r "$WORK/out/." "$OUT/" 2>/dev/null || true
# 判读要连配置, 所以回容器里做; 截图留在宿主的 $OUT 供人看。
docker exec -w /srv/dhc "$CONTAINER" python3 -m scripts.workspace_visual_check \
  --read-results /tmp/visual --out /tmp/visual/out "$@"
rc=$?
echo
echo "截图在应用机的 $OUT/ —— **一定要看图**, 关键词匹配只是线索不是判决。"
exit $rc
