"""Contention tests for one-time invitations and device authorization codes."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="dhc-concurrency-security-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import credits, db, security, teams
from app.main import create_app


def _insert_user(label: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    user_id = f"u_{label}_{suffix}"
    email = f"{label}.{suffix}@example.test"
    db.query(
        "INSERT INTO users (id,email,session_epoch,created) VALUES (?,?,0,?)",
        (user_id, email, time.time()),
    )
    return user_id, email


@pytest.fixture
def seed_team():
    owner_id, _ = _insert_user("owner")
    invited_id, invited_email = _insert_user("invited")
    rival_id, _ = _insert_user("rival")
    org_id = teams.create_org(owner_id, f"Race {uuid.uuid4().hex[:8]}")
    teams.set_seats(org_id, 3, time.time() + 3600)
    return {
        "org_id": org_id,
        "invited_id": invited_id,
        "rival_id": rival_id,
        "open_code": teams.create_invite(org_id),
        "bound_code": teams.create_invite(org_id, invited_email),
    }


def test_invite_is_consumed_once_under_contention(seed_team):
    org_id = seed_team["org_id"]
    contenders = (seed_team["invited_id"], seed_team["rival_id"])
    code = seed_team["open_code"]

    def join(user_id):
        try:
            return teams.accept_invite(code, user_id)
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(join, contenders))
    assert results.count(org_id) == 1
    assert len([value for value in results if value in (400, 409)]) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM org_members WHERE org_id=?", (org_id,))["n"] == 2


def test_email_bound_invite_rejects_another_account(seed_team):
    with pytest.raises(HTTPException) as exc_info:
        teams.accept_invite(seed_team["bound_code"], seed_team["rival_id"])
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "invite_email_mismatch"


def test_postgres_invite_acceptance_locks_the_org_seat_row(monkeypatch):
    statements: list[str] = []

    class Cursor:
        def __init__(self, row=None, rowcount=-1):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, _params=()):
            statements.append(sql)
            if sql.startswith("UPDATE org_invites"):
                return Cursor(rowcount=1)
            if "FROM org_invites i" in sql:
                return Cursor({"org_id": "org_lock", "email": "", "joining_email": "u@example.test"})
            if "FROM org_members WHERE user_id" in sql:
                return Cursor()
            if "SELECT seats FROM orgs" in sql:
                return Cursor({"seats": 2})
            if "COUNT(*) AS n FROM org_members" in sql:
                return Cursor({"n": 1})
            return Cursor()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(teams.config, "DB_BACKEND", "postgres")
    monkeypatch.setattr(teams.db, "tx", Transaction)

    assert teams.accept_invite("LOCKME", "u_joining") == "org_lock"
    assert any("SELECT seats FROM orgs" in sql and sql.endswith(" FOR UPDATE") for sql in statements)


def test_postgres_spend_locks_every_shared_ledger_holder(monkeypatch):
    statements: list[tuple[str, tuple]] = []

    class Cursor:
        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

        def fetchall(self):
            return []

    class Connection:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            if "COALESCE(SUM(remaining),0) AS balance" in sql:
                return Cursor({"balance": 0})
            return Cursor()

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(credits.config, "DB_BACKEND", "postgres")
    monkeypatch.setattr(credits, "_pools", lambda _user_id: ["org_shared", "u_member"])
    monkeypatch.setattr(credits.db, "tx", Transaction)

    credits.spend("u_member", 1, kind="llm")
    lock_params = [params[0] for sql, params in statements if "pg_advisory_xact_lock" in sql]
    assert lock_params == ["dsh-credit:org_shared", "dsh-credit:u_member"]


@pytest.fixture
def approved_device_code():
    user_id, _ = _insert_user("device")
    device_code = f"dc_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    now = time.time()
    db.query(
        "INSERT INTO device_codes "
        "(device_code_hash,user_code,status,user_id,client_info,expires,created) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            security.token_hash(device_code),
            f"T-{uuid.uuid4().hex[:6].upper()}",
            "approved",
            user_id,
            json.dumps({"name": "race", "platform": "test"}),
            now + 600,
            now,
        ),
    )
    client = TestClient(create_app())
    yield SimpleNamespace(device_code=device_code, user_id=user_id, client=client)
    client.close()


def test_device_code_mints_only_one_device(approved_device_code):
    def poll():
        return approved_device_code.client.post(
            "/api/device/poll", json={"device_code": approved_device_code.device_code}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: poll(), range(2)))
    approved = [
        response
        for response in responses
        if response.status_code == 200 and response.json().get("status") == "approved"
    ]
    assert len(approved) == 1
    assert (
        db.query_one(
            "SELECT COUNT(*) AS n FROM devices WHERE user_id=?",
            (approved_device_code.user_id,),
        )["n"]
        == 1
    )
