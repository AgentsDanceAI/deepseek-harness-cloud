"""Accounts: register / login (password or email code) / me / logout / delete.

Credential resolution order (resolve_user):
  1. Authorization: Bearer <token>   — desktop device tokens and API use
  2. Cookie dhc_session              — browser console
Tokens carry the user's session_epoch; bumping it revokes everything at once.
"""

from __future__ import annotations

import logging
import re
import secrets
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from . import config, credits, db, rate_limit, security

log = logging.getLogger("dhc.accounts")
router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
LEGACY_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# API key 的形状: ak- 前缀让它与签名令牌一眼可分, 也便于用户在自己的配置里辨认。
API_KEY_PREFIX = "ak-"
API_KEY_MAX_PER_USER = 20


def normalize_email_identity(raw: object) -> str:
    """Canonicalize an identity accepted by earlier DSH Cloud releases.

    This function is for looking up existing accounts only. New identities must
    still pass :func:`normalize_email` before they are persisted.
    """
    email = str(raw or "").strip().lower()
    if len(email) > 254 or not LEGACY_EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "invalid_email")
    return email


def normalize_email(raw: object) -> str:
    """Canonicalize and validate an email before creating a new identity."""
    email = normalize_email_identity(raw)
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "invalid_email")
    return email


# --- credential resolution ---------------------------------------------------


def _load_user(user_id: str):
    return db.query_one("SELECT * FROM users WHERE id=?", (user_id,))


# 只有**用 cookie 认证的写操作**才有 CSRF 风险: Bearer / x-api-key 是调用方自己
# 放进去的, 浏览器不会替第三方页面附上。
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _write_origins() -> set[str]:
    """允许发起 cookie 写操作的页面来源。

    刻意是白名单而不是"同站即可": preview.<domain> 与主站是 same-site, 所以
    SameSite=Lax **不会**拦住从预览页发出的带凭据 POST, 而 CORS 只挡"读响应",
    挡不住"请求已经执行" —— 改密码这种操作读不到响应也照样成立。
    预览页上跑的是智能体生成的内容, 它必须不在这个集合里。
    """
    base = config.PUBLIC_BASE.rstrip("/")
    out = {base}
    if config.WORK_DOMAIN:
        # 工作台外壳 (work.<domain>) 上的按钮会调 /api/work/stop 与 /api/auth/logout
        scheme = "http" if base.startswith("http://") else "https"
        out.add(f"{scheme}://{config.WORK_DOMAIN}")
    return out


def _cookie_write_allowed(request: Request) -> bool:
    # WebSocket 没有 .method。**当作不安全方法查来源**, 而不是放行 —— 建立一通
    # 数字人通话要烧 GPU、按分钟扣积分, 与 POST 同性质; 而 WS 握手不受 CORS
    # 约束, 跨站页面照样发得出来。
    # (直接 request.method 会 AttributeError, 表现是通话一连就断 —— 页面本身
    # 好好的, 只有"点了开始通话没反应"。踩过。)
    is_ws = not hasattr(request, "method")
    if not is_ws and request.method.upper() not in _UNSAFE_METHODS:
        return True
    origin = request.headers.get("origin", "").rstrip("/")
    # 没有 Origin 的一律放行: 非浏览器客户端 (桌面端、CLI) 不发这个头, 而浏览器
    # 在**跨源**写入时一定会发 —— 攻击场景里它必然存在, 所以这里不会漏。
    if not origin:
        return True
    return origin in _write_origins()


def resolve_user(request: Request) -> dict:
    user = try_resolve_user(request)
    if user is None:
        raise HTTPException(401, "not_authenticated")
    return user


def try_resolve_user(request: Request, *, cookie_only: bool = False) -> dict | None:
    """解析当前用户。

    cookie_only=True 时**只看会话 cookie**, 不看 Authorization / X-Api-Key。
    这是给云空间那条 forward_auth 用的: 那里的请求是浏览器发给**产品**的, 而
    产品可能自带 Authorization 头。这个函数是"哪个来源先有值就只用它" —— 见到
    Bearer 就再也不看 cookie, 于是拿别人的令牌当 DSH 令牌验, 验不过就 302 去
    登录页, 表现是**产品控制台每个请求都被我们弹回登录**, 而用户明明已经登录、
    我们这边也一个错都不报。

    更正 (2026-08-30): 提这条改动时以为 Dify 正踩在上面, 实测**不是** —— Dify
    的 access_token 是 HttpOnly, 前端读不到, 它走的是 cookie + X-CSRF-Token,
    并不发 Authorization。那个头只出现在我手工构造的请求里。所以这条是**防御性**
    的, 当时并没有修复线上故障; 但语义本身是对的: 这两条路认的是浏览器。
    """
    token = ""
    from_cookie = False
    if not cookie_only:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.headers.get("x-api-key", "").strip()
        if not token:
            # Gemini CLI 只会发这个头 (实测 0.57.0: x-goog-api-key, 不带
            # Authorization, 也不把 key 放查询串)。装的是同一个令牌。
            token = request.headers.get("x-goog-api-key", "").strip()
    if not token:
        token = request.cookies.get(config.SESSION_COOKIE, "")
        from_cookie = bool(token)
    if not token:
        return None
    if from_cookie and not _cookie_write_allowed(request):
        log.warning(
            "[auth] 拒绝跨源 cookie 写入: %s %s origin=%r",
            getattr(request, "method", "WEBSOCKET"),
            request.url.path,
            request.headers.get("origin", ""),
        )
        return None
    # API key 先判: 它有 ak- 前缀, 与签名令牌的形状不会混淆, 一眼分流不必两边都试。
    # Dispatch by prefix before running the signed-token verifier.
    if token.startswith(API_KEY_PREFIX):
        return _user_from_api_key(token)
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
    extra = {"domain": config.COOKIE_DOMAIN} if config.COOKIE_DOMAIN else {}
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=not config.DEV_MODE,
        path="/",
        **extra,
    )


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# --- registration / login ----------------------------------------------------


def _create_user(email: str, password: str = "") -> dict:
    email = normalize_email(email)
    uid = security.new_id("u_")
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, created, last_login) "
            "VALUES (?,?,?,?,?,?)",
            (uid, email, security.hash_password(password) if password else "", email.split("@")[0], now, now),
        )
    if config.FREE_SIGNUP_CREDITS > 0:
        # lifetime free allowance: 10-year expiry stands in for "never expires"
        credits.grant(uid, config.FREE_SIGNUP_CREDITS, 10 * 365 * 86400, kind="grant_signup")
    return _load_user(uid)


def find_or_create_oauth_user(email: str, display_name: str = "") -> dict | None:
    """Log an OAuth identity (email-ownership verified by the provider) into its
    account. Email IS the identity — the same namespace as password / email-code
    logins, so a Google/GitHub login lands on the existing account for that
    address. Creates the account on first sight when OAUTH_AUTO_REGISTER is on;
    returns None when the email has no account and auto-register is off."""
    email = normalize_email_identity(email)
    row = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if row is None:
        if not config.OAUTH_AUTO_REGISTER:
            return None
        email = normalize_email(email)
        row = _create_user(email)
    user = dict(row)
    # only seed the display name — never clobber a name the user later changed
    if display_name and user["display_name"] in ("", email.split("@")[0]):
        db.query("UPDATE users SET display_name=? WHERE id=?", (display_name, user["id"]))
        user["display_name"] = display_name
    return user


# New accounts require verified email-code or OAuth ownership. Password login
# remains available for existing accounts.


@router.post("/login")
def login(body: dict, request: Request, response: Response):
    email = normalize_email_identity(body.get("email"))
    password = str(body.get("password", ""))
    ip = _client_ip(request)
    if rate_limit.login_locked(email, ip):
        raise HTTPException(429, "locked_try_later")
    user = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if (
        user is None
        or not user["password_hash"]
        or not security.verify_password(password, user["password_hash"])
    ):
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
    try:
        with smtplib.SMTP_SSL(config.MAIL_SMTP_HOST, config.MAIL_SMTP_PORT, timeout=15) as smtp:
            if config.MAIL_SMTP_USER:
                smtp.login(config.MAIL_SMTP_USER, config.MAIL_SMTP_PASS)
            smtp.sendmail(config.MAIL_FROM, [to], msg.as_string())
    except smtplib.SMTPRecipientsRefused:
        # The address parses but the provider will not deliver to it.
        raise HTTPException(400, "undeliverable_email") from None
    except smtplib.SMTPResponseException as exc:
        # 5xx here is about THIS message (a rejected recipient domain, a
        # suppression-list hit); 4xx is transient. Either way it is not a bug in
        # our code, and surfacing it as a 500 told the user "请求失败 (500)" on
        # the only sign-in path we have — with no hint that the address was the
        # problem. Log the provider's text, show the user something actionable.
        log.warning("smtp rejected message to %s: %s %s", to, exc.smtp_code, exc.smtp_error)
        if 500 <= int(exc.smtp_code or 0) < 600:
            raise HTTPException(400, "undeliverable_email") from None
        raise HTTPException(503, "mail_temporarily_unavailable") from None
    except OSError as exc:
        log.warning("smtp transport failure for %s: %s", to, exc)
        raise HTTPException(503, "mail_temporarily_unavailable") from None


@router.post("/email/send")
def email_send(body: dict, request: Request):
    email = normalize_email_identity(body.get("email"))
    if not EMAIL_RE.fullmatch(email) and db.query_one("SELECT id FROM users WHERE email=?", (email,)) is None:
        raise HTTPException(400, "invalid_email")
    ip = _client_ip(request)
    if not (
        rate_limit.allow(f"code:i:{ip}", 5, 600)
        and rate_limit.allow(f"code:e:{email}", 10, 86400)
        and rate_limit.allow("code:all", 500, 86400)
    ):
        raise HTTPException(429, "too_many_requests")
    code = f"{secrets.randbelow(1000000):06d}"
    now = time.time()
    with db.tx() as conn:
        conn.execute("DELETE FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
        conn.execute(
            "INSERT INTO email_codes (email, code_hash, purpose, expires, created) VALUES (?,?,?,?,?)",
            (email, security.token_hash(code), "login", now + 600, now),
        )
    _send_mail(email, "DSH Cloud 登录验证码", f"您的登录验证码是 {code}，10 分钟内有效。若非本人操作请忽略。")
    return {"ok": True}


@router.post("/email/login")
def email_login(body: dict, request: Request, response: Response):
    email = normalize_email_identity(body.get("email"))
    code = str(body.get("code", "")).strip()
    ip = _client_ip(request)
    if rate_limit.login_locked(email, ip):
        raise HTTPException(429, "locked_try_later")
    row = db.query_one("SELECT * FROM email_codes WHERE email=? AND purpose=?", (email, "login"))
    if (
        row is None
        or float(row["expires"]) < time.time()
        or int(row["attempts"]) >= 5
        or not secrets.compare_digest(row["code_hash"], security.token_hash(code))
    ):
        if row is not None:
            db.query(
                "UPDATE email_codes SET attempts=attempts+1 WHERE email=? AND purpose=?", (email, "login")
            )
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
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "created": user["created"],
    }


@router.get("/me")
def me(user: dict = Depends(resolve_user)):
    from . import plans  # local import to avoid cycle

    plan = plans.current_plan(user["id"])
    return {
        "user": public_user(user),
        "plan": {
            "tier": plan["tier"],
            "name": plan.get("name", plan["tier"]),
            "expires": plan["expires"],
            "concurrency": plan["concurrency"],
        },
        "credits": credits.balance(user["id"]),
    }


def clear_session_cookie(response: Response) -> None:
    """Delete the session cookie with the SAME attributes it was set with.

    A cookie is identified by (name, domain, path). Ours is set with
    COOKIE_DOMAIN so the workspace subdomain sees it; deleting without that
    domain removes a *different* cookie that never existed, and the real one
    survives — which is exactly why signing out appeared to do nothing.
    """
    extra = {"domain": config.COOKIE_DOMAIN} if config.COOKIE_DOMAIN else {}
    response.delete_cookie(config.SESSION_COOKIE, path="/", **extra)
    # Belt and braces: a host-only cookie may linger from before COOKIE_DOMAIN
    # was introduced, and it would keep the person signed in on the apex.
    if extra:
        response.delete_cookie(config.SESSION_COOKIE, path="/")


@router.post("/logout")
def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/logout")
def logout_get(request: Request):
    """A plain link that signs out and lands on the homepage.

    The JS handler needs fetch + JSON to succeed; if anything about that path
    breaks, the person is stuck signed in. A GET that works without JavaScript
    is the floor under that.
    """
    response = RedirectResponse(config.PUBLIC_BASE.rstrip("/") + "/", status_code=303)
    clear_session_cookie(response)
    return response


@router.post("/password")
def change_password(body: dict, user: dict = Depends(resolve_user)):
    old, new = str(body.get("old", "")), str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "password_too_short")
    if user["password_hash"] and not security.verify_password(old, user["password_hash"]):
        raise HTTPException(401, "bad_credentials")
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?",
            (security.hash_password(new), user["id"]),
        )
        conn.execute("UPDATE devices SET revoked=1 WHERE user_id=?", (user["id"],))
    return {"ok": True, "relogin": True}


@router.get("/devices")
def list_devices(user: dict = Depends(resolve_user)):
    rows = db.query(
        "SELECT id, name, platform, last_seen, created, revoked FROM devices "
        "WHERE user_id=? ORDER BY created DESC",
        (user["id"],),
    )
    return {"devices": [dict(r) for r in rows]}


@router.post("/devices/revoke")
def revoke_device(body: dict, user: dict = Depends(resolve_user)):
    device_id = str(body.get("device_id", ""))
    db.query("UPDATE devices SET revoked=1 WHERE id=? AND user_id=?", (device_id, user["id"]))
    return {"ok": True}


@router.post("/delete-account")
def delete_account(body: dict, user: dict = Depends(resolve_user)):
    try:
        confirmed_email = normalize_email_identity(body.get("confirm"))
    except HTTPException:
        raise HTTPException(400, "confirm_mismatch") from None
    if confirmed_email != user["email"]:
        raise HTTPException(400, "confirm_mismatch")
    uid, email = user["id"], user["email"]
    with db.tx() as conn:
        # Erasure, not a status flag. The privacy policy promises deletion of
        # personal data, so this has to actually remove it — an anonymised email
        # on a row still carrying the display name, password hash and every
        # session artefact is not what a data-subject request asks for.
        conn.execute(
            "UPDATE users SET status='deleted', session_epoch=session_epoch+1, "
            "email=?, display_name='', password_hash='' WHERE id=?",
            (f"deleted+{uid}@invalid.local", uid),
        )
        conn.execute("DELETE FROM devices WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM email_codes WHERE email=?", (email,))
        conn.execute("DELETE FROM org_invites WHERE email=?", (email,))
        conn.execute("DELETE FROM org_members WHERE user_id=?", (uid,))
        # orders / usage_log / credit_grants are deliberately KEPT: tax law
        # requires retaining payment records and the privacy policy says so
        # (5 years). Those rows carry a user id, not contact details.
    return {"ok": True}


# --- API keys ----------------------------------------------------------------
# 为什么要有这个: 网关是 OpenAI 兼容的, 官网也这么宣传, 但在此之前用户拿不到可用
# 凭据 —— 只能去桌面端的 cloud-auth.json 里把设备令牌抠出来。那样拦住的只有老实人,
# 而且令牌与登录态绑定 (改密码即失效), 拿去做集成很脆。


def _user_from_api_key(key: str) -> dict | None:
    """API key → 用户。与 devices 一样只按哈希查, 明文不落库。"""
    row = db.query_one(
        "SELECT id, user_id, revoked FROM api_keys WHERE key_hash=?", (security.token_hash(key),)
    )
    if row is None or int(row["revoked"]):
        return None
    user = _load_user(row["user_id"])
    if user is None or user["status"] != "active":
        return None
    # last_used 是用户排查"这把还在用吗"的唯一依据, 也是将来做异常检测的原料
    db.query("UPDATE api_keys SET last_used=? WHERE id=?", (time.time(), row["id"]))
    out = dict(user)
    out["device_id"] = ""  # API key 不绑设备
    out["api_key_id"] = row["id"]
    out["is_admin"] = user["email"].lower() in config.ADMIN_EMAILS or user["role"] == "admin"
    return out


@router.get("/api-keys")
def list_api_keys(user: dict = Depends(resolve_user)):
    rows = db.query(
        "SELECT id, prefix, label, created, last_used FROM api_keys "
        "WHERE user_id=? AND revoked=0 ORDER BY created DESC",
        (user["id"],),
    )
    return {"keys": [dict(r) for r in rows]}


@router.post("/api-keys")
def create_api_key(body: dict, user: dict = Depends(resolve_user)):
    label = str(body.get("label", "")).strip()[:64]
    live = db.query_one("SELECT COUNT(*) c FROM api_keys WHERE user_id=? AND revoked=0", (user["id"],))
    if int(live["c"]) >= API_KEY_MAX_PER_USER:
        raise HTTPException(400, "too_many_keys")
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    db.query(
        "INSERT INTO api_keys (id, user_id, key_hash, prefix, label, created) VALUES (?,?,?,?,?,?)",
        ("k_" + secrets.token_hex(12), user["id"], security.token_hash(key), key[:12], label, time.time()),
    )
    # 明文只在这一次返回 —— 库里只有哈希, 之后任何人 (包括我们) 都取不回来
    return {"ok": True, "key": key, "label": label}


@router.post("/api-keys/revoke")
def revoke_api_key(body: dict, user: dict = Depends(resolve_user)):
    db.query("UPDATE api_keys SET revoked=1 WHERE id=? AND user_id=?", (str(body.get("id", "")), user["id"]))
    return {"ok": True}
