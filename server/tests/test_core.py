"""Core flows: accounts, device auth, credits, plans, and the LLM gateway.

Environment is pinned before app imports (config reads env at import time).
Upstream LLM calls are stubbed at the gateway's _upstream_client seam.
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-core-")
os.environ.update({
    "DHC_DEV": "1",
    "AUTH_SECRET": "test-secret",
    "DHC_DATA_DIR": _TMP,
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "UPSTREAM_API_KEY": "sk-upstream-test",
    "FREE_SIGNUP_CREDITS": "500",
})

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import pytest  # noqa: E402

from app import config, credits, db, gateway, plans, security  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _pin_gateway_config(monkeypatch):
    # config freezes env at first import, and test collection order across
    # modules is not guaranteed — pin the values the gateway tests depend on so
    # this file passes regardless of which test module imported config first.
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-upstream-test")
    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://api.qianmian.ai/v1")


def _register(email: str = "u1@test.local", password: str = "password123") -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def _user_id(email: str) -> str:
    return db.query_one("SELECT id FROM users WHERE email=?", (email,))["id"]


# --- accounts ----------------------------------------------------------------

def test_register_login_me():
    _register()
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "u1@test.local"
    assert body["credits"] == 500          # signup grant
    assert body["plan"]["tier"] == "free"

    client.post("/api/auth/logout")
    fresh = TestClient(app)
    assert fresh.get("/api/auth/me").status_code == 401
    r = fresh.post("/api/auth/login", json={"email": "u1@test.local", "password": "password123"})
    assert r.status_code == 200
    assert fresh.get("/api/auth/me").status_code == 200


def test_bad_password_and_lockout():
    fresh = TestClient(app)
    for _ in range(5):
        r = fresh.post("/api/auth/login", json={"email": "u1@test.local", "password": "wrong"})
        assert r.status_code == 401
    r = fresh.post("/api/auth/login", json={"email": "u1@test.local", "password": "password123"})
    assert r.status_code == 429  # locked even with the right password


# --- device flow -------------------------------------------------------------

def test_device_flow_and_revocation():
    browser = TestClient(app)
    browser.post("/api/auth/register", json={"email": "dev@test.local", "password": "password123"})

    desktop = TestClient(app)
    start = desktop.post("/api/device/start", json={"name": "mac-mini", "platform": "darwin"}).json()
    assert "-" in start["user_code"] and start["device_code"]

    # pending until the browser user approves
    assert desktop.post("/api/device/poll", json={"device_code": start["device_code"]}).json()["status"] == "pending"
    info = browser.get(f"/api/device/info?code={start['user_code']}").json()
    assert info["client"]["name"] == "mac-mini"
    assert browser.post("/api/device/approve", json={"user_code": start["user_code"]}).status_code == 200

    result = desktop.post("/api/device/poll", json={"device_code": start["device_code"]}).json()
    assert result["status"] == "approved"
    token = result["token"]

    # the device token authenticates API calls
    authed = client.get("/api/auth/me", headers={"authorization": f"Bearer {token}"})
    assert authed.status_code == 200 and authed.json()["user"]["email"] == "dev@test.local"

    # revoking the device kills the token
    devices = browser.get("/api/auth/devices").json()["devices"]
    browser.post("/api/auth/devices/revoke", json={"device_id": devices[0]["id"]})
    assert client.get("/api/auth/me", headers={"authorization": f"Bearer {token}"}).status_code == 401


def test_device_password_login():
    r = client.post("/api/device/login", json={
        "email": "dev@test.local", "password": "password123", "name": "win-pc", "platform": "win32"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert client.get("/api/auth/me", headers={"authorization": f"Bearer {token}"}).status_code == 200


def test_epoch_revocation_on_password_change():
    fresh = TestClient(app)
    fresh.post("/api/auth/register", json={"email": "epoch@test.local", "password": "password123"})
    token = fresh.post("/api/device/login", json={
        "email": "epoch@test.local", "password": "password123", "name": "x", "platform": "linux"}).json()["token"]
    assert fresh.post("/api/auth/password", json={"old": "password123", "new": "password456"}).status_code == 200
    assert client.get("/api/auth/me", headers={"authorization": f"Bearer {token}"}).status_code == 401


# --- credits -----------------------------------------------------------------

def test_credit_buckets_expiry_and_overdraft():
    uid = "u_credit_test"
    with db.tx() as conn:
        conn.execute("INSERT INTO users (id, email, created) VALUES (?,?,?)",
                     (uid, "credit@test.local", db.now()))
    credits.grant(uid, 100, ttl_s=-1, kind="grant_topup")       # already expired
    credits.grant(uid, 50, ttl_s=3600, kind="grant_plan")       # expires first
    credits.grant(uid, 200, ttl_s=86400, kind="grant_topup")
    assert credits.balance(uid) == 250                          # expired bucket ignored

    credits.spend(uid, 60, kind="llm", model="m")               # 50 from soonest + 10 from next
    assert credits.balance(uid) == 190
    rows = db.query("SELECT remaining FROM credit_grants WHERE user_id=? AND kind='grant_plan'", (uid,))
    assert int(rows[0]["remaining"]) == 0

    credits.spend(uid, 250, kind="llm", model="m")              # overdraft by 60
    assert credits.balance(uid) == -60
    assert plans.check_run_blocked(uid) == "insufficient_credits"


def test_plan_apply_and_renewal():
    uid = "u_plan_test"
    with db.tx() as conn:
        conn.execute("INSERT INTO users (id, email, created) VALUES (?,?,?)",
                     (uid, "plan@test.local", db.now()))
    plans.apply_plan(uid, "pro", "monthly", order_id="o1")
    first = plans.current_plan(uid)
    assert first["tier"] == "pro" and first["concurrency"] == 5
    assert credits.balance(uid) == 13000

    plans.apply_plan(uid, "pro", "monthly", order_id="o2")      # renewal extends
    assert plans.current_plan(uid)["expires"] > first["expires"]
    assert credits.balance(uid) == 26000


# --- gateway -----------------------------------------------------------------

class _FakeStreamResponse:
    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class _FakeClient:
    """Stub for gateway._upstream_client covering stream and non-stream."""
    last_request = None

    def __init__(self, *, chunks=None, json_body=None, status=200):
        self._chunks = chunks
        self._json = json_body
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, **kw):
        _FakeClient.last_request = {"url": url, **kw}
        return _FakeStreamCtx(_FakeStreamResponse(self._chunks))

    async def post(self, url, **kw):
        _FakeClient.last_request = {"url": url, **kw}
        return httpx.Response(self._status, json=self._json,
                              request=httpx.Request("POST", url))


@pytest.fixture()
def gw_user():
    fresh = TestClient(app)
    email = "gw@test.local"
    if db.query_one("SELECT id FROM users WHERE email=?", (email,)) is None:
        fresh.post("/api/auth/register", json={"email": email, "password": "password123"})
    else:
        fresh.post("/api/auth/login", json={"email": email, "password": "password123"})
    return fresh, _user_id(email)


def test_gateway_requires_auth():
    assert TestClient(app).post("/llm/v1/chat/completions", json={}).status_code == 401


def test_gateway_stream_bills_from_usage_chunk(gw_user, monkeypatch):
    fresh, uid = gw_user
    before = credits.balance(uid)
    usage_chunk = {"choices": [], "usage": {
        "prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 2000}}
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b"data: " + json.dumps(usage_chunk).encode() + b"\n\n",
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _FakeClient(chunks=chunks))
    r = fresh.post("/llm/v1/chat/completions",
                   json={"model": "deepseek-v4-flash", "stream": True, "messages": []})
    assert r.status_code == 200
    assert "hi" in r.text and "[DONE]" in r.text
    # upstream got OUR key, not the user token
    assert _FakeClient.last_request["headers"]["authorization"] == "Bearer sk-upstream-test"
    # billed: 600 uncached * 2 + 400 cached * 0.2 + 2000 out * 6 per 1M CNY, *100 credits *1.2
    spent = before - credits.balance(uid)
    row = db.query_one("SELECT * FROM usage_log WHERE user_id=? ORDER BY created DESC", (uid,))
    assert (row["uncached_input"], row["cache_read"], row["output"]) == (600, 400, 2000)
    assert spent == row["credits"] >= 1


def test_gateway_nonstream_and_model_rewrite(gw_user, monkeypatch):
    fresh, uid = gw_user
    body = {"id": "x", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": []}
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _FakeClient(json_body=body))
    r = fresh.post("/llm/v1/chat/completions", json={"model": "deepseek-v4-pro", "messages": []})
    assert r.status_code == 200 and r.json()["id"] == "x"
    sent = json.loads(_FakeClient.last_request["json"] if isinstance(
        _FakeClient.last_request.get("json"), str) else json.dumps(_FakeClient.last_request["json"]))
    assert sent["model"] == "deepseek-v4-pro"


def test_gateway_unknown_model_rejected(gw_user):
    fresh, _ = gw_user
    r = fresh.post("/llm/v1/chat/completions", json={"model": "gpt-9", "messages": []})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_gateway_upstream_auth_error_not_leaked(gw_user, monkeypatch):
    """Our upstream key being rejected must NOT surface as 401 (dsh would blame the user)."""
    fresh, _ = gw_user
    monkeypatch.setattr(gateway, "_upstream_client",
                        lambda: _FakeClient(json_body={"error": {"message": "bad key"}}, status=401))
    r = fresh.post("/llm/v1/chat/completions", json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 502
    assert "sk-upstream" not in r.text


def test_gateway_blocks_when_credits_exhausted(monkeypatch):
    fresh = TestClient(app)
    fresh.post("/api/auth/register", json={"email": "poor@test.local", "password": "password123"})
    uid = _user_id("poor@test.local")
    credits.spend(uid, 500, kind="llm", model="m")  # burn the signup grant
    assert credits.balance(uid) == 0
    r = fresh.post("/llm/v1/chat/completions", json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 402
    assert r.json()["error"]["type"] == "insufficient_quota"


def test_gateway_anthropic_search_surface(gw_user, monkeypatch):
    from app import config as app_config
    monkeypatch.setattr(app_config, "SEARCH_PROVIDER", "upstream")  # this test covers passthrough
    fresh, uid = gw_user
    before = credits.balance(uid)
    body = {"id": "msg", "content": [], "usage": {"input_tokens": 100, "output_tokens": 50}}
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _FakeClient(json_body=body))
    r = fresh.post("/llm/anthropic/v1/messages", json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200
    assert _FakeClient.last_request["headers"]["x-api-key"] == "sk-upstream-test"
    assert _FakeClient.last_request["url"].endswith("/anthropic/v1/messages")
    spent = before - credits.balance(uid)
    assert spent >= 5  # flat search fee + token cost


def test_models_listing(gw_user):
    fresh, _ = gw_user
    r = fresh.get("/llm/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "deepseek-v4-flash" in ids and "deepseek-v4-pro" in ids
