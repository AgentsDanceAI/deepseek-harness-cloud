"""Stripe Checkout without the SDK — plain httpx against api.stripe.com.

Security model:
  - Checkout Sessions are created server-side; the amount comes from the order
    row (which came from pricing.json), never from the client.
  - Webhooks are verified with the Stripe-Signature scheme (HMAC-SHA256 over
    "{t}.{payload}" with STRIPE_WEBHOOK_SECRET, 300 s tolerance), then the
    session is re-fetched from Stripe and payment_status must be "paid" before
    the caller may fulfil ("verify, then confirm").
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import httpx
from fastapi import HTTPException

from .. import config, db

API_BASE = "https://api.stripe.com"
SIG_TOLERANCE_S = 300


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.STRIPE_SECRET_KEY}"}


def create_checkout(order: dict) -> str:
    """Creates a Checkout Session for the order; returns the redirect URL."""
    oid = order["order_id"]
    methods = [m.strip() for m in
               os.environ.get("STRIPE_PAYMENT_METHODS", "card,alipay,wechat_pay").split(",") if m.strip()]
    data = {
        "mode": "payment",
        "client_reference_id": oid,
        "metadata[order_id]": oid,
        "success_url": f"{config.PUBLIC_BASE}/console?pay=success&order={oid}",
        "cancel_url": f"{config.PUBLIC_BASE}/pricing?pay=cancel",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": order["currency"].lower(),
        "line_items[0][price_data][unit_amount]": str(order["amount_cents"]),
        "line_items[0][price_data][product_data][name]": order["description"],
    }
    for i, m in enumerate(methods):
        data[f"payment_method_types[{i}]"] = m
    if "wechat_pay" in methods:
        data["payment_method_options[wechat_pay][client]"] = "web"
    r = httpx.post(f"{API_BASE}/v1/checkout/sessions", data=data, headers=_headers(), timeout=30)
    if r.status_code >= 400:
        raise HTTPException(502, "stripe_error")
    url = r.json().get("url")
    if not url:
        raise HTTPException(502, "stripe_error")
    return url


def _api_get(path: str) -> dict:
    r = httpx.get(f"{API_BASE}{path}", headers=_headers(), timeout=30)
    if r.status_code >= 400:
        raise HTTPException(502, "stripe_error")
    return r.json()


def _verify_signature(payload: bytes, header: str) -> dict:
    secret = config.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(400, "webhook_not_configured")
    t, sigs = "", []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            t = v
        elif k == "v1":
            sigs.append(v)
    try:
        ts = float(t)
    except ValueError:
        raise HTTPException(400, "bad_signature")
    if not sigs or abs(time.time() - ts) > SIG_TOLERANCE_S:
        raise HTTPException(400, "bad_signature")
    expect = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expect, s) for s in sigs):
        raise HTTPException(400, "bad_signature")
    try:
        return json.loads(payload)
    except ValueError:
        raise HTTPException(400, "bad_payload")


def process_webhook(payload: bytes, sig_header: str) -> dict | None:
    """Verify + confirm. Returns {"event": "paid"|"refund", "order_id", ...} or None."""
    event = _verify_signature(payload, sig_header)
    obj = event.get("data", {}).get("object", {}) or {}
    etype = event.get("type", "")
    if etype == "checkout.session.completed":
        order_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("order_id", "")
        session_id = obj.get("id", "")
        if not order_id or not session_id:
            return None
        session = _api_get(f"/v1/checkout/sessions/{session_id}")  # authoritative state
        if session.get("payment_status") != "paid":
            return None
        return {"event": "paid", "order_id": order_id,
                "provider_ref": str(session.get("payment_intent") or session_id)}
    if etype == "charge.refunded":
        pi = str(obj.get("payment_intent") or "")
        row = db.query_one("SELECT id FROM orders WHERE provider_ref=?", (pi,)) if pi else None
        if row:
            return {"event": "refund", "order_id": row["id"]}
        oid = (obj.get("metadata") or {}).get("order_id", "")
        if oid:
            return {"event": "refund", "order_id": oid}
    return None
