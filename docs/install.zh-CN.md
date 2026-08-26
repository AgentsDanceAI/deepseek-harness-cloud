# 安装 DSH Cloud Community Edition

[English](install.md) | 简体中文

`0.2.2` 提供 npm 和 Python 两套等价的版本锁定安装器。两者默认使用本地试用模式，
把 Caddy 绑定到 `127.0.0.1:8787`，生成保存于 `0600` 文件中的随机 256 位认证密钥，
并且不通过 shell 调用 Docker。

## 一键本地试用

```bash
npx --yes @agentsdanceai/dsh-cloud@0.2.2 start --mode trial --wait
# 或
uvx dsh-cloud==0.2.2 start --mode trial --wait
```

打开 <http://localhost:8787>。自动化请固定明确版本，不要改成 `latest`。

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
npx --yes @agentsdanceai/dsh-cloud@0.2.2 init --mode selfhost \
  --domain cloud.example.com --admin-email admin@example.com
$EDITOR dsh-cloud/.env  # 配置 SMTP 或 Google/GitHub OAuth，以及模型上游
npx --yes @agentsdanceai/dsh-cloud@0.2.2 doctor dsh-cloud
npx --yes @agentsdanceai/dsh-cloud@0.2.2 up dsh-cloud --wait
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
