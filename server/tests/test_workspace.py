"""Cloud workspace orchestration: routing gate, container lifecycle, billing.

The docker socket proxy and the dsh container's :3081 are stubbed at the
httpx boundary so the whole ensure/route/bill flow runs without Docker.
"""
import asyncio
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="dhc-work-")
os.environ.update({
    "DHC_DEV": "1",
    "AUTH_SECRET": "test-secret",
    "DHC_DATA_DIR": _TMP,
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "WORK_ENABLED": "1",
    "WORK_CREDITS_PER_MIN": "2",
    "WORK_MAX_CONCURRENT": "2",
    "UPSTREAM_API_KEY": "sk-upstream-test",
    "FREE_SIGNUP_CREDITS": "500",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, credits, db, rate_limit, work_access, workspace, workbackend  # noqa: E402
from app.main import app  # noqa: E402
from ._signup import signup


class FakeDocker:
    """Emulates the scoped docker socket proxy + the container's readiness."""
    def __init__(self):
        self.containers = {}      # cname -> {"State": {"Status": ...}}
        self.ready = set()        # cnames that answer on :3081
        self.creates = 0
        self.starts = 0
        self.stops = 0
        self.deletes = 0
        self.created_cmd = []   # boot script of each created container
        # WORK_IMAGE's currently resolved id. Rebuilding or retagging the image
        # moves this without moving the tag, which is the case the real engine
        # hits on every runtime bump.
        self.image_id = "sha256:img-old"
        self.image_lookup_ok = True

    async def docker(self, method, path, *, json_body=None, params=None):
        import httpx
        req = httpx.Request(method, "http://proxy" + path)

        def resp(code, body=None):
            return httpx.Response(code, json=body if body is not None else {}, request=req)

        if path == "/containers/create":
            self.creates += 1
            cname = params["name"]
            # carry the caller's labels verbatim: the real _LABEL value is the
            # user id, and the reaper bills whoever that label names
            self.containers[cname] = {"State": {"Status": "created"},
                                      "Image": self.image_id,
                                      "Config": {"Labels": (json_body or {}).get("Labels", {})}}
            self.created_cmd.append(((json_body or {}).get("Cmd") or ["", "", ""])[-1])
            return resp(201, {"Id": cname})
        if path == "/containers/json":   # list — must precede the inspect pattern
            out = [{"Names": ["/" + n], "Labels": c["Config"]["Labels"]}
                   for n, c in self.containers.items()
                   if c["State"]["Status"] == "running"]
            return resp(200, out)
        if path.startswith("/containers/") and path.endswith("/json"):
            cname = path.split("/")[2]
            if cname in self.containers:
                return resp(200, self.containers[cname])
            return resp(404)
        if path.endswith("/start"):
            cname = path.split("/")[2]
            self.starts += 1
            self.containers[cname]["State"]["Status"] = "running"
            self.ready.add(cname)   # boots "instantly" in the stub
            return resp(204)
        if path.endswith("/stop"):
            cname = path.split("/")[2]
            self.stops += 1
            if cname in self.containers:
                self.containers[cname]["State"]["Status"] = "exited"
            self.ready.discard(cname)
            return resp(204)
        if path.startswith("/images/") and path.endswith("/json"):
            if not self.image_lookup_ok:
                return resp(404)
            return resp(200, {"Id": self.image_id})
        if method == "DELETE" and path.startswith("/containers/"):
            cname = path.split("/")[2]
            self.deletes += 1
            self.containers.pop(cname, None)
            self.ready.discard(cname)
            return resp(204)
        return resp(500)


@pytest.fixture(autouse=True)
def _work_config(monkeypatch):
    # config freezes env at first import; pin what these tests depend on so the
    # suite passes regardless of which module imported config first.
    monkeypatch.setattr(config, "WORK_ENABLED", True)
    monkeypatch.setattr(config, "WORK_MAX_CONCURRENT", 2)
    monkeypatch.setattr(config, "WORK_CREDITS_PER_MIN", 2)
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-upstream-test")
    rate_limit._windows.clear()  # shared process global: reset per test so the
    # suite's cumulative registrations don't trip the per-IP register cap


@pytest.fixture()
def fake(monkeypatch):
    fd = FakeDocker()

    # 打在 DockerBackend._api 上, 不是打在某个模块级函数上: 这样跑过的是后端
    # 真正的请求构造与响应解析, 而不只是 workspace.py 的调用顺序。
    async def api(self, method, path, *, json_body=None, params=None):
        return await fd.docker(method, path, json_body=json_body, params=params)
    monkeypatch.setattr(workbackend.DockerBackend, "_api", api)
    monkeypatch.setattr(config, "WORK_BACKEND", "docker")
    monkeypatch.setattr(workspace, "_backend", workbackend.DockerBackend())
    workspace._host.clear()

    async def ready(uid):
        return workspace._cname(uid) in fd.ready
    monkeypatch.setattr(workspace, "_ready", ready)
    # fresh in-process state each test
    workspace._last_seen.clear()
    workspace._starting.clear()
    workspace._started_at.clear()
    return fd


def _user(email="w@test.local"):
    c = TestClient(app)
    signup(c, email)
    uid = db.query_one("SELECT id FROM users WHERE email=?", (email,))["id"]
    return c, uid


def test_route_requires_auth():
    r = TestClient(app).get("/api/work/route", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["location"]


def test_route_creates_and_serves_container(fake):
    c, uid = _user()
    r = c.get("/api/work/route", follow_redirects=False)
    # first hit boots the container -> starting page (or 200 if ready same tick)
    assert r.status_code in (200, 302)
    # drive to ready and re-route
    r2 = c.get("/api/work/route", follow_redirects=False)
    assert r2.status_code == 200
    assert r2.headers["X-Work-Upstream"] == f"{workspace._cname(uid)}:3081"
    assert fake.creates == 1 and fake.starts >= 1


def test_route_blocks_when_no_credits(fake):
    c, uid = _user("broke@test.local")
    credits.spend(uid, 500, kind="llm", model="m")  # burn signup grant
    r = c.get("/api/work/route", follow_redirects=False)
    assert r.status_code == 302 and "/pricing" in r.headers["location"]
    assert fake.creates == 0  # never even created a container


def test_capacity_cap(fake, monkeypatch):
    monkeypatch.setattr(config, "WORK_MAX_CONCURRENT", 1)
    c1, _ = _user("cap1@test.local")
    c1.get("/api/work/route"); c1.get("/api/work/route")  # boots + ready
    c2, _ = _user("cap2@test.local")
    r = c2.get("/api/work/route", follow_redirects=False)
    assert r.status_code == 302 and "state=busy" in r.headers["location"]


def _mark_agent_active(uid, ago_s=0.0):
    """Move the workspace device's last_seen — the 'agent worked' signal."""
    db.query("UPDATE devices SET last_seen=? WHERE user_id=? AND platform='cloud'",
             (time.time() - ago_s, uid))


def test_meters_only_minutes_the_agent_worked(fake):
    """An open tab must be free: only a minute with a real gateway call counts."""
    c, uid = _user("bill@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")

    # agent just called the gateway -> this minute is metered
    _mark_agent_active(uid, ago_s=5)
    before_minutes = work_access.used_minutes(uid)
    before_credits = credits.balance(uid)
    asyncio.run(workspace.reaper_tick(time.time()))
    assert work_access.used_minutes(uid) == before_minutes + 1
    # …and machine time is NOT paid for in credits
    assert credits.balance(uid) == before_credits

    # tab still open, agent quiet for 5 minutes -> free, container stays up
    _mark_agent_active(uid, ago_s=300)
    workspace._last_seen[uid] = time.time()
    before_minutes = work_access.used_minutes(uid)
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert work_access.used_minutes(uid) == before_minutes
    assert fake.stops == stops_before


def test_agent_last_active_ignores_browser_polling(fake):
    """Browser traffic hits /api/work/route with the session cookie (no device),
    so it must not register as agent work."""
    c, uid = _user("idlebill@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    _mark_agent_active(uid, ago_s=600)
    stale = workspace.agent_last_active(uid)
    for _ in range(3):
        c.get("/api/work/route")
    assert workspace.agent_last_active(uid) == stale


def test_abandoned_open_tab_is_reaped(fake, monkeypatch):
    """Free idle minutes must not let an open tab hold RAM forever."""
    c, uid = _user("abandon@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 30)
    _mark_agent_active(uid, ago_s=31 * 60)             # agent quiet past the backstop
    workspace._started_at[uid] = time.time() - 31 * 60  # and running that long
    workspace._last_seen[uid] = time.time()            # …but the tab is still polling
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before + 1


def test_resumed_workspace_gets_a_grace_window(fake, monkeypatch):
    """Resuming a workspace whose last agent call is older than the backstop must
    not reap it before the user can type — otherwise returning after a long break
    starts a start/stop loop."""
    c, uid = _user("resume@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 30)
    _mark_agent_active(uid, ago_s=6 * 3600)     # last worked hours ago
    workspace._started_at[uid] = time.time()    # …but just started now
    workspace._last_seen[uid] = time.time()
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before           # still alive
    # and the grace window is not infinite: once it lapses, the backstop fires
    workspace._started_at[uid] = time.time() - 31 * 60
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before + 1


def test_user_gone_still_reaps(fake, monkeypatch):
    c, uid = _user("gone@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    _mark_agent_active(uid, ago_s=10)
    workspace._last_seen[uid] = time.time() - (config.WORK_IDLE_STOP_MIN + 1) * 60
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before + 1


def test_stop_endpoint(fake):
    c, uid = _user("stop@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    r = c.post("/api/work/stop")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake.stops >= 1


def test_status_reports_state(fake):
    c, uid = _user("status@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    s = c.get("/api/work/status").json()
    assert s["enabled"] is True
    assert s["credits_per_min"] == 2
    assert s["state"] in ("running", "starting")


# --- runtime upgrades: a new image has to actually reach existing workspaces --

def test_existing_workspace_is_recreated_when_the_image_changes(fake):
    """Bumping WORK_IMAGE must rebuild the container, not just restart it.

    The boot fingerprint does not move when only the image does. Before the
    image check, a runtime bump (rc6 -> rc8) reached brand-new users only:
    anyone who already had a container got it started again on the old image,
    with nothing in the logs to say so.
    """
    c, uid = _user("upgrade@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    assert fake.creates == 1

    fake.image_id = "sha256:img-new"      # operator rebuilt / retagged WORK_IMAGE
    # Staleness is a cold-path check: /route's 30s fast path returns without
    # touching the engine at all, deliberately, because it gates every asset
    # and WebSocket frame. Backdate last_seen so this request is a cold one —
    # without it this test passes no matter what ensure_workspace does.
    workspace._last_seen[uid] = time.time() - 31
    c.get("/api/work/route")

    assert fake.creates == 2, "restarted on the stale image instead of rebuilding"
    assert fake.deletes == 1
    assert fake.containers[workspace._cname(uid)]["Image"] == "sha256:img-new"


def test_image_lookup_failure_leaves_the_workspace_alone(fake):
    """An unresolvable WORK_IMAGE must not tear down a working container.

    Treating a failed lookup as "stale" would turn a transient engine hiccup
    into every session on the host being destroyed at once — a far worse
    outcome than running one build behind.
    """
    c, uid = _user("hiccup@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    assert fake.creates == 1

    fake.image_lookup_ok = False
    workspace._last_seen[uid] = time.time() - 31   # cold path; see the test above
    c.get("/api/work/route")

    assert fake.creates == 1 and fake.deletes == 0


# --- port preview: the agent's server runs on the CONTAINER's loopback -------

@pytest.fixture()
def container_http(monkeypatch):
    """Stub the container's own HTTP server (what /preview/<port>/ proxies to)."""
    seen = {}

    class FakeUpstream:
        def __init__(self):
            self.routes = {
                "/": (200, "text/html", b"<html><head><title>Snake</title></head>"
                                        b"<body><script src='./game.js'></script></body></html>"),
                "/game.js": (200, "application/javascript", b"// snake"),
                "/dir": (301, "text/html", b""),
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kw):
            import httpx as _h
            seen["url"] = url
            seen["method"] = method
            path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
            code, ctype, body = self.routes.get(path, (404, "text/plain", b"nope"))
            headers = {"content-type": ctype}
            if code == 301:
                headers["location"] = "/dir/"
            return _h.Response(code, headers=headers, content=body,
                               request=_h.Request(method, url))

    monkeypatch.setattr(workspace.httpx, "AsyncClient", lambda **kw: FakeUpstream())
    return seen


def test_boot_seeds_platform_instructions_mergeably(fake):
    """The agent must learn the preview URL from $DSH_HOME/AGENTS.md, and the
    boot script must merge (not clobber) whatever the user wrote there."""
    c, uid = _user("bootmd@test.local")
    c.get("/api/work/route")
    boot = fake.created_cmd[-1]
    assert "/root/.dsh/AGENTS.md" in boot
    assert "/preview/" in boot                      # the URL the agent hands out
    assert "0.0.0.0" in boot                        # bind guidance
    assert "dshcloud:begin" in boot and "dshcloud:end" in boot   # marker-delimited
    assert "cat > /root/.dsh/AGENTS.md" not in boot  # never a wholesale overwrite


def test_preview_requires_login():
    r = TestClient(app).get("/preview/8080/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["location"]


def test_preview_proxies_container_port(fake, container_http):
    c, uid = _user("prev@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    r = c.get("/preview/8080/")
    assert r.status_code == 200
    assert container_http["url"] == f"http://{workspace._cname(uid)}:8080/"
    # relative asset refs must resolve under the preview prefix, not the site root
    assert '<base href="/preview/8080/">' in r.text
    assert "Snake" in r.text


def test_preview_rejects_dsh_own_ports(fake, container_http):
    """3080/3081 drive the agent with the session's authority — never proxy them."""
    c, _ = _user("prevblock@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    for port in (3080, 3081):
        assert c.get(f"/preview/{port}/").status_code == 400


def test_preview_rewrites_upstream_redirect(fake, container_http):
    c, _ = _user("prevredir@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    r = c.get("/preview/8080/dir", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/preview/8080/dir/"


def test_absolute_asset_path_falls_back_through_cookie(fake, container_http):
    """A previewed page asking for "/game.js" escapes the prefix; the preview
    cookie routes it back instead of 404ing."""
    c, uid = _user("prevfall@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    c.get("/preview/8080/")                       # sets the cookie
    r = c.get("/game.js")
    assert r.status_code == 200
    assert container_http["url"] == f"http://{workspace._cname(uid)}:8080/game.js"


def test_fallback_never_shadows_real_routes(fake, container_http):
    """The catch-all is registered last; real pages and APIs must still win."""
    c, _ = _user("prevshadow@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")
    c.get("/preview/8080/")                       # cookie is set for this client
    assert c.get("/api/health").json()["ok"] is True
    assert c.get("/api/work/status").json()["enabled"] is True
    assert c.get("/pricing").status_code == 200


def test_unknown_path_without_cookie_is_404():
    assert TestClient(app).get("/no/such/thing").status_code == 404


# --- outputs survive the container ------------------------------------------

def test_products_are_listed_from_the_volume_when_the_container_is_asleep(tmp_path, monkeypatch):
    """The workspace stops after 15 idle minutes, and 個人成品 read its listing
    over HTTP from the container — so for most of the day the page told a user
    their work was not there. The volume outlives the container; the listing
    comes off it."""
    from app import config, workspace

    uid = "u_" + "a" * 24
    vol = tmp_path / f"dshwork-ws-u{'a' * 24}" / "_data"
    vol.mkdir(parents=True)
    (vol / "report.html").write_text("<h1>hi</h1>")
    (vol / "deck.pptx").write_bytes(b"PK")
    (vol / "game").mkdir()
    (vol / "node_modules").mkdir()          # plumbing, not a product
    (vol / ".cache").mkdir()

    monkeypatch.setattr(config, "WORK_VOLUME_ROOT", str(tmp_path))
    names = workspace._workspace_files_offline(uid)
    assert names == ["deck.pptx", "game/", "report.html"]

    monkeypatch.setattr(config, "WORK_VOLUME_ROOT", "")
    assert workspace._workspace_files_offline(uid) == []   # unset -> feature off


def test_offline_file_route_cannot_walk_out_of_the_volume(tmp_path, monkeypatch):
    from app import config, workspace

    uid = "u_" + "b" * 24
    vol = tmp_path / f"dshwork-ws-u{'b' * 24}" / "_data"
    vol.mkdir(parents=True)
    (tmp_path / "secret").write_text("nope")
    monkeypatch.setattr(config, "WORK_VOLUME_ROOT", str(tmp_path))

    root = workspace._ws_volume_dir(uid)
    assert root is not None
    for attempt in ("../secret", "../../etc/passwd", "%2e%2e%2fsecret"):
        from urllib.parse import unquote
        try:
            target = (root / unquote(attempt)).resolve()
            target.relative_to(root.resolve())
            escaped = True
        except ValueError:
            escaped = False
        assert not escaped, attempt


def test_out_of_hours_lands_on_a_page_that_exists():
    """The paywall redirected to /work/upgrade, deleted with the workspace pass:
    every visitor whose hours ran out got a 404 instead of a way to fix it."""
    import inspect
    from app import workspace

    src = inspect.getsource(workspace)
    # the string as it appears in a redirect, not in prose about the old bug
    assert 'f"{site}/work/upgrade"' not in src
    assert src.count('f"{site}/pricing?reason=work#plans"') == 3


# ── 容量闸门: 静态并发上限之外的那道内存闸 ────────────────────────────────

def test_free_memory_is_read_from_the_host_not_the_container(monkeypatch, tmp_path):
    """容器里的 /proc/meminfo 就是宿主的 —— 这道闸门的全部前提。

    用 MemAvailable 而不是 MemFree: 后者把可回收的 page cache 算作已用, 在一台
    跑了半个月的机器上会永远接近 0, 那样这道闸门就变成永远关闭。"""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       15690236 kB\n"
        "MemFree:          204800 kB\n"        # 只有 200M "空闲"
        "MemAvailable:    7821312 kB\n")       # 但 7.4G 可用
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: real_open(meminfo, *a, **k)
                        if p == "/proc/meminfo" else real_open(p, *a, **k))
    assert workbackend.host_free_mb() == 7638        # 取的是 MemAvailable


def test_capacity_blocks_when_the_host_is_low_on_memory(monkeypatch):
    monkeypatch.setattr(config, "WORK_MEM_LIMIT_MB", 512)
    monkeypatch.setattr(config, "WORK_MIN_FREE_MB", 1536)
    # 需要 512 + 1536 = 2048
    monkeypatch.setattr(workbackend, "host_free_mb", lambda: 2048)
    assert workspace._capacity_reason() == ""          # 刚好够, 放行
    monkeypatch.setattr(workbackend, "host_free_mb", lambda: 2047)
    assert workspace._capacity_reason().startswith("memory:")   # 差 1M, 拦下


def test_unreadable_meminfo_lets_the_workspace_start(monkeypatch):
    """读不到就放行 —— 与本模块其余闸门同一姿态。反过来的话, 一个读不到 /proc
    的环境异常会把所有人挡在门外, 而那比偶尔一次内存紧张糟得多。"""
    monkeypatch.setattr(workbackend, "host_free_mb", lambda: None)
    assert workspace._capacity_reason() == ""


def test_workspace_containers_are_first_in_line_for_the_oom_killer():
    """OOM killer 按内存占用挑, 同机最大的是 postgres / elasticsearch —— 那是别人
    的数据库, 而且不是它闯的祸。工作台可随时重启、卷还在, 该它先死。"""
    assert config.WORK_OOM_SCORE_ADJ > 0
