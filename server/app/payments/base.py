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


def team_terms() -> dict:
    """Seat terms from the active (per-currency) price table.

    Deliberately not a flat env default: a seat must never undercut the cheapest
    individual plan, and that threshold is a different number in ¥ than in $.
    An organisation buys governance — SSO, one invoice, member budgets, usage
    visibility — not a bulk discount on the personal product; sell it cheaper and
    buyers will simply expense personal plans instead.
    """
    p = plans.pricing()
    t = dict(p.get("team") or {})
    t.setdefault("seat_cents", config.TEAM_SEAT_PRICE)
    t.setdefault("seat_credits", config.TEAM_SEAT_CREDITS)
    t.setdefault("seat_minutes", config.TEAM_SEAT_MINUTES)
    t.setdefault("min_seats", config.TEAM_SEAT_MIN)
    t.setdefault("volume_tiers", config.TEAM_SEAT_TIERS)
    return t


def seat_unit_price(seats: int) -> int:
    """Per-seat price at this volume. Bands are (min_seats, percent_off), and the
    discount applies to the seat fee only."""
    terms = team_terms()
    price = int(terms["seat_cents"])
    for min_seats, off in sorted(terms["volume_tiers"], key=lambda b: -b[0]):
        if seats >= int(min_seats):
            return max(1, round(price * (100 - int(off)) / 100))
    return price


def resolve_item(item: str) -> dict:
    """Validates an item id against the price table. Returns {kind, amount_cents,
    currency, description, ...} — the ONLY place order amounts come from."""
    p = plans.pricing()
    parts = item.split(":")
    if parts[0] == "plan" and len(parts) == 3:
        tier, cycle = parts[1], parts[2]
        tdef = p["tiers"].get(tier)
        if not tdef or tier == "free" or cycle not in ("monthly", "yearly"):
            raise HTTPException(400, "unknown_item")
        cents = tdef.get(f"{cycle}_cents")
        if not cents:
            raise HTTPException(400, "unknown_item")
        return {"kind": "plan", "tier": tier, "cycle": cycle, "amount_cents": int(cents),
                "currency": p.get("currency", "CNY"),
                "description": f"DSH Cloud {tdef['name']} ({'年付' if cycle == 'yearly' else '月付'})"}
    if parts[0] == "pack" and len(parts) == 2:
        pdef = p["packs"].get(parts[1])
        if not pdef:
            raise HTTPException(400, "unknown_item")
        return {"kind": "pack", "pack": parts[1], "credits": int(pdef["credits"]),
                "valid_days": int(pdef.get("valid_days", 365)), "amount_cents": int(pdef["cents"]),
                "currency": p.get("currency", "CNY"), "description": f"DSH Cloud {pdef['name']}"}
    # Cloud-workspace pass: a period of machine time, priced per period so the
    # bill is predictable. The amount is NOT taken from the request — the intro
    # price applies only to someone who has never bought one (server-side check).
    # Team seats: N seats for a month. The pool credits scale with the seat
    # count, so a bigger team gets a bigger shared balance, not just more logins.
    if parts[0] == "seats" and len(parts) == 2 and parts[1].isdigit():
        terms = team_terms()
        n = max(int(terms["min_seats"]), min(int(parts[1]), 500))
        # Volume discount applies to the SEAT FEE only. The included credits and
        # minutes are real cost, so discounting them would be giving away
        # margin rather than rewarding commitment.
        unit = seat_unit_price(n)
        return {"kind": "seats", "seats": n, "cycle": "monthly",
                "amount_cents": unit * n,
                "unit_cents": unit,
                "credits": int(terms["seat_credits"]) * n,
                "minutes": int(terms["seat_minutes"]) * n,
                "currency": p.get("currency", "CNY"),
                "description": f"DSH Cloud 团队席位 × {n}（月付）"}
    if parts[0] == "workpass" and len(parts) == 2 and parts[1] == "week":
        return {"kind": "workpass", "days": config.WORK_PASS_DAYS,
                "amount_cents": config.WORK_PASS_INTRO_PRICE,
                "standard_cents": config.WORK_PASS_PRICE,
                "currency": p.get("currency", "CNY"),
                "description": f"DSH Cloud 云工作台 {config.WORK_PASS_DAYS} 天通行证"}
    raise HTTPException(400, "unknown_item")


def price_for(user_id: str, info: dict) -> int:
    """The amount this user actually owes. Only the workspace pass varies: the
    intro price is a first-purchase offer, so it is decided here from stored
    history — never from anything the client sent."""
    if info.get("kind") != "workpass":
        return int(info["amount_cents"])
    price, _kind = work_access.next_price(user_id)
    return int(price)


def create_order(user_id: str, provider: str, item: str) -> dict:
    info = resolve_item(item)
    info["amount_cents"] = price_for(user_id, info)
    order_id = ORDER_PREFIX[provider] + time.strftime("%y%m%d") + secrets.token_hex(5).upper()
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO orders (id, user_id, provider, item, amount_cents, currency, status, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (order_id, user_id, provider, item, info["amount_cents"], info["currency"], "pending", now))
    return {"order_id": order_id, **info}


def mark_paid(order_id: str, provider_ref: str = "") -> bool:
    """First pending->paid transition returns True and the caller MUST fulfil
    exactly then. Repeat webhooks return False and change nothing."""
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='paid', provider_ref=?, paid_at=? WHERE id=? AND status='pending'",
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
    info = resolve_item(order["item"])
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
        terms = team_terms()
        teams.set_default_caps(
            org_id,
            credit_cap=int(int(terms["seat_credits"]) * config.TEAM_DEFAULT_CREDIT_CAP_X),
            minute_cap=int(int(terms["seat_minutes"]) * config.TEAM_DEFAULT_MINUTE_CAP_X))
    elif info["kind"] == "workpass":
        work_access.grant_pass(
            order["user_id"],
            kind=work_access.PASS_INTRO if order["amount_cents"] <= config.WORK_PASS_INTRO_PRICE
            else work_access.PASS_STANDARD,
            days=info["days"], price=order["amount_cents"],
            currency=order["currency"], ref=order_id)
    else:
        credits.grant(order["user_id"], info["credits"], info["valid_days"] * 86400,
                      kind="grant_topup", ref=order_id)


def get_order(order_id: str, user_id: str | None = None):
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None or (user_id is not None and order["user_id"] != user_id):
        return None
    return dict(order)
