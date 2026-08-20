#!/usr/bin/env bash
# 把 docker 后端的每用户卷搬到 NAS, 供 ECI 后端接手。
#
#   dshwork-home-<hexid>  ->  $NAS/<hexid>/home
#   dshwork-ws-<hexid>    ->  $NAS/<hexid>/workspace
#
# 为什么需要: ECI 没有"停止但保留", 闲置回收就是删实例, 用户的东西只能活在
# NAS 上。切 WORK_BACKEND=eci 而不搬, 存量用户会打开一个空工作台 —— 文件没丢
# (还在卷里), 但他看不见, 而这比真丢了更难解释。
#
# 用法: bash deploy/prod/migrate-volumes-to-nas.sh [--apply]
#   不带 --apply 只体检并打印计划, 什么都不动。
set -euo pipefail
NAS="${NAS:-/mnt/dshwork-nas}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

command -v docker >/dev/null || { echo "没有 docker"; exit 1; }
mountpoint -q "$NAS" || { echo "❌ $NAS 不是挂载点 —— 先按 /etc/fstab 挂上 NAS 再来"; exit 1; }
touch "$NAS/.wtest" 2>/dev/null && rm -f "$NAS/.wtest" || { echo "❌ $NAS 不可写"; exit 1; }

plan=(); skip=()
for vol in $(docker volume ls --format '{{.Name}}' | grep -E '^dshwork-(home|ws)-' | sort); do
  case "$vol" in
    dshwork-home-*) hexid=${vol#dshwork-home-}; sub=home ;;
    dshwork-ws-*)   hexid=${vol#dshwork-ws-};   sub=workspace ;;
  esac
  # 跑着的工作台正在写这些文件, 拷出来的东西不可信
  if [ -n "$(docker ps -q --filter "name=^dshwork-${hexid}$" 2>/dev/null)" ]; then
    skip+=("$vol (容器在运行, 先停掉)"); continue
  fi
  dst="$NAS/$hexid/$sub"
  # 幂等护栏: 目标非空说明搬过了 (或用户已经在 ECI 上产出了新东西)。
  # 覆盖会静默吞掉后者, 所以宁可跳过让人来判断。
  if [ -d "$dst" ] && [ -n "$(ls -A "$dst" 2>/dev/null)" ]; then
    skip+=("$vol -> $dst (目标非空)"); continue
  fi
  plan+=("$vol|$hexid|$sub")
done

echo "==> 计划搬运 ${#plan[@]} 个卷"
for p in "${plan[@]:-}"; do
  [ -z "$p" ] && continue
  IFS='|' read -r vol hexid sub <<<"$p"
  n=$(docker run --rm -v "$vol":/v:ro alpine:3 sh -c 'find /v -type f 2>/dev/null | wc -l')
  echo "    $vol  ($n 个文件)  ->  $NAS/$hexid/$sub"
done
for s in "${skip[@]:-}"; do [ -n "$s" ] && echo "    跳过: $s"; done

if [ "$APPLY" != "1" ]; then
  echo
  echo "(体检模式, 什么都没动。加 --apply 真正执行)"
  exit 0
fi

echo
fail=0
for p in "${plan[@]:-}"; do
  [ -z "$p" ] && continue
  IFS='|' read -r vol hexid sub <<<"$p"
  src_n=$(docker run --rm -v "$vol":/v:ro alpine:3 sh -c 'find /v -type f 2>/dev/null | wc -l')
  mkdir -p "$NAS/$hexid/$sub"
  # 源只读挂载; -a 保住权限与时间戳 (dsh 的会话文件按 mtime 排序)
  docker run --rm -v "$vol":/from:ro -v "$NAS/$hexid/$sub":/to alpine:3 \
    sh -c 'cd /from && cp -a . /to/'
  dst_n=$(find "$NAS/$hexid/$sub" -type f 2>/dev/null | wc -l)
  if [ "$src_n" -eq "$dst_n" ]; then
    echo "    ✅ $vol  $src_n -> $dst_n 个文件"
  else
    echo "    ❌ $vol  文件数对不上: $src_n -> $dst_n"; fail=1
  fi
done

echo
if [ "$fail" = 0 ]; then
  echo "✅ 全部一致。原卷未动 —— 回滚就是把 WORK_BACKEND 改回 docker。"
  echo "   观察确认无碍后再删卷。"
else
  echo "❌ 有对不上的, 不要切 WORK_BACKEND, 先查。"
  exit 1
fi
