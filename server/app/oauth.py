"""Google / GitHub OAuth login.

A provider activates when its client id + secret pair is set (see config).
Buttons on the login/activate pages 302 through /api/auth/{provider}/start;
the callback exchanges the code, requires a *verified* email, and logs into the
account keyed by that email (email IS the identity — shared with password and
email-code logins). Every failure path 302s back to /login with an error query
and never leaves a half-logged-in session.

CSRF: the state is an HMAC(auth_secret) signature over context + a per-flow
nonce + timestamp (10 min TTL, stateless — survives restarts / multi-instance).
The nonce is also dropped as a short httpOnly cookie the browser must return on
callback, binding the state to the browser that started the flow (RFC 6749
§10.12): an attacker's valid state replayed at the victim's browser fails the
signature because the victim holds no matching nonce cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from . import accounts, config, db, security
from .redirects import safe_local_path

log = logging.getLogger("dhc")

router = APIRouter(tags=["oauth"])

_STATE_TTL = 600  # signed-state / nonce cookie lifetime, seconds
_NONCE_COOKIE = "dhc_oauth_nonce"
_NEXT_COOKIE = "dhc_oauth_next"
_SECURE = not config.DEV_MODE


# --- signed state (CSRF) -----------------------------------------------------


def _signed_state(ctx: str, nonce: str = "") -> str:
    ts = str(int(time.time()))
    sig = hmac.new(security._secret(), f"{ctx}:{nonce}:{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def _signed_state_ok(ctx: str, state: str, nonce: str = "") -> bool:
    try:
        ts, sig = state.split(".", 1)
        if time.time() - int(ts) > _STATE_TTL:
            return False
    except (ValueError, TypeError):
        return False
    want = hmac.new(security._secret(), f"{ctx}:{nonce}:{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(want, sig)


def _issue_nonce(resp: RedirectResponse) -> str:
    """Drop a short-lived httpOnly nonce cookie; return the nonce (signed into state)."""
    nonce = secrets.token_urlsafe(16)
    resp.set_cookie(
        _NONCE_COOKIE, nonce, max_age=_STATE_TTL, httponly=True, samesite="lax", path="/", secure=_SECURE
    )
    return nonce


def _clear_flow_cookies(resp: RedirectResponse) -> None:
    resp.delete_cookie(_NONCE_COOKIE, path="/")
    resp.delete_cookie(_NEXT_COOKIE, path="/")


def _safe_next(nxt: str) -> str:
    """Backward-compatible name for the shared local redirect canonicalizer."""
    return safe_local_path(nxt, "/console")


def providers_configured() -> dict:
    """{'google': bool, 'github': bool} — a provider is on when its pair is set.
    Exposed so templates/webpages can show or hide the buttons."""
    return {
        "google": bool(config.GOOGLE_LOGIN_CLIENT_ID and config.GOOGLE_LOGIN_CLIENT_SECRET),
        "github": bool(config.GITHUB_LOGIN_CLIENT_ID and config.GITHUB_LOGIN_CLIENT_SECRET),
    }


# --- Google ------------------------------------------------------------------


@router.get("/api/auth/google/start")
async def google_start(next: str = ""):
    """Login-page button entry — 302 to Google's consent screen; unconfigured
    credentials 302 back to /login with a hint. Binds a nonce cookie for CSRF;
    the (validated) next path rides a short httpOnly cookie to the callback."""
    cid, secret = config.GOOGLE_LOGIN_CLIENT_ID, config.GOOGLE_LOGIN_CLIENT_SECRET
    if not (cid and secret):
        return RedirectResponse(
            "/login?google_error=" + urllib.parse.quote("Google 登录未配置"), status_code=302
        )
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth", status_code=302)
    nonce = _issue_nonce(resp)
    query = urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": config.GOOGLE_LOGIN_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": _signed_state("gglogin", nonce),
            "prompt": "select_account",  # let multi-account users pick each time
        }
    )
    resp.headers["location"] = f"https://accounts.google.com/o/oauth2/v2/auth?{query}"
    resp.set_cookie(
        _NEXT_COOKIE,
        _safe_next(next),
        max_age=_STATE_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_SECURE,
    )
    return resp


@router.get("/api/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Consent callback: code → token → userinfo verified email → login/auto-register.
    Any failure 302s back to /login with google_error — never a half-login."""

    def back(msg: str) -> RedirectResponse:
        r = RedirectResponse(f"/login?google_error={urllib.parse.quote(msg)}", status_code=302)
        _clear_flow_cookies(r)
        return r

    cid, secret = config.GOOGLE_LOGIN_CLIENT_ID, config.GOOGLE_LOGIN_CLIENT_SECRET
    if not (cid and secret):
        return back("Google 登录未配置")
    if error:
        return back("Google 授权已取消")
    if not code:
        return back("Google 授权失败, 请重试")
    if not _signed_state_ok("gglogin", state, request.cookies.get(_NONCE_COOKIE, "")):
        return back("登录状态已过期, 请重新发起")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            tr = await http.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": cid,
                    "client_secret": secret,
                    "redirect_uri": config.GOOGLE_LOGIN_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            td = tr.json() if tr.content else {}
            access_token = str(td.get("access_token") or "")
            if not access_token:
                log.warning("[google] token exchange failed: %s", td.get("error"))
                return back("Google 授权失败, 请重试")
            ur = await http.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if ur.status_code != 200:
                # Google-side 429/500 etc: an API fault, not "email unverified"
                log.warning("[google] userinfo failed: HTTP %s", ur.status_code)
                return back("Google 接口暂时不可用, 请稍后再试")
            ui = ur.json() if ur.content else {}
    except Exception:
        log.exception("[google] callback exchange error")
        return back("Google 接口暂时不可用, 请稍后再试")

    email = str(ui.get("email") or "").strip().lower()
    if not email or ui.get("email_verified") is not True:
        return back("该 Google 账号邮箱未验证, 无法登录")
    return _finish(request, email, str(ui.get("name") or "").strip(), back)


# --- GitHub ------------------------------------------------------------------


@router.get("/api/auth/github/start")
async def github_start(next: str = ""):
    """Login-page button entry — 302 to GitHub's consent screen. Same pattern as
    Google; scope user:email reads the address only, no repo/org access."""
    cid, secret = config.GITHUB_LOGIN_CLIENT_ID, config.GITHUB_LOGIN_CLIENT_SECRET
    if not (cid and secret):
        return RedirectResponse(
            "/login?github_error=" + urllib.parse.quote("GitHub 登录未配置"), status_code=302
        )
    resp = RedirectResponse("https://github.com/login/oauth/authorize", status_code=302)
    nonce = _issue_nonce(resp)
    query = urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": config.GITHUB_LOGIN_REDIRECT_URI,
            "scope": "user:email",
            "state": _signed_state("ghlogin", nonce),
        }
    )
    resp.headers["location"] = f"https://github.com/login/oauth/authorize?{query}"
    resp.set_cookie(
        _NEXT_COOKIE,
        _safe_next(next),
        max_age=_STATE_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_SECURE,
    )
    return resp


@router.get("/api/auth/github/callback")
async def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Consent callback: code → token → primary verified email → login/auto-register.
    Any failure 302s back to /login with github_error — never a half-login."""

    def back(msg: str) -> RedirectResponse:
        r = RedirectResponse(f"/login?github_error={urllib.parse.quote(msg)}", status_code=302)
        _clear_flow_cookies(r)
        return r

    cid, secret = config.GITHUB_LOGIN_CLIENT_ID, config.GITHUB_LOGIN_CLIENT_SECRET
    if not (cid and secret):
        return back("GitHub 登录未配置")
    if error:
        return back("GitHub 授权已取消")
    if not code:
        return back("GitHub 授权失败, 请重试")
    if not _signed_state_ok("ghlogin", state, request.cookies.get(_NONCE_COOKIE, "")):
        return back("登录状态已过期, 请重新发起")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            tr = await http.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "code": code,
                    "client_id": cid,
                    "client_secret": secret,
                    "redirect_uri": config.GITHUB_LOGIN_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
            td = tr.json() if tr.content else {}
            access_token = str(td.get("access_token") or "")
            if not access_token:
                log.warning("[github] token exchange failed: %s", td.get("error"))
                return back("GitHub 授权失败, 请重试")
            gh = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "dhc-cloud",
            }  # GitHub rejects requests with no UA
            ur = await http.get("https://api.github.com/user", headers=gh)
            er = await http.get("https://api.github.com/user/emails", headers=gh)
            if ur.status_code != 200 or er.status_code != 200:
                # GitHub-side 429/500 etc: an API fault, not "no verified email"
                log.warning("[github] user/emails failed: HTTP %s/%s", ur.status_code, er.status_code)
                return back("GitHub 接口暂时不可用, 请稍后再试")
            ui = ur.json() if ur.content else {}
            emails = er.json() if er.content else []
    except Exception:
        log.exception("[github] callback exchange error")
        return back("GitHub 接口暂时不可用, 请稍后再试")

    # primary verified email preferred, else first verified — unverified ones can
    # be squatted, so they are never trusted
    email = ""
    if isinstance(emails, list):
        verified = [e for e in emails if isinstance(e, dict) and e.get("verified") is True and e.get("email")]
        primary = [e for e in verified if e.get("primary") is True]
        email = str((primary or verified or [{}])[0].get("email") or "").strip().lower()
    if not email:
        return back("该 GitHub 账号无已验证邮箱, 无法登录")
    name = str(ui.get("name") or ui.get("login") or "").strip()
    return _finish(request, email, name, back)


# --- shared login tail -------------------------------------------------------


def _finish(request: Request, email: str, display_name: str, back):
    """Verified email → session cookie → 302 to the safe next (default /console)."""
    user = accounts.find_or_create_oauth_user(email, display_name)
    if user is None:
        return back("该邮箱尚未开通账号, 请联系管理员")
    if user["status"] != "active":
        return back("账号已停用, 请联系管理员")
    db.query("UPDATE users SET last_login=? WHERE id=?", (time.time(), user["id"]))
    dest = _safe_next(request.cookies.get(_NEXT_COOKIE, ""))
    resp = RedirectResponse(dest, status_code=302)
    _clear_flow_cookies(resp)
    accounts.set_session_cookie(resp, user)
    return resp
