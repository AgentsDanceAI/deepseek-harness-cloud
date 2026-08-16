"""Web console page tests. Environment is prepared BEFORE the app import."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types

_DATA_DIR = tempfile.mkdtemp(prefix="dhc-test-data-")
_LEGAL_DIR = tempfile.mkdtemp(prefix="dhc-test-legal-")  # empty: legal pages must show placeholder

os.environ["DHC_DEV"] = "1"
os.environ["AUTH_SECRET"] = "test"
os.environ["DHC_DATA_DIR"] = _DATA_DIR
os.environ["DHC_LEGAL_DIR"] = _LEGAL_DIR
os.environ.pop("DOWNLOAD_URL_MAC", None)
os.environ.pop("DOWNLOAD_URL_WIN", None)

# The payments API is developed in parallel; stub its router if not present yet
# so the page tests do not depend on it.
try:
    importlib.import_module("app.payments.api")
except Exception:  # pragma: no cover - only taken while payments is unfinished
    from fastapi import APIRouter

    _stub = types.ModuleType("app.payments.api")
    _stub.router = APIRouter(prefix="/api/pay")
    sys.modules["app.payments.api"] = _stub

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# --- public pages ------------------------------------------------------------

def test_landing_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "DSH Cloud" in body
    assert "桌面 AI 编程助手" in body
    assert "/static/app.css" in body
    assert "/download" in body
    assert "/legal/terms" in body


def test_landing_no_icp_when_unset(client):
    r = client.get("/")
    assert "beian.miit.gov.cn" not in r.text  # ICP_NUMBER empty by default


def test_login_page_renders(client):
    """Assert the sign-in AFFORDANCES, not their wording — the copy moves with
    design work and pinning it turned every visual change into a red suite."""
    r = client.get("/login")
    assert r.status_code == 200
    assert 'data-tab="pw"' in r.text and 'data-tab="code"' in r.text
    assert 'id="form-code"' in r.text and 'id="form-pw"' in r.text
    assert "/api/auth/google/start" in r.text and "/api/auth/github/start" in r.text


def test_login_page_offers_a_way_out(client):
    """Sign-in used to be a dead end: no header, no link home. Anyone who lands
    here by accident must be able to leave."""
    r = client.get("/login")
    assert 'href="/"' in r.text


def test_pricing_page_renders(client):
    r = client.get("/pricing")
    assert r.status_code == 200
    body = r.text
    assert "定价" in body
    assert "Pro" in body
    assert "积分包" in body
    assert "plan:pro:monthly" in body
    assert "pack:pack1000" in body


def test_activate_page_renders(client):
    r = client.get("/activate?code=AB12-CD34")
    assert r.status_code == 200
    assert "授权此设备" in r.text
    assert "拒绝" in r.text
    assert "AB12-CD34" in r.text


def test_download_page_placeholder(client):
    """With no DOWNLOAD_URL_* set, the buttons must be inert rather than linking
    somewhere broken."""
    r = client.get("/download")
    assert r.status_code == 200
    assert "disabled" in r.text


def test_download_page_with_urls(client):
    os.environ["DOWNLOAD_URL_MAC"] = "https://example.com/dsh.dmg"
    try:
        r = client.get("/download")
        assert "https://example.com/dsh.dmg" in r.text
    finally:
        os.environ.pop("DOWNLOAD_URL_MAC", None)


# --- legal pages -------------------------------------------------------------

def test_legal_pages_placeholder_when_missing(client):
    for doc in ("terms", "privacy", "refund", "aup"):
        r = client.get(f"/legal/{doc}")
        assert r.status_code == 200, doc
        assert "文档整理中" in r.text, doc


def test_legal_page_renders_markdown(client, tmp_path, monkeypatch):
    # A dedicated tmp dir per run: this test writes and deletes a fixture file,
    # and must never be able to touch the repo's real legal/ documents.
    monkeypatch.setenv("DHC_LEGAL_DIR", str(tmp_path))
    path = tmp_path / "terms.zh.md"
    path.write_text(
        "# 服务条款\n\n欢迎使用 **DSH Cloud**。\n\n- 第一条\n- 第二条\n\n"
        "| 项目 | 说明 |\n|---|---|\n| 积分 | 1 积分 = ¥0.01 |\n\n"
        "详见[隐私政策](/legal/privacy)。\n",
        encoding="utf-8",
    )
    try:
        r = client.get("/legal/terms")
        assert r.status_code == 200
        assert "文档整理中" not in r.text
        assert "<h1>服务条款</h1>" in r.text
        assert "<strong>DSH Cloud</strong>" in r.text
        assert "<li>第一条</li>" in r.text
        assert "<th>项目</th>" in r.text
        assert '<a href="/legal/privacy"' in r.text
    finally:
        path.unlink()


def test_legal_redirects(client):
    r = client.get("/privacy", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert r.headers["location"] == "/legal/privacy"
    r = client.get("/terms", follow_redirects=False)
    assert r.headers["location"] == "/legal/terms"


def test_markdown_escapes_html():
    from app.webpages import markdown_to_html

    out = markdown_to_html("<script>alert(1)</script>\n\n**bold** ok")
    assert "<script>" not in out
    assert "<strong>bold</strong>" in out


# --- auth-gated pages --------------------------------------------------------

def test_console_redirects_anonymous(client):
    r = client.get("/console", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("/login")


def test_orders_redirects_anonymous(client):
    r = client.get("/orders", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("/login")


def test_register_login_console_flow(client):
    email = "webuser@example.com"
    password = "secret-pass-123"

    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    assert client.cookies.get("dhc_session")

    # fresh client: prove password login works, then browse the console
    with TestClient(app) as c2:
        r = c2.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text

        r = c2.get("/console")
        assert r.status_code == 200
        body = r.text
        assert "控制台" in body
        assert email in body
        assert "积分余额" in body
        assert "免费版" in body  # default plan
        assert "危险区" in body

        r = c2.get("/orders")
        assert r.status_code == 200
        assert "我的订单" in r.text


def test_static_assets_served(client):
    for path in ("/static/app.css", "/static/app.js", "/static/qr.js"):
        r = client.get(path)
        assert r.status_code == 200, path
