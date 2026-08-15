# deepseek-harness-cloud 架构

> 一句话：给 [deepseek-harness-desktop](https://github.com/anywhere-labs/deepseek-harness-desktop) 套上登陆墙与账号体系，
> 所有 LLM 流量经我们的服务端网关转发（上游 key 永不出服务器），以"免费额度 → 付费套餐 + 积分"变现。

## 1. 总体拓扑

```
用户桌面机                                        我们的服务器
┌──────────────────────────────┐                ┌─────────────────────────────────┐
│ DSH Cloud Desktop (Electron)  │                │  Caddy (TLS, 唯一公网入口)        │
│ ┌──────────────────────────┐ │   HTTPS        │    │                            │
│ │ 登陆墙 (boot 之前)         │─┼───────────────▶│    ├─▶ server (FastAPI :8100)   │
│ │  - 设备码激活 / 邮箱登录    │ │                │    │     ├─ 账号 /api/auth/*      │
│ │  - token 存 safeStorage   │ │                │    │     ├─ 设备授权 /api/device/* │
│ └──────────────────────────┘ │                │    │     ├─ 网关 /llm/v1/*        │
│ ┌──────────────────────────┐ │                │    │     │      /llm/anthropic/* │
│ │ dsh (Cordis 插件树, 上游包) │ │  Bearer <用户token>   │     ├─ 积分/套餐/支付        │
│ │  llm-deepseek ────────────┼─┼───────────────▶│    │     └─ Web 控制台(Jinja2)    │
│ │  web-search-deepseek ─────┼─┼───────────────▶│    │            │ 上游 key 仅在此   │
│ └──────────────────────────┘ │                │    │            ▼                │
└──────────────────────────────┘                │    │   api.deepseek.com (官方)    │
                                                │    │   或任意 OpenAI 兼容上游       │
                                                │    └─▶ SQLite / PostgreSQL      │
                                                └─────────────────────────────────┘
```

核心原则：

1. **上游 API key 只存在于服务端进程环境**。客户端拿到的是"用户会话 token"，网关校验 token → 计量 → 用服务端的 key 转发上游。
2. **不 fork dsh 本体**。dsh 的 LLM 路由（`llm-deepseek` 的 `baseURL`/`apiKeyEnv`）是官方配置面，通过 Cordis patch 层注入即可，dsh 上游升级不受影响。
3. **desktop 仓库以"pin + 补丁"方式复用**（同 desktop 复用 dsh 的方式）：`desktop/upstream.json` 记 pin，`desktop/patches/` 是最小 git 补丁集（品牌、更新 URL、登陆墙挂载点），`assemble.mjs` 一键装配。上游 bump 是机械操作。

## 2. 服务端（`server/`）

FastAPI + SQLite（默认）/ PostgreSQL（`DB_BACKEND=postgres`），Python 3.11+，uv 管理依赖。
所在文件与职责：

| 模块 | 职责 |
|---|---|
| `app/config.py` | 全部环境变量在此集中声明 |
| `app/db.py` | SQLite/PG 双后端薄层（沿用 a sibling production system 验证过的模式） |
| `app/security.py` | scrypt 口令哈希；HMAC-SHA256 会话 token（带 epoch 吊销门）；无第三方 JWT 依赖 |
| `app/accounts.py` | 注册/登录（邮箱+密码、邮箱验证码）、me、登出、注销 |
| `app/device_auth.py` | 设备授权流（RFC 8628 风格）：桌面端起授权 → 浏览器批准 → 轮询取 token |
| `app/gateway.py` | LLM 网关：`/llm/v1/chat/completions`（OpenAI 兼容 SSE 透传）与 `/llm/anthropic/v1/messages`（web_search 用），usage 捕获、并发闸、QPS 桶、错误映射 |
| `app/model_catalog.py` | 模型目录 + 牌价（`config/models.json`）+ 毛利加成 |
| `app/credits.py` | 积分账本：grant 桶（带过期）+ 扣减（先过期先扣）+ 用量流水 |
| `app/plans.py` | 套餐定义（`config/pricing.json` 单一价目源）、权益判定、并发上限 |
| `app/payments/` | `base.py` 订单内核（幂等状态机）；`stripe_provider.py`、`alipay_provider.py`、`wechatpay_provider.py` 按 env 启用 |
| `app/rate_limit.py` | 滑动窗口限速 + 登录防爆破（进程内，可选 Redis） |
| `app/webpages.py` | Web 控制台（Jinja2 服务端渲染）：登录/激活/仪表盘/套餐/订单/法务页 |
| `app/admin.py` | 管理接口（送积分、封号、改套餐） |

### 2.1 认证与 token

- token 形如 `base64url(payload).hmac_hex`，payload = `{u: user_id, d: device_id?, e: epoch, exp}`；
  密钥 `AUTH_SECRET`（部署时必须设置强随机值）。
- **吊销门**：`users.session_epoch` 落库，token 携带签发时 epoch；改密/注销/踢设备 → `epoch+1`，旧 token 全部即刻失效。
- 三种凭证：浏览器 cookie（`dhc_session`，httponly+lax）、桌面设备 token（长期，随 epoch 吊销）、管理 key。
- 网关只认 `Authorization: Bearer <token>`——这正是 dsh `llm-deepseek` 唯一会带的头。

### 2.2 设备授权流（桌面登陆墙）

```
桌面端                                服务端                        浏览器
POST /api/device/start ────────────▶ 生成 device_code + user_code
   ◀── {device_code, user_code,      (10 分钟有效)
        verification_url}
打开浏览器 verification_url ──────────────────────────────────────▶ /activate?code=XXXX-XXXX
                                                                   登录(或已登录) → 点击"授权此设备"
POST /api/device/poll {device_code}─▶ approved → 签发设备 token
   ◀── {token, user}                 (记入 devices 表)
写入 safeStorage, 进入主界面
```

同时保留"窗口内直接邮箱登录"作为回退（无浏览器环境/企业内网）。

### 2.3 网关与计量

- dsh 的请求特征：永远 `stream: true` + `stream_options: {include_usage: true}` → 最后一个 SSE chunk 带精确 usage，
  网关流式透传的同时在尾部捕获 `prompt_tokens`（含 `prompt_cache_hit_tokens` 拆分）与 `completion_tokens` 入账。
- `web_search` 走 **Anthropic Messages 协议**（dsh 的 `web-search-deepseek` 打 `{base}/messages`，头带 `x-api-key` + `anthropic-version`），
  网关单独提供 `/llm/anthropic/v1/messages` 透传到上游 `${UPSTREAM_ANTHROPIC_BASE}`，按次+token 计费。
- 计费口径：`积分 = ceil((uncached_in × P_in + cache_read × P_cache + out × P_out) / 1M × 100 × MARKUP)`，
  牌价 CNY/百万 token 存 `config/models.json`，`MODEL_PRICE_MARKUP` 默认 1.2。1 积分 = ¥0.01 牌价用量。
- 错误映射（dsh 侧行为已核实）：401/403 → dsh 报 AUTH 不重试；429 → RATE_LIMIT；余额不足返回 **402 + OpenAI 风格 error body**（dsh 映射 QUOTA_EXCEEDED，不重试）。
- 闸门顺序：token 有效 → 账号状态 → 并发上限（按套餐）→ QPS 桶 → 积分余额 > 0。
  原则（沿用 a sibling production system）：**只拦新请求，绝不掐断进行中的流**；途中耗尽让它跑完如实入账（允许小额透支）。
- 模型路由：客户端请求的 model id 必须在目录内；目录条目可配 `upstream_model` 改写（对外名 → 上游真实名）。

### 2.4 积分模型

- `credit_grants(id, user_id, amount, remaining, expires, kind, ref)`：注册赠送（一次性）、套餐月额度（31 天）、加油包（12 个月）、管理员调账。
- 扣减：事务内按 `expires` 升序扣 `remaining`；全部不足时允许最后一笔透支（记负余额，下次 grant 先补）。
- 余额 = Σ 未过期 grant 的 remaining。用量明细记 `usage_log`（模型、三段 token、积分、request_id）。
- 免费档：注册即送 `FREE_SIGNUP_CREDITS`（默认 500 积分），并发 1。

### 2.5 支付与订单

- `orders(id, user_id, provider, item, amount_cents, currency, status, provider_ref, created, paid_at)`，
  订单号前缀区分渠道（`DHS`=Stripe `DHA`=支付宝 `DHW`=微信）。
- 三条铁律（a sibling production system 生产验证）：
  1. 金额只认服务端价目表 `config/pricing.json`，绝不信客户端；
  2. webhook 先验签、**再主动查单**确认才落账；
  3. 幂等：只有首个 `pending→paid` 迁移触发发货（套餐生效/积分入账），终态不可逆，退款是唯一 `paid→refunded` 出口。
- 渠道按 env 自动启用：配了 `STRIPE_SECRET_KEY` 就开 Stripe，配了支付宝/微信商户参数就开对应渠道；都没配时前端降级为"开通意向"收集。

## 3. 桌面端（`desktop/`）

### 3.1 装配模型（与上游随时兼容的关键）

```
desktop/upstream.json       # pin: desktop repo commit + dsh runtime 包版本族
desktop/patches/*.patch     # 对 pin 版本的最小补丁（见下）
desktop/dsh-plugin-cloud/   # 我们自己的 Cordis 插件包（登陆墙 + 网关注入），独立目录，不在补丁里
desktop/scripts/assemble.mjs   # clone 上游 @pin → git apply 补丁 → 把 dsh-plugin-cloud 加入 workspace → yarn install/build
desktop/scripts/bump-upstream.mjs  # 改 pin → 重放补丁 → 冲突时逐补丁报告
```

补丁面刻意压到最小（每个一个 .patch，互相独立）：

| 补丁 | 内容 | 冲突风险 |
|---|---|---|
| `0001-branding.patch` | appId/productName/图标/文案常量 | 低（都是模块级常量） |
| `0002-update-endpoints.patch` | 3 个更新 URL 常量 → 我们的域名 | 低 |
| `0003-cloud-gate.patch` | `main.ts` 的 `await boot(...)` 前插 **一行** `await cloudGate(...)`；`profile.ts` 的 patches 数组尾部插 **一行** `...cloudProfilePatches()` | 中（仅两行，函数体都在我们自己的包里） |
| `0004-workspace-member.patch` | 根 package.json workspaces + verify-layout 放行 | 低 |

登陆墙、登录窗口、token 管理、网关 row 注入的**全部逻辑**都在 `dsh-plugin-cloud` 包内——上游怎么改，补丁只需保住那两行调用。

### 3.2 网关注入（登录成功后）

`cloudProfilePatches()` 返回追加到 `prepareDesktopProfile()` 的 Cordis patch rows：

```js
{ id: 'llm-deepseek', config: {
    baseURL: `${CLOUD_BASE}/llm/v1`,
    apiKeyEnv: 'DSH_CLOUD_TOKEN',        // 环境变量名——值在 main 进程 process.env，即用户 token
}},
{ id: 'web-search', config: {            // web-search-deepseek row
    baseURL: `${CLOUD_BASE}/llm/anthropic/v1`,
    apiKeyEnv: 'DSH_CLOUD_TOKEN',
}},
{ id: 'session-telemetry-otel', disabled: true },
```

已核实的支撑事实：

- dsh 凭据 seam 里 `env` 源优先级最高且只读 → 用户改 settings 也压不过我们注入的 token；
- dsh 对子进程做 `SENSITIVE_ENV_PATTERN(/KEY|PASSWORD|SECRET|TOKEN/i)` 擦除 → `DSH_CLOUD_TOKEN` 不会漏进 bash 工具/MCP 子进程；
- `DSH_` 前缀属 bootstrap-only 名单，`.env` 文件写不了 → 只能由我们的 main 进程注入，用户没有旁路；
- patch 层是"整行 config 替换"→ 上游给这两个 row 加新默认字段时会被我们盖掉，`bump-upstream.mjs` 会 diff 上游默认 config 提醒同步（见 docs/compatibility.md）。

### 3.3 再分发注意

打安装包时**剔除 `@deepseek-ai/dsh-subagent-claude-code`**（其内嵌 `@anthropic-ai/claude-agent-sdk` 的分发授权仅限 DeepSeek 自身，不传递给我们）。assemble 脚本处理 + 打包校验兜底。

## 4. 与上游的兼容策略（详见 docs/compatibility.md）

1. 两级 pin：dsh runtime 包版本族 + desktop repo commit，全记录于 `desktop/upstream.json`；
2. 我们依赖的上游契约点显式成文（row id：`llm-deepseek` / `web-search` / `session-telemetry-otel`；config 字段：`baseURL`/`apiKeyEnv`；`boot()` 签名；`prepareDesktopProfile` 的 patches 数组）；
3. `bump-upstream.mjs` 自动重放补丁并对契约点做存在性断言，坏了 fail-loud；
4. 服务端与客户端松耦合：网关是标准 OpenAI/Anthropic 兼容面，dsh 侧任何版本只要还支持自定义 `baseURL` 就能接。

## 5. 安全要点

- 上游 key：仅 `UPSTREAM_API_KEY` env，永不入库、永不出现在任何响应/日志；
- 用户 token 哈希落库（`devices.token_hash`），明文只在签发响应出现一次；
- 口令 scrypt(n=2^14,r=8,p=1)；登录防爆破（同账号 5 次/15min、同 IP 30 次/15min）；
- 发码限额（IP/邮箱/全局三层）；设备码 10 分钟过期、轮询限速；
- CORS 关闭（同源控制台）；网关不回显任何上游头中的敏感信息；
- 日志不落消息正文（隐私政策承诺的口径，见 legal/）。
