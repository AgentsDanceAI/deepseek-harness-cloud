#!/usr/bin/env bash
# 构建并发布 Operator 工作台镜像。
#
#   ./build.sh                # 建 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh    # 只建不推
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

# --- 1. 先跑自检, 再建镜像 ---------------------------------------------------
# 主循环坏了的话镜像建得再顺也没用, 而且那种坏法要到用户第一次说话才暴露。
echo "==> 主循环自检"
"$repo/server/.venv/bin/python" "$here/verify.py"

# --- 2. 建 -------------------------------------------------------------------
echo "==> 建镜像 $REF"
docker build \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  -t "$REF" .

# --- 3. 冒烟: 容器真起来了、健康端点真回话 -----------------------------------
# 只验"镜像能跑", 不验业务 —— 业务在第 1 步验过了, 这里要抓的是"少装了个包"
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
