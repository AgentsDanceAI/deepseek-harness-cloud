"""Order kernel shared by every payment provider.

Three iron rules (carried from a sibling production system production):
  1. Amounts come from config/pricing.json only; the client picks an item id.
  2. A webhook is verified first, then the provider is queried for the order
     state before any fulfilment ("verify, then confirm").
  3. Idempotence: only the first pending->paid transition fulfils. Terminal
     states never regress; refunded is the only exit from paid.

Order ids: DHS... Stripe, DHA... Alipay, DHW... WeChat Pay.
Item encoding: "plan:<tier>:<cycle>" or "pack:<pack_id>".
"""
from __future__ import annotations

import secrets
import time

from fastapi import HTTPException

from .. import config, credits, db, plans, teams, work_access

ORDER_PREFIX = {"stripe": "DHS", "alipay": "DHA", "wechat": "DHW", "waffo": "DHF"}


def team_terms(cur: str | None = None) -> dict:
    """Seat terms from the price table the buyer was quoted in.

    Deliberately not a flat env default: a seat must never undercut the cheapest
    individual plan, and that threshold is a different number in ¥ than in $.
    An organisation buys governance — SSO, one invoice, member budgets, usage
    visibility — not a bulk discount on the personal product; sell it cheaper and
    buyers will simply expense personal plans instead.
    """
    p = plans.pricing(cur)
    t = dict(p.get("team") or {})
    t.setdefault("seat_cents", config.TEAM_SEAT_PRICE)
    t.setdefault("seat_credits", config.TEAM_SEAT_CREDITS)
    t.setdefault("seat_minutes", config.TEAM_SEAT_MINUTES)
    t.setdefault("min_seats", config.TEAM_SEAT_MIN)
    t.setdefault("volume_tiers", config.TEAM_SEAT_TIERS)
    return t


def seat_unit_price(seats: int, cur: str | None = None) -> int:
    """Per-seat price at this volume. Bands are (min_seats, percent_off), and the
    discount applies to the seat fee only."""
    terms = team_terms(cur)
    price = int(terms["seat_cents"])
    for min_seats, off in sorted(terms["volume_tiers"], key=lambda b: -b[0]):
        if seats >= int(min_seats):
            return max(1, round(price * (100 - int(off)) / 100))
    return price


def resolve_item(item: str, cur: str | None = None) -> dict:
    """Validates an item id against the price table. Returns {kind, amount_cents,
    currency, description, ...} — the ONLY place order amounts come from.

    `cur` is the currency the visitor was QUOTED in, resolved server-side from
    their request (never sent by the client — that would let a caller shop the
    six tables for the cheapest one). Passing None keeps the default table,
    which is what fulfilment uses: quotas are identical across currencies, so
    only the amount depends on this.
    """
    p = plans.pricing(cur)
    parts = item.split(":")
    if parts[0] == "plan" and len(parts) == 3:
        tier, cycle = parts[1], parts[2]
        tdef = p["tiers"].get(tier)
        if not tdef or tier == "free" or cycle not in ("monthly", "yearly"):
            raise HTTPException(400, "unknown_item")
        cents = tdef.get(f"{cycle}_cents")
        if not cents:
            raise HTTPException(400, "unknown_item")
        # `intro_cents` is the ADVERTISED first-month price, carried alongside the
        # standard one so price_for can decide between them from stored history.
        # It is never the amount on its own: an item nobody is eligible for still
        # has to resolve to something chargeable.
        intro = int(tdef.get("monthly_intro_cents") or 0) if cycle == "monthly" else 0
        return {"kind": "plan", "tier": tier, "cycle": cycle, "amount_cents": int(cents),
                "intro_cents": intro if 0 < intro < int(cents) else 0,
                "currency": p.get("currency", "CNY"),
                "description": f"deepseek-harness-cloud {tdef['name']} ({'年付' if cycle == 'yearly' else '月付'})"}
    if parts[0] == "pack" and len(parts) == 2:
        pdef = p["packs"].get(parts[1])
        if not pdef:
            raise HTTPException(400, "unknown_item")
        return {"kind": "pack", "pack": parts[1], "credits": int(pdef["credits"]),
                "valid_days": int(pdef.get("valid_days", 365)), "amount_cents": int(pdef["cents"]),
                "currency": p.get("currency", "CNY"), "description": f"deepseek-harness-cloud {pdef['name']}"}
    # Team seats: N seats for a month. The pool credits scale with the seat
    # count, so a bigger team gets a bigger shared balance, not just more logins.
    if parts[0] == "seats" and len(parts) == 2 and parts[1].isdigit():
        terms = team_terms(cur)
        n = max(int(terms["min_seats"]), min(int(parts[1]), 500))
        # Volume discount applies to the SEAT FEE only. The included credits and
        # minutes are real cost, so discounting them would be giving away
        # margin rather than rewarding commitment.
        unit = seat_unit_price(n, cur)
        return {"kind": "seats", "seats": n, "cycle": "monthly",
                "amount_cents": unit * n,
                "unit_cents": unit,
                "credits": int(terms["seat_credits"]) * n,
                "minutes": int(terms["seat_minutes"]) * n,
                "currency": p.get("currency", "CNY"),
                "description": f"deepseek-harness-cloud 团队席位 × {n}（月付）"}
    raise HTTPException(400, "unknown_item")


def intro_eligible(user_id: str, tier: str) -> bool:
    """Has this user never paid for a month of `tier`?

    Per tier, not per account: the first month of Plus and the first month of Pro
    are two separate offers, and someone who tried Plus has not yet been sold Pro.

    Only 'paid' counts. A refunded order gives the offer back — we did not end up
    selling that month, and holding the discount against the buyer turns a refund
    into a second, silent penalty. 'pending'/'expired' rows are abandoned
    checkouts; treating those as consumed would let anyone burn a stranger's
    offer, or their own by closing a tab.
    """
    row = db.query_one(
        "SELECT 1 FROM orders WHERE user_id=? AND item=? AND status='paid' LIMIT 1",
        (user_id, f"plan:{tier}:monthly"))
    return row is None


def intro_eligibility(user_id: str, cur: str | None = None) -> dict[str, bool]:
    """Per-tier eligibility for the pricing page, so a repeat buyer is shown the
    price they will actually be charged. The page defaults to eligible (the
    common case, and what a logged-out visitor is quoted); this narrows it."""
    tiers = plans.pricing(cur).get("tiers") or {}
    return {t: intro_eligible(user_id, t) for t in tiers if t != "free"}


def price_for(user_id: str, info: dict) -> int:
    """The amount this user actually owes. Only a monthly plan varies: the intro
    price is a first-purchase offer, so it is decided here from stored history —
    never from anything the client sent.

    This function is the reason the pricing page may advertise a first-month
    price at all. Without it the card struck through the standard price, showed
    the intro one, and the order still charged the standard one.
    """
    amount = int(info["amount_cents"])
    intro = int(info.get("intro_cents") or 0)
    if not intro or info.get("kind") != "plan" or info.get("cycle") != "monthly":
        return amount
    return intro if intro_eligible(user_id, str(info["tier"])) else amount


def create_order(user_id: str, provider: str, item: str, cur: str | None = None) -> dict:
    info = resolve_item(item, cur)
    info["amount_cents"] = price_for(user_id, info)
    order_id = ORDER_PREFIX[provider] + time.strftime("%y%m%d") + secrets.token_hex(5).upper()
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO orders (id, user_id, provider, item, amount_cents, currency, status, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (order_id, user_id, provider, item, info["amount_cents"], info["currency"], "pending", now))
    return {"order_id": order_id, **info}


# A checkout the buyer walked away from stays 'pending' forever otherwise, and
# their order list fills up with rows that will never resolve. Well past any
# provider's session lifetime, so nothing still payable is swept.
PENDING_TTL_S = 24 * 3600


def expire_stale_pending(user_id: str) -> int:
    """Retire abandoned checkouts. Safe because mark_paid accepts 'expired'
    too — a webhook that arrives after the sweep still fulfils."""
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='expired' WHERE user_id=? AND status='pending' AND created < ?",
            (user_id, time.time() - PENDING_TTL_S))
        return cur.rowcount


def mark_paid(order_id: str, provider_ref: str = "") -> bool:
    """First transition into paid returns True and the caller MUST fulfil
    exactly then. Repeat webhooks return False and change nothing.

    'expired' is accepted alongside 'pending' on purpose: expiry is our own
    housekeeping guess, and a provider confirming a payment always outranks it.
    Refusing here would mean money taken with nothing delivered.
    """
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='paid', provider_ref=?, paid_at=? "
            "WHERE id=? AND status IN ('pending','expired')",
            (provider_ref, time.time(), order_id))
        return cur.rowcount > 0


def mark_refunded(order_id: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute("UPDATE orders SET status='refunded' WHERE id=? AND status='paid'", (order_id,))
        return cur.rowcount > 0


def fulfil(order_id: str) -> None:
    """Deliver what the order bought. Call only after mark_paid returned True."""
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None:
        raise ValueError(f"order {order_id} not found")
    # Priced in the order's OWN currency, not today's default table: the row
    # is the record of what was sold, and re-resolving it in another currency
    # would make a refund or an audit disagree with the receipt.
    info = resolve_item(order["item"], order["currency"])
    if info["kind"] == "plan":
        plans.apply_plan(order["user_id"], info["tier"], info["cycle"], order_id=order_id)
    elif info["kind"] == "seats":
        # Seats are org-scoped: create the org on first purchase so the buyer
        # never lands on "you bought seats but have nowhere to put them".
        org = teams.org_of(order["user_id"])
        if org is None:
            org_id = teams.create_org(order["user_id"], "我的团队", seats=info["seats"])
        else:
            org_id = org["id"]
        teams.set_seats(org_id, info["seats"], time.time() + 31 * 86400)
        # Seats buy BOTH resources: tokens (credits) and machine time (minutes).
        teams.grant_pool(org_id, info["credits"], 31 * 86400, ref=order_id)
        teams.grant_minute_pool(org_id, info["minutes"], 31 * 86400, ref=order_id)
        # Seed per-member ceilings so a fresh org is protected by default; the
        # owner can raise, lower, or clear them.
        terms = team_terms(order["currency"])
        teams.set_default_caps(
            org_id,
            credit_cap=int(int(terms["seat_credits"]) * config.TEAM_DEFAULT_CREDIT_CAP_X),
            minute_cap=int(int(terms["seat_minutes"]) * config.TEAM_DEFAULT_MINUTE_CAP_X))
    else:
        credits.grant(order["user_id"], info["credits"], info["valid_days"] * 86400,
                      kind="grant_topup", ref=order_id)


def get_order(order_id: str, user_id: str | None = None):
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None or (user_id is not None and order["user_id"] != user_id):
        return None
    return dict(order)
