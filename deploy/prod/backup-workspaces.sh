#!/usr/bin/env bash
# 每日把用户工作台文件 (NAS) 同步到 Cloudflare R2。
#
# 为什么需要: 数据库有异地备份, **用户自己做出来的东西没有**。切到 ECI 之后
# 那些文件只存在于 NAS 一处 —— 本机卷已经删了。NAS 有多副本和 3 天回收站,
# 但整个文件系统出事就没有第二条路, 而丢的是用户几周的产出。
#
# 加密, 不商量。这个部署唯一有令牌的 R2 桶是 dsh-releases, 而它**publish 在
# https://dl.dshcloud.online** —— 写进去的对象任何人按路径都能取。数据库备份
# 曾以明文放了两天 (账号、邮箱、口令哈希、订单), 这里不能重演。
# 用 rclone 的 crypt 远端: 内容和**文件名**都加密, 所以连"某用户有个叫
# 报价单.xlsx 的文件"都不泄露。
#
# 增量同步而不是每天打整包: 今天 190MB, 但这个数只会涨, 每天一份全量在保留期
# 内会乘以天数。
#
#   BACKUP_PASSPHRASE  — 与数据库备份共用同一把, 存在 .env, 并且**另存一份在
#                        这台机器之外**。丢了这把, 备份等于没有。
set -euo pipefail

cd "$(dirname "$0")"

# 只挑需要的键读, 不整份 source: .env 里有带空格且未加引号的值 (例如
# LEGAL_ENTITY_ZH=AgentsDance AI), 直接 source 会把它当命令执行然后整个脚本
# 挂掉。已有的 backup-db.sh 就是这么绕的, 这里照同一套。
# 已存在的同名环境变量优先 —— 测试时可以从外面覆盖 (护栏用例靠它)。
[ -f .env ] || { echo "!! 同目录下没有 .env" >&2; exit 1; }
while IFS= read -r line; do
  case "$line" in
    R2_*=*|BACKUP_PASSPHRASE=*|WORK_NAS_LOCAL_MOUNT_HOST=*|WORKSPACE_BACKUP_*=*)
      name="${line%%=*}"
      eval "current=\${$name:-}"
      [ -n "$current" ] || export "${name}=${line#*=}"
      ;;
  esac
done < .env

: "${BACKUP_PASSPHRASE:?refusing to run without a passphrase (see header)}"
: "${R2_ACCOUNT_ID:?}"; : "${R2_ACCESS_KEY_ID:?}"; : "${R2_SECRET_ACCESS_KEY:?}"
: "${R2_BUCKET:?}"
SRC="${WORK_NAS_LOCAL_MOUNT_HOST:-/mnt/dshwork-nas}"
PREFIX="${WORKSPACE_BACKUP_PREFIX:-backups/workspaces}"

command -v rclone >/dev/null || { echo "!! 缺 rclone" >&2; exit 1; }

# --- 三道护栏, 全是为了同一件事: sync 会删目标端多余的文件 -------------------
# NAS 没挂上时源目录是个空的本地目录, 一次 sync 就能把整份备份删干净 ——
# 而且不会报错, 它会认为"用户把文件都删了"。
mountpoint -q "$SRC" || { echo "!! $SRC 不是挂载点, 拒绝同步 (NAS 可能没挂上)" >&2; exit 1; }
files="$(find "$SRC" -type f 2>/dev/null | wc -l)"
[ "$files" -gt 0 ] || { echo "!! $SRC 下一个文件都没有, 拒绝同步" >&2; exit 1; }
echo "==> 源: $SRC  ($files 个文件, $(du -sh "$SRC" 2>/dev/null | cut -f1))"

export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

# crypt 远端叠在 R2 之上。obscure 只是混淆而不是加密, 但真正的数据加密用的是
# 口令本身; 混淆值与口令同等对待即可。
export RCLONE_CONFIG_SEC_TYPE=crypt
export RCLONE_CONFIG_SEC_REMOTE="R2:$R2_BUCKET/$PREFIX"
RCLONE_CONFIG_SEC_PASSWORD="$(rclone obscure "$BACKUP_PASSPHRASE")"
export RCLONE_CONFIG_SEC_PASSWORD
export RCLONE_CONFIG_SEC_FILENAME_ENCRYPTION=standard
export RCLONE_CONFIG_SEC_DIRECTORY_NAME_ENCRYPTION=true

# --max-delete: 第三道护栏。前两道挡的是"源没了", 这道挡的是"源还在但内容异常
# 消失" (比如某次迁移把目录搬走了)。宁可同步失败, 也不要把备份跟着删掉。
echo "==> 同步到 r2://$R2_BUCKET/$PREFIX/ (内容与文件名均加密)"
rclone sync "$SRC" "SEC:" \
  --max-delete "${WORKSPACE_BACKUP_MAX_DELETE:-50}" \
  --transfers 4 --checkers 8 --s3-chunk-size 16M \
  --exclude '**/node_modules/**' --exclude '**/.npm/**' \
  --exclude '**/.git/**' --exclude '**/__pycache__/**' \
  --header-upload "Cache-Control: no-store, private" \
  --stats-one-line --stats 30s

echo "==> 校验"
n="$(rclone size "SEC:" --json 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["count"])' || echo '?')"
echo "    远端 $n 个对象"
echo
echo "✓ 工作台文件已备份 (加密)。恢复: rclone copy SEC: <目标目录> (同样的环境变量)"
