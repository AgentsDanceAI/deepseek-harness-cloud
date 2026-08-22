# 自部署 DSH Cloud / Self-host DSH Cloud

English / 中文说明 | [简体中文独立版](README.zh-CN.md)

**中文** — 一条命令，在自己的服务器上跑起一套私有的 dsh 云平台：账号体系、统一
LLM 网关（上游 key 永不下发到客户端）、积分与套餐计费、Web 控制台，以及可选的
「云工作台」（每个用户一个浏览器里可用的 dsh 容器）。

**English** — One command gives you a private dsh cloud on your own box:
accounts, a unified LLM gateway (the upstream key never leaves the server),
credit/plan metering, a web console, and optionally *cloud workspaces* — one
browser-usable dsh container per user.

```
                    ┌── https://dsh.example.com ─────────────────┐
  browser / desktop │  dhc-caddy  ── TLS, reverse proxy          │
  ───────────────▶  │      │                                     │
                    │  dhc-server ── accounts · gateway · credits│
                    │      │            · console · payments     │
                    │      ├─▶ upstream LLM  (your API key)      │
                    │      └─▶ dhc-docker-proxy ─▶ dshwork-<user>│
                    └────────────────────────────────────────────┘
```

---

## 1. 前置条件 / Prerequisites

| | |
|---|---|
| Docker | Engine 20.10+ with **Compose v2** (`docker compose version`) |
| 机器 / Machine | 1 vCPU / 1 GB RAM 起步；开云工作台按 `每用户 512 MB` 另算 |
| 域名 / Domain | 一条 A/AAAA 记录指向本机；80 与 443 入站放行（证书签发要用）<br>A record pointing at this host, inbound 80 **and** 443 open |
| 模型上游 / Upstream | 任意 **OpenAI 兼容** 端点 + 一把 API key（DeepSeek 官方、代理网关、或你自己的 vLLM/Ollama）<br>Any OpenAI-compatible endpoint and its key |

没有域名也可以先本地体验：见 [§4 本地模式](#4-本地模式--local-trial-mode)。
No domain yet? Start with the local mode in [§4](#4-本地模式--local-trial-mode).

---

## 2. 五分钟起步 / Five-minute start

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud
cd deepseek-harness-cloud

./scripts/quickstart.sh --domain dsh.example.com --admin-email you@example.com
# The first public run creates deploy/selfhost/.env and stops safely.
$EDITOR deploy/selfhost/.env  # set SMTP or Google/GitHub OAuth, and UPSTREAM_API_KEY
./scripts/quickstart.sh --domain dsh.example.com --admin-email you@example.com
```

脚本会拷 `.env.example`、生成 `AUTH_SECRET`，并在公网身份入口未配置时先停止；配置
SMTP 或 Google/GitHub OAuth 后重跑，它会构建并启动服务，再轮询 `/readyz`。已有
`.env` 不会被覆盖。The script scaffolds the environment and stops before a
public launch until SMTP or Google/GitHub OAuth is configured. Re-run it to
build, start, and wait for `/readyz`; the existing `.env` is preserved.

等价的手工步骤 / The manual equivalent:

```bash
cd deploy/selfhost
cp .env.example .env
openssl rand -hex 32          # -> AUTH_SECRET
$EDITOR .env                  # DOMAIN / AUTH_SECRET / upstream / admin / identity
docker compose --env-file .env -f docker-compose.yml -f compose.build.yml up -d --build
```

首次构建约 1–3 分钟；证书在域名解析正确时 10–30 秒内签发。
First build takes 1–3 minutes; the certificate arrives 10–30 s after that,
provided DNS already points here.

---

## 3. 必须配的三件事 / The three things you must set

`.env` 里每一项都有逐行注释，但只有三处不配就不能用：
Everything in `.env` is commented line by line; these three are non-negotiable:

| 变量 / Variable | 为什么 / Why | 不配的后果 / If unset |
|---|---|---|
| `AUTH_SECRET` | 会话与设备 token 的签名密钥 (`openssl rand -hex 32`) | 服务直接拒绝启动 |
| `DOMAIN` + `SITE_SCHEME` | 决定 Caddy 站点地址与 `PUBLIC_BASE`（OAuth 回调、支付回跳、云工作台网关地址都由它推导） | 登录跳转、回调、下载链接全错 |
| `UPSTREAM_BASE_URL` + `UPSTREAM_API_KEY` | 模型上游。URL 必须带版本路径（通常 `/v1`），网关会拼 `/chat/completions` | 服务能起来，但所有模型请求返回 503 |

可选但强烈建议 / Optional but recommended:

- `ZHIPU_SEARCH_API_KEY` — 不配则智能体**不能联网搜索**（`web_search` 返回 503），
  聊天与写代码不受影响。申请：<https://open.bigmodel.cn>。
  Without it the agent still chats and codes, it just cannot browse.
- `MAIL_SMTP_*` 或 Google/GitHub OAuth — 公网首次建号必须先验证邮箱或身份；密码仅供
  已完成验证并设置密码的账号后续登录。A public deployment needs SMTP or OAuth
  for first-account verification; password login is for an existing verified account.
- 支付 / Payments — 一个都不配时购买页降级为「意向收集」，其他功能不受影响。
  With no payment provider configured the pricing page just records intent.

**模型目录 / Model catalogue.** `server/config/models.json` 被只读挂载进容器，
改完重启即可，不用重新构建镜像。仓库自带的 id 是 `deepseek-v4-flash` /
`deepseek-v4-pro`；如果你的上游用别的名字，改这里的 `id` 或 `upstream_model`，
否则请求会 404 `model_not_found`。价目同理（`server/config/pricing.json`）。
The catalogue is bind-mounted read-only, so edit it in the repo and restart —
an id that is not listed there answers 404.

---

## 4. 本地模式 / Local trial mode

```bash
./scripts/quickstart.sh --domain localhost -y
# -> http://localhost:8787
```

本地模式做了三件不同的事 / What differs in local mode:

- `SITE_SCHEME=http`：Caddy 站点地址是 `http://localhost`，入口映射到
  `http://localhost:8787`，**完全不签证书**。
- `DHC_DEV=1`：登录验证码打印到容器日志（不需要 SMTP），会话 cookie 不要求 https，
  `/api/docs` 打开。**公网部署必须 `DHC_DEV=0`。**
  ```bash
  docker compose -f deploy/selfhost/docker-compose.yml logs dhc-server | grep dev-mail
  ```
- 云工作台不可用：应用会把用户重定向到 `https://<WORK_DOMAIN>/`，localhost 下没有意义。
  Cloud workspaces need a real HTTPS domain and stay off here.

---

## 5. 初始化管理员 / Bootstrapping the admin

没有单独的管理员注册流程：**`ADMIN_EMAILS` 就是管理员名单**。
There is no separate admin signup — `ADMIN_EMAILS` *is* the admin list.

```bash
# 1) .env
ADMIN_EMAILS=you@example.com,ops@example.com     # 逗号分隔
docker compose --env-file .env up -d             # 改完重启生效

# 2) 在浏览器用该邮箱的验证码登录（公网需先配置 SMTP；也可用已配置的 OAuth）。
#    Sign in with that address's e-mail code, or a configured OAuth provider.
# 3) 可选：在账号设置里设一个至少 8 位的密码，供后续设备/CLI 密码登录。
```

该账号立刻拥有 `/api/admin/*`：查用户、送积分、改套餐、封禁账号、发布桌面版本号。
That account now has `/api/admin/*`: list users, grant credits, set plans,
suspend accounts, publish desktop versions.

```bash
# 例：给某个用户送 5000 积分（需要管理员 token，见 §6 第 3 步）
curl -fsS -X POST https://dsh.example.com/api/admin/grant-credits \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"user_id":"u_xxx","amount":5000,"valid_days":365}'
```

---

## 6. 验证清单 / Verification checklist

```bash
BASE=https://dsh.example.com
EMAIL=you@example.com
PASS=a-long-password

# 1) 健康检查 —— 期望 {"ok":true,"service":"deepseek-harness-cloud"}
curl -fsS $BASE/api/health

# 2) 站点与证书 —— 期望 HTTP/2 200 且证书有效
curl -sSI $BASE/ | head -1

# 3) 请求邮箱验证码并完成已验证注册。把邮件中的 6 位码填入 CODE。
curl -fsS -X POST $BASE/api/auth/email/send -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\"}"
read -r -p 'E-mail code: ' CODE
COOKIE_JAR="$(mktemp)"
curl -fsS -c "$COOKIE_JAR" -X POST $BASE/api/auth/email/login \
  -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"code\":\"$CODE\"}"

# 4) 可选：设置密码后换一个设备 token（设置密码会主动吊销旧会话）。
curl -fsS -b "$COOKIE_JAR" -X POST $BASE/api/auth/password \
  -H 'content-type: application/json' -d "{\"old\":\"\",\"new\":\"$PASS\"}"
rm -f "$COOKIE_JAR"
TOKEN=$(curl -fsS -X POST $BASE/api/device/login -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"name\":\"smoke\",\"platform\":\"cli\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# 5) 网关目录 —— 期望列出 models.json 里的模型
curl -fsS $BASE/llm/v1/models -H "authorization: Bearer $TOKEN"

# 6) 真实推理（会扣积分）—— 证明上游 key 打通
curl -fsS -X POST $BASE/llm/v1/chat/completions -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"ping"}]}'

# 7) 控制台 —— 浏览器打开 $BASE/console，应看到余额与设备列表
```

全部通过即代表：TLS、账号、设备 token、网关、计量、控制台 六条链路都活着。
All six pass ⇒ TLS, accounts, device tokens, gateway, metering and console are live.

---

## 7. 云工作台 / Cloud workspaces (optional)

每个用户一个 dsh 容器，手机浏览器也能用。它会**把可执行代码的沙箱交给用户**，
并占用内存，所以默认关闭。
One dsh container per user, usable from a phone. It hands users a
code-executing sandbox and costs RAM, so it is **off by default**.

### 7.1 需要的东西 / What it needs

1. **宿主 docker**：应用通过 `tecnativa/docker-socket-proxy` 访问引擎，并将 API
   缩小为 `CONTAINERS/NETWORKS/IMAGES/INFO/POST=1`、`EXEC=0`、`VOLUMES=0`。
   **不要放宽这些开关**。但代理不会检查 container-create 的 `HostConfig` 请求体，
   因而不能阻止已攻陷的控制面请求特权容器、宿主挂载、设备或宿主命名空间；此配置只适合
   受信任的单一运维方，不是不可信多租户的安全边界。The app never sees the raw
   socket, but these flags only reduce API surface; they do not make the optional
   profile a hostile-tenant isolation boundary.
2. **一个 dsh 镜像**（`WORK_IMAGE`）。正式发行会提供版本化镜像；从源码工作时，
   用 `release/release.json` 的唯一版本来源构建 canonical Dockerfile：

   ```bash
   NODE_IMAGE=$(node -p "require('./release/release.json').baseImages.node")
   HARNESS_RUNTIME=$(node -p "require('./release/release.json').harnessRuntime")
   VERSION=$(node -p "require('./release/release.json').version")
   docker build -t dsh-cloud-workspace:local -f deploy/workspace/Dockerfile \
     --build-arg NODE_IMAGE="$NODE_IMAGE" \
     --build-arg HARNESS_RUNTIME="$HARNESS_RUNTIME" \
     --build-arg VERSION="$VERSION" .
   ```

   镜像里不需要写 `CMD`：应用创建容器时会自己下发启动命令（写入 `settings.yaml`
   与 `AGENTS.md`，用 `socat` 把 3081 转到 dsh 的回环 3080，再 `exec dsh web`）。
   `socat` 是必需的，`python3/make/g++` 是给智能体装 npm 原生依赖用的。
   No `CMD` needed — the app supplies the boot command; `socat` is mandatory.
3. **一条 DNS 记录**：`work.<你的域名>` 指向本机。

### 7.2 打开 / Turning it on

```ini
# deploy/selfhost/.env
WORK_ENABLED=1                      # 应用开关
COMPOSE_PROFILES=work               # 启动 docker-socket-proxy（编排开关）
WORK_DOMAIN=work.dsh.example.com    # Caddy 的 work 站点 + 应用跳转目标
COOKIE_DOMAIN=.dsh.example.com      # 会话 cookie 必须能带到子域，注意前导点
WORK_IMAGE=dsh-cloud-workspace:local
```

```bash
docker compose --env-file .env up -d
```

或者一步到位：`./scripts/quickstart.sh --domain dsh.example.com --work`
（自动填好上面四项，只剩镜像和 DNS 要你准备）。

验证 / Verify：浏览器打开 `https://dsh.example.com/work` → 应跳到
`https://work.dsh.example.com/` 并在 5–20 秒内出现 dsh 界面；
`docker ps | grep dshwork-` 能看到该用户的容器。

### 7.3 容量与计费 / Capacity and metering

| 变量 | 默认 | 含义 |
|---|---|---|
| `WORK_MAX_CONCURRENT` | 40 | 同时在跑的工作台上限。**`上限 × WORK_MEM_LIMIT_MB` 必须放得进宿主内存** |
| `WORK_MEM_LIMIT_MB` / `WORK_CPUS` | 512 / 1.0 | 单容器资源上限 |
| `WORK_CREDITS_PER_MIN` | 2 | 只按「智能体真的调用了网关」的活跃分钟计费；开着页面发呆不收钱 |
| `WORK_IDLE_STOP_MIN` | 15 | 无浏览器流量则停容器（卷保留，下次秒起） |
| `WORK_AGENT_IDLE_STOP_MIN` | 30 | 页面开着但智能体长期不干活也停，回收内存 |
| `WORK_FREE_MINUTES` | 120 | 每人免费的活跃分钟数，用完进「工作台通行证」付费墙（`WORK_PASS_*`）。私有化部署把它调得很大即可等同于关闭付费墙 |

### 7.4 关掉 / Turning it off

```ini
WORK_ENABLED=0
COMPOSE_PROFILES=
WORK_DOMAIN=
```
```bash
docker compose --env-file .env up -d --remove-orphans
```

`COMPOSE_PROFILES` 留空后 `dhc-docker-proxy` 根本不会启动 —— **本栈没有任何容器挂载
docker socket**；`WORK_DOMAIN` 留空后 Caddy 的 work 站点退回 `work.localhost`
（内部 CA），不会去申请你并不拥有的域名的证书。
With the profile empty, nothing in the stack mounts the docker socket at all.

---

## 8. 常见问题 / Troubleshooting

### 端口被占用 / Port already in use

```
Error starting userland proxy: listen tcp4 0.0.0.0:80: bind: address already in use
```
```bash
sudo ss -lptn 'sport = :80' ; sudo ss -lptn 'sport = :443'   # 谁占着
```
两条路：停掉占用者；或改端口 —— `.env` 里 `HTTP_PORT=8080`、`HTTPS_PORT=8443`，
并把端口写进 `PUBLIC_BASE`（如 `http://localhost:8080`）。
注意非 80/443 时 Let's Encrypt 的 HTTP-01 / TLS-ALPN 挑战无法完成，需要在上游把外部
80/443 转发进来，或改用 DNS 挑战。
Either free the port, or move both ports and put the port into `PUBLIC_BASE` —
but ACME challenges still need external 80/443 forwarded here.

### 证书签发失败 / Certificate not issued

```bash
docker compose -f deploy/selfhost/docker-compose.yml logs dhc-caddy | tail -50
```
按顺序排查 / Check in this order:

1. `dig +short dsh.example.com` 是否返回本机公网 IP（云厂商还要看安全组）。
2. 入站 **80 和 443 都要开**：HTTP-01 走 80，TLS-ALPN 走 443。
3. `PUBLIC_BASE` / `DOMAIN` 拼写与实际访问域名一致（含有无 `www.`）。
4. 反复失败会撞 Let's Encrypt 的限流（同域名 1 小时 5 次失败）。先用 staging 验证：
   在 `deploy/selfhost/Caddyfile` **文件最开头**加全局块，跑通后删掉并 `docker compose restart dhc-caddy`：
   ```
   {
       acme_ca https://acme-staging-v02.api.letsencrypt.org/directory
   }
   ```
5. 站在 Cloudflare 等代理后面时，源站证书由 Caddy 签，代理侧请用 Full (strict)。

### 容器起不来 / Container will not start

```bash
docker compose -f deploy/selfhost/docker-compose.yml ps
docker compose -f deploy/selfhost/docker-compose.yml logs --tail 100 dhc-server
```

| 日志里看到 | 原因 / 修法 |
|---|---|
| `RuntimeError: AUTH_SECRET must be set` | `.env` 里 `AUTH_SECRET` 为空 → `openssl rand -hex 32` 填上再 `up -d` |
| `UPSTREAM_API_KEY is not set — the LLM gateway will answer 503` | 只是警告；填上 key 即可 |
| 反复重启无日志 | 多半是构建产物旧了：`docker compose --env-file .env up -d --build --force-recreate` |

手工探活（绕过 Caddy，判断是应用问题还是代理问题）：
Probe the app directly to tell an app problem from a proxy problem:
```bash
docker compose -f deploy/selfhost/docker-compose.yml exec -T dhc-server \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8100/api/health').read())"
```

### 登录不进去 / Cannot log in

- 验证码收不到：`MAIL_SMTP_*` 没配（`/api/auth/email/send` 返回 503）。公网请配置 SMTP
  或 OAuth；本地模式可用 `DHC_DEV=1` 从日志里读验证码。
- 提示 `registration_disabled`：`ALLOW_REGISTRATION=0`，这是私有化部署的邀请制开关。
- OAuth 回调 400/redirect_uri_mismatch：Google/GitHub 后台登记的回调必须与
  `${PUBLIC_BASE}/api/auth/google|github/callback` **逐字符一致**。

### 模型请求报错 / Model requests fail

| 返回 | 含义 |
|---|---|
| `503` | `UPSTREAM_API_KEY` 为空 |
| `404 model_not_found` | 请求的 id 不在 `server/config/models.json` 里 |
| `402 insufficient_quota` | 积分用完 → 管理员 `/api/admin/grant-credits` 或去购买 |
| `429` | 触发 `GATEWAY_QPS` / 并发闸 |
| 上游 4xx 原样透传 | 你的上游 key、额度或模型名的问题，先用 curl 直连上游验证 |

### 云工作台打不开 / Workspace does not open

```bash
docker ps | grep dshwork-                       # 用户容器起来了吗
docker logs --tail 50 dshwork-<hex>             # 容器内 dsh 的日志
docker compose -f deploy/selfhost/docker-compose.yml logs dhc-server | grep -i work
```
逐项对照 / Check list:

- `COMPOSE_PROFILES=work` 是否生效（`docker compose ps` 里要有 `dhc-docker-proxy`）；
- `WORK_IMAGE` 是否真的在**本机**（如 `docker image inspect dsh-cloud-workspace:local`）；
- `work.<域名>` 的 DNS 记录与证书是否就绪；
- `COOKIE_DOMAIN=.<域名>`（带前导点）——否则会话带不到子域，表现为无限跳登录页；
- 页面能开但**回复永远不来** → 多半是有人删了 Caddyfile 里 `forward_auth` 的
  `header_up -Upgrade`（WebSocket 的鉴权子请求会被当成握手 → 403）；
- **一切都 403** → 多半是删了 `header_up Host 127.0.0.1:3080` /
  `header_up Origin http://127.0.0.1:3080`（dsh 只信任来自自身回环的请求）。

---

## 9. 日常运维 / Day-2 operations

```bash
cd deploy/selfhost

# 日志 / logs
docker compose --env-file .env logs -f dhc-server

# 升级：拉代码重建，数据卷不动 / upgrade in place
git -C ../.. pull && docker compose --env-file .env up -d --build

# 备份 SQLite（卷名 dsh-selfhost_dhc-data）/ back up the database volume
docker compose --env-file .env exec -T dhc-server \
  python -c "import sqlite3;sqlite3.connect('/app/data/dhc.db').backup(sqlite3.connect('/app/data/backup.db'))"
docker run --rm -v dsh-selfhost_dhc-data:/data -v "$PWD:/out" alpine \
  tar czf /out/dhc-data-$(date +%F).tgz -C /data .

# 停止 / 卸载（-v 会连数据一起删，慎用）
docker compose --env-file .env down
```

- **密钥轮换**：上游 key 改完重启即可（无状态）；`AUTH_SECRET` 轮换会让所有用户与设备
  重新登录，数据不受影响。
- **扩容**：限流、并发闸、QPS 桶目前是单进程语义（单 worker 正确）。要上多 worker/多机，
  先把这三处换成 Redis 实现，并把 `DB_BACKEND` 切到 `postgres`（表结构会自动建）。
- **监控**：把 `/api/health` 挂到 Uptime 探针；容器日志即应用日志，不含消息正文。

---

## 10. 安全须知 / Security notes

- **上游 key 只存在于服务端进程**，客户端只拿到可吊销的用户/设备 token；吊销设备或
  bump 用户 epoch 即可一键全灭。
  The upstream key never leaves the server process.
- **docker-socket-proxy 的权限不可放宽**（`EXEC=0`、`VOLUMES=0`），且只在
  `COMPOSE_PROFILES=work` 时才启动。
- **`.env` 是全部机密的唯一落点**：`chmod 600`，永远不要提交（仓库 `.gitignore`
  已经拦了 `deploy/**/.env`）。
- **公网部署必须 `DHC_DEV=0`**：开发模式会把验证码写进日志并去掉 cookie 的 Secure 标记。
- **法律文本**：`legal/` 下是草案，且署名为原运营方。`/legal/*` 页面在没有文档时显示
  「待发布」；要上线请写自己的文档，设 `LEGAL_ENTITY_*` / `LEGAL_CONTACT_EMAIL`，
  并在 `docker-compose.yml` 里打开 legal 目录挂载。
  The shipped legal drafts name the original operator — write your own before launch.
- **私有化建议值 / suggested for an internal deployment**：
  `ALLOW_REGISTRATION=0`（先建好账号再关）、`OAUTH_AUTO_REGISTER=0`、
  `ENTITLE_ENFORCE=0`（只记账不拦人）、`FREE_SIGNUP_CREDITS` 给一个很大的数、
  `UPSTREAM_BASE_URL` 指向内网推理服务。
