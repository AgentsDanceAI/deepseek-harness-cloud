"""Cloud workspaces ("dshwork"): a per-user dsh container, usable from a phone.

Architecture:
  browser -> Caddy site work.<domain>
             forward_auth -> GET /api/work/route here (session cookie), which
               ensures the user's container is running and answers 200 with
               X-Work-Upstream: dshwork-<uid>:3081
             reverse_proxy {X-Work-Upstream} with Host/Origin rewritten to
               127.0.0.1:3080 (dsh's reachability fence trusts loopback)
  container: `dsh web` bound to in-container loopback, socat relaying :3081;
             the user's GATEWAY token is injected as DEEPSEEK_API_KEY with our
             gateway as DEEPSEEK_BASE_URL / DEEPSEEK_SEARCH_BASE_URL, so all
             model+search traffic is metered exactly like the desktop app.

Where that container runs is WORK_BACKEND (see workbackend.py):
  docker  本机引擎, 隔离网络 + 命名卷。可 stop/start；自部署使用此后端。
  eci     阿里云弹性容器实例。**没有"停止但保留"** —— 闲置回收就是
          删除, 所以 /root 与 /workspace 必须落在 NAS 上, 而"恢复"是一次完整
          冷启动。

Security model:
  - dsh executes arbitrary code -> one container per user, memory/cpu/pids
    limits, isolated network, named volumes, no host mounts.
  - The engine API is reached ONLY through a scoped socket proxy (containers/
    networks endpoints); the app container never sees the raw docker socket.
  - Running time is metered separately from model credits; idle containers are
    reclaimed after the configured inactivity window while persistent data is retained.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import config, credits, db, products, security, work_access, workbackend
from .accounts import resolve_user, try_resolve_user
from .http_limits import read_limited_body
from .products import PREVIEW_STATIC_PORT

log = logging.getLogger("dhc.work")
router = APIRouter(tags=["workspace"])

# 标签常量与容器命名都在 workbackend 里 —— 两个后端要用同一套, 否则
# "同一个工作台" 在两边不是同一个东西。
_LABEL = workbackend.LABEL
_CFG_LABEL = workbackend.CFG_LABEL
_cname = workbackend.cname

_backend: workbackend.Backend | None = None


def backend() -> workbackend.Backend:
    """选中的后端 (WORK_BACKEND)。惰性建, 于是测试可以先替换 config 再取。"""
    global _backend
    if _backend is None:
        _backend = workbackend.make_backend()
        log.info("[work] 后端 = %s", type(_backend).__name__)
    return _backend


# 工作台在哪个主机名/IP 上应答。docker 后端恒等于容器名; ECI 后端是实例的
# VPC IP —— 直到实例真的起来才知道, 所以由 inspect 回填。
_host: dict[str, str] = {}
# in-process activity + start-state tracking (single-worker semantics, like the
# rest of the gateway guards; the reaper re-seeds after a server restart)
_last_seen: dict[str, float] = {}
_starting: dict[str, float] = {}
# when each workspace was last started — the agent-idle backstop measures from
# here too, so resuming a container whose last agent call is ancient gets a full
# grace window instead of being reaped before the user can type
_started_at: dict[str, float] = {}
# 真人在场的时刻 —— 由工作台外壳在**发生了真实交互**之后上报 (见
# /api/work/active)。刻意不等同于 _last_seen: 后者是浏览器还在轮询, 一个没人
# 的标签页照样刷新它; 而"有人正在用"必须是键盘、指针、滚轮这类动作才算。
# 没有条目 = 这个客户端还没上报过 (老缓存或脚本没跑起来), 那时回落到旧口径。
_user_active: dict[str, float] = {}


def _work_url(path: str = "/", product: products.Product | None = None) -> str:
    """Public URL of the workspace host. The scheme follows PUBLIC_BASE so a
    self-hosted deployment on plain HTTP (or localhost) is not redirected to an
    https origin it does not serve.

    域名按产品走: 每个产品一个子域, 因为 ComfyUI 前端用绝对路径引资源。"""
    scheme = "http" if config.PUBLIC_BASE.startswith("http://") else "https"
    domain = product.domain if product else config.WORK_DOMAIN
    return f"{scheme}://{domain}{path}"


def _boot_fingerprint(boot: str) -> str:
    return hashlib.sha256(boot.encode()).hexdigest()[:16]


def _upstream_host(user_id: str) -> str:
    """Where :3081 answers. Falls back to the container name so the docker
    backend keeps working before anything has been inspected."""
    return _host.get(user_id) or _cname(user_id)


def _login_next(product: products.Product) -> str:
    """登录后该回到哪个工作台。

    写死 /work 的话, 从 comfy.dshcloud.online 被弹去登录的人, 登完会落进 dsh
    工作台 —— 他要的那个从没打开过, 而且没有任何提示说发生了什么。
    """
    if product.id == products.DEFAULT:
        return "/work"
    return f"/work?product_id={product.id}"


def _product_of(request: Request) -> products.Product:
    """这是哪个产品的工作台。

    先看 **?product_id=**, 再看 Host。查询参数优先不是随意选的: 启动等待页
    (/work/starting) 跑在**主站域**上, 它轮询 /api/work/status 时 Host 是
    dshcloud.online —— 只按 Host 判就会永远在查 dsh 的工作台, 而用户等的是
    ComfyUI 的, 于是进度条卡在「正在排队」不动, 且没有任何线索。

    每个产品一个域名, 因为 ComfyUI 前端用绝对路径引资源, 塞不进子路径。
    Caddy 的 forward_auth 透传原始 Host, 所以正常访问时 Host 就是用户敲的域名。
    两者都认不出来时回落到 dsh。
    """
    asked = (request.query_params.get("product_id") or "").strip()
    if asked:
        chosen = products.get(asked)
        if chosen is not None:
            return chosen
    return products.by_domain(request.headers.get("host", "")) or products.registry()[products.DEFAULT]


def _upstream(key: str, product: products.Product) -> str:
    """写进 X-Work-Upstream 的 host:port。端口按产品走 —— dsh 是 socat 转出来的
    3081, ComfyUI 直接听 8188。"""
    return f"{_upstream_host(key)}:{product.port}"


def _mint_workspace_token(user: dict, product: products.Product) -> str:
    """容器用来打网关的凭据, 生命周期与桌面设备一致 (控制台的设备列表里可见、可撤)。

    **按工作台隔离, 不按人。** 撤销条件原来只有 user_id+platform='cloud' —— 那是
    "一个人只有一个工作台"时代的写法 (容器就以用户 id 命名)。加了 ComfyUI 之后
    这个前提不成立了: 开第二个工作台会把第一个容器里的令牌撤掉, 而那个容器还在
    跑、界面照常, 只是**往网关发的每一发都 401**, 没有任何提示。
    2026-08-28 线上踩到: 12:51 起 ComfyUI, 13:03 起 dsh, ComfyUI 从此取不到在售
    清单也生成不了任何东西。
    """
    key = products.wskey(user["id"], product.id)
    device_id = security.new_id("dev_")
    epoch = int(user["session_epoch"])
    token = security.sign_token(user["id"], device_id=device_id, epoch=epoch, ttl=config.DEVICE_TOKEN_TTL)
    now = time.time()
    name = "云工作台" if product.id == products.DEFAULT else f"云工作台 · {product.name}"
    with db.tx() as conn:
        # 迁移前铸的凭据没有 workspace, 新逻辑永远撤不到它们 —— 那就成了系统再也
        # 收不回的长期凭据。归属到 **dsh**: ComfyUI 是 2026-08 才有的, 历史行
        # 几乎都是 dsh 的。归属完再走下面的按工作台撤销, 于是"给 ComfyUI 铸币"
        # 不会误伤这些历史行, 而"给 dsh 铸币"会正常顶替掉它们。
        conn.execute(
            "UPDATE devices SET workspace=? WHERE user_id=? AND platform='cloud' "
            "AND (workspace IS NULL OR workspace='')",
            (products.wskey(user["id"]), user["id"]),
        )
        # 被顶替的那份必须立刻失效 —— 但只顶替**同一个工作台**的。
        conn.execute(
            "UPDATE devices SET revoked=1 WHERE user_id=? AND platform='cloud' AND workspace=? AND revoked=0",
            (user["id"], key),
        )
        # 留最近几条备查, 其余删掉
        stale = conn.execute(
            "SELECT id FROM devices WHERE user_id=? AND platform='cloud' AND workspace=? "
            "AND revoked=1 ORDER BY created DESC",
            (user["id"], key),
        ).fetchall()
        for row in stale[2:]:
            conn.execute("DELETE FROM devices WHERE id=?", (row["id"],))
        conn.execute(
            "INSERT INTO devices (id, user_id, name, platform, workspace, token_hash, epoch, "
            "last_seen, created) VALUES (?,?,?,?,?,?,?,?,?)",
            (device_id, user["id"], name, "cloud", key, security.token_hash(token), epoch, now, now),
        )
    return token


def agent_last_active(user_id: str) -> float:
    """When this user's workspace agent last called our gateway (epoch seconds).

    The workspace device token is used for exactly one thing — the container's
    LLM and web_search calls — and `accounts.resolve_user` stamps
    `devices.last_seen` on every authenticated gateway request. So the newest
    "cloud" device's `last_seen` is a precise, restart-durable record of the
    agent doing real work, and it does NOT move while a tab merely sits open.
    Returns 0.0 when the user has never had a workspace device.
    """
    row = db.query_one(
        "SELECT MAX(last_seen) AS ts FROM devices WHERE user_id=? AND platform='cloud'", (user_id,)
    )
    return float((row["ts"] if row is not None else None) or 0.0)


async def _inspect(user_id: str) -> workbackend.WorkInfo | None:
    info = await backend().inspect(user_id)
    if info is not None and info.host:
        _host[user_id] = info.host
    return info


def _capacity_reason() -> str:
    """后端自己的容量判定。docker 看宿主内存余量; ECI 没有宿主可看, 恒为空。"""
    return backend().capacity_reason()


async def _running_workspaces() -> list[str]:
    return await backend().running_users()


async def _create(user: dict, product: products.Product) -> None:
    token = _mint_workspace_token(user, product)
    boot = products.boot_script(product.id)
    # 栈产品 env 里的密钥占位符在这里换成该用户的确定性密钥 —— 产品定义是静态
    # 数据, 而密钥按用户走; 确定性是为了实例重建后应用内会话不作废。
    sidecars = products.resolve_sidecars(product.sidecars, security.stack_secret(user["id"]), token)
    await backend().create(
        products.wskey(user["id"], product.id),
        boot=boot,
        env=products.env_for(product.id, token, security.stack_secret(user["id"])),
        boot_fp=_boot_fingerprint(boot),
        image=product.image,
        image_ref=product.image_ref,
        mem_mb=product.mem_mb,
        cpus=product.cpus,
        sidecars=sidecars,
        host_aliases=product.host_aliases,
        init_containers=products.resolve_init_containers(
            product.init_containers, security.stack_secret(user["id"]), token
        ),
        seeds=product.seeds,
        run_as_user=product.run_as_user,
    )


async def _start(user_id: str) -> None:
    await backend().start(user_id)
    _started_at[user_id] = time.time()


async def _stop(user_id: str) -> None:
    """闲置回收。docker 上是 stop (卷保留); ECI 上是删除 —— 那边没有
    "停止但保留"这个状态, 用户的东西靠 NAS 活下来。"""
    await backend().release(user_id)
    _host.pop(user_id, None)


async def _ready(key: str, product: products.Product) -> bool:
    """产品在自己的端口上应答后才算就绪。

    dsh 那道可达性围栏只信回环 Host, 所以探活要伪装成回环; 别的产品没有这道
    围栏, 带上也无妨 —— 统一发, 免得每加一个产品就多一条分支。

    判据是「应答了」而不是「回了 200」: 未初始化的 Dify 首页是 307 (跳 /install),
    要求 200 的话它永远停在 warming —— 容器全 Running、应用真在应答, 而用户对着
    进度条等到天荒地老, 服务端一个错都不报。2026-08-29 Dify 首次接入时踩到。
    5xx 才是"起来了但坏了", 那种不算就绪。
    """
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            r = await client.get(f"http://{_upstream(key, product)}/", headers={"host": "127.0.0.1:3080"})
            return r.status_code < 500
    except httpx.HTTPError:
        return False


def _boot_is_stale(info: workbackend.WorkInfo, product_id: str) -> bool:
    """True when the workspace was built from a different boot configuration.

    A workspace without a stamp predates the mechanism and is stale by
    definition.
    """
    return info.boot_fp != _boot_fingerprint(products.boot_script(product_id))


async def _image_is_stale(info: workbackend.WorkInfo, product: products.Product) -> bool:
    """True when the workspace runs an image other than what it would be born
    from now.

    The boot fingerprint does not change when only the image changes, so the
    image identity must be checked independently for existing workspaces.

    What "the image" means differs per backend — docker resolves the tag to an
    image ID (so a same-tag rebuild counts), ECI can only compare the registry
    reference. Either way an EMPTY answer means "could not resolve", and that
    is deliberately *not* stale: tearing a working workspace down over a failed
    lookup trades a cosmetic staleness for a real outage.
    """
    want = await backend().current_image_id(product.image_ref or product.image)
    return bool(want) and bool(info.image_id) and want != info.image_id


_ensure_locks: dict[str, asyncio.Lock] = {}


def _ensure_lock(uid: str) -> asyncio.Lock:
    """每个用户一把锁, 串行化"看一眼再决定建不建"这一段。

    没有它, 几乎同时到达的请求会各自看到"没有实例"然后各建一台。这不是理论:
    Caddy 的 forward_auth 会为页面上每个资源问一次 /api/work/route, 冷启动时
    _last_seen 是空的, 30 秒快路径拦不住, 于是整个扇出一起落进 _create。
    2026-08-24 05:24:15/16 就这样建出两台同名实例, 两台都 Running、各占一个
    EIP、按秒双份计费, 而用户只看得到一台 (ECI 不保证名字唯一, 见
    workbackend._find_all)。
    单进程单事件循环, asyncio.Lock 够用; 换成多 worker 时这里要改成数据库锁。
    """
    lock = _ensure_locks.get(uid)
    if lock is None:
        lock = _ensure_locks[uid] = asyncio.Lock()
    return lock


async def ensure_workspace(user: dict, product: products.Product) -> str:
    key = products.wskey(user["id"], product.id)
    async with _ensure_lock(key):
        return await _ensure_workspace(user, product)


async def _ensure_workspace(user: dict, product: products.Product) -> str:
    """Idempotent create+start; returns 'running' | 'starting'. Raises on
    hard failures (cap reached, engine down)."""
    uid = products.wskey(user["id"], product.id)
    info = await _inspect(uid)
    if info is not None:
        # Rebuild rather than restart: the settings the user would get are baked
        # into the old Cmd, and the runtime into the old image. Storage is
        # named volumes (docker) or NAS (ECI), so files and history persist
        # across the recreate.
        stale = (
            "boot config"
            if _boot_is_stale(info, product.id)
            else "image"
            if await _image_is_stale(info, product)
            else None
        )
        if stale is not None:
            log.info("workspace %s has stale %s; recreating", uid, stale)
            await backend().destroy(uid)
            _host.pop(uid, None)
            info = None
    if info is None:
        running = await _running_workspaces()
        if len(running) >= config.WORK_MAX_CONCURRENT or _capacity_reason():
            raise RuntimeError("capacity")
        await _create(user, product)
        await _start(uid)
        _starting[uid] = time.time()
        return "starting"
    if not info.running:
        # docker: 停着的容器 start 一下就回来。ECI: 没有 start 这个动作 ——
        # info 存在但没 running 说明它已经在 Pending/Scheduling, 该做的只是等,
        # backend.start() 在那边是空操作。
        running = await _running_workspaces()
        if len(running) >= config.WORK_MAX_CONCURRENT or _capacity_reason():
            raise RuntimeError("capacity")
        await _start(uid)
        _starting[uid] = time.time()
        return "starting"
    if await _ready(uid, product):
        _starting.pop(uid, None)
        return "running"
    if uid not in _starting:
        _starting[uid] = time.time()
    return "starting"


# --- port preview (see the app the agent just built) -------------------------
# The agent builds a web page and starts a server inside the container. Its
# `localhost:PORT` is the CONTAINER's loopback — unreachable from the user's
# browser. These routes publish a container port over the authenticated main
# domain: /preview/<port>/<path> proxies to dshwork-<hex>:<port>/<path>.
# dhc-server sits on dshwork-net, so it can reach the container directly.

# A static server over /workspace runs for the life of the container, so a file
# the agent wrote is viewable without asking it to start anything.
PREVIEW_PROBE_PORTS = (8080, 3000, 5173, 8000, 5000, 4173, 8888, 3001, 4200, 9000)
_PREVIEW_PORT_COOKIE = "dhc_preview_port"
# Ports we will never expose: dsh's own UI (its API drives the agent with the
# session's authority) and the socat bridge in front of it.
_PREVIEW_BLOCKED_PORTS = {3080, 3081}
# /preview/file/... 与 /preview/<port>/... 吐的是**智能体生成的字节**;
# /preview 本身是我们自己的界面, 不在此列。
_AGENT_CONTENT_RE = re.compile(r"^/preview/(file/|\d+(/|$))")


def preview_origin(path: str = "/") -> str:
    """智能体内容该从哪个源提供。未配置隔离域时就是本站。"""
    dom = (config.PREVIEW_DOMAIN or "").strip()
    if not dom:
        return path
    scheme = "http" if config.PUBLIC_BASE.startswith("http://") else "https"
    return f"{scheme}://{dom}{path}"


def is_agent_content(path: str) -> bool:
    return bool(_AGENT_CONTENT_RE.match(path))


def on_preview_host(request: Request) -> bool:
    dom = (config.PREVIEW_DOMAIN or "").strip()
    return bool(dom) and request.headers.get("host", "").split(":")[0] == dom


_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "content-encoding",
    "content-length",
}


async def _port_open(user_id: str, port: int, timeout: float = 0.6) -> bool:
    """TCP-connect probe. The docker socket proxy denies exec (by design), so
    reachability is tested the same way the proxy itself would reach it."""
    try:
        fut = asyncio.open_connection(_upstream_host(user_id), port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


_HREF_RE = re.compile(r'<a href="([^"?/][^"]*)"')
# Names that are build plumbing, not something anyone made.
_HIDDEN_NAMES = {"node_modules", "package-lock.json", "__pycache__", ".git"}


def _ws_volume_dir(user_id: str):
    """Read-only path to this user's /workspace as seen from the app machine.

    Where that is depends on the backend (docker volume vs NAS), so the backend
    owns it. Reading persistent storage keeps the artifacts view available even
    while the compute instance is stopped or deleted.
    """
    return backend().offline_workspace_dir(user_id)


def _workspace_files_offline(user_id: str, limit: int = 60) -> list[str]:
    """Top-level names straight off the volume, in the same shape as the
    directory-index parse: directories carry a trailing slash."""
    d = _ws_volume_dir(user_id)
    if d is None:
        return []
    try:
        entries = sorted(d.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return []
    names = []
    for e in entries:
        if e.name.startswith(".") or e.name in _HIDDEN_NAMES:
            continue
        names.append(e.name + "/" if e.is_dir() else e.name)
        if len(names) >= limit:
            break
    return names


async def _workspace_files(user_id: str, limit: int = 60) -> list[str]:
    """Top-level names in /workspace, read from the container's static server.

    Parsed from its directory index rather than exec'd — the docker socket proxy
    denies exec by design, and that restriction is worth keeping.
    """
    try:
        async with httpx.AsyncClient(timeout=4.0) as http:
            r = await http.get(f"http://{_upstream_host(user_id)}:{PREVIEW_STATIC_PORT}/")
        if r.status_code != 200:
            return []
    except httpx.HTTPError:
        return []
    names = []
    for raw in _HREF_RE.findall(r.text):
        name = raw.strip()
        # Apply the same filtering as the offline path so results do not depend
        # on whether the workspace is currently running.
        # 名字从目录索引来时是百分号编码的, 先解回来再比对, 否则过滤形同虚设。
        try:
            plain = unquote(name).rstrip("/")
        except Exception:  # noqa: BLE001 - 名字畸形不该让整页空掉
            plain = name.rstrip("/")
        if not name or plain.startswith(".") or plain in _HIDDEN_NAMES:
            continue
        names.append(name)
        if len(names) >= limit:
            break
    # things you can actually open in a browser first
    viewable = (".html", ".htm", ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".txt", ".md")
    names.sort(key=lambda n: (not n.lower().endswith(viewable), n.lower()))
    return names


async def _open_ports(user_id: str) -> list[int]:
    probes = [p for p in PREVIEW_PROBE_PORTS if p not in _PREVIEW_BLOCKED_PORTS]
    results = await asyncio.gather(*(_port_open(user_id, p) for p in probes))
    return [p for p, ok in zip(probes, results, strict=True) if ok]


def _inject_base(body: bytes, prefix: str) -> bytes:
    """Give proxied HTML a <base> so its relative links resolve under /preview/
    instead of the site root."""
    lowered = body[:4096].lower()
    tag = f'<base href="{prefix}">'.encode()
    for anchor in (b"<head>", b"<html>"):
        idx = lowered.find(anchor)
        if idx != -1:
            cut = idx + len(anchor)
            return body[:cut] + tag + body[cut:]
    return tag + body


async def _close_preview_upstream(upstream, client) -> None:  # noqa: ANN001
    try:
        await upstream.aclose()
    finally:
        await client.aclose()


class _PreviewHTMLTooLarge(Exception):
    pass


async def _read_preview_html(upstream, max_bytes: int) -> bytes:  # noqa: ANN001
    limit = max(0, int(max_bytes))
    declared = upstream.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _PreviewHTMLTooLarge
    chunks: list[bytes] = []
    seen = 0
    async for chunk in upstream.aiter_raw():
        seen += len(chunk)
        if seen > limit:
            raise _PreviewHTMLTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


# How a workspace file is presented: a glyph, a tile colour, and a human type
# name. Keyed by extension because that is all the container's directory index
# gives us — no stat, no mime (the docker socket proxy denies exec, and that
# restriction is worth more than richer metadata).
_ICON_PAGE = (
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01"/></svg>'
)
_ICON_DOC = (
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 3v5h5M9 13h6M9 17h4"/></svg>'
)
_ICON_IMG = (
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/>'
    '<path d="m4 18 5-4 4 3 3-2 4 3"/></svg>'
)
_ICON_DECK = (
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M12 16v4M8 20h8"/></svg>'
)
_ICON_CODE = (
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/></svg>'
)
_ICON_DIR = (
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M4 19V7.5A1.5 1.5 0 0 1 5.5 6h3.8l2 2.5h7.2A1.5 1.5 0 0 1 20 10v9z"/></svg>'
)

_KINDS = {
    ".html": ("page", _ICON_PAGE, "网页"),
    ".htm": ("page", _ICON_PAGE, "网页"),
    ".pdf": ("doc", _ICON_DOC, "PDF"),
    ".md": ("doc", _ICON_DOC, "Markdown"),
    ".txt": ("doc", _ICON_DOC, "文本"),
    ".csv": ("doc", _ICON_DOC, "表格"),
    ".docx": ("doc", _ICON_DOC, "Word"),
    ".xlsx": ("doc", _ICON_DOC, "Excel"),
    ".pptx": ("deck", _ICON_DECK, "演示文稿"),
    ".ppt": ("deck", _ICON_DECK, "演示文稿"),
    ".png": ("img", _ICON_IMG, "图片"),
    ".jpg": ("img", _ICON_IMG, "图片"),
    ".jpeg": ("img", _ICON_IMG, "图片"),
    ".gif": ("img", _ICON_IMG, "图片"),
    ".svg": ("img", _ICON_IMG, "矢量图"),
    ".webp": ("img", _ICON_IMG, "图片"),
    ".js": ("code", _ICON_CODE, "脚本"),
    ".mjs": ("code", _ICON_CODE, "脚本"),
    ".ts": ("code", _ICON_CODE, "脚本"),
    ".py": ("code", _ICON_CODE, "脚本"),
    ".json": ("code", _ICON_CODE, "JSON"),
    ".css": ("code", _ICON_CODE, "样式"),
}


def _describe(name: str, href: str) -> dict:
    """Presentation facts for one workspace entry."""
    if name.endswith("/"):
        return {
            "name": name,
            "href": href,
            "label": name.rstrip("/"),
            "kind": "dir",
            "glyph": _ICON_DIR,
            "type_label": "文件夹",
        }
    ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    kind, glyph, type_label = _KINDS.get(ext, ("file", _ICON_CODE, ext.lstrip(".").upper() or "文件"))
    # Names arrive percent-encoded from the directory index; a card reading
    # "AI-Agent-%E5%87%BA%E6%B5%B7.pptx" is not a product, it is a leak of how
    # the list is fetched. The href keeps the encoded form.
    try:
        label = unquote(name)
    except Exception:  # noqa: BLE001 - a malformed name must not blank the page
        label = name
    return {
        "name": name,
        "href": href,
        "label": label,
        "kind": kind,
        "glyph": glyph,
        "type_label": type_label,
    }


@router.get("/preview/file/{name:path}")
async def preview_offline_file(request: Request, name: str):
    """Open a workspace file without starting the container.

    Served from the volume, so it costs no machine minutes — which matters
    most for the user whose hours have run out and who just wants the thing
    they already made.

    Two guards. The path is resolved and checked to be inside the volume, so a
    crafted name cannot walk out of it. And the response is sandboxed into an
    opaque origin: this is agent-generated HTML on our own domain, and without
    it a page the agent wrote could read the session cookie of the person
    viewing it.
    """
    user = try_resolve_user(request)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next=/preview", status_code=302)
    root = _ws_volume_dir(user["id"])
    if root is None:
        return JSONResponse(status_code=404, content={"detail": "no_workspace"})
    try:
        target = (root / unquote(name)).resolve()
        target.relative_to(root.resolve())
    except (ValueError, OSError):
        return JSONResponse(status_code=400, content={"detail": "bad_path"})
    sandbox = {"X-Content-Type-Options": "nosniff"}
    if not config.PREVIEW_DOMAIN:
        # 与代理那条路同一个口径: 没有独立预览域时, 靠沙箱把文档打进不透明源。
        sandbox["Content-Security-Policy"] = "sandbox allow-scripts allow-popups allow-forms"
    if target.is_dir():
        # A trailing slash is load-bearing: without it the page's relative links
        # resolve one level too high and every asset 404s.
        if not request.url.path.endswith("/"):
            return RedirectResponse(request.url.path + "/", status_code=307)
        index = target / "index.html"
        if index.is_file():
            from fastapi.responses import FileResponse

            return FileResponse(index, headers=sandbox)
        rows = "".join(
            f'<li><a href="{quote(e.name, safe="")}{"/" if e.is_dir() else ""}">{e.name}</a></li>'
            for e in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            if not e.name.startswith(".")
        )
        return HTMLResponse(f"<meta charset=utf-8><ul>{rows}</ul>", headers=sandbox)
    if not target.is_file():
        return JSONResponse(status_code=404, content={"detail": "not_found"})
    from fastapi.responses import FileResponse

    return FileResponse(target, headers=sandbox)


@router.get("/preview")
async def preview_index(request: Request):
    """个人成品 — what the agent made, ready to open.

    Two things live here: the files in /workspace (served by the container's
    always-on static server, so nothing needs starting) and any dev server the
    agent happens to be running. Everything is behind the session — a preview
    only ever reaches the requester's own container.
    """
    user = try_resolve_user(request)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next=/preview", status_code=302)
    info = await _inspect(user["id"])
    running = bool(info) and info.running

    files, ports = [], []
    if running:
        files = [
            _describe(f, preview_origin(f"/preview/{PREVIEW_STATIC_PORT}/{f}"))
            for f in await _workspace_files(user["id"])
        ]
        ports = [p for p in await _open_ports(user["id"]) if p != PREVIEW_STATIC_PORT]
        if not files:
            # The runtime may be healthy while its static-preview endpoint is
            # temporarily unreachable. Preserve access to persisted artifacts.
            offline = _workspace_files_offline(user["id"])
            if offline:
                log.warning(
                    "[work] workspace %s preview endpoint :%d unavailable; using %d persisted files",
                    user["id"],
                    PREVIEW_STATIC_PORT,
                    len(offline),
                )
                files = [_describe(f, preview_origin("/preview/file/" + quote(f, safe="/"))) for f in offline]
    else:
        # Asleep is the normal state, not an error state: the files are still
        # on the volume, so list them from there and open them from there too.
        files = [
            _describe(f, preview_origin("/preview/file/" + quote(f, safe="/")))
            for f in _workspace_files_offline(user["id"])
        ]

    from .webpages import _render

    return _render(
        request,
        "works.html",
        "works",
        running=running,
        files=files,
        ports=ports,
        static_port=PREVIEW_STATIC_PORT,
    )


@router.api_route(
    "/preview/{port}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
async def preview_proxy(request: Request, port: int, path: str):
    user = try_resolve_user(request)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next=/preview/{port}/{path}", status_code=302)
    if not (1 <= port <= 65535) or port in _PREVIEW_BLOCKED_PORTS:
        return JSONResponse(status_code=400, content={"detail": "port_not_previewable"})
    info = await _inspect(user["id"])
    if not info or not info.running:
        return RedirectResponse(f"{site}/work", status_code=302)

    url = f"http://{_upstream_host(user['id'])}:{port}/{path}"
    fwd = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_HEADERS and k.lower() not in ("host", "cookie")
    }
    content = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        content = await read_limited_body(
            request,
            max_bytes=config.PREVIEW_BODY_MAX_BYTES,
            timeout_s=config.REQUEST_BODY_TIMEOUT_S,
        )
    client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            params=dict(request.query_params),
            headers=fwd,
            content=content,
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return HTMLResponse(
            status_code=502,
            content=(
                f"<!doctype html><meta charset=utf-8><p>端口 {port} 没有响应。"
                "确认服务已启动且监听 <code>0.0.0.0</code>（不是 127.0.0.1）。</p>"
                '<p><a href="/preview">← 查看正在监听的端口</a></p>'
            ),
        )

    payload_headers = {"content-encoding", "content-length"}
    headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_HEADERS or k.lower() in payload_headers
    }
    ctype = upstream.headers.get("content-type", "").lower()
    # Relocate upstream redirects into the preview namespace so a trailing-slash
    # redirect (the common case) doesn't bounce the user out to the site root.
    location = upstream.headers.get("location")
    if location and location.startswith("/"):
        headers["location"] = f"/preview/{port}{location}"
    # Preview services are user-controlled. They cannot set cookies or override
    # the control plane's CSP. Without an isolated preview origin, sandbox the
    # document into an opaque origin so authenticated APIs remain cross-origin.
    _agent_controlled = {"content-security-policy", "content-security-policy-report-only", "set-cookie"}
    headers = {k: v for k, v in headers.items() if k.lower() not in _agent_controlled}
    # An isolated origin preserves dev-server storage and HMR without sharing
    # the control plane's origin.
    if not config.PREVIEW_DOMAIN:
        headers["content-security-policy"] = "sandbox allow-scripts allow-popups allow-forms"
    if request.method == "HEAD":
        await _close_preview_upstream(upstream, client)
        resp = Response(content=b"", status_code=upstream.status_code, headers=headers)
    elif ctype.startswith("text/html") and not upstream.headers.get("content-encoding"):
        try:
            body = await _read_preview_html(upstream, config.PREVIEW_HTML_MAX_BYTES)
        except _PreviewHTMLTooLarge:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "preview_html_too_large",
                    "max_bytes": config.PREVIEW_HTML_MAX_BYTES,
                },
            )
        except httpx.HTTPError:
            return HTMLResponse(status_code=502, content="preview_upstream_read_failed")
        finally:
            await _close_preview_upstream(upstream, client)
        body = _inject_base(body, f"/preview/{port}/")
        headers = {k: v for k, v in headers.items() if k.lower() not in payload_headers}
        resp = Response(content=body, status_code=upstream.status_code, headers=headers)
    else:
        if ctype.startswith("text/html"):
            headers["x-dsh-preview-rewrite"] = "skipped-content-encoding"

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await _close_preview_upstream(upstream, client)

        resp = StreamingResponse(relay(), status_code=upstream.status_code, headers=headers)
    # Assets requested with an absolute path ("/style.css") land outside this
    # prefix; the cookie lets the fallback handler route them back here.
    resp.set_cookie(
        _PREVIEW_PORT_COOKIE,
        str(port),
        max_age=86400,
        httponly=True,
        samesite="lax",
        secure=config.PUBLIC_BASE.startswith("https"),
        domain=config.COOKIE_DOMAIN or None,
    )
    return resp


async def preview_fallback(request: Request):
    """Last-resort handler (registered after every real route in main.py).

    A previewed page that requests an absolute-path asset ("/assets/app.js")
    escapes the /preview/<port>/ prefix and would 404. The preview cookie names
    the port that page came from, so route it back. Without the cookie this is
    an ordinary 404.
    """
    port_raw = request.cookies.get(_PREVIEW_PORT_COOKIE, "")
    path = request.path_params.get("path", "")
    if not port_raw.isdigit() or path.startswith("preview"):
        return JSONResponse(status_code=404, content={"detail": "not_found"})
    # 预览 cookie 的域是整个站点 (COOKIE_DOMAIN=.<domain>), 所以主站也收得到它。
    # 隔离开启后必须在这里拦一道, 否则绝对路径的资源照样从**会话源**吐出来,
    # 隔离就只挡住了带 /preview/ 前缀的那一半。
    if config.PREVIEW_DOMAIN and not on_preview_host(request):
        return JSONResponse(status_code=404, content={"detail": "not_found"})
    return await preview_proxy(request, int(port_raw), path)


# --- routing (Caddy forward_auth hits this on EVERY request incl. WS) --------


def _route_headers(key: str, product: products.Product, user: dict) -> dict[str, str]:
    """forward_auth 回给 Caddy 的头。

    除了上游地址, 还带一个身份头: 有些产品 (OpenClaw) 用 trusted-proxy 模式
    鉴权 —— 它不肯在无鉴权下监听 LAN, 而这个模式正是为"边缘已经鉴过权"设计的。
    Caddy 用 copy_headers 把它写到发往容器的请求上, **覆盖**掉浏览器可能自带的
    同名头, 所以伪造不了。容器那边还额外只认 WORK_PROXY_CIDR 这个来源。

    对不需要它的产品也照发: Caddyfile 里没列进 copy_headers 的头不会往上游走,
    多发一个不会有任何影响, 而少发一个的症状是"整个产品打不开"。
    """
    return {
        "X-Work-Upstream": _upstream(key, product),
        products.PROXY_USER_HEADER: user["id"],
    }


@router.get("/api/work/route")
async def work_route(request: Request):
    if not config.WORK_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "work_disabled"})
    product = _product_of(request)
    # cookie_only: 这两条都跑在**产品域**上, 认的是浏览器, 不是产品自己带的
    # Authorization 头。见 accounts.try_resolve_user (含一条更正: 这是防御性的,
    # Dify 实际并不发那个头)。
    user = try_resolve_user(request, cookie_only=True)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next={_login_next(product)}", status_code=302)
    if credits.balance(user["id"]) <= 0:
        return RedirectResponse(f"{site}/pricing?reason=credits", status_code=302)

    if not product.image:
        return JSONResponse(status_code=404, content={"detail": "product_disabled"})
    # 计时/在场状态按**工作台**计, 额度按**用户**计 —— 两者不是一个键: 同一个人
    # 的 dsh 与 ComfyUI 各自空闲、各自回收, 但花的是同一份机时。
    key = products.wskey(user["id"], product.id)

    now = time.time()
    # Fast path first: this endpoint gates EVERY asset and WebSocket frame, so
    # the quota lookup must not run per request. A session already in flight
    # keeps its workspace to the end of the minute; the gate below catches it
    # on the next cold check, which is where a new task would land anyway.
    if now - _last_seen.get(key, 0) < 30 and key not in _starting:
        _last_seen[key] = now
        return Response(status_code=200, headers=_route_headers(key, product, user))

    # When the machine-time allowance is exhausted, route to plans rather than
    # consuming model credits.
    if work_access.blocked_reason(user["id"]):
        return RedirectResponse(f"{site}/pricing?reason=work#plans", status_code=302)

    try:
        state = await ensure_workspace(user, product)
    except RuntimeError as e:
        reason = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(
            f"{site}/work/starting?state={reason}&product_id={product.id}", status_code=302
        )
    if state != "running":
        return RedirectResponse(f"{site}/work/starting?product_id={product.id}", status_code=302)
    _last_seen[key] = now
    return Response(status_code=200, headers=_route_headers(key, product, user))


# --- PWA shell: the workspace document with mobile/PWA layers injected -------

# Versioned so a CSS fix reaches phones today rather than whenever Cloudflare's
# 24-hour cache expires. The workspace's stylesheets carry the mobile layout
# fixes, so a stale copy is exactly the bug the fix was for.
_PWA_INJECT_TMPL = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1c38">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="deepseek-harness-cloud">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/pwa/icon-180.png">
<link rel="stylesheet" href="/pwa/mobile.css?v={asset_v}">
<link rel="stylesheet" href="/pwa/workspace-chrome.css?v={asset_v}">
<script>if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js').catch(function(){{}})}}</script>
<script defer src="/pwa/workspace-chrome.js?v={asset_v}"></script>
"""


def _pwa_inject() -> str:
    """The injected head block, stamped with the current asset version."""
    from .webpages import ASSET_V

    return _PWA_INJECT_TMPL.replace("{asset_v}", ASSET_V)


@router.get("/api/work/shell")
async def work_shell(request: Request):
    """The workspace index document, fetched from the user's container and
    served with the PWA/mobile layers injected before </head>. dsh itself is
    untouched — this survives upstream updates (plain string injection)."""
    if not config.WORK_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "work_disabled"})
    product = _product_of(request)
    # cookie_only: 这两条都跑在**产品域**上, 认的是浏览器, 不是产品自己带的
    # Authorization 头。见 accounts.try_resolve_user (含一条更正: 这是防御性的,
    # Dify 实际并不发那个头)。
    user = try_resolve_user(request, cookie_only=True)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next={_login_next(product)}", status_code=302)
    if credits.balance(user["id"]) <= 0:
        return RedirectResponse(f"{site}/pricing?reason=credits", status_code=302)
    if work_access.blocked_reason(user["id"]):
        return RedirectResponse(f"{site}/pricing?reason=work#plans", status_code=302)
    if not product.image:
        return RedirectResponse(f"{site}/console", status_code=302)
    key = products.wskey(user["id"], product.id)
    try:
        state = await ensure_workspace(user, product)
    except RuntimeError as e:
        kind = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(f"{site}/work/starting?state={kind}&product_id={product.id}", status_code=302)
    if state != "running":
        return RedirectResponse(f"{site}/work/starting?product_id={product.id}", status_code=302)
    _last_seen[key] = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.get(
                f"http://{_upstream(key, product)}/", headers={"host": "127.0.0.1:3080"}
            )
    except httpx.HTTPError:
        return RedirectResponse(f"{site}/work/starting?product_id={product.id}", status_code=302)
    html = upstream.text
    if "</head>" in html:
        html = html.replace("</head>", _pwa_inject() + "</head>", 1)
    return HTMLResponse(html, headers={"cache-control": "no-store"})


_PWA_DIR = None


def _pwa_path(name: str):
    from pathlib import Path

    global _PWA_DIR
    if _PWA_DIR is None:
        _PWA_DIR = Path(__file__).resolve().parent / "static" / "pwa"
    return _PWA_DIR / name


@router.get("/manifest.webmanifest")
async def pwa_manifest():
    from fastapi.responses import FileResponse

    return FileResponse(_pwa_path("manifest.webmanifest"), media_type="application/manifest+json")


@router.get("/sw.js")
async def pwa_sw():
    from fastapi.responses import FileResponse

    return FileResponse(
        _pwa_path("sw.js"), media_type="text/javascript", headers={"cache-control": "no-cache"}
    )


@router.get("/pwa/{name}")
async def pwa_asset(name: str):
    from fastapi.responses import FileResponse

    safe = re.sub(r"[^a-zA-Z0-9._-]", "", name)
    path = _pwa_path(safe)
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "not_found"})
    media = (
        "text/css" if safe.endswith(".css") else "text/javascript" if safe.endswith(".js") else "image/png"
    )
    return FileResponse(path, media_type=media, headers={"cache-control": "public, max-age=86400"})


# --- user-facing endpoints ---------------------------------------------------


@router.get("/api/work/status")
async def work_status(request: Request):
    user = try_resolve_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "not_authenticated"})
    if not config.WORK_ENABLED:
        return {"enabled": False}
    product = _product_of(request)
    key = products.wskey(user["id"], product.id)
    info = await _inspect(key)
    state = (info.state or "unknown") if info else "none"
    ready = bool(info) and info.running and await _ready(key, product)
    # The startup page uses backend state rather than a time-based approximation.
    # 用一套与后端无关的词暴露出来, 免得页面去解析 docker/ECI 各自的状态名。
    phase = (
        "ready"
        if ready
        else "warming"
        if info and info.running  # 实例起来了, dsh 还在绑端口
        else "booting"
        if info  # 实例在调度/启动
        else "queued"
    )  # 还没有实例
    out = {
        "enabled": True,
        "phase": phase,
        "state": "running" if ready else ("starting" if info and info.running else state),
        "url": _work_url("/", product),
        "credits_per_min": config.WORK_CREDITS_PER_MIN,
        "idle_stop_min": config.WORK_IDLE_STOP_MIN,
        "balance": credits.balance(user["id"]),
        # The workspace runs on its own subdomain and renders dsh's UI, so it
        # has no server-side template to branch on — the admin entry in the
        # floating menu is gated on this flag instead.
        "is_admin": bool(user.get("is_admin")),
    }
    out.update(work_access.state(user["id"]))
    return out


@router.post("/api/work/active")
async def work_active(request: Request):
    """“有真人正在用这台工作台。”

    工作台外壳在发生真实交互 (按键、指针、滚轮) 之后最多每分钟报一次。**只有
    这一条路径**能刷新在场时刻 —— 普通的轮询和资源请求不行, 否则一个开着没人
    的标签页就能永远续租, 而机时是按容器存在时间计费的, 等于替用户烧额度。

    不 ensure_workspace: 这只是续租, 不该把一台已经回收的工作台重新拉起来。
    """
    user = resolve_user(request)
    _user_active[user["id"]] = time.time()
    return {"ok": True}


@router.post("/api/work/stop")
async def work_stop(request: Request):
    user = resolve_user(request)
    key = products.wskey(user["id"], _product_of(request).id)
    await _stop(key)
    _last_seen.pop(key, None)
    _starting.pop(key, None)
    _started_at.pop(key, None)
    _user_active.pop(key, None)
    return {"ok": True}


@router.get("/work")
async def work_entry(request: Request, product_id: str = products.DEFAULT):
    """Site entry point: kick the container and land the user on the UI.

    product_id 决定进哪个工作台。默认 dsh —— /work 这个地址的语义不变。
    """
    site = config.PUBLIC_BASE.rstrip("/")
    if not config.WORK_ENABLED:
        return RedirectResponse(f"{site}/download", status_code=302)
    product = products.get(product_id)
    if product is None or not product.image or not product.domain:
        return RedirectResponse(f"{site}/console", status_code=302)
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse(f"{site}/login?next=/work", status_code=302)
    if credits.balance(user["id"]) <= 0:
        return RedirectResponse(f"{site}/pricing?reason=credits", status_code=302)
    if work_access.blocked_reason(user["id"]):
        return RedirectResponse(f"{site}/pricing?reason=work#plans", status_code=302)
    # Carry the homepage composer's task across the redirect; workspace-chrome.js
    # types it into dsh so the first prompt is not retyped.
    task = (request.query_params.get("task") or "").strip()
    suffix = ("?task=" + quote(task[:2000], safe="")) if task else ""
    try:
        state = await ensure_workspace(user, product)
    except RuntimeError as e:
        kind = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(f"{site}/work/starting?state={kind}&product_id={product.id}", status_code=302)
    if state == "running":
        return RedirectResponse(_work_url("/" + suffix, product), status_code=302)
    joiner = "&" if suffix else "?"
    return RedirectResponse(f"{site}/work/starting{suffix}{joiner}product_id={product.id}", status_code=302)


# Keep the launch-page assets outside the HTML f-string so CSS/JS braces remain
# readable and do not require manual escaping.
_BOOT_CSS = """
/* 加载页整页用主页那块深蓝, 且**不跟随浅/深色主题** —— 它是品牌页面, 不是文档页,
   五个云空间产品共用这一张 (/work/starting), 所以它就是产品的第一印象。 */
body[data-page="work"]{background:#0b1c38;color:#eef3fb;min-height:100vh}
/* 主页同款细网格。用伪元素而不是主页那块 canvas: 这页要在几百毫秒内出现,
   不值得为背景多下一段脚本。 */
body[data-page="work"]::before{content:"";position:fixed;inset:0;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px);
  background-size:72px 72px;
  -webkit-mask-image:linear-gradient(#000 55%,transparent);
  mask-image:linear-gradient(#000 55%,transparent)}
body[data-page="work"] .auth-wrap{position:relative;z-index:1;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:24px}
body[data-page="work"] .auth-card{max-width:520px;width:100%;color:#eef3fb;
  background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.10);
  box-shadow:none;backdrop-filter:blur(6px)}
body[data-page="work"] .auth-title{color:#eef3fb}
body[data-page="work"] .muted{color:rgba(238,243,251,.66)}
body[data-page="work"] a{color:#8fb0e8}
body[data-page="work"] a:hover{color:#eef3fb}

.boot{max-width:430px;margin:0 auto;text-align:center}
.boot .track{position:relative;height:6px;margin:30px 0 14px;border-radius:999px;
  background:rgba(255,255,255,.10)}
.boot .fill{height:100%;width:0;border-radius:999px;background:#4d84e0;
  transition:width .5s cubic-bezier(.22,.61,.36,1)}
/* 光标骑在进度条头上: 它和填充用的是同一个百分比, 不会各走各的 */
.boot .caret{position:absolute;top:50%;left:0;width:0;
  transition:left .5s cubic-bezier(.22,.61,.36,1)}
.boot .caret i{position:absolute;left:-1.5px;top:-9px;width:3px;height:18px;
  border-radius:1.5px;background:#eaf0fb;
  animation:caret-blink 1.06s step-end infinite}
/* step-end: 光标是"亮/灭"两态, 不是渐隐 —— 渐隐看着像呼吸灯, 不像光标 */
@keyframes caret-blink{0%,49%{opacity:1}50%,100%{opacity:0}}
.boot .phase{margin:2px 0 0;font-size:13px;color:rgba(238,243,251,.6)}
.boot .slow{margin:12px 0 0;font-size:13px;color:#f0c674}
/* 动效敏感的人只看进度, 不看闪烁 */
@media (prefers-reduced-motion: reduce){
  .boot .caret i{animation:none}
  .boot .fill,.boot .caret{transition:none}
}
"""

_BOOT_JS = """
(function(){
var track=document.getElementById('track'),fill=document.getElementById('fill'),
    caret=document.getElementById('caret'),phaseEl=document.getElementById('phase'),
    slowEl=document.getElementById('slow');
// 每个阶段一个区间: [下界, 上界, 时间常数]。阶段跳变才是大跨步; 区间内按停留
// 时长渐近逼近上界, 永远不撞天花板 —— 慢的时候表现为"变慢"而不是"卡死",
// 也不会在还没好的时候假装做完了。
var BAND={queued:[2,12,6],booting:[12,68,9],warming:[68,94,4],ready:[100,100,1]};
var LABEL={queued:'正在排队',booting:'正在分配算力',warming:'工作台就绪中',ready:'就绪'};
var cur=0,phase='queued',since=Date.now(),t0=Date.now();
function paint(){
  var b=BAND[phase]||BAND.queued,el=(Date.now()-since)/1000,
      target=b[0]+(b[1]-b[0])*(1-Math.exp(-el/b[2]));
  if(target>cur)cur=target;              // 只前进, 不回退
  fill.style.width=cur+'%';caret.style.left=cur+'%';
  track.setAttribute('aria-valuenow',Math.round(cur));
  phaseEl.textContent=LABEL[phase]||'';
  // 比平时久就直说。一条不动的进度条只会让人以为坏了。
  if(slowEl)slowEl.hidden=(Date.now()-t0)<60000||phase==='ready';
}
setInterval(paint,120);paint();
(async function poll(){
  try{
    var s=await (await fetch('/api/work/status'+location.search)).json();
    if(s.phase&&s.phase!==phase){phase=s.phase;since=Date.now();}
    if(s.state==='running'){
      phase='ready';cur=100;paint();
      var t=new URLSearchParams(location.search).get('task');
      location.href=s.url+(t?'?task='+encodeURIComponent(t):'');return;
    }
  }catch(e){}
  setTimeout(poll,1500);
})();
})();
"""


def _boot_wait_hint() -> str:
    """等多久, 按后端说实话。

    docker 使用 stop/start，卷和镜像保留在本机；ECI 每次创建新实例，通常需要
    更长的等待时间。用户界面的提示应按后端区分。
    """
    return "20–40 秒" if not backend().resumable else "5–20 秒"


@router.get("/work/starting")
async def work_starting(request: Request, state: str = ""):
    """启动等待页。

    ECI 上“恢复”是一次完整冷启动，因此等待页显示后端报告的真实阶段：
    (/api/work/status 的 phase), 页面按阶段推进, 阶段内渐近而不撞满格。
    """
    if state == "busy":
        title, body, poll = "云工作台当前繁忙", "在线名额已满，请稍后再试或使用桌面版。", False
    elif state == "error":
        title, body, poll = "启动失败", "云工作台启动失败，请稍后重试；问题持续请联系支持。", False
    else:
        title, body, poll = (
            "云工作台启动中…",
            f"正在为你准备云端工作区，通常需要 {_boot_wait_hint()}。",
            True,
        )

    if poll:
        progress = (
            '<div class="track" id="track" role="progressbar" aria-valuemin="0" '
            'aria-valuemax="100" aria-valuenow="0" aria-label="启动进度">'
            '<div class="fill" id="fill"></div>'
            '<div class="caret" id="caret"><i></i></div>'
            "</div>"
            '<p class="phase" id="phase">正在排队</p>'
            '<p class="slow" id="slow" hidden>比平时久一些，仍在继续。</p>'
        )
        tail = f"<style>{_BOOT_CSS}</style><script>{_BOOT_JS}</script>"
    else:
        progress, tail = "", ""

    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/static/app.css">
</head><body data-page="work">
<section class="auth-wrap"><div class="card auth-card center boot">
<h1 class="auth-title">{title}</h1>
<p class="muted">{body}</p>
{progress}
<p class="muted small" style="margin-top:18px"><a href="/console">返回控制台</a></p>
</div></section>{tail}</body></html>"""
    return HTMLResponse(html)


# --- billing + idle reaper (one asyncio task, started from main.py) ----------


async def reaper_tick(now: float) -> None:
    """One meter/reaper pass over every running workspace."""
    for uid in await _running_workspaces():
        # 按**容器存在的时间**计量, 不是按智能体干活的时间。
        #
        # Meter the full lifetime of a running container rather than only the
        # minutes in which the agent calls the gateway. This matches the resource
        # actually reserved and prevents sparse activity from bypassing metering.
        #
        # 机时依然只扣 MINUTES, 永不扣积分: 套餐里机时是单独的额度 (GitHub
        # Actions 那种口径), 积分留给 token。这里仍写一行 usage_log (work_access
        # 数的就是它), 但 credits=0, 所以一分钟机时绝不会动到 token 余额。
        # uid 是**工作台键**, 不是用户 id —— 多产品之后二者不再相同。机时记在人
        # 头上 (同一个人的 dsh 与 ComfyUI 花的是同一份额度), 而回收计时按工作台。
        owner, product_id = products.split_key(uid)
        credits.spend(
            owner,
            0,
            kind=work_access.MINUTE_KIND,
            model=f"work:{product_id}",
            request_id=f"ws-{product_id}-{int(now // 60)}",
        )
        work_access.consume_minute(owner)
        last = _last_seen.setdefault(uid, now)  # re-seed after restart
        started = _started_at.setdefault(uid, now)  # re-seed after restart
        # 口径: **打开一次, 持续做事, 只关一次。**
        #
        # 回收只在"没人在 **且** 没活儿在跑"时发生 —— 两个条件都要成立:
        #
        #   away  没人在。在场只认真实交互 (/api/work/active), 不认浏览器轮询:
        #         轮询在没人的标签页上照样发生, 拿它当在场, 一个忘了关的页面
        #         就能整夜续租, 而机时按容器存在时间计费 —— 那是在替用户烧额度。
        #   quiet 没活儿在跑。智能体的长任务必须能在关掉标签页之后继续 (它可能
        #         正跑一小时的活), 所以网关调用单独顶着一个窗口。
        #
        # 关掉标签页之后要收得快, 但不能靠 pagehide/sendBeacon —— iOS 切个应用
        # 也发, 硬杀则一条都不发。轮询停了本身就是可靠信号: 页面没了, 就没有
        # 请求经过 forward_auth。所以标签页在时给足 WORK_IDLE_STOP_MIN, 一旦
        # 连轮询都停了, 在场窗口缩到 WORK_TAB_GONE_MIN, 省下的是用户的机时。
        #
        # 没上报过在场的客户端 (老缓存、脚本没跑起来) 回落到 started, 于是行为
        # 与加这条之前完全一致 —— 由 quiet 单独决定, 绝不会因为"没收到心跳"
        # 而把正在用的人踢掉。
        product = products.get(product_id)
        # 宽限期按产品走: ComfyUI 冷启动 ~26 秒, 多留几分钟机时比让人重等一遍划算;
        # dsh 没这个包袱, 用全局值。
        grace_min = (product.tab_grace_min if product else 0) or config.WORK_TAB_GONE_MIN
        tab_gone = now - last > grace_min * 60
        idle_min = grace_min if tab_gone else config.WORK_IDLE_STOP_MIN
        # 上面两个信号都是 **dsh 专有** 的:
        #   _user_active   由 dsh 前端调 /api/work/active 上报
        #   agent_last_active 数的是智能体经本站网关发起的调用
        # ComfyUI 两样都没有 —— 它不认识 /api/work/active, 也不经网关跑模型
        # (自有节点直连 /llm/v1, 不算 agent 活动)。于是 present 退化成"容器启动
        # 时间"、quiet 恒为真, **容器起来 WORK_IDLE_STOP_MIN 分钟后必被回收,
        # 不管人在不在用**。2026-08-27 实测: 老板正在 ComfyUI 里操作, 101 秒后
        # 工作台被杀, 日志只写一句 (idle)。
        #
        # 所以对没有上报器的产品, 用**请求流量**当在场信号 —— Caddy 的
        # forward_auth 会为页面上每个资源、每个 WebSocket 帧打一次
        # /api/work/route, 页面还开着就一定有流量。代价是"忘了关的标签页会续租",
        # 但那正是 tab_gone 那套机制在管的事: 标签页真关了流量就断, 3 分钟后收掉。
        reports_presence = product.reports_presence if product else True
        present = max(_user_active.get(uid, 0.0), started)
        agent = agent_last_active(uid)
        if not reports_presence:
            present = max(present, last)
            agent = max(agent, last)
        away = now - present > idle_min * 60
        quiet = now - max(agent, started) > config.WORK_AGENT_IDLE_STOP_MIN * 60
        # 余额要按**人**查。uid 是工作台键 (u_xxx~comfyui), 拿它查 credits 永远
        # 得 0 —— 于是欠费用户的 ComfyUI 工作台永远不会因欠费被回收。
        broke = credits.balance(owner) <= -config.OVERDRAFT_LIMIT_CREDITS
        if (away and quiet) or broke:
            reason = "credits exhausted" if broke else ("tab closed" if tab_gone else "idle")
            log.info("stopping workspace %s (%s)", uid, reason)
            await _stop(uid)
            _last_seen.pop(uid, None)
            _started_at.pop(uid, None)
            _user_active.pop(uid, None)


async def billing_reaper_loop() -> None:
    log.info(
        "workspace billing/reaper loop started (%s credits per ACTIVE min, "
        "idle-stop %s min, agent-idle-stop %s min)",
        config.WORK_CREDITS_PER_MIN,
        config.WORK_IDLE_STOP_MIN,
        config.WORK_AGENT_IDLE_STOP_MIN,
    )
    while True:
        try:
            await asyncio.sleep(60)
            await reaper_tick(time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("workspace loop iteration failed")  # never die
