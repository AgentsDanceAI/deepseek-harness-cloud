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

from app import credits, db, plans, teams, work_access  # noqa: E402
from app.payments import base as pay_base  # noqa: E402

db.ensure_schema()


def _user(uid):
    with db.tx() as c:
        c.execute("DELETE FROM org_members WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM credit_grants WHERE user_id=?", (uid,))
        c.execute("DELETE FROM usage_log WHERE user_id=?", (uid,))
        c.execute("DELETE FROM minute_grants WHERE user_id=?", (uid,))
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


def test_member_cap_stops_that_member_only():
    """Pooled billing is only fair if one person cannot spend the team's month.
    Hitting a cap must block the capped member and leave the others working."""
    owner = _user("u_t_cap_o")
    heavy = _user("u_t_cap_h")
    light = _user("u_t_cap_l")
    org_id = teams.create_org(owner, "CapCo")
    teams.set_seats(org_id, 9, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), heavy)
    teams.accept_invite(teams.create_invite(org_id), light)
    teams.grant_pool(org_id, 10_000, 3600)
    teams.set_default_caps(org_id, credit_cap=100)

    credits.spend(heavy, 100, kind="llm", model="m")
    assert teams.credit_cap_exceeded(heavy) is True
    assert teams.credit_cap_exceeded(light) is False      # unaffected
    assert plans.check_run_blocked(heavy) == "member_cap_reached"
    assert plans.check_run_blocked(light) is None
    assert teams.pool_balance(org_id) == 9_900             # pool still has plenty


def test_member_override_beats_the_org_default():
    owner = _user("u_t_cap2_o")
    member = _user("u_t_cap2_m")
    org_id = teams.create_org(owner, "CapCo2")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)
    teams.grant_pool(org_id, 10_000, 3600)
    teams.set_default_caps(org_id, credit_cap=50)

    credits.spend(member, 60, kind="llm", model="m")
    assert teams.credit_cap_exceeded(member) is True
    teams.set_member_caps(org_id, member, credit_cap=500)   # owner raises it
    assert teams.credit_cap_exceeded(member) is False
    teams.set_member_caps(org_id, member, credit_cap=0)     # 0 = blocked
    assert teams.credit_cap_exceeded(member) is True


def test_org_pools_minutes_separately_from_credits():
    """Seats buy two resources; spending one must not move the other."""
    owner = _user("u_t_min_o")
    member = _user("u_t_min_m")
    org_id = teams.create_org(owner, "MinCo")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)
    teams.grant_pool(org_id, 5_000, 3600)
    teams.grant_minute_pool(org_id, 600, 3600)

    st = work_access.state(member)
    # org pool is ADDED to the member's own free allowance, never instead of it
    assert st["scope"] == "org"
    assert st["org_pool_minutes"] == 600
    assert st["minutes_left"] == 600 + work_access.included_minutes(member)

    credits.spend(member, 200, kind="llm", model="m")        # tokens
    assert teams.minute_pool(org_id) == 600                  # minutes untouched

    for _ in range(30):                                      # machine time
        credits.spend(member, 0, kind=work_access.MINUTE_KIND, model="dshwork")
    assert teams.pool_balance(org_id) == 4_800               # credits untouched
    assert work_access.state(member)["used_minutes"] == 30


def test_member_usage_view_splits_credits_and_minutes():
    owner = _user("u_t_view_o")
    member = _user("u_t_view_m")
    org_id = teams.create_org(owner, "ViewCo")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)
    teams.grant_pool(org_id, 5_000, 3600)

    credits.spend(member, 40, kind="llm", model="m")
    for _ in range(7):
        credits.spend(member, 0, kind=work_access.MINUTE_KIND, model="dshwork")

    row = {r["email"]: r for r in teams.member_usage(org_id, 0)}["u_t_view_m@t.local"]
    assert row["credits"] == 40 and row["minutes"] == 7


def test_seat_volume_discount_applies_to_the_fee_not_the_allowance():
    """Buying more seats lowers the seat fee; the included credits/minutes are
    real cost and must scale linearly."""
    small = pay_base.resolve_item("seats:5")
    large = pay_base.resolve_item("seats:50")
    assert large["unit_cents"] < small["unit_cents"]          # volume discount
    assert large["credits"] == small["credits"] // 5 * 50     # allowance is linear
    assert large["minutes"] == small["minutes"] // 5 * 50


def test_seat_price_never_undercuts_the_individual_plan():
    """An org buys governance, not a bulk discount — if seats were cheaper than
    a personal plan, buyers would just expense personal plans instead."""
    from app import config as cfg
    cheapest = pay_base.seat_unit_price(500)
    paid_tiers = [t for k, t in plans.pricing()["tiers"].items() if k != "free"]
    individual = min(int(t["monthly_cents"]) for t in paid_tiers)
    assert cheapest >= individual, (cheapest, individual, cfg.TEAM_SEAT_TIERS)


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


def test_joining_a_team_never_removes_your_own_allowance():
    """A member of an org with an empty pool must still have their personal
    plan minutes — joining a team cannot leave someone worse off than alone."""
    owner = _user("u_t_keep_o")
    member = _user("u_t_keep_m")
    org_id = teams.create_org(owner, "KeepCo")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)
    # no minute pool bought at all
    assert teams.minute_pool(org_id) == 0
    st = work_access.state(member)
    assert st["minutes_left"] == work_access.included_minutes(member)  # own allowance survives
    assert st["minutes_left"] > 0
    assert work_access.blocked_reason(member) is None


# --- API surface: these endpoints move money, so authorisation is the test ----

def _client_for(uid, email):
    """A TestClient carrying a session for an existing user id."""
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    c = TestClient(app)
    c.cookies.set("dhc_session", security.sign_token(uid, ttl=3600))
    return c


def test_only_the_owner_may_set_caps():
    owner = _user("u_t_api_o")
    member = _user("u_t_api_m")
    org_id = teams.create_org(owner, "ApiCo")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)

    as_member = _client_for(member, "u_t_api_m@t.local")
    r = as_member.post("/api/team/member-caps", json={"user_id": member, "credit_cap": 999999})
    assert r.status_code == 403                      # a member cannot lift their own cap

    as_owner = _client_for(owner, "u_t_api_o@t.local")
    r = as_owner.post("/api/team/member-caps", json={"user_id": member, "credit_cap": 250})
    assert r.status_code == 200
    assert teams.effective_caps(teams.org_of(member), member)[0] == 250


def test_caps_accept_null_to_fall_back_to_the_default():
    owner = _user("u_t_api2_o")
    member = _user("u_t_api2_m")
    org_id = teams.create_org(owner, "ApiCo2")
    teams.set_seats(org_id, 5, time.time() + 86400)
    teams.accept_invite(teams.create_invite(org_id), member)
    teams.set_default_caps(org_id, credit_cap=100)

    c = _client_for(owner, "u_t_api2_o@t.local")
    c.post("/api/team/member-caps", json={"user_id": member, "credit_cap": 900})
    assert teams.effective_caps(teams.org_of(member), member)[0] == 900
    c.post("/api/team/member-caps", json={"user_id": member, "credit_cap": None})
    assert teams.effective_caps(teams.org_of(member), member)[0] == 100   # back to default


def test_caps_cannot_target_someone_outside_the_org():
    owner = _user("u_t_api3_o")
    outsider = _user("u_t_api3_x")
    teams.create_org(owner, "ApiCo3")
    c = _client_for(owner, "u_t_api3_o@t.local")
    r = c.post("/api/team/member-caps", json={"user_id": outsider, "credit_cap": 10})
    assert r.status_code == 404
