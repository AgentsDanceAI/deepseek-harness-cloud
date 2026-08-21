<div align="center">

# DSH Cloud

**Turn [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) into a hosted product — accounts, credits, and a browser-based agent workspace.**

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 套上账号、积分与云端工作台，
把它变成一个可运营、可私有化的产品。

[English](#english) · [中文](#中文) · [Live demo](https://dshcloud.online) · [Self-host in 5 min](deploy/selfhost/README.md)

</div>

---

## English

DeepSeek Harness is an excellent agent runtime, but shipping it to people who are
not you means solving the boring half: who is allowed in, whose API key pays for
the tokens, what happens when someone leaves a tab open, and where the agent runs
if the user is on a phone. This repository is that half.

**Two ways to use it.**

1. **Run it as a business.** Users sign in, get free credits, and pay for more.
   Your upstream model key never leaves the server — clients hold a revocable
   device token instead.
2. **Run it for your company.** Deploy it internally and you have a private agent
   platform: your own accounts, your own model upstream, your data inside your
   own network.

### What you get

| | |
|---|---|
| **Login wall** | Email code, Google, GitHub. Desktop apps authorise via a device flow. |
| **Unified model gateway** | OpenAI- and Anthropic-compatible surfaces. The upstream key stays server-side; every call is metered and billed to credits. |
| **Cloud workspace** | One isolated container per user, driven from the browser or a phone. Free machine-time allowance, then a paid pass. |
| **Port preview** | The agent builds a web app; the user opens it on a real public URL instead of an unreachable `localhost`. |
| **Credits & plans** | Bucketed ledger with expiry, subscriptions, credit packs, per-request usage log. |
| **Teams** | Seats plus one shared credit pool; spend is attributed to the member who incurred it. |
| **Payments** | Stripe, Alipay, WeChat Pay, Waffo. Amounts always resolved server-side. |
| **Desktop clients** | macOS / Windows builds of the upstream app with the login wall layered on — no fork, three small patches. |

### Quick start

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud
cd deepseek-harness-cloud
bash scripts/quickstart.sh --domain localhost --admin-email you@example.com
```

That copies the env template, generates a signing secret, brings the stack up and
waits for health. Then open <http://localhost> and sign in — in local mode the
email code is printed to the logs.

For a real deployment, see **[deploy/selfhost/README.md](deploy/selfhost/README.md)**.

### The keys you actually need

Only one is mandatory to get a working system:

| Variable | Required? | What it is |
|---|---|---|
| `AUTH_SECRET` | **yes** | Signs sessions and device tokens. `openssl rand -hex 32` (quickstart generates it). |
| `UPSTREAM_BASE_URL` + `UPSTREAM_API_KEY` | **yes** | Any OpenAI-compatible endpoint — DeepSeek's own API, a gateway, or your self-hosted inference. This is the key that must never reach a client. |
| `MAIL_SMTP_*` | for real sign-ups | Delivers login codes. Without it, codes only appear in the logs (fine locally). |
| `ZHIPU_SEARCH_API_KEY` | optional | Enables the agent's web search. Without it, search returns empty. |
| `WORK_ENABLED` + `WORK_DOMAIN` | optional | Turns on the cloud workspace (needs Docker access and a subdomain). |
| `GOOGLE_LOGIN_*` / `GITHUB_LOGIN_*` | optional | Social sign-in buttons; they degrade gracefully when unset. |
| Payment provider vars | optional | Until one is set, purchases are recorded as intents so you can see demand. |

Every variable is documented inline in
[`deploy/selfhost/.env.example`](deploy/selfhost/.env.example).

> **Model ids.** `server/config/models.json` ships with the ids our own upstream
> serves. If yours exposes different names, edit that file (it is mounted
> read-only, so no rebuild is needed) or requests will 404 with `model_not_found`.

### How it fits together

```
 browser / phone ─┐
 desktop app ─────┤
                  ▼
        ┌──────────────────────┐     Bearer <user device token>
        │  DSH Cloud server    │
        │  accounts · credits  │────▶ upstream model API
        │  gateway · billing   │      Bearer <YOUR key, server-side only>
        └──────────┬───────────┘
                   │ scoped docker socket proxy
                   ▼
        per-user dsh container  ──▶  port preview on a public URL
```

The desktop client is not a fork: we pin an upstream commit, apply three small
patches, and drop in a self-contained plugin. `desktop/scripts/verify-contract.mjs`
asserts the upstream seams we depend on still exist, so upgrades fail loudly
rather than silently.

### Development

```bash
cd server
python -m venv .venv && .venv/bin/pip install -e .
DHC_DEV=1 AUTH_SECRET=dev .venv/bin/python -m uvicorn app.main:app --reload
.venv/bin/python -m pytest tests -q      # 267 tests
```

### Licence and attribution

[AGPL-3.0](LICENSE). Built on DeepSeek Harness (MIT, assembled at build
time — not vendored). Self-hosting is free; if you run a modified version as
a network service, the AGPL requires you to publish your modifications.
Contact us for commercial licensing. This is an independent project — not
affiliated with, nor endorsed by, DeepSeek. "DeepSeek" belongs to its owner.

---

## 中文

DeepSeek Harness 是很好的智能体运行时，但要把它交到别人手上，就得解决无趣的另一半：
谁能进来、token 花谁的钱、有人开着页面走开怎么办、用户只有手机时智能体在哪跑。
这个仓库就是那另一半。

**两种用法：**

1. **拿它做生意。** 用户登录即用、送免费额度、用超了付费。你的上游模型密钥
   永远不出服务器——客户端拿到的是可随时吊销的设备令牌。
2. **给自己公司用。** 部署到内网就是一套私有智能体平台：自己的账号体系、
   自己的模型上游、数据不出内网。

### 能得到什么

| | |
|---|---|
| **登录墙** | 邮箱验证码、Google、GitHub；桌面端走设备授权流程 |
| **统一模型网关** | 同时兼容 OpenAI 与 Anthropic 协议；上游密钥只在服务端，每次调用按积分实时计量 |
| **云工作台** | 每用户一个隔离容器，浏览器与手机直接用；免费机时用完转付费通行证 |
| **端口预览** | 智能体做出的网页，用户能用真实公网地址打开，而不是够不着的 `localhost` |
| **积分与套餐** | 带过期的分桶账本、订阅、积分包、逐条用量记录 |
| **团队** | 席位 + 组织共享积分池，用量仍归属到具体成员 |
| **支付** | Stripe、支付宝、微信支付、Waffo；金额一律服务端裁定 |
| **桌面客户端** | 在上游应用外面套登录墙的 macOS / Windows 安装包——零 fork，只有三个小补丁 |

### 5 分钟跑起来

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud
cd deepseek-harness-cloud
bash scripts/quickstart.sh --domain localhost --admin-email you@example.com
```

脚本会拷贝配置模板、生成签名密钥、拉起全栈并等待健康检查。然后打开
<http://localhost> 登录——本地模式下验证码直接打在日志里。

正式部署见 **[deploy/selfhost/README.md](deploy/selfhost/README.md)**。

### 到底要配哪几个 key

真正必填的只有一组：

| 变量 | 必填？ | 说明 |
|---|---|---|
| `AUTH_SECRET` | **是** | 给会话与设备令牌签名。`openssl rand -hex 32`（快速脚本会自动生成） |
| `UPSTREAM_BASE_URL` + `UPSTREAM_API_KEY` | **是** | 任意 OpenAI 兼容端点：DeepSeek 官方 API、某个网关，或你自建的推理服务。这就是那把绝不能给到客户端的钥匙 |
| `MAIL_SMTP_*` | 正式注册需要 | 发登录验证码。不配则验证码只打进日志（本地够用） |
| `ZHIPU_SEARCH_API_KEY` | 可选 | 开启智能体联网搜索；不配则搜索返回空 |
| `WORK_ENABLED` + `WORK_DOMAIN` | 可选 | 开启云工作台（需要 Docker 权限与一个子域名） |
| `GOOGLE_LOGIN_*` / `GITHUB_LOGIN_*` | 可选 | 社交登录按钮；不配时优雅降级，不报错 |
| 支付相关变量 | 可选 | 一个都没配时，下单会记成「意向单」，方便你先看需求量 |

每个变量在 [`deploy/selfhost/.env.example`](deploy/selfhost/.env.example) 里都有逐行注释。

> **模型 id 提醒**：`server/config/models.json` 里预置的是我们自己上游的模型名。
> 如果你的上游用别的名字，改这个文件即可（它是只读挂载，不用重新构建镜像），
> 否则请求会以 `model_not_found` 404。

### 私有化部署能得到什么

部署完这套代码，你就拥有一份**完全属于自己的 dsh 云平台**：

- 内部同事用邮箱登录，不需要给每个人发模型密钥
- 额度按人或按部门发放，不会有人不小心刷爆账单
- 云工作台跑在你自己的机器上，代码与数据不出内网
- 模型上游换成你自己的（自建推理、专属网关、公有云都行）

### 开发

```bash
cd server
python -m venv .venv && .venv/bin/pip install -e .
DHC_DEV=1 AUTH_SECRET=dev .venv/bin/python -m uvicorn app.main:app --reload
.venv/bin/python -m pytest tests -q      # 267 个测试
```

### 仓库布局

| 目录 | 内容 |
|---|---|
| `server/` | FastAPI 服务端：账号、网关、积分、支付、云工作台、网页 |
| `desktop/` | 桌面端叠加层：上游 pin + 3 个补丁 + 自包含 cloud 插件 + 装配脚本 |
| `deploy/selfhost/` | **自部署编排**（docker compose + Caddy + .env 模板） |
| `deploy/prod/` | 我们自己生产环境的编排，可作为进阶参考 |
| `mobile/` `miniprogram/` | Capacitor 移动壳与微信小程序脚手架 |
| `docs/` | 架构、上游兼容性、上线合规清单 |

### 许可与声明

[AGPL-3.0](LICENSE)。基于 DeepSeek Harness（MIT，构建时装配，不随仓库
分发）。自部署免费；若将修改后的版本作为网络服务运营，AGPL 要求公开你的
修改。商业授权请联系我们。本项目独立运营，与 DeepSeek 无隶属或背书关系，
「DeepSeek」为其权利人所有的商标。
