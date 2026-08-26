# 自部署

[English](deploy.md) | 简体中文

本指南面向 Community Edition，不描述 DSH Cloud 线上环境。需要免运维服务可使用
[DSH Cloud Hosted](https://dshcloud.online/login?next=%2Fwork)。

## 安装方式

| 方式 | 适用场景 | 网络暴露 |
|---|---|---|
| 标准 Docker Compose | 带 Caddy/TLS 的持久部署 | 只发布配置的 HTTP/HTTPS 端口 |
| Docker 单容器 | 本地评估或接入自己的反向代理 | 默认绑定回环地址 |
| npm/npx 或 uv/uvx | 引导式配置与生命周期管理 | 生成并操作同一版本化栈 |
| 源码开发 | 修改服务端行为 | 仅开发环境，禁止公网暴露 |

## 标准 Compose

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud.git
cd deepseek-harness-cloud
cp deploy/selfhost/.env.example deploy/selfhost/.env
chmod 600 deploy/selfhost/.env
```

至少设置域名/协议/`PUBLIC_BASE`、随机 `AUTH_SECRET`、模型上游和管理员邮箱。公网还
必须配置 SMTP、Google OAuth 或 GitHub OAuth 中至少一种可验证的首次建号路径。

本地试用值：`DOMAIN=localhost`、`SITE_SCHEME=http`、`DHC_DEV=1`、
`PUBLIC_BASE=http://localhost:8787`、`BIND_ADDRESS=127.0.0.1`、
`HTTP_PORT=8787`、`HTTPS_PORT=8443`。

```bash
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml config --quiet
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml up -d --build --wait
curl --fail --show-error http://localhost:8787/readyz
```

使用 PostgreSQL 时再叠加 `compose.postgres.yml`。所有 overlay 必须在后续 `up`、
`down`、`config` 和升级命令中保持一致。

## 发行镜像和 CLI

```bash
npx --yes @agentsdanceai/dsh-cloud@0.2.4 start --mode trial --wait
uvx dsh-cloud==0.2.4 start --mode trial --wait
```

公网模式先执行 `init --mode selfhost`，编辑生成的 `.env`，再运行 `doctor` 和
`up --wait`。自动化固定精确版本；镜像优先固定 digest。

## 备份、升级与回滚

升级前备份数据库、数据卷、配置和密钥，并在一次性环境验证恢复。先运行 Compose
`config`，再拉取或构建目标版本，观察 `/readyz`、日志、错误率和关键登录/模型流程。
保留上一版本镜像、Compose 文件和数据库备份。不要用 `down -v`，它会删除持久卷。

SQLite 适合单实例；多副本应使用 PostgreSQL，并由外部负载均衡器只向就绪实例转发。
生产工作区必须单独审查隔离、容量和存储，详见
[工作区伸缩](workspace-scaling.zh-CN.md)。
