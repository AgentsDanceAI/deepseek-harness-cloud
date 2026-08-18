"""Waffo (waffo.ai) — overseas merchant-of-record collection channel.

Ported from a sibling production system production (backend/waffo_pay.py) onto this repo's
order kernel (payments/base.py). Waffo mechanics kept verbatim; storage,
fulfilment and idempotence are delegated to base.py — Waffo has NO table of its
own, it rides the shared `orders` table like every other provider here.

API auth (docs.waffo.ai): RSA-SHA256 request signing —
  canonical = METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + SHA256_BASE64(BODY)
  headers: X-Merchant-Id / X-Timestamp / X-Signature (Base64).
The API key is bound to test|prod at creation time, so requests carry no env header.

Webhook: X-Waffo-Signature "t=<ms>,v1=<base64>", RSA-SHA256 over f"{t}.{body}"
with a 5-minute tolerance. Reversal events (refund/dispute/chargeback) ALWAYS
require a valid signature — an unsigned reversal could be forged to strip a
paying user of what they bought.

Security baseline (same as stripe/alipay/wechat here):
  - amounts are resolved server-side (base.resolve_item), never from the client;
  - a webhook is verified, then the local pending order is matched by
    orderMerchantExternalId before any fulfilment;
  - idempotence lives in base.mark_paid/mark_refunded (first transition only);
  - private keys stay in env/config, never in the DB, code, or the frontend.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Optional

import httpx
from fastapi import HTTPException

from .. import config, db
from . import base

logger = logging.getLogger("dhc.waffo")


# ── PEM handling ───────────────────────────────────────────────────────────

def _pem(raw: str, kind: str = "PRIVATE KEY") -> str:
    """Normalise a PEM env value. Tolerates three paste shapes the Waffo
    dashboard hands out: (1) a full PEM with real newlines, (2) a single line
    with \\n escapes, (3) bare base64 with no BEGIN/END armor (the "copy" button
    often gives only the body). cryptography's load_pem_* needs the armor, so we
    add it and re-wrap at 64 chars for case (3)."""
    v = (raw or "").strip()
    if not v:
        return ""
    if "\\n" in v:
        v = v.replace("\\n", "\n")
    if "-----BEGIN" in v:
        return v
    body = "".join(v.split())
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {kind}-----\n{lines}\n-----END {kind}-----\n"


def configured() -> bool:
    return bool(config.WAFFO_MERCHANT_ID and config.WAFFO_PRIVATE_KEY)


def _api_base() -> str:
    return (config.WAFFO_API_BASE or "https://api.waffo.ai").rstrip("/")


# ── RSA-SHA256 request signing ─────────────────────────────────────────────

def sign_request(method: str, path: str, body: bytes,
                 timestamp: Optional[int] = None,
                 private_key_pem: Optional[str] = None) -> dict:
    """Build the API-key auth headers.
    canonical = METHOD\\nPATH\\nTS\\nSHA256_BASE64(BODY), signed RSA PKCS1v15 SHA256."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = int(timestamp if timestamp is not None else time.time())
    body_digest = base64.b64encode(hashlib.sha256(body or b"").digest()).decode()
    canonical = f"{method.upper()}\n{path}\n{ts}\n{body_digest}".encode()
    pem = private_key_pem if private_key_pem is not None else _pem(config.WAFFO_PRIVATE_KEY)
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    sig = key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    return {
        "X-Merchant-Id": config.WAFFO_MERCHANT_ID,
        "X-Timestamp": str(ts),
        "X-Signature": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


async def _waffo_request(path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = sign_request("POST", path, body)
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(_api_base() + path, content=body, headers=headers)
    try:
        return r.status_code, (r.json() if r.content else {})
    except ValueError:
        return r.status_code, {"raw": r.text[:500]}


# ── webhook signature verification ─────────────────────────────────────────

def verify_webhook_signature(payload: bytes, sig_header: str, public_key_pem: str,
                             tolerance: int = 300, now: Optional[float] = None) -> bool:
    """Verify RSA-SHA256 over f"{t}.{body}"; t is milliseconds; 5-minute tolerance
    guards replay. Header shape: "t=<ms>,v1=<base64>"."""
    t, v1 = "", ""
    for part in (sig_header or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            t = v
        elif k == "v1":
            v1 = v
    try:
        t_ms = int(t)
    except ValueError:
        return False
    ref = now if now is not None else time.time()
    if not v1 or abs(ref - t_ms / 1000.0) > tolerance:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        pub = serialization.load_pem_public_key(public_key_pem.encode())
        pub.verify(base64.b64decode(v1), f"{t}.".encode() + payload,
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


# ── event classification ───────────────────────────────────────────────────
# Reversal keywords match loosely (Waffo event names vary), but negative
# suffixes must be excluded: refund.failed contains "refund" yet means the
# refund did NOT happen (money still ours) — acting on it would strip a paying
# user's plan. dispute.won / chargeback.rejected are the same shape. The
# Dashboard's default subscriptions include refund.failed, so this is not
# hypothetical.
_REVERSAL_KEYS = ("refund", "dispute", "chargeback", "reversal")
_NOT_REVERSAL_SUFFIX = (".failed", "_failed", ".won", "_won", ".rejected", "_rejected")


def classify_event(etype: str) -> str:
    """Classify a webhook event type -> 'paid' | 'reversal' | 'ignore'."""
    et = (etype or "").strip().lower()
    if et == "order.completed":
        return "paid"
    if any(k in et for k in _REVERSAL_KEYS) and not any(s in et for s in _NOT_REVERSAL_SUFFIX):
        return "reversal"
    return "ignore"


# ── kv-backed product/store cache (shared kv table, portable upsert) ────────

def _kv_get(k: str) -> str:
    row = db.query_one("SELECT v FROM kv WHERE k=?", (k,))
    return row["v"] if row else ""


def _kv_set(k: str, v: str) -> None:
    with db.tx() as conn:
        cur = conn.execute("UPDATE kv SET v=? WHERE k=?", (v, k))
        if cur.rowcount == 0:
            conn.execute("INSERT INTO kv (k, v) VALUES (?,?)", (k, v))


# ── store / product resolution (checkout needs a productId) ─────────────────

async def ensure_store_id() -> str:
    """create-product needs a storeId (STO_…). Prefer the env override; else
    query the merchant's stores over GraphQL and cache — single store is taken
    directly, multiple takes the first (set WAFFO_STORE_ID to disambiguate)."""
    sid = config.WAFFO_STORE_ID or _kv_get("waffo_store_id")
    if sid:
        return sid
    status, data = await _waffo_request(
        "/v1/graphql", {"query": "query { stores { id name slug } }"})
    stores = ((data.get("data") or {}).get("stores") or []) if isinstance(data, dict) else []
    if status >= 300 or not stores:
        logger.error("[waffo] store query failed status=%s resp=%s", status, data)
        raise HTTPException(502, "waffo_store_unresolved")
    sid = str(stores[0].get("id") or "").strip()
    if len(stores) > 1:
        logger.warning("[waffo] merchant has %d stores, using first %s (%s); set "
                       "WAFFO_STORE_ID to choose", len(stores), sid, stores[0].get("name"))
    _kv_set("waffo_store_id", sid)
    logger.info("[waffo] resolved store %s (%s)", sid, stores[0].get("name"))
    return sid


def catalog_prices(item: str) -> dict:
    """Catalog placeholder prices for every currency the site quotes.

    Placeholders because checkout overrides the amount with priceSnapshot — but
    the CURRENCY KEYS are not cosmetic: create-session is rejected for a
    currency the product does not list, so a USD-only product breaks checkout
    for every visitor quoted in anything else.

    A STRING per currency, not a number. The API documents amount as "display
    format string" and rejects a JSON number with a bare {"message":"Invalid
    input"} naming no field — every create-product call failed this way until
    it was traced.
    """
    from .. import currency as _cur
    prices = {}
    for cur in _cur.SUPPORTED:
        try:
            cents = base.resolve_item(item, cur)["amount_cents"]
        except Exception:      # an item priced in some tables but not others
            continue
        prices[cur] = {"amount": f"{cents / 100:.2f}", "taxIncluded": True, "taxCategory": "saas"}
    return prices


async def ensure_product_id(item: str) -> str:
    """Resolve (or create+publish) the Waffo product backing this item. Name and
    description come from base.resolve_item — the catalog price is a placeholder,
    the real amount is overridden by priceSnapshot at checkout. Resolution order:
    env override > kv cache > reuse existing by name > auto-create+publish."""
    if config.WAFFO_PRODUCT_ID:
        return config.WAFFO_PRODUCT_ID
    cache_key = f"waffo_product:{item}"
    pid = _kv_get(cache_key)
    if pid:
        return pid
    info = base.resolve_item(item)
    name = info["description"]
    store_id = await ensure_store_id()
    # Reuse an existing product with the same name — a lost cache / a parse
    # failure on create must not spawn duplicate catalog products.
    # `limit` matters: the query defaults to 10 products, so once the catalog
    # outgrew that the scan stopped seeing most of the store and "reuse" quietly
    # became "create another one". Deactivated products are skipped — matching
    # one would hand checkout a product Waffo will not sell.
    try:
        _st, d = await _waffo_request("/v1/graphql", {
            "query": "query($s:String!){ onetimeProducts(storeId:$s, limit:200){ id name status } }",
            "variables": {"s": store_id}})
        for p in ((d.get("data") or {}).get("onetimeProducts") or []) if isinstance(d, dict) else []:
            if p.get("status") != "active":
                continue
            if str(p.get("name") or "").strip() == name:
                pid = str(p.get("id") or "").strip()
                if pid:
                    _kv_set(cache_key, pid)
                    logger.info("[waffo] reuse product item=%s id=%s", item, pid)
                    return pid
    except HTTPException:
        raise
    except Exception:
        logger.exception("[waffo] existing-product lookup failed, creating a new one")
    status, data = await _waffo_request("/v1/actions/onetime-product/create-product", {
        "storeId": store_id,
        "name": name,
        "description": name,
        # prices is keyed by ISO-4217 currency (not an array); each sellable
        # currency needs a key or its create-session is rejected. The site
        # quotes six, so all six go in — a product carrying only USD makes
        # checkout fail for exactly the visitors who were shown a local price.
        "prices": catalog_prices(item),
    })
    body = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    prod = (body or {}).get("product") if isinstance((body or {}).get("product"), dict) else (body or {})
    pid = str((prod.get("id") or prod.get("productId") or "")).strip()
    if status >= 300 or not pid:
        logger.error("[waffo] create-product failed item=%s status=%s resp=%s", item, status, data)
        raise HTTPException(502, "waffo_product_failed")
    # The field is "id", not "productId" — the docs are explicit and the API
    # answers a fieldless "Invalid input" to anything else. Publishing is
    # one-way test -> production and only works once per product.
    # publish-product copies a TEST-environment version into production. Our API
    # key writes straight to production, so there is no test version to copy and
    # the call answers "No test version found" — for us that is the normal path,
    # not a failure. Treat it as success when the product is already active, and
    # only escalate a publish error that leaves the product unusable.
    # Cache the id BEFORE publishing. Publishing failed on the first two
    # attempts here, and because the id was only cached afterwards, every retry
    # created a NEW product — the store ended up with three copies of all eleven.
    # The product exists the moment create-product returns; caching that fact is
    # what makes a retry idempotent.
    _kv_set(cache_key, pid)
    st2, d2 = await _waffo_request("/v1/actions/onetime-product/publish-product", {"id": pid})
    if st2 >= 300:
        msg = ""
        if isinstance(d2, dict):
            msg = " ".join(str(e.get("message", "")) for e in (d2.get("errors") or []))
        if "no test version" in msg.lower():
            logger.info("[waffo] product %s already lives in production; publish not needed", pid)
        else:
            logger.error("[waffo] publish-product failed item=%s status=%s resp=%s", item, st2, d2)
            raise HTTPException(502, "waffo_publish_failed")
    _kv_set(cache_key, pid)
    logger.info("[waffo] auto-created product item=%s id=%s name=%s", item, pid, name)
    return pid


# ── payment methods × currency ──────────────────────────────────────────────
# card/applepay/googlepay take USD/EUR/GBP/JPY/HKD (never CNY); wechat takes
# USD/CNY only, one-time, capped ~$140 / ¥1000 (its HKD-settled ceiling). A CNY
# order over the cap has no payable method on Waffo — the caller must block it
# before the checkout page rather than show an empty method list.
WECHAT_CAP = {"USD": 140.0, "CNY": 1000.0}
_WALLET_METHODS = ["card", "applepay", "googlepay"]


def payable(currency: str, amount_cents: int) -> bool:
    """Whether Waffo has any method for this currency at this amount.

    Verified against the API, not assumed: card, applepay and googlepay are all
    rejected for CNY with "Payment methods not supported for CNY (onetime)", so
    a yuan-quoted order above the WeChat ceiling has nowhere to go. Checking
    before the order row is written lets the page offer the buyer a way out
    instead of stranding them on a checkout that cannot be paid.
    """
    return bool(payment_methods_for(currency, amount_cents / 100))


def payment_methods_for(currency: str, amount: float) -> list[str]:
    """Checkout method whitelist. CNY -> wechat only; USD adds wechat within the
    cap; other currencies get card + both wallets. Empty list = no payable
    combination (the caller must reject)."""
    cur = (currency or "").upper()
    if cur == "CNY":
        return ["wechat"] if amount <= WECHAT_CAP["CNY"] else []
    methods = list(_WALLET_METHODS)
    if cur in WECHAT_CAP and amount <= WECHAT_CAP[cur]:
        methods.append("wechat")
    return methods


# ── checkout ────────────────────────────────────────────────────────────────

def _item_of(order: dict) -> str:
    """Reconstruct the item id from a base.create_order() output row.

    Every kind resolve_item accepts has to round-trip here. Seats and the
    workspace pass used to fall through to order['pack'] and raise KeyError,
    so neither could ever reach checkout.
    """
    kind = order.get("kind")
    if kind == "plan":
        return f"plan:{order['tier']}:{order['cycle']}"
    if kind == "seats":
        return f"seats:{order['seats']}"
    return f"pack:{order['pack']}"


async def create_checkout(order: dict) -> str:
    """order is a base.create_order(...) output. Creates a Waffo checkout session
    (amount overridden by priceSnapshot) and returns the hosted checkout URL."""
    order_id = order["order_id"]
    currency = (order["currency"] or "USD").upper()
    # Waffo priceSnapshot.amount is the currency's normal decimal (29.00 for
    # $29), so amount_cents / 100 for 2-decimal currencies like USD.
    # A STRING here too — priceSnapshot.amount is documented the same way as the
    # catalog price, and a JSON number is rejected with a fieldless
    # {"message":"Invalid input","layer":"order"}.
    amount = f"{order['amount_cents'] / 100:.2f}"
    methods = payment_methods_for(currency, float(amount))
    if not methods:
        raise HTTPException(400, "waffo_no_payment_method")
    item = _item_of(order)
    product_id = await ensure_product_id(item)
    payload = {
        "productId": product_id,
        "currency": currency,
        "priceSnapshot": {"amount": amount, "taxIncluded": True, "taxCategory": "saas"},
        "includePaymentMethods": methods,
        "successUrl": f"{config.PUBLIC_BASE}/console?pay=success&order={order_id}",
        "orderMerchantExternalId": order_id,
        "metadata": {"order_id": order_id, "item": item},
    }
    status, data = await _waffo_request("/v1/actions/checkout/create-session", payload)
    body = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    url = str((body or {}).get("checkoutUrl") or "").strip()
    if status >= 300 or not url:
        logger.error("[waffo] create-session failed order=%s status=%s resp=%s", order_id, status, data)
        raise HTTPException(502, "waffo_session_failed")
    logger.info("[waffo] checkout created order=%s %s %s", order_id, amount, currency)
    return url


# ── webhook processing (drives api._settle) ─────────────────────────────────

def process_webhook(raw: bytes, sig_header: str) -> dict | None:
    """Verify + classify + match a local order. Returns a dict for _settle
    ({"event": "paid"|"refund", "order_id", "provider_ref"}) or None to ignore.
    Raises HTTPException(400) when a reversal (or a pubkey-protected event) is
    unsigned — the webhook route surfaces that as a 400 so Waffo retries."""
    pub = _pem(config.WAFFO_WEBHOOK_PUBLIC_KEY, "PUBLIC KEY")
    verified = bool(pub) and verify_webhook_signature(raw, sig_header, pub)
    if pub and not verified:
        logger.warning("[waffo] webhook signature invalid, rejected")
        raise HTTPException(400, "invalid_signature")
    try:
        event = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "invalid_json")
    kind = classify_event(str(event.get("eventType") or ""))
    if kind == "ignore":
        return None
    data = event.get("data") or {}
    order_id = str(data.get("orderMerchantExternalId") or "").strip()
    order = base.get_order(order_id) if order_id else None
    if not order:
        logger.warning("[waffo] webhook for unknown order id=%r", order_id)
        return None
    session_id = str(data.get("sessionId") or data.get("id") or "").strip()
    if kind == "reversal":
        # Reversals can be forged to downgrade a paying user — always require a
        # valid signature.
        if not verified:
            logger.error("[waffo] unsigned reversal rejected order=%s", order_id)
            raise HTTPException(400, "signature_required")
        return {"event": "refund", "order_id": order_id, "provider_ref": session_id}
    # kind == "paid"
    if not verified:
        # No-pubkey degraded path: only outside prod AND when the amount matches
        # a local pending order. prod without a webhook pubkey never settles.
        expected = round(order["amount_cents"] / 100, 2)
        got = str(data.get("total") or data.get("amount") or "")
        amount_ok = got in (str(expected), str(order["amount_cents"]))
        if not (config.WAFFO_ENV != "prod" and amount_ok and order.get("status") == "pending"):
            logger.error("[waffo] unverified webhook rejected order=%s (set "
                         "WAFFO_WEBHOOK_PUBLIC_KEY to settle in prod)", order_id)
            raise HTTPException(400, "signature_required")
        logger.warning("[waffo] non-prod settle without signature order=%s", order_id)
    return {"event": "paid", "order_id": order_id, "provider_ref": session_id}
