#!/usr/bin/env bash
# aistore.best cutover: bring up DHC and (re)write the DHC site blocks in the
# shared Caddy (aistore.best + work.aistore.best 的站点块)。
# Idempotent; safe to re-run. Run from the repo root.
#
#   bash deploy/prod/cutover.sh
#
# Prereqs: deploy/prod/.env filled in; the shared Caddy container ($CADDY_CTR)
# running (it owns 80/443 and the domains' TLS + Cloudflare origin).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE="$REPO/deploy/prod/compose.yml"
ENVFILE="$REPO/deploy/prod/.env"
# 这两个是**逐机器**的: 共享 Caddy 的配置文件路径与容器名。写死会让换机迁移时
# 传进来的值被静默忽略 —— 脚本照旧去写源机的路径, 在新机上要么文件不存在直接
# 退出, 要么(更糟)写错文件。所以两者都必须显式传入, 不给默认值 —— 写死或留默认
# 都会在换机时指向上一台机器的路径。
#   CADDYFILE=/path/to/Caddyfile CADDY_CTR=<caddy 容器名> bash deploy/prod/cutover.sh
CADDYFILE="${CADDYFILE:?set CADDYFILE to the shared Caddy's config path}"
CADDY_CTR="${CADDY_CTR:?set CADDY_CTR to the shared Caddy's container name}"
[ -f "$CADDYFILE" ] || { echo "Caddyfile 不存在: $CADDYFILE (换机时用 CADDYFILE= 指定)"; exit 1; }
docker inspect "$CADDY_CTR" >/dev/null 2>&1 || { echo "Caddy 容器不存在: $CADDY_CTR (换机时用 CADDY_CTR= 指定)"; exit 1; }

[ -f "$ENVFILE" ] || { echo "missing $ENVFILE (copy .env.template and fill secrets)"; exit 1; }

echo "==> 1/6 ensure docker networks exist"
docker network create dhc-net 2>/dev/null || echo "    dhc-net already exists"
docker network create dshwork-net 2>/dev/null || echo "    dshwork-net already exists"

echo "==> 2/6 build + start dhc-server + docker-proxy"
docker compose -f "$COMPOSE" --env-file "$ENVFILE" up -d --build

echo "==> 3/6 attach the shared Caddy to dhc-net (+dshwork-net for work UI proxy)"
docker network connect dhc-net "$CADDY_CTR" 2>/dev/null || echo "    dhc-net already connected"
docker network connect dshwork-net "$CADDY_CTR" 2>/dev/null || echo "    dshwork-net already connected"

echo "==> 4/6 wait for dhc-server health"
for i in $(seq 1 30); do
  if docker exec dhc-server python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/api/health')" 2>/dev/null; then
    echo "    healthy"; break
  fi
  [ "$i" = 30 ] && { echo "    dhc-server did not become healthy"; docker logs --tail 40 dhc-server; exit 1; }
  sleep 2
done

echo "==> 5/6 ensure DHC site blocks in the shared Caddyfile (dshcloud-v3, backup kept)"
# Declarative + idempotent: strip every previously managed block (all
# generations), then append the current set:
#   aistore.best        -> dhc-server (primary console/site)
#   www.aistore.best    -> 308 to apex
#   work.aistore.best   -> dshwork-v2 routing (PWA shell + forward_auth)
#   dshcloud.online     -> 上一个域名的兼容层: 机器面直通, 人看的页面 308 (见下)
PRIMARY_HOST="${PRIMARY_DOMAIN:-aistore.best}"
WORK_HOST="${WORK_DOMAIN:-work.aistore.best}"
# 上一个域名 (2026-09-08 从 dshcloud.online 换到 aistore.best)。留着它是因为已经
# 分发出去的桌面端和 CLI 把它写死在包里。置空 (OLD_DOMAIN=) 则该域不再由本机提供
# 任何服务 —— Caddy 没有它的站点块, CF 回源会拿到 SNI 不匹配而握手失败。
# (上一代品牌那个更早的域名已于 2026-08-17 按站主决定彻底撤除, 不在此列。)
OLD_HOST="${OLD_DOMAIN-dshcloud.online}"
# 智能体生成内容的隔离域。留空则不生成对应站点块 (内容仍从主站提供, 靠沙箱兜底)。
# 开启前 DNS 要先有这条记录, 否则 Caddy 申请证书会一直失败。
PREVIEW_HOST="${PREVIEW_DOMAIN:-}"
cp "$CADDYFILE" "$CADDYFILE.bak.$(date +%s 2>/dev/null || echo bak)"
python3 - "$CADDYFILE" "$PRIMARY_HOST" "$WORK_HOST" "$PREVIEW_HOST" "$OLD_HOST" <<'PY'
import re, sys
p, primary, work, preview, old = sys.argv[1:6]
s = open(p, encoding="utf-8").read()
# 0) 守卫: 这一段里除了本脚本生成的几块, 还手写着**每个云工作台产品一个子域**的
#    站点块 (comfy / dify / coze / claude / ...), 而下面的剥离是整段剥。没有这道
#    守卫, 在这台机器上跑一次本脚本 = 那十几个子域当场从 Caddy 里消失, 而且不报
#    任何错: 主站照旧 200, 只有点进那些格子的人拿到握手失败。
existing = re.search(
    r"# ── DHC sites v3 BEGIN ──.*?# ── DHC sites v3 END ──", s, flags=re.DOTALL
)
if existing:
    had = set(re.findall(r"^([a-z0-9][a-z0-9.*-]*)\s*\{", existing.group(0), flags=re.M))
    will = {primary, f"www.{primary}", work}
    if preview:
        will.add(preview)
    if old:
        will |= {old, f"www.{old}", f"work.{old}"}
    lost = sorted(had - will)
    if lost:
        sys.exit(
            "拒绝执行: 下面这些站点块已经在 Caddyfile 里, 但本脚本不生成它们,\n"
            "而剥离是整段剥 —— 跑下去它们会从 Caddy 里消失:\n  "
            + "\n  ".join(lost)
            + "\n它们是手写维护的 (每个云工作台产品一个子域)。要改域名请直接改\n"
            "Caddyfile, 或先把这些块搬进本脚本, 不要绕过这道守卫。"
        )
# 1) strip the marker-wrapped v3 section from previous runs (must run first so
#    the host-pattern strips below never touch v3-managed content)
s = re.sub(r"\n?# ── DHC sites v3 BEGIN ──.*?# ── DHC sites v3 END ──\n?", "\n",
           s, flags=re.DOTALL)
# 2) strip the pre-v3 work block (comment + block)
s = re.sub(r"\n?# ── DSH Cloud workspaces[^\n]*\nwork\.[^\s{]+\s*\{.*?\n\}\n?",
           "\n", s, flags=re.DOTALL)
s = s.rstrip("\n") + "\n"
block = f"""
# ── DHC sites v3 BEGIN ── (managed by deepseek-harness-cloud cutover.sh; do not hand-edit)
{primary} {{
\treverse_proxy dhc-server:8100 {{
\t\tflush_interval -1
\t}}
}}
www.{primary} {{
\tredir https://{primary}{{uri}} 308
}}
# per-user dsh containers + PWA shell (dshwork-v2 routing):
#  - "/" (+PWA assets) -> dhc-server, which serves the container document with
#    the mobile/PWA layers injected (manifest, icons, service worker, CSS);
#  - everything else passes forward_auth (session -> container upstream) and is
#    reverse-proxied with a loopback Host so dsh's fence trusts it.
{work} {{
\t@pwa path / /index.html /manifest.webmanifest /sw.js /pwa/*
\thandle @pwa {{
\t\t@rootdoc path / /index.html
\t\trewrite @rootdoc /api/work/shell
\t\treverse_proxy dhc-server:8100
\t}}
\thandle {{
\t\tforward_auth dhc-server:8100 {{
\t\t\turi /api/work/route
\t\t\tcopy_headers X-Work-Upstream
\t\t\t# Strip the WS Upgrade header from the AUTH subrequest: dsh's chat
\t\t\t# uses WebSocket upgrades (/api/events.mux, /api/events.host); with
\t\t\t# Upgrade present uvicorn routes the /api/work/route subrequest as a
\t\t\t# WS handshake to an HTTP-only path -> 403 -> forward_auth fails ->
\t\t\t# the whole chat WS is killed (page loads, replies never arrive).
\t\t\theader_up -Upgrade
\t\t}}
\t\treverse_proxy {{http.request.header.X-Work-Upstream}} {{
\t\t\theader_up Host 127.0.0.1:3080
\t\t\theader_up Origin http://127.0.0.1:3080
\t\t\tflush_interval -1
\t\t}}
\t}}
}}
"""
if preview:
    block += f"""
# 智能体生成内容的隔离域。这里**只负责把流量送到应用**, 路径限制 (预览域上不
# 提供 /api/) 由应用的中间件做 —— 安全属性放在有测试覆盖的地方, 而不是一段
# 谁都能手改的反代配置里。
{preview} {{
\treverse_proxy dhc-server:8100 {{
\t\tflush_interval -1
\t}}
}}
"""
if old:
    block += f"""
# 上一个域名的兼容层 ({old} -> {primary})。
# **机器面直通, 人看的页面才 308** —— 已分发的桌面端和 CLI 把旧域写死在包里,
# 而按 fetch 规范, 跨域重定向会丢掉 Authorization 头: 对 /api/ 做 308 等于把那些
# 客户端静默变成 401 (它们没有任何改法, 包已经在用户机器上了)。所以下面两个
# handle 的分工不能动。
{old} {{
\t@passthrough path /api/* /llm/* /releases/*
\thandle @passthrough {{
\t\treverse_proxy dhc-server:8100 {{
\t\t\tflush_interval -1
\t\t}}
\t}}
\thandle {{
\t\tredir https://{primary}{{uri}} 308
\t}}
}}
www.{old} {{
\tredir https://{primary}{{uri}} 308
}}
work.{old} {{
\tredir https://{work}{{uri}} 308
}}
"""
block += """# ── DHC sites v3 END ──
"""
open(p, "w", encoding="utf-8").write(s + block)
print("    DHC site blocks (v3) written")
PY

echo "==> 5d/6 sync Caddyfile into the running container + reload"
# single-file bind mounts can go stale in a long-running container; push the
# authoritative host file into a container-local path and reload from it.
docker cp "$CADDYFILE" "$CADDY_CTR:/tmp/Caddyfile.dhc"
docker exec "$CADDY_CTR" caddy validate --config /tmp/Caddyfile.dhc --adapter caddyfile
docker exec "$CADDY_CTR" caddy reload --config /tmp/Caddyfile.dhc --adapter caddyfile

echo "==> 6/6 stop the old dsh container"
docker stop dsh 2>/dev/null && echo "    dsh stopped" || echo "    dsh not running"

echo
echo "cutover done. Verify:"
echo "  curl -s https://aistore.best/api/health"
echo "  (aistore.best / www / work DNS records must point at this origin via Cloudflare)"
