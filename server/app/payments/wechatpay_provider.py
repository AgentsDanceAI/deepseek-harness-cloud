"""WeChat Pay APIv3 Native (QR) without the SDK.

Security model:
  - Requests are signed WECHATPAY2-SHA256-RSA2048 (merchant private key from
    WECHAT_PAY_PRIVATE_KEY_PATH, serial WECHAT_PAY_SERIAL_NO); amounts come
    from the order row only.
  - Webhook: the notify resource is decrypted with AES-256-GCM using the APIv3
    key. Platform-certificate signature verification is deliberately skipped —
    instead the order is ALWAYS re-queried from the merchant API over TLS
    (GET /v3/pay/transactions/out-trade-no/...) and only trade_state=SUCCESS
    fulfils, so an unauthenticated notification alone cannot grant value.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from .. import config

API_BASE = "https://api.mch.weixin.qq.com"


def _private_key():
    try:
        return serialization.load_pem_private_key(
            Path(config.WECHAT_PAY_PRIVATE_KEY_PATH).read_bytes(), password=None
        )
    except (OSError, ValueError):
        raise HTTPException(500, "wechat_key_invalid") from None


def _auth_header(method: str, path: str, body: str) -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    msg = f"{method}\n{path}\n{ts}\n{nonce}\n{body}\n"
    sig = base64.b64encode(_private_key().sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    return (
        f'WECHATPAY2-SHA256-RSA2048 mchid="{config.WECHAT_PAY_MCHID}",nonce_str="{nonce}",'
        f'signature="{sig}",timestamp="{ts}",serial_no="{config.WECHAT_PAY_SERIAL_NO}"'
    )


def create_native(order: dict) -> str:
    """POST /v3/pay/transactions/native; returns the QR code_url."""
    path = "/v3/pay/transactions/native"
    body = json.dumps(
        {
            "appid": config.WECHAT_PAY_APPID,
            "mchid": config.WECHAT_PAY_MCHID,
            "description": order["description"],
            "out_trade_no": order["order_id"],
            "notify_url": f"{config.PUBLIC_BASE}/api/pay/webhook/wechat",
            "amount": {"total": order["amount_cents"], "currency": "CNY"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    r = httpx.post(
        API_BASE + path,
        content=body,
        timeout=30,
        headers={
            "Authorization": _auth_header("POST", path, body),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    if r.status_code >= 300:
        raise HTTPException(502, "wechat_error")
    code_url = r.json().get("code_url")
    if not code_url:
        raise HTTPException(502, "wechat_error")
    return code_url


def query_order(order_id: str) -> dict:
    path = f"/v3/pay/transactions/out-trade-no/{order_id}?mchid={config.WECHAT_PAY_MCHID}"
    r = httpx.get(
        API_BASE + path,
        timeout=30,
        headers={"Authorization": _auth_header("GET", path, ""), "Accept": "application/json"},
    )
    if r.status_code >= 300:
        raise HTTPException(502, "wechat_error")
    return r.json()


def decrypt_resource(resource: dict) -> dict:
    aes = AESGCM(config.WECHAT_PAY_APIV3_KEY.encode())
    plain = aes.decrypt(
        resource["nonce"].encode(),
        base64.b64decode(resource["ciphertext"]),
        str(resource.get("associated_data", "")).encode(),
    )
    return json.loads(plain)


def process_webhook(body: dict) -> dict | None:
    """Decrypt + confirm. Returns {"event": "paid"|"refund", "order_id", ...} or None."""
    try:
        data = decrypt_resource(body.get("resource") or {})
    except Exception:
        raise HTTPException(400, "bad_ciphertext") from None
    order_id = str(data.get("out_trade_no", ""))
    if not order_id:
        return None
    if str(body.get("event_type", "")).startswith("REFUND"):
        if data.get("refund_status") == "SUCCESS":
            return {"event": "refund", "order_id": order_id}
        return None
    q = query_order(order_id)  # authoritative state — never trust the notify alone
    if q.get("trade_state") == "SUCCESS":
        return {"event": "paid", "order_id": order_id, "provider_ref": str(q.get("transaction_id", ""))}
    return None
