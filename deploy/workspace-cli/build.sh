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

REPO=$(python3 -c "import json;print(json.load(open('upstream.json'))['repository'])")
COMMIT=$(python3 -c "import json;print(json.load(open('upstream.json'))['commit'])")
IMAGE="${IMAGE:-ghcr.io/agentsdancepro/workspace-cli}"
# 标签 = <上游版本>-r<修订号>。**补丁改了就把 upstream.json 的 revision +1** ——
# 同一个标签重推, 已经拉过那层的节点不会再拉 (imagePullPolicy 不是 Always),
# 结果是有的节点跑新的有的跑旧的, 而两边都"正常"。
TAG="${TAG:-$(python3 -c "import json;d=json.load(open('upstream.json'));print(f\"{d['version']}-r{d['revision']}\")")}"

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
docker build --platform linux/amd64 -f "$WORK/src/Dockerfile.aistore" -t "$IMAGE:$TAG" "$WORK/src"
echo "==> 完成: $IMAGE:$TAG"
echo "    推送: docker push $IMAGE:$TAG"
