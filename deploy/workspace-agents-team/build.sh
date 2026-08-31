#!/usr/bin/env bash
# 构建并发布 Agents Team 工作台镜像。
#
#   ./build.sh                # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh    # 只建不推
#
# ⚠️ **必须在 x86_64 上建** (生产机 144 就是), 不要在 Apple Silicon 的 Mac 上建。
# ECI 跑的是 amd64: 在 arm64 上建出来的镜像推上去, 实例会卡在拉取/启动失败,
# 而错误里通常只说容器没起来, 不会说架构不对。
#
# ⚠️ 推完必须确认镜像**匿名可拉**: ghcr 新推的包默认私有, 而我们给 ECI 的创建参数
# 里没传 registry 凭据 —— 靠公开来拉。症状是实例卡在 Pending、镜像缓存同时 Failed,
# 而错误里一个字都不提"私有"。改可见性没有 API, 只能去网页点:
#   https://github.com/users/AgentsDancePro/packages/container/agents-team/settings
# 脚本最后那条 curl 就是钉这个的。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

IMAGE="${IMAGE:-ghcr.io/agentsdancepro/agents-team}"
TAG="${1:-0.1.0}"
REF="$IMAGE:$TAG"

# --- 1. 建 -------------------------------------------------------------------
echo "==> 建镜像 $REF"
docker build \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  -t "$REF" .

# --- 2. 自检**在镜像里跑** ---------------------------------------------------
# 不在宿主上跑: 宿主未必有 httpx/playwright (生产机就没有那个 venv), 而且在镜像里
# 跑测的正是要发出去的那份代码 —— 依赖装错、文件漏 COPY 这类问题只有这样才照得出。
# verify.py 不进镜像 (镜像只装运行需要的东西), 临时挂进去。
echo "==> 主循环自检 (在镜像里)"
docker run --rm -v "$here/verify.py:/opt/agents-team/verify.py:ro" \
  --entrypoint python "$REF" /opt/agents-team/verify.py

# --- 3. 冒烟: 容器真起来了、健康端点真回话 -----------------------------------
# 只验"镜像能跑", 不验业务 —— 业务在第 2 步验过了, 这里要抓的是"少装了个包"
# 这类只有在镜像里才暴露的问题。
echo "==> 容器冒烟"
cid="$(docker run -d --rm -p 18710:8710 "$REF")"
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18710/api/health >/dev/null 2>&1; then break; fi
  [ "$i" = 30 ] && { echo "!! 健康端点 30 秒没起来"; docker logs "$cid" | tail -20; exit 1; }
  sleep 1
done
curl -fsS http://127.0.0.1:18710/api/health | sed 's/^/    /'
# 根证书: 缺了的话智能体装任何东西都会证书错误, 而报错只出现在它的工具输出里。
docker run --rm --entrypoint sh "$REF" -c 'curl -fsS -o /dev/null https://ghcr.io/v2/' \
  || { echo "!! 镜像里 curl 走不通 HTTPS —— 根证书没装上"; exit 1; }
echo "    根证书 OK"

# Chromium 真能起来吗。**只有在镜像里才验得出**: pip 装上 playwright 不代表浏览器
# 能跑 —— 缺系统依赖时它装得上、起不来, 报错是一串找不到 .so, 而工具层只会显示成
# "浏览器操作失败", 排查方向完全指偏。不联网, 用 set_content 自己造一页。
docker run --rm --entrypoint python "$REF" -c '
import asyncio, sys
sys.path.insert(0, "/opt/agents-team")
from app import browser

async def main():
    page = await browser._ensure()
    await page.set_content("<h1>chromium-alive</h1>")
    text = await page.inner_text("body")
    await browser.shutdown()
    assert "chromium-alive" in text, text
asyncio.run(main())
' || { echo "!! 镜像里 Chromium 起不来 —— 浏览器工具会全线失效"; exit 1; }
echo "    Chromium OK"

docker rm -f "$cid" >/dev/null; trap - EXIT

# --- 4. 推 -------------------------------------------------------------------
if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
  exit 0
fi
docker push "$REF"

echo "==> 确认匿名可拉"
if curl -s "https://ghcr.io/token?scope=repository:${IMAGE#ghcr.io/}:pull&service=ghcr.io" \
   | grep -q '"token"'; then
  echo "    匿名可拉 ✓"
else
  echo "!! 这个包是**私有**的, ECI 拉不动 (会卡 Pending, 且不报私有)。" >&2
  echo "   去网页改成 public: https://github.com/orgs/AgentsDancePro/packages" >&2
  exit 1
fi
