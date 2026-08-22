"""Externally visible HTTP security boundary contracts."""

from __future__ import annotations

import json
import os
import tempfile

from fastapi import HTTPException
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="dhc-http-security-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import config, db, security
from app.main import create_app
from app.redirects import safe_local_path


def test_security_headers_cover_html_and_api():
    client = TestClient(create_app())
    html = client.get("/")
    api = client.get("/livez")
    for response in (html, api):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in html.headers["content-security-policy"]


def test_https_pages_enable_hsts(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://example.test")
    response = TestClient(create_app()).get("/")
    assert response.headers["strict-transport-security"] == ("max-age=31536000; includeSubDomains")


def test_oversized_json_is_rejected_before_route_parsing(monkeypatch):
    monkeypatch.setattr(config, "GATEWAY_BODY_MAX_BYTES", 128, raising=False)
    response = TestClient(create_app()).post(
        "/llm/v1/chat/completions",
        content=json.dumps({"messages": [{"content": "x" * 512}]}),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "request_body_too_large"


def test_webhook_has_a_smaller_body_limit(monkeypatch):
    monkeypatch.setattr(config, "API_BODY_MAX_BYTES", 1024, raising=False)
    monkeypatch.setattr(config, "WEBHOOK_BODY_MAX_BYTES", 64, raising=False)
    response = TestClient(create_app()).post(
        "/api/pay/webhook/stripe",
        content=b"x" * 65,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_content_length_over_limit_is_rejected_without_route_auth(monkeypatch):
    monkeypatch.setattr(config, "API_BODY_MAX_BYTES", 8, raising=False)
    response = TestClient(create_app()).post(
        "/api/auth/login",
        content=b'{"email":"long@example.com"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_production_waffo_requires_webhook_key(monkeypatch):
    monkeypatch.setattr(config, "WAFFO_ENV", "prod")
    monkeypatch.setattr(config, "WAFFO_MERCHANT_ID", "merchant")
    monkeypatch.setattr(config, "WAFFO_PRIVATE_KEY", "private")
    monkeypatch.setattr(config, "WAFFO_WEBHOOK_PUBLIC_KEY", "")
    from app.main import validate_startup_config

    try:
        validate_startup_config()
    except RuntimeError as exc:
        assert "WAFFO_WEBHOOK_PUBLIC_KEY" in str(exc)
    else:
        raise AssertionError("production Waffo configuration must fail closed")


def test_inactive_partial_waffo_configuration_does_not_block_other_providers(monkeypatch):
    monkeypatch.setattr(config, "WAFFO_MERCHANT_ID", "stale-merchant")
    monkeypatch.setattr(config, "WAFFO_PRIVATE_KEY", "")
    monkeypatch.setattr(config, "WAFFO_WEBHOOK_PUBLIC_KEY", "")
    from app.main import validate_startup_config

    validate_startup_config()


def test_auth_secret_supports_a_mounted_secret_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "auth_secret"
    secret_file.write_text("mounted-secret\n", encoding="utf-8")
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.setenv("AUTH_SECRET_FILE", str(secret_file))
    assert config.auth_secret() == "mounted-secret"


def test_unsigned_test_waffo_payment_never_settles(monkeypatch):
    from app.payments import base, waffo_provider

    monkeypatch.setattr(config, "WAFFO_ENV", "test")
    monkeypatch.setattr(config, "WAFFO_WEBHOOK_PUBLIC_KEY", "")
    order = {"id": "DHF1", "amount_cents": 2900, "status": "pending"}
    monkeypatch.setattr(base, "get_order", lambda _order_id: order)
    payload = json.dumps(
        {
            "eventType": "order.completed",
            "data": {"orderMerchantExternalId": "DHF1", "total": "29.0"},
        }
    ).encode()
    try:
        waffo_provider.process_webhook(payload, "")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("unsigned payment was accepted")


def test_safe_local_path_rejects_browser_normalization_tricks():
    fallback = "/console"
    for value in (
        "https://evil.example/",
        "//evil.example/",
        r"/\evil.example/",
        "/%2f%2fevil.example/",
        "/%5cevil.example/",
        "/\x00console",
    ):
        assert safe_local_path(value, fallback) == fallback
    assert safe_local_path("/team/join?code=A", fallback) == "/team/join?code=A"


def test_email_rejects_html_metacharacters():
    response = TestClient(create_app()).post("/api/auth/email/send", json={"email": "<svg>@example.com"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_email"


def test_password_login_preserves_existing_unicode_email_identity():
    db.ensure_schema()
    email = "用户@例子.公司"
    db.query(
        "INSERT OR REPLACE INTO users (id, email, password_hash, created) VALUES (?,?,?,?)",
        ("u_legacy_unicode", email, security.hash_password("valid-password"), db.now()),
    )

    response = TestClient(create_app()).post(
        "/api/auth/login",
        json={"email": email, "password": "valid-password"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["email"] == email
