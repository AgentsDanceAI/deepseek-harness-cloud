#!/usr/bin/env bash
# 把已有的用户目录一次性推到 OSS (K8sBackend 的 OSS 同步用的同一布局:
#   oss://<bucket>/<prefix>/<hexid>/{home,workspace,...})。
#
#   bash deploy/k8s-node/migrate_to_oss.sh    # 应用机: NAS 上的 <hexid>/ (ECI 时代)
# 节点本地盘上的目录用 migrate-node-to-oss.yaml (Job, 凭据取集群 Secret)。
#
# 凭据从 ENVFILE (默认 deploy/prod/.env) 读 K8S_SYNC_OSS_*, 不打印。用 rclone copy
# --update: 目标上更新的文件不被旧的盖掉 —— 先推节点 (新), 再推 NAS (旧) 也安全。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../.." && pwd)"
ENVFILE="${ENVFILE:-$repo/deploy/prod/.env}"
# **不要 source .env**: 它不是 shell 语法 (有带空格不加引号的值), source 会当命令执行。
# 只按行取需要的几个键, 去掉两头的引号。
envget() { grep -E "^$1=" "$ENVFILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"; }
K8S_SYNC_OSS_BUCKET="${K8S_SYNC_OSS_BUCKET:-$(envget K8S_SYNC_OSS_BUCKET)}"
K8S_SYNC_OSS_PREFIX="${K8S_SYNC_OSS_PREFIX:-$(envget K8S_SYNC_OSS_PREFIX)}"
K8S_SYNC_OSS_ENDPOINT="${K8S_SYNC_OSS_ENDPOINT:-$(envget K8S_SYNC_OSS_ENDPOINT)}"
K8S_SYNC_OSS_ACCESS_KEY_ID="${K8S_SYNC_OSS_ACCESS_KEY_ID:-$(envget K8S_SYNC_OSS_ACCESS_KEY_ID)}"
K8S_SYNC_OSS_ACCESS_KEY_SECRET="${K8S_SYNC_OSS_ACCESS_KEY_SECRET:-$(envget K8S_SYNC_OSS_ACCESS_KEY_SECRET)}"
WORK_NAS_LOCAL_MOUNT_HOST="${WORK_NAS_LOCAL_MOUNT_HOST:-$(envget WORK_NAS_LOCAL_MOUNT_HOST)}"
: "${K8S_SYNC_OSS_BUCKET:?K8S_SYNC_OSS_BUCKET 未配置}"
: "${K8S_SYNC_OSS_ACCESS_KEY_ID:?}"; : "${K8S_SYNC_OSS_ACCESS_KEY_SECRET:?}"
export RCLONE_CONFIG_OSS_TYPE=s3 RCLONE_CONFIG_OSS_PROVIDER=Alibaba
export RCLONE_CONFIG_OSS_ENDPOINT="${K8S_SYNC_OSS_ENDPOINT:-oss-ap-southeast-1-internal.aliyuncs.com}"
export RCLONE_CONFIG_OSS_ACCESS_KEY_ID="$K8S_SYNC_OSS_ACCESS_KEY_ID"
export RCLONE_CONFIG_OSS_SECRET_ACCESS_KEY="$K8S_SYNC_OSS_ACCESS_KEY_SECRET"
REMOTE="oss:${K8S_SYNC_OSS_BUCKET}/${K8S_SYNC_OSS_PREFIX:-dshwork}"
FLAGS=(--metadata --links --fast-list --transfers 16 --checkers 32 --s3-directory-markers --create-empty-src-dirs --exclude '/.dsh-*' --update --stats-one-line --stats 30s)

SRC="${WORK_NAS_LOCAL_MOUNT_HOST:-/mnt/dshwork-nas}"
[ -d "$SRC" ] || { echo "!! $SRC 不存在 (NAS 没挂?)" >&2; exit 1; }
echo "==> $SRC -> $REMOTE"
n=0
for d in "$SRC"/*/; do
  hexid="$(basename "$d")"
  case "$hexid" in testhex|lost+found|.*) continue ;; esac
  echo "--- $hexid"
  rclone copy "$d" "$REMOTE/$hexid" "${FLAGS[@]}"
  n=$((n + 1))
done
echo "==> 推了 $n 个用户目录到 $REMOTE"
