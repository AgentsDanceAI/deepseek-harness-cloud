"""Payment HTTP surface: context, checkout, order polling, provider webhooks.

Security model (see base.py for the iron rules):
  - The client only ever names an item id; every amount is resolved from
    config/pricing.json server-side.
  - Checkout/order routes require a logged-in user; webhook routes are
    unauthenticated but each provider module verifies cryptographically AND
    re-queries the provider for the authoritative order state before we fulfil.
  - Fulfilment runs exactly once: only the first pending->paid transition
    (base.mark_paid -> True) calls base.fulfil.
Each webhook answers in the ack dialect its provider retries on: stripe JSON,
alipay the literal text "success", wechat {"code": "SUCCESS"}.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import config, db, plans
from ..accounts import resolve_user
from . import alipay_provider, base, stripe_provider, waffo_provider, wechatpay_provider

router = APIRouter(prefix="/api/pay", tags=["pay"])


def active_providers() -> list[str]:
    """A provider is active when its env config is complete (config.py)."""
    out = []
    if config.STRIPE_SECRET_KEY:
        out.append("stripe")
    if config.ALIPAY_APP_ID and config.ALIPAY_APP_PRIVATE_KEY:
        out.append("alipay")
    if (config.WECHAT_PAY_MCHID and config.WECHAT_PAY_APIV3_KEY and config.WECHAT_PAY_PRIVATE_KEY_PATH
            and config.WECHAT_PAY_SERIAL_NO and config.WECHAT_PAY_APPID):
        out.append("wechat")
    if config.WAFFO_MERCHANT_ID and config.WAFFO_PRIVATE_KEY:
        out.append("waffo")
    return out


@router.get("/context")
def pay_context():
    p = plans.pricing()
    return {"providers": active_providers(), "pricing": p, "currency": p.get("currency", "CNY")}


@router.post("/checkout")
def checkout(body: dict, user: dict = Depends(resolve_user)):
    item = str(body.get("item", ""))
    requested = str(body.get("provider", "") or "")
    active = active_providers()
    if not active:
        # No payment channel configured yet: record the intent so demand is
        # visible; the frontend shows "支付即将开通".
        info = base.resolve_item(item)
        order_id = "DHI" + time.strftime("%y%m%d") + secrets.token_hex(5).upper()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO orders (id, user_id, provider, item, amount_cents, currency, status, created) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (order_id, user["id"], "intent", item, info["amount_cents"], info["currency"],
                 "intent", time.time()))
        return {"order_id": order_id, "provider": None, "intent": True}
    provider = requested if requested in active else active[0]
    order = base.create_order(user["id"], provider, item)
    if provider == "stripe":
        return {"order_id": order["order_id"], "provider": "stripe",
                "pay_url": stripe_provider.create_checkout(order)}
    if provider == "alipay":
        return {"order_id": order["order_id"], "provider": "alipay",
                "pay_url": alipay_provider.create_page_pay(order)}
    if provider == "waffo":
        # create_checkout is async (httpx); this route is sync (runs in a
        # threadpool worker with no running loop), so drive it with asyncio.run.
        return {"order_id": order["order_id"], "provider": "waffo",
                "pay_url": asyncio.run(waffo_provider.create_checkout(order))}
    return {"order_id": order["order_id"], "provider": "wechat",
            "code_url": wechatpay_provider.create_native(order)}


PUBLIC_ORDER_COLS = ("id", "provider", "item", "amount_cents", "currency", "status", "created", "paid_at")


@router.get("/orders")
def list_orders(user: dict = Depends(resolve_user)):
    rows = db.query(
        f"SELECT {', '.join(PUBLIC_ORDER_COLS)} FROM orders WHERE user_id=? "
        "ORDER BY created DESC LIMIT 100", (user["id"],))
    return {"orders": [dict(r) for r in rows]}


@router.get("/orders/{order_id}")
def order_status(order_id: str, user: dict = Depends(resolve_user)):
    order = base.get_order(order_id, user["id"])
    if order is None:
        raise HTTPException(404, "order_not_found")
    return {"order": {k: order[k] for k in PUBLIC_ORDER_COLS}}


# --- webhooks (no auth; verified inside the provider modules) ----------------

def _settle(result: dict | None) -> None:
    """The one place the iron-rule state machine is driven from webhooks."""
    if not result:
        return
    if result["event"] == "paid":
        if base.mark_paid(result["order_id"], result.get("provider_ref", "")):
            base.fulfil(result["order_id"])  # exactly once, on the first transition
    elif result["event"] == "refund":
        base.mark_refunded(result["order_id"])


@router.post("/webhook/stripe")
async def webhook_stripe(request: Request):
    payload = await request.body()
    _settle(stripe_provider.process_webhook(payload, request.headers.get("stripe-signature", "")))
    return {"received": True}


@router.post("/webhook/alipay")
async def webhook_alipay(request: Request):
    form = dict(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
    try:
        _settle(alipay_provider.process_notify(form))
    except HTTPException:
        return PlainTextResponse("failure")  # alipay retries on any non-"success" body
    return PlainTextResponse("success")


@router.post("/webhook/wechat")
async def webhook_wechat(request: Request):
    try:
        body = await request.json()
        _settle(wechatpay_provider.process_webhook(body))
    except HTTPException as e:
        return JSONResponse({"code": "FAIL", "message": str(e.detail)}, status_code=e.status_code)
    except ValueError:
        return JSONResponse({"code": "FAIL", "message": "bad_json"}, status_code=400)
    return {"code": "SUCCESS", "message": "OK"}


@router.post("/webhook/waffo")
async def webhook_waffo(request: Request):
    raw = await request.body()
    # process_webhook raises HTTPException(400) on an invalid/unsigned event;
    # letting it propagate makes the route answer 400 so Waffo retries.
    _settle(waffo_provider.process_webhook(raw, request.headers.get("X-Waffo-Signature", "")))
    return {"received": True}
