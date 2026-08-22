"""Alipay 电脑网站支付 (alipay.trade.page.pay) with RSA2 request signing.

Security model:
  - The redirect URL is built and signed server-side (SHA256withRSA over the
    sorted "k=v&..." string, `sign` excluded) with ALIPAY_APP_PRIVATE_KEY;
    amounts come from the order row only.
  - Async notify: verify the RSA2 signature with ALIPAY_PUBLIC_KEY (exclude
    sign/sign_type and empty values, sort keys), require app_id to match, then
    confirm via alipay.trade.query before the caller fulfils. The query goes
    over TLS straight to the official gateway; its response signature is not
    re-verified, so this path relies on transport authentication.
Keys may be full PEM or the bare base64 body the Alipay console hands out.
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import HTTPException

from .. import config

GATEWAY = "https://openapi.alipay.com/gateway.do"


def _pem(body: str, kind: str) -> bytes:
    body = body.strip()
    if "BEGIN" in body:
        return body.replace("\\n", "\n").encode()
    b64 = "".join(body.split())
    lines = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {kind}-----\n{lines}\n-----END {kind}-----\n".encode()


def _private_key():
    raw = config.ALIPAY_APP_PRIVATE_KEY
    for kind in ("PRIVATE KEY", "RSA PRIVATE KEY"):
        try:
            return serialization.load_pem_private_key(_pem(raw, kind), password=None)
        except ValueError:
            continue
    raise HTTPException(500, "alipay_key_invalid")


def _sign_content(params: dict, exclude: tuple) -> str:
    return "&".join(f"{k}={params[k]}" for k in sorted(params) if k not in exclude and params[k] != "")


def _sign(params: dict) -> str:
    content = _sign_content(params, exclude=("sign",))  # request signing keeps sign_type
    sig = _private_key().sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _base_params(method: str) -> dict:
    return {
        "app_id": config.ALIPAY_APP_ID,
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
    }


def create_page_pay(order: dict) -> str:
    """Returns the signed gateway redirect URL for 电脑网站支付."""
    oid = order["order_id"]
    biz = {
        "out_trade_no": oid,
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": f"{order['amount_cents'] / 100:.2f}",
        "subject": order["description"],
    }
    params = _base_params("alipay.trade.page.pay")
    params["notify_url"] = f"{config.PUBLIC_BASE}/api/pay/webhook/alipay"
    params["return_url"] = f"{config.PUBLIC_BASE}/console?pay=success&order={oid}"
    params["biz_content"] = json.dumps(biz, ensure_ascii=False, separators=(",", ":"))
    params["sign"] = _sign(params)
    return GATEWAY + "?" + urlencode(params)


def verify_notify(form: dict) -> bool:
    """RSA2 verify of an async notify (sign/sign_type and empty values excluded)."""
    try:
        pub = serialization.load_pem_public_key(_pem(config.ALIPAY_PUBLIC_KEY, "PUBLIC KEY"))
        content = _sign_content(form, exclude=("sign", "sign_type"))
        pub.verify(
            base64.b64decode(form.get("sign", "")), content.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return True
    except Exception:
        return False


def query_trade(order_id: str) -> dict:
    params = _base_params("alipay.trade.query")
    params["biz_content"] = json.dumps({"out_trade_no": order_id}, separators=(",", ":"))
    params["sign"] = _sign(params)
    r = httpx.get(GATEWAY, params=params, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(502, "alipay_error")
    return r.json().get("alipay_trade_query_response", {}) or {}


def process_notify(form: dict) -> dict | None:
    """Verify + confirm. Returns {"event": "paid"|"refund", "order_id", ...} or None."""
    if not verify_notify(form):
        raise HTTPException(400, "bad_signature")
    if form.get("app_id") != config.ALIPAY_APP_ID:
        raise HTTPException(400, "app_id_mismatch")
    order_id = form.get("out_trade_no", "")
    if not order_id:
        return None
    if form.get("refund_fee"):  # refund notify (trade_status may stay TRADE_SUCCESS)
        return {"event": "refund", "order_id": order_id}
    if form.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        q = query_trade(order_id)  # authoritative state
        if q.get("code") == "10000" and q.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            return {"event": "paid", "order_id": order_id, "provider_ref": str(q.get("trade_no", ""))}
    return None
