# 安装 DSH Cloud Community Edition

[English](install.md) | 简体中文

`0.3.0` 提供 npm 和 Python 两套等价的版本锁定安装器。两者默认使用本地试用模式，
把 Caddy 绑定到 `127.0.0.1:8787`，生成保存于 `0600` 文件中的随机 256 位认证密钥，
并且不通过 shell 调用 Docker。

## 一键本地试用

```bash
npx --yes @agentsdanceai/dsh-cloud@0.3.0 start --mode trial --wait
# 或
uvx dsh-cloud==0.3.0 start --mode trial --wait
```

这条命令一句不问：写好配置、拉起整栈，然后打印一张面板告诉你打开哪、验证码
怎么取（试用模式没有邮件服务器，验证码打在服务端日志里）、以及重启和停止的命令。
打开 <http://localhost:8787>。

站点上的桌面端下载与云工作台指向托管版 dshcloud.online——试用部署本机不需要
模型密钥。想在本机跑模型，把 `UPSTREAM_API_KEY` 填进 `./dsh-cloud/.env` 再执行
一次同样的命令。

自动化请固定明确版本，不要改成 `latest`。

## 从源码验证

```bash
node packages/cli-npm/bin/dsh-cloud.mjs --version
node packages/cli-npm/bin/dsh-cloud.mjs start --dry-run --json
npm --prefix packages/cli-npm test
uv run --project packages/cli-python dsh-cloud --version
uv run --project packages/cli-python dsh-cloud start --dry-run --json
```

从源码构建容器：

```bash
cp deploy/selfhost/.env.example deploy/selfhost/.env
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml up -d --build --wait
```

## 公网自部署

先初始化，使身份凭证不进入 shell 历史：

```bash
npx --yes @agentsdanceai/dsh-cloud@0.3.0 init --mode selfhost \
  --domain cloud.example.com --admin-email admin@example.com
$EDITOR dsh-cloud/.env  # 配置 SMTP 或 Google/GitHub OAuth，以及模型上游
npx --yes @agentsdanceai/dsh-cloud@0.3.0 doctor dsh-cloud
npx --yes @agentsdanceai/dsh-cloud@0.3.0 up dsh-cloud --wait
```

公网启动会在缺少 SMTP 或完整 OAuth 客户端时拒绝继续，避免全新实例无法创建首个
已验证账号。本地试用模式会把验证码写入日志。

## 产物验证

```bash
node scripts/release/build-packages.mjs dist/packages
npm pack dist/packages/npm --dry-run --json
uv build --wheel --project dist/packages/python --out-dir dist/packages/python
```

这些命令只生成本地产物，不会发布 Registry 或部署生产服务。
