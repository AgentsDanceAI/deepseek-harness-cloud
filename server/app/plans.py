"""Plan definitions and entitlement checks.

config/pricing.json is the single source of truth for prices and quotas.
Principles carried over from a sibling production system:
  - gates only block NEW requests, never kill in-flight work;
  - every check fails open if the check itself breaks;
  - amounts are always resolved server-side from the price table.
"""
from __future__ import annotations

import json
import threading
import time

from . import config, credits, db

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = 0.0


def pricing(cur: str | None = None) -> dict:
    """Price table for one currency.

    Cached per currency and hot-reloaded on mtime, so editing a table takes
    effect without a restart. `cur=None` keeps the historical behaviour (the
    PRICING_FILE default), which is what every server-side charge path uses —
    the amount a user owes must come from the table their order was priced in,
    never from whatever currency the current page happens to be showing.
    """
    key = (cur or "").upper() or "_default"
    if key == "_default":
        p = config.CONFIG_DIR / config.PRICING_FILE
    else:
        from . import currency as _cur
        p = config.CONFIG_DIR / _cur.price_file(key)
        if not p.is_file():
            p = config.CONFIG_DIR / config.PRICING_FILE
    mtime = p.stat().st_mtime
    with _lock:
        entry = _cache.get(key) if isinstance(_cache, dict) else None
        if entry is None or entry[0] != mtime:
            data = json.loads(p.read_text())
            if not isinstance(_cache, dict):
                _cache_reset()
            _cache[key] = (mtime, data)
        return _cache[key][1]


def _cache_reset() -> None:
    global _cache
    _cache = {}


def tier_def(tier: str) -> dict | None:
    return pricing()["tiers"].get(tier)


def pack_def(pack: str) -> dict | None:
    return pricing()["packs"].get(pack)


def current_plan(user_id: str) -> dict:
    """Active plan for the user; expired subscriptions fall back to free."""
    row = db.query_one("SELECT tier, cycle, expires FROM subscriptions WHERE user_id=?", (user_id,))
    if row and float(row["expires"]) > time.time() and tier_def(row["tier"]):
        d = dict(tier_def(row["tier"]))
        d.update(tier=row["tier"], cycle=row["cycle"], expires=float(row["expires"]))
        return d
    d = dict(tier_def("free") or {"concurrency": 1})
    d.update(tier="free", cycle="", expires=0.0)
    return d


def apply_plan(user_id: str, tier: str, cycle: str, order_id: str = "") -> None:
    """Idempotence lives in the order state machine (payments/base.py); this is
    the fulfilment: extend/replace the subscription and grant this period's credits."""
    tdef = tier_def(tier)
    if tdef is None or tier == "free":
        raise ValueError(f"unknown paid tier {tier!r}")
    now = time.time()
    days = 366 if cycle == "yearly" else 31
    with db.tx() as conn:
        row = conn.execute("SELECT tier, expires FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
        # Same-tier renewal extends from current expiry; upgrades restart from now.
        base = max(now, float(row["expires"])) if row and row["tier"] == tier else now
        expires = base + days * 86400
        if row:
            conn.execute("UPDATE subscriptions SET tier=?, cycle=?, expires=?, updated=? WHERE user_id=?",
                         (tier, cycle, expires, now, user_id))
        else:
            conn.execute("INSERT INTO subscriptions (user_id, tier, cycle, started, expires, updated) "
                         "VALUES (?,?,?,?,?,?)", (user_id, tier, cycle, now, expires, now))
    months = 12 if cycle == "yearly" else 1
    credits.grant(user_id, int(tdef["monthly_credits"]) * months, days * 86400,
                  kind="grant_plan", ref=order_id or f"{tier}:{cycle}")


def concurrency_limit(user_id: str) -> int:
    try:
        return int(current_plan(user_id).get("concurrency", 1))
    except Exception:
        return 1  # fail open with the floor, never block on a broken check


def check_run_blocked(user_id: str) -> str | None:
    """Returns a human-readable block reason, or None to admit. Fail-open."""
    if not config.ENTITLE_ENFORCE:
        return None
    try:
        # New requests need a positive balance; overdraft exists only so
        # in-flight streams can finish and be billed truthfully.
        if credits.balance(user_id) <= 0:
            return "insufficient_credits"
        # A member of an organisation also has their own ceiling on the shared
        # pool: hitting it stops THAT member, never the whole team.
        from . import teams
        if teams.credit_cap_exceeded(user_id):
            return "member_cap_reached"
        return None
    except Exception:
        return None
