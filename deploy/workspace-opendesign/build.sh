#!/usr/bin/env bash
# 构建并发布 Open Design 工作台镜像。
#
#   ./build.sh                 # 打 profile bundle -> 建镜像 -> 自检 -> 推
#   SKIP_PUSH=1 ./build.sh     # 只建不推
#
# 两个上游, 两个钉死的版本:
#   OD_VERSION   ghcr.io/nexu-io/od 的 tag。上游漂得很快 (README 说 0.10,
#                latest 已 0.21) —— 升级前先 spike。
#   OD_REF       nexu-io/open-design 仓库里打 dsh profile bundle 用的 git ref。
#                bundle (packages/dsh-runtime) 上游**没发 npm**, 只能从源码打;
#                协议版本 (probe 的 protocol_version) 必须与 OD_VERSION 匹配。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

OD_VERSION="${OD_VERSION:-0.21.0}"
OD_REF="${OD_REF:-main}"
# dsh 版本不在这里写死 (有守卫测试盯着) —— 从生产 .env 的 WORK_IMAGE_REF 取,
# 与 dsh 工作台跑同一个版本。
ENVFILE="${ENVFILE:-$repo/deploy/prod/.env}"
DSH_IMAGE="${DSH_IMAGE:-$(grep -hoE '^WORK_IMAGE_REF=.+' "$ENVFILE" 2>/dev/null | cut -d= -f2-)}"
[ -n "$DSH_IMAGE" ] || { echo "!! 定不出 DSH_IMAGE: $ENVFILE 里没有 WORK_IMAGE_REF, 用 DSH_IMAGE= 指定" >&2; exit 1; }
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/od-local}"
TAG="${1:-${OD_VERSION}-r1}"
REF="$IMAGE:$TAG"

# --- 1. 从上游源码打 dsh profile bundle -------------------------------------
# 用 od 镜像自己的 node 来构建 (版本一致), pnpm 装进临时容器不污染镜像。
if [ ! -f open-design-dsh-runtime.tgz ] || [ "${REBUILD_BUNDLE:-0}" = "1" ]; then
  echo "==> 打 profile bundle (nexu-io/open-design @ $OD_REF)"
  work="$(mktemp -d)"
  git clone -q --depth 1 --branch "$OD_REF" https://github.com/nexu-io/open-design.git "$work/od" 2>/dev/null \
    || git clone -q --depth 1 https://github.com/nexu-io/open-design.git "$work/od"
  docker run --rm -u root -v "$work/od":/src -w /src --entrypoint sh "ghcr.io/nexu-io/od:${OD_VERSION}" -c '
    npm install -g pnpm@10 >/dev/null 2>&1
    pnpm install --filter @open-design/dsh-runtime... >/dev/null 2>&1 || true
    cd packages/dsh-runtime && pnpm build >/dev/null 2>&1
    test -f dist/index.js || { echo "!! bundle 构建失败: dist/index.js 不存在" >&2; exit 1; }
    npm pack --pack-destination /src >/dev/null 2>&1
    ls /src/open-design-dsh-runtime-*.tgz'
  cp "$work"/od/open-design-dsh-runtime-*.tgz open-design-dsh-runtime.tgz
  rm -rf "$work"
fi
echo "    bundle: $(ls -la open-design-dsh-runtime.tgz | awk '{print $5}') 字节"

# --- 2. 建镜像 ---------------------------------------------------------------
docker build \
  --build-arg OD_VERSION="$OD_VERSION" \
  --build-arg DSH_IMAGE="$DSH_IMAGE" \
  --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" \
  -t "$REF" .

# --- 3. 自检: 装 profile -> 按**守护进程真正用的方式**跑起来 ------------------
# 照的是 musl/glibc 这类"装上了但一跑就炸"的错 (sharp、hmr 都踩过), 以及 bundle
# 与 od 版本的协议不匹配。
#
# 必须跑 --stdio, 不能只跑 --probe: probe 不会走到插件栈的 hmr 那一步, 所以
# **镜像坏了它照样绿**。2026-08-30 就是这么漏出去的 —— probe 握手成功, 而
# 守护进程一 spawn 就 DSH_PROFILE_MISSING_RESULT。自检要照着线上的用法做。
echo "==> 镜像内自检"
docker run --rm -e DSH_CLOUD_TOKEN=selfcheck --entrypoint sh "$REF" -c '
  dsh plugin --profile open-design add /opt/od-profile.tgz >/dev/null 2>&1
  ln -sfn /root/.dsh/profiles/open-design/node_modules/@open-design \
    /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@open-design
  out="$(dsh --profile open-design --probe 2>&1 | head -1)"
  echo "$out" | grep -q "\"type\":\"probe\"" || { echo "!! probe 没握手: $out" >&2; exit 1; }
  echo "  ✓ probe: $out"

  log="$(echo "" | timeout 60 dsh --profile open-design --stdio 2>&1)"
  echo "$log" | grep -q "\"type\":\"ready\"" || {
    echo "!! stdio 没就绪 (守护进程就是这么起它的):" >&2
    echo "$log" | tail -20 >&2; exit 1; }
  echo "$log" | grep -qE "^Error:|failed to apply loader entry" && {
    echo "!! stdio 起来了但插件栈报错 —— 用户侧会是 DSH_PROFILE_MISSING_RESULT:" >&2
    echo "$log" | grep -E "^Error:|failed to apply loader entry|\[cause\]" | head -5 >&2
    exit 1; }
  echo "  ✓ stdio: ready, 插件栈无报错"'

# --- 4. 推 -------------------------------------------------------------------
if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推"
else
  docker push -q "$REF" >/dev/null && echo "==> 已推 $REF"
fi
echo "下一步: deploy/prod/.env 设 OPEN_DESIGN_IMAGE_REF=$REF, 跑 scripts/safe_deploy.sh"
