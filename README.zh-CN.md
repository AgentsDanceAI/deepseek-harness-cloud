<div align="center">

# DSH Cloud

**围绕 DeepSeek Harness 构建的托管云端智能体与可自部署平台。**

提供账号体系、服务端模型网关、用量策略、团队能力和可选浏览器工作台，
无需把模型上游密钥分发给每个客户端。

[![CI](https://github.com/AgentsDanceAI/deepseek-harness-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/AgentsDanceAI/deepseek-harness-cloud/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-4c6ef5.svg)](LICENSE)
[![Security policy](https://img.shields.io/badge/security-private%20reporting-2f9e44.svg)](SECURITY.md)

源码候选版本：[`0.2.0`](release/release.json) · Registry 产物尚未发布

[English](README.md) · [架构](docs/architecture.md) ·
[自部署](docs/deploy.md) · [版本边界](docs/editions.md) ·
[安全](SECURITY.md) · [支持](SUPPORT.md)

</div>

> [!IMPORTANT]
> 本仓库当前采用 **AGPL-3.0-only**。未来 Open Core / source-available 仅为
> 尚未生效的规划，必须经过律师审批和完整权利链审计；该规划不会改变当前代码
> 的许可证，也不能撤销既有 AGPL 版本已经授予的权利。详见
> [许可说明](LICENSING.md)。

## 选择使用方式

### DSH Cloud 托管版

[DSH Cloud 托管版](https://dshcloud.online/login?next=%2Fwork)是托管订阅服务：
无需安装服务器，由服务方管理模型与工作台容量、升级、监控、备份和账号支持。
它不是 Token 转售服务。

当前公开方案按所选月度或年度周期一次性预付，**到期不自动续费**。

[**开始使用 DSH Cloud 托管版**](https://dshcloud.online/login?next=%2Fwork) ·
[个人套餐](https://dshcloud.online/pricing#plans) ·
[团队方案](https://dshcloud.online/pricing#team)

### 自部署 Community Edition

使用自己的域名、数据库、身份提供方、模型上游、存储和运维控制运行
AGPL-3.0 Community Edition。Docker Compose 是标准持久化路径；Docker、
npm/npx、uv/uvx 遵循同一套版本化安装契约。

[**自部署指南**](docs/deploy.md) ·
[配置模板](deploy/selfhost/.env.example) ·
[安全清单](docs/security.md)

### 开发与贡献

公开仓库包含 FastAPI 服务、Web 控制台、模型网关、部署定义、桌面叠加层、
移动端外壳、测试和发行契约。

[**开发环境**](#开发) · [贡献指南](CONTRIBUTING.md) ·
[架构](docs/architecture.md) · [变更日志](CHANGELOG.md)

## 主要能力

| 能力 | Community 行为 |
|---|---|
| 账号访问 | 邮箱密码与邮箱验证码，可选 Google/GitHub OAuth，可吊销浏览器/设备/API 凭证 |
| 模型网关 | OpenAI 兼容聊天/模型接口与 Anthropic 兼容 Messages 接口；上游密钥仅保存在服务端 |
| 用量与套餐 | 模型目录、服务端定价、额度账本、速率/并发闸门、用量记录和可选权益 |
| 团队 | 成员、角色、席位、共享额度与成员级用量归属 |
| 支付 | 可选支付适配器；商品与金额由服务端裁定，webhook 需要认证并幂等处理 |
| Web 控制台 | 账号、套餐、订单、团队、管理、法务和下载页面 |
| 桌面与移动端 | 设备授权、桌面叠加层和移动端集成契约 |
| 工作台 | 可选的浏览器智能体运行环境；默认关闭，启用前必须评估隔离边界 |
| 运维 | Docker/Compose、就绪/存活/版本端点、持久化数据、发行元数据和升级指南 |

自部署方负责基础设施、模型成本、提供方协议、TLS、身份与邮件、支付配置、
数据保护、备份、监控及当地法律义务。

## 快速开始

源码现在即可使用。`0.2.0` 的 registry 坐标是候选发行坐标，只有在 npm、
PyPI 或 GHCR 可见首次 `v0.2.0` 发布后才能使用；源码中的版本号不代表包已发布。

### 从源码使用 Docker Compose（现在可用）

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud.git
cd deepseek-harness-cloud
bash scripts/quickstart.sh --domain localhost --admin-email you@example.com
```

脚本会创建 `deploy/selfhost/.env`、生成 `AUTH_SECRET`、询问模型上游、启动标准
Compose 栈并检查就绪状态。随后打开 <http://localhost:8787>。开发模式会把登录验证码
写入服务端日志，禁止在公网使用。

手工验证与启动：

```bash
cp deploy/selfhost/.env.example deploy/selfhost/.env
chmod 600 deploy/selfhost/.env
# 本地试用：DOMAIN=localhost、SITE_SCHEME=http、DHC_DEV=1、
# PUBLIC_BASE=http://localhost:8787、BIND_ADDRESS=127.0.0.1、HTTP_PORT=8787；
# 另设置 AUTH_SECRET、上游和管理员。
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml config --quiet
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml up -d --build
curl --fail --show-error http://localhost:8787/readyz
```

### 从源码使用单容器 Docker（现在可用）

```bash
docker build --tag dsh-cloud-server:local --file server/Dockerfile .
docker volume create dsh-cloud-data
mkdir -p .dsh-cloud
umask 077
printf 'AUTH_SECRET=%s\nDHC_DEV=1\nPUBLIC_BASE=http://127.0.0.1:8081\nPRICING_FILE=pricing.cny.json\n' \
  "$(openssl rand -hex 32)" > .dsh-cloud/docker.env
docker run --rm --name dsh-cloud \
  --env-file .dsh-cloud/docker.env \
  --publish 127.0.0.1:8081:8100 \
  --mount type=volume,src=dsh-cloud-data,dst=/app/data \
  dsh-cloud-server:local
```

在另一终端检查 `http://127.0.0.1:8081/readyz`。调用模型前，把上游密钥加入
`.dsh-cloud/docker.env`。单容器不负责 TLS；对外访问时应保留回环绑定，并使用
经过审核的反向代理。

### npm 与 npx（`0.2.0` 发布后）

```bash
npx --yes @agentsdanceai/dsh-cloud@0.2.0 start --mode trial --wait

npm install --global @agentsdanceai/dsh-cloud@0.2.0
dsh-cloud start --mode trial --wait
```

### uv 与 uvx（`0.2.0` 发布后）

```bash
uvx dsh-cloud==0.2.0 start --mode trial --wait

uv tool install dsh-cloud==0.2.0
dsh-cloud start --mode trial --wait
```

两个 CLI 还提供 `init`、`doctor` 和 `up`，用于显式配置、诊断和生命周期控制。
自动化必须固定不可变版本；启动前应审核生成的站点、密钥、提供方、存储和工作台配置。

完整部署指南包含 GHCR 版本化路径、备份、升级、回滚、扩容和排障：
[docs/deploy.md](docs/deploy.md)。

## 架构概览

```text
浏览器 / 桌面端 / 移动端
          |
          | HTTPS + 会话/设备凭证
          v
   TLS 入口 / 反向代理
          |
          v
  DSH Cloud FastAPI 服务 --------> 自部署方选择的模型/搜索上游
   | 账号、团队、套餐                上游凭证仅在服务端
   | 模型网关与计量
   | Web 控制台与支付
   |
   +---- SQLite 或 PostgreSQL
   |
   +---- 可选工作台后端 -> 智能体运行容器
```

网关负责认证与授权、验证已配置的模型 ID、把客户端凭证替换为自部署方的上游
凭证、流式返回响应并记录标准化用量。完整流程和信任边界见
[架构文档](docs/architecture.md)。

## 安全边界

工作台会执行用户控制的代码，因此默认关闭。容器和 Docker socket proxy 本身
并不等于恶意多租户安全边界；公网运营方必须评估镜像信任、Docker 权限、网络、
挂载、特权、出网、预览源、凭证和资源隔离。详见
[docs/security.md](docs/security.md)。

漏洞请通过
[GitHub 私密安全报告](https://github.com/AgentsDanceAI/deepseek-harness-cloud/security/advisories/new)
或 `security@agentsdance.ai` 私下提交。不要公开未修复漏洞，也不要附带真实凭证和
用户数据。详见 [SECURITY.md](SECURITY.md)。

## 开发

测试工具链为 Python 3.11+、`uv` 和 Node.js 22：

```bash
uv sync --project server --all-extras --locked
uv run --project server pytest server/tests -q
uv run --project server ruff check server/app server/tests server/scripts
node --test server/tests/js/*.test.mjs
```

本地启动：

```bash
DHC_DEV=1 AUTH_SECRET=local-development-secret \
  uv run --project server uvicorn app.main:app --app-dir server --reload
```

CLI 包目录落入候选树后，可直接验证源码入口：

```bash
node packages/cli-npm/bin/dsh-cloud.mjs --help
node packages/cli-npm/bin/dsh-cloud.mjs start --dry-run --json
uv run --project packages/cli-python dsh-cloud --help
uv run --project packages/cli-python dsh-cloud start --dry-run --json
```

DCO 签署、来源披露、测试、安全报告和版本边界要求见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 版本、许可与商标

- 当前软件许可证：[GNU AGPL v3 only](LICENSE)
- 许可状态与未来门禁：[LICENSING.md](LICENSING.md)
- Community / Hosted / Enterprise 边界：[docs/editions.md](docs/editions.md)
- 商标使用：[TRADEMARKS.md](TRADEMARKS.md)
- 第三方声明：[legal/THIRD_PARTY_NOTICES.md](legal/THIRD_PARTY_NOTICES.md)
- 发行变更：[CHANGELOG.md](CHANGELOG.md)

已经按 AGPL-3.0 获得的版本继续享有既有权利。未来 Dify 风格 Open Core /
source-available 条款只能向未来生效，并以合格律师批准和充分再许可权证明为前提。

DSH Cloud 为独立开发与运营的项目，与 DeepSeek 无隶属或背书关系。
“DeepSeek”及相关标识归其各自权利人所有。
