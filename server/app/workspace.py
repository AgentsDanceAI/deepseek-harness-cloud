"""Cloud workspaces ("dshwork"): a per-user dsh container, usable from a phone.

Architecture (all pieces already proven in production separately):
  browser -> Caddy site work.<domain>
             forward_auth -> GET /api/work/route here (session cookie), which
               ensures the user's container is running and answers 200 with
               X-Work-Upstream: dshwork-<uid>:3081
             reverse_proxy {X-Work-Upstream} with Host/Origin rewritten to
               127.0.0.1:3080 (dsh's reachability fence trusts loopback)
  container: image dsh-local:rc6, `dsh web` bound to in-container loopback,
             socat relaying :3081 on an isolated docker network; the user's
             GATEWAY token is injected as DEEPSEEK_API_KEY with our gateway as
             DEEPSEEK_BASE_URL / DEEPSEEK_SEARCH_BASE_URL, so all model+search
             traffic is metered exactly like the desktop app.

Security model:
  - dsh executes arbitrary code -> one container per user, memory/cpu/pids
    limits, isolated network, named volumes, no host mounts.
  - The engine API is reached ONLY through a scoped socket proxy (containers/
    networks endpoints); the app container never sees the raw docker socket.
  - Billing: WORK_CREDITS_PER_MIN per running minute; idle containers are
    stopped after WORK_IDLE_STOP_MIN minutes without traffic (volumes persist,
    next visit restarts within seconds).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config, credits, db, security
from .accounts import resolve_user, try_resolve_user

log = logging.getLogger("dhc.work")
router = APIRouter(tags=["workspace"])

_LABEL = "dshwork.user"
# in-process activity + start-state tracking (single-worker semantics, like the
# rest of the gateway guards; the reaper re-seeds after a server restart)
_last_seen: dict[str, float] = {}
_starting: dict[str, float] = {}


def _cname(user_id: str) -> str:
    # container names double as docker-DNS hostnames for Caddy's dynamic
    # upstream; strip the "u_" prefix so the name stays hostname-safe
    return "dshwork-" + re.sub(r"[^a-zA-Z0-9]", "", user_id)


def _upstream(user_id: str) -> str:
    return f"{_cname(user_id)}:3081"


async def _docker(method: str, path: str, *, json_body: dict | None = None,
                  params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=config.DOCKER_PROXY_URL, timeout=30.0) as client:
        return await client.request(method, path, json=json_body, params=params)


def _mint_workspace_token(user: dict) -> str:
    """Device token for the container's gateway auth — same lifecycle as a
    desktop device: visible in the console's device list, revocable there."""
    device_id = security.new_id("dev_")
    epoch = int(user["session_epoch"])
    token = security.sign_token(user["id"], device_id=device_id, epoch=epoch,
                                ttl=config.DEVICE_TOKEN_TTL)
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO devices (id, user_id, name, platform, token_hash, epoch, last_seen, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (device_id, user["id"], "云工作台", "cloud", security.token_hash(token), epoch, now, now))
    return token


async def _inspect(user_id: str) -> dict | None:
    r = await _docker("GET", f"/containers/{_cname(user_id)}/json")
    return r.json() if r.status_code == 200 else None


async def _running_workspaces() -> list[dict]:
    r = await _docker("GET", "/containers/json",
                      params={"filters": '{"label":["%s"]}' % _LABEL})
    return r.json() if r.status_code == 200 else []


async def _create(user: dict) -> None:
    gateway = config.PUBLIC_BASE.rstrip("/")
    token = _mint_workspace_token(user)
    hexid = _cname(user["id"])[len("dshwork-"):]
    # Chat goes through dsh's pi-ai adapter (openai-completions protocol), NOT
    # the llm-deepseek adapter: our upstream speaks standard OpenAI streaming,
    # and llm-deepseek's DeepSeek-flavored tool-call parsing assembles empty
    # tool names from it (every tool call died with UNKNOWN_TOOL — the exact
    # combination proven to work is pi-ai + openai-completions against this
    # upstream). web_search stays on the deepseek search row via env.
    settings_yaml = (
        "llm-pi-ai:\n"
        "  providers:\n"
        "    dshcloud:\n"
        "      displayName: DSH Cloud\n"
        "      apiKeyEnv: DSH_CLOUD_TOKEN\n"
        "      api: openai-completions\n"
        f"      baseURL: {gateway}/llm/v1\n"
        "      models:\n"
        "        - id: deepseek-v4-flash\n"
        "        - id: deepseek-v4-pro\n"
        "agent-default-model:\n"
        "  provider: dshcloud\n"
        "  model: deepseek-v4-flash\n"
    )
    boot = (
        "mkdir -p /root/.dsh && cat > /root/.dsh/settings.yaml <<'DHCEOF'\n"
        + settings_yaml +
        "DHCEOF\n"
        "socat TCP-LISTEN:3081,fork,reuseaddr TCP:127.0.0.1:3080 & "
        "exec dsh web --host 127.0.0.1 --port 3080"
    )
    body = {
        "Image": config.WORK_IMAGE,
        "Cmd": ["sh", "-c", boot],
        "WorkingDir": "/workspace",
        "Labels": {_LABEL: user["id"]},
        "Env": [
            f"DSH_CLOUD_TOKEN={token}",
            f"DEEPSEEK_API_KEY={token}",
            f"DEEPSEEK_SEARCH_BASE_URL={gateway}/llm/anthropic/v1",
            "DSH_TELEMETRY_DISABLED=1",
            # The per-user container IS the sandbox boundary (512MB/1CPU/pids512,
            # isolated network, no docker.sock, non-privileged, ephemeral). dsh's
            # own bash sandbox needs bubblewrap or a Landlock kernel (5.13+) —
            # neither exists in dsh-local:rc6 on this al8 5.10 host, so under the
            # default "workspace-write" mode EVERY bash call died with "no sandbox
            # backend is usable on this host" and the agent then stalled on an
            # approval prompt no cloud UI can answer. dsh's web profile keys both
            # the sandbox-policy mode AND the approval policy off this one env
            # var (dump-config: mode = DSH_PERMISSION_MODE ?? 'workspace-write';
            # approval = mode==='danger-full-access' ? 'never' : 'ask'), so
            # danger-full-access makes tools run unconfined and prompt-free —
            # correct when the container is the sandbox. DSH_ is bootstrap-only
            # (a workspace .env cannot forge it); only we set it, here.
            "DSH_PERMISSION_MODE=danger-full-access",
        ],
        "HostConfig": {
            "Memory": config.WORK_MEM_LIMIT_MB * 1024 * 1024,
            "NanoCpus": int(config.WORK_CPUS * 1e9),
            "PidsLimit": 512,
            "NetworkMode": config.WORK_NETWORK,
            "RestartPolicy": {"Name": "no"},
            "Mounts": [
                {"Type": "volume", "Source": f"dshwork-home-{hexid}", "Target": "/root"},
                {"Type": "volume", "Source": f"dshwork-ws-{hexid}", "Target": "/workspace"},
            ],
        },
    }
    r = await _docker("POST", "/containers/create", json_body=body,
                      params={"name": _cname(user["id"])})
    if r.status_code not in (201, 409):  # 409 = already exists (race)
        raise RuntimeError(f"container create failed: {r.status_code} {r.text[:200]}")


async def _start(user_id: str) -> None:
    r = await _docker("POST", f"/containers/{_cname(user_id)}/start")
    if r.status_code not in (204, 304):
        raise RuntimeError(f"container start failed: {r.status_code} {r.text[:200]}")


async def _stop(user_id: str) -> None:
    await _docker("POST", f"/containers/{_cname(user_id)}/stop", params={"t": 5})


async def _ready(user_id: str) -> bool:
    """dsh answers on :3081 once booted; the fence trusts a loopback Host."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://{_upstream(user_id)}/",
                                 headers={"host": "127.0.0.1:3080"})
            return r.status_code == 200
    except httpx.HTTPError:
        return False


async def ensure_workspace(user: dict) -> str:
    """Idempotent create+start; returns 'running' | 'starting'. Raises on
    hard failures (cap reached, engine down)."""
    uid = user["id"]
    info = await _inspect(uid)
    if info is None:
        running = await _running_workspaces()
        if len(running) >= config.WORK_MAX_CONCURRENT:
            raise RuntimeError("capacity")
        await _create(user)
        await _start(uid)
        _starting[uid] = time.time()
        return "starting"
    state = (info.get("State") or {}).get("Status", "")
    if state != "running":
        running = await _running_workspaces()
        if len(running) >= config.WORK_MAX_CONCURRENT:
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
    # Fast path: recently seen as running -> answer instantly (this endpoint
    # gates every asset/WS request; only re-probe after a quiet gap).
    if now - _last_seen.get(user["id"], 0) < 30 and user["id"] not in _starting:
        _last_seen[user["id"]] = now
        return Response(status_code=200, headers={"X-Work-Upstream": _upstream(user["id"])})

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

_PWA_INJECT = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1c38">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="DSH Cloud">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/pwa/icon-180.png">
<link rel="stylesheet" href="/pwa/mobile.css">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){})}</script>
"""


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
            upstream = await client.get(f"http://{_upstream(user['id'])}/",
                                        headers={"host": "127.0.0.1:3080"})
    except httpx.HTTPError:
        return RedirectResponse(f"{site}/work/starting", status_code=302)
    html = upstream.text
    if "</head>" in html:
        html = html.replace("</head>", _PWA_INJECT + "</head>", 1)
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
    return FileResponse(_pwa_path("manifest.webmanifest"),
                        media_type="application/manifest+json")


@router.get("/sw.js")
async def pwa_sw():
    from fastapi.responses import FileResponse
    return FileResponse(_pwa_path("sw.js"), media_type="text/javascript",
                        headers={"cache-control": "no-cache"})


@router.get("/pwa/{name}")
async def pwa_asset(name: str):
    from fastapi.responses import FileResponse
    safe = re.sub(r"[^a-zA-Z0-9._-]", "", name)
    path = _pwa_path(safe)
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "not_found"})
    media = "text/css" if safe.endswith(".css") else "image/png"
    return FileResponse(path, media_type=media,
                        headers={"cache-control": "public, max-age=86400"})


# --- user-facing endpoints ---------------------------------------------------

@router.get("/api/work/status")
async def work_status(request: Request):
    user = try_resolve_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "not_authenticated"})
    if not config.WORK_ENABLED:
        return {"enabled": False}
    info = await _inspect(user["id"])
    state = (info.get("State") or {}).get("Status", "none") if info else "none"
    ready = state == "running" and await _ready(user["id"])
    return {"enabled": True, "state": "running" if ready else ("starting" if state == "running" else state),
            "url": f"https://{config.WORK_DOMAIN}/",
            "credits_per_min": config.WORK_CREDITS_PER_MIN,
            "idle_stop_min": config.WORK_IDLE_STOP_MIN}


@router.post("/api/work/stop")
async def work_stop(request: Request):
    user = resolve_user(request)
    await _stop(user["id"])
    _last_seen.pop(user["id"], None)
    _starting.pop(user["id"], None)
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
    try:
        state = await ensure_workspace(user)
    except RuntimeError as e:
        kind = "busy" if str(e) == "capacity" else "error"
        return RedirectResponse(f"{site}/work/starting?state={kind}", status_code=302)
    if state == "running":
        return RedirectResponse(f"https://{config.WORK_DOMAIN}/", status_code=302)
    return RedirectResponse(f"{site}/work/starting", status_code=302)


@router.get("/work/starting")
async def work_starting(request: Request, state: str = ""):
    """Minimal polling page shown while the container boots (~5-20s)."""
    if state == "busy":
        title, body, poll = "云工作台当前繁忙", "在线名额已满，请稍后再试或使用桌面版。", "false"
    elif state == "error":
        title, body, poll = "启动失败", "云工作台启动失败，请稍后重试；问题持续请联系支持。", "false"
    else:
        title, body, poll = "云工作台启动中…", "正在为你准备云端工作区，通常需要 5–20 秒。", "true"
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/static/app.css">
</head><body data-page="work">
<section class="auth-wrap"><div class="card auth-card center">
<h1 class="auth-title">{title}</h1>
<p class="muted">{body}</p>
<div class="spinner" aria-hidden="true" style="margin:18px auto"></div>
<p class="muted small"><a href="/console">返回控制台</a></p>
</div></section>
<script>
if ({poll}) {{
  (async function poll() {{
    try {{
      const r = await fetch('/api/work/status');
      const s = await r.json();
      if (s.state === 'running') {{ location.href = s.url; return; }}
    }} catch (e) {{}}
    setTimeout(poll, 2000);
  }})();
}}
</script></body></html>"""
    return HTMLResponse(html)


# --- billing + idle reaper (one asyncio task, started from main.py) ----------

async def billing_reaper_loop() -> None:
    log.info("workspace billing/reaper loop started (%s credits/min, idle-stop %s min)",
             config.WORK_CREDITS_PER_MIN, config.WORK_IDLE_STOP_MIN)
    while True:
        try:
            await asyncio.sleep(60)
            containers = await _running_workspaces()
            now = time.time()
            for c in containers:
                uid = (c.get("Labels") or {}).get(_LABEL, "")
                if not uid:
                    continue
                # a running workspace bills whether or not the tab is focused;
                # the reaper is what caps the meter
                credits.spend(uid, config.WORK_CREDITS_PER_MIN, kind="workspace",
                              model="dshwork", request_id=f"ws-{int(now // 60)}")
                last = _last_seen.setdefault(uid, now)  # re-seed after restart
                idle_out = now - last > config.WORK_IDLE_STOP_MIN * 60
                broke = credits.balance(uid) <= -config.OVERDRAFT_LIMIT_CREDITS
                if idle_out or broke:
                    log.info("stopping workspace %s (%s)", uid,
                             "idle" if idle_out else "credits exhausted")
                    await _stop(uid)
                    _last_seen.pop(uid, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("workspace loop iteration failed")  # never die
