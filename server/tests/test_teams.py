"""Organisations: seats, and one credit pool several people draw from.

The rules that decide whose money is spent:
  * a member's charges come out of the ORG pool first, personal credits second;
  * every charge is still logged against the member, so "who spent what" stays
    answerable — that is the whole point of a shared pool;
  * seats bound membership, and the owner cannot be removed.
"""
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="dhc-team-")
os.environ.update({
    "DHC_DEV": "1",
    "AUTH_SECRET": "test-secret",
    "DHC_DATA_DIR": _TMP,
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "UPSTREAM_API_KEY": "sk-upstream-test",
})

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import credits, db, teams  # noqa: E402

db.ensure_schema()


def _user(uid):
    with db.tx() as c:
        c.execute("DELETE FROM org_members WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM credit_grants WHERE user_id=?", (uid,))
        c.execute("INSERT INTO users (id,email,session_epoch,created) VALUES (?,?,0,0)",
                  (uid, uid + "@t.local"))
    return uid


def test_pool_is_spent_before_personal_credits():
    owner = _user("u_t_owner")
    org_id = teams.create_org(owner, "Acme")
    credits.grant(owner, 100, 3600, kind="grant_signup")   # personal
    teams.grant_pool(org_id, 500, 3600)                    # shared

    assert credits.balance(owner) == 600
    assert credits.personal_balance(owner) == 100

    credits.spend(owner, 300, kind="llm", model="m")
    assert teams.pool_balance(org_id) == 200
    assert credits.personal_balance(owner) == 100          # untouched


def test_personal_credits_cover_the_remainder():
    owner = _user("u_t_owner2")
    org_id = teams.create_org(owner, "Acme2")
    credits.grant(owner, 100, 3600, kind="grant_signup")
    teams.grant_pool(org_id, 50, 3600)

    credits.spend(owner, 120, kind="llm", model="m")
    assert teams.pool_balance(org_id) == 0
    assert credits.personal_balance(owner) == 30


def test_usage_is_attributed_to_the_member_not_the_org():
    owner = _user("u_t_owner3")
    member = _user("u_t_member3")
    org_id = teams.create_org(owner, "Acme3")
    code = teams.create_invite(org_id)
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(code, member)
    teams.grant_pool(org_id, 1000, 3600)

    credits.spend(member, 40, kind="llm", model="m")
    assert teams.pool_balance(org_id) == 960

    rows = {r["email"]: r["credits"] for r in teams.member_usage(org_id, 0)}
    assert rows["u_t_member3@t.local"] == 40
    assert rows["u_t_owner3@t.local"] == 0


def test_seats_bound_membership():
    owner = _user("u_t_owner4")
    outsider = _user("u_t_out4")
    org_id = teams.create_org(owner, "Acme4")   # seats default to 1, owner fills it
    code = teams.create_invite(org_id)
    with pytest.raises(HTTPException) as e:
        teams.accept_invite(code, outsider)
    assert e.value.status_code == 409

    teams.set_seats(org_id, 2, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), outsider)
    assert teams.seats_used(org_id) == 2


def test_invite_is_single_use_and_expires():
    owner = _user("u_t_owner5")
    a, b = _user("u_t_a5"), _user("u_t_b5")
    org_id = teams.create_org(owner, "Acme5")
    teams.set_seats(org_id, 9, time.time() + 86400)

    code = teams.create_invite(org_id)
    teams.accept_invite(code, a)
    with pytest.raises(HTTPException):
        teams.accept_invite(code, b)          # already used

    stale = teams.create_invite(org_id)
    with db.tx() as c:
        c.execute("UPDATE org_invites SET expires=? WHERE code=?", (time.time() - 1, stale))
    with pytest.raises(HTTPException):
        teams.accept_invite(stale, b)


def test_one_org_per_person_and_owner_cannot_be_removed():
    owner = _user("u_t_owner6")
    org_id = teams.create_org(owner, "Acme6")
    with pytest.raises(HTTPException) as e:
        teams.create_org(owner, "Second")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException):
        teams.remove_member(org_id, owner)


def test_leaving_the_org_stops_pool_access():
    owner = _user("u_t_owner7")
    member = _user("u_t_member7")
    org_id = teams.create_org(owner, "Acme7")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)
    teams.grant_pool(org_id, 800, 3600)

    assert credits.balance(member) == 800
    teams.remove_member(org_id, member)
    assert credits.balance(member) == 0
