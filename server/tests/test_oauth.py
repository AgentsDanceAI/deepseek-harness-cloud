"""Google / GitHub OAuth login.

Environment is pinned before app imports (config reads env at import time).
Provider HTTP calls are stubbed by monkeypatching httpx.AsyncClient at the
oauth module boundary; no real network is touched.
"""

import os
import tempfile
import urllib.parse

_TMP = tempfile.mkdtemp(prefix="dhc-oauth-")
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "GOOGLE_LOGIN_CLIENT_ID": "gid",
        "GOOGLE_LOGIN_CLIENT_SECRET": "gsec",
        "GITHUB_LOGIN_CLIENT_ID": "hid",
        "GITHUB_LOGIN_CLIENT_SECRET": "hsec",
        # config freezes at first app import; when this module imports before
        # test_core (e.g. `pytest test_oauth.py test_core.py`), core's gateway
        # tests still need our upstream key present in config.
        "UPSTREAM_API_KEY": "sk-upstream-test",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, oauth  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _oauth_config(monkeypatch):
    """Pin OAuth creds on the config module for every test. config constants
    freeze on the first app import, so in a full-suite run (where test_core
    imports first, without these creds) our routes would otherwise see them
    unset — setting them here keeps the tests order-independent."""
    monkeypatch.setattr(config, "GOOGLE_LOGIN_CLIENT_ID", "gid")
    monkeypatch.setattr(config, "GOOGLE_LOGIN_CLIENT_SECRET", "gsec")
    monkeypatch.setattr(config, "GITHUB_LOGIN_CLIENT_ID", "hid")
    monkeypatch.setattr(config, "GITHUB_LOGIN_CLIENT_SECRET", "hsec")


GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
GITHUB_EMAILS = "https://api.github.com/user/emails"


class _Resp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.content = b"x"

    def json(self):
        return self._json


def _stub_httpx(monkeypatch, *, post=None, get=None):
    """Replace httpx.AsyncClient with a fake that returns canned responses per URL."""
    posts = post or {}
    gets = get or {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return posts[url]

        async def get(self, url, **kw):
            return gets[url]

    monkeypatch.setattr(oauth.httpx, "AsyncClient", _FakeClient)


def _state_from(location: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("state", [""])[0]


# --- start -------------------------------------------------------------------


def test_google_start_redirects_and_sets_nonce():
    c = TestClient(app, follow_redirects=False)
    r = c.get("/api/auth/google/start?next=/console")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "state=" in loc and _state_from(loc)
    assert r.cookies.get(oauth._NONCE_COOKIE)


def test_github_start_redirects_and_sets_nonce():
    c = TestClient(app, follow_redirects=False)
    r = c.get("/api/auth/github/start")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://github.com/login/oauth/authorize")
    assert _state_from(loc)
    assert r.cookies.get(oauth._NONCE_COOKIE)


# --- state / CSRF ------------------------------------------------------------


def test_google_callback_bad_state_redirects_error():
    c = TestClient(app, follow_redirects=False)
    r = c.get("/api/auth/google/callback?code=abc&state=bogus")
    assert r.status_code == 302
    assert "/login?google_error" in r.headers["location"]


def test_google_callback_missing_state_redirects_error():
    c = TestClient(app, follow_redirects=False)
    r = c.get("/api/auth/google/callback?code=abc")
    assert r.status_code == 302
    assert "/login?google_error" in r.headers["location"]


# --- google happy / unverified ----------------------------------------------


def test_google_callback_happy_creates_user_and_logs_in(monkeypatch):
    c = TestClient(app, follow_redirects=False)
    start = c.get("/api/auth/google/start?next=/console")
    state = _state_from(start.headers["location"])
    _stub_httpx(
        monkeypatch,
        post={GOOGLE_TOKEN: _Resp(200, {"access_token": "tok"})},
        get={
            GOOGLE_USERINFO: _Resp(
                200, {"email": "g@test.local", "email_verified": True, "name": "Google User"}
            )
        },
    )
    r = c.get(f"/api/auth/google/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert r.headers["location"] == "/console"
    assert r.cookies.get(config.SESSION_COOKIE)
    user = db.query_one("SELECT * FROM users WHERE email=?", ("g@test.local",))
    assert user is not None
    assert user["display_name"] == "Google User"


def test_google_callback_unverified_email_no_login(monkeypatch):
    c = TestClient(app, follow_redirects=False)
    start = c.get("/api/auth/google/start")
    state = _state_from(start.headers["location"])
    _stub_httpx(
        monkeypatch,
        post={GOOGLE_TOKEN: _Resp(200, {"access_token": "tok"})},
        get={GOOGLE_USERINFO: _Resp(200, {"email": "unverified@test.local", "email_verified": False})},
    )
    r = c.get(f"/api/auth/google/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert "/login?google_error" in r.headers["location"]
    assert not r.cookies.get(config.SESSION_COOKIE)
    assert db.query_one("SELECT id FROM users WHERE email=?", ("unverified@test.local",)) is None


# --- github happy ------------------------------------------------------------


def test_github_callback_happy_primary_verified_logs_in(monkeypatch):
    c = TestClient(app, follow_redirects=False)
    start = c.get("/api/auth/github/start")
    state = _state_from(start.headers["location"])
    _stub_httpx(
        monkeypatch,
        post={GITHUB_TOKEN: _Resp(200, {"access_token": "tok"})},
        get={
            GITHUB_USER: _Resp(200, {"login": "octocat", "name": "The Octocat"}),
            GITHUB_EMAILS: _Resp(
                200,
                [
                    {"email": "secondary@test.local", "verified": True, "primary": False},
                    {"email": "primary@test.local", "verified": True, "primary": True},
                    {"email": "junk@test.local", "verified": False, "primary": False},
                ],
            ),
        },
    )
    r = c.get(f"/api/auth/github/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert r.headers["location"] == "/console"
    assert r.cookies.get(config.SESSION_COOKIE)
    user = db.query_one("SELECT * FROM users WHERE email=?", ("primary@test.local",))
    assert user is not None
    assert user["display_name"] == "The Octocat"


def test_github_callback_no_verified_email_no_login(monkeypatch):
    c = TestClient(app, follow_redirects=False)
    start = c.get("/api/auth/github/start")
    state = _state_from(start.headers["location"])
    _stub_httpx(
        monkeypatch,
        post={GITHUB_TOKEN: _Resp(200, {"access_token": "tok"})},
        get={
            GITHUB_USER: _Resp(200, {"login": "nobody"}),
            GITHUB_EMAILS: _Resp(
                200, [{"email": "unverified@test.local", "verified": False, "primary": True}]
            ),
        },
    )
    r = c.get(f"/api/auth/github/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert "/login?github_error" in r.headers["location"]
    assert db.query_one("SELECT id FROM users WHERE email=?", ("unverified@test.local",)) is None


# --- _safe_next --------------------------------------------------------------


def test_safe_next_rejects_open_redirects():
    assert oauth._safe_next("/console") == "/console"
    assert oauth._safe_next("/activate?code=ABCD-1234") == "/activate?code=ABCD-1234"
    assert oauth._safe_next("//evil.com") == "/console"
    assert oauth._safe_next("https://evil.com") == "/console"
    assert oauth._safe_next("/\\evil.com") == "/console"
    assert oauth._safe_next("") == "/console"
