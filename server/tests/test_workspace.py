"""Cloud workspace orchestration: routing gate, container lifecycle, billing.

The docker socket proxy and the dsh container's :3081 are stubbed at the
httpx boundary so the whole ensure/route/bill flow runs without Docker.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="dhc-work-")
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test-secret",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "WORK_ENABLED": "1",
        "WORK_CREDITS_PER_MIN": "2",
        "WORK_MAX_CONCURRENT": "2",
        "UPSTREAM_API_KEY": "sk-upstream-test",
        "FREE_SIGNUP_CREDITS": "500",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import StreamingResponse  # noqa: E402

from app import (  # noqa: E402
    config,
    credits,
    db,
    model_catalog,
    products,
    rate_limit,
    security,
    work_access,
    workbackend,
    workspace,
)
from app.main import app  # noqa: E402

from ._signup import signup


class FakeDocker:
    """Emulates the scoped docker socket proxy + the container's readiness."""

    def __init__(self):
        self.containers = {}  # cname -> {"State": {"Status": ...}}
        self.ready = set()  # cnames that answer on :3081
        self.creates = 0
        self.starts = 0
        self.stops = 0
        self.deletes = 0
        self.created_cmd = []  # boot script of each created container
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
            self.containers[cname] = {
                "State": {"Status": "created"},
                "Image": self.image_id,
                "Config": {"Labels": (json_body or {}).get("Labels", {})},
            }
            self.created_cmd.append(((json_body or {}).get("Cmd") or ["", "", ""])[-1])
            return resp(201, {"Id": cname})
        if path == "/containers/json":  # list — must precede the inspect pattern
            out = [
                {"Names": ["/" + n], "Labels": c["Config"]["Labels"]}
                for n, c in self.containers.items()
                if c["State"]["Status"] == "running"
            ]
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
            self.ready.add(cname)  # boots "instantly" in the stub
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

    async def ready(key, product=None):
        return workspace._cname(key) in fd.ready

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


def test_route_ignores_the_products_own_authorization_header(fake):
    """产品自带的 Authorization 头不能把用户从我们这层踢出去。

    forward_auth 的子请求会原样带上浏览器发给**产品**的头。要是拿产品自己的
    Bearer 当 DSH 令牌验, 验不过就 302 去登录页 —— 表现是产品控制台每个请求都
    被我们弹回登录, 而用户明明已经登录, 服务端也一个错都不报。

    (更正: 加这条时以为 Dify 正踩着, 实测不是 —— 它的 access_token 是 HttpOnly,
    前端读不到, 走的是 cookie + X-CSRF-Token, 不发 Authorization。所以这是
    防御性的守卫, 不是在复现一次真实故障。)
    """
    c, uid = _user("bearer@test.local")
    foreign = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.not-ours.sig"}
    c.get("/api/work/route", headers=foreign, follow_redirects=False)
    r = c.get("/api/work/route", headers=foreign, follow_redirects=False)
    assert r.status_code == 200, "被产品自己的令牌顶掉了 —— 控制台会整个弹回登录"
    assert r.headers["X-Work-Upstream"] == f"{workspace._cname(uid)}:3081"
    # 没有 cookie 时仍该拒绝: 只是"不看 Authorization", 不是"放行"
    bare = TestClient(app).get("/api/work/route", headers=foreign, follow_redirects=False)
    assert bare.status_code == 302 and "/login" in bare.headers["location"]


def test_route_blocks_when_no_credits(fake):
    c, uid = _user("broke@test.local")
    credits.spend(uid, 500, kind="llm", model="m")  # burn signup grant
    r = c.get("/api/work/route", follow_redirects=False)
    assert r.status_code == 302 and "/pricing" in r.headers["location"]
    assert fake.creates == 0  # never even created a container


def test_capacity_cap(fake, monkeypatch):
    monkeypatch.setattr(config, "WORK_MAX_CONCURRENT", 1)
    c1, _ = _user("cap1@test.local")
    c1.get("/api/work/route")
    c1.get("/api/work/route")  # boots + ready
    c2, _ = _user("cap2@test.local")
    r = c2.get("/api/work/route", follow_redirects=False)
    assert r.status_code == 302 and "state=busy" in r.headers["location"]


def _mark_agent_active(uid, ago_s=0.0):
    """Move the workspace device's last_seen — the 'agent worked' signal."""
    db.query(
        "UPDATE devices SET last_seen=? WHERE user_id=? AND platform='cloud'", (time.time() - ago_s, uid)
    )


def test_meters_every_minute_the_workspace_runs(fake):
    """按容器运行时间计量；智能体活跃时间只参与空闲回收判定。"""
    c, uid = _user("bill@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    # 智能体刚干过活 —— 计一分钟
    _mark_agent_active(uid, ago_s=5)
    before_minutes = work_access.used_minutes(uid)
    before_credits = credits.balance(uid)
    asyncio.run(workspace.reaper_tick(time.time()))
    assert work_access.used_minutes(uid) == before_minutes + 1
    # 机时永远不扣积分 —— 这条没变, 两种额度互不占用
    assert credits.balance(uid) == before_credits

    # 页面开着但智能体闲着时，容器仍在运行，因此继续计量。
    _mark_agent_active(uid, ago_s=300)
    workspace._last_seen[uid] = time.time()
    workspace._started_at[uid] = time.time()
    before_minutes = work_access.used_minutes(uid)
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert work_access.used_minutes(uid) == before_minutes + 1, "运行中的闲置分钟必须计量"
    assert fake.stops == stops_before  # 还没到回收窗口


def test_idle_workspaces_are_reclaimed_within_the_stated_window(fake, monkeypatch):
    """条款写着"约 10 分钟未操作后自动回收" —— 这条钉住那个承诺。

    场景是标签页开着但没人动它: 轮询照常, 交互没有, 智能体也没活儿。
    """
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    c, uid = _user("idle10@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    now = time.time()
    workspace._last_seen[uid] = now  # 页面还开着, 轮询没停
    _mark_agent_active(uid, ago_s=9 * 60)
    workspace._started_at[uid] = now - 9 * 60
    workspace._user_active[uid] = now - 9 * 60  # 最后一次真人动作
    stops = fake.stops
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops, "9 分钟就回收了 —— 比承诺的还早"

    _mark_agent_active(uid, ago_s=11 * 60)
    workspace._started_at[uid] = now - 11 * 60
    workspace._user_active[uid] = now - 11 * 60
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops + 1, "过了 10 分钟仍未回收 —— 闲置容器会一直计费"


def test_a_person_using_it_is_never_reaped(fake, monkeypatch):
    """**这条就是用户报的毛病。**

    开着工作台看长回答、翻文件、自己敲代码 —— 一个小时没让智能体调过网关, 也
    绝不该被回收。旧规则只认"智能体调过网关", 于是读十分钟东西就被踢下线, 回来
    还要等一次冷启动 (顺带弹一个新 EIP, 给站主发一条短信)。
    """
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    c, uid = _user("using@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    now = time.time()
    workspace._started_at[uid] = now - 60 * 60  # 开了一小时
    _mark_agent_active(uid, ago_s=60 * 60)  # 智能体一小时没动
    workspace._last_seen[uid] = now  # 页面开着
    workspace._user_active[uid] = now - 30  # 半分钟前还在动
    stops = fake.stops
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops, "人正在用却被回收了 —— 正是这次要修的毛病"


def test_presence_is_not_inferred_from_browser_polling(fake, monkeypatch):
    """轮询 ≠ 有人在。

    忘了关的标签页会一直轮询。要是拿它当在场, 工作台整夜不回收, 而机时按容器
    存在时间计费 —— 烧的是用户自己的额度。
    """
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    c, uid = _user("polling@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    now = time.time()
    workspace._started_at[uid] = now - 40 * 60
    _mark_agent_active(uid, ago_s=40 * 60)
    workspace._user_active[uid] = now - 40 * 60  # 40 分钟没人动过
    stops = fake.stops
    for _ in range(3):  # 轮询照常进行
        workspace._last_seen[uid] = now
        asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops + 1, "只靠轮询就续住了 —— 空标签页会整夜烧机时"


def test_closed_tab_is_reaped_quickly(fake, monkeypatch):
    """关掉页面之后每多留一分钟, 扣的都是用户的机时。

    判定不用 pagehide/sendBeacon (iOS 切个应用也发, 硬杀则一条都不发), 而是
    "轮询停了" —— 页面没了就没有请求经过 forward_auth。
    """
    monkeypatch.setattr(config, "WORK_TAB_GONE_MIN", 3)
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    c, uid = _user("closed@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    now = time.time()
    workspace._started_at[uid] = now - 30 * 60
    _mark_agent_active(uid, ago_s=30 * 60)  # 没活儿在跑
    workspace._user_active[uid] = now - 2 * 60  # 两分钟前还在用
    workspace._last_seen[uid] = now - 2 * 60  # 轮询停了两分钟
    stops = fake.stops
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops, "刚断 2 分钟就回收 —— 手机切个应用回来就得冷启动"

    workspace._last_seen[uid] = now - 4 * 60
    workspace._user_active[uid] = now - 4 * 60
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops + 1, "页面早关了还留着 —— 白扣用户机时"


def test_closed_tab_with_a_running_agent_is_kept(fake, monkeypatch):
    """长任务必须能在关掉标签页之后跑完 —— 这是云工作台的意义所在。"""
    monkeypatch.setattr(config, "WORK_TAB_GONE_MIN", 3)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    c, uid = _user("longtask@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    now = time.time()
    workspace._started_at[uid] = now - 30 * 60
    workspace._last_seen[uid] = now - 20 * 60  # 页面早关了
    workspace._user_active[uid] = now - 20 * 60
    _mark_agent_active(uid, ago_s=30)  # 但智能体 30 秒前刚调过网关
    stops = fake.stops
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops, "把正在跑的长任务杀了"


def test_agent_last_active_ignores_browser_polling(fake):
    """Browser traffic hits /api/work/route with the session cookie (no device),
    so it must not register as agent work."""
    c, uid = _user("idlebill@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    _mark_agent_active(uid, ago_s=600)
    stale = workspace.agent_last_active(uid)
    for _ in range(3):
        c.get("/api/work/route")
    assert workspace.agent_last_active(uid) == stale


def test_abandoned_open_tab_is_reaped(fake, monkeypatch):
    """Free idle minutes must not let an open tab hold RAM forever."""
    c, uid = _user("abandon@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 30)
    _mark_agent_active(uid, ago_s=31 * 60)  # agent quiet past the backstop
    workspace._started_at[uid] = time.time() - 31 * 60  # and running that long
    workspace._last_seen[uid] = time.time()  # …but the tab is still polling
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before + 1


def test_resumed_workspace_gets_a_grace_window(fake, monkeypatch):
    """Resuming a workspace whose last agent call is older than the backstop must
    not reap it before the user can type — otherwise returning after a long break
    starts a start/stop loop."""
    c, uid = _user("resume@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 30)
    _mark_agent_active(uid, ago_s=6 * 3600)  # last worked hours ago
    workspace._started_at[uid] = time.time()  # …but just started now
    workspace._last_seen[uid] = time.time()
    stops_before = fake.stops
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before  # still alive
    # and the grace window is not infinite: once it lapses, the backstop fires
    workspace._started_at[uid] = time.time() - 31 * 60
    asyncio.run(workspace.reaper_tick(time.time()))
    assert fake.stops == stops_before + 1


def test_a_client_that_never_reports_presence_keeps_the_old_behaviour(fake, monkeypatch):
    """脚本没跑起来 (老缓存、CSP 挡了、报错) 就收不到在场上报。

    那时必须回落到加这条之前的口径 —— 由"智能体安静了多久"单独决定。绝不能因
    为"没收到心跳"就把正在用的人踢掉, 也不能反过来永远不收。
    """
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    c, uid = _user("noscript@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    workspace._user_active.pop(uid, None)  # 从来没上报过

    now = time.time()
    workspace._last_seen[uid] = now  # 页面开着, 轮询正常
    workspace._started_at[uid] = now - 5 * 60
    _mark_agent_active(uid, ago_s=5 * 60)
    stops = fake.stops
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops, "智能体才安静 5 分钟就收了"

    workspace._started_at[uid] = now - 11 * 60
    _mark_agent_active(uid, ago_s=11 * 60)
    asyncio.run(workspace.reaper_tick(now))
    assert fake.stops == stops + 1, "收不到心跳就永远不回收 —— 会一直计费"


def test_active_endpoint_stamps_presence(fake):
    """在场只能由这条路径写入 —— 普通轮询不行。"""
    c, uid = _user("active@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    workspace._user_active.pop(uid, None)

    for _ in range(3):  # 轮询: 不算在场
        c.get("/api/work/route")
    assert uid not in workspace._user_active, "轮询把自己算成了真人在场"

    r = c.post("/api/work/active")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert time.time() - workspace._user_active[uid] < 5


def test_active_endpoint_does_not_resurrect_a_reaped_workspace(fake):
    """续租不该把已经回收的工作台重新拉起来 —— 那是一次冷启动加一个新 EIP。"""
    c, uid = _user("nores@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    creates = fake.creates
    c.post("/api/work/stop")
    c.post("/api/work/active")
    assert fake.creates == creates, "心跳把容器又拉起来了"


def test_stop_endpoint(fake):
    c, uid = _user("stop@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    r = c.post("/api/work/stop")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake.stops >= 1


def test_status_reports_state(fake):
    c, uid = _user("status@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    s = c.get("/api/work/status").json()
    assert s["enabled"] is True
    assert s["credits_per_min"] == 2
    assert s["state"] in ("running", "starting")


# --- runtime upgrades: a new image has to actually reach existing workspaces --


def test_existing_workspace_is_recreated_when_the_image_changes(fake):
    """Changing WORK_IMAGE must rebuild an existing container, not restart it."""
    c, uid = _user("upgrade@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    assert fake.creates == 1

    fake.image_id = "sha256:img-new"  # operator rebuilt / retagged WORK_IMAGE
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
    c.get("/api/work/route")
    c.get("/api/work/route")
    assert fake.creates == 1

    fake.image_lookup_ok = False
    workspace._last_seen[uid] = time.time() - 31  # cold path; see the test above
    c.get("/api/work/route")

    assert fake.creates == 1 and fake.deletes == 0


# --- port preview: the agent's server runs on the CONTAINER's loopback -------


class _StreamingUpstream:
    def __init__(self, chunks: list[bytes], headers: dict[str, str], status_code: int = 200):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self.yielded = 0
        self.eager_reads = 0
        self.closed = False

    @property
    def content(self):
        self.eager_reads += 1
        self.yielded = len(self._chunks)
        return b"".join(self._chunks)

    async def aread(self):
        return self.content

    async def aiter_raw(self):
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class _StreamingClient:
    def __init__(self, upstream: _StreamingUpstream):
        self.upstream = upstream
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()
        return False

    async def request(self, *_args, **_kwargs):
        return self.upstream

    def build_request(self, method, url, **kwargs):
        import httpx

        return httpx.Request(method, url, **kwargs)

    async def send(self, _request, *, stream=False):
        assert stream is True
        return self.upstream

    async def aclose(self):
        self.closed = True


def _preview_request(method: str = "GET") -> Request:
    async def receive():
        raise AssertionError(f"{method} preview unexpectedly read a request body")

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/preview/8080/download.bin",
            "raw_path": b"/preview/8080/download.bin",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


@pytest.mark.asyncio
async def test_preview_non_html_response_streams_and_closes_after_iteration(monkeypatch):
    upstream = _StreamingUpstream(
        [b"abc", b"def"],
        {
            "content-type": "application/octet-stream",
            "content-encoding": "gzip",
            "content-length": "6",
        },
    )
    client = _StreamingClient(upstream)

    async def running(_user_id):
        return SimpleNamespace(running=True)

    monkeypatch.setattr(workspace, "try_resolve_user", lambda _request: {"id": "u_stream"})
    monkeypatch.setattr(workspace, "_inspect", running)
    monkeypatch.setattr(workspace, "_upstream_host", lambda _user_id: "workspace")
    monkeypatch.setattr(workspace.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await workspace.preview_proxy(_preview_request(), 8080, "download.bin")

    assert isinstance(response, StreamingResponse)
    assert upstream.eager_reads == 0
    assert upstream.yielded == 0
    assert upstream.closed is False and client.closed is False
    assert response.headers["content-encoding"] == "gzip"
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"abcdef"
    assert upstream.closed is True and client.closed is True


@pytest.mark.asyncio
async def test_preview_html_stops_at_rewrite_limit_and_closes_upstream(monkeypatch):
    upstream = _StreamingUpstream(
        [b"<html>123", b"must-not-be-read"],
        {"content-type": "text/html; charset=utf-8"},
    )
    client = _StreamingClient(upstream)

    async def running(_user_id):
        return SimpleNamespace(running=True)

    monkeypatch.setattr(config, "PREVIEW_HTML_MAX_BYTES", 8, raising=False)
    monkeypatch.setattr(workspace, "try_resolve_user", lambda _request: {"id": "u_html_limit"})
    monkeypatch.setattr(workspace, "_inspect", running)
    monkeypatch.setattr(workspace, "_upstream_host", lambda _user_id: "workspace")
    monkeypatch.setattr(workspace.httpx, "AsyncClient", lambda **_kwargs: client)

    response = await workspace.preview_proxy(_preview_request(), 8080, "index.html")

    assert response.status_code == 502
    assert b"preview_html_too_large" in response.body
    assert upstream.yielded == 1
    assert upstream.closed is True and client.closed is True


@pytest.fixture()
def container_http(monkeypatch):
    """Stub the container's own HTTP server (what /preview/<port>/ proxies to)."""
    seen = {}

    class FakeUpstream:
        def __init__(self):
            self.routes = {
                "/": (
                    200,
                    "text/html",
                    b"<html><head><title>Snake</title></head>"
                    b"<body><script src='./game.js'></script></body></html>",
                ),
                "/game.js": (200, "application/javascript", b"// snake"),
                "/dir": (301, "text/html", b""),
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def build_request(self, method, url, **kw):
            import httpx as _h

            seen["url"] = url
            seen["method"] = method
            seen["content"] = kw.get("content")
            return _h.Request(method, url, **kw)

        async def send(self, request, *, stream=False):
            assert stream is True
            path = request.url.path
            code, ctype, body = self.routes.get(path, (404, "text/plain", b"nope"))
            headers = {"content-type": ctype}
            if code == 301:
                headers["location"] = "/dir/"
            return _StreamingUpstream([body], headers, status_code=code)

        async def aclose(self):
            return None

    monkeypatch.setattr(workspace.httpx, "AsyncClient", lambda **kw: FakeUpstream())
    return seen


def test_boot_seeds_platform_instructions_mergeably(fake):
    """The agent must learn the preview URL from $DSH_HOME/AGENTS.md, and the
    boot script must merge (not clobber) whatever the user wrote there."""
    c, uid = _user("bootmd@test.local")
    c.get("/api/work/route")
    boot = fake.created_cmd[-1]
    assert "/root/.dsh/AGENTS.md" in boot
    assert "/preview/" in boot  # the URL the agent hands out
    assert "0.0.0.0" in boot  # bind guidance
    assert "dshcloud:begin" in boot and "dshcloud:end" in boot  # marker-delimited
    assert "cat > /root/.dsh/AGENTS.md" not in boot  # never a wholesale overwrite


def test_preview_requires_login():
    r = TestClient(app).get("/preview/8080/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["location"]


def test_preview_proxies_container_port(fake, container_http):
    c, uid = _user("prev@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    r = c.get("/preview/8080/")
    assert r.status_code == 200
    assert container_http["url"] == f"http://{workspace._cname(uid)}:8080/"
    assert container_http["content"] is None
    # relative asset refs must resolve under the preview prefix, not the site root
    assert '<base href="/preview/8080/">' in r.text
    assert "Snake" in r.text


def test_preview_chunked_upload_is_limited_after_login(fake, container_http, monkeypatch):
    c, _ = _user("prev-upload@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    monkeypatch.setattr(config, "PREVIEW_BODY_MAX_BYTES", 5)
    monkeypatch.setattr(config, "REQUEST_BODY_TIMEOUT_S", 1)

    r = c.post("/preview/8080/upload", content=iter([b"123", b"456"]))

    assert r.status_code == 413
    assert r.json()["detail"] == "request_body_too_large"


def test_preview_rejects_dsh_own_ports(fake, container_http):
    """3080/3081 drive the agent with the session's authority — never proxy them."""
    c, _ = _user("prevblock@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    for port in (3080, 3081):
        assert c.get(f"/preview/{port}/").status_code == 400


def test_preview_rewrites_upstream_redirect(fake, container_http):
    c, _ = _user("prevredir@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    r = c.get("/preview/8080/dir", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/preview/8080/dir/"


def test_absolute_asset_path_falls_back_through_cookie(fake, container_http):
    """A previewed page asking for "/game.js" escapes the prefix; the preview
    cookie routes it back instead of 404ing."""
    c, uid = _user("prevfall@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    c.get("/preview/8080/")  # sets the cookie
    r = c.get("/game.js")
    assert r.status_code == 200
    assert container_http["url"] == f"http://{workspace._cname(uid)}:8080/game.js"


def test_fallback_never_shadows_real_routes(fake, container_http):
    """The catch-all is registered last; real pages and APIs must still win."""
    c, _ = _user("prevshadow@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    c.get("/preview/8080/")  # cookie is set for this client
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
    (vol / "node_modules").mkdir()  # plumbing, not a product
    (vol / ".cache").mkdir()

    monkeypatch.setattr(config, "WORK_VOLUME_ROOT", str(tmp_path))
    names = workspace._workspace_files_offline(uid)
    assert names == ["deck.pptx", "game/", "report.html"]

    monkeypatch.setattr(config, "WORK_VOLUME_ROOT", "")
    assert workspace._workspace_files_offline(uid) == []  # unset -> feature off


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
        "MemFree:          204800 kB\n"  # 只有 200M "空闲"
        "MemAvailable:    7821312 kB\n"
    )  # 但 7.4G 可用
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(meminfo, *a, **k) if p == "/proc/meminfo" else real_open(p, *a, **k),
    )
    assert workbackend.host_free_mb() == 7638  # 取的是 MemAvailable


def test_capacity_blocks_when_the_host_is_low_on_memory(monkeypatch):
    monkeypatch.setattr(config, "WORK_MEM_LIMIT_MB", 512)
    monkeypatch.setattr(config, "WORK_MIN_FREE_MB", 1536)
    # 需要 512 + 1536 = 2048
    monkeypatch.setattr(workbackend, "host_free_mb", lambda: 2048)
    assert workspace._capacity_reason() == ""  # 刚好够, 放行
    monkeypatch.setattr(workbackend, "host_free_mb", lambda: 2047)
    assert workspace._capacity_reason().startswith("memory:")  # 差 1M, 拦下


def test_unreadable_meminfo_lets_the_workspace_start(monkeypatch):
    """缺少可选的宿主内存指标时保持现有可用性。"""
    monkeypatch.setattr(workbackend, "host_free_mb", lambda: None)
    assert workspace._capacity_reason() == ""


def test_workspace_containers_are_first_in_line_for_the_oom_killer():
    """可重建的工作区应优先于持久化控制面进程被系统回收。"""
    assert config.WORK_OOM_SCORE_ADJ > 0


def test_running_workspace_falls_back_to_storage_when_unreachable(fake, monkeypatch, caplog):
    """运行时预览不可达时，从持久化存储列出已有产物并记录降级。"""
    c, uid = _user("fallback@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    # 先确认容器确实在跑 —— 否则下面测的是另一条分支
    assert c.get("/api/work/status").json()["state"] in ("running", "starting")

    async def unreachable(user_id, limit=60):
        return []

    monkeypatch.setattr(workspace, "_workspace_files", unreachable)

    async def no_ports(user_id):
        return []

    monkeypatch.setattr(workspace, "_open_ports", no_ports)
    monkeypatch.setattr(
        workspace, "_workspace_files_offline", lambda user_id, limit=60: ["report.html", "src/"]
    )

    with caplog.at_level("WARNING"):
        r = c.get("/preview")
    assert r.status_code == 200
    assert "report.html" in r.text, "存储里有文件却没列出来 —— 用户会以为东西丢了"
    assert "/preview/file/report.html" in r.text
    assert "preview endpoint" in caplog.text, "降级时必须记录可操作诊断"


# --- 预览路径: 被预览的东西不能自己决定防护 ----------------------------------


def test_live_preview_is_sandboxed_like_the_offline_one(fake, container_http):
    """用户控制的在线预览与离线文件使用相同的源隔离策略。"""
    c, uid = _user("sandbox@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    r = c.get("/preview/8080/")
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" in csp, "活容器预览没有沙箱 —— 智能体写的页面能以用户身份调 API"
    assert "allow-same-origin" not in csp, "带了 allow-same-origin 等于没沙箱"


def test_upstream_cannot_override_our_csp_or_set_cookies(fake, monkeypatch):
    """智能体的服务若自带 CSP 或 Set-Cookie, 不能盖过我们的、也不能在我们的域上
    种 cookie。大小写不敏感地剔除 —— 这道防线不该押在"上游规规矩矩"上, 上游
    正是被预览的那个东西。"""
    c, uid = _user("hdr@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    class Hostile:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def build_request(self, method, url, **kw):
            import httpx as _h

            return _h.Request(method, url, **kw)

        async def send(self, request, *, stream=False):
            assert stream is True
            return _StreamingUpstream(
                [b"<html>hi</html>"],
                {
                    "content-type": "text/html",
                    "Content-Security-Policy": "default-src *",  # 故意用大写
                    "Set-Cookie": "evil=1; Path=/",
                },
            )

        async def aclose(self):
            return None

    monkeypatch.setattr(workspace.httpx, "AsyncClient", lambda **kw: Hostile())

    r = c.get("/preview/8080/")
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" in csp, "上游的 CSP 盖住了我们的"
    assert "default-src *" not in csp
    assert "evil=1" not in "; ".join(r.headers.get_list("set-cookie")), "智能体的服务在我们的域上种了 cookie"


def test_both_listings_hide_the_same_noise(fake, monkeypatch, tmp_path):
    """同一个工作台不该因为容器碰巧在不在跑就列出不同的东西。

    ECI 上容器闲置即销毁, 这个来回比 docker 时代频繁得多 —— 用户会看见自己的
    成品列表凭空多出 package-lock.json 又消失。
    """
    c, uid = _user("noise@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")

    # 容器的目录索引: 名字按百分号编码给出, 中文名也走这条路
    index = (
        '<a href="report.html">report.html</a>'
        '<a href="package-lock.json">package-lock.json</a>'
        '<a href="node_modules/">node_modules/</a>'
        '<a href="AI-%E5%87%BA%E6%B5%B7.pptx">x</a>'
    )

    class Idx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, **kw):
            import httpx as _h

            return _h.Response(200, text=index, request=_h.Request("GET", url))

    monkeypatch.setattr(workspace.httpx, "AsyncClient", lambda **kw: Idx())

    import asyncio

    got = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(workspace._workspace_files(uid))
    assert "package-lock.json" not in got
    assert not any(n.startswith("node_modules") for n in got)
    assert "report.html" in got
    # 编码过的中文名要留下 —— 过滤是为了噪音, 不是为了非 ASCII
    assert "AI-%E5%87%BA%E6%B5%B7.pptx" in got


# --- 预览源隔离 --------------------------------------------------------------


@pytest.fixture()
def isolated(monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_DOMAIN", "preview.dshcloud.online")
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://dshcloud.online")
    return "preview.dshcloud.online"


def test_agent_content_on_the_main_host_is_sent_to_the_preview_host(fake, isolated):
    """智能体生成的字节不能从会话源吐出来 —— 那正是要隔离掉的东西。"""
    c, uid = _user("iso1@test.local")
    for p in ("/preview/file/report.html", "/preview/8088/index.html"):
        r = c.get(p, follow_redirects=False)
        assert r.status_code == 307, f"{p} 没有被送去预览域"
        assert r.headers["location"].startswith(f"https://{isolated}{p}")


def test_our_own_ui_page_stays_on_the_main_host(fake, isolated):
    """/preview 是我们自己的界面, 不是智能体的内容 —— 不该被赶走。"""
    c, uid = _user("iso2@test.local")
    r = c.get("/preview", follow_redirects=False)
    assert r.status_code != 307


def test_the_preview_host_serves_no_api(fake, isolated):
    """否则智能体页面对着自己的源就能带凭据调接口, 而**同源请求连 Origin
    白名单那道闸都不会触发**。"""
    c, uid = _user("iso3@test.local")
    r = c.get("/api/work/status", headers={"host": "preview.dshcloud.online"})
    assert r.status_code == 404


def test_the_absolute_asset_fallback_refuses_on_the_main_host(fake, isolated):
    """预览 cookie 的域是整个站点, 主站也收得到。

    不在兜底处理器里拦一道, 绝对路径的资源照样从会话源吐出来 —— 隔离就只挡住了
    带 /preview/ 前缀的那一半。
    """
    c, uid = _user("iso4@test.local")
    c.cookies.set(workspace._PREVIEW_PORT_COOKIE, "8088")
    r = c.get("/style.css", follow_redirects=False)
    assert r.status_code == 404, "智能体的资源从主站源吐出来了"


def test_isolation_off_keeps_the_sandbox(fake, container_http, monkeypatch):
    """没有独立预览域时, 沙箱是唯一的防线, 不能跟着一起去掉。"""
    monkeypatch.setattr(config, "PREVIEW_DOMAIN", "")
    c, uid = _user("iso5@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    r = c.get("/preview/8080/")
    assert "sandbox" in r.headers.get("content-security-policy", "")


def test_isolation_on_drops_the_sandbox(fake, container_http, isolated):
    """有了独立域 + Origin 白名单, 跨源写入那条路已经断了, 就不必再为沙箱付
    掉 dev server 的 localStorage 与 HMR。"""
    c, uid = _user("iso6@test.local")
    c.get("/api/work/route")
    c.get("/api/work/route")
    r = c.get("/preview/8080/", headers={"host": "preview.dshcloud.online"})
    assert "sandbox" not in r.headers.get("content-security-policy", "")


def test_the_starting_page_is_a_real_page_not_a_stray_string():
    """装饰器贴错函数不会报错, 只会让路由指向另一个东西。

    实际发生过: @router.get("/work/starting") 一度贴在 _boot_wait_hint 上,
    于是这条路由返回 13 字节的 "20–40 秒", 而真正的轮询页根本没注册 ——
    工作台没就绪的用户看到一个裸字符串, 不轮询、不会自动进去。
    直接调函数的端到端测试全都够不着这一层, 只有走真实路由才能发现。
    """
    r = TestClient(app).get("/work/starting")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "/api/work/status" in r.text, "页面没有轮询逻辑 —— 用户会永远停在这里"
    assert len(r.text) > 500, f"返回体只有 {len(r.text)} 字节, 不像一个页面"


def test_the_progress_bar_is_driven_by_real_phases(fake, monkeypatch):
    """进度条要跟着后端真实阶段走, 不是按秒数假装。

    /api/work/status 必须给出与后端无关的 phase, 否则页面只能去解析
    docker/ECI 各自的状态名, 换后端就悄悄失准。
    """
    c, uid = _user("phase@test.local")
    s0 = c.get("/api/work/status").json()
    assert s0["phase"] in ("queued", "booting", "warming", "ready"), s0

    c.get("/api/work/route")
    c.get("/api/work/route")
    s1 = c.get("/api/work/status").json()
    assert s1["phase"] == "ready"  # FakeDocker 里容器瞬间就绪

    # 页面把这套词汇用起来了吗 —— 后端给了而页面不认, 等于没给
    page = c.get("/work/starting").text
    for p in ("queued", "booting", "warming", "ready"):
        assert p in page, f"页面没有处理 phase={p}"


def test_the_bar_never_claims_to_be_done_early():
    """未就绪的阶段, 上界必须小于 100。

    一条走到满格却还没进去的进度条, 比没有进度条更让人以为坏了。
    """
    import re

    from app import workspace as w

    bands = re.findall(r"(\w+):\[(\d+),(\d+),", w._BOOT_JS.replace(" ", ""))
    assert bands, "解析不到 BAND 定义"
    got = {name: (int(lo), int(hi)) for name, lo, hi in bands}
    assert set(got) == {"queued", "booting", "warming", "ready"}, got
    for name, (lo, hi) in got.items():
        assert lo <= hi, f"{name} 区间反了: {lo}..{hi}"
        if name != "ready":
            assert hi < 100, f"{name} 上界是 {hi}, 会在未就绪时走满"
    assert got["ready"][1] == 100


def test_the_loading_page_does_not_ship_someone_elses_logo():
    """页脚已经声明"与 DeepSeek 无背书关系"。把对方的标识摆进自家加载页,
    会正好抵消那句声明。这页是**五个云空间产品共用的第一印象**, 所以尤其要干净。
    """
    from app import workspace as w

    page = w._BOOT_CSS + w._BOOT_JS
    assert "deepseek" not in page.lower()


def test_the_loading_page_wears_the_homepage_blue():
    """加载页用主页那块深蓝, 且不跟随浏览器的浅/深色主题。

    它是品牌页面而不是文档页 —— 跟着系统主题变的话, 同一个产品在两个人的
    机器上是两种观感, 而这是用户见到工作台的第一屏。
    """
    from app import workspace as w

    assert "#0b1c38" in w._BOOT_CSS, "主页那块深蓝没用上"
    css = w._BOOT_CSS.replace(" ", "")
    assert 'body[data-page="work"]{background:#0b1c38' in css
    # 颜色写死而不是走 --brand 之类的变量: 变量在深色模式下会被改写
    assert "prefers-color-scheme" not in w._BOOT_CSS


def test_the_progress_head_is_a_blinking_caret():
    """进度条头上是一个**闪烁的光标**, 且与填充共用同一个百分比。

    两者各走各的话, 光标会飘在填充前面或后面 —— 不报错, 只是看着坏了。
    闪烁必须是两态跳变 (step-end): 渐隐看着像呼吸灯, 不像光标。
    """
    from app import workspace as w

    css = w._BOOT_CSS.replace(" ", "")
    assert "caret-blink" in css
    # 认的是 animation 声明本身, 不是注释里提到的 step-end ——
    # 第一版断言写成 `"step-end" in _BOOT_CSS`, 把 timing function 改成
    # ease-in-out 它照样绿, 因为旁边注释里就有这四个字。
    assert "animation:caret-blink1.06sstep-endinfinite" in css, "渐隐的话就不像光标了"
    js = w._BOOT_JS.replace(" ", "")
    assert "caret.style.left=cur+'%'" in js and "fill.style.width=cur+'%'" in js, "光标和填充必须用同一个 cur"


# --- 手机端外壳与 dsh 的接缝 -------------------------------------------------


def _pwa(name):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "app" / "static" / "pwa" / name).read_text(
        encoding="utf-8"
    )


def test_sidebar_tap_behaviour_actually_runs(tmp_path):
    """真的执行那段 JS, 而不是在源码里找子串。

    这个 bug 修了两次都没修好, 而两次的"测试"都是子串匹配 —— 第二次断言
    `col.contains(e.target)`, 连 `!col.contains(e.target)` (正是第一版的错误
    写法) 都能通过。字符串匹配对判定方向的错误完全无能为力。

    node 不在 dhc-server 镜像里, 所以这里在没有 node 时跳过; 手动跑:
        docker run --rm -v $PWD/server:/srv:ro --entrypoint node \
            dsh-local:rc8 /srv/tests/js/sidebar_tap.test.mjs
    """
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node —— 用 dsh-local 镜像手动跑 tests/js/sidebar_tap.test.mjs")
    script = Path(__file__).resolve().parent / "js" / "sidebar_tap.test.mjs"
    r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_scrim_is_not_a_pseudo_element_on_a_clipped_container():
    """遮罩曾是 sidebarCol 的 ::after —— 而 dsh 的 .pI_x6G_sidebarCol 与父容器
    .pI_x6G_frame **都是 overflow:hidden**, 定位在侧栏之外的伪元素被整个裁掉,
    从未渲染。改成挂在 body 上的真实元素。
    """
    from pathlib import Path

    pwa = Path(__file__).resolve().parent.parent / "app" / "static" / "pwa"
    css = (pwa / "mobile.css").read_text(encoding="utf-8")
    assert "#dhc-scrim" in css
    assert "sidebarCol" not in css[css.index("#dhc-scrim") :], "遮罩又挂回侧栏里了"


def test_tap_outside_is_scoped_to_narrow_screens():
    """桌面端点聊天区收起侧栏是错的 —— 断点要和 mobile.css 一致。"""
    js, css = _pwa("workspace-chrome.js"), _pwa("mobile.css")
    assert "max-width: 760px" in js
    assert "max-width: 760px" in css, "两处断点不一致, 会出现只改一半的行为"


def test_the_tap_must_still_reach_what_it_hit():
    """点输入框那一下要既收抽屉又聚焦输入框。

    吞掉它会让人以为"点了没反应" —— 而那正是这次要修的毛病。
    """
    js = _pwa("workspace-chrome.js")
    body = js[js.index("function tapOutsideSidebar") : js.index("function watchSidebar")]
    assert "preventDefault" not in body, "吞掉了原本的点击"
    assert "stopPropagation" not in body


def test_the_tap_handler_runs_in_capture_phase():
    """抽屉里的条目自己会 stopPropagation, 冒泡阶段收不到抽屉外那一下。"""
    import re

    js = _pwa("workspace-chrome.js")
    m = re.search(r"addEventListener\(\s*['\"]click['\"]\s*,\s*tapOutsideSidebar\s*,\s*(\w+)\s*\)", js)
    assert m, "没有注册抽屉外点击处理"
    assert m.group(1) == "true", "不是捕获阶段 —— 会被抽屉内部的 stopPropagation 吃掉"


def test_the_workspace_host_is_exempt_from_our_csp(monkeypatch):
    """CSP 落在工作台文档上 = 整页白屏。

    dsh 的打包产物用 new Function(), script-src 不带 'unsafe-eval' 会让它启动
    即死, 页面只剩我们注入的外壳按钮。2026-08-23 生产真实发生。
    dsh 在自己的子域上, 与主站不同源 —— 给它的 CSP 保护不了我们的会话。
    注意验证方式: curl -I 看不出来 (HEAD 响应无 content-type, CSP 分支不触发),
    这条测试用真实 GET。
    """
    monkeypatch.setattr(config, "WORK_DOMAIN", "work.dshcloud.online")
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app())

    main_site = c.get("/", headers={"host": "dshcloud.online"})
    assert "content-security-policy" in main_site.headers, "主站的 CSP 丢了"

    r = c.get("/work/starting", headers={"host": "work.dshcloud.online"})
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers, "work 域带了 CSP —— dsh 会启动即死, 整页白屏"


# --- 冷启动时的请求扇出 ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_burst_of_cold_requests_creates_exactly_one_instance(monkeypatch):
    """Caddy 的 forward_auth 会为页面上每个资源问一次 /api/work/route。

    冷启动时 _last_seen 是空的, 30 秒快路径拦不住, 于是整个扇出一起落进
    "没有实例 -> 建一台"。ECI 不保证同名唯一, 所以真的会建出好几台: 2026-08-24
    05:24:15/16 各一台, 都 Running、各自动创建一个 EIP、按秒双份计费, 而用户只
    看得到一台。这里把并发直接跑出来 —— 少一把锁, created 就 > 1。
    """
    state = {"created": 0, "exists": False}

    async def fake_inspect(_uid):
        await asyncio.sleep(0)  # 让另一个协程有机会插进来, 正如真实的 API 往返
        if not state["exists"]:
            return None
        return workbackend.WorkInfo(
            running=True, boot_fp="fp", image_id="img", host="172.29.0.5", state="Running"
        )

    async def fake_create(_user, _product=None):
        await asyncio.sleep(0)
        state["created"] += 1
        state["exists"] = True

    async def fake_start(_uid):
        await asyncio.sleep(0)

    async def no_running():
        await asyncio.sleep(0)
        return []

    async def not_stale(_info, _product=None):
        return False

    monkeypatch.setattr(workspace, "_inspect", fake_inspect)
    monkeypatch.setattr(workspace, "_create", fake_create)
    monkeypatch.setattr(workspace, "_start", fake_start)
    monkeypatch.setattr(workspace, "_running_workspaces", no_running)
    monkeypatch.setattr(workspace, "_boot_is_stale", lambda _info, _pid=None: False)
    monkeypatch.setattr(workspace, "_image_is_stale", not_stale)
    monkeypatch.setattr(workspace, "_capacity_reason", lambda: "")
    monkeypatch.setattr(workspace, "_ready", lambda _key, _product=None: _true())
    monkeypatch.setattr(config, "WORK_MAX_CONCURRENT", 10)
    workspace._ensure_locks.pop("u_burst", None)
    workspace._starting.pop("u_burst", None)

    user = {"id": "u_burst"}
    dsh = products.registry()[products.DEFAULT]
    await asyncio.gather(*[workspace.ensure_workspace(user, dsh) for _ in range(6)])
    assert state["created"] == 1, f"一次冷启动建了 {state['created']} 台实例 —— 每台都按秒计费并各占一个 EIP"


async def _true():
    return True


# --- 多产品工作台 --------------------------------------------------------------


def test_dsh_keeps_the_legacy_key(fake):
    """dsh 的工作台键必须**仍然是 user_id 本身**。

    线上已有一批按 user_id 命名的容器与卷 (dshwork-<hexid> / dshwork-home-<hexid>
    / dshwork-ws-<hexid>)。给 dsh 也加上产品后缀 = 那些用户的工作台和历史文件
    全部弃养: 容器成孤儿, 卷还在磁盘上但再没人引用, 而且不报错 —— 用户只会看到
    自己的东西凭空消失。
    """
    assert products.wskey("usr_abc", products.DEFAULT) == "usr_abc"
    assert products.wskey("usr_abc", "comfyui") == "usr_abc~comfyui"
    assert products.split_key("usr_abc") == ("usr_abc", products.DEFAULT)
    assert products.split_key("usr_abc~comfyui") == ("usr_abc", "comfyui")


def test_two_products_get_two_containers(fake, monkeypatch):
    """同一个人开两个产品, 必须是两台容器、两个卷、两个上游。

    键是不透明字符串, 所以整条链路 (容器名/卷名/锁/回收计时) 都靠它分身 ——
    这条用例钉的就是"它们真的没有互相顶掉"。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")

    dsh = products.registry()[products.DEFAULT]
    comfy = products.registry()["comfyui"]

    assert workspace._cname(products.wskey("usr_x", dsh.id)) != workspace._cname(
        products.wskey("usr_x", comfy.id)
    )
    # 上游端口也按产品走: dsh 是 socat 转出来的 3081, ComfyUI 直接听 8188
    assert workspace._upstream(products.wskey("usr_x", dsh.id), dsh).endswith(":3081")
    assert workspace._upstream(products.wskey("usr_x", comfy.id), comfy).endswith(":8188")


def test_comfyui_boot_persists_output_on_the_volume(monkeypatch):
    """ComfyUI 默认往 /opt/ComfyUI/output 写, 而持久化的是 /workspace。

    不改指向的话, 容器一回收用户生成的图和视频全没 —— 而且没有任何报错, 只是
    下次进来空空如也。
    """
    boot = products.boot_script("comfyui")
    assert "ln -s /workspace/output /opt/ComfyUI/output" in boot
    assert "--port 8188" in boot
    assert "--cpu" in boot, "编排模式不带 GPU"


def test_comfyui_env_carries_only_the_gateway(monkeypatch):
    """节点不认识任何一家厂商, 只认我们的网关 —— 容器里不该出现上游凭据。"""
    env = products.env_for("comfyui", "tok_123")
    assert env["DSH_CLOUD_TOKEN"] == "tok_123"
    assert env["DSH_CLOUD_VIDEO_BASE"].endswith("/llm/v1")
    joined = " ".join(env.values())
    assert "qianmian" not in joined and "api.deepseek.com" not in joined


def test_a_product_without_an_image_is_not_reachable(fake, monkeypatch):
    """没配镜像 = 该产品未启用。默认就是这个状态, 所以这次改动上线后行为不变。"""
    monkeypatch.setattr(config, "COMFY_IMAGE", "")
    assert "comfyui" not in [p.id for p in products.enabled()]


def test_machine_time_is_billed_to_the_person_not_the_workspace():
    """回收器拿到的是工作台键。直接拿它计费会记到一个不存在的用户头上 ——
    同一个人的 dsh 与 ComfyUI 花的是同一份机时额度。"""
    owner, pid = products.split_key("usr_abc~comfyui")
    assert owner == "usr_abc", "机时必须记在人头上"
    assert pid == "comfyui"


def test_login_bounces_back_to_the_product_you_came_from(fake, monkeypatch):
    """从 comfy 域被弹去登录, 登完必须回 comfy。

    写死 next=/work 的话人会落进 dsh 工作台 —— 他要的那个从没打开过, 而且
    没有任何提示说发生了什么。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    c = TestClient(app)
    r = c.get("/api/work/route", headers={"host": "comfy.test.local"}, follow_redirects=False)
    assert r.status_code == 302
    assert "next=/work?product_id=comfyui" in r.headers["location"], r.headers["location"]

    r2 = c.get("/api/work/route", headers={"host": "work.test.local"}, follow_redirects=False)
    assert r2.headers["location"].endswith("next=/work"), r2.headers["location"]


def test_starting_page_can_see_a_non_default_workspace(fake, monkeypatch):
    """启动等待页跑在**主站域**上, 只按 Host 判产品会永远查错工作台。

    2026-08-27 实测故障: 打开 comfy.dshcloud.online -> 实例确实建出来了 ->
    但页面跳到 dshcloud.online/work/starting, 那里轮询 /api/work/status 时
    Host 是主站域 -> 判成 dsh -> 查一个不存在的 dsh 工作台 -> 进度条永远停在
    「正在排队」。实例在跑、计费在走, 而用户以为坏了。

    所以 status 必须认 ?product_id=, 且跳向 starting 时必须带上它。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    c, uid = _user("starting@test.local")

    # 从 comfy 域进 —— 应当被送到带 product_id 的等待页
    r = c.get("/api/work/route", headers={"host": "comfy.test.local"}, follow_redirects=False)
    assert r.status_code in (200, 302)
    if r.status_code == 302:
        assert "product_id=comfyui" in r.headers["location"], r.headers["location"]

    # 等待页在主站域上问状态: 不带 product_id 看到的是 dsh, 带上才是 comfy
    as_dsh = c.get("/api/work/status").json()
    as_comfy = c.get("/api/work/status?product_id=comfyui").json()
    # 协议跟 PUBLIC_BASE 走 (测试环境是 http), 这里只认域名
    assert "//comfy.test.local" in as_comfy["url"], as_comfy["url"]
    assert "//comfy.test.local" not in as_dsh["url"], as_dsh["url"]


def _reap(monkeypatch, key, *, last_ago, started_ago, product_id="dsh"):
    """把回收器的输入摆成指定的样子, 跑一轮, 返回是否被回收。"""
    now = time.time()
    workspace._last_seen[key] = now - last_ago
    workspace._started_at[key] = now - started_ago
    workspace._user_active.pop(key, None)

    stopped = []

    async def running():
        return [key]

    async def stop(k):
        stopped.append(k)

    monkeypatch.setattr(workspace, "_running_workspaces", running)
    monkeypatch.setattr(workspace, "_stop", stop)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(workspace.reaper_tick(now))
    return bool(stopped)


def test_a_workspace_in_active_use_is_not_reaped(fake, monkeypatch):
    """ComfyUI 没有 /api/work/active 上报器, 也不经网关跑模型。

    2026-08-27 实测事故: 老板正在 ComfyUI 里操作, 容器起来 101 秒后被回收,
    日志只写一句 (idle)。根因是 present 退化成「容器启动时间」、quiet 恒为真 ——
    于是**只要容器活过 WORK_IDLE_STOP_MIN, 不管人在不在用, 必被杀**。

    页面还开着就一定有 /api/work/route 流量 (Caddy 为每个资源打一次), 所以
    对没有上报器的产品, 流量就是在场。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_TAB_GONE_MIN", 3)

    key = products.wskey("u_reap", "comfyui")
    # 容器活了 30 分钟 (远超 IDLE_STOP), 但 10 秒前还有流量 = 人就在用
    assert not _reap(monkeypatch, key, last_ago=10, started_ago=1800), "有流量还被回收 = 把正在用的人踢了"


def test_a_closed_tab_is_still_reaped(fake, monkeypatch):
    """流量当在场的代价必须有边界: 标签页真关了, 流量就断, 该收还得收。

    生效的窗口是 WORK_AGENT_IDLE_STOP_MIN 而不是 WORK_TAB_GONE_MIN —— 流量同时
    喂给 present 与 agent 两个信号, 回收要求两者都超时。对 ComfyUI 这是**想要**
    的: 一条视频要跑好几分钟, 关掉标签页不该把它掐掉。"""
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_TAB_GONE_MIN", 3)

    key = products.wskey("u_reap2", "comfyui")
    # 5 分钟无流量: 标签页确实关了, 但还在 AGENT_IDLE 窗口内 —— 故意不收
    assert not _reap(monkeypatch, key, last_ago=300, started_ago=1800), "跑到一半的活儿不该被掐"
    # 11 分钟无流量: 两个窗口都过了, 必须收掉, 否则一直烧机时
    assert _reap(monkeypatch, key, last_ago=660, started_ago=1800), "标签页关了还不收, 会一直烧机时"


def test_opening_one_workspace_does_not_kill_the_others_credential(monkeypatch):
    """凭据按**工作台**隔离, 不按人。

    撤销条件原来只有 user_id + platform='cloud' —— 那是"一个人只有一个工作台"
    时代的写法。加了 ComfyUI 之后: 开第二个工作台会把第一个容器里的令牌撤掉,
    而那个容器还在跑、界面照常, 只是**往网关发的每一发都 401**, 没有任何提示。
    2026-08-28 线上就是这么坏的 —— 12:51 起 ComfyUI, 13:03 起 dsh, ComfyUI 从此
    取不到在售清单也生成不了任何东西, 用户只看到"执行失败"。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    _, uid = _user("two-workspaces@test.local")
    user = db.query_one("SELECT id, session_epoch FROM users WHERE id=?", (uid,))
    reg = products.registry()

    workspace._mint_workspace_token(dict(user), reg["comfyui"])
    workspace._mint_workspace_token(dict(user), reg[products.DEFAULT])

    live = db.query(
        "SELECT workspace FROM devices WHERE user_id=? AND platform='cloud' AND revoked=0", (uid,)
    )
    keys = sorted(r["workspace"] for r in live)
    assert keys == sorted([products.wskey(uid, "comfyui"), products.wskey(uid)]), (
        f"开第二个工作台把第一个的凭据撤了: {keys}"
    )

    # 同一个工作台再铸一次, 旧的**必须**失效 —— 顶替语义不能因为分了产品就丢掉
    workspace._mint_workspace_token(dict(user), reg["comfyui"])
    comfy_live = db.query(
        "SELECT id FROM devices WHERE user_id=? AND platform='cloud' AND workspace=? AND revoked=0",
        (uid, products.wskey(uid, "comfyui")),
    )
    assert len(comfy_live) == 1, f"同一工作台重铸后应只剩一份有效凭据: {len(comfy_live)}"


def test_legacy_credentials_are_adopted_by_dsh_not_left_unrevocable(monkeypatch):
    """迁移前铸的凭据没有 workspace —— 不认领的话新逻辑永远撤不到它们, 那就成了
    系统再也收不回的长期凭据。归属到 dsh (ComfyUI 是 2026-08 才有的)。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    _, uid = _user("legacy-cred@test.local")
    user = db.query_one("SELECT id, session_epoch FROM users WHERE id=?", (uid,))
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO devices (id, user_id, name, platform, workspace, token_hash, epoch, "
            "last_seen, created) VALUES (?,?,?,?,?,?,?,?,?)",
            ("dev_legacy", uid, "云工作台", "cloud", "", "hash_legacy", 0, now, now),
        )
    reg = products.registry()
    # 给 ComfyUI 铸币不该误伤历史行 —— 但要把它认领给 dsh
    workspace._mint_workspace_token(dict(user), reg["comfyui"])
    row = db.query_one("SELECT workspace, revoked FROM devices WHERE id='dev_legacy'")
    assert row["workspace"] == products.wskey(uid), "历史凭据没被认领, 将永远撤不掉"
    assert not row["revoked"], "给 ComfyUI 铸币误伤了历史凭据"
    # 给 dsh 铸币则应正常顶替掉它
    workspace._mint_workspace_token(dict(user), reg[products.DEFAULT])
    assert db.query_one("SELECT revoked FROM devices WHERE id='dev_legacy'")["revoked"]


def test_tab_grace_is_per_product_not_global(fake, monkeypatch):
    """宽限期一刀切会替 dsh 用户白烧机时。

    "关掉标签页后多留一会儿"划不划算, 取决于**冷启动有多贵**: ComfyUI 实测约
    26 秒 (ECI 调度 16s + 建 EIP 4.8s + ComfyUI 启动 5.5s), 关一次页再回来就要
    重等一遍, 所以宁可多留几分钟。dsh 没这个包袱 —— 把它也拉到 10 分钟, 等于
    每次关页都多收用户 7 分钟机时, 而他并没有因此少等什么。
    """
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "WORK_IDLE_STOP_MIN", 10)
    monkeypatch.setattr(config, "WORK_AGENT_IDLE_STOP_MIN", 3)  # 让宽限期成为决定项
    monkeypatch.setattr(config, "WORK_TAB_GONE_MIN", 3)  # 全局
    monkeypatch.setattr(config, "COMFY_TAB_GRACE_MIN", 10)  # ComfyUI 单列

    comfy = products.wskey("u_grace", "comfyui")
    # 5 分钟无流量: 全局宽限期早过了, 但 ComfyUI 的是 10 分钟 —— 不该收
    assert not _reap(monkeypatch, comfy, last_ago=300, started_ago=1800), (
        "ComfyUI 用了全局的 3 分钟宽限期 —— 关页 5 分钟回来又要重等 26 秒"
    )
    # 11 分钟: 自己的窗口也过了, 必须收
    assert _reap(monkeypatch, comfy, last_ago=660, started_ago=1800), "过了自己的宽限期还不收"

    # dsh 保持 3 分钟 —— 没跟着一起放宽
    assert _reap(monkeypatch, "u_grace", last_ago=300, started_ago=1800), (
        "dsh 跟着放宽到 10 分钟了 —— 那是在替它的用户白烧 7 分钟机时"
    )


def test_machine_time_and_overdraft_are_read_off_the_person(fake, monkeypatch):
    """uid 是工作台键。拿它查余额永远得 0 —— 欠费用户的 ComfyUI 永远收不掉。"""
    c, uid = _user("overdraft@test.local")
    key = products.wskey(uid, "comfyui")
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "OVERDRAFT_LIMIT_CREDITS", 20)
    db.query("DELETE FROM credit_grants WHERE user_id=?", (uid,))
    credits.spend(uid, 100, kind="llm", model="x")  # 欠到 -100
    assert credits.balance(uid) <= -20
    assert credits.balance(key) == 0, "前提: 工作台键不是用户, 查不到余额"
    # 有流量也要收 —— 欠费优先于在场
    assert _reap(monkeypatch, key, last_ago=5, started_ago=60), "欠费的工作台必须回收"


def test_preset_workflows_update_only_when_untouched():
    """发出去的工作流要能更新到已有用户, 但不能抹掉用户自己的改动。

    2026-08-27 实测: 生视频节点改成返回 VIDEO、预置图接上了 SaveVideo, 而老板
    工作台里仍是那张孤零零的旧图 —— 启动脚本当时是「只补缺的」, 于是任何更新都
    到不了已有用户。反过来无条件覆盖又会每次冷启动抹掉用户的改动。

    做法: 留一份「我们发的是什么」在 .shipped/, 磁盘上那份与它逐字节相同才换新。
    """
    boot = products.boot_script("comfyui")
    assert "/.shipped/" in boot, "没有留发货标记就无法判断用户改没改过"
    assert "cmp -s" in boot, "必须逐字节比对, 不能只看时间戳"
    # 三个分支缺一不可: 没有本地文件 / 没有标记(迁移) / 与标记相同
    assert '[ ! -e "$live" ]' in boot
    assert '[ ! -e "$mark" ]' in boot, "已有用户没有标记, 缺这条就永远收不到更新"
    assert 'cp "$f" "$mark"' in boot, "更新后必须同步标记, 否则下次又判成被改过"


def test_open_design_product_spec(monkeypatch):
    """Open Design: 单容器, 里面跑 dsh。三处一错就整个产品是死的:

    boot 必须装 profile + 打软链 (loader 从 dsh 自己的 node_modules 解析包名,
    镜像文件系统每次全新, 软链一次性; 漏了 = agent 报 profile incompatible);
    env 必须带网关凭据 (漏了 = agent 在但每次调用 401); 数据目录必须软链到
    /workspace (漏了 = 用户的项目随实例回收消失, 不报错)。
    """
    monkeypatch.setattr(config, "OPEN_DESIGN_DOMAIN", "od.test.local")
    monkeypatch.setattr(config, "OPEN_DESIGN_IMAGE_REF", "ghcr.io/x/od-local:t")
    prod = products.registry()["open-design"]
    assert prod.id in [p.id for p in products.enabled()]
    assert prod.port == 7456
    assert prod.sidecars == (), "它是单容器, 不是栈"

    boot = products.boot_script("open-design")
    assert "dsh plugin --profile open-design add /opt/od-profile.tgz" in boot
    assert "ln -sfn /root/.dsh/profiles/open-design/node_modules/@open-design" in boot
    assert "ln -s /workspace/.od /app/.od" in boot
    assert "exec node apps/daemon/dist/cli.js" in boot
    # 预置应用偏好 —— 漏了就有第二道 (上游的) 登录墙, 见下一个测试
    assert "app-config.json" in boot
    assert "onboardingCompleted" in boot
    assert "deepseek-harness" in boot

    env = products.env_for("open-design", "tok_x")
    assert env["DEEPSEEK_API_KEY"] == "tok_x"
    assert env["DEEPSEEK_BASE_URL"].endswith("/llm/v1")
    assert env["OD_DISABLE_API_AUTH"] == "1"
    assert env["OD_ALLOWED_ORIGINS"] == "https://od.test.local"


def _openclaw_ready(monkeypatch):
    monkeypatch.setattr(config, "OPENCLAW_DOMAIN", "claw.test.local")
    monkeypatch.setattr(config, "WORK_PROXY_CIDR", "10.1.2.3/32")
    return products.registry()["openclaw"]


def test_openclaw_state_lands_on_nas_and_runs_as_root(monkeypatch):
    """状态目录必须落在 NAS 上, 而且要以 root 跑才写得进去。

    两个都是"错了也不报错"的:
      · 镜像默认把状态写 /home/node —— 那是容器内的盘, 实例一回收用户的会话、
        频道、记忆全没了, 而过程中一句错都没有。
      · 镜像里 USER 是 node, 而 NAS 挂进来的目录是 root 的。不改 uid 的话它只
        在日志里抱怨一句数据库打不开, 照常起来, 东西照样落在容器内。
    """
    prod = _openclaw_ready(monkeypatch)
    assert prod.run_as_user == 0, "以 node 跑 -> 写不进 NAS -> 数据随回收消失"
    env = products.env_for("openclaw", "tok_x")
    assert env["OPENCLAW_STATE_DIR"].startswith("/workspace/")
    assert env["OPENCLAW_CONFIG_PATH"].startswith("/workspace/")
    assert env["DSH_CLOUD_TOKEN"] == "tok_x"


def test_openclaw_patches_config_instead_of_overwriting(monkeypatch):
    """只能 patch, 不能整份重写。

    openclaw.json 里还装着**用户自己接的频道** (Telegram/Discord/Slack)。每次
    启动重写一遍等于把他配的东西全抹掉 —— 而且是静默的, 他只会发现机器人不回
    消息了。`config patch` 是递归合并的。
    """
    _openclaw_ready(monkeypatch)
    boot = products.boot_script("openclaw")
    assert "config patch --stdin" in boot
    assert "cat >" not in boot, "又变成整份覆盖了 —— 会抹掉用户接的频道"
    # 令牌靠**不带引号**的 heredoc 展开; 加了引号就会把字面量写进配置
    assert "<<PATCH" in boot and "<<'PATCH'" not in boot
    assert "$DSH_CLOUD_TOKEN" in boot


def test_openclaw_auth_is_trusted_proxy_not_open(monkeypatch):
    """鉴权走 trusted-proxy, 且身份头只认我们的反代来源。

    OpenClaw 明确拒绝"监听 LAN + 无鉴权" (实测 Refusing to bind gateway to lan
    without auth), 所以关掉鉴权这条路本来就走不通; 而 trusted-proxy 正是为
    "边缘已经鉴过权"设计的。来源放宽等于让任何能连到容器的人自称是任意用户。
    """
    _openclaw_ready(monkeypatch)
    boot = products.boot_script("openclaw")
    assert '"mode": "trusted-proxy"' in boot
    assert f'"userHeader": "{products.PROXY_USER_HEADER}"' in boot
    assert (
        '"trustedProxies": [\n        "10.1.2.3/32"\n      ]' in boot.replace("\r", "")
        or "10.1.2.3/32" in boot
    )
    assert '"mode": "none"' not in boot


def test_openclaw_allows_its_own_origin(monkeypatch):
    """控制台 UI 的来源必须列全。

    少了它页面照样打得开, 但一连 WebSocket 就被网关按来源拒掉 —— 页面上显示
    "浏览器来源不被允许", 并退回一个要你填 WebSocket URL / 令牌 / 密码的连接
    表单, 看着就像第二道登录墙。不支持通配符, 只能按域名拼。
    """
    _openclaw_ready(monkeypatch)
    boot = products.boot_script("openclaw")
    assert '"https://claw.test.local"' in boot
    assert "allowedOrigins" in boot
    # 设备配对那道也得关: 它会让用户去主机上跑 `openclaw devices approve <id>`,
    # 而他既没有主机也不该有 —— 那是第三道墙, 长得同样不像登录墙。
    assert '"dangerouslydisabledeviceauth": true' in boot.lower()
    assert '"allowinsecureauth": true' in boot.lower()


def test_openclaw_hidden_until_the_proxy_cidr_is_configured(monkeypatch):
    """没配反代来源就别出现在目录里。

    退回一个宽松的默认值等于让身份头可以被任何人伪造 —— 宁可这个产品先不上。
    """
    monkeypatch.setattr(config, "OPENCLAW_DOMAIN", "claw.test.local")
    monkeypatch.setattr(config, "WORK_PROXY_CIDR", "")
    assert "openclaw" not in [p.id for p in products.enabled()]


def test_hermes_binds_loopback_so_there_is_no_second_login_wall(monkeypatch):
    """Hermes 绑**回环**, 由同组的 nginx 做那条隧道。

    它自己的规矩: 非回环绑定强制要鉴权 (`--insecure` 从 2026-06 起是 no-op),
    而它只有表单密码和 OAuth 两种 —— 都是第二道登录墙。文档给的建议正是
    "绑 127.0.0.1 + 隧道", 而容器组共享网络命名空间, 主容器那个 nginx 就是隧道。
    所以这不是绕开它的安全控制, 是按它推荐的姿势部署。
    """
    monkeypatch.setattr(config, "HERMES_DOMAIN", "hermes.test.local")
    prod = products.registry()["hermes"]
    assert prod.id in [p.id for p in products.enabled()]
    hm = next(sc for sc in prod.sidecars if sc.name == "hermes")
    assert "--host" in hm.args and "127.0.0.1" in hm.args, "绑了非回环就会冒出登录墙"
    assert "0.0.0.0" not in hm.args
    # 主容器是 nginx, 代理到同组的回环端口
    assert f"proxy_pass http://127.0.0.1:{products.HERMES_PORT}" in products.boot_script("hermes")
    # 它有 Host 白名单, 只认**绑定的**主机名; 送真实域名过去就是每一发 400
    # `Invalid Host header`, 而容器全 Running、日志还写着 READY, 看着一切正常。
    # 而配 public_url 那条路它要求必须有鉴权提供方 (否则直接拒绝启动) = 登录墙。
    boot = products.boot_script("hermes")
    assert f"proxy_set_header Host 127.0.0.1:{products.HERMES_PORT};" in boot
    assert "HERMES_DASHBOARD_PUBLIC_URL" not in str(hm.env), "配了它就必须带鉴权 = 第二道墙"
    # 免登录: cookie 值里带双引号 (rt="eyJ..."), 不转义就是 nginx 语法错 ->
    # reload 静默失败 -> 配置根本没换。而且要先验再 reload, 否则报了成功也没用。
    sh = products._HERMES_AUTOLOGIN
    assert r"sed 's/\"/\\\\\"/g'" in sh or 's/"/' in sh, "cookie 里的双引号没转义"
    assert "nginx -t" in sh, "不先验就 reload, 失败了也不知道"
    assert "reload 失败" in sh


def test_hermes_keeps_its_entrypoint(monkeypatch):
    """只传 args, 不覆盖 entrypoint。

    它的 entrypoint 是 s6 监督树。顶掉之后脚本自己会在 stderr 抱怨一句
    "supervised services are unavailable" 然后**照常跑** —— 进程在、端口不在,
    表现是"起来了但什么都不工作"。
    """
    monkeypatch.setattr(config, "HERMES_DOMAIN", "hermes.test.local")
    hm = next(sc for sc in products.registry()["hermes"].sidecars if sc.name == "hermes")
    assert hm.cmd == (), "覆盖了 entrypoint -> s6 监督树不起 -> 服务全无"
    assert hm.args[0] == "dashboard"


def test_hermes_model_is_written_before_it_starts(monkeypatch):
    """模型配置由初始化容器写, 而且用 `config set` 不整份重写。

    config.yaml 里还有用户自己调的东西 (人格、技能、渠道), 重写等于抹掉。
    令牌占位符必须在这里被换掉 —— 漏了会把字面量写进配置, 文件看着好好的,
    一发消息就 401。
    """
    monkeypatch.setattr(config, "HERMES_DOMAIN", "hermes.test.local")
    ics = products.registry()["hermes"].init_containers
    assert len(ics) == 1
    cmd = ics[0].cmd[-1]
    assert "hermes config set model.base_url" in cmd
    assert "hermes config set model.api_key" in cmd
    assert "cat >" not in cmd and "config.yaml" not in cmd, "整份重写会抹掉用户自己的配置"
    assert ("hermes/data", "/opt/data") in ics[0].mounts, "配置没写到 NAS 上 = 重建即失"
    assert products.GATEWAY_TOKEN_PLACEHOLDER in cmd
    done = products.resolve_init_containers(ics, "s" * 64, "tok_live")
    assert "tok_live" in done[0].cmd[-1] and "__DSH_" not in done[0].cmd[-1]


def test_open_design_points_dsh_at_our_gateway_not_the_deepseek_adapter():
    """Open Design 里的 dsh 必须走 pi-ai (openai-completions), 不是 llm-deepseek。

    `~/.dsh/settings.yaml` 对**命名 profile 不生效** —— 那是 web profile 的用户层。
    不给 profile 的 cordis.patch.yml 写配置的话, agent-default-model 解析出来是
    `provider: deepseek-official`, 即 llm-deepseek 适配器; 它对着我们这个说标准
    OpenAI 流式的网关, 会把工具调用拼成空的工具名 —— 智能体跑一半就结束、
    **不给终态**, 用户看到 DSH_PROFILE_MISSING_RESULT。
    而每一层自己看都正常: 令牌是对的、网关是通的、probe 也握手成功, 所以只能靠
    测试钉住。2026-08-30 线上撞到过。
    """
    boot = products.boot_script("open-design")
    assert "cordis.patch.yml" in boot, "没给 profile 写用户层配置"
    patch = products._opendesign_patch_yaml()
    assert "api: openai-completions" in patch, "又掉回 llm-deepseek 适配器了"
    assert "apiKeyEnv: DSH_CLOUD_TOKEN" in patch
    assert "provider: dshcloud" in patch, "默认模型没指向我们的 provider"
    assert f"model: {model_catalog.default_model()}" in patch
    # patch 层是 YAML 数组, 两个条目各带 id —— 写成映射的话 dsh 读不出来
    assert patch.startswith("- id: llm-pi-ai")
    assert "\n- id: agent-default-model" in patch


def test_open_design_and_dsh_share_one_provider_definition():
    """两个工作台的 provider 定义必须同源。

    各写一份的话, 目录里加个模型只会在一边生效 —— 表现是"某个工作台里选不到
    新模型", 不报错也不好查。
    """
    ids = [m["id"] for m in model_catalog.catalog().values()]
    dsh_boot = products.boot_script("dsh")
    od_patch = products._opendesign_patch_yaml()
    for mid in ids:
        assert f"- id: {mid}" in dsh_boot, f"dsh 工作台少了 {mid}"
        assert f"- id: {mid}" in od_patch, f"Open Design 少了 {mid}"
    assert len(ids) > 1


def test_open_design_app_config_seed_kills_upstream_login_wall(tmp_path):
    """预置的那段 JS 真跑一遍: 三种起始状态各该落到哪。

    没有它, 用户在**我们的**登录墙之后又撞上 OpenDesign 自己的 onboarding
    向导 —— 停在"登录 OpenDesign", 点下去报 `vela binary not found`, 因为默认
    选中的 agent 是它自家的 amr(vela), 而镜像里只有 dsh。用户已经登过一次了,
    不该再登第三方账号, 何况那条路根本走不通。2026-08-29 线上被用户逮到。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node, 跑不了这段预置脚本")

    js = products._OD_APP_CONFIG_JS
    # 断言守卫本身在场: 没有 node 的环境至少还能挡住"把条件删了"这种改动
    assert "c.agentId==='amr'" in js, "卡在 amr 的老用户救不回来"
    assert "if(!c.telemetry)" in js, "会把用户自己的遥测选择按回去"

    def run(initial: dict | None) -> dict:
        d = tmp_path / "od"
        (d / ".od").mkdir(parents=True, exist_ok=True)
        cfg = d / ".od" / "app-config.json"
        if initial is not None:
            cfg.write_text(json.dumps(initial))
        elif cfg.exists():
            cfg.unlink()
        subprocess.run(
            [node, "-e", js.replace("/app/.od/app-config.json", str(cfg))],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return json.loads(cfg.read_text())

    fresh = run(None)
    assert fresh["onboardingCompleted"] is True, "向导还会弹"
    assert fresh["agentId"] == "deepseek-harness", "选中的 agent 不是镜像里唯一可用的那个"
    assert fresh["telemetry"] == {"metrics": False, "content": False}, (
        "上游默认把设计内容也发给第三方; 我们是托管方, 该替用户默认关掉"
    )

    stuck = run({"onboardingCompleted": True, "agentId": "amr"})
    assert stuck["agentId"] == "deepseek-harness", "在向导里点过一次的用户仍卡在登录墙"

    chosen = run(
        {"onboardingCompleted": True, "agentId": "claude", "telemetry": {"metrics": True, "content": True}}
    )
    assert chosen["agentId"] == "claude", "把用户自己选的 agent 按回去了"
    assert chosen["telemetry"] == {"metrics": True, "content": True}, "把用户的遥测选择按回去了"


def test_open_design_disabled_without_domain_or_image(monkeypatch):
    monkeypatch.setattr(config, "OPEN_DESIGN_DOMAIN", "")
    assert "open-design" not in [p.id for p in products.enabled()]


def _coze_ready(monkeypatch):
    monkeypatch.setattr(config, "COZE_DOMAIN", "coze.test.local")
    monkeypatch.setattr(config, "COZE_ASSETS_IMAGE_REF", "ghcr.io/x/coze-assets:0.5.1-r1")
    return products.registry()["coze"]


def test_coze_stack_shape(monkeypatch):
    """10 个容器 + 1 个初始化容器, 主容器是上游的 nginx 前端。

    容器数是有上限的 (ECI 容器组最多 20), 而漏掉一个中间件的症状散在别处:
    没有 nsqd 就是工作流不执行、没有 milvus 就是知识库建不了 —— 都不会说
    "少了个容器"。
    """
    prod = _coze_ready(monkeypatch)
    assert prod.id in [p.id for p in products.enabled()]
    assert prod.port == 80
    names = [sc.name for sc in prod.sidecars]
    assert names == [
        "coze-server",
        "mysql",
        "redis",
        "elasticsearch",
        "minio",
        "etcd",
        "milvus",
        "nsqlookupd",
        "nsqd",
    ]
    assert 1 + len(prod.sidecars) <= 20, "ECI 容器组最多 20 个容器"
    assert len(prod.init_containers) == 1
    assert prod.init_containers[0].image_ref == "ghcr.io/x/coze-assets:0.5.1-r1"
    # 主容器要拿到 nginx 配置, coze-server 要拿到后端配置目录
    assert prod.seeds == (("nginx", "/seed"),)
    server = next(sc for sc in prod.sidecars if sc.name == "coze-server")
    assert ("conf", "/app/resources/conf") in server.seeds


def test_coze_minio_endpoint_must_stay_the_service_name(monkeypatch):
    """MINIO_ENDPOINT 必须是 `minio:9000`, 不能"顺手"改成回环。

    上游 nginx 用 `sub_filter 'minio:9000' '$http_host/local_storage'` 把后端
    返回的对象存储直链改写成同源路径。写成 127.0.0.1:9000 的话改写不匹配 ——
    **页面上的图片和附件全部打不开**, 而每个容器自己看都正常, 日志里一个错
    都没有。这是这一栈里最容易被"统一成回环"清理掉的一行。
    """
    prod = _coze_ready(monkeypatch)
    env = dict(next(sc for sc in prod.sidecars if sc.name == "coze-server").env)
    assert env["MINIO_ENDPOINT"] == "minio:9000"
    assert env["MINIO_API_HOST"] == "http://minio:9000"
    # 而 host_aliases 得把这个名字指回环, 否则连都连不上
    assert "minio" in prod.host_aliases
    assert "coze-server" in prod.host_aliases, "nginx 的 proxy_pass 认这个名字"


def test_coze_boot_rewrites_object_storage_links_to_https(monkeypatch):
    """对象存储直链必须被改写成 https。

    后端 presign 出来的是 `http://minio:9000/...` —— scheme 取自 MINIO_USE_SSL,
    而那个必须是 false (服务端走回环明文连 minio)。STORAGE_UPLOAD_HTTP_SCHEME
    管不到这里, 它只写进上传令牌的 HostScheme。上游自己的部署是纯 http 的所以
    碰不到; 我们的站点在 https 上, 页面里出现 http:// 的图片就是**混合内容**,
    浏览器直接拦 —— 头像和附件一片空白, 而服务端一切正常、日志里一个错都没有。
    2026-08-29 上线当天实测到: 头像 URL 是 http://coze.dshcloud.online/...,
    换成 https 同一个地址就是 200 + 2366 字节。
    """
    _coze_ready(monkeypatch)
    boot = products.boot_script("coze")
    # 上游那份配置照抄进来 (它有 sub_filter 与剥离签名参数的 rewrite, 手抄必错)
    assert "cp /seed/conf.d/default.conf /etc/nginx/conf.d/default.conf" in boot
    # 唯一的改动: 直链改写带上 https
    assert products._COZE_SUBFILTER_TO in boot
    assert "https://" in products._COZE_SUBFILTER_TO
    assert "http://minio:9000" in products._COZE_SUBFILTER_TO
    # sed 的被替换串必须是上游的原样 —— 对不上就是**静默失效**, 什么都不会报。
    # deploy/workspace-coze/build.sh 在构建期断言上游那行还长这样。
    assert products._COZE_SUBFILTER_FROM.startswith("sub_filter 'minio:9000'")
    assert f"s#{products._COZE_SUBFILTER_FROM}#{products._COZE_SUBFILTER_TO}#" in boot


def test_coze_has_no_second_login_wall(monkeypatch):
    """平台的通则: **只有我们这一层登录墙**。

    用户进到这个域已经过了 forward_auth, 容器是他一个人的; 再让他注册一个
    Coze 账号既多余又走不通 —— 密码是工作台随机生成的, 他根本不知道。
    而 Coze 那边没有任何免登开关 (SessionAuthMW 只认 session_key cookie ->
    ValidateSession), 所以只能由工作台自己登一次, 把会话注入到上游请求里。
    2026-08-30 老板在 Coze 上点名要求。
    """
    _coze_ready(monkeypatch)
    boot = products.boot_script("coze")
    # 先落一份透传默认值: 会话要等 coze-server 起来才拿得到, 而 nginx 现在就要
    # 能起 —— 引用未定义的变量会让 nginx 直接启动失败, 那连静态页都没有了。
    assert "map $cookie_session_key $dsh_cookie { default $http_cookie; }" in boot
    assert "proxy_set_header Cookie $dsh_cookie;" in boot
    assert "/usr/local/bin/dsh-coze-autologin" in boot

    sh = products._COZE_AUTOLOGIN
    # 地址是拼出来的 ($API/login/), 所以分开认
    assert "http://127.0.0.1:8888/api/passport/web/email" in sh
    assert '"$API/login/"' in sh
    assert '"$API/register/v2/"' in sh
    # 密码落在 NAS 上 —— 实例重建后还是同一个账号, 里面的智能体和知识库都还在
    assert "/root/.coze-autologin" in sh
    # 账号不能用 admin@: 那个可能已被人工建过而密码不在我们手里, 注册与登录会
    # 双双失败, 而且**不报错**, 只是又看到登录墙
    assert "owner@dshcloud.online" in sh
    assert "admin@" not in sh
    # 浏览器自己带了 session_key 就原样透传 (他想切账号也切得了)。
    # 脚本里这些 $ 是给 nginx 的, 对 shell 转义过, 所以认转义后的形态。
    assert "default \\$http_cookie;" in sh
    assert "map \\$cookie_session_key \\$dsh_cookie {" in sh


def test_coze_elasticsearch_init_gets_an_explicit_address(monkeypatch):
    """setup_es.sh 必须显式收到 --es-address。

    它自己**没有默认值**: 指望 compose 的 env_file 给每个容器都塞一份 .env
    (里面有 ES_ADDR)。我们只给 coze-server 发了这个变量, 所以在 ES 容器里
    ES_ADDR 是空串 —— 它拿空地址探测 60 次全失败, 然后打印
    "smartcn plugin not loaded correctly", **报错完全指错方向** (插件其实装好了)。
    真正的后果是索引一个都没建, 用户一进工作区就是 500:
    `no such index [project_draft]`。2026-08-30 开 UI 才看到, 只测 API 测不出来。
    """
    _coze_ready(monkeypatch)
    es = next(sc for sc in products.registry()["coze"].sidecars if sc.name == "elasticsearch")
    cmd = es.cmd[-1]
    assert "--es-address http://127.0.0.1:9200" in cmd, "空地址会让索引静默建不出来"
    assert "--docker-host false" in cmd, "别再绕一次 localhost -> elasticsearch 的名字改写"
    assert "--index-dir /seed/elasticsearch/es_index_schema" in cmd


def test_coze_wires_our_gateway_so_users_need_no_api_key(monkeypatch):
    """开箱就有模型可选: 模型 key 走网关凭据占位符, create 时换成该用户的令牌。

    留着占位符没换 = 用户一发消息就是 401, 而错误显示在 Coze 的模型调用里,
    看不出是我们没替换。
    """
    prod = _coze_ready(monkeypatch)
    raw = dict(next(sc for sc in prod.sidecars if sc.name == "coze-server").env)
    assert raw["MODEL_API_KEY_0"] == products.GATEWAY_TOKEN_PLACEHOLDER
    assert raw["MODEL_BASE_URL_0"].endswith("/llm/v1")
    assert raw["BUILTIN_CM_TYPE"] == "openai"

    resolved = products.resolve_sidecars(prod.sidecars, "s" * 64, "tok_live")
    env = dict(next(sc for sc in resolved if sc.name == "coze-server").env)
    assert env["MODEL_API_KEY_0"] == "tok_live"
    assert env["BUILTIN_CM_OPENAI_API_KEY"] == "tok_live"
    # 一个占位符都不许漏 —— 漏了是运行期 401, 不是启动期报错
    leftovers = [(sc.name, k) for sc in resolved for k, v in sc.env if "__DSH_" in v]
    assert leftovers == [], f"没换掉的占位符: {leftovers}"


def test_coze_knowledge_base_embeds_through_our_gateway(monkeypatch):
    """知识库的向量化也走我们的网关, 用户不用再去第三方申请一把 key。

    三个值是**耦合**的, 而错配一个都不会在启动期报错:
      - MODEL 必须在网关的向量化目录里, 否则每次向量化都 404, 错误显示在
        Coze 的"知识库处理失败"里, 看不出是模型名的事;
      - DIMS 必须是该模型的原生维度 —— Coze 拿它建向量集合, 对不上要等到写
        Milvus 那一刻才炸;
      - REQUEST_DIMS 为真时每次都带 dimensions, 而有的型号上游拒收 (400)。
    """
    from app import model_catalog

    prod = _coze_ready(monkeypatch)
    raw = dict(next(sc for sc in prod.sidecars if sc.name == "coze-server").env)
    assert raw["EMBEDDING_TYPE"] == "openai"
    assert raw["OPENAI_EMBEDDING_BASE_URL"].endswith("/llm/v1")
    assert raw["OPENAI_EMBEDDING_API_KEY"] == products.GATEWAY_TOKEN_PLACEHOLDER

    entry = model_catalog.resolve_embedding(raw["OPENAI_EMBEDDING_MODEL"])
    assert entry is not None, "配给 Coze 的向量化模型不在网关目录里"
    assert raw["OPENAI_EMBEDDING_DIMS"] == str(entry["dimensions"])
    assert raw["OPENAI_EMBEDDING_REQUEST_DIMS"] == ("true" if entry["supports_dimensions"] else "false")


def test_coze_never_asks_for_dimensions_a_model_refuses(monkeypatch):
    """REQUEST_DIMS 得**跟着模型算**, 不能写死。

    当前默认型号恰好接受 dimensions, 所以写死成 "true" 上面那条也照样绿 ——
    只有拿一个拒收的型号当默认才验得出来。换默认模型是一行配置的事, 而错了的
    表现是知识库整个不能用, 启动期一声不吭。
    """
    from app import model_catalog

    picky = next(
        (m for m in model_catalog.embedding_catalog().values() if not m["supports_dimensions"]), None
    )
    if picky is None:
        pytest.skip("目录里当前没有拒收 dimensions 的型号")
    monkeypatch.setattr(model_catalog, "default_embedding_model", lambda: picky["id"])
    env = dict(products._coze_embedding_env("https://example.test"))
    assert env["OPENAI_EMBEDDING_MODEL"] == picky["id"]
    assert env["OPENAI_EMBEDDING_DIMS"] == str(picky["dimensions"])
    assert env["OPENAI_EMBEDDING_REQUEST_DIMS"] == "false"


def test_coze_dims_follow_the_model_not_a_constant(monkeypatch):
    """DIMS 也得跟着模型算。上面那条钉不住它: 当前默认恰好是 1024 维, 写死成
    1024 照样绿 —— 换个维度不同的型号才验得出来。填错的后果是 Coze 按 1024 建
    好集合, 写入 2560 维的向量时才炸, 报错说的是"写入"。"""
    from app import model_catalog

    current = model_catalog.resolve_embedding(model_catalog.default_embedding_model())
    other = next(
        (m for m in model_catalog.embedding_catalog().values() if m["dimensions"] != current["dimensions"]),
        None,
    )
    if other is None:
        pytest.skip("目录里所有型号维度都一样")
    monkeypatch.setattr(model_catalog, "default_embedding_model", lambda: other["id"])
    env = dict(products._coze_embedding_env("https://example.test"))
    assert env["OPENAI_EMBEDDING_DIMS"] == str(other["dimensions"])


def test_coze_keeps_the_upstream_default_when_nothing_is_offered(monkeypatch):
    """自建部署带着旧的 models.json 时目录是空的 —— 那就退回上游开箱的 ark,
    而不是把 OPENAI_EMBEDDING_MODEL 配成空串 (那要到运行期才炸)。"""
    from app import model_catalog

    monkeypatch.setattr(model_catalog, "default_embedding_model", lambda: "")
    assert dict(products._coze_embedding_env("https://example.test")) == {"EMBEDDING_TYPE": "ark"}


def test_coze_plugin_aes_keys_are_16_bytes_and_per_user(monkeypatch):
    """插件 OAuth 令牌的 AES 密钥: 必须正好 16 字节, 且按用户走。

    长度不对时 Coze 报的错跟密钥无关, 排查方向完全指错; 而所有用户共用一把
    的话, 加密的是**落库的用户数据** —— 那就等于没加密。
    """
    prod = _coze_ready(monkeypatch)
    a = dict(products.resolve_sidecars(prod.sidecars, "a" * 64, "t")[0].env)
    b = dict(products.resolve_sidecars(prod.sidecars, "b" * 64, "t")[0].env)
    for key in ("PLUGIN_AES_AUTH_SECRET", "PLUGIN_AES_STATE_SECRET", "PLUGIN_AES_OAUTH_TOKEN_SECRET"):
        assert len(a[key].encode()) == 16, f"{key} 不是 16 字节"
        assert a[key] != b[key], f"{key} 没有按用户区分"


def test_coze_bitnami_middleware_runs_as_root(monkeypatch):
    """bitnami/minio/milvus 那几个要以 root 跑。

    它们镜像里的 USER 是 1001, 而 NAS 挂进来的目录是 root 的 —— 启动脚本里的
    chown 会失败, 然后进程写不进数据目录。上游 compose 靠 `user: root`,
    这里是等价物。漏了的症状是容器起来就退出, 日志里一句 Permission denied。
    """
    prod = _coze_ready(monkeypatch)
    as_root = {sc.name for sc in prod.sidecars if sc.run_as_user == 0}
    assert as_root == {"mysql", "redis", "elasticsearch", "minio", "etcd", "milvus"}


def test_coze_disabled_without_domain_or_assets_image(monkeypatch):
    """资产镜像没配就别出现在目录里。

    空着的话容器组会被阿里云拒掉, 而用户看到的是一直转圈 —— 服务端不报错。
    """
    monkeypatch.setattr(config, "COZE_DOMAIN", "coze.test.local")
    monkeypatch.setattr(config, "COZE_ASSETS_IMAGE_REF", "")
    assert "coze" not in [p.id for p in products.enabled()]
    monkeypatch.setattr(config, "COZE_DOMAIN", "")
    monkeypatch.setattr(config, "COZE_ASSETS_IMAGE_REF", "ghcr.io/x/a:t")
    assert "coze" not in [p.id for p in products.enabled()]


def test_dify_has_no_second_login_wall(monkeypatch):
    """Dify 也只留我们这一层登录墙。

    与 Coze 的两处关键差别 (改错任何一处都是"看着像好了, 其实没登进去"):
      · Dify 认 **cookie + X-CSRF-Token 头**, 而 access_token 是 HttpOnly ——
        前端读不到, 也就不发 Authorization。所以必须给**浏览器**发 Set-Cookie,
        只在上游注入是不够的 (前端还要自己从 csrf_token 里取值拼请求头)。
      · Dify 是单租户: setup 一辈子只能跑一次, 建不了第二个账号。所以免登录的
        密码必须**可推导** —— 随机后存盘的话, NAS 一丢就再也登不进那个既有账号,
        而 Dify 没有找回密码的路。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    boot = products.boot_script("dify")
    # 三个 cookie 缺一不可: 少 csrf 前端拼不出请求头, 少 refresh 一小时后就掉线
    for var in ("$dsh_at", "$dsh_rt", "$dsh_ct"):
        assert f"add_header Set-Cookie {var} always;" in boot, f"少发 {var}"
        assert f'map $sent_http_content_type {var} {{ default ""; }}' in boot, (
            f"{var} 没有安全默认值 —— nginx 会因为引用未定义变量直接起不来"
        )
    assert "/usr/local/bin/dsh-dify-autologin" in boot

    sh = products._DIFY_AUTOLOGIN
    assert "csrf_token" in sh and "refresh_token" in sh and "access_token" in sh
    # 登录收 base64 的密码, setup 收明文 —— 上游就是这么不对称的
    assert "base64" in sh
    assert '"$API/setup"' in sh and '"$API/login"' in sh
    # access_token 只活一小时, 容器能跑很久 -> 必须定期重登 (间隔见另一条测试)
    assert "sleep 1200" in sh


def test_dify_reissues_the_session_on_every_page_load(monkeypatch):
    """补发会话不能以"浏览器还没有"为条件。

    以 cookie 是否存在为条件有个致命洞: 浏览器手里那份一旦**失效**
    (账号改过密码、会话被服务端作废), 它仍然"存在" —— 于是永远补不上新的,
    用户被永久钉在 Dify 自己的登录页, 不手动清 cookie 怎么刷新都没用。
    2026-08-30 上线当天就是这么锁住老板的: 我给账号迁密码作废了他浏览器里的
    会话, 而规则只在"缺失"时补发。

    工作台只有一个账号, 不存在"别人的会话"要保住, 所以每次页面加载都覆盖。
    按响应类型收窄到 HTML: 静态资源和接口响应不必背这三个头。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    sh = products._DIFY_AUTOLOGIN
    assert "$cookie_refresh_token" not in sh, "又退回了'只在缺失时补发'"
    assert "$cookie_access_token" not in sh
    assert sh.count("~*^text/html") == 3, "三个 cookie 都要按 HTML 文档补发"
    # 刷新间隔要明显短于 token 的 60 分钟寿命, 否则新开的标签页会拿到过期的
    assert "sleep 1200" in sh


def test_dify_preinstalls_a_model_provider(monkeypatch):
    """Dify 开箱必须有模型可用。

    它的模型供应商是**插件**, 全新实例一个都没装 —— 用户新建个聊天助手, 模板里
    写的是 gpt-*, 于是当场报 `Provider langgenius/openai/openai does not exist`,
    模型那栏还标着"不兼容"。2026-08-30 老板一进去就撞上。

    所以启动后自己装一个 OpenAI 兼容插件, 把我们的网关配成自定义模型并设默认。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    sh = products._DIFY_AUTOLOGIN
    assert "provision()" in sh and "provision || true" in sh
    assert "openai_api_compatible" in sh
    assert "plugin/install/marketplace" in sh
    assert "models/credentials" in sh, "只 POST /models 是空转 —— 上游那个接口不建凭据却回 success"
    assert "default-model" in sh
    # **每次启动都要把令牌写一遍**: 工作台每次重建都会铸新令牌并撤销旧的, 而
    # Dify 把它存在自己库里 —— 只在缺失时写的话, 实例一回收它手里那枚就永远是
    # 废的, 模型节点报 401 not_authenticated 而 Dify 侧一切正常。
    assert 'api PUT "$CREDS_URL"' in sh, "已有凭据不会被刷新 -> 实例重建后必 401"
    assert "current_credential_id" in sh
    # **写凭据要重试**: 容器组刚起来时插件运行时还没加载完, 写会被顶回来
    # (no available node, plugin runtime not found)。一次就放弃 = 凭据里留着
    # 上一枚已撤销的令牌, 用户点开就是 401, 而 Dify 侧看着一切正常。
    assert "LLM_OK=no; EMB_OK=no" in sh and 'while [ "$n" -lt 30 ]' in sh
    assert '"result":"success"' in sh, "不看返回就没法判成败, 重试也就无从谈起"
    # 排查用的日志: 此前全部输出丢进 /dev/null, 出问题只能翻上游日志
    assert "/root/.dify-autologin.log" in sh
    # 装插件这一步才幂等 (装一次就够, 配置在 NAS 上的 Postgres, 实例重建后还在);
    # 凭据则每次启动都刷 —— 见上。
    assert "model-providers" in sh

    env = products.env_for("dify", "tok_x", "s" * 64)
    assert env["DSH_CLOUD_TOKEN"] == "tok_x"
    assert env["DSH_GATEWAY_BASE"].endswith("/llm/v1")
    assert env["DSH_DEFAULT_MODEL"] == model_catalog.default_model()


def test_dify_only_preconfigures_one_model_of_each_kind():
    """只预置默认的那一个 chat 模型 + 一个向量化模型, 不是整份目录。

    每加一个模型 Dify 都会真打一次上游做校验, 也就是**真扣一次积分**。二十个
    模型全配等于每次首次进入白烧二十次, 而用户想要别的在界面上点两下就能加。
    """
    sh = products._DIFY_AUTOLOGIN
    # 每写一次凭据 Dify 都会真打一次上游校验 = 真扣一次积分
    assert sh.count('ensure_model "$DSH_') == 2, "配的模型不止两个 —— 每个都要扣一次积分"
    assert 'ensure_model "$DSH_DEFAULT_MODEL" llm' in sh
    assert 'ensure_model "$DSH_EMBEDDING_MODEL" text-embedding' in sh
    # 目录里没有向量化模型时要跳过, 而不是配个空的进去 (那会让知识库在运行期才炸)
    assert '[ -n "$DSH_EMBEDDING_MODEL" ] || EMB_OK=yes' in sh
    # 两类各自判"有没有已存凭据", 各自建或刷 —— 只看 llm 的话, 已经配了聊天模型
    # 的老实例永远补不上向量化模型
    assert sh.count("CID=$(cred_id") == 1 and "ensure_model" in sh


def test_dify_autologin_backs_off_instead_of_locking_the_account(monkeypatch):
    """认证失败要退避, 不能死循环快重试。

    Dify 连错 5 次密码就把账号锁 **24 小时** (LOGIN_LOCKOUT_DURATION, 键在
    redis)。每 3 秒重试一次的话, 密码一旦对不上, 十几秒内就把用户自己的产品
    锁一整天 —— 2026-08-30 迁移既有账号密码时就这么锁了一次。

    分两档: 连不上/5xx 是 api 还没起来 (常态, 快重试); 4xx 是它答了但不认,
    再快也没用, 只会攒锁。setup 只在头几次试 —— 那个接口一辈子只能成功一次。
    """
    sh = products._DIFY_AUTOLOGIN
    assert "000|5*)" in sh, "没有区分'还没起来'和'不认'"
    assert "sleep 120" in sh, "认证失败没有退避 —— 会把账号锁 24 小时"
    assert '"$tries" -le 3' in sh, "setup 会被反复调用"
    # 单用户工作台里那把锁只锁得住我们自己, 所以启动时先清掉
    assert "login_error_rate_limit" in sh
    assert "DSH_REDIS_PASSWORD" in sh
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    assert products.env_for("dify", "t", "s" * 64)["DSH_REDIS_PASSWORD"]
    # 纵深防御: 锁定时长也收短, 免得哪天还是锁上了要等一天
    api = next(sc for sc in products._dify_stack() if sc.name == "api")
    assert dict(api.env)["LOGIN_LOCKOUT_DURATION"] == "300"


def test_dify_autologin_password_is_derived_not_stored(monkeypatch):
    """密码按用户推导, 且不同用户不同; 没有密钥时**不给**弱口令兜底。"""
    a = products.autologin_password("a" * 64)
    b = products.autologin_password("b" * 64)
    assert a != b, "所有用户共用一个口令等于没有口令"
    assert len(a) >= 12
    assert any(c.isupper() for c in a) and any(c.isdigit() for c in a), "过不了口令强度校验"
    assert products.autologin_password("") == "", (
        "没有密钥时该跳过免登录, 而不是退回一个人人都知道的口令"
    )

    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    env = products.env_for("dify", "tok", "s" * 64)
    assert env["DSH_AUTOLOGIN_PASSWORD"] == products.autologin_password("s" * 64)
    # 同一个推导也给 Hermes 的伴随容器用 (占位符), 两边必须是同一个值 ——
    # 对不上的症状只是"页面能开、接口全 401", 看不出是口令的问题。
    monkeypatch.setattr(config, "HERMES_DOMAIN", "hermes.test.local")
    hm = products.resolve_sidecars(
        products.registry()["hermes"].sidecars, "s" * 64, "t"
    )[0]
    assert dict(hm.env)["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] == products.env_for(
        "hermes", "t", "s" * 64
    )["HERMES_PASS"]
    # 邮箱必须与 setup 建的那个一致 —— 单租户, 没有第二个账号可用
    assert env["DSH_AUTOLOGIN_EMAIL"] == "admin@dshcloud.online"


def _dify_env(sidecars, name):
    return dict(next(s for s in sidecars if s.name == name).env)


def test_dify_stack_avoids_the_shared_namespace_port_collisions(monkeypatch):
    """ECI 容器组共享网络命名空间 —— compose 里各有 IP 所以能撞的端口, 在这里
    会真撞。2026-08-29 spike 里逐个跑出来的三处, 每处都不会在 ECI 侧报错。

    api 与 api_websocket 都默认 5001: 后起的那个绑不上, 容器反复重启。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    prod = products.registry()["dify"]
    assert len(prod.sidecars) == 9, "主容器 nginx + 9 个伴随 = 10"

    used = {}
    for sc in prod.sidecars:
        for k, v in sc.env:
            if k in ("DIFY_PORT", "SERVER_PORT", "SANDBOX_PORT", "PORT"):
                used.setdefault(v, []).append(sc.name)
    dupes = {p: n for p, n in used.items() if len(n) > 1}
    assert not dupes, f"共享命名空间里端口撞了: {dupes}"
    assert _dify_env(prod.sidecars, "api")["DIFY_PORT"] == "5001"
    assert _dify_env(prod.sidecars, "api-ws")["DIFY_PORT"] == "5011"


def test_dify_web_binds_all_interfaces(monkeypatch):
    """Next.js standalone 不设 HOSTNAME 就绑容器 IP 而不是 0.0.0.0 ——
    回环够不着, 症状是 nginx 回 502 而 web 容器一切正常。"""
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    web = _dify_env(products.registry()["dify"].sidecars, "web")
    assert web["HOSTNAME"] == "0.0.0.0", "不绑 0.0.0.0 -> 502"


def test_dify_secret_key_is_per_user_and_not_a_placeholder(monkeypatch):
    """compose 的 .env.example 把 SECRET_KEY 留空。空着**不影响启动**, 但注册
    成功之后登录报 Invalid encrypted data —— 加密解密对不上, 而且没有堆栈。

    必须按用户确定性推导: 换一个值, 用户已有的账号与凭据全部作废。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    prod = products.registry()["dify"]
    raw = _dify_env(prod.sidecars, "api")["SECRET_KEY"]
    assert raw == products.STACK_SECRET_PLACEHOLDER, "SECRET_KEY 该是占位符"

    s1 = security.stack_secret("u_a")
    resolved = products.resolve_sidecars(prod.sidecars, s1)
    for name in ("api", "api-ws", "worker"):
        got = _dify_env(resolved, name)["SECRET_KEY"]
        assert got == s1, f"{name} 的 SECRET_KEY 没被替换"
    assert products.STACK_SECRET_PLACEHOLDER not in str([sc.env for sc in resolved])
    assert security.stack_secret("u_a") == s1
    assert security.stack_secret("u_b") != s1


def test_dify_celery_backend_is_a_type_not_a_host(monkeypatch):
    """CELERY_BACKEND=redis 是**后端类型**, CELERY_BROKER_URL 的 redis:// 是
    **scheme** —— 都不是主机名。做主机改写时把它俩一起换成回环, celery 会报
    `No such transport: ''`, 那句话完全不指向根因 (spike 里栽过)。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    api = _dify_env(products.registry()["dify"].sidecars, "api")
    assert api["CELERY_BACKEND"] == "redis", "后端类型被当成主机名换掉了"
    assert api["CELERY_BROKER_URL"].startswith("redis://"), "scheme 被换掉了"
    assert "@127.0.0.1:6379" in api["CELERY_BROKER_URL"], "主机没指回环"


def test_dify_nginx_conf_is_generated_with_literal_loopback(monkeypatch):
    """官方 nginx 配置用 `set $up api:5001` + `resolver 127.0.0.11` (Docker
    内嵌 DNS)。nginx 的 resolver **不读 /etc/hosts** —— host_aliases 兜不住,
    所以 conf 由启动脚本自己生成, upstream 写死回环。
    """
    boot = products.boot_script("dify")
    assert "resolver" not in boot, "别把 docker DNS resolver 抄进来"
    assert "proxy_pass http://127.0.0.1:5001" in boot
    assert "proxy_pass http://127.0.0.1:3000" in boot
    assert "location /socket.io/" in boot and "127.0.0.1:5011" in boot
    assert "exec nginx" in boot


def test_dify_ssrf_proxy_is_blank_not_a_dead_address(monkeypatch):
    """我们不跑 squid (它要挂配置文件)。留着指向不存在的 3128 的话,
    HTTP 请求节点会全部超时失败 —— 留空 = 直出。"""
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    api = _dify_env(products.registry()["dify"].sidecars, "api")
    assert api["SSRF_PROXY_HTTP_URL"] == ""
    assert api["SSRF_PROXY_HTTPS_URL"] == ""


def test_dify_disabled_without_domain(monkeypatch):
    monkeypatch.setattr(config, "DIFY_DOMAIN", "")
    assert "dify" not in [p.id for p in products.enabled()]


def test_dify_plugin_daemon_has_every_field_it_validates_at_boot(monkeypatch):
    """plugin daemon 启动时逐个校验配置, 缺一个就 exit 1 -> CrashLoopBackOff。

    2026-08-29 首次上 ECI 栽在 PLUGIN_REMOTE_INSTALLING_HOST 上 —— 日志只有
    一句 "plugin remote installing host is empty", 而整个 Dify 的 UI/API 看起来
    都正常 (插件市场用不了才会发现)。这些不是"看起来像默认值"的可选项。
    """
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    env = _dify_env(products.registry()["dify"].sidecars, "plugind")
    for k in (
        "DB_TYPE",
        "PLUGIN_REMOTE_INSTALLING_HOST",
        "PLUGIN_REMOTE_INSTALLING_PORT",
        "PLUGIN_MEDIA_CACHE_PATH",
        "PLUGIN_PACKAGE_CACHE_PATH",
        "SERVER_PORT",
        "SERVER_KEY",
        "PLUGIN_STORAGE_LOCAL_ROOT",
        "PLUGIN_WORKING_PATH",
    ):
        assert env.get(k), f"plugind 缺必填项 {k} —— 它会启动即崩"
    # 调试端口绑回环: 实例自带 EIP, 0.0.0.0 等于多开一个公网面
    assert env["PLUGIN_REMOTE_INSTALLING_HOST"] == "127.0.0.1"


def test_readiness_accepts_any_answer_not_only_200(monkeypatch):
    """判据是「应答了」, 不是「回了 200」。

    未初始化的 Dify 首页是 307 (跳 /install)。要求 200 的话它永远停在 warming ——
    10 个容器全 Running、应用真在应答, 而用户对着进度条等到天荒地老, 服务端一个
    错都不报, 日志里只有一串 302。2026-08-29 Dify 首次接入时踩到。
    5xx 是"起来了但坏了", 不算就绪。
    """
    import httpx

    seen = {}

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_client(*_a, **_kw):
        class _C:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *_):
                return False

            async def get(self_inner, url, headers=None):
                return _Resp(seen["code"])

        return _C()

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    prod = products.registry()["comfyui"]
    loop = asyncio.get_event_loop_policy().new_event_loop()

    for code, want in (
        (200, True),
        (302, True),
        (307, True),
        (404, True),
        (500, False),
        (502, False),
        (503, False),
    ):
        seen["code"] = code
        got = loop.run_until_complete(workspace._ready("k", prod))
        assert got is want, f"HTTP {code} 应当判为 {'就绪' if want else '未就绪'}"


def test_readiness_probes_the_product_ready_path(monkeypatch):
    """探针打产品自己那条路径, 不是一律打首页。

    栈产品的首页答的是**前端**, 而前端比后端早起来得多 —— 拿首页当判据只探到
    门脸。2026-08-30 事故: Dify 的 api 还在跑数据库迁移 (约 75 秒), 首页那个
    Next.js 早就回 200 了, 于是用户在 api 就绪前 44 秒被放进去, 看到的是 Dify
    自己的 React 错误边界。
    """
    import httpx

    urls = []

    class _Resp:
        status_code = 200

    def fake_client(*_a, **_kw):
        class _C:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *_):
                return False

            async def get(self_inner, url, headers=None):
                urls.append(url)
                return _Resp()

        return _C()

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    loop = asyncio.get_event_loop_policy().new_event_loop()
    reg = products.registry()

    loop.run_until_complete(workspace._ready("k", reg["dify"]))
    assert urls[-1].endswith(reg["dify"].ready_path), urls[-1]

    # 单容器产品仍旧打首页: 端口一通就是应用本身在应答, 没有"门脸"这一层。
    loop.run_until_complete(workspace._ready("k", reg[products.DEFAULT]))
    assert urls[-1].endswith("/"), urls[-1]


def test_dify_ready_path_lands_on_the_api_upstream():
    """Dify 的 ready_path 必须落在**转发去 api** 的那条 location 上。

    这是一对跨文件的改动: 探活路径写在 registry(), 而它转发去哪个上游写在
    _dify_boot() 生成的 nginx 配置里。两处不一致不报错, 只是探针又探回了
    Next.js —— 事故原样复现, 而且照样一路绿灯。按 nginx 的最长前缀匹配钉住。
    """
    import re

    prod = products.registry()["dify"]
    conf = products.boot_script("dify")
    locs = re.findall(r"location\s+(/\S*)\s*\{\s*proxy_pass\s+http://127\.0\.0\.1:(\d+);", conf)
    assert locs, "没解析出 location —— nginx 配置的写法变了, 这个测试要跟着改"
    matched = [loc for loc in locs if prod.ready_path.startswith(loc[0])]
    assert matched, f"ready_path {prod.ready_path} 不落在任何 location 上"
    path, port = max(matched, key=lambda loc: len(loc[0]))
    assert port == "5001", f"ready_path 命中的是 location {path} -> {port}, 不是 api(5001)"
