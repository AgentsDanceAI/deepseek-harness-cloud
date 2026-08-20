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
from ._signup import signup, signup_with_password

client = TestClient(app)


@pytest.fixture(autouse=True)
def _pin_gateway_config(monkeypatch):
    # config freezes env at first import, and test collection order across
    # modules is not guaranteed — pin the values the gateway tests depend on so
    # this file passes regardless of which test module imported config first.
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-upstream-test")
    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://api.qianmian.ai/v1")


def _register(email: str = "u1@test.local", password: str = "password123") -> dict:
    # 名字保留 _register 以免改动过多调用点; 实际走验证码路径 + 设密码
    # (下游用例既测 /api/auth/me 又测密码登录)。
    signup_with_password(client, email, password)
    return client.get("/api/auth/me").json()["user"]


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
    signup_with_password(browser, "dev@test.local")

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
    signup_with_password(fresh, "epoch@test.local")
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
    assert credits.balance(uid) == plans.pricing()["tiers"]["pro"]["monthly_credits"]

    plans.apply_plan(uid, "pro", "monthly", order_id="o2")      # renewal extends
    assert plans.current_plan(uid)["expires"] > first["expires"]
    assert credits.balance(uid) == plans.pricing()["tiers"]["pro"]["monthly_credits"] * 2


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
    # signup() 幂等 (email/login 是"验证码即登录或注册"), 建号与再登录同一条路。
    # 曾在 else 分支用密码登录 —— 但验证码注册的号没有密码, 第二个用例起全 401。
    signup(fresh, email)
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
    signup(fresh, "poor@test.local")
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
    assert spent >= config.SEARCH_CALL_CREDITS  # flat search fee + token cost


def test_models_listing(gw_user):
    fresh, _ = gw_user
    r = fresh.get("/llm/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "deepseek-v4-flash" in ids and "deepseek-v4-pro" in ids


def test_gateway_concurrency_headroom(gw_user, monkeypatch):
    """One agent task = main stream + short aux calls (title/compaction). The
    admission limit must be plan concurrency PLUS headroom, or free tier (1)
    deadlocks against its own session-title request — the exact production bug
    from session-504bc795: 'Plan allows 1 concurrent request(s)' killing chat."""
    fresh, uid = gw_user
    monkeypatch.setitem(gateway._inflight, uid, 1)  # main stream in flight
    body = {"id": "x", "usage": {"prompt_tokens": 5, "completion_tokens": 5}, "choices": []}
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _FakeClient(json_body=body))
    # aux call while the main stream runs must be admitted (free concurrency=1)
    r = fresh.post("/llm/v1/chat/completions", json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200
    # but the cap still exists: at limit+headroom, reject
    monkeypatch.setitem(gateway._inflight, uid, 1 + gateway.AUX_REQUEST_HEADROOM)
    r2 = fresh.post("/llm/v1/chat/completions", json={"model": "deepseek-v4-flash", "messages": []})
    assert r2.status_code == 429


# --- credit convention (must stay aligned with AgentsDance for portability) ---

def test_multiplier_is_anchored_on_the_baseline_model():
    """1.00x is defined as the baseline model, and 1.00x == 1000 credits / 1M.
    These two facts are what make a credit balance mean the same thing in both
    products, so they are locked here rather than left to the generator."""
    from app import model_catalog
    meta = model_catalog.meta()
    baseline = meta.get("baseline_model")
    assert baseline, "generated catalog must record its 1.00x anchor"
    entry = model_catalog.resolve(baseline)
    assert entry is not None
    assert abs(entry["multiplier"] - 1.0) < 0.01
    assert entry["credits_per_m"] == meta["credits_per_baseline_m"] == 1000


def test_cheaper_model_costs_proportionally_fewer_credits():
    from app import model_catalog
    cat = model_catalog.catalog()
    baseline = model_catalog.meta()["baseline_model"]
    cheap = min(cat.values(), key=lambda m: m["multiplier"])
    # Charge the SAME 75/25 input:output split the multiplier is defined from.
    # Billing pure input instead compares input prices, which only tracks the
    # blended multiplier when the two models share the baseline's price shape —
    # they don't, so that version of this test was asserting a coincidence.
    base_cost = model_catalog.charge_credits(baseline, 750_000, 0, 250_000)
    cheap_cost = model_catalog.charge_credits(cheap["id"], 750_000, 0, 250_000)
    assert cheap_cost < base_cost
    ratio = cheap_cost / base_cost
    assert ratio == pytest.approx(cheap["multiplier"] / cat[baseline]["multiplier"], rel=0.05)


def test_unknown_model_bills_at_the_priciest_entry():
    """A gap in the catalog must never become a free ride."""
    from app import model_catalog
    priciest = max(model_catalog.catalog().values(),
                   key=lambda m: m.get("output_usd_per_m", 0))
    assert (model_catalog.charge_credits("no-such-model", 0, 0, 100_000)
            == model_catalog.charge_credits(priciest["id"], 0, 0, 100_000))


def test_any_token_flow_costs_at_least_one_credit():
    from app import model_catalog
    cheapest = min(model_catalog.catalog().values(), key=lambda m: m["multiplier"])
    assert model_catalog.charge_credits(cheapest["id"], 1, 0, 1) >= 1
    assert model_catalog.charge_credits(cheapest["id"], 0, 0, 0) == 0


def test_query_tolerates_writes_that_return_no_rows():
    """db.query() is used for one-off writes as well as SELECTs. SQLite happily
    fetchall()s a DELETE; psycopg raises "the last operation didn't produce
    records". That difference took production login down after the Postgres
    switch, so the layer must absorb it rather than every call site."""
    from app import db
    db.query("DELETE FROM email_codes WHERE email=?", ("nobody@nowhere.invalid",))
    assert db.query("UPDATE users SET display_name=display_name WHERE id=?", ("no-such-id",)) == []
    assert db.query_one("SELECT COUNT(*) AS n FROM users") is not None


def test_workspace_offers_exactly_the_sellable_catalog():
    """The container's model picker is generated from the catalog, not a copy of
    it. A hardcoded list drifts: the workspace offered two models while twenty
    were priced and advertised, so nineteen of them were unreachable in the
    product we actually ship."""
    from app import model_catalog, workspace

    boot = workspace._boot_script()
    ids = list(model_catalog.catalog())
    assert len(ids) == 20, f"curated catalog should be 20 models, got {len(ids)}"
    for mid in ids:
        assert f"- id: {mid}\n" in boot, f"{mid} is sellable but not offered in the workspace"
    assert f"model: {model_catalog.default_model()}\n" in boot

    # dsh ships its own DeepSeek provider. Left alone it renders a second group
    # in the picker whose entries resolve to the public api.deepseek.com with
    # our device token as the key — broken, and a credential sent off-platform.
    assert "llm-deepseek:" in boot and "models: []" in boot
    assert "api.deepseek.com" not in boot


def test_boot_fingerprint_tracks_configuration_not_the_user():
    """Recreating stale containers hinges on this: if the digest moved per user
    (or per call) every workspace would be rebuilt on every visit."""
    from app import workspace
    a = workspace._boot_fingerprint(workspace._boot_script())
    b = workspace._boot_fingerprint(workspace._boot_script())
    assert a == b
    from app.workbackend import WorkInfo
    blank = WorkInfo(running=True, boot_fp="", image_id="i", host="h")
    assert workspace._boot_is_stale(blank) is True          # 没盖过戳 = 早于这套机制
    assert workspace._boot_is_stale(WorkInfo(True, a, "i", "h")) is False


def test_smtp_rejection_is_a_client_error_not_a_500():
    """Email codes are the primary sign-in path. A provider rejecting the
    recipient (bad domain, suppression list) is not a bug in our code, and
    surfacing it as 500 showed users "请求失败 (500)" with no hint that the
    address was the problem."""
    import smtplib
    from unittest.mock import patch
    from fastapi import HTTPException
    from app import accounts

    def reject(code, text):
        exc = smtplib.SMTPDataError(code, text)
        # without a host configured _send_mail short-circuits to the dev printer
        with patch.object(accounts.config, "MAIL_SMTP_HOST", "smtp.test"), \
                patch("smtplib.SMTP_SSL", side_effect=exc):
            try:
                accounts._send_mail("someone@nowhere.invalid", "s", "t")
            except HTTPException as e:
                return e.status_code
        return None

    assert reject(550, b"Invalid `to` field") == 400      # permanent -> user fixes the address
    assert reject(451, b"try again later") == 503         # transient -> not the user's fault


def test_smtp_transport_failure_is_503():
    from unittest.mock import patch
    from fastapi import HTTPException
    from app import accounts
    with patch.object(accounts.config, "MAIL_SMTP_HOST", "smtp.test"), \
            patch("smtplib.SMTP_SSL", side_effect=OSError("connection refused")):
        try:
            accounts._send_mail("a@b.com", "s", "t")
            raise AssertionError("expected HTTPException")
        except HTTPException as e:
            assert e.status_code == 503


def test_account_deletion_actually_erases_personal_data():
    """The privacy policy promises erasure. A status flag beside an intact
    display name and password hash is not erasure, and would make the published
    policy untrue."""
    from app import accounts, db, security
    import time
    uid = security.new_id("u_")
    email = "erase-me@example.test"
    with db.tx() as c:
        c.execute("INSERT INTO users (id,email,display_name,password_hash,role,session_epoch,created) "
                  "VALUES (?,?,?,?,?,?,?)", (uid, email, "要删的人", "hash", "user", 1, time.time()))
        c.execute("INSERT INTO email_codes (email,code_hash,purpose,expires,created) VALUES (?,?,?,?,?)",
                  (email, "h", "login", time.time() + 600, time.time()))

    user = dict(db.query_one("SELECT * FROM users WHERE id=?", (uid,)))
    accounts.delete_account({"confirm": email}, user=user)

    row = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    assert row["status"] == "deleted"
    assert email not in (row["email"] or "")
    assert not row["display_name"], "display name must not survive deletion"
    assert not row["password_hash"], "password hash must not survive deletion"
    assert db.query("SELECT 1 FROM email_codes WHERE email=?", (email,)) == []
    assert db.query("SELECT 1 FROM devices WHERE user_id=?", (uid,)) == []


def test_waffo_product_amount_is_a_display_string():
    """Waffo documents prices[CUR].amount as a display-format STRING. Sending a
    JSON number gets {"message":"Invalid input"} with no field named, which is
    almost impossible to diagnose from the response alone."""
    import inspect
    from app.payments import waffo_provider
    src = inspect.getsource(waffo_provider.catalog_prices)
    assert 'f"{cents / 100:.2f}"' in src
    assert "round(cents" not in src


def test_waffo_products_carry_every_quoted_currency():
    """create-session is rejected for a currency the product does not list, so a
    USD-only product breaks checkout for exactly the visitors who were quoted a
    local price."""
    from app import currency
    from app.payments import waffo_provider
    prices = waffo_provider.catalog_prices("plan:plus:monthly")
    assert set(prices) == set(currency.SUPPORTED)
    # 价目表是商业决定, 会变 —— 读表而不是钉死数字, 否则改价必然连累这条测试
    table = plans.pricing("USD")["tiers"]["plus"], plans.pricing("CNY")["tiers"]["plus"]
    assert prices["USD"]["amount"] == f'{table[0]["monthly_cents"] / 100:.2f}'
    assert prices["CNY"]["amount"] == f'{table[1]["monthly_cents"] / 100:.2f}'


def test_item_of_round_trips_every_sellable_kind():
    """resolve_item accepts plan / pack / seats; _item_of has to rebuild all three. It knew only two, so seats and the workspace pass raised
    KeyError on the way to checkout and could never be bought."""
    from app.payments import base, waffo_provider
    for item in ("plan:pro:yearly", "pack:pack1000", "seats:3"):
        info = dict(base.resolve_item(item))
        assert waffo_provider._item_of(info) == item, item


def test_workspace_assets_are_version_stamped():
    """Cloudflare caches /pwa/*.css for 24h. Those stylesheets carry the phone
    layout fixes, so an unversioned URL means a layout fix ships a day late —
    the stale copy IS the bug being fixed."""
    import re
    from app import workspace
    head = workspace._pwa_inject()
    assert "{asset_v}" not in head, "version placeholder was not substituted"
    for asset in ("mobile.css", "workspace-chrome.css", "workspace-chrome.js"):
        assert re.search(rf"{re.escape(asset)}\?v=\w+", head), f"{asset} is not version-stamped"


def test_i18n_catalogs_are_in_parity():
    """Both languages must define the same keys with the same placeholders.
    A key present in only one language renders that language's text inside the
    other's page; a placeholder mismatch throws away a runtime value."""
    import json, re
    from app import config, i18n
    cats = {}
    for lang in i18n.SUPPORTED:
        p = config.CONFIG_DIR / "i18n" / f"{lang}.json"
        cats[lang] = {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}
    zh, en = cats["zh"], cats["en"]
    assert set(zh) == set(en), f"key drift: only-zh={sorted(set(zh)-set(en))} only-en={sorted(set(en)-set(zh))}"
    for k in zh:
        assert set(re.findall(r"{(\w+)}", zh[k])) == set(re.findall(r"{(\w+)}", en[k])), k


def test_i18n_falls_back_rather_than_blanking():
    from app import i18n
    assert i18n.t("en", "nav.pricing") == "Pricing"
    # an unknown key renders as itself — visible in review, never a blank page
    assert i18n.t("en", "no.such.key") == "no.such.key"


def test_language_resolution_prefers_an_explicit_choice():
    """A click on EN must beat a zh-CN browser, and must be reported as
    explicit so the caller persists it — otherwise the switch lasts one page."""
    from app import i18n

    class Req:
        def __init__(self, q=None, cookie=None, accept=""):
            self.query_params = q or {}
            self.cookies = {i18n.COOKIE: cookie} if cookie else {}
            self.headers = {"accept-language": accept}

    assert i18n.resolve(Req(q={"lang": "en"}, cookie="zh", accept="zh-CN")) == ("en", True)
    assert i18n.resolve(Req(cookie="en", accept="zh-CN")) == ("en", False)
    assert i18n.resolve(Req(accept="en-GB,en;q=0.9,zh;q=0.4")) == ("en", False)
    assert i18n.resolve(Req(accept="fr-FR,fr;q=0.9")) == (i18n.DEFAULT, False)


def test_query_survives_a_literal_percent():
    """psycopg treats % as a placeholder marker, so `LIKE 'x_%'` blew up with
    "only '%s', '%b', '%t' are allowed as placeholders" — on SQLite the same
    query works fine, so this only ever failed in production."""
    from app import db
    db.query("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v",
             ("pcttest_a", "1"))
    rows = db.query("SELECT k FROM kv WHERE k LIKE 'pcttest%'")
    assert any(r["k"] == "pcttest_a" for r in rows)
    db.query("DELETE FROM kv WHERE k LIKE 'pcttest%'")


def test_admin_role_guards_cannot_lock_everyone_out():
    """The three ways an admin panel loses its last admin: demoting yourself,
    demoting the only one left, and demoting someone the env grants anyway
    (which the UI would then be lying about, since the next request restores it)."""
    import time
    from fastapi import HTTPException
    from app import admin, config, db, security

    def mk(email, role="user"):
        uid = security.new_id("u_")
        with db.tx() as c:
            c.execute("INSERT INTO users (id,email,display_name,role,session_epoch,created) "
                      "VALUES (?,?,?,?,?,?)", (uid, email, "", role, 1, time.time()))
        return dict(db.query_one("SELECT * FROM users WHERE id=?", (uid,)))

    a = mk("guard-a@t.local", "admin")
    b = mk("guard-b@t.local", "admin")

    for body, expect in (({"user_id": a["id"], "admin": False}, "cannot_demote_self"),):
        try:
            admin.set_role(body, user=a); raise AssertionError("expected refusal")
        except HTTPException as e:
            assert expect in str(e.detail)

    # demoting the other one is fine while two exist
    admin.set_role({"user_id": b["id"], "admin": False}, user=a)
    assert db.query_one("SELECT role FROM users WHERE id=?", (b["id"],))["role"] == "user"

    # promoting works, and is what the UI calls
    admin.set_role({"user_id": b["id"], "admin": True}, user=a)
    assert db.query_one("SELECT role FROM users WHERE id=?", (b["id"],))["role"] == "admin"

    # an env-granted admin cannot be demoted from the UI
    old = list(config.ADMIN_EMAILS)
    config.ADMIN_EMAILS.append("guard-b@t.local")
    try:
        admin.set_role({"user_id": b["id"], "admin": False}, user=a)
        raise AssertionError("expected refusal")
    except HTTPException as e:
        assert "admin_from_env" in str(e.detail)
    finally:
        config.ADMIN_EMAILS[:] = old
    db.query("DELETE FROM users WHERE email LIKE 'guard-%@t.local'")


def test_product_id_is_cached_before_publish():
    """Publishing is a separate call that can fail. If the id is only cached
    after it succeeds, every retry creates another product — that is how the
    store accumulated three copies of all eleven items."""
    import inspect
    from app.payments import waffo_provider
    src = inspect.getsource(waffo_provider.ensure_product_id)
    cache_pos = src.index('_kv_set(cache_key, pid)')
    publish_pos = src.index('publish-product')
    assert cache_pos < publish_pos, "the id must be cached before the publish call"


def test_existing_product_scan_sees_the_whole_store():
    """The reuse-by-name scan is the second line of defence against duplicates,
    and it was blind twice over: onetimeProducts returns 10 products unless
    asked for more, and a name match against a DEACTIVATED product would have
    handed checkout a product Waffo refuses to sell."""
    import inspect
    from app.payments import waffo_provider
    src = inspect.getsource(waffo_provider.ensure_product_id)
    assert "limit:200" in src
    scan = src[src.index("onetimeProducts"):src.index('"/v1/actions/onetime-product/create-product"')]
    assert 'p.get("status") != "active"' in scan
