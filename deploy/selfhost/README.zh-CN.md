# 自部署 DSH Cloud

[English / 双语详细版](README.md) | 简体中文

一套标准 Compose 栈提供账号、统一模型网关、用量与套餐、Web 控制台和可选工作区。
模型上游密钥只保留在服务端。详细变量、支付、工作区、备份和排障说明请同时参考
[双语详细版](README.md) 与 [部署文档](../../docs/deploy.zh-CN.md)。

## 前置条件

- Docker Engine 20.10+ 和 Compose v2；
- 公网部署需要指向主机的域名，并开放 80/443；
- 一个 OpenAI 兼容上游 URL 和 API key；
- 公网首次建号需要 SMTP、Google OAuth 或 GitHub OAuth 中至少一种；
- 持久存储、备份、监控和经过审查的安全边界。

## 五分钟启动

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud
cd deepseek-harness-cloud
./scripts/quickstart.sh --domain dsh.example.com --admin-email you@example.com
# 首次公网执行会创建配置并安全停止
$EDITOR deploy/selfhost/.env  # 配置身份入口和 UPSTREAM_API_KEY
./scripts/quickstart.sh --domain dsh.example.com --admin-email you@example.com
```

脚本不会覆盖已有 `.env`。手工方式：

```bash
cp deploy/selfhost/.env.example deploy/selfhost/.env
chmod 600 deploy/selfhost/.env
$EDITOR deploy/selfhost/.env
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml up -d --build --wait
```

## 必要配置

- `AUTH_SECRET`：使用 `openssl rand -hex 32` 生成；
- `DOMAIN`、`SITE_SCHEME`、`PUBLIC_BASE`：公开站点和回调地址；
- `UPSTREAM_BASE_URL`、`UPSTREAM_API_KEY`：模型上游；
- `ADMIN_EMAILS`：管理员邮箱；
- 公网模式配置 SMTP 或完整 OAuth 客户端，并保持 `DHC_DEV=0`。

本地试用：

```bash
./scripts/quickstart.sh --domain localhost -y
# http://localhost:8787
```

本地模式绑定 `127.0.0.1`、使用 HTTP，并把登录验证码写入容器日志。禁止把
`DHC_DEV=1` 暴露到公网。

## PostgreSQL 与工作区

SQLite 适合单应用实例。使用 PostgreSQL 时叠加：

```bash
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml \
  -f deploy/selfhost/compose.postgres.yml up -d --build --wait
```

工作区默认关闭，会执行用户控制的代码。公网启用前必须审查 Docker socket proxy、
容器权限、共享网络、挂载、出网、预览、凭证、配额和跨租户隔离。详见
[安全指南](../../docs/security.zh-CN.md)。

## 升级、备份和排障

升级前备份数据库、数据卷、`.env` 和密钥，并在一次性环境验证恢复。固定版本或
digest，先运行 `docker compose ... config --quiet`，再启动并检查 `/readyz`。
不得使用 `docker compose down -v`，否则会删除持久卷。

常用检查：

```bash
docker compose --env-file deploy/selfhost/.env -f deploy/selfhost/docker-compose.yml ps
docker compose --env-file deploy/selfhost/.env -f deploy/selfhost/docker-compose.yml logs dhc-server
curl --fail --show-error https://dsh.example.com/readyz
curl --fail --show-error https://dsh.example.com/version
```

安全问题请按 [安全政策](../../SECURITY.zh-CN.md) 私密报告；普通安装问题使用
[支持渠道](../../SUPPORT.zh-CN.md)。
