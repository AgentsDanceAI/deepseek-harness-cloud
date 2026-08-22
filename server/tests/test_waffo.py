"""Waffo provider tests: request signing, webhook verification, event
classification, USD checkout, and the paid/refund webhook state machine.

Config reads env at import time, so the environment (and the RSA material) is
pinned BEFORE any app import. To stay correct when run inside the full suite —
where app.config may already be imported with the CNY price table by another
test module — an autouse fixture re-pins config attributes and resets the plans
price cache per test, and reverts cleanly afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return key, priv, pub


# API-signing keypair and the (separate) webhook keypair — generated locally so
# every signature path runs for real.
_API_KEY, _API_PRIV_PEM, _API_PUB_PEM = _keypair()
_WH_KEY, _WH_PRIV_PEM, _WH_PUB_PEM = _keypair()

_TMP = tempfile.mkdtemp(prefix="dhc-waffo-test-")
os.environ["DHC_DEV"] = "1"
os.environ["AUTH_SECRET"] = "test"
os.environ["DHC_DATA_DIR"] = _TMP
os.environ["PRICING_FILE"] = "pricing.usd.json"
os.environ["WAFFO_MERCHANT_ID"] = "MER_test"
os.environ["WAFFO_PRIVATE_KEY"] = _API_PRIV_PEM
os.environ["WAFFO_WEBHOOK_PUBLIC_KEY"] = _WH_PUB_PEM
os.environ["WAFFO_ENV"] = "prod"
for _k in list(os.environ):
    if _k.startswith(("STRIPE_", "ALIPAY_", "WECHAT_PAY_")):
        del os.environ[_k]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, credits, db, plans, security
from app.payments import base, waffo_provider
from app.payments.api import router as pay_router

app = FastAPI()
app.include_router(pay_router)
client = TestClient(app)

_seq = 0


@pytest.fixture(autouse=True)
def _waffo_env(monkeypatch):
    """Pin Waffo config + USD pricing for every test in this module and revert
    afterwards, so nothing leaks into other test modules in the full suite."""
    monkeypatch.setattr(config, "PRICING_FILE", "pricing.usd.json")
    monkeypatch.setattr(config, "WAFFO_MERCHANT_ID", "MER_test")
    monkeypatch.setattr(config, "WAFFO_PRIVATE_KEY", _API_PRIV_PEM)
    monkeypatch.setattr(config, "WAFFO_WEBHOOK_PUBLIC_KEY", _WH_PUB_PEM)
    monkeypatch.setattr(config, "WAFFO_ENV", "prod")
    monkeypatch.setattr(config, "WAFFO_PRODUCT_ID", "")
    monkeypatch.setattr(config, "WAFFO_STORE_ID", "")
    monkeypatch.setattr(config, "WAFFO_API_BASE", "https://api.waffo.ai")
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://dsh.example.com")
    monkeypatch.setattr(plans, "_cache", None)
    monkeypatch.setattr(plans, "_cache_mtime", 0.0)
    yield


def make_user() -> tuple[str, dict]:
    global _seq
    _seq += 1
    uid = security.new_id("u_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (id, email, created) VALUES (?,?,?)", (uid, f"w{_seq}@test.local", time.time())
        )
    token = security.sign_token(uid, epoch=0)
    return uid, {"Authorization": f"Bearer {token}"}


def waffo_sig(payload: bytes, t_ms: int | None = None) -> str:
    """Sign a webhook body with the webhook private key (verified against the
    configured public key), header shape 't=<ms>,v1=<base64>'."""
    t_ms = int(t_ms if t_ms is not None else time.time() * 1000)
    sig = _WH_KEY.sign(f"{t_ms}.".encode() + payload, padding.PKCS1v15(), hashes.SHA256())
    return f"t={t_ms},v1={base64.b64encode(sig).decode()}"


# --- request signing ---------------------------------------------------------


def test_sign_request_canonical_and_signature():
    path = "/v1/actions/checkout/create-session"
    body = json.dumps({"a": 1}).encode()
    headers = waffo_provider.sign_request("POST", path, body)
    assert headers["X-Merchant-Id"] == "MER_test"
    assert headers["Content-Type"] == "application/json"
    ts = headers["X-Timestamp"]
    assert ts.isdigit() and abs(int(ts) - int(time.time())) < 5
    # canonical = METHOD\nPATH\nTS\nSHA256_BASE64(BODY), signed PKCS1v15 SHA256
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    canonical = f"POST\n{path}\n{ts}\n{digest}".encode()
    _API_KEY.public_key().verify(
        base64.b64decode(headers["X-Signature"]), canonical, padding.PKCS1v15(), hashes.SHA256()
    )
    # a tampered canonical must NOT verify
    with pytest.raises(InvalidSignature):
        _API_KEY.public_key().verify(
            base64.b64decode(headers["X-Signature"]), canonical + b"x", padding.PKCS1v15(), hashes.SHA256()
        )


# --- webhook signature verification -----------------------------------------


def test_verify_webhook_signature_roundtrip_tamper_stale():
    payload = json.dumps({"eventType": "order.completed"}).encode()
    sig = waffo_sig(payload)
    assert waffo_provider.verify_webhook_signature(payload, sig, _WH_PUB_PEM) is True
    # tampered body no longer matches the signature
    assert waffo_provider.verify_webhook_signature(payload + b"x", sig, _WH_PUB_PEM) is False
    # stale timestamp (outside the 300 s tolerance) is rejected
    stale = waffo_sig(payload, t_ms=int((time.time() - 4000) * 1000))
    assert waffo_provider.verify_webhook_signature(payload, stale, _WH_PUB_PEM) is False
    # garbage / missing header
    assert waffo_provider.verify_webhook_signature(payload, "", _WH_PUB_PEM) is False
    assert waffo_provider.verify_webhook_signature(payload, "t=abc,v1=zzz", _WH_PUB_PEM) is False


# --- event classification ----------------------------------------------------


def test_classify_event_table():
    assert waffo_provider.classify_event("order.completed") == "paid"
    assert waffo_provider.classify_event("refund.succeeded") == "reversal"
    assert waffo_provider.classify_event("chargeback.created") == "reversal"
    assert waffo_provider.classify_event("dispute.created") == "reversal"
    # negative suffixes must NOT be treated as reversals
    assert waffo_provider.classify_event("refund.failed") == "ignore"
    assert waffo_provider.classify_event("dispute.won") == "ignore"
    assert waffo_provider.classify_event("chargeback.rejected") == "ignore"
    # unrelated events are ignored
    assert waffo_provider.classify_event("subscription.updated") == "ignore"
    assert waffo_provider.classify_event("") == "ignore"


# --- checkout ----------------------------------------------------------------


def test_checkout_creates_pending_order_usd(monkeypatch):
    monkeypatch.setattr(config, "WAFFO_PRODUCT_ID", "PROD_test")  # skip store/product resolution
    uid, headers = make_user()
    sent = {}

    async def fake_request(path, payload):
        sent["path"], sent["payload"] = path, payload
        return 200, {"data": {"checkoutUrl": "https://pay.waffo.ai/c/SESS_1", "sessionId": "SESS_1"}}

    monkeypatch.setattr(waffo_provider, "_waffo_request", fake_request)
    r = client.post(
        "/api/pay/checkout", json={"item": "plan:plus:monthly", "provider": "waffo"}, headers=headers
    )
    body = r.json()
    assert r.status_code == 200
    assert body["provider"] == "waffo"
    assert body["pay_url"] == "https://pay.waffo.ai/c/SESS_1"
    oid = body["order_id"]
    assert oid.startswith("DHF")

    # order row: pending, USD, amount straight from pricing.usd.json — the intro
    # price, because this user has not bought a month of Plus before
    order = base.get_order(oid, uid)
    assert order["status"] == "pending"
    assert order["provider"] == "waffo"
    assert order["currency"] == "USD"
    assert order["amount_cents"] == plans.pricing()["tiers"]["plus"]["monthly_intro_cents"]
    assert credits.balance(uid) == 0  # nothing fulfilled yet

    # create-session payload: amount in major units, external id + success url
    assert sent["path"] == "/v1/actions/checkout/create-session"
    p = sent["payload"]
    assert p["productId"] == "PROD_test"
    assert p["currency"] == "USD"
    # A STRING, not a number. This assertion used to demand a float, which
    # locked in exactly the shape the live API rejects with a fieldless
    # {"message":"Invalid input","layer":"order"} — every real checkout
    # failed against production while the suite stayed green.
    expected = f"{plans.pricing()['tiers']['plus']['monthly_intro_cents'] / 100:.2f}"
    assert p["priceSnapshot"]["amount"] == expected
    assert isinstance(p["priceSnapshot"]["amount"], str)
    assert p["orderMerchantExternalId"] == oid
    assert f"order={oid}" in p["successUrl"]
    assert "card" in p["includePaymentMethods"]


# --- webhook: paid, idempotence, refund, unsigned reversal -------------------


def test_webhook_paid_idempotent_then_refund(monkeypatch):
    uid, _ = make_user()
    order = base.create_order(uid, "waffo", "plan:plus:monthly")
    oid = order["order_id"]
    assert base.get_order(oid)["status"] == "pending"

    event = {"eventType": "order.completed", "data": {"orderMerchantExternalId": oid, "sessionId": "SESS_9"}}
    payload = json.dumps(event).encode()
    r = client.post(
        "/api/pay/webhook/waffo", content=payload, headers={"X-Waffo-Signature": waffo_sig(payload)}
    )
    assert r.status_code == 200 and r.json() == {"received": True}

    settled = base.get_order(oid)
    assert settled["status"] == "paid" and settled["provider_ref"] == "SESS_9"
    assert (
        credits.balance(uid) == plans.pricing()["tiers"]["plus"]["monthly_credits"]
    )  # plus monthly_credits (USD table)
    sub = db.query_one("SELECT * FROM subscriptions WHERE user_id=?", (uid,))
    assert sub["tier"] == "plus" and float(sub["expires"]) > time.time()
    expires_before = float(sub["expires"])

    # duplicate webhook: no double fulfilment
    r = client.post(
        "/api/pay/webhook/waffo", content=payload, headers={"X-Waffo-Signature": waffo_sig(payload)}
    )
    assert r.status_code == 200
    assert credits.balance(uid) == plans.pricing()["tiers"]["plus"]["monthly_credits"]
    sub = db.query_one("SELECT * FROM subscriptions WHERE user_id=?", (uid,))
    assert float(sub["expires"]) == expires_before

    # refund with a valid signature: paid -> refunded
    refund = {
        "eventType": "refund.succeeded",
        "data": {"orderMerchantExternalId": oid, "sessionId": "SESS_9"},
    }
    rp = json.dumps(refund).encode()
    r = client.post("/api/pay/webhook/waffo", content=rp, headers={"X-Waffo-Signature": waffo_sig(rp)})
    assert r.status_code == 200
    assert base.get_order(oid)["status"] == "refunded"


def test_unsigned_webhook_rejected_no_state_change(monkeypatch):
    uid, _ = make_user()
    oid = base.create_order(uid, "waffo", "plan:plus:monthly")["order_id"]

    # unsigned reversal must be rejected with 400 (pubkey configured + no sig)
    refund = {"eventType": "refund.succeeded", "data": {"orderMerchantExternalId": oid}}
    rp = json.dumps(refund).encode()
    r = client.post("/api/pay/webhook/waffo", content=rp)  # no X-Waffo-Signature
    assert r.status_code == 400
    assert base.get_order(oid)["status"] == "pending"  # untouched

    # a bad signature on a paid event is likewise rejected, nothing fulfilled
    paid = {"eventType": "order.completed", "data": {"orderMerchantExternalId": oid}}
    pp = json.dumps(paid).encode()
    r = client.post(
        "/api/pay/webhook/waffo",
        content=pp,
        headers={
            "X-Waffo-Signature": f"t={int(time.time() * 1000)},v1="
            + base64.b64encode(b"not-a-signature").decode()
        },
    )
    assert r.status_code == 400
    assert base.get_order(oid)["status"] == "pending"
    assert credits.balance(uid) == 0


def test_process_webhook_unknown_order_is_ignored():
    event = {"eventType": "order.completed", "data": {"orderMerchantExternalId": "DHFNOPE"}}
    payload = json.dumps(event).encode()
    # valid signature but no matching local order -> None (no raise)
    assert waffo_provider.process_webhook(payload, waffo_sig(payload)) is None


def test_configured_and_active_provider():
    assert waffo_provider.configured() is True
    from app.payments import api

    assert "waffo" in api.active_providers()


def test_missing_webhook_key_disables_provider(monkeypatch):
    monkeypatch.setattr(config, "WAFFO_WEBHOOK_PUBLIC_KEY", "")
    assert waffo_provider.configured() is False
    from app.payments import api

    assert "waffo" not in api.active_providers()
