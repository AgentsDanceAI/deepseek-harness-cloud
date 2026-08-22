"""Truthful liveness, readiness, and release metadata contracts."""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="dhc-health-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import config
from app.main import create_app


def test_liveness_never_claims_dependency_health():
    response = TestClient(create_app()).get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_checks_database_and_pricing(monkeypatch):
    monkeypatch.setattr(config, "PRICING_FILE", "missing.json")
    response = TestClient(create_app()).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["pricing"].startswith("error:")


def test_version_is_non_secret_release_metadata(monkeypatch):
    monkeypatch.setattr(config, "RELEASE_VERSION", "1.2.3", raising=False)
    monkeypatch.setattr(config, "RELEASE_REVISION", "abc1234", raising=False)
    response = TestClient(create_app()).get("/version")
    assert response.json() == {"version": "1.2.3", "revision": "abc1234"}


def test_legacy_api_health_is_liveness_alias():
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "deepseek-harness-cloud"}
