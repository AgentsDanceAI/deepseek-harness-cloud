"""Central configuration. Every environment variable the server reads is declared here.

Deployment notes live in deploy/.env.example — keep the two in sync.
"""

from __future__ import annotations

import os
import re
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
# Anthropic 面转发到哪。**默认跟着 UPSTREAM_BASE_URL 走** —— 我们的密钥是那家的,
# 指到别家去只会被 401, 而我们把 401 映射成 502, 于是表现成"上游挂了"。
# 早先写死成 DeepSeek 是 web_search 走它家搜索时代的遗留; 现在搜索走智谱, 这个
# 地址只服务普通对话转发, 跟着主上游才对 (千面自己就有原生 /v1/messages, 实测
# x-api-key 打过去 200, 返回原生 content 块)。
UPSTREAM_ANTHROPIC_BASE = _env("UPSTREAM_ANTHROPIC_BASE", "") or UPSTREAM_BASE_URL

# Gemini 面转发到哪。Google 原生协议的路径是 `/v1beta/models/{model}:generateContent`
# —— 注意它**不挂在 /v1 下面**, 所以这里要的是上游的根, 不是 UPSTREAM_BASE_URL。
# 默认由主上游推导 (千面: https://api.qianmian.ai/v1 -> https://api.qianmian.ai),
# 实测 x-goog-api-key 打过去 200, 返回原生 candidates。
UPSTREAM_GEMINI_BASE = _env("UPSTREAM_GEMINI_BASE", "") or re.sub(r"/v1beta/?$|/v1/?$", "", UPSTREAM_BASE_URL)

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
# OpenHands —— 自主编码智能体 (2026-09-01)。
# 用**官方小镜像** openhands/openhands (解开 1.78GB), 不是 agent-canvas 那个
# all-in-one (5.81GB): 小的把设置存在服务端, POST 一次 /api/v1/settings 对所有
# 用户都生效; agent-canvas 把首启向导记在浏览器 localStorage 里, 服务端预置不掉,
# 每个用户都要撞一次墙。
OPENHANDS_DOMAIN = _env("OPENHANDS_DOMAIN", "")
OPENHANDS_IMAGE_REF = _env("OPENHANDS_IMAGE_REF", "ghcr.io/openhands/openhands:latest")
# 4 核不是阔气: 起沙箱时它另跑一个 agent server 进程, 而应用只等 120 秒 ——
# 2 核上那堆 import 走不完, 页面就一直"等待沙盒"。
OPENHANDS_MEM_LIMIT_MB = _env_int("OPENHANDS_MEM_LIMIT_MB", 8192)
OPENHANDS_CPUS = _env_float("OPENHANDS_CPUS", 4.0)
OPENHANDS_TAB_GRACE_MIN = _env_int("OPENHANDS_TAB_GRACE_MIN", 10)

# AutoGen Studio —— 微软的多智能体搭建台 (2026-09-01)。上游没有官方镜像, 这是
# 我们打的 (deploy/workspace-autogen)。镜像里打了补丁把写死的模型名换成读环境
# 变量 —— 它默认写的是 gpt-4o-mini, 而网关只放行在售目录里的型号。
AUTOGEN_DOMAIN = _env("AUTOGEN_DOMAIN", "")
AUTOGEN_IMAGE_REF = _env("AUTOGEN_IMAGE_REF", "")
AUTOGEN_MEM_LIMIT_MB = _env_int("AUTOGEN_MEM_LIMIT_MB", 2048)
AUTOGEN_CPUS = _env_float("AUTOGEN_CPUS", 1.0)
AUTOGEN_TAB_GRACE_MIN = _env_int("AUTOGEN_TAB_GRACE_MIN", 10)

# LangChain —— 官方对话前端 (agent-chat-ui) + 跑在 LangGraph 上的智能体
# (2026-09-01)。两半在同一个容器里, 前面一个 node 反代把 /langgraph 分给后端 ——
# 前端是**浏览器**去连 LangGraph 的, 走同源前缀最省事 (不用开跨域, 也不用给
# LangGraph 再要一个域名和一套鉴权)。见 deploy/workspace-langchain。
LANGCHAIN_DOMAIN = _env("LANGCHAIN_DOMAIN", "")
LANGCHAIN_IMAGE_REF = _env("LANGCHAIN_IMAGE_REF", "")
LANGCHAIN_MEM_LIMIT_MB = _env_int("LANGCHAIN_MEM_LIMIT_MB", 2048)
LANGCHAIN_CPUS = _env_float("LANGCHAIN_CPUS", 1.0)
LANGCHAIN_TAB_GRACE_MIN = _env_int("LANGCHAIN_TAB_GRACE_MIN", 10)

# OpenManus 与 CrewAI —— 两个产品**共用一个镜像** (deploy/workspace-frameworks),
# 因为它们的依赖高度重叠, 分开打两份等于让 ECI 多存一份、冷启动多拉一次。
# 两个都没有自己的界面 (OpenManus 是命令行智能体, CrewAI 是 Python 库), 所以
# 这一格的界面是浏览器里的终端 (ttyd)。
FRAMEWORKS_IMAGE_REF = _env("FRAMEWORKS_IMAGE_REF", "")
OPENMANUS_DOMAIN = _env("OPENMANUS_DOMAIN", "")
CREWAI_DOMAIN = _env("CREWAI_DOMAIN", "")
FRAMEWORKS_MEM_LIMIT_MB = _env_int("FRAMEWORKS_MEM_LIMIT_MB", 2048)
FRAMEWORKS_CPUS = _env_float("FRAMEWORKS_CPUS", 1.0)
FRAMEWORKS_TAB_GRACE_MIN = _env_int("FRAMEWORKS_TAB_GRACE_MIN", 10)

# 私有镜像仓库的凭据 (2026-09-01)。空 = 不带凭据, 只能拉公开镜像。
#
# 为什么需要: ghcr 上**新建的包默认是私有的**, 而 ECI 拉不动私有镜像 —— 表现是
# 冷启动一直失败。此前每接一个自建镜像的产品, 都要人去 GitHub 网页上点一次
# "Change visibility → Public"; 而改可见性**没有 REST 接口** (试过 PATCH/PUT/POST
# 三种方法, 一律 404, 那是网页端专有的)。带凭据拉是唯一能自动化的路, 顺带我们的
# 产品镜像也不必对全世界公开。
#
# ⚠️ 这里放的应当是**只读**令牌 (read:packages)。目前用的是 144 上那把推送用的
# (write:packages) —— 能用但权限过大, 有条件应当换成只读的。
WORK_REGISTRY_SERVER = _env("WORK_REGISTRY_SERVER", "")
WORK_REGISTRY_USERNAME = _env("WORK_REGISTRY_USERNAME", "")
WORK_REGISTRY_PASSWORD = _env("WORK_REGISTRY_PASSWORD", "")


def registry_credential() -> dict[str, str]:
    """拼给阿里云 ECI 的镜像仓库凭据 (扁平参数)。没配就返回空 dict。"""
    if not (WORK_REGISTRY_SERVER and WORK_REGISTRY_USERNAME and WORK_REGISTRY_PASSWORD):
        return {}
    return {
        "ImageRegistryCredential.1.Server": WORK_REGISTRY_SERVER,
        "ImageRegistryCredential.1.UserName": WORK_REGISTRY_USERNAME,
        "ImageRegistryCredential.1.Password": WORK_REGISTRY_PASSWORD,
    }


# Dify —— 云空间的 LLM 应用搭建坑位 (多容器栈, 10 个容器)。
DIFY_DOMAIN = _env("DIFY_DOMAIN", "")
DIFY_VERSION = _env("DIFY_VERSION", "1.17.0")
DIFY_PLUGIN_DAEMON_VERSION = _env("DIFY_PLUGIN_DAEMON_VERSION", "0.6.10-local")
DIFY_SANDBOX_VERSION = _env("DIFY_SANDBOX_VERSION", "0.2.15")
DIFY_MEM_LIMIT_MB = _env_int("DIFY_MEM_LIMIT_MB", 4096)
DIFY_CPUS = _env_float("DIFY_CPUS", 2.0)
DIFY_TAB_GRACE_MIN = _env_int("DIFY_TAB_GRACE_MIN", 10)

# Coze Studio (10 容器栈)。COZE_VERSION 同时钉 server 与 web 两个镜像;
# COZE_ASSETS_IMAGE_REF 是我们自建的资产镜像 (deploy/workspace-coze), 它的
# tag 里带着同一个版本号 —— **两者必须同源**, 错位就是库 schema 与二进制对不上。
COZE_DOMAIN = _env("COZE_DOMAIN", "")
COZE_VERSION = _env("COZE_VERSION", "0.5.1")
COZE_ASSETS_IMAGE_REF = _env("COZE_ASSETS_IMAGE_REF", "")
# 这一栈里有 Elasticsearch + Milvus + MySQL, 是目前最重的产品 (实测占用 ~9GiB:
# ES 堆 1G + Milvus ~3G + MySQL ~1G + Go 服务 ~0.5G + 其余)。
# 取 4c16g 而不是更省的 4c12g: ECI 的规格要落在合法的 vCPU:内存 比例上 (1/2/4/8),
# 12/4=3 不是 —— 会被拒或悄悄抬价, 而报错里不会提比例两个字。
COZE_MEM_LIMIT_MB = _env_int("COZE_MEM_LIMIT_MB", 16384)
COZE_CPUS = _env_float("COZE_CPUS", 4.0)
# 冷启动最贵的一个 (ES 装分词器 + Milvus 起 standalone), 多留一会儿。
COZE_TAB_GRACE_MIN = _env_int("COZE_TAB_GRACE_MIN", 15)

# OpenClaw (单容器)。控制台 UI 在 18789, 自带 /healthz /readyz 探针。
OPENCLAW_DOMAIN = _env("OPENCLAW_DOMAIN", "")
OPENCLAW_IMAGE_REF = _env("OPENCLAW_IMAGE_REF", "ghcr.io/openclaw/openclaw:2026.8.1")
OPENCLAW_MEM_LIMIT_MB = _env_int("OPENCLAW_MEM_LIMIT_MB", 4096)
OPENCLAW_CPUS = _env_float("OPENCLAW_CPUS", 2.0)
OPENCLAW_TAB_GRACE_MIN = _env_int("OPENCLAW_TAB_GRACE_MIN", 10)

# Operator (单容器)。我们自己写的动手型智能体, 见 deploy/workspace-agents-team。
# 8710 上同时是前端和 API; 就绪探针打 /api/health 而不是首页 —— 首页是静态文件,
# 后端没起来它照样 200 (2026-08-30 Dify/Coze 都栽过这个)。
AGENTS_TEAM_DOMAIN = _env("AGENTS_TEAM_DOMAIN", "")
AGENTS_TEAM_IMAGE_REF = _env("AGENTS_TEAM_IMAGE_REF", "ghcr.io/agentsdancepro/agents-team:0.7.5")
# 4G 是给 **Chromium** 留的 (浏览器工具, 见 app/browser.py): Python 侧空载才
# 一百多兆, 而一个带页面的 headless Chromium 轻易吃掉 500MB+, 复杂页面更多。
# 给少了的表现是容器被 OOM 杀掉重启 —— 用户看到的是"对话突然断了", 而日志里
# 只有一行退出码, 不会说是内存。
AGENTS_TEAM_MEM_LIMIT_MB = _env_int("AGENTS_TEAM_MEM_LIMIT_MB", 4096)
AGENTS_TEAM_CPUS = _env_float("AGENTS_TEAM_CPUS", 2.0)
AGENTS_TEAM_TAB_GRACE_MIN = _env_int("AGENTS_TEAM_TAB_GRACE_MIN", 10)

# --- 编码智能体工作台 (Claude Code / Codex 共用一个镜像) ---------------------
# 见 deploy/workspace-codecli。两个产品跑同一个镜像, 区别只在启动脚本写进去的
# 终端配置与网关 env。
CODECLI_IMAGE_REF = _env("CODECLI_IMAGE_REF", "ghcr.io/agentsdancepro/codecli-local:4.135.0-r1")
CODECLI_MEM_LIMIT_MB = _env_int("CODECLI_MEM_LIMIT_MB", 4096)
CODECLI_CPUS = _env_float("CODECLI_CPUS", 2.0)
# code-server 冷启动实测 ~8 秒, 但用户在里面是**连续工作**的 (读代码、等 agent
# 跑完), 关一下标签页再回来很常见 —— 留久一点, 与 ComfyUI 同理。
CODECLI_TAB_GRACE_MIN = _env_int("CODECLI_TAB_GRACE_MIN", 10)
# CloudCLI 版的外壳 (聊天式界面, 见 deploy/workspace-cloudcli)。与 code-server
# 版跑同一批 CLI、同一套网关接线, 只是形态不同 —— 老板要两版摆一起比。
CLOUDCLI_IMAGE_REF = _env("CLOUDCLI_IMAGE_REF", "ghcr.io/agentsdancepro/cloudcli-local:1.37.2-r1")
# 自研的智能体工作台 (见 deploy/workspace-agentui)。老板 2026-08-31 拍板顶掉
# CloudCLI —— 别人的界面里挂着别人的引流入口, 而积分这个核心机制在里面没有位置。
AGENTUI_IMAGE_REF = _env("AGENTUI_IMAGE_REF", "ghcr.io/agentsdancepro/agentui:0.2.2")

# --- 数字人 (实时口型视频通话) ----------------------------------------------
# 与其它产品**不同**: 它不起每用户容器, 而是转发到我们自己的 GPU 节点 (那张 L20
# 上跑着 SoulX-FlashHead), 三路并发满了排队。所以计费也不一样 —— 其它产品收的是
# "容器存在时间"的机时额度, 这个收的是**真实通话分钟**的积分:
#   · 没有容器, 收机时讲不通;
#   · 排队等的那几分钟不该计费, 而排队恰恰是因为卡不够 —— 让用户为我们的容量
#     不足付钱是说不过去的。
AVATAR_DOMAIN = _env("AVATAR_DOMAIN", "")
AVATAR_GPU_URL = _env("AVATAR_GPU_URL", "https://gpu.agentsdance.ai/avatar")
# 与 GPU 节点共享的 HMAC 密钥 (两机的 .env 必须一致, 否则一律 bad token)。
AVATAR_TOKEN_SECRET = _env("AVATAR_TOKEN_SECRET", "")
# 一分钟通话多少积分。一路通话独占 1/3 张 L20, 成本远高于普通对话 —— 定价要
# 反映这一点, 否则每一分钟都是我们在补贴。
AVATAR_CREDITS_PER_MIN = _env_int("AVATAR_CREDITS_PER_MIN", 10)
CLAUDE_CODE_DOMAIN = _env("CLAUDE_CODE_DOMAIN", "")
CODEX_DOMAIN = _env("CODEX_DOMAIN", "")

# 反代所在主机的 IP/CIDR。OpenClaw 的 trusted-proxy 鉴权只认这个来源送来的身份头
# —— 放宽等于让任何能连到容器的人自称是任意用户, 所以**不给默认值**: 没配就
# 让这个产品不出现在目录里 (见 products.enabled), 而不是退回一个宽松值。
# 与安全组里那条入站规则是同一个地址。
WORK_PROXY_CIDR = _env("WORK_PROXY_CIDR", "")

# Hermes Agent (Nous Research)。两容器: nginx 主容器 + hermes 伴随容器。
HERMES_DOMAIN = _env("HERMES_DOMAIN", "")
HERMES_IMAGE_REF = _env("HERMES_IMAGE_REF", "nousresearch/hermes-agent:v2026.8.27")
HERMES_MEM_LIMIT_MB = _env_int("HERMES_MEM_LIMIT_MB", 4096)
HERMES_CPUS = _env_float("HERMES_CPUS", 2.0)
HERMES_TAB_GRACE_MIN = _env_int("HERMES_TAB_GRACE_MIN", 10)
# 智能体最后一次调网关之后再等这么久。长任务必须能在关掉标签页之后接着跑完,
# 所以这一条单独顶着, 与"有没有人在"无关。
WORK_AGENT_IDLE_STOP_MIN = _env_int("WORK_AGENT_IDLE_STOP_MIN", 30)
# 回收前先问一句, 等这么久没人表态才真收 (2026-09-01 老板拍板 120 秒)。
# 判据总有失灵的时候 —— 这个窗口把"判错"从"活儿当场没了"降级成"被打扰一下"。
WORK_RECLAIM_ASK_SEC = _env_int("WORK_RECLAIM_ASK_SEC", 120)

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
