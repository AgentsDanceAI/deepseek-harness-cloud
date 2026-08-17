"""The cloud-workspace gate: machine time is its own resource, not credits.

The rules that must hold no matter what the client sends:
  * a workspace minute NEVER costs credits, and a token call never costs
    minutes — the two meters are independent (GitHub-Actions shape);
  * the included allowance comes from the plan tier;
  * purchased minute packs extend past the allowance;
  * the intro pass price is a first-purchase offer, decided server-side;
  * a renewal bought early extends, it never burns the remainder.
"""
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="dhc-wa-")
os.environ.update({
    "DHC_DEV": "1",
    "AUTH_SECRET": "test-secret",
    "DHC_DATA_DIR": _TMP,
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "UPSTREAM_API_KEY": "sk-upstream-test",
    "FREE_SIGNUP_CREDITS": "500",
})

import pytest  # noqa: E402

from app import config, credits, db, plans, work_access  # noqa: E402
from app.payments import base as pay_base  # noqa: E402

db.ensure_schema()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # Included minutes now come from the price table (the real source), so the
    # tests pin a small table rather than the config fallback — otherwise every
    # allowance change in pricing.json would rewrite these numbers.
    table = dict(plans.pricing())
    tiers = {k: dict(v) for k, v in table["tiers"].items()}
    tiers["free"]["work_minutes"] = 120
    tiers["pro"]["work_minutes"] = 3600
    table["tiers"] = tiers
    monkeypatch.setattr(plans, "pricing", lambda: table)
    monkeypatch.setattr(config, "WORK_FREE_MINUTES", 120)
    monkeypatch.setattr(config, "WORK_PASS_DAYS", 7)
    monkeypatch.setattr(config, "WORK_PASS_INTRO_PRICE", 200)
    monkeypatch.setattr(config, "WORK_PASS_PRICE", 900)


def _user(uid):
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM usage_log WHERE user_id=?", (uid,))
        c.execute("DELETE FROM work_passes WHERE user_id=?", (uid,))
        c.execute("DELETE FROM minute_grants WHERE user_id=?", (uid,))
        c.execute("DELETE FROM credit_grants WHERE user_id=?", (uid,))
        c.execute("DELETE FROM subscriptions WHERE user_id=?", (uid,))
        c.execute("INSERT INTO users (id,email,session_epoch,created) VALUES (?,?,0,0)",
                  (uid, uid + "@t.local"))
    return uid


def _burn(uid, minutes):
    """Consume active workspace minutes the same way the reaper meters them."""
    for _ in range(minutes):
        credits.spend(uid, 0, kind=work_access.MINUTE_KIND, model="dshwork")
        work_access.consume_minute(uid)


def test_included_allowance_then_paywall():
    uid = _user("u_wa1")
    assert work_access.blocked_reason(uid) is None
    _burn(uid, 119)
    assert work_access.state(uid)["minutes_left"] == 1
    assert work_access.blocked_reason(uid) is None
    _burn(uid, 1)
    assert work_access.state(uid)["minutes_left"] == 0
    assert work_access.blocked_reason(uid) == "work_quota"


def test_machine_time_never_costs_credits():
    """The whole point of the split: a workspace minute is metered in minutes,
    so a long-running container can never drain the token balance."""
    uid = _user("u_wa2")
    credits.grant(uid, 1000, 3600, kind="grant_signup")
    before = credits.balance(uid)
    _burn(uid, 60)
    assert credits.balance(uid) == before
    assert work_access.used_minutes(uid) == 60


def test_token_spend_never_eats_the_minute_allowance():
    """And the reverse: model and search calls must not consume machine time."""
    uid = _user("u_wa2b")
    credits.grant(uid, 5000, 3600, kind="grant_signup")
    for _ in range(50):
        credits.spend(uid, 1, kind="llm", model="deepseek-v4-flash")
        credits.spend(uid, 5, kind="search", model="web_search:zhipu")
    assert work_access.used_minutes(uid) == 0
    assert work_access.state(uid)["minutes_left"] == 120


def test_plan_tier_sets_the_allowance():
    """Included minutes come from the plan, GitHub-Actions style."""
    uid = _user("u_wa2c")
    assert work_access.included_minutes(uid) == 120          # free
    plans.apply_plan(uid, "pro", "monthly")
    assert work_access.included_minutes(uid) == 3600         # pro: 60h
    st = work_access.state(uid)
    assert st["plan_tier"] == "pro" and st["minutes_left"] == 3600


def test_purchased_minutes_extend_beyond_the_plan():
    uid = _user("u_wa2d")
    _burn(uid, 120)                                          # allowance spent
    assert work_access.blocked_reason(uid) == "work_quota"
    work_access.grant_minutes(uid, 300, 30 * 86400, kind="pack")
    assert work_access.blocked_reason(uid) is None
    assert work_access.state(uid)["minutes_left"] == 300
    _burn(uid, 10)
    assert work_access.minute_packs_left(uid) == 290


def test_pass_lifts_the_gate_and_expires():
    uid = _user("u_wa3")
    _burn(uid, 120)
    assert work_access.blocked_reason(uid) == "work_quota"
    work_access.grant_pass(uid, kind=work_access.PASS_INTRO, days=7)
    assert work_access.blocked_reason(uid) is None
    # an expired pass must not keep the gate open
    with db.tx() as c:
        c.execute("UPDATE work_passes SET expires=? WHERE user_id=?", (time.time() - 1, uid))
    assert work_access.blocked_reason(uid) == "work_quota"


def test_intro_price_is_first_purchase_only():
    uid = _user("u_wa4")
    price, kind = work_access.next_price(uid)
    assert (price, kind) == (200, work_access.PASS_INTRO)
    work_access.grant_pass(uid, kind=work_access.PASS_INTRO, days=7)
    price, kind = work_access.next_price(uid)
    assert (price, kind) == (900, work_access.PASS_STANDARD)


def test_order_amount_ignores_the_client_and_follows_history():
    """The price is decided from stored purchases, never from the request."""
    uid = _user("u_wa5")
    info = pay_base.resolve_item("workpass:week")
    assert pay_base.price_for(uid, info) == 200          # first one: intro
    work_access.grant_pass(uid, kind=work_access.PASS_INTRO, days=7)
    assert pay_base.price_for(uid, info) == 900          # renewals: standard


def test_renewal_extends_instead_of_overwriting():
    uid = _user("u_wa6")
    work_access.grant_pass(uid, kind=work_access.PASS_INTRO, days=7)
    first = work_access.active_pass(uid)["expires"]
    work_access.grant_pass(uid, kind=work_access.PASS_STANDARD, days=7)
    second = work_access.active_pass(uid)["expires"]
    assert second - first == pytest.approx(7 * 86400, abs=5)


def test_state_reports_what_the_ui_needs():
    uid = _user("u_wa7")
    _burn(uid, 30)
    st = work_access.state(uid)
    assert st["minutes_left"] == 90
    assert st["allowed"] is True
    assert st["pass_active"] is False
    assert st["next_price"] == 200 and st["next_price_kind"] == "intro"
    assert st["standard_price"] == 900
