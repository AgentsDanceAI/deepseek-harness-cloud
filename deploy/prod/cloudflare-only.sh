#!/usr/bin/env bash
# 生产机 (应用机) 上: 80/443 只收 Cloudflare 的来源。
#
# 为什么: 我们所有域名都挂在 Cloudflare 后面 (橙云), 但源站 IP 在公开仓库的历史里出现过
# (deploy 脚本注释、旧的运维文档)。知道源站 IP 的人可以绕过 Cloudflare 直接打 Caddy ——
# WAF / 限速 / DDoS 防护全部失效。源站只认 Cloudflare 的网段, 这个 IP 泄不泄露就无所谓了。
#
# 影响面: 这台机上 Caddy 服务的 **全部** 域名 (两条产品线)。前提: 每个域名都必须是橙云
# (代理开启) —— 灰云 (仅 DNS) 的域名会被这道墙挡死。2026-09-05 核对: Caddy 里 21 个域名
# 全部解析到 104.21.x / 172.67.x。ACME 证书续期不受影响 (Let's Encrypt 经 Cloudflare 打到
# 源站的 /.well-known/acme-challenge, 来源也是 Cloudflare)。
#
# Caddy 跑在容器里, 进容器的流量走 FORWARD 链的 DOCKER-USER, 不走 INPUT —— 两条链都加。
# 网段列表每次 apply 时从 cloudflare.com 拉最新的; 用 `install` 装成每天刷一次的 systemd timer。
#
#   bash cloudflare-only.sh check     # 只打印会装的规则和当前网段, 不动任何东西
#   bash cloudflare-only.sh apply     # 装 (幂等)
#   bash cloudflare-only.sh install   # 装到 /usr/local/sbin + 每日刷新 timer + 立刻 apply
#   bash cloudflare-only.sh remove    # 退场
set -euo pipefail
CHAIN=CF-ONLY
PORTS=80,443
WAN_IF="${WAN_IF:-$(ip -4 route show default | awk '{print $5; exit}')}"

fetch() {
  v4=$(curl -fsS -m 15 https://www.cloudflare.com/ips-v4) || { echo "cannot fetch cloudflare ips-v4" >&2; exit 3; }
  v6=$(curl -fsS -m 15 https://www.cloudflare.com/ips-v6) || { echo "cannot fetch cloudflare ips-v6" >&2; exit 3; }
  [ "$(echo "$v4" | grep -c /)" -ge 10 ] || { echo "ips-v4 list looks wrong: $v4" >&2; exit 3; }
}

rules() {  # $1 = iptables|ip6tables, $2 = 网段列表
  local ipt=$1 nets=$2
  $ipt -w -N "$CHAIN" 2>/dev/null || $ipt -w -F "$CHAIN"
  for n in $nets; do $ipt -w -A "$CHAIN" -s "$n" -j RETURN; done
  # 本机 / 回环 / docker 网桥内部访问不算外来
  $ipt -w -A "$CHAIN" ! -i "$WAN_IF" -j RETURN
  $ipt -w -A "$CHAIN" -m limit --limit 10/min -j LOG --log-prefix "cf-only-drop: "
  $ipt -w -A "$CHAIN" -j DROP
  for parent in INPUT DOCKER-USER; do
    $ipt -w -L "$parent" -n >/dev/null 2>&1 || continue
    $ipt -w -C "$parent" -p tcp -m multiport --dports "$PORTS" -j "$CHAIN" 2>/dev/null \
      || $ipt -w -I "$parent" 1 -p tcp -m multiport --dports "$PORTS" -j "$CHAIN"
  done
}

apply() { fetch; rules iptables "$v4"; rules ip6tables "$v6"; logger -t cf-only "applied: $(echo "$v4" | wc -l) v4 + $(echo "$v6" | wc -l) v6 ranges on $WAN_IF:$PORTS"; echo "applied on $WAN_IF"; }

remove() {
  for ipt in iptables ip6tables; do
    for parent in INPUT DOCKER-USER; do
      $ipt -w -D "$parent" -p tcp -m multiport --dports "$PORTS" -j "$CHAIN" 2>/dev/null || true
    done
    $ipt -w -F "$CHAIN" 2>/dev/null || true; $ipt -w -X "$CHAIN" 2>/dev/null || true
  done
  echo removed
}

install_self() {
  install -m 0755 "$0" /usr/local/sbin/cf-only
  cat > /etc/systemd/system/cf-only.service <<'UNIT'
[Unit]
Description=Allow 80/443 only from Cloudflare (refresh ranges)
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cf-only apply
[Install]
WantedBy=multi-user.target
UNIT
  cat > /etc/systemd/system/cf-only.timer <<'UNIT'
[Unit]
Description=Refresh Cloudflare ranges daily
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now cf-only.service cf-only.timer
  systemctl --no-pager status cf-only.service | head -5
}

case "${1:-}" in
  check) fetch; echo "WAN_IF=$WAN_IF ports=$PORTS"; echo "v4 ranges: $(echo "$v4" | tr '\n' ' ')"; echo "v6 ranges: $(echo "$v6" | tr '\n' ' ')"; echo "chains present: INPUT=$(iptables -w -L INPUT -n >/dev/null 2>&1 && echo yes) DOCKER-USER=$(iptables -w -L DOCKER-USER -n >/dev/null 2>&1 && echo yes || echo no)";;
  apply) apply ;;
  remove) remove ;;
  install) install_self ;;
  *) echo "usage: $0 check|apply|remove|install" >&2; exit 2 ;;
esac
