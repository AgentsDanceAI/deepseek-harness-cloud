"""Who may run a cloud workspace, and on what terms.

The workspace is the one product that costs us real machine time, so it has its
own gate on top of credits:

  * everyone gets `WORK_FREE_MINUTES` of ACTIVE agent time on the house — the
    same meter that bills, so idle tabs never eat it (see workspace.reaper_tick);
  * once that is spent, the next task hits a paywall instead of quietly draining
    the credit balance, which is what made the meter feel like a trap;
  * a **workspace pass** lifts the gate for a fixed window. The first one is an
    intro price, and renewals are the standard price — priced per period rather
    than per minute so the person can predict the bill.

Credits still pay for the model and search calls inside the workspace; the pass
buys the machine, not the tokens.
"""
from __future__ import annotations

import time

from . import config, db, security

PASS_INTRO = "intro"
PASS_STANDARD = "standard"


def free_minutes_used(user_id: str) -> int:
    """Billed active workspace minutes ever — one usage row per active minute."""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM usage_log WHERE user_id=? AND kind='workspace'",
        (user_id,))
    return int((row["n"] if row is not None else 0) or 0)


def free_minutes_left(user_id: str) -> int:
    return max(0, config.WORK_FREE_MINUTES - free_minutes_used(user_id))


def active_pass(user_id: str) -> dict | None:
    """The unexpired pass with the furthest expiry, or None."""
    row = db.query_one(
        "SELECT id, kind, started, expires FROM work_passes "
        "WHERE user_id=? AND expires > ? ORDER BY expires DESC LIMIT 1",
        (user_id, time.time()))
    return dict(row) if row is not None else None


def has_ever_purchased(user_id: str) -> bool:
    row = db.query_one("SELECT COUNT(*) AS n FROM work_passes WHERE user_id=?", (user_id,))
    return int((row["n"] if row is not None else 0) or 0) > 0


def next_price(user_id: str) -> tuple[int, str]:
    """(minor units, kind) — the intro price applies to the first pass only."""
    if has_ever_purchased(user_id):
        return config.WORK_PASS_PRICE, PASS_STANDARD
    return config.WORK_PASS_INTRO_PRICE, PASS_INTRO


def grant_pass(user_id: str, *, kind: str, days: int | None = None,
               price: int = 0, currency: str = "", ref: str = "") -> str:
    """Open (or extend) a paid window. Extending stacks onto an unexpired pass
    so buying early never burns the remainder."""
    now = time.time()
    span = (days if days is not None else config.WORK_PASS_DAYS) * 86400
    current = active_pass(user_id)
    start = now
    expires = (current["expires"] if current else now) + span
    pass_id = security.new_id("wpass_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO work_passes (id, user_id, kind, started, expires, price, currency, ref, created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pass_id, user_id, kind, start, expires, price, currency, ref, now))
    return pass_id


def state(user_id: str) -> dict:
    """Everything the UI needs to decide between 'go' and 'show the paywall'."""
    left = free_minutes_left(user_id)
    current = active_pass(user_id)
    price, kind = next_price(user_id)
    return {
        "free_minutes_total": config.WORK_FREE_MINUTES,
        "free_minutes_left": left,
        "pass_active": current is not None,
        "pass_expires": current["expires"] if current else 0,
        "allowed": left > 0 or current is not None,
        "next_price": price,
        "next_price_kind": kind,       # "intro" only for the very first pass
        "pass_days": config.WORK_PASS_DAYS,
        "standard_price": config.WORK_PASS_PRICE,
    }


def blocked_reason(user_id: str) -> str | None:
    """None when the workspace may run, else a machine-readable reason."""
    st = state(user_id)
    return None if st["allowed"] else "work_quota"
