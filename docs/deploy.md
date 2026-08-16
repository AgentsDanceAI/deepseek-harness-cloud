# 部署手册

## 1. 前置

- 一台公网服务器（境内主体走备案流程后用境内机，或先用境外机 + 海外域名验证产品）
- 域名 A 记录指向服务器；`deploy/.env` 的 `DHC_DOMAIN`/`PUBLIC_BASE` 填该域名
- Docker + Docker Compose v2

## 2. 首次上线

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud && cd deepseek-harness-cloud
cp deploy/.env.example deploy/.env
openssl rand -hex 32        # 填入 AUTH_SECRET
vi deploy/.env              # UPSTREAM_API_KEY、域名、SMTP、支付(可后补)
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
curl -s https://<域名>/api/health   # {"ok":true,...}
```

支付渠道是"配了即开"：`.env` 里补齐某渠道的变量并重启，`/api/pay/context`
即出现该渠道；一个都没配时，购买按钮降级为"意向收集"。

## 3. 管理员

`.env` 的 `ADMIN_EMAILS=you@example.com`（逗号分隔）。该邮箱注册的账号自动获得
管理接口权限（`/api/admin/*`：查用户、送积分、改套餐、封禁、发桌面版本号）。

## 4. 数据与备份

- 默认 SQLite，落在 docker volume `dhc-data`（`/app/data/dhc.db`，WAL 模式）。
  备份：`docker compose exec dhc-server sqlite3 /app/data/dhc.db ".backup /app/data/backup.db"`
  后拷出 volume；或直接快照 volume。
- 规模上来后切 PostgreSQL：`.env` 里 `DB_BACKEND=postgres` + `POSTGRES_DSN=...`，
  并 `pip install -e '.[postgres]'`（Dockerfile 里加一行即可）。表结构自动建。

## 5. 桌面安装包发布

```bash
# 1) 构建（见 README 桌面端一节；mac 签名/公证与 win 签名见 中国大陆上线清单）
# 2) 传到 Caddy 的 releases volume：
docker cp DSH-Cloud-Desktop-2.0.0-mac.dmg <caddy容器>:/srv/releases/
# 3) .env 里设 DOWNLOAD_URL_MAC=https://<域名>/releases/DSH-Cloud-Desktop-2.0.0-mac.dmg 并重启
# 4) 通知客户端自动更新：
curl -X POST https://<域名>/api/admin/desktop-version \
  -H "authorization: Bearer <管理员token>" -d '{"version":"2.0.0"}'
```

客户端每 6 小时查一次 `/api/desktop/version`（严格 stable SemVer），用户确认后
从 `/api/downloads/mac|windows` 下载（302 到 releases 文件）。

## 6. 多 worker / 扩容注意

当前限流、并发闸、QPS 桶是**单进程语义**（uvicorn 单 worker 正确）。要开多
worker 或多机，先把这三处换 Redis 实现（接口都在 `rate_limit.py` / `gateway.py`
的 `_inflight`，a sibling production system 有生产验证过的 Redis Lua 版本可移植），
DB 同步切 PostgreSQL。单 worker + SQLite 支撑早期完全够用。

## 7. 监控与日志

- 容器日志即应用日志；`docker compose logs -f dhc-server`。
- 日志不含消息正文（隐私政策口径）；含 request_id 可与 `usage_log` 关联。
- 建议接 Uptime 监控打 `/api/health`。

## 8. 密钥轮换

- 上游 key：改 `.env` 重启即可（无状态）。
- `AUTH_SECRET`：轮换会使所有 token/session 失效（用户需重新登录），
  低峰期执行；不影响数据。
