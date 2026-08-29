#!/usr/bin/env bash
# 构建并发布 ComfyUI 工作台镜像, **顺手删掉本机的旧 tag**。
#
#   ./build.sh              # 自动取下一个 rN, 建 -> 自检 -> 推 -> 删旧
#   ./build.sh v0.34.1-r20  # 指定 tag
#   KEEP=4 ./build.sh       # 多留几个回滚位 (默认 2)
#   SKIP_PUSH=1 ./build.sh  # 只建不推 (本地试)
#
# 为什么要有这个脚本: 这个镜像 3.85GB, 而这条线一天能出十来个 tag。手敲
# build+push 是不会顺手删旧的 —— 2026-08-28 就这么把 144 的盘撑满了 (15 个 tag
# 占 53GB)。删旧必须和构建绑在同一条命令里, 否则总会忘。
#
# 删的只是**本机**镜像。生产跑在 ECI 上, 从 ghcr 拉 + 走 ECI 镜像缓存, 本机
# 这份只用于构建与自检; ghcr 上的 tag 也都还在, 真要回滚随时拉得回来。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$here"

IMAGE="${IMAGE:-ghcr.io/agentsdancepro/comfy-local}"
KEEP="${KEEP:-2}"                       # 保留最近几个本机 tag (含刚建的这个)
ENVFILE="${ENVFILE:-$repo/deploy/prod/.env}"

# 从 .env 里读出生产在用的 tag。取**最后一个冒号之后**的部分 ——
# 一行是 COMFY_IMAGE_REF=ghcr.io/…/comfy-local:v0.34.1-r15, 按冒号切只有两段,
# 早先写的 `cut -d: -f3-` 取到的是空串。空串意味着"没有要保护的 tag", 于是
# 这段会**把线上正在用的镜像删掉** —— 首次试跑时正好被 KEEP 盖住才没出事。
prod_tag_of() {
  local line ref
  line="$(grep -hoE '^COMFY_IMAGE_REF=.+' "$1" 2>/dev/null | head -1 || true)"
  ref="${line#COMFY_IMAGE_REF=}"
  [ "$ref" = "$line" ] && return 0     # 没匹配到就什么都不输出
  printf '%s' "${ref##*:}"             # 最后一个冒号之后 = tag
}

# --- 定 tag ------------------------------------------------------------------
if [ $# -ge 1 ]; then
  TAG="$1"
else
  # 取现有最大的 rN 加一。只看本机 —— 本机没有就从 ghcr 上那个当前生产 tag 续,
  # 两边都没有才从 r1 起。
  base="$(prod_tag_of "$ENVFILE")"
  latest="$(docker images "$IMAGE" --format '{{.Tag}}' | grep -oE 'r[0-9]+$' | tr -d r | sort -n | tail -1 || true)"
  cur="$(printf '%s\n' "$base" | grep -oE 'r[0-9]+$' | tr -d r || true)"
  n=$(( ${latest:-0} > ${cur:-0} ? ${latest:-0} : ${cur:-0} ))
  TAG="v0.34.1-r$(( n + 1 ))"
fi
REF="$IMAGE:$TAG"
echo "==> 目标镜像 $REF"

# --- 建 ----------------------------------------------------------------------
docker build --build-arg REVISION="$(git -C "$repo" rev-parse --short HEAD)" -t "$REF" .

# --- 自检: 用**镜像里烤好的那份垫片**, 不是工作树的副本 -----------------------
# 这一步过去每次都是手动跑的, 手动就有忘的一天。镜像里的垫片与工作树不一致时
# (COPY 漏了、构建缓存吃了旧层), 只有这样才照得出来。
echo "==> 镜像内自检"
docker run --rm -v "$here":/chk -w /chk --entrypoint sh "$REF" -c '
  ADVERTISE_HOST=localhost python3 stub_gateway.py >/dev/null 2>&1 &
  sleep 1
  DSH_CLOUD_VIDEO_BASE=http://127.0.0.1:9797/llm/v1 DSH_CLOUD_TOKEN=t \
    python3 /opt/dsh-api-shim.py >/dev/null 2>&1 &
  sleep 2
  python3 shim_check.py'

# --- 推 ----------------------------------------------------------------------
if [ "${SKIP_PUSH:-0}" = "1" ]; then
  echo "==> SKIP_PUSH=1, 不推 ghcr"
else
  docker push -q "$REF" >/dev/null
  echo "==> 已推 $REF"
fi

# --- 删旧 --------------------------------------------------------------------
# 三条保险, 缺一不可:
#   1. 生产 .env 引用的那个永远不删 (哪怕它很旧)
#   2. 有容器 (含 exited) 在用的不删 —— 那多半是谁正在调试
#   3. 其余按 rN 倒序留 KEEP 个
echo "==> 删旧 tag (保留最近 $KEEP 个)"
prod_tag="$(prod_tag_of "$ENVFILE")"
if [ -n "$prod_tag" ]; then
  echo "    生产在用: $prod_tag (永不删)"
else
  # 读不出来就**一个都不删**。宁可留着占盘, 也不能在"不知道线上用哪个"的情况下
  # 动手 —— 删错的代价是线上冷启动退回全量拉取, 而且没有任何报错。
  echo "    !! 读不出生产 tag ($ENVFILE) —— 保险起见跳过删旧" >&2
  KEEP=999999
fi

mapfile -t all < <(docker images "$IMAGE" --format '{{.Tag}}' \
  | grep -E 'r[0-9]+$' | sort -t r -k2 -n -r)
kept=0
for t in "${all[@]}"; do
  if [ "$t" = "$prod_tag" ]; then
    echo "    保留 $t (生产)"
    continue
  fi
  if [ "$kept" -lt "$KEEP" ]; then
    echo "    保留 $t"
    kept=$(( kept + 1 ))
    continue
  fi
  users="$(docker ps -a --filter "ancestor=$IMAGE:$t" -q | wc -l | tr -d ' ')"
  if [ "$users" != "0" ]; then
    echo "    保留 $t ($users 个容器在用 —— 有人在调试, 不动)"
    continue
  fi
  docker rmi "$IMAGE:$t" >/dev/null 2>&1 && echo "    删除 $t" || echo "    !! $t 删除失败"
done

echo
echo "==> 现存 tag:"
docker images "$IMAGE" --format '    {{.Tag}}  {{.Size}}  {{.CreatedSince}}'
echo "==> 磁盘:"
df -h / | tail -1 | awk '{print "    / 已用 " $3 " / " $2 " (" $5 "), 可用 " $4}'
echo
echo "下一步: 改 deploy/prod/.env 的 COMFY_IMAGE / COMFY_IMAGE_REF 到 $TAG,"
echo "        再跑 scripts/safe_deploy.sh (它会在切换前先备好 ECI 镜像缓存)。"
