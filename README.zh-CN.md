<div align="center">

# DSH Cloud

**围绕 DeepSeek Harness 构建的托管云端智能体与可自部署平台。**

提供账号体系、服务端模型网关、用量策略、团队能力和可选浏览器工作台，
无需把模型上游密钥分发给每个客户端。

[![CI](https://github.com/AgentsDanceAI/deepseek-harness-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/AgentsDanceAI/deepseek-harness-cloud/actions/workflows/ci.yml)
[![License: DSH Cloud Community 1.0](https://img.shields.io/badge/license-DSH%20Cloud%20Community%201.0-4c6ef5.svg)](LICENSE)
[![Security policy](https://img.shields.io/badge/security-private%20reporting-2f9e44.svg)](SECURITY.zh-CN.md)

发行版本：[`0.3.0`](release/release.json)

[English](README.md) · [架构](docs/architecture.zh-CN.md) ·
[自部署](docs/deploy.zh-CN.md) · [版本边界](docs/editions.zh-CN.md) ·
[安全](SECURITY.zh-CN.md) · [支持](SUPPORT.zh-CN.md)

</div>

---

一条命令在本机拉起整套 Community Edition（需要 Docker）：

```bash
npx --yes @agentsdanceai/dsh-cloud start
```

把你的模型上游密钥填进 `./dsh-cloud/.env` 的 `UPSTREAM_API_KEY=`，再跑一次同样的
命令，打开 <http://localhost:8787> 即可。不用 Node 的话 `uvx dsh-cloud start`
等效。详见[快速开始](#快速开始)。

## 选择使用方式

### DSH Cloud 托管版

[DSH Cloud 托管版](https://dshcloud.online/login?next=%2Fwork)是托管订阅服务：
无需安装服务器，按月度或年度服务期提供模型访问、工作台容量、升级、监控、
备份与账号支持。当前方案按所选服务期一次性付费，**到期不自动续费**。

**注册即送 500 积分**；网关内建 **20 个模型**，无需自备任何上游密钥。

[**开始使用 DSH Cloud 托管版**](https://dshcloud.online/login?next=%2Fwork) ·
[个人套餐](https://dshcloud.online/pricing#plans) ·
[团队方案](https://dshcloud.online/pricing#team)

### 自部署 Community Edition

使用自己的域名、数据库、身份提供方、模型上游、存储和运维控制运行
source-available Community Edition。Docker Compose 是标准持久化路径；Docker、
npm/npx、uv/uvx 遵循同一套版本化安装契约。

[**自部署指南**](docs/deploy.zh-CN.md) ·
[配置模板](deploy/selfhost/.env.example) ·
[安全清单](docs/security.zh-CN.md)

### 开发与贡献

公开仓库包含 FastAPI 服务、Web 控制台、模型网关、部署定义、桌面叠加层、
移动端外壳、测试和发行契约。

[**开发环境**](#开发) · [贡献指南](CONTRIBUTING.zh-CN.md) ·
[架构](docs/architecture.zh-CN.md) · [变更日志](CHANGELOG.zh-CN.md)

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

## 20 个模型，一个入口

<!-- model-catalog:start -->
| Provider | Models |
| --- | --- |
| DeepSeek | `DeepSeek-V4-Flash` · `DeepSeek-V4-Pro` |
| Google | `Gemini-3.6-Flash` |
| Xiaomi | `MiMo-V2-Omni` |
| MiniMax | `MiniMax-M2.7` · `MiniMax-M3` |
| Alibaba | `Qwen3-Omni-30B-A3B` · `Qwen3-VL-32B` · `Qwen3.8-Max` |
| Moonshot | `Kimi-K2.7-Code` · `Kimi-K3` |
| Zhipu | `GLM-5.2` |
| ByteDance | `Doubao-Seed-2.0-Pro` |
| OpenAI | `GPT-5.6-Luna` · `GPT-5.6-Terra` · `GPT-5.6-Sol` |
| xAI | `Grok-4.5` |
| Anthropic | `Claude-Sonnet-5` · `Claude-Opus-5` · `Claude-Fable-5` |
<!-- model-catalog:end -->

**注册即送 500 积分——上面每一个模型开箱直接可用。** 不绑卡、不要任何
API Key、不用挨家注册。到 [DSH Cloud 托管版](https://dshcloud.online)直接试，
或者一条命令把它们接进你已有的原版 DeepSeek Harness：

```bash
npx --yes dsh-plugin-cloud setup
```

网关下发的是实时目录，本表与
[`server/config/models.json`](server/config/models.json) 由合同测试互钉。

## 知识库，同一个入口

网关同时提供 `POST /v1/embeddings`——Coze、Dify、RAGFlow 的知识库认的就是
这一支 OpenAI 接口。向量化**只按输入 token 计费**，扣的是与对话同一份积分。

<!-- embedding-catalog:start -->
| 向量化模型 | 维度 |
| --- | --- |
| `Qwen3-Embedding-0.6B` | 1024 |
| `Qwen3-Embedding-4B` | 2560 |
| `Qwen3-Embedding-8B` | 4096 |
| `BGE-M3` | 1024 |
| `Text-Embedding-3-Small` | 1536 |
| `Text-Embedding-3-Large` | 3072 |
<!-- embedding-catalog:end -->

## 快速开始

以下命令均固定使用 `0.3.0`；自动化环境也应固定精确版本。

### 一条命令 — npm/npx

```bash
npx --yes @agentsdanceai/dsh-cloud@0.3.0 start --mode trial --wait

npm install --global @agentsdanceai/dsh-cloud@0.3.0
dsh-cloud start --mode trial --wait
```

**这条命令一句不问。** 它写好 `./dsh-cloud/.env`、拉起整栈、打印从哪里登录，
一分钟左右站点就起来了。桌面端下载与云工作台都指向托管版，所以试用部署本机
并不需要模型密钥。

想在本机跑模型：把你的 OpenAI 兼容端点与密钥填进那个 `.env` 的
`UPSTREAM_API_KEY`，再执行一次同样的命令。`--mode selfhost` 才会提问——对外
部署没有 SMTP 或 OAuth 就注册不了第一个账号，CLI 会先把它问清楚再启动。


### 一条命令 — uv/uvx

```bash
uvx dsh-cloud==0.3.0 start --mode trial --wait

uv tool install dsh-cloud==0.3.0
dsh-cloud start --mode trial --wait
```

两个 CLI 还提供 `init`、`doctor` 和 `up`，用于显式配置、诊断和生命周期控制。
自动化必须固定不可变版本；启动前应审核生成的站点、密钥、提供方、存储和工作台配置。

完整部署指南包含 GHCR 版本化路径、备份、升级、回滚、扩容和排障：
[部署文档](docs/deploy.zh-CN.md)。

### 从源码 — Docker Compose

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud.git && cd deepseek-harness-cloud
bash scripts/quickstart.sh --domain localhost --admin-email you@example.com
```

脚本会生成带随机 `AUTH_SECRET` 的 `deploy/selfhost/.env`、询问模型上游、启动
整栈并等待就绪——然后打开 <http://localhost:8787>。开发模式会把登录码打到服务
端日志，绝不要在公网使用。手动 Compose 步骤见[部署文档](docs/deploy.zh-CN.md)。

### 单容器 — 预构建镜像

```bash
(umask 077; mkdir -p .dsh-cloud; printf 'AUTH_SECRET=%s\nDHC_DEV=1\nPUBLIC_BASE=http://127.0.0.1:8081\nPRICING_FILE=pricing.cny.json\n' "$(openssl rand -hex 32)" > .dsh-cloud/docker.env)
docker run --rm --name dsh-cloud --env-file .dsh-cloud/docker.env --publish 127.0.0.1:8081:8100 --mount type=volume,src=dsh-cloud-data,dst=/app/data ghcr.io/agentsdanceai/dsh-cloud-server:0.3.0
```

检查 <http://127.0.0.1:8081/readyz>，调用模型前把上游密钥加入
`.dsh-cloud/docker.env`。单容器不负责 TLS，保持回环绑定。从源码构建镜像见
[部署文档](docs/deploy.zh-CN.md)。

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
[架构文档](docs/architecture.zh-CN.md)。

## 安全边界

工作台会执行用户控制的代码，因此默认关闭。容器和 Docker socket proxy 本身
并不等于恶意多租户安全边界；公网运营方必须评估镜像信任、Docker 权限、网络、
挂载、特权、出网、预览源、凭证和资源隔离。详见
[安全指南](docs/security.zh-CN.md)。

漏洞请通过
[GitHub 私密安全报告](https://github.com/AgentsDanceAI/deepseek-harness-cloud/security/advisories/new)
或 `security@agentsdance.ai` 私下提交。不要公开未修复漏洞，也不要附带真实凭证和
用户数据。详见 [安全政策](SECURITY.zh-CN.md)。

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
[贡献指南](CONTRIBUTING.zh-CN.md)。

## 版本、许可与商标

- 当前软件许可证：[DSH Cloud Community License 1.0](LICENSE)
- 许可说明：[LICENSING.zh-CN.md](LICENSING.zh-CN.md)
- Community / Hosted / Enterprise 边界：[版本说明](docs/editions.zh-CN.md)
- 商标使用：[商标政策](TRADEMARKS.zh-CN.md)
- 第三方声明：[legal/THIRD_PARTY_NOTICES.md](legal/THIRD_PARTY_NOTICES.md)
- 发行变更：[变更日志](CHANGELOG.zh-CN.md)

DSH Cloud 为独立开发与运营的项目，与 DeepSeek 无隶属或背书关系。
“DeepSeek”及相关标识归其各自权利人所有。
