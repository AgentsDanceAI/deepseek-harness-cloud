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

from . import config, credits, db, model_catalog, security, work_access, workbackend
from .accounts import resolve_user, try_resolve_user
from .http_limits import read_limited_body

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


def _work_url(path: str = "/") -> str:
    """Public URL of the workspace host. The scheme follows PUBLIC_BASE so a
    self-hosted deployment on plain HTTP (or localhost) is not redirected to an
    https origin it does not serve."""
    scheme = "http" if config.PUBLIC_BASE.startswith("http://") else "https"
    return f"{scheme}://{config.WORK_DOMAIN}{path}"


def _boot_fingerprint(boot: str) -> str:
    return hashlib.sha256(boot.encode()).hexdigest()[:16]


def _upstream_host(user_id: str) -> str:
    """Where :3081 answers. Falls back to the container name so the docker
    backend keeps working before anything has been inspected."""
    return _host.get(user_id) or _cname(user_id)


def _upstream(user_id: str) -> str:
    return f"{_upstream_host(user_id)}:3081"


def _mint_workspace_token(user: dict) -> str:
    """Device token for the container's gateway auth — same lifecycle as a
    desktop device: visible in the console's device list, revocable there.

    There is ONE workspace per user (the container is named after the user id),
    so each rebuild replaces the credential rather than adding another: the old
    row is revoked and pruned. Otherwise every recreate left a dead "云工作台"
    entry behind and the device list filled with历史 noise.
    """
    device_id = security.new_id("dev_")
    epoch = int(user["session_epoch"])
    token = security.sign_token(user["id"], device_id=device_id, epoch=epoch, ttl=config.DEVICE_TOKEN_TTL)
    now = time.time()
    with db.tx() as conn:
        # the superseded credential must stop working the moment a new one exists
        conn.execute(
            "UPDATE devices SET revoked=1 WHERE user_id=? AND platform='cloud' AND revoked=0", (user["id"],)
        )
        # keep the last few for the audit trail; drop the rest
        stale = conn.execute(
            "SELECT id FROM devices WHERE user_id=? AND platform='cloud' AND revoked=1 ORDER BY created DESC",
            (user["id"],),
        ).fetchall()
        for row in stale[2:]:
            conn.execute("DELETE FROM devices WHERE id=?", (row["id"],))
        conn.execute(
            "INSERT INTO devices (id, user_id, name, platform, token_hash, epoch, last_seen, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (device_id, user["id"], "云工作台", "cloud", security.token_hash(token), epoch, now, now),
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


def _boot_script() -> str:
    """The container's entrypoint script.

    Deliberately free of per-user values (the credential arrives through env),
    so its digest identifies the CONFIGURATION rather than the user — that is
    what lets a running container be recognised as out of date.
    """
    gateway = config.PUBLIC_BASE.rstrip("/")
    # Chat goes through dsh's pi-ai adapter (openai-completions protocol), NOT
    # the llm-deepseek adapter: our upstream speaks standard OpenAI streaming,
    # and llm-deepseek's DeepSeek-flavored tool-call parsing assembles empty
    # tool names from it (every tool call died with UNKNOWN_TOOL — the exact
    # combination proven to work is pi-ai + openai-completions against this
    # upstream). web_search stays on the deepseek search row via env.
    # The model list is the catalog, not a hand-kept copy of it: the picker in
    # the workspace and the price table on /pricing are then the same rows by
    # construction, so a model can never be sellable but unpickable (or worse,
    # pickable but unpriced — which bills at the most expensive entry).
    model_rows = "".join(
        f"        - id: {m['id']}\n          name: {m.get('display_name', m['id'])}\n"
        for m in model_catalog.catalog().values()
    )
    settings_yaml = (
        # dsh registers its own DeepSeek provider, which showed up in the model
        # picker as a second "DeepSeek" group offering V4-Flash/V4-Pro. Those
        # entries are incompatible with the gateway's tool-call deltas (see
        # above), and their default endpoint is outside this service. An explicit
        # empty catalog removes them from
        # the picker; baseURL keeps anything that still resolves on-platform.
        "llm-deepseek:\n"
        f"  baseURL: {gateway}/llm/v1\n"
        "  models: []\n"
        "llm-pi-ai:\n"
        "  providers:\n"
        "    dshcloud:\n"
        "      displayName: DSH Cloud\n"
        "      apiKeyEnv: DSH_CLOUD_TOKEN\n"
        "      api: openai-completions\n"
        f"      baseURL: {gateway}/llm/v1\n"
        "      models:\n" + model_rows + "agent-default-model:\n"
        "  provider: dshcloud\n"
        f"  model: {model_catalog.default_model()}\n"
    )
    # dsh loads $DSH_HOME/AGENTS.md as user-global instructions for every
    # session. Without this the agent tells people to open http://localhost:PORT
    # — which is the CONTAINER's loopback and unreachable from their browser.
    agents_md = (
        "# DSH Cloud 云工作台\n\n"
        "你运行在一个云端容器里，用户通过浏览器访问你。用户的电脑和这个容器"
        "**不是同一台机器**。\n\n"
        "## 让用户能打开你做的网页 / 服务\n\n"
        "- **绝不要**让用户访问 `http://localhost:<端口>` 或 `127.0.0.1` —— 那是本容器的"
        "回环地址，用户的浏览器打不开。\n"
        f"- 本容器的端口可以通过这个公网地址预览：`{gateway}/preview/<端口>/`\n"
        f"  例如你在 8080 起了服务，就告诉用户打开 `{gateway}/preview/8080/`\n"
        "- **服务必须监听 `0.0.0.0`**，只听 127.0.0.1 的服务无法被预览代理到。\n"
        "  - `python3 -m http.server 8080 --bind 0.0.0.0`\n"
        "  - vite: `--host 0.0.0.0`；next: `-H 0.0.0.0`\n"
        "- 页面里引用资源请用**相对路径**（`./game.js`），预览代理对相对路径最稳。\n"
        "- 纯静态单文件（如一个 index.html）也需要起个 http 服务再给预览地址，"
        "不要只把文件路径告诉用户。\n"
    )
    # Boot runs on every container start, so both files self-heal if the agent
    # or the user deletes them. AGENTS.md is merged rather than overwritten: our
    # platform facts live between markers and anything the user wrote around
    # them (their own global preferences) survives the restart.
    merge_agents_md = (
        "node -e '"
        'const fs=require("fs"),p="/root/.dsh/AGENTS.md";'
        'const B="<!-- dshcloud:begin -->",E="<!-- dshcloud:end -->";'
        'const block=B+"\\n"+fs.readFileSync("/root/.dsh/.dshcloud-agents.md","utf8").trim()+"\\n"+E;'
        'let cur="";try{cur=fs.readFileSync(p,"utf8")}catch(e){}'
        'const re=new RegExp(B+"[\\\\s\\\\S]*?"+E);'
        'fs.writeFileSync(p,re.test(cur)?cur.replace(re,block):(cur.trim()?block+"\\n\\n"+cur.trim()+"\\n":block+"\\n"));'
        "'"
    )
    boot = (
        "mkdir -p /root/.dsh && cat > /root/.dsh/settings.yaml <<'DHCEOF'\n" + settings_yaml + "DHCEOF\n"
        "cat > /root/.dsh/.dshcloud-agents.md <<'DHCMDEOF'\n"
        + agents_md
        + "DHCMDEOF\n"
        + merge_agents_md
        + "\n"
        # A always-on static server over /workspace. Without it, seeing a file
        # the agent just wrote meant asking the agent to start a server — and
        # that server dies with the container, so the link rots. This one is
        # part of the workspace itself, so "open what it made" always works.
        f"python3 -m http.server {PREVIEW_STATIC_PORT} --bind 0.0.0.0 "
        "--directory /workspace >/dev/null 2>&1 & "
        "socat TCP-LISTEN:3081,fork,reuseaddr TCP:127.0.0.1:3080 & "
        "exec dsh web --host 127.0.0.1 --port 3080"
    )
    return boot


async def _create(user: dict) -> None:
    token = _mint_workspace_token(user)
    gateway = config.PUBLIC_BASE.rstrip("/")
    boot = _boot_script()
    env = {
        "DSH_CLOUD_TOKEN": token,
        "DEEPSEEK_API_KEY": token,
        "DEEPSEEK_BASE_URL": f"{gateway}/llm/v1",
        "DEEPSEEK_SEARCH_BASE_URL": f"{gateway}/llm/anthropic/v1",
        "DSH_TELEMETRY_DISABLED": "1",
        # The per-user container is the sandbox boundary: resource limits,
        # isolated networking, no docker socket, and no privileged mode. dsh
        # currently couples its sandbox policy and interactive approval policy
        # to this environment variable. The web client cannot answer approval
        # prompts, so tools run without a second in-container sandbox. Revisit
        # this setting when those policies can be configured independently.
        "DSH_PERMISSION_MODE": "danger-full-access",
    }
    await backend().create(user["id"], boot=boot, env=env, boot_fp=_boot_fingerprint(boot))


async def _start(user_id: str) -> None:
    await backend().start(user_id)
    _started_at[user_id] = time.time()


async def _stop(user_id: str) -> None:
    """闲置回收。docker 上是 stop (卷保留); ECI 上是删除 —— 那边没有
    "停止但保留"这个状态, 用户的东西靠 NAS 活下来。"""
    await backend().release(user_id)
    _host.pop(user_id, None)


async def _ready(user_id: str) -> bool:
    """dsh answers on :3081 once booted; the fence trusts a loopback Host."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://{_upstream(user_id)}/", headers={"host": "127.0.0.1:3080"})
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def _boot_is_stale(info: workbackend.WorkInfo) -> bool:
    """True when the workspace was built from a different boot configuration.

    A workspace without a stamp predates the mechanism and is stale by
    definition.
    """
    return info.boot_fp != _boot_fingerprint(_boot_script())


async def _image_is_stale(info: workbackend.WorkInfo) -> bool:
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
    want = await backend().current_image_id()
    return bool(want) and bool(info.image_id) and want != info.image_id


async def ensure_workspace(user: dict) -> str:
    """Idempotent create+start; returns 'running' | 'starting'. Raises on
    hard failures (cap reached, engine down)."""
    uid = user["id"]
    info = await _inspect(uid)
    if info is not None:
        # Rebuild rather than restart: the settings the user would get are baked
        # into the old Cmd, and the runtime into the old image. Storage is
        # named volumes (docker) or NAS (ECI), so files and history persist
        # across the recreate.
        stale = "boot config" if _boot_is_stale(info) else "image" if await _image_is_stale(info) else None
        if stale is not None:
            log.info("workspace %s has stale %s; recreating", uid, stale)
            await backend().destroy(uid)
            _host.pop(uid, None)
            info = None
    if info is None:
        running = await _running_workspaces()
        if len(running) >= config.WORK_MAX_CONCURRENT or _capacity_reason():
            raise RuntimeError("capacity")
        await _create(user)
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
    if await _ready(uid):
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
PREVIEW_STATIC_PORT = 8088
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


@router.get("/api/work/route")
async def work_route(request: Request):
    if not config.WORK_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "work_disabled"})
    user = try_resolve_user(request)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next=/work", status_code=302)
    if credits.balance(user["id"]) <= 0:
        return RedirectResponse(f"{site}/pricing?reason=credits", status_code=302)

    now = time.time()
    # Fast path first: this endpoint gates EVERY asset and WebSocket frame, so
    # the quota lookup must not run per request. A session already in flight
    # keeps its workspace to the end of the minute; the gate below catches it
    # on the next cold check, which is where a new task would land anyway.
    if now - _last_seen.get(user["id"], 0) < 30 and user["id"] not in _starting:
        _last_seen[user["id"]] = now
        return Response(status_code=200, headers={"X-Work-Upstream": _upstream(user["id"])})

    # When the machine-time allowance is exhausted, route to plans rather than
    # consuming model credits.
    if work_access.blocked_reason(user["id"]):
        return RedirectResponse(f"{site}/pricing?reason=work#plans", status_code=302)

    try:
        state = await ensure_workspace(user)
    except RuntimeError as e:
        reason = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(f"{site}/work/starting?state={reason}", status_code=302)
    if state != "running":
        return RedirectResponse(f"{site}/work/starting", status_code=302)
    _last_seen[user["id"]] = now
    return Response(status_code=200, headers={"X-Work-Upstream": _upstream(user["id"])})


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
    user = try_resolve_user(request)
    site = config.PUBLIC_BASE.rstrip("/")
    if user is None:
        return RedirectResponse(f"{site}/login?next=/work", status_code=302)
    if credits.balance(user["id"]) <= 0:
        return RedirectResponse(f"{site}/pricing?reason=credits", status_code=302)
    if work_access.blocked_reason(user["id"]):
        return RedirectResponse(f"{site}/pricing?reason=work#plans", status_code=302)
    try:
        state = await ensure_workspace(user)
    except RuntimeError as e:
        kind = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(f"{site}/work/starting?state={kind}", status_code=302)
    if state != "running":
        return RedirectResponse(f"{site}/work/starting", status_code=302)
    _last_seen[user["id"]] = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.get(
                f"http://{_upstream(user['id'])}/", headers={"host": "127.0.0.1:3080"}
            )
    except httpx.HTTPError:
        return RedirectResponse(f"{site}/work/starting", status_code=302)
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
    info = await _inspect(user["id"])
    state = (info.state or "unknown") if info else "none"
    ready = bool(info) and info.running and await _ready(user["id"])
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
        "url": _work_url("/"),
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


@router.post("/api/work/stop")
async def work_stop(request: Request):
    user = resolve_user(request)
    await _stop(user["id"])
    _last_seen.pop(user["id"], None)
    _starting.pop(user["id"], None)
    _started_at.pop(user["id"], None)
    return {"ok": True}


@router.get("/work")
async def work_entry(request: Request):
    """Site entry point: kick the container and land the user on the UI."""
    site = config.PUBLIC_BASE.rstrip("/")
    if not config.WORK_ENABLED:
        return RedirectResponse(f"{site}/download", status_code=302)
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
        state = await ensure_workspace(user)
    except RuntimeError as e:
        kind = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(f"{site}/work/starting?state={kind}", status_code=302)
    if state == "running":
        return RedirectResponse(_work_url("/" + suffix), status_code=302)
    return RedirectResponse(f"{site}/work/starting{suffix}", status_code=302)


# Keep the launch-page assets outside the HTML f-string so CSS/JS braces remain
# readable and do not require manual escaping.
_BOOT_CSS = """
.boot{max-width:430px;margin:0 auto;text-align:center}
.boot .track{position:relative;height:8px;margin:30px 0 12px;border-radius:999px;
  background:var(--brand-weak)}
.boot .fill{height:100%;width:0;border-radius:999px;background:var(--brand);
  transition:width .5s cubic-bezier(.22,.61,.36,1)}
/* 鲸鱼骑在进度条头上: 它和填充用的是同一个百分比, 不会各走各的 */
.boot .swimmer{position:absolute;top:50%;left:0;width:0;
  transition:left .5s cubic-bezier(.22,.61,.36,1)}
.boot .whale{position:absolute;left:-19px;top:-14px;width:38px;height:26px;
  color:var(--brand);animation:bob 2.6s ease-in-out infinite}
/* Anchor the tail animation to its own bounding box. `fill-box` prevents viewBox
   changes from moving the transform origin. */
.boot .whale .fluke{transform-box:fill-box;transform-origin:0% 50%;
  animation:flick 1.15s ease-in-out infinite}
@keyframes bob{0%,100%{transform:translateY(0) rotate(-2deg)}
  50%{transform:translateY(-3px) rotate(2deg)}}
@keyframes flick{0%,100%{transform:rotate(-10deg)}50%{transform:rotate(10deg)}}
.boot .phase{margin:2px 0 0;font-size:13px;color:var(--muted)}
.boot .slow{margin:12px 0 0;font-size:13px;color:var(--warn)}
/* 动效敏感的人只看进度, 不看游动 */
@media (prefers-reduced-motion: reduce){
  .boot .whale,.boot .whale .fluke{animation:none}
  .boot .fill,.boot .swimmer{transition:none}
}
"""

# 自己画的鲸鱼, 不是 DeepSeek 的商标。页脚已经声明"与其无背书关系", 把对方的
# 标识摆进自家加载动画会正好抵消那句声明。
_BOOT_WHALE = """
<svg class="whale" viewBox="0 0 38 26" fill="none" aria-hidden="true">
<g transform="translate(38,0) scale(-1,1)">
<path class="fluke" d="M27 13c3-3 6-5 8-5 .8 0 1.1.7.7 1.4L34.2 13l1.5 3.6c.4.7.1 1.4-.7 1.4-2 0-5-2-8-5z" fill="currentColor" opacity=".72"/>
<path d="M5 14.4C5 9.7 10.4 6.4 17 6.4c6.3 0 11 3.2 11 7.3 0 4-4.7 6.8-11 6.8-6.6 0-12-2.5-12-6.1z" fill="currentColor"/>
<circle cx="11.4" cy="12.3" r="1.35" fill="var(--card)"/>
<path d="M14 6.1c.4-1.6 1.7-2.6 3.1-2.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" opacity=".5"/>
</g>
</svg>
"""

_BOOT_JS = """
(function(){
var track=document.getElementById('track'),fill=document.getElementById('fill'),
    swim=document.getElementById('swim'),phaseEl=document.getElementById('phase'),
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
  fill.style.width=cur+'%';swim.style.left=cur+'%';
  track.setAttribute('aria-valuenow',Math.round(cur));
  phaseEl.textContent=LABEL[phase]||'';
  // 比平时久就直说。一条不动的进度条只会让人以为坏了。
  if(slowEl)slowEl.hidden=(Date.now()-t0)<60000||phase==='ready';
}
setInterval(paint,120);paint();
(async function poll(){
  try{
    var s=await (await fetch('/api/work/status')).json();
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
            f'<div class="swimmer" id="swim">{_BOOT_WHALE}</div>'
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
        credits.spend(
            uid, 0, kind=work_access.MINUTE_KIND, model="dshwork", request_id=f"ws-{int(now // 60)}"
        )
        work_access.consume_minute(uid)
        last = _last_seen.setdefault(uid, now)  # re-seed after restart
        # Two stop rules, because idle minutes are now free and RAM is not:
        #   - the user left (no browser traffic) — the original rule;
        #   - the tab was abandoned open with the agent doing nothing for a
        #     longer window (capacity backstop). Volumes persist, so a stopped
        #     workspace resumes in seconds on the next message.
        # The backstop measures from the later of "agent worked" and "container
        # started": resuming a workspace whose last agent call is older than the
        # backstop must not be reaped before the user can type into it.
        started = _started_at.setdefault(uid, now)  # re-seed after restart
        gone = now - last > config.WORK_IDLE_STOP_MIN * 60
        agent_gone = now - max(agent_last_active(uid), started) > config.WORK_AGENT_IDLE_STOP_MIN * 60
        broke = credits.balance(uid) <= -config.OVERDRAFT_LIMIT_CREDITS
        if gone or agent_gone or broke:
            reason = "user idle" if gone else "agent idle" if agent_gone else "credits exhausted"
            log.info("stopping workspace %s (%s)", uid, reason)
            await _stop(uid)
            _last_seen.pop(uid, None)
            _started_at.pop(uid, None)


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
