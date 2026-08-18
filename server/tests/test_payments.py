"""Payments layer tests: item resolution, checkout, webhooks, idempotence.

Config reads env at import time, so the environment is pinned BEFORE any app
import. All outbound httpx calls are stubbed; RSA/AES material is generated
locally so signature paths run for real.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

_TMP = tempfile.mkdtemp(prefix="dhc-pay-test-")
os.environ["DHC_DEV"] = "1"
os.environ["AUTH_SECRET"] = "test"
os.environ["DHC_DATA_DIR"] = _TMP
for _k in list(os.environ):
    if _k.startswith(("STRIPE_", "ALIPAY_", "WECHAT_PAY_")):
        del os.environ[_k]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import plans, config, credits, db, security
from app.payments import alipay_provider, base, stripe_provider, wechatpay_provider
from app.payments.api import router as pay_router

app = FastAPI()
app.include_router(pay_router)
client = TestClient(app)

_seq = 0


def make_user() -> tuple[str, dict]:
    """User row without signup credits so balances start at exactly 0."""
    global _seq
    _seq += 1
    uid = security.new_id("u_")
    with db.tx() as conn:
        conn.execute("INSERT INTO users (id, email, created) VALUES (?,?,?)",
                     (uid, f"t{_seq}@test.local", time.time()))
    token = security.sign_token(uid, epoch=0)
    return uid, {"Authorization": f"Bearer {token}"}


class R:
    def __init__(self, data, status_code=200):
        self._data, self.status_code, self.text = data, status_code, json.dumps(data)

    def json(self):
        return self._data


def stripe_sig(payload: bytes, secret: str = "whsec_test", t: int | None = None) -> str:
    t = int(t if t is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return key, priv, pub


# --- item resolution ---------------------------------------------------------

def test_resolve_item_rejects_unknown_and_free():
    for bad in ("", "garbage", "plan:free:monthly", "plan:plus:weekly", "plan:nope:monthly",
                "pack:nope", "pack", "plan:plus", "plan:plus:monthly:extra"):
        with pytest.raises(HTTPException) as e:
            base.resolve_item(bad)
        assert e.value.status_code == 400

    table = plans.pricing()["tiers"]
    assert base.resolve_item("plan:plus:monthly")["amount_cents"] == table["plus"]["monthly_cents"]
    assert base.resolve_item("plan:pro:yearly")["amount_cents"] == table["pro"]["yearly_cents"]
    # Read the pack id from the table rather than pinning one: the tiers are a
    # commercial decision that changes, and a test that fails when they do is
    # testing the price list, not the code.
    pack_id = next(iter(plans.pricing()["packs"]))
    info = base.resolve_item(f"pack:{pack_id}")
    pack = plans.pricing()["packs"][pack_id]
    assert info["amount_cents"] == pack["cents"] and info["credits"] == pack["credits"]


def test_checkout_charges_the_currency_the_page_quoted():
    """The pricing page renders from the visitor's currency table while checkout
    priced from the default one, so a visitor shown ¥70 landed on a payment page
    asking for $10. Both sides now resolve the currency the same way."""
    _, headers = make_user()
    r = client.post("/api/pay/checkout?cur=CNY", json={"item": "plan:plus:monthly"}, headers=headers)
    assert r.status_code == 200
    order = base.get_order(r.json()["order_id"])
    assert order["currency"] == "CNY"
    assert order["amount_cents"] == plans.pricing("CNY")["tiers"]["plus"]["monthly_cents"]
    assert client.get("/api/pay/context?cur=CNY").json()["currency"] == "CNY"


def test_client_cannot_choose_its_own_currency():
    """Six independently set tables are not exact conversions of each other, so a
    body-supplied currency would let a caller shop for the cheapest one."""
    _, headers = make_user()
    r = client.post("/api/pay/checkout", headers=headers,
                    json={"item": "plan:plus:monthly", "currency": "JPY", "cur": "JPY"})
    order = base.get_order(r.json()["order_id"])
    assert order["currency"] == plans.pricing()["currency"]


def test_abandoned_checkouts_expire_but_stay_fulfillable():
    """Abandoned checkouts sat as "pending" forever, including ones whose item
    had since been withdrawn from the price table. They expire now — and a late
    webhook still fulfils them, because expiry is our guess and the provider
    confirming a payment outranks it."""
    uid, headers = make_user()
    oid = client.post("/api/pay/checkout", json={"item": "pack:pack1000"},
                      headers=headers).json()["order_id"]
    with db.tx() as conn:
        conn.execute("UPDATE orders SET status='pending', created=? WHERE id=?",
                     (time.time() - base.PENDING_TTL_S - 60, oid))

    assert client.get("/api/pay/orders", headers=headers).status_code == 200
    assert base.get_order(oid)["status"] == "expired"

    # money still outranks housekeeping
    assert base.mark_paid(oid, "ref") is True
    assert base.get_order(oid)["status"] == "paid"
    assert base.mark_paid(oid, "ref") is False


def test_client_supplied_amount_is_ignored():
    _, headers = make_user()
    r = client.post("/api/pay/checkout", headers=headers,
                    json={"item": "pack:pack1000", "amount_cents": 1, "total_amount": "0.01"})
    assert r.status_code == 200
    order = base.get_order(r.json()["order_id"])
    assert order["amount_cents"] == plans.pricing()["packs"]["pack1000"]["cents"]  # from pricing.json, nothing else


# --- context / intent path ---------------------------------------------------

def test_context_lists_active_providers(monkeypatch):
    body = client.get("/api/pay/context").json()
    assert body["providers"] == [] and body["currency"] == plans.pricing()["currency"]
    assert "plus" in body["pricing"]["tiers"] and "pack1000" in body["pricing"]["packs"]
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    assert client.get("/api/pay/context").json()["providers"] == ["stripe"]


def test_checkout_without_provider_records_intent():
    uid, headers = make_user()
    r = client.post("/api/pay/checkout", json={"item": "plan:pro:yearly"}, headers=headers)
    body = r.json()
    assert body["provider"] is None and body["intent"] is True
    order = base.get_order(body["order_id"], uid)
    assert order["status"] == "intent" and order["provider"] == "intent"
    assert order["amount_cents"] == plans.pricing()["tiers"]["pro"]["yearly_cents"]
    assert credits.balance(uid) == 0  # nothing fulfilled


def test_checkout_requires_auth():
    assert client.post("/api/pay/checkout", json={"item": "pack:pack1000"}).status_code == 401
    assert client.get("/api/pay/orders").status_code == 401


# --- stripe ------------------------------------------------------------------

def test_stripe_webhook_bad_signature_rejected(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = json.dumps({"type": "checkout.session.completed"}).encode()
    r = client.post("/api/pay/webhook/stripe", content=payload,
                    headers={"stripe-signature": f"t={int(time.time())},v1=" + "0" * 64})
    assert r.status_code == 400
    # stale timestamp outside the 300 s tolerance
    r = client.post("/api/pay/webhook/stripe", content=payload,
                    headers={"stripe-signature": stripe_sig(payload, t=int(time.time()) - 4000)})
    assert r.status_code == 400
    # missing header entirely
    assert client.post("/api/pay/webhook/stripe", content=payload).status_code == 400


def test_stripe_happy_path_idempotence_and_refund(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    uid, headers = make_user()

    sent = {}

    def fake_post(url, **kw):
        sent["url"], sent["data"] = url, kw.get("data", {})
        return R({"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})

    monkeypatch.setattr(stripe_provider.httpx, "post", fake_post)
    r = client.post("/api/pay/checkout", json={"item": "plan:plus:monthly"}, headers=headers)
    body = r.json()
    assert body["provider"] == "stripe" and body["pay_url"].startswith("https://checkout.stripe.com")
    oid = body["order_id"]
    assert oid.startswith("DHS")
    assert sent["data"]["line_items[0][price_data][unit_amount]"] == str(plans.pricing()["tiers"]["plus"]["monthly_cents"])
    assert sent["data"]["line_items[0][price_data][currency]"] == plans.pricing()["currency"].lower()
    assert sent["data"]["client_reference_id"] == oid
    assert sent["data"]["payment_method_types[0]"] == "card"
    assert sent["data"]["payment_method_options[wechat_pay][client]"] == "web"
    assert f"order={oid}" in sent["data"]["success_url"]

    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": "cs_1", "client_reference_id": oid,
                                 "metadata": {"order_id": oid}}}}
    payload = json.dumps(event).encode()
    monkeypatch.setattr(stripe_provider.httpx, "get",
                        lambda url, **kw: R({"id": "cs_1", "payment_status": "paid",
                                             "payment_intent": "pi_1"}))
    r = client.post("/api/pay/webhook/stripe", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200 and r.json() == {"received": True}

    order = base.get_order(oid)
    assert order["status"] == "paid" and order["provider_ref"] == "pi_1"
    assert credits.balance(uid) == plans.pricing()["tiers"]["plus"]["monthly_credits"]  # plus monthly_credits
    sub = db.query_one("SELECT * FROM subscriptions WHERE user_id=?", (uid,))
    assert sub["tier"] == "plus" and float(sub["expires"]) > time.time()
    expires_before = float(sub["expires"])

    # duplicate webhook: no double fulfilment
    r = client.post("/api/pay/webhook/stripe", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200
    assert credits.balance(uid) == plans.pricing()["tiers"]["plus"]["monthly_credits"]
    sub = db.query_one("SELECT * FROM subscriptions WHERE user_id=?", (uid,))
    assert float(sub["expires"]) == expires_before

    # unpaid session never fulfils
    uid2, headers2 = make_user()
    r = client.post("/api/pay/checkout", json={"item": "pack:pack1000"}, headers=headers2)
    oid2 = r.json()["order_id"]
    ev2 = {"type": "checkout.session.completed",
           "data": {"object": {"id": "cs_2", "client_reference_id": oid2}}}
    p2 = json.dumps(ev2).encode()
    monkeypatch.setattr(stripe_provider.httpx, "get",
                        lambda url, **kw: R({"id": "cs_2", "payment_status": "unpaid"}))
    client.post("/api/pay/webhook/stripe", content=p2, headers={"stripe-signature": stripe_sig(p2)})
    assert base.get_order(oid2)["status"] == "pending" and credits.balance(uid2) == 0

    # refund: paid -> refunded via charge.refunded (looked up by provider_ref)
    ev3 = {"type": "charge.refunded", "data": {"object": {"payment_intent": "pi_1"}}}
    p3 = json.dumps(ev3).encode()
    r = client.post("/api/pay/webhook/stripe", content=p3, headers={"stripe-signature": stripe_sig(p3)})
    assert r.status_code == 200
    assert base.get_order(oid)["status"] == "refunded"

    # a refund event for a pending order changes nothing (pending -> refunded impossible)
    assert base.get_order(oid2)["status"] == "pending"
    assert base.mark_refunded(oid2) is False
    assert base.get_order(oid2)["status"] == "pending"


# --- alipay ------------------------------------------------------------------

def test_alipay_sign_verify_roundtrip(monkeypatch):
    key, priv_pem, pub_pem = rsa_keypair()
    monkeypatch.setattr(config, "ALIPAY_APP_ID", "2021000000000001")
    monkeypatch.setattr(config, "ALIPAY_APP_PRIVATE_KEY", priv_pem)
    monkeypatch.setattr(config, "ALIPAY_PUBLIC_KEY", pub_pem)
    uid, headers = make_user()

    r = client.post("/api/pay/checkout", json={"item": "pack:pack1000", "provider": "alipay"},
                    headers=headers)
    body = r.json()
    assert body["provider"] == "alipay"
    oid = body["order_id"]
    assert oid.startswith("DHA")

    qs = dict(parse_qsl(urlsplit(body["pay_url"]).query, keep_blank_values=True))
    assert qs["method"] == "alipay.trade.page.pay" and qs["sign_type"] == "RSA2"
    biz = json.loads(qs["biz_content"])
    expected = "%.2f" % (plans.pricing()["packs"]["pack1000"]["cents"] / 100)
    assert biz["out_trade_no"] == oid and biz["total_amount"] == expected
    assert qs["notify_url"].endswith("/api/pay/webhook/alipay")
    # request signature verifies against our public key (sign covers everything but `sign`)
    content = "&".join(f"{k}={v}" for k, v in sorted(qs.items()) if k != "sign" and v != "")
    key.public_key().verify(base64.b64decode(qs["sign"]), content.encode(),
                            padding.PKCS1v15(), hashes.SHA256())

    def signed_notify(fields: dict) -> dict:
        content = "&".join(f"{k}={v}" for k, v in sorted(fields.items()) if v != "")
        out = dict(fields)
        out["sign"] = base64.b64encode(
            key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        out["sign_type"] = "RSA2"
        return out

    notify = signed_notify({"app_id": "2021000000000001", "out_trade_no": oid, "trade_no": "ali_1",
                            "trade_status": "TRADE_SUCCESS", "total_amount": "10.00",
                            "notify_id": "n1", "gmt_payment": "2026-08-16 10:00:00"})
    monkeypatch.setattr(alipay_provider.httpx, "get",
                        lambda url, **kw: R({"alipay_trade_query_response": {
                            "code": "10000", "trade_status": "TRADE_SUCCESS",
                            "trade_no": "ali_1", "out_trade_no": oid}}))
    r = client.post("/api/pay/webhook/alipay", data=notify)
    assert r.text == "success"
    order = base.get_order(oid)
    assert order["status"] == "paid" and order["provider_ref"] == "ali_1"
    assert credits.balance(uid) == 1000

    # tampered notify (signature over different content) is rejected
    bad = dict(notify)
    bad["total_amount"] = "0.01"
    assert client.post("/api/pay/webhook/alipay", data=bad).text == "failure"
    # wrong app_id is rejected even with a valid signature
    other = signed_notify({"app_id": "9999", "out_trade_no": oid, "trade_status": "TRADE_SUCCESS"})
    assert client.post("/api/pay/webhook/alipay", data=other).text == "failure"


# --- wechat ------------------------------------------------------------------

def test_wechat_native_and_webhook(monkeypatch, tmp_path):
    _, priv_pem, _ = rsa_keypair()
    key_path = tmp_path / "wx_key.pem"
    key_path.write_text(priv_pem)
    apiv3_key = "0123456789abcdef0123456789abcdef"  # 32 bytes
    monkeypatch.setattr(config, "WECHAT_PAY_MCHID", "1900000001")
    monkeypatch.setattr(config, "WECHAT_PAY_APIV3_KEY", apiv3_key)
    monkeypatch.setattr(config, "WECHAT_PAY_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setattr(config, "WECHAT_PAY_SERIAL_NO", "SERIAL1")
    monkeypatch.setattr(config, "WECHAT_PAY_APPID", "wx0000000001")
    uid, headers = make_user()

    sent = {}

    def fake_post(url, **kw):
        sent["url"], sent["headers"], sent["content"] = url, kw["headers"], kw["content"]
        return R({"code_url": "weixin://wxpay/bizpayurl?pr=abc123"})

    monkeypatch.setattr(wechatpay_provider.httpx, "post", fake_post)
    r = client.post("/api/pay/checkout", json={"item": "pack:pack1000", "provider": "wechat"},
                    headers=headers)
    body = r.json()
    assert body["provider"] == "wechat" and body["code_url"].startswith("weixin://")
    oid = body["order_id"]
    assert oid.startswith("DHW")
    assert sent["headers"]["Authorization"].startswith("WECHATPAY2-SHA256-RSA2048 mchid=")
    req = json.loads(sent["content"])
    # WeChat settles in CNY only, so this one IS a literal — the provider
    # would reject anything else regardless of what the page was showing.
    assert req["amount"] == {"total": plans.pricing()["packs"]["pack1000"]["cents"], "currency": "CNY"} and req["out_trade_no"] == oid

    def encrypted_hook(event_type: str, payload: dict) -> dict:
        nonce = "abcdef123456"
        ct = AESGCM(apiv3_key.encode()).encrypt(
            nonce.encode(), json.dumps(payload).encode(), b"transaction")
        return {"event_type": event_type,
                "resource": {"ciphertext": base64.b64encode(ct).decode(), "nonce": nonce,
                             "associated_data": "transaction"}}

    hook = encrypted_hook("TRANSACTION.SUCCESS",
                          {"out_trade_no": oid, "trade_state": "SUCCESS", "transaction_id": "wx_1"})
    queried = {}

    def fake_get(url, **kw):
        queried["url"] = url
        return R({"trade_state": "SUCCESS", "out_trade_no": oid, "transaction_id": "wx_1"})

    monkeypatch.setattr(wechatpay_provider.httpx, "get", fake_get)
    r = client.post("/api/pay/webhook/wechat", json=hook)
    assert r.status_code == 200 and r.json()["code"] == "SUCCESS"
    assert f"/v3/pay/transactions/out-trade-no/{oid}?mchid=1900000001" in queried["url"]
    order = base.get_order(oid)
    assert order["status"] == "paid" and order["provider_ref"] == "wx_1"
    assert credits.balance(uid) == 1000

    # duplicate notify: fulfilment does not repeat
    r = client.post("/api/pay/webhook/wechat", json=hook)
    assert r.status_code == 200 and credits.balance(uid) == 1000

    # refund notify: paid -> refunded (decrypt only, no query needed)
    refund = encrypted_hook("REFUND.SUCCESS", {"out_trade_no": oid, "refund_status": "SUCCESS"})
    r = client.post("/api/pay/webhook/wechat", json=refund)
    assert r.status_code == 200
    assert base.get_order(oid)["status"] == "refunded"

    # garbage ciphertext is rejected
    bad = {"event_type": "TRANSACTION.SUCCESS",
           "resource": {"ciphertext": "AAAA", "nonce": "abcdef123456", "associated_data": "transaction"}}
    r = client.post("/api/pay/webhook/wechat", json=bad)
    assert r.status_code == 400 and r.json()["code"] == "FAIL"


def test_wechat_notify_alone_never_fulfils(monkeypatch, tmp_path):
    """The decrypted notify claims SUCCESS but the authoritative query says otherwise."""
    _, priv_pem, _ = rsa_keypair()
    key_path = tmp_path / "wx_key.pem"
    key_path.write_text(priv_pem)
    apiv3_key = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(config, "WECHAT_PAY_MCHID", "1900000001")
    monkeypatch.setattr(config, "WECHAT_PAY_APIV3_KEY", apiv3_key)
    monkeypatch.setattr(config, "WECHAT_PAY_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setattr(config, "WECHAT_PAY_SERIAL_NO", "SERIAL1")
    monkeypatch.setattr(config, "WECHAT_PAY_APPID", "wx0000000001")
    uid, headers = make_user()
    monkeypatch.setattr(wechatpay_provider.httpx, "post",
                        lambda url, **kw: R({"code_url": "weixin://x"}))
    oid = client.post("/api/pay/checkout", json={"item": "pack:pack1000"},
                      headers=headers).json()["order_id"]

    nonce = "abcdef123456"
    ct = AESGCM(apiv3_key.encode()).encrypt(
        nonce.encode(), json.dumps({"out_trade_no": oid, "trade_state": "SUCCESS"}).encode(), b"t")
    hook = {"event_type": "TRANSACTION.SUCCESS",
            "resource": {"ciphertext": base64.b64encode(ct).decode(), "nonce": nonce,
                         "associated_data": "t"}}
    monkeypatch.setattr(wechatpay_provider.httpx, "get",
                        lambda url, **kw: R({"trade_state": "NOTPAY", "out_trade_no": oid}))
    r = client.post("/api/pay/webhook/wechat", json=hook)
    assert r.status_code == 200  # acked so wechat stops retrying, but nothing fulfilled
    assert base.get_order(oid)["status"] == "pending" and credits.balance(uid) == 0


# --- order listing / polling -------------------------------------------------

def test_orders_listing_and_polling():
    _, headers = make_user()
    r1 = client.post("/api/pay/checkout", json={"item": "pack:pack1000"}, headers=headers)
    time.sleep(0.02)
    r2 = client.post("/api/pay/checkout", json={"item": "plan:plus:monthly"}, headers=headers)
    orders = client.get("/api/pay/orders", headers=headers).json()["orders"]
    assert [o["id"] for o in orders] == [r2.json()["order_id"], r1.json()["order_id"]]  # newest first
    assert all("provider_ref" not in o for o in orders)

    one = client.get(f"/api/pay/orders/{r1.json()['order_id']}", headers=headers).json()["order"]
    assert one["status"] == "intent" and one["amount_cents"] == plans.pricing()["packs"]["pack1000"]["cents"]

    # another user cannot see it
    _, other = make_user()
    assert client.get(f"/api/pay/orders/{r1.json()['order_id']}", headers=other).status_code == 404
    assert client.get("/api/pay/orders/DHSNOPE", headers=headers).status_code == 404
