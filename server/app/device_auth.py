"""Device authorization flow (RFC 8628 style) — the desktop login wall.

Desktop:  POST /api/device/start          -> {device_code, user_code, verification_url}
Browser:  /activate?code=USER-CODE        -> login if needed, click approve
          POST /api/device/approve        -> binds the code to the browser user
Desktop:  POST /api/device/poll           -> pending | {token, user}

The long-lived device token embeds the device id; revoking the device (or
bumping the user's session epoch) kills it. Plaintext tokens are never stored.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import config, db, rate_limit, security
from .accounts import public_user, resolve_user

router = APIRouter(prefix="/api/device", tags=["device"])

CODE_TTL = 600  # 10 minutes


@router.post("/start")
def start(body: dict, request: Request):
    ip = request.client.host if request.client else ""
    if not rate_limit.allow(f"dev:start:{ip}", 10, 600):
        raise HTTPException(429, "too_many_requests")
    device_code = security.new_id("dc_") + security.new_id("")
    user_code = security.user_code()
    now = time.time()
    client_info = {
        "name": str(body.get("name", ""))[:80],
        "platform": str(body.get("platform", ""))[:40],
        "app_version": str(body.get("app_version", ""))[:40],
    }
    with db.tx() as conn:
        conn.execute("DELETE FROM device_codes WHERE expires<?", (now,))
        conn.execute(
            "INSERT INTO device_codes (device_code_hash, user_code, status, client_info, expires, created) "
            "VALUES (?,?,?,?,?,?)",
            (security.token_hash(device_code), user_code, "pending",
             json.dumps(client_info, ensure_ascii=False), now + CODE_TTL, now))
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_url": f"{config.PUBLIC_BASE}/activate?code={user_code}",
        "expires_in": CODE_TTL,
        "interval": 3,
    }


@router.get("/info")
def info(code: str):
    """Shown on the /activate page so the user sees what they are approving."""
    row = db.query_one("SELECT user_code, status, client_info, expires FROM device_codes WHERE user_code=?",
                       (code.strip().upper(),))
    if row is None or float(row["expires"]) < time.time():
        raise HTTPException(404, "code_not_found")
    return {"user_code": row["user_code"], "status": row["status"],
            "client": json.loads(row["client_info"])}


@router.post("/approve")
def approve(body: dict, user: dict = Depends(resolve_user)):
    code = str(body.get("user_code", "")).strip().upper()
    deny = bool(body.get("deny"))
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM device_codes WHERE user_code=?", (code,)).fetchone()
        if row is None or float(row["expires"]) < time.time():
            raise HTTPException(404, "code_not_found")
        if row["status"] != "pending":
            raise HTTPException(409, "already_handled")
        conn.execute("UPDATE device_codes SET status=?, user_id=? WHERE user_code=?",
                     ("denied" if deny else "approved", user["id"], code))
    return {"ok": True, "status": "denied" if deny else "approved"}


@router.post("/login")
def login(body: dict, request: Request):
    """In-window email+password fallback for the desktop login wall: verifies
    credentials and mints a device token in one call (no browser needed)."""
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    ip = request.client.host if request.client else ""
    if rate_limit.login_locked(email, ip):
        raise HTTPException(429, "locked_try_later")
    user = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if user is None or not user["password_hash"] \
            or not security.verify_password(password, user["password_hash"]):
        rate_limit.login_failed(email, ip)
        raise HTTPException(401, "bad_credentials")
    if user["status"] != "active":
        raise HTTPException(403, "account_disabled")
    device_id = security.new_id("dev_")
    epoch = int(user["session_epoch"])
    token = security.sign_token(user["id"], device_id=device_id, epoch=epoch, ttl=config.DEVICE_TOKEN_TTL)
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO devices (id, user_id, name, platform, token_hash, epoch, last_seen, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (device_id, user["id"], str(body.get("name", ""))[:80], str(body.get("platform", ""))[:40],
             security.token_hash(token), epoch, now, now))
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user["id"]))
    return {"token": token, "user": public_user(dict(user))}


@router.post("/poll")
def poll(body: dict, request: Request):
    device_code = str(body.get("device_code", ""))
    ip = request.client.host if request.client else ""
    if not rate_limit.allow(f"dev:poll:{ip}", 60, 60):
        raise HTTPException(429, "slow_down")
    row = db.query_one("SELECT * FROM device_codes WHERE device_code_hash=?",
                       (security.token_hash(device_code),))
    if row is None or float(row["expires"]) < time.time():
        raise HTTPException(404, "expired")
    if row["status"] == "pending":
        return {"status": "pending"}
    if row["status"] == "denied":
        db.query("DELETE FROM device_codes WHERE device_code_hash=?", (security.token_hash(device_code),))
        return {"status": "denied"}

    # approved: issue the device token exactly once
    user = db.query_one("SELECT * FROM users WHERE id=?", (row["user_id"],))
    if user is None or user["status"] != "active":
        raise HTTPException(403, "account_disabled")
    client = json.loads(row["client_info"])
    device_id = security.new_id("dev_")
    epoch = int(user["session_epoch"])
    token = security.sign_token(user["id"], device_id=device_id, epoch=epoch, ttl=config.DEVICE_TOKEN_TTL)
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO devices (id, user_id, name, platform, token_hash, epoch, last_seen, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (device_id, user["id"], client.get("name", ""), client.get("platform", ""),
             security.token_hash(token), epoch, now, now))
        conn.execute("DELETE FROM device_codes WHERE device_code_hash=?", (security.token_hash(device_code),))
    return {"status": "approved", "token": token, "user": public_user(dict(user))}
