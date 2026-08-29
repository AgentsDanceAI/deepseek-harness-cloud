#!/usr/bin/env bash
# 只测 build.sh 的**删旧**那段, 不建镜像、不碰 docker。
#
# 为什么单拎出来测: 删错的代价是把线上正在用的镜像删掉, 而后果是冷启动退回全量
# 拉取 —— 不报错, 只是慢几分钟, 只能靠对着日志看才发现。
#
# 关键的一幕是「生产 tag 排在很后面」: 2026-08-28 第一版用 `cut -d: -f3-` 从
# .env 里取 tag, 取到的是空串 (那行按冒号只有两段), 于是"永不删生产"这条保险
# 整个失效。首次试跑时正好 KEEP 盖住了生产 tag, 什么都没发生 —— 那种"侥幸通过"
# 正是这个脚本要堵的。
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
fail=0

# 把 build.sh 里的 prod_tag_of 原样取出来用 —— 复制一份的话, 改了那边这边不会红。
eval "$(sed -n '/^prod_tag_of()/,/^}/p' "$here/build.sh")"

# 删旧那段的纯逻辑复刻: 输入 (生产tag, KEEP, tag 列表) -> 输出 保留/删除。
# docker 相关的两条 (镜像存在、有无容器在用) 不在这里测, 那要真 docker。
plan() {
  local prod="$1" keep="$2"; shift 2
  local kept=0 t
  [ -z "$prod" ] && keep=999999      # 与 build.sh 一致: 读不出生产 tag 就一个都不删
  for t in "$@"; do
    if [ "$t" = "$prod" ]; then echo "keep $t"; continue; fi
    if [ "$kept" -lt "$keep" ]; then echo "keep $t"; kept=$(( kept + 1 )); continue; fi
    echo "rmi $t"
  done
}

check() {
  local name="$1" want="$2" got="$3"
  if [ "$want" = "$got" ]; then
    echo "  ✓ $name"
  else
    echo "  ✗ $name"
    echo "      期望: $want"
    echo "      实际: $got"
    fail=1
  fi
}

# --- 1. 从 .env 里把 tag 取对 ---
env1="$(mktemp)"; printf 'FOO=1\nCOMFY_IMAGE_REF=ghcr.io/agentsdancepro/comfy-local:v0.34.1-r15\nBAR=2\n' > "$env1"
check "从 .env 读出生产 tag" "v0.34.1-r15" "$(prod_tag_of "$env1")"

env2="$(mktemp)"; printf 'COMFY_IMAGE_REF=localhost:5000/comfy:v1\n' > "$env2"
check "仓库地址带端口号 (多一个冒号) 也要取对" "v1" "$(prod_tag_of "$env2")"

env3="$(mktemp)"; printf 'WORK_IMAGE_REF=x:y\n' > "$env3"
check "没有 COMFY_IMAGE_REF 时输出空" "" "$(prod_tag_of "$env3")"

# --- 2. 生产 tag 排在 KEEP 之外时必须被保住 ---
# 这一幕就是那个 bug 的现场: 15 个 tag、生产在用的是很旧的 r3。
tags="v0.34.1-r15 v0.34.1-r14 v0.34.1-r13 v0.34.1-r4 v0.34.1-r3"
got="$(plan "v0.34.1-r3" 2 $tags | tr '\n' ' ')"
check "生产 tag 很旧时仍被保住" \
  "keep v0.34.1-r15 keep v0.34.1-r14 rmi v0.34.1-r13 rmi v0.34.1-r4 keep v0.34.1-r3 " "$got"

# --- 3. 读不出生产 tag 时一个都不删 ---
got="$(plan "" 2 $tags | grep -c '^rmi ')"
check "读不出生产 tag 时不删任何东西" "0" "$got"

# --- 4. 正常情况: 留 KEEP 个, 其余删 ---
got="$(plan "v0.34.1-r15" 2 $tags | tr '\n' ' ')"
check "常规: 保留生产 + 最近 2 个" \
  "keep v0.34.1-r15 keep v0.34.1-r14 keep v0.34.1-r13 rmi v0.34.1-r4 rmi v0.34.1-r3 " "$got"

rm -f "$env1" "$env2" "$env3"
[ "$fail" = 0 ] && echo "=== 删旧逻辑全部达标 ===" || echo "=== 有不达标项 ==="
exit "$fail"
