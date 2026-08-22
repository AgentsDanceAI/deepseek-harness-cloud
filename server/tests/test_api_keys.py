"""API key 合约：明文仅签发时可见、服务端只存哈希、支持吊销和网关认证。"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-apikeys-")
# 环境必须在 import app 之前钉好 —— config 在 import 期读 env (与其他测试同约定)
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test-secret",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "FREE_SIGNUP_CREDITS": "500",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, security  # noqa: E402
from app.main import app  # noqa: E402

from ._signup import signup  # noqa: E402


def _client(email: str) -> TestClient:
    c = TestClient(app)
    signup(c, email)
    return c


def test_create_returns_plaintext_once_and_stores_only_hash():
    c = _client("ak1@test.local")
    r = c.post("/api/auth/api-keys", json={"label": "我的集成"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("ak-")

    # 列表里只有前缀, 没有明文 —— 用户丢了只能重建, 这是刻意的
    listed = c.get("/api/auth/api-keys").json()["keys"]
    assert len(listed) == 1
    assert listed[0]["label"] == "我的集成"
    assert listed[0]["prefix"] == key[:12]
    assert "key" not in listed[0]

    # 库里存的是哈希, 不是明文
    row = db.query_one("SELECT key_hash FROM api_keys WHERE id=?", (listed[0]["id"],))
    assert row["key_hash"] == security.token_hash(key)
    assert db.query_one("SELECT COUNT(*) c FROM api_keys WHERE key_hash=?", (key,))["c"] == 0


def test_key_authenticates_both_header_styles():
    c = _client("ak2@test.local")
    key = c.post("/api/auth/api-keys", json={}).json()["key"]

    fresh = TestClient(app)  # 无 cookie, 只能靠 key
    assert fresh.get("/api/auth/me", headers={"authorization": f"Bearer {key}"}).status_code == 200
    assert fresh.get("/api/auth/me", headers={"x-api-key": key}).status_code == 200


def test_revoked_key_stops_working():
    c = _client("ak3@test.local")
    key = c.post("/api/auth/api-keys", json={}).json()["key"]
    kid = c.get("/api/auth/api-keys").json()["keys"][0]["id"]

    fresh = TestClient(app)
    assert fresh.get("/api/auth/me", headers={"x-api-key": key}).status_code == 200
    c.post("/api/auth/api-keys/revoke", json={"id": kid})
    assert fresh.get("/api/auth/me", headers={"x-api-key": key}).status_code == 401
    # 吊销后不再出现在列表里
    assert c.get("/api/auth/api-keys").json()["keys"] == []


def test_key_cannot_revoke_another_users_key():
    victim = _client("ak-victim@test.local")
    vkey_id = (
        victim.post("/api/auth/api-keys", json={}),
        victim.get("/api/auth/api-keys").json()["keys"][0]["id"],
    )[1]
    attacker = _client("ak-attacker@test.local")
    attacker.post("/api/auth/api-keys/revoke", json={"id": vkey_id})
    # 受害者的 key 仍在 —— revoke 的 WHERE 带 user_id
    assert len(victim.get("/api/auth/api-keys").json()["keys"]) == 1


def test_garbage_key_rejected():
    fresh = TestClient(app)
    for bad in ("ak-nonexistent", "ak-", "not-a-key"):
        assert fresh.get("/api/auth/me", headers={"x-api-key": bad}).status_code == 401


def test_last_used_updates():
    c = _client("ak4@test.local")
    key = c.post("/api/auth/api-keys", json={}).json()["key"]
    assert c.get("/api/auth/api-keys").json()["keys"][0]["last_used"] is None
    TestClient(app).get("/api/auth/me", headers={"x-api-key": key})
    assert c.get("/api/auth/api-keys").json()["keys"][0]["last_used"] is not None
