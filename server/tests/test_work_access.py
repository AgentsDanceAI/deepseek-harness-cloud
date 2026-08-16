"""The cloud-workspace gate: free machine time, then a paid pass.

The money rules that must hold no matter what the client sends:
  * the free allowance is spent by ACTIVE agent minutes, nothing else;
  * the intro price is a first-purchase offer, decided server-side;
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

from app import config, credits, db, work_access  # noqa: E402
from app.payments import base as pay_base  # noqa: E402

db.ensure_schema()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "WORK_FREE_MINUTES", 120)
    monkeypatch.setattr(config, "WORK_PASS_DAYS", 7)
    monkeypatch.setattr(config, "WORK_PASS_INTRO_PRICE", 200)
    monkeypatch.setattr(config, "WORK_PASS_PRICE", 900)


def _user(uid):
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM usage_log WHERE user_id=?", (uid,))
        c.execute("DELETE FROM work_passes WHERE user_id=?", (uid,))
        c.execute("INSERT INTO users (id,email,session_epoch,created) VALUES (?,?,0,0)",
                  (uid, uid + "@t.local"))
    return uid


def _burn(uid, minutes):
    """Spend active workspace minutes the same way the reaper bills them."""
    for _ in range(minutes):
        credits.spend(uid, 0, kind="workspace", model="dshwork")


def test_free_allowance_then_paywall():
    uid = _user("u_wa1")
    assert work_access.blocked_reason(uid) is None
    _burn(uid, 119)
    assert work_access.free_minutes_left(uid) == 1
    assert work_access.blocked_reason(uid) is None
    _burn(uid, 1)
    assert work_access.free_minutes_left(uid) == 0
    assert work_access.blocked_reason(uid) == "work_quota"


def test_only_workspace_minutes_consume_the_allowance():
    """Model and search spending must not eat the machine-time allowance."""
    uid = _user("u_wa2")
    for _ in range(50):
        credits.spend(uid, 1, kind="llm", model="deepseek-v4-flash")
        credits.spend(uid, 5, kind="search", model="web_search:zhipu")
    assert work_access.free_minutes_left(uid) == 120


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
    assert st["free_minutes_left"] == 90
    assert st["allowed"] is True
    assert st["pass_active"] is False
    assert st["next_price"] == 200 and st["next_price_kind"] == "intro"
    assert st["standard_price"] == 900
