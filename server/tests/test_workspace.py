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

from app import config, credits, db, rate_limit, workspace  # noqa: E402
from app.main import app  # noqa: E402


class FakeDocker:
    """Emulates the scoped docker socket proxy + the container's readiness."""
    def __init__(self):
        self.containers = {}      # cname -> {"State": {"Status": ...}}
        self.ready = set()        # cnames that answer on :3081
        self.creates = 0
        self.starts = 0
        self.stops = 0

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
                                      "Config": {"Labels": (json_body or {}).get("Labels", {})}}
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
    monkeypatch.setattr(workspace, "_docker", fd.docker)

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
    r = c.post("/api/auth/register", json={"email": email, "password": "password123"})
    if r.status_code == 409:
        c.post("/api/auth/login", json={"email": email, "password": "password123"})
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


def test_bills_only_minutes_the_agent_worked(fake):
    """An open tab must be free: only a minute with a real gateway call bills."""
    c, uid = _user("bill@test.local")
    c.get("/api/work/route"); c.get("/api/work/route")

    # agent just called the gateway -> this minute is billable
    _mark_agent_active(uid, ago_s=5)
    before = credits.balance(uid)
    asyncio.run(workspace.reaper_tick(time.time()))
    assert credits.balance(uid) == before - config.WORK_CREDITS_PER_MIN

    # tab still open, agent quiet for 5 minutes -> free, container stays up
    _mark_agent_active(uid, ago_s=300)
    workspace._last_seen[uid] = time.time()
    before = credits.balance(uid)
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert credits.balance(uid) == before
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
