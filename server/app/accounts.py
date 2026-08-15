"""Accounts: register / login (password or email code) / me / logout / delete.

Credential resolution order (resolve_user):
  1. Authorization: Bearer <token>   — desktop device tokens and API use
  2. Cookie dhc_session              — browser console
Tokens carry the user's session_epoch; bumping it revokes everything at once.
"""
from __future__ import annotations

import re
import secrets
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from . import config, credits, db, rate_limit, security

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- credential resolution ---------------------------------------------------

def _load_user(user_id: str):
    return db.query_one("SELECT * FROM users WHERE id=?", (user_id,))


def resolve_user(request: Request) -> dict:
    user = try_resolve_user(request)
    if user is None:
        raise HTTPException(401, "not_authenticated")
    return user


def try_resolve_user(request: Request) -> dict | None:
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get(config.SESSION_COOKIE, "")
    if not token:
        return None
    payload = security.verify_token(token)
    if not payload:
        return None
    user = _load_user(payload.get("u", ""))
    if user is None or user["status"] != "active":
        return None
    if int(payload.get("e", -1)) != int(user["session_epoch"]):
        return None
    device_id = payload.get("d", "")
    if device_id:
        dev = db.query_one("SELECT revoked, epoch FROM devices WHERE id=?", (device_id,))
        if dev is None or int(dev["revoked"]) or int(dev["epoch"]) != int(payload.get("e", -1)):
            return None
        db.query("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device_id))
    out = dict(user)
    out["device_id"] = device_id
    out["is_admin"] = user["email"].lower() in config.ADMIN_EMAILS or user["role"] == "admin"
    return out


def set_session_cookie(response: Response, user: dict) -> None:
    token = security.sign_token(user["id"], epoch=int(user["session_epoch"]))
    response.set_cookie(config.SESSION_COOKIE, token, max_age=config.SESSION_TTL,
                        httponly=True, samesite="lax", secure=not config.DEV_MODE, path="/")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# --- registration / login ----------------------------------------------------

def _create_user(email: str, password: str = "") -> dict:
    uid = security.new_id("u_")
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, created, last_login) "
            "VALUES (?,?,?,?,?,?)",
            (uid, email, security.hash_password(password) if password else "", email.split("@")[0], now, now))
    if config.FREE_SIGNUP_CREDITS > 0:
        # lifetime free allowance: 10-year expiry stands in for "never expires"
        credits.grant(uid, config.FREE_SIGNUP_CREDITS, 10 * 365 * 86400, kind="grant_signup")
    return _load_user(uid)


@router.post("/register")
def register(body: dict, request: Request, response: Response):
    if not config.ALLOW_REGISTRATION:
        raise HTTPException(403, "registration_disabled")
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "invalid_email")
    if len(password) < 8:
        raise HTTPException(400, "password_too_short")
    if not rate_limit.allow(f"reg:{_client_ip(request)}", 10, 3600):
        raise HTTPException(429, "too_many_requests")
    if db.query_one("SELECT id FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "email_exists")
    user = _create_user(email, password)
    set_session_cookie(response, user)
    return {"ok": True, "user": public_user(user)}


@router.post("/login")
def login(body: dict, request: Request, response: Response):
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    ip = _client_ip(request)
    if rate_limit.login_locked(email, ip):
        raise HTTPException(429, "locked_try_later")
    user = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if user is None or not user["password_hash"] or not security.verify_password(password, user["password_hash"]):
        rate_limit.login_failed(email, ip)
        raise HTTPException(401, "bad_credentials")
    if user["status"] != "active":
        raise HTTPException(403, "account_disabled")
    db.query("UPDATE users SET last_login=? WHERE id=?", (time.time(), user["id"]))
    set_session_cookie(response, dict(user))
    return {"ok": True, "user": public_user(dict(user))}


# --- email verification codes ------------------------------------------------

def _send_mail(to: str, subject: str, text: str) -> None:
    if not config.MAIL_SMTP_HOST:
        if config.DEV_MODE:
            print(f"[dev-mail] to={to} subject={subject}\n{text}")
            return
        raise HTTPException(503, "mail_not_configured")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    with smtplib.SMTP_SSL(config.MAIL_SMTP_HOST, config.MAIL_SMTP_PORT, timeout=15) as smtp:
        if config.MAIL_SMTP_USER:
            smtp.login(config.MAIL_SMTP_USER, config.MAIL_SMTP_PASS)
        smtp.sendmail(config.MAIL_FROM, [to], msg.as_string())


@router.post("/email/send")
def email_send(body: dict, request: Request):
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "invalid_email")
    ip = _client_ip(request)
    if not (rate_limit.allow(f"code:i:{ip}", 5, 600)
            and rate_limit.allow(f"code:e:{email}", 10, 86400)
            and rate_limit.allow("code:all", 500, 86400)):
        raise HTTPException(429, "too_many_requests")
    code = f"{secrets.randbelow(1000000):06d}"
    now = time.time()
    with db.tx() as conn:
        conn.execute("DELETE FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
        conn.execute("INSERT INTO email_codes (email, code_hash, purpose, expires, created) VALUES (?,?,?,?,?)",
                     (email, security.token_hash(code), "login", now + 600, now))
    _send_mail(email, "DSH Cloud 登录验证码", f"您的登录验证码是 {code}，10 分钟内有效。若非本人操作请忽略。")
    return {"ok": True}


@router.post("/email/login")
def email_login(body: dict, request: Request, response: Response):
    email = str(body.get("email", "")).strip().lower()
    code = str(body.get("code", "")).strip()
    ip = _client_ip(request)
    if rate_limit.login_locked(email, ip):
        raise HTTPException(429, "locked_try_later")
    row = db.query_one("SELECT * FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
    if (row is None or float(row["expires"]) < time.time() or int(row["attempts"]) >= 5
            or not secrets.compare_digest(row["code_hash"], security.token_hash(code))):
        if row is not None:
            db.query("UPDATE email_codes SET attempts=attempts+1 WHERE email=? AND purpose=?", (email, "login"))
        rate_limit.login_failed(email, ip)
        raise HTTPException(401, "bad_code")
    db.query("DELETE FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
    user = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if user is None:
        if not config.ALLOW_REGISTRATION:
            raise HTTPException(403, "registration_disabled")
        user = _create_user(email)  # email-ownership-verified auto signup
    if user["status"] != "active":
        raise HTTPException(403, "account_disabled")
    db.query("UPDATE users SET last_login=? WHERE id=?", (time.time(), user["id"]))
    set_session_cookie(response, dict(user))
    return {"ok": True, "user": public_user(dict(user))}


# --- session management ------------------------------------------------------

def public_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "display_name": user["display_name"],
            "created": user["created"]}


@router.get("/me")
def me(user: dict = Depends(resolve_user)):
    from . import plans  # local import to avoid cycle
    plan = plans.current_plan(user["id"])
    return {"user": public_user(user),
            "plan": {"tier": plan["tier"], "name": plan.get("name", plan["tier"]),
                     "expires": plan["expires"], "concurrency": plan["concurrency"]},
            "credits": credits.balance(user["id"])}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(body: dict, user: dict = Depends(resolve_user)):
    old, new = str(body.get("old", "")), str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "password_too_short")
    if user["password_hash"] and not security.verify_password(old, user["password_hash"]):
        raise HTTPException(401, "bad_credentials")
    with db.tx() as conn:
        conn.execute("UPDATE users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?",
                     (security.hash_password(new), user["id"]))
        conn.execute("UPDATE devices SET revoked=1 WHERE user_id=?", (user["id"],))
    return {"ok": True, "relogin": True}


@router.get("/devices")
def list_devices(user: dict = Depends(resolve_user)):
    rows = db.query("SELECT id, name, platform, last_seen, created, revoked FROM devices "
                    "WHERE user_id=? ORDER BY created DESC", (user["id"],))
    return {"devices": [dict(r) for r in rows]}


@router.post("/devices/revoke")
def revoke_device(body: dict, user: dict = Depends(resolve_user)):
    device_id = str(body.get("device_id", ""))
    db.query("UPDATE devices SET revoked=1 WHERE id=? AND user_id=?", (device_id, user["id"]))
    return {"ok": True}


@router.post("/delete-account")
def delete_account(body: dict, user: dict = Depends(resolve_user)):
    if str(body.get("confirm", "")) != user["email"]:
        raise HTTPException(400, "confirm_mismatch")
    with db.tx() as conn:
        conn.execute("UPDATE users SET status='deleted', session_epoch=session_epoch+1, email=? WHERE id=?",
                     (f"deleted+{user['id']}@invalid.local", user["id"]))
        conn.execute("UPDATE devices SET revoked=1 WHERE user_id=?", (user["id"],))
    return {"ok": True}
