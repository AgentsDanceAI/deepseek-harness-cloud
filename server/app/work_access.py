"""Cloud-workspace machine time — a resource of its own, not credits.

Two different things cost us money, and mixing them into one number made the
bill unreadable:

  * **tokens** (model + search) — elastic, priced per call → credits;
  * **machine time** — a container reserving RAM and CPU → minutes.

So minutes are metered like GitHub Actions: every plan includes an allowance per
billing period, and running out means "upgrade or buy more time", never "your
credits quietly drained". Credits are never charged for a workspace minute.

Only ACTIVE minutes count — a minute in which the agent actually called our
gateway (see workspace.reaper_tick). Reading a reply or leaving a tab open is
free, which is what makes an hourly allowance honest.

Organisations pool minutes the same way they pool credits: seats contribute to
one balance, drawn by whoever works, bounded per member so a single person
cannot spend the team's month.
"""
from __future__ import annotations

import time

from . import config, db, plans, security

PASS_INTRO = "intro"
PASS_STANDARD = "standard"

# usage_log rows written by the reaper carry this kind; one row == one minute.
MINUTE_KIND = "workspace"


# --- billing period ----------------------------------------------------------

def period_start(user_id: str) -> float:
    """Start of the current allowance window.

    A paid plan's window follows its own renewal date, so someone who subscribes
    on the 20th is not handed a fresh allowance on the 1st. Free users ride the
    calendar month.
    """
    plan = plans.current_plan(user_id)
    expires = float(plan.get("expires") or 0)
    if plan.get("tier") != "free" and expires > 0:
        days = 366 if plan.get("cycle") == "yearly" else 31
        return expires - days * 86400
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


# --- allowance ---------------------------------------------------------------

def included_minutes(user_id: str) -> int:
    """Minutes this user's plan includes per period (org seats add to this)."""
    plan = plans.current_plan(user_id)
    base = int(plan.get("work_minutes") or 0)
    if base == 0 and plan.get("tier") == "free":
        base = config.WORK_FREE_MINUTES
    return base


def used_minutes(user_id: str, since: float | None = None) -> int:
    """Active workspace minutes this period — one usage_log row per minute."""
    since = period_start(user_id) if since is None else since
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM usage_log WHERE user_id=? AND kind=? AND created>?",
        (user_id, MINUTE_KIND, since))
    return int((row["n"] if row is not None else 0) or 0)


def minute_packs_left(user_id: str) -> int:
    """Unexpired purchased minutes (top-ups outlive the plan period)."""
    row = db.query_one(
        "SELECT COALESCE(SUM(remaining),0) AS n FROM minute_grants "
        "WHERE user_id=? AND expires>?", (user_id, time.time()))
    return int((row["n"] if row is not None else 0) or 0)


def grant_minutes(user_id: str, minutes: int, ttl_s: float, kind: str, ref: str = "") -> str:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    gid = security.new_id("mgrant_")
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO minute_grants (id, user_id, amount, remaining, expires, kind, ref, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (gid, user_id, minutes, minutes, now + ttl_s, kind, ref, now))
    return gid


def consume_minute(user_id: str) -> None:
    """Draw one minute: the plan allowance first, then purchased packs.

    Called after the minute has already been served — we never interrupt work in
    flight; the gate below decides whether the NEXT task may start.
    """
    if used_minutes(user_id) <= included_minutes(user_id):
        return  # still inside the plan's allowance; nothing to decrement
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM minute_grants WHERE user_id=? AND expires>? AND remaining>0 "
            "ORDER BY expires ASC LIMIT 1", (user_id, time.time())).fetchone()
        if row is not None:
            conn.execute("UPDATE minute_grants SET remaining=remaining-1 WHERE id=?", (row["id"],))


# --- passes (the no-subscription entry point) --------------------------------

def active_pass(user_id: str) -> dict | None:
    row = db.query_one(
        "SELECT id, kind, started, expires FROM work_passes "
        "WHERE user_id=? AND expires > ? ORDER BY expires DESC LIMIT 1",
        (user_id, time.time()))
    return dict(row) if row is not None else None


def has_ever_purchased(user_id: str) -> bool:
    row = db.query_one("SELECT COUNT(*) AS n FROM work_passes WHERE user_id=?", (user_id,))
    return int((row["n"] if row is not None else 0) or 0) > 0


def next_price(user_id: str) -> tuple[int, str]:
    """(minor units, kind) — the intro price is a first-purchase offer."""
    if has_ever_purchased(user_id):
        return config.WORK_PASS_PRICE, PASS_STANDARD
    return config.WORK_PASS_INTRO_PRICE, PASS_INTRO


def grant_pass(user_id: str, *, kind: str, days: int | None = None,
               price: int = 0, currency: str = "", ref: str = "") -> str:
    """Open (or extend) a pass window. Buying early stacks rather than burns."""
    now = time.time()
    span = (days if days is not None else config.WORK_PASS_DAYS) * 86400
    current = active_pass(user_id)
    expires = (current["expires"] if current else now) + span
    pass_id = security.new_id("wpass_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO work_passes (id, user_id, kind, started, expires, price, currency, ref, created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pass_id, user_id, kind, now, expires, price, currency, ref, now))
    return pass_id


# --- the gate ----------------------------------------------------------------

def state(user_id: str) -> dict:
    """Everything the UI needs to show the meter and decide go / paywall.

    A member of an organisation gets the org pool ON TOP of their own plan
    allowance, never instead of it — joining a team must not leave someone worse
    off than they were alone, which is what happens if the pool is empty and the
    personal allowance is ignored.
    """
    from . import teams
    org = teams.org_of(user_id)
    if org is not None:
        return teams.work_state(org, user_id, personal=_personal_state(user_id))

    return _personal_state(user_id)


def _personal_state(user_id: str) -> dict:
    included = included_minutes(user_id)
    used = used_minutes(user_id)
    packs = minute_packs_left(user_id)
    current = active_pass(user_id)
    price, kind = next_price(user_id)
    plan = plans.current_plan(user_id)
    left = max(0, included - used) + packs
    return {
        "scope": "personal",
        "plan_tier": plan.get("tier", "free"),
        "plan_name": plan.get("name", "Free"),
        "included_minutes": included,
        "used_minutes": used,
        "pack_minutes": packs,
        "minutes_left": left,
        "period_start": period_start(user_id),
        "pass_active": current is not None,
        "pass_expires": current["expires"] if current else 0,
        "allowed": left > 0 or current is not None,
        "next_price": price,
        "next_price_kind": kind,
        "pass_days": config.WORK_PASS_DAYS,
        "standard_price": config.WORK_PASS_PRICE,
        # kept for older callers/templates that still read the free-hours wording
        "free_minutes_total": included,
        "free_minutes_left": left,
    }


def blocked_reason(user_id: str) -> str | None:
    """None when a new workspace task may start, else a machine-readable reason."""
    st = state(user_id)
    if st["allowed"]:
        return None
    return st.get("blocked_reason") or "work_quota"
