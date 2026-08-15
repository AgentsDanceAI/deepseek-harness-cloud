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

from .. import credits, db, plans

ORDER_PREFIX = {"stripe": "DHS", "alipay": "DHA", "wechat": "DHW"}


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
    raise HTTPException(400, "unknown_item")


def create_order(user_id: str, provider: str, item: str) -> dict:
    info = resolve_item(item)
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
    else:
        credits.grant(order["user_id"], info["credits"], info["valid_days"] * 86400,
                      kind="grant_topup", ref=order_id)


def get_order(order_id: str, user_id: str | None = None):
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None or (user_id is not None and order["user_id"] != user_id):
        return None
    return dict(order)
