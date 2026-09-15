#!/usr/bin/env bash
# 构建 claude-code / codex 两格的工作台镜像。
#
# 上游是社区的 pi-web-ui (MIT, xing-shuyin)。我们**不 fork**, 照桌面版那套
# (desktop/patches/) 把改动存成补丁, 构建时打到钉住的上游提交上:
#   patches/0001-cli-engine.patch      第三个引擎 (server/cli/)
#   patches/0002-engine-branding.patch 界面按引擎显示名字
#   patches/0003-engine-tests.patch    适配层与会话读取的测试
#
# **git apply 失败就整个失败。** 不加 --3way 也不加 --reject: 半打上的补丁会
# 产出一个"能构建、行为不对"的镜像, 那比构建失败难查一百倍。
set -euo pipefail
cd "$(dirname "$0")"

# VARIANT=openmanus: 不建外壳, 把已推到 ghcr 的外壳叠到 agent-frameworks 上
# (见 Dockerfile.openmanus)。先建/推外壳 (无 VARIANT), 再建这个 —— 它按 SHELL_REF
# 从 ghcr 拉外壳那一层, 本机没推过的层叠不上去。
VARIANT="${VARIANT:-}"
# 目标架构。默认 amd64 (线上节点全是 amd64), 但桌面端把这些镜像跑在**用户自己的
# 机器**上 —— Apple Silicon 上跑 amd64 要过 Rosetta, 实测 node 冷启动 27ms → 208ms,
# sha256 慢 8 倍 (Rosetta 用不上 ARM 的加密指令)。所以 arm64 那份也要有。
# 两份**各自在原生机器上建**再合成一个 manifest list (见文件末尾的说明); 用 buildx
# 跨架构建等于全程 QEMU, 光 apt + npm 就要几十分钟。
PLATFORM="${PLATFORM:-linux/amd64}"
REPO=$(python3 -c "import json;print(json.load(open('upstream.json'))['repository'])")
COMMIT=$(python3 -c "import json;print(json.load(open('upstream.json'))['commit'])")
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/workspace-cli}"
# 标签 = <上游版本>-r<修订号>。**补丁改了就把 upstream.json 的 revision +1** ——
# 同一个标签重推, 已经拉过那层的节点不会再拉 (imagePullPolicy 不是 Always),
# 结果是有的节点跑新的有的跑旧的, 而两边都"正常"。
TAG="${TAG:-$(python3 -c "import json;d=json.load(open('upstream.json'));print(f\"{d['version']}-r{d['revision']}\")")}"

if [ "$VARIANT" = "openmanus" ]; then
  # 变体放在**同一个包**里, 靠标签后缀区分 (node:22-slim 那种写法), 不另开
  # workspace-cli-openmanus 包: ghcr 新包默认私有, 节点匿名拉不动, 还得有人去网页
  # 上点一次"公开"; 老包早就公开了, 新标签推上去当场能拉。
  OM_TAG="$TAG-openmanus"
  FRAMEWORKS_REF="${FRAMEWORKS_REF:-$(python3 -c "import json;print(json.load(open('upstream.json'))['frameworks_ref'])")}"
  echo "==> 构建 $IMAGE:$OM_TAG  (外壳 $IMAGE:$TAG 叠在 $FRAMEWORKS_REF 上)"
  docker build --platform "$PLATFORM" -f Dockerfile.openmanus \
    --build-arg "SHELL_REF=$IMAGE:$TAG" --build-arg "FRAMEWORKS_REF=$FRAMEWORKS_REF" \
    --build-arg "REVISION=$OM_TAG" -t "$IMAGE:$OM_TAG" .
  echo "==> 完成: $IMAGE:$OM_TAG"
  echo "    推送: docker push $IMAGE:$OM_TAG"
  exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "==> 检出上游 $COMMIT"
git clone -q "$REPO" "$WORK/src"
git -C "$WORK/src" checkout -q "$COMMIT"

echo "==> 打补丁"
for p in patches/*.patch; do
  git -C "$WORK/src" apply --check "$PWD/$p"
  git -C "$WORK/src" apply "$PWD/$p"
  echo "    applied $(basename "$p")"
done

cp Dockerfile "$WORK/src/Dockerfile.aistore"
echo "==> 构建 $IMAGE:$TAG"
docker build --platform "$PLATFORM" -f "$WORK/src/Dockerfile.aistore" -t "$IMAGE:$TAG" "$WORK/src"
echo "==> 完成: $IMAGE:$TAG"
echo "    推送: docker push $IMAGE:$TAG"

# ── 多架构 (2026-09-15) ────────────────────────────────────────────────────
# 线上节点全是 amd64, 但桌面端把同一个镜像跑在**用户自己的机器**上。Apple
# Silicon 上跑 amd64 要过 Rosetta, 本机实测 (M 系, Docker Desktop 29.2.1,
# 已开 Rosetta):
#     node 冷启动      12ms → 208ms   (17 倍)
#     sha256 200MB     87ms → 747ms   (8.6 倍, Rosetta 用不上 ARM 的加密指令)
#     容器起到首页 200  879ms → 1499ms
# Docker Desktop 会在货架上挂一个橙色 AMD64 角标: "may have poor performance,
# or fail, if run via emulation" —— 老板 2026-09-15 截的就是它。
#
# 两份**各自在原生机器上建**, 再把两个 digest 合成一个 manifest list。别用
# buildx 跨架构建: 那是全程 QEMU, 光 apt + npm 就要几十分钟。
#
#   # amd64 (在 144 上, 或任何 amd64 机器)
#   PLATFORM=linux/amd64 TAG=<tag> bash build.sh && docker push $IMAGE:<tag>
#   # arm64 (在这台 Mac 上)
#   PLATFORM=linux/arm64 TAG=<tag>-arm64 bash build.sh && docker push $IMAGE:<tag>-arm64
#   # 合成 (两个都推完之后)
#   docker buildx imagetools create -t $IMAGE:<tag> \
#     $IMAGE@<amd64 digest> $IMAGE@<arm64 digest>
#
# **合成时 amd64 那一份要按 digest 引用已经推上去的那个**, 不要重建: digest 不变
# 线上节点就不会重拉 (crictl 答 "Image is up to date"), 也不会出现"有的节点跑新的
# 有的跑旧的"。
#
# 推 ghcr 的凭据: 这台 Mac 的 docker-credential-desktop 会无限挂起, 要
#   export DOCKER_CONFIG=$(mktemp -d); export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock
#   gh auth token | docker login ghcr.io -u AgentsDancePro --password-stdin
