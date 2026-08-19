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
    return _env("AUTH_SECRET")


DEV_MODE = _env_bool("DHC_DEV", False)
SESSION_COOKIE = "dhc_session"
SESSION_TTL = _env_int("AUTH_TOKEN_TTL", 90 * 24 * 3600)  # browser session tokens
DEVICE_TOKEN_TTL = _env_int("DEVICE_TOKEN_TTL", 365 * 24 * 3600)  # desktop device tokens

# --- database ---------------------------------------------------------------
DB_BACKEND = _env("DB_BACKEND", "sqlite")  # sqlite | postgres
DB_PATH = _env("DB_PATH", str(DATA_DIR / "dhc.db"))
POSTGRES_DSN = _env("POSTGRES_DSN")  # required when DB_BACKEND=postgres

# --- upstream LLM (the money secret: never leaves this process) -------------
# Default points at the Alibaba-hosted qianmian gateway, which is OpenAI-compatible
# and serves deepseek-v4-flash / deepseek-v4-pro under the same ids dsh sends.
UPSTREAM_BASE_URL = _env("UPSTREAM_BASE_URL", "https://api.qianmian.ai/v1")
UPSTREAM_API_KEY = _env("UPSTREAM_API_KEY")
UPSTREAM_TIMEOUT_S = _env_float("UPSTREAM_TIMEOUT_S", 600.0)

# --- web_search backend -----------------------------------------------------
# dsh's web-search-deepseek speaks Anthropic Messages and expects native
# web_search result blocks. Two ways to serve it:
#   SEARCH_PROVIDER=zhipu     translate to Zhipu web_search (open.bigmodel.cn),
#                             cheap per-call; needs ZHIPU_SEARCH_API_KEY
#   SEARCH_PROVIDER=upstream  proxy verbatim to UPSTREAM_ANTHROPIC_BASE/messages
#                             (DeepSeek official — a full billed model turn)
SEARCH_PROVIDER = _env("SEARCH_PROVIDER", "zhipu").lower()
ZHIPU_SEARCH_API_KEY = _env("ZHIPU_SEARCH_API_KEY")
# Engine ladder. dsh discards any result without a url, and as of 2026-08-16
# Zhipu's search_pro/search_std answer 200 with rich content but an EMPTY link
# on every row — so the default leads with an engine that carries real links
# and falls back through the others (each may have its own quota).
ZHIPU_SEARCH_ENGINE = _env("ZHIPU_SEARCH_ENGINE", "search_pro_sogou")
ZHIPU_SEARCH_FALLBACKS = [e.strip() for e in _env(
    "ZHIPU_SEARCH_FALLBACKS", "search_pro,search_std").split(",") if e.strip()]
ZHIPU_SEARCH_BASE = _env("ZHIPU_SEARCH_BASE", "https://open.bigmodel.cn/api/paas/v4")
UPSTREAM_ANTHROPIC_BASE = _env("UPSTREAM_ANTHROPIC_BASE", "https://api.deepseek.com/anthropic/v1")

# --- pricing / credits ------------------------------------------------------
# Which price table to serve: pricing.json (CNY) or pricing.usd.json (overseas).
PRICING_FILE = _env("PRICING_FILE", "pricing.usd.json")
MODEL_PRICE_MARKUP = _env_float("MODEL_PRICE_MARKUP", 1.2)
FREE_SIGNUP_CREDITS = _env_int("FREE_SIGNUP_CREDITS", 500)  # $1 = 100 credits
SEARCH_CALL_CREDITS = _env_int("SEARCH_CALL_CREDITS", 1)  # flat per web_search call, on top of tokens
OVERDRAFT_LIMIT_CREDITS = _env_int("OVERDRAFT_LIMIT_CREDITS", 20)  # in-flight streams may finish

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
GOOGLE_LOGIN_REDIRECT_URI = _env("GOOGLE_LOGIN_REDIRECT_URI") or (PUBLIC_BASE.rstrip("/") + "/api/auth/google/callback")
GITHUB_LOGIN_CLIENT_ID = _env("GITHUB_LOGIN_CLIENT_ID")
GITHUB_LOGIN_CLIENT_SECRET = _env("GITHUB_LOGIN_CLIENT_SECRET")
GITHUB_LOGIN_REDIRECT_URI = _env("GITHUB_LOGIN_REDIRECT_URI") or (PUBLIC_BASE.rstrip("/") + "/api/auth/github/callback")
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
WORK_IMAGE = _env("WORK_IMAGE", "dsh-local:rc6")
WORK_NETWORK = _env("WORK_NETWORK", "dshwork-net")
DOCKER_PROXY_URL = _env("DOCKER_PROXY_URL", "http://dhc-docker-proxy:2375")
# Billed per ACTIVE minute — a minute in which the agent actually called our
# gateway. Reading a reply or leaving a tab open is free (an open tab polls
# /api/work/route forever, so wall-clock billing charged people for nothing).
WORK_CREDITS_PER_MIN = _env_int("WORK_CREDITS_PER_MIN", 2)
# Host path where the per-user workspace volumes live, mounted read-only. The
# workspace stops after 15 idle minutes but its volume outlives it, so this is
# what lets 個人成品 keep showing a user's files instead of an empty page for
# the 23 hours a day the container is asleep. Empty disables the offline view.
WORK_VOLUME_ROOT = _env("WORK_VOLUME_ROOT", "")
WORK_IDLE_STOP_MIN = _env_int("WORK_IDLE_STOP_MIN", 15)      # no browser traffic -> stop
# Capacity backstop: idle minutes are free, RAM is not. A tab left open with the
# agent doing nothing this long is stopped too (volumes persist, resume is fast).
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
TEAM_SEAT_PRICE = _env_int("TEAM_SEAT_PRICE", 1500)      # per seat, monthly (minor units)
TEAM_SEAT_CREDITS = _env_int("TEAM_SEAT_CREDITS", 3500)  # pool credits added per seat per cycle
TEAM_SEAT_MINUTES = _env_int("TEAM_SEAT_MINUTES", 1200)  # pool workspace minutes per seat
TEAM_SEAT_MIN = _env_int("TEAM_SEAT_MIN", 3)             # below this it is an individual plan
# Volume bands as "minSeats:percentOff,..." — the discount applies to the seat
# fee only, never to the included credits/minutes (those are real cost).
TEAM_SEAT_TIERS = [
    (int(b.split(":")[0]), int(b.split(":")[1]))
    for b in _env("TEAM_SEAT_TIERS", "10:10,25:15,50:20").split(",") if ":" in b
]
# Default per-member ceilings on the shared pools (None = unlimited). Sized as a
# multiple of one seat's contribution so a single person cannot spend the team's
# month, while a normally-heavy user is never nagged.
TEAM_DEFAULT_CREDIT_CAP_X = _env_float("TEAM_DEFAULT_CREDIT_CAP_X", 3.0)
TEAM_DEFAULT_MINUTE_CAP_X = _env_float("TEAM_DEFAULT_MINUTE_CAP_X", 3.0)
WORK_MAX_CONCURRENT = _env_int("WORK_MAX_CONCURRENT", 40)    # global running-container cap
WORK_MEM_LIMIT_MB = _env_int("WORK_MEM_LIMIT_MB", 512)
# 起新工作台前要求宿主至少还剩这么多可用内存(MB, 不含即将分配的那 512)。
# WORK_MAX_CONCURRENT 是**静态**上限, 它不知道同机还跑着别的东西 —— 本机与
# a sibling production system 全栈共用 14G, 8 × 512M 的额度在对方峰值时可能就是压垮线。
# 而 Linux 的 OOM killer 不挑肇事者, 它按内存占用选, 最可能被杀的是 postgres
# 或 elasticsearch 这种大块头, 而不是闯祸的工作台。所以在**分配之前**就拦。
WORK_MIN_FREE_MB = _env_int("WORK_MIN_FREE_MB", 1536)
# 工作台容器的 OOM 优先级(-1000..1000, 越大越先被杀)。真到了内存悬崖, 该死的是
# 一个可随时重启、卷还在的工作台, 不是别人的数据库。0 = 与系统默认同权。
WORK_OOM_SCORE_ADJ = _env_int("WORK_OOM_SCORE_ADJ", 800)
WORK_CPUS = _env_float("WORK_CPUS", 1.0)
WORK_START_TIMEOUT_S = _env_float("WORK_START_TIMEOUT_S", 45.0)

# Session cookie domain: set to ".dshcloud.online" so the browser sends the
# session to the work subdomain too. Empty = host-only (single-domain deploys).
COOKIE_DOMAIN = _env("COOKIE_DOMAIN", "")

# Downloads served before we started counting (installers published by hand).
# Shown on the homepage so the number reflects reality rather than restarting
# at zero on every deploy.
DOWNLOAD_COUNT_BASE = _env_int("DOWNLOAD_COUNT_BASE", 0)

# --- admin ------------------------------------------------------------------
ADMIN_EMAILS = [e.strip().lower() for e in _env("ADMIN_EMAILS").split(",") if e.strip()]

# --- legal entity (rendered into legal pages; replace with your company) ----
LEGAL_ENTITY_ZH = _env("LEGAL_ENTITY_ZH", "")
LEGAL_ENTITY_EN = _env("LEGAL_ENTITY_EN", "")
LEGAL_CONTACT_EMAIL = _env("LEGAL_CONTACT_EMAIL", "")
ICP_NUMBER = _env("ICP_NUMBER", "")  # e.g. 京ICP备XXXXXXXX号-X
PSB_NUMBER = _env("PSB_NUMBER", "")  # e.g. 京公网安备XXXXXXXXXXXXX号
