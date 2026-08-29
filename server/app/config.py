"""Central configuration. Every environment variable the server reads is declared here.

Deployment notes live in deploy/.env.example — keep the two in sync.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("DHC_CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))
DATA_DIR = Path(os.environ.get("DHC_DATA_DIR", "/app/data"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# --- identity / crypto ------------------------------------------------------
# MUST be a long random value in production. Startup refuses to serve without it
# unless DHC_DEV=1 (see main.py).
def auth_secret() -> str:
    inline = _env("AUTH_SECRET")
    if inline:
        return inline
    secret_file = _env("AUTH_SECRET_FILE")
    if not secret_file:
        return ""
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        # Startup treats an absent or unreadable secret mount as no secret.
        return ""


DEV_MODE = _env_bool("DHC_DEV", False)
# Application log level; INFO preserves lifecycle and billing diagnostics.
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
RELEASE_VERSION = _env("RELEASE_VERSION", "dev")
RELEASE_REVISION = _env("RELEASE_REVISION", "")
SESSION_COOKIE = "dhc_session"
SESSION_TTL = _env_int("AUTH_TOKEN_TTL", 90 * 24 * 3600)  # browser session tokens
DEVICE_TOKEN_TTL = _env_int("DEVICE_TOKEN_TTL", 365 * 24 * 3600)  # desktop device tokens

# --- database ---------------------------------------------------------------
DB_BACKEND = _env("DB_BACKEND", "sqlite")  # sqlite | postgres
DB_PATH = _env("DB_PATH", str(DATA_DIR / "dhc.db"))
POSTGRES_DSN = _env("POSTGRES_DSN")  # required when DB_BACKEND=postgres

# --- upstream LLM (the provider credential never leaves this process) -------
# The default endpoint is OpenAI-compatible; operators may replace it.
UPSTREAM_BASE_URL = _env("UPSTREAM_BASE_URL", "https://api.qianmian.ai/v1")
UPSTREAM_API_KEY = _env("UPSTREAM_API_KEY")
UPSTREAM_TIMEOUT_S = _env_float("UPSTREAM_TIMEOUT_S", 600.0)

# --- inbound HTTP bounds ----------------------------------------------------
# Ordinary JSON API requests should remain small.
API_BODY_MAX_BYTES = _env_int("API_BODY_MAX_BYTES", 2 * 1024 * 1024)
# Multimodal prompts and tool definitions are commonly larger than account API
# payloads. The gateway still buffers JSON, so keep the default finite.
GATEWAY_BODY_MAX_BYTES = _env_int("GATEWAY_BODY_MAX_BYTES", 32 * 1024 * 1024)
# 官方节点的素材输入 (参考图、参考视频、驱动音频) 要先换成一个**公网可取**的 URL
# 才能交给上游厂商, 所以本站得存一下。只是中转, 存活期很短。
# 「auto」时长的预扣估值。真实秒数出片后由 usage.output_video_duration 结算,
# 这个数只影响预扣多少 —— 实测 wan3.0 的 auto 出 5 秒。
VIDEO_AUTO_DURATION_S = _env_int("VIDEO_AUTO_DURATION_S", 5)
MEDIA_UPLOAD_MAX_BYTES = _env_int("MEDIA_UPLOAD_MAX_BYTES", 64 * 1024 * 1024)
MEDIA_UPLOAD_TTL_S = _env_int("MEDIA_UPLOAD_TTL_S", 6 * 3600)
# Preview applications may accept file uploads through the authenticated proxy.
PREVIEW_BODY_MAX_BYTES = _env_int("PREVIEW_BODY_MAX_BYTES", 64 * 1024 * 1024)
# HTML preview responses must be buffered briefly to inject their base URL.
PREVIEW_HTML_MAX_BYTES = _env_int("PREVIEW_HTML_MAX_BYTES", 8 * 1024 * 1024)
# Provider notifications are small signed documents; keep their attack surface
# narrower without changing the normal webhook contract.
WEBHOOK_BODY_MAX_BYTES = _env_int("WEBHOOK_BODY_MAX_BYTES", 256 * 1024)
# Total time allowed to receive a protected request body, including all chunks.
REQUEST_BODY_TIMEOUT_S = _env_float("REQUEST_BODY_TIMEOUT_S", 30.0)

# --- web_search backend -----------------------------------------------------
# dsh's web-search-deepseek speaks Anthropic Messages and expects native
# web_search result blocks. Two ways to serve it:
#   SEARCH_PROVIDER=zhipu     translate to Zhipu web_search (open.bigmodel.cn),
#                             cheap per-call; needs ZHIPU_SEARCH_API_KEY
#   SEARCH_PROVIDER=upstream  proxy verbatim to UPSTREAM_ANTHROPIC_BASE/messages
#                             (DeepSeek official — a full billed model turn)
SEARCH_PROVIDER = _env("SEARCH_PROVIDER", "zhipu").lower()
ZHIPU_SEARCH_API_KEY = _env("ZHIPU_SEARCH_API_KEY")
# Prefer a search engine that returns source URLs; fallbacks may have separate quotas.
ZHIPU_SEARCH_ENGINE = _env("ZHIPU_SEARCH_ENGINE", "search_pro_sogou")
ZHIPU_SEARCH_FALLBACKS = [
    e.strip() for e in _env("ZHIPU_SEARCH_FALLBACKS", "search_pro,search_std").split(",") if e.strip()
]
ZHIPU_SEARCH_BASE = _env("ZHIPU_SEARCH_BASE", "https://open.bigmodel.cn/api/paas/v4")
UPSTREAM_ANTHROPIC_BASE = _env("UPSTREAM_ANTHROPIC_BASE", "https://api.deepseek.com/anthropic/v1")

# --- pricing / credits ------------------------------------------------------
# Which price table to serve: pricing.json (CNY) or pricing.usd.json (overseas).
PRICING_FILE = _env("PRICING_FILE", "pricing.usd.json")
MODEL_PRICE_MARKUP = _env_float("MODEL_PRICE_MARKUP", 1.2)
FREE_SIGNUP_CREDITS = _env_int("FREE_SIGNUP_CREDITS", 500)  # $1 = 100 credits
SEARCH_CALL_CREDITS = _env_int("SEARCH_CALL_CREDITS", 1)  # flat per web_search call, on top of tokens
OVERDRAFT_LIMIT_CREDITS = _env_int("OVERDRAFT_LIMIT_CREDITS", 20)  # in-flight streams may finish

# --- 视频生成 -----------------------------------------------------------------
# 售价在 config/video_models.json (手写: 上游单价只在供应商控制台里, 没有 API)。
# 那份文件默认全部未定价, 于是视频端点默认对所有模型 404 —— 要开卖得先填价。
# 媒体生成默认只对管理员开放。价格是手填的、还没经过真实账单核对, 在那之前
# 让所有人可用 = 拿真金白银试错。填准价格并确认毛利后再置 0 开放全量。
MEDIA_ADMIN_ONLY = _env_bool("MEDIA_ADMIN_ONLY", True)
VIDEO_DEFAULT_DURATION = _env_int("VIDEO_DEFAULT_DURATION", 5)
VIDEO_DEFAULT_RESOLUTION = _env("VIDEO_DEFAULT_RESOLUTION", "480p")
# 生成失败时退回的积分能活多久。作业是提交时预扣的, 失败退款只是把钱放回去,
# 所以给足时间, 别让用户因为我们的上游出错而损失额度。
VIDEO_REFUND_TTL_S = _env_float("VIDEO_REFUND_TTL_S", 365 * 24 * 3600)
# 服务端兜底: 作业状态不能只靠客户端轮询驱动 —— 浏览器一关、节点一报错,
# 作业就永远停在 processing, 而钱是提交时就扣掉的 (失败不退, 成功不记账)。
VIDEO_RECONCILE_INTERVAL_S = _env_float("VIDEO_RECONCILE_INTERVAL_S", 60.0)
# 超过这么久还没终态就判失败并退款。上游偶尔会把作业丢掉 —— 既不 succeeded
# 也不 failed, 就是不动, 不设上限那笔钱永远悬着。
VIDEO_JOB_MAX_AGE_S = _env_float("VIDEO_JOB_MAX_AGE_S", 30 * 60)
# 一次请求最多出几张。图是同步出的, 批量大了会把 UPSTREAM_TIMEOUT_S 顶穿。
IMAGE_MAX_BATCH = _env_int("IMAGE_MAX_BATCH", 4)

# --- 阿里云百炼 (直连, 不经千面) ---------------------------------------------
# 千面没有的两项能力在这里: 图像编辑 (qwen-image-edit) 与视频编辑 (vace)。
#
# ⚠️ NATIVE_BASE 必须是**业务空间专属域名**, 绝不能回落到公共的
# dashscope.aliyuncs.com —— 打公共域名一样能通、结果也一样, 但**预付套餐不抵扣,
# 走按量计费, 且没有任何报错提示** (AgentsDance 2026-08-12 踩过, 见其
# backend/dataset/bailian_native.py 的注释)。所以默认值留空: 没配就不启用,
# 而不是偷偷去打公共域名。
BAILIAN_NATIVE_BASE = _env("BAILIAN_NATIVE_BASE", "").rstrip("/")
BAILIAN_API_KEY = _env("BAILIAN_API_KEY")

# --- gateway guards ---------------------------------------------------------
GATEWAY_QPS = _env_float("GATEWAY_QPS", 5.0)  # per-user requests/second (token bucket)
GATEWAY_QPS_BURST = _env_int("GATEWAY_QPS_BURST", 15)
ENTITLE_ENFORCE = _env_bool("ENTITLE_ENFORCE", True)  # escape hatch: 0 disables credit gating

# --- public URLs ------------------------------------------------------------
PUBLIC_BASE = _env("PUBLIC_BASE", "http://127.0.0.1:8100")  # e.g. https://dsh.example.com

# --- mail (email verification codes) ----------------------------------------
MAIL_SMTP_HOST = _env("MAIL_SMTP_HOST")
MAIL_SMTP_PORT = _env_int("MAIL_SMTP_PORT", 465)
MAIL_SMTP_USER = _env("MAIL_SMTP_USER")
MAIL_SMTP_PASS = _env("MAIL_SMTP_PASS")
MAIL_FROM = _env("MAIL_FROM", MAIL_SMTP_USER)

# --- registration switches --------------------------------------------------
ALLOW_REGISTRATION = _env_bool("ALLOW_REGISTRATION", True)

# --- oauth login (Google / GitHub; a provider activates when its pair is set) ---
GOOGLE_LOGIN_CLIENT_ID = _env("GOOGLE_LOGIN_CLIENT_ID") or _env("AGENT_PLUGIN_GOOGLE_CLIENT_ID")
GOOGLE_LOGIN_CLIENT_SECRET = _env("GOOGLE_LOGIN_CLIENT_SECRET") or _env("AGENT_PLUGIN_GOOGLE_CLIENT_SECRET")
GOOGLE_LOGIN_REDIRECT_URI = _env("GOOGLE_LOGIN_REDIRECT_URI") or (
    PUBLIC_BASE.rstrip("/") + "/api/auth/google/callback"
)
GITHUB_LOGIN_CLIENT_ID = _env("GITHUB_LOGIN_CLIENT_ID")
GITHUB_LOGIN_CLIENT_SECRET = _env("GITHUB_LOGIN_CLIENT_SECRET")
GITHUB_LOGIN_REDIRECT_URI = _env("GITHUB_LOGIN_REDIRECT_URI") or (
    PUBLIC_BASE.rstrip("/") + "/api/auth/github/callback"
)
OAUTH_AUTO_REGISTER = _env_bool("OAUTH_AUTO_REGISTER", True)

# --- payments (a provider activates when its variables are set) -------------
# Waffo (overseas merchant of record; see app/payments/waffo_provider.py)
WAFFO_MERCHANT_ID = _env("WAFFO_MERCHANT_ID")
WAFFO_PRIVATE_KEY = _env("WAFFO_PRIVATE_KEY")
WAFFO_WEBHOOK_PUBLIC_KEY = _env("WAFFO_WEBHOOK_PUBLIC_KEY")
WAFFO_PRODUCT_ID = _env("WAFFO_PRODUCT_ID")
WAFFO_STORE_ID = _env("WAFFO_STORE_ID")
WAFFO_ENV = _env("WAFFO_ENV", "test")
WAFFO_API_BASE = _env("WAFFO_API_BASE", "https://api.waffo.ai")

STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
ALIPAY_APP_ID = _env("ALIPAY_APP_ID")
ALIPAY_APP_PRIVATE_KEY = _env("ALIPAY_APP_PRIVATE_KEY")
ALIPAY_PUBLIC_KEY = _env("ALIPAY_PUBLIC_KEY")
WECHAT_PAY_MCHID = _env("WECHAT_PAY_MCHID")
WECHAT_PAY_SERIAL_NO = _env("WECHAT_PAY_SERIAL_NO")
WECHAT_PAY_PRIVATE_KEY_PATH = _env("WECHAT_PAY_PRIVATE_KEY_PATH")
WECHAT_PAY_APIV3_KEY = _env("WECHAT_PAY_APIV3_KEY")
WECHAT_PAY_APPID = _env("WECHAT_PAY_APPID")

# --- cloud workspaces (dshwork: per-user dsh containers, phone-usable) ------
WORK_ENABLED = _env_bool("WORK_ENABLED", False)
WORK_DOMAIN = _env("WORK_DOMAIN", "")  # dsh UI host; empty = workspace off (self-host safe default)

# 本部署缺的能力 (没配桌面安装包 / 没开云工作台), 页面挂出官方托管版作为去处。
# 这是有意的引流, 也是对访客有用的信息 —— 但必须**明说那是托管版**, 而且自部署
# 方可以整条关掉 (置空)。绝不做成"点了以为是自己的服务、其实到了别人那里"。
HOSTED_SITE = _env("HOSTED_SITE", "https://dshcloud.online").rstrip("/")
WORK_IMAGE = _env("WORK_IMAGE", "dsh-local:rc8")
WORK_NETWORK = _env("WORK_NETWORK", "dshwork-net")
DOCKER_PROXY_URL = _env("DOCKER_PROXY_URL", "http://dhc-docker-proxy:2375")
# 工作台跑在哪:
#   docker  本机引擎, 经受限 socket 代理。可 stop/start, 恢复几秒。自部署只有这条路。
#   eci     阿里云弹性容器实例。**没有"停止但保留"**, 闲置回收就是删除, 因此
#           WORK_NAS_* 必须配上, 否则用户的文件和会话会随回收消失。
WORK_BACKEND = _env("WORK_BACKEND", "docker")
# ECI 拉的是仓库引用 (ghcr.io/... 或 ACR), 本机 docker 拉的是本地 tag ——
# 两者不是一回事, 所以分开配。留空则回落到 WORK_IMAGE。
WORK_IMAGE_REF = _env("WORK_IMAGE_REF", "")

# --- ComfyUI 工作台 -----------------------------------------------------------
# 与 dsh 工作台同构的第二种产品: 每用户一个容器, 同一套回收与计费。
# 以**纯编排器**模式运行 (无 GPU, 算力全在远端), 实测内存峰值 583MB, 所以规格
# 与 dsh 工作台同量级。见 deploy/workspace-comfyui/README.md。
# 域名必须独立: ComfyUI 前端用绝对路径引资源, 塞不进子路径。
COMFY_IMAGE = _env("COMFY_IMAGE", "")
COMFY_IMAGE_REF = _env("COMFY_IMAGE_REF", "")
COMFY_DOMAIN = _env("COMFY_DOMAIN", "")
# 规格由**冷启动**决定, 不是由内存峰值决定 (峰值实测 902MB, 1.5G 就够)。
# 2026-08-27 在 ECI 上实测同一镜像、缓存均命中:
#     1 核  59.1 秒  (其中 ComfyUI 自身启动 35.6 秒)
#     2 核  31.8 秒  (ComfyUI 10.5 秒)
#     4 核  31.7 秒  ← 与 2 核持平, 纯浪费
# 2 核是拐点: 再加核也压不动剩下的 21 秒 —— 那是 ECI 的调度时间, 与我们无关。
# 内存跟着 CPU 走 ECI 的规格档 (1:2), 给 4G 不是因为需要, 是因为 2 核就配这么多。
COMFY_MEM_LIMIT_MB = _env_int("COMFY_MEM_LIMIT_MB", 4096)
COMFY_CPUS = _env_float("COMFY_CPUS", 2.0)
ECI_REGION_ID = _env("ECI_REGION_ID", "")
ECI_ZONE_ID = _env("ECI_ZONE_ID", "")
ECI_VSWITCH_ID = _env("ECI_VSWITCH_ID", "")
# The workspace bridge has no application authentication. Its security group
# must allow only the application service and must not be shared broadly.
ECI_SECURITY_GROUP_ID = _env("ECI_SECURITY_GROUP_ID", "")
ECI_ACCESS_KEY_ID = _env("ECI_ACCESS_KEY_ID", "")
ECI_ACCESS_KEY_SECRET = _env("ECI_ACCESS_KEY_SECRET", "")
ECI_COMPUTE_CATEGORY = _env("ECI_COMPUTE_CATEGORY", "economy")
ECI_EIP_BANDWIDTH = _env_int("ECI_EIP_BANDWIDTH", 100)
# NAS。ECI 后端下这不是可选项 —— 容器一删, 容器里的一切都不再存在。
# 每个用户在 WORK_NAS_PATH 下有 <hexid>/home 与 <hexid>/workspace 两个子目录。
WORK_NAS_SERVER = _env("WORK_NAS_SERVER", "")
WORK_NAS_PATH = _env("WORK_NAS_PATH", "/")
# 应用机上把同一个 NAS 挂到哪 (只读即可)。ECI 后端下「個人成品」靠它列出用户的
# 文件 —— 那边容器闲置即销毁, 不挂就意味着用户不在时那个页面永远是空的。
WORK_NAS_LOCAL_MOUNT = _env("WORK_NAS_LOCAL_MOUNT", "")
# 智能体生成的内容单独放一个域, 与会话源隔开。留空 = 仍在主站上提供 (靠
# Content-Security-Policy: sandbox 兜底)。
# ⚠️ 它与主站是 same-site, 所以**只有子域是不够的**: SameSite=Lax 拦不住从这里
# 发出的带凭据 POST。真正吃重的是 accounts._cookie_write_allowed 那道 Origin
# 白名单 —— 这个域绝不能出现在白名单里。
PREVIEW_DOMAIN = _env("PREVIEW_DOMAIN", "")
# Billed per ACTIVE minute — a minute in which the agent actually called our
# gateway. Reading a reply or leaving a tab open is free (an open tab polls
# /api/work/route forever, so wall-clock billing charged people for nothing).
WORK_CREDITS_PER_MIN = _env_int("WORK_CREDITS_PER_MIN", 2)
# Host path where the per-user workspace volumes live, mounted read-only. The
# workspace is reclaimed once nobody is using it, but its volume outlives it, so
# this is what lets 個人成品 keep showing a user's files instead of an empty page
# for the hours the container is not running. Empty disables the offline view.
WORK_VOLUME_ROOT = _env("WORK_VOLUME_ROOT", "")

# --- 什么时候回收一台工作台 (workspace.reaper_tick) --------------------------
# 口径是"打开一次, 持续做事, 只关一次": 只有**没人在且没活儿在跑**才回收。
# 三个窗口各管一件事, 谁都不单独构成回收理由。
#
# 标签页还开着 (轮询还在) 时, 允许真人这么久不动一下 —— 读一段长回答、去倒杯
# 水都不该被踢掉。在场只认真实交互, 所以这个窗口可以给得宽松。
WORK_IDLE_STOP_MIN = _env_int("WORK_IDLE_STOP_MIN", 15)
# 连轮询都停了 = 页面已经关掉/被冻结。这时在场窗口缩到这么短, 省下的是用户的
# 机时 (按容器存在时间计费, 关了还留着就是白烧)。别缩到 1 分钟以内: 手机切个
# 应用、隧道抖一下都会短暂断掉轮询, 回来时不该是一次冷启动。
WORK_TAB_GONE_MIN = _env_int("WORK_TAB_GONE_MIN", 3)
# ComfyUI 的冷启动比 dsh 贵得多 (实测 ~26 秒), 所以它的标签页宽限期单列。
COMFY_TAB_GRACE_MIN = _env_int("COMFY_TAB_GRACE_MIN", 10)
# Open Design (nexu-io/open-design) —— 云空间的设计坑位 (design.dshcloud.online)。
# Penpot 曾短暂占过这个位, 老板 2026-08-29 拍板下架、域名让给 open-design。
OPEN_DESIGN_DOMAIN = _env("OPEN_DESIGN_DOMAIN", "")
OPEN_DESIGN_IMAGE_REF = _env("OPEN_DESIGN_IMAGE_REF", "")
OPEN_DESIGN_MEM_LIMIT_MB = _env_int("OPEN_DESIGN_MEM_LIMIT_MB", 1024)
OPEN_DESIGN_CPUS = _env_float("OPEN_DESIGN_CPUS", 1.0)
OPEN_DESIGN_TAB_GRACE_MIN = _env_int("OPEN_DESIGN_TAB_GRACE_MIN", 10)
# Dify —— 云空间的 LLM 应用搭建坑位 (多容器栈, 10 个容器)。
DIFY_DOMAIN = _env("DIFY_DOMAIN", "")
DIFY_VERSION = _env("DIFY_VERSION", "1.17.0")
DIFY_PLUGIN_DAEMON_VERSION = _env("DIFY_PLUGIN_DAEMON_VERSION", "0.6.10-local")
DIFY_SANDBOX_VERSION = _env("DIFY_SANDBOX_VERSION", "0.2.15")
DIFY_MEM_LIMIT_MB = _env_int("DIFY_MEM_LIMIT_MB", 4096)
DIFY_CPUS = _env_float("DIFY_CPUS", 2.0)
DIFY_TAB_GRACE_MIN = _env_int("DIFY_TAB_GRACE_MIN", 10)
# 智能体最后一次调网关之后再等这么久。长任务必须能在关掉标签页之后接着跑完,
# 所以这一条单独顶着, 与"有没有人在"无关。
WORK_AGENT_IDLE_STOP_MIN = _env_int("WORK_AGENT_IDLE_STOP_MIN", 30)

# --- cloud workspace paywall ------------------------------------------------
# Everyone gets this many ACTIVE agent minutes on the house; when they run out,
# the next task hits the paywall instead of silently draining credits. Active
# minutes are the same meter that bills (see workspace.reaper_tick).
# 180 = 3h/month for the free tier; only a fallback for when the price table
# carries no work_minutes for it. The workspace pass that used to top this up
# was withdrawn — machine hours now come from a plan or from nowhere.
WORK_FREE_MINUTES = _env_int("WORK_FREE_MINUTES", 180)

# --- teams ------------------------------------------------------------------
# Seats bound how many people may share an organisation's credit pool. Price is
# minor units per seat per month, in PRICING_CURRENCY.
TEAM_SEAT_PRICE = _env_int("TEAM_SEAT_PRICE", 1500)  # per seat, monthly (minor units)
TEAM_SEAT_CREDITS = _env_int("TEAM_SEAT_CREDITS", 3500)  # pool credits added per seat per cycle
TEAM_SEAT_MINUTES = _env_int("TEAM_SEAT_MINUTES", 1200)  # pool workspace minutes per seat
TEAM_SEAT_MIN = _env_int("TEAM_SEAT_MIN", 3)  # below this it is an individual plan
# Volume bands as "minSeats:percentOff,..." — the discount applies to the seat
# fee only, never to the included credits/minutes (those are real cost).
TEAM_SEAT_TIERS = [
    (int(b.split(":")[0]), int(b.split(":")[1]))
    for b in _env("TEAM_SEAT_TIERS", "10:10,25:15,50:20").split(",")
    if ":" in b
]
# Default per-member ceilings on the shared pools (None = unlimited). Sized as a
# multiple of one seat's contribution so a single person cannot spend the team's
# month, while a normally-heavy user is never nagged.
TEAM_DEFAULT_CREDIT_CAP_X = _env_float("TEAM_DEFAULT_CREDIT_CAP_X", 3.0)
TEAM_DEFAULT_MINUTE_CAP_X = _env_float("TEAM_DEFAULT_MINUTE_CAP_X", 3.0)
WORK_MAX_CONCURRENT = _env_int("WORK_MAX_CONCURRENT", 40)  # global running-container cap
WORK_MEM_LIMIT_MB = _env_int("WORK_MEM_LIMIT_MB", 512)
# Require this much free host memory before allocating another workspace. The
# static concurrency cap alone cannot account for unrelated host workloads.
WORK_MIN_FREE_MB = _env_int("WORK_MIN_FREE_MB", 1536)
# Workspace OOM priority (-1000..1000); higher values are reclaimed first.
WORK_OOM_SCORE_ADJ = _env_int("WORK_OOM_SCORE_ADJ", 800)
WORK_CPUS = _env_float("WORK_CPUS", 1.0)
WORK_START_TIMEOUT_S = _env_float("WORK_START_TIMEOUT_S", 45.0)

# Session cookie domain: set to ".dshcloud.online" so the browser sends the
# session to the work subdomain too. Empty = host-only (single-domain deploys).
COOKIE_DOMAIN = _env("COOKIE_DOMAIN", "")

# Optional starting value for download counters migrated from another system.
DOWNLOAD_COUNT_BASE = _env_int("DOWNLOAD_COUNT_BASE", 0)

# --- admin ------------------------------------------------------------------
ADMIN_EMAILS = [e.strip().lower() for e in _env("ADMIN_EMAILS").split(",") if e.strip()]

# --- legal entity (rendered into legal pages; replace with your company) ----
LEGAL_ENTITY_ZH = _env("LEGAL_ENTITY_ZH", "")
LEGAL_ENTITY_EN = _env("LEGAL_ENTITY_EN", "")
LEGAL_CONTACT_EMAIL = _env("LEGAL_CONTACT_EMAIL", "")
ICP_NUMBER = _env("ICP_NUMBER", "")  # e.g. 京ICP备XXXXXXXX号-X
PSB_NUMBER = _env("PSB_NUMBER", "")  # e.g. 京公网安备XXXXXXXXXXXXX号
