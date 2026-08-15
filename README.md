# deepseek-harness-cloud

给 [deepseek-harness-desktop](https://github.com/anywhere-labs/deepseek-harness-desktop)
套上账号体系与登陆墙的商业化云服务：用户登录即用，无需自购 API Key；
LLM 流量统一经我们的网关转发（**上游 key 永不出服务器**）；
免费额度起步，套餐 + 积分变现。

```
桌面端 (Electron, 上游 pin + 3 个小补丁 + 自包含 cloud 目录)
   │  登陆墙 → 设备授权/邮箱登录 → token 注入 dsh (env 凭据源, 最高优先级)
   ▼
服务端 (FastAPI):  账号 · 设备授权 · LLM 网关(OpenAI+Anthropic 兼容双面) ·
                   积分账本 · 套餐 · 支付(Stripe/支付宝/微信) · Web 控制台
   │  Bearer <用户token> 进, Bearer <我们的上游key> 出, usage 精确计量扣积分
   ▼
上游模型 (DeepSeek 官方 API 或任意 OpenAI 兼容端点)
```

## 仓库布局

| 目录 | 内容 |
|---|---|
| `server/` | FastAPI 服务端（账号/网关/积分/支付/控制台），pytest 测试 |
| `desktop/` | 桌面端 overlay：`upstream.json` pin + `patches/` + `dsh-plugin-cloud/`（登陆墙与网关注入的全部逻辑）+ 装配/升级脚本 |
| `deploy/` | docker-compose + Caddyfile + `.env.example` |
| `legal/` | 用户协议 / 隐私政策 / 退款政策 / AUP / 第三方声明（草案，上线前过律师） |
| `docs/` | [架构](docs/architecture.md) · [上游兼容策略](docs/compatibility.md) · [上线申请清单](docs/applications-checklist.md) · [部署](docs/deploy.md) |

## 服务端：本地开发

```bash
cd server
uv venv --python 3.11 && uv pip install -e '.[dev]'
DHC_DEV=1 AUTH_SECRET=dev UPSTREAM_API_KEY=sk-xxx .venv/bin/uvicorn app.main:app --port 8100
# 测试
.venv/bin/python -m pytest -q
```

`DHC_DEV=1` 下邮件验证码打印到控制台、cookie 不要求 https。

## 服务端：生产部署

```bash
cp deploy/.env.example deploy/.env    # 填 AUTH_SECRET / UPSTREAM_API_KEY / 域名等
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
```

Caddy 自动 HTTPS，唯一公网入口；应用只绑 loopback。详见 [docs/deploy.md](docs/deploy.md)。

## 桌面端：装配与打包

```bash
node desktop/scripts/assemble.mjs        # clone 上游 pin → 应用补丁 → 拷入 cloud 目录
cd desktop/build/upstream && corepack enable && yarn install
node ../../scripts/verify-contract.mjs "$PWD"   # 断言上游契约点
cd dsh-plugin-desktop && yarn build && yarn package:dir   # 或 dist:mac / dist:win
```

上游发新版后的升级流程见 [docs/compatibility.md](docs/compatibility.md)——
定制面只有 3 个补丁 + 1 个自包含目录，升级是机械操作，装配脚本会守住
所有契约点（包括不得再分发 `@anthropic-ai/claude-agent-sdk` 的许可红线）。

## 商业模式与安全要点

- **key 隔离**：上游 API key 仅存在于服务端进程环境；客户端只持有可吊销的用户
  token（设备级、随 epoch 一键全灭）。dsh 自身会把该 token 从所有子进程环境擦除。
- **计量**：dsh 恒为流式 + `include_usage`，网关从 usage chunk 拿精确三段 token
  入账；`1 积分 = ¥0.01 牌价用量`，牌价见 `server/config/models.json`（上线前按官
  方价目校准），毛利率 `MODEL_PRICE_MARKUP` 控制。
- **套餐**：`server/config/pricing.json` 是唯一价目源（免费 500 积分起，
  Plus/Pro/Max 月/年付 + 加油包）。金额绝不信客户端。
- **原则**：额度闸门只拦新请求，绝不掐进行中的流；支付 webhook 先验签再回查
  才发货，幂等唯一迁移。

## 上线前必办

见 [docs/applications-checklist.md](docs/applications-checklist.md)：域名+ICP/公安备案、
支付渠道（Stripe/支付宝/微信商户）、SMTP、上游 key、代码签名（Apple/Windows）等，
含官方入口、材料与依赖顺序。`legal/` 四份文书为草案，正式发布前请经法律专业人士审阅。

## 许可

- 本仓库自有代码：© 跃迁效应，专有（见 LICENSE）。
- 上游 deepseek-harness / deepseek-harness-desktop 均为 MIT，声明见
  [legal/THIRD_PARTY_NOTICES.md](legal/THIRD_PARTY_NOTICES.md)；“DeepSeek” 为第三方商标，
  本产品独立运营，与 DeepSeek 无隶属或背书关系。
