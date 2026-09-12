#!/usr/bin/env bash
# Claude Code 的请求形状经我们这条链路还通不通 —— 在**应用机**上跑, cron 每天一次。
#
# 为什么要有它: 这条链路的故障是"一半一半"的, 而且**不在我们这边**。上游中继把同一
# 个牌名按请求轮询到几家后端, 各家对 Claude Code 的 body 各有各的不收 (Bedrock 拒
# thinking.type=enabled, 另一家拒 context_management, 只有直连 Anthropic 全收)。
# 2026-09-11 实测 claude-sonnet-5 原样只有 3/6 通过, 而:
#   · 网关日志里只是几条 400, 没人会盯;
#   · 用户侧的表现是"发一条有时有回应有时没有", 最容易被当成自己网络不好;
#   · 我们这边一行代码都没改过, 上游换一家权重就会复发。
# 修法是按 thinking 钉 `-thinking` 通道 (见 memory anthropic-face-thinking-channel),
# 而那是**建立在上游当前行为上的**假设 —— 所以要有一条每天自己说话的巡检。
#
# **一次成功证明不了任何事**: 轮询意味着单发是抽样。所以按组重复采样, 看通过率。
# 这条会真花钱: 每天 2 个型号 × 6 发的短请求, 量级可以忽略, 但不是零。
set -uo pipefail
STATE=/var/tmp/dsh-anthropic-face; mkdir -p "$STATE"
ALERT=/data/workspace/AgentsDanceCloud/scripts/alert_mail.sh
APP_CONTAINER="${APP_CONTAINER:-dhc-server}"
PROBE=/srv/dhc/scripts/probe_anthropic_face.py   # 镜像里的路径 (server/ 挂进容器)
MODELS="${MODELS:-claude-sonnet-5,claude-fable-5}"
TIMES="${TIMES:-6}"
FLOOR="${FLOOR:-0.9}"                            # 低于它就发信
TS="$(date '+%F %T %Z')"

# **打我们自己的网关**, 不是直打上游 —— 要测的是用户真正走的那条路 (含钉通道那一步)。
#
# 令牌两个坑, 都踩过:
#  · _mint_workspace_token 会**撤掉同一个 (用户, 工作台) 的上一份令牌**。拿真产品
#    去铸, 就等于每天把那个人正在跑的工作台踢下线 —— 容器还在、界面照常, 只是往
#    网关发的每一发都 401, 没有任何提示 (2026-08-28 线上踩过这个形状)。
#    所以这里用一个**合成的产品 id**, 工作台键 `<用户>::__face_probe__` 没有任何
#    容器在用, 撤的永远只是上一次巡检自己的令牌。
#  · 别拿 `ORDER BY id LIMIT 1` 当"我们的账号" —— 那会挑到某个真实第三方用户。
#    认准 ADMIN_EMAILS 的第一个 (我们自己的运营账号), 找不到就跳过, 不乱花别人的钱。
out="$(docker exec "$APP_CONTAINER" python -c "
import dataclasses, os, runpy, sys
from app import config, db, products, workspace

admin = (config.ADMIN_EMAILS or [None])[0]
if not admin:
    print('SKIP 没配 ADMIN_EMAILS, 不替别人花钱'); raise SystemExit(0)
row = db.query_one('SELECT * FROM users WHERE lower(email)=?', (admin,))
if not row:
    print('SKIP 运营账号 %s 不在库里' % admin); raise SystemExit(0)

probe_product = dataclasses.replace(products.get('claude-code'), id='__face_probe__')
os.environ['DSH_GATEWAY_BASE'] = config.PUBLIC_BASE.rstrip('/')
os.environ['DSH_CLOUD_TOKEN'] = workspace._mint_workspace_token(dict(row), probe_product)
sys.argv = ['probe', '-m', '$MODELS', '-n', '$TIMES', '--fail-under', '$FLOOR']
runpy.run_path('$PROBE', run_name='__main__')
" 2>&1)"
rc=$?
printf '%s\n' "$out" | sed "s/^/$TS  /"

if [ $rc -eq 0 ]; then
  rm -f "$STATE/alerted"
  exit 0
fi
body="Claude Code 链路巡检不通过 @ $TS

$out

含义: 上游中继大概率换了后端权重, 或 -thinking 通道选择器失效了。
先跑 --upstream --variants 定位是哪一样被拒 (在 $APP_CONTAINER 里):
  docker exec $APP_CONTAINER python $PROBE --upstream --variants -m claude-sonnet-5 -n 6
背景与修法: memory anthropic-face-thinking-channel
巡检脚本: deploy 机 /data/workspace/deepseek-harness-cloud/scripts/watch_anthropic_face.sh"
sig="$(printf '%s' "$out" | md5sum | cut -c1-8)"
if [ "$(cat "$STATE/alerted" 2>/dev/null)" != "$sig" ] && [ -x "$ALERT" ]; then
  printf '%s\n' "$body" | "$ALERT" "Claude Code 链路: 通过率跌破 $FLOOR" >/dev/null 2>&1 \
    && echo "$sig" > "$STATE/alerted"
fi
exit 1
