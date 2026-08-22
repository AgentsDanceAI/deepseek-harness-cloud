"""Admin endpoints. Authorization: the resolved user must be an admin
(ADMIN_EMAILS env or users.role='admin')."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from . import config, credits, db, plans, work_access
from .accounts import resolve_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: dict = Depends(resolve_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "admin_only")
    return user


@router.get("/users")
def list_users(q: str = "", limit: int = 50, _: dict = Depends(require_admin)):
    like = f"%{q}%"
    rows = db.query(
        "SELECT id, email, display_name, role, status, created, last_login FROM users "
        "WHERE email LIKE ? ORDER BY created DESC LIMIT ?",
        (like, min(limit, 200)),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["credits"] = credits.balance(r["id"])
        d["plan"] = plans.current_plan(r["id"])["tier"]
        d["work_minutes_used"] = work_access.used_minutes(r["id"])
        d["work_minutes_included"] = work_access.included_minutes(r["id"])
        # Same rule accounts.resolve_user applies, so the button reflects the
        # rights the user actually has rather than only the stored role.
        d["is_admin"] = (r["email"] or "").lower() in config.ADMIN_EMAILS or r["role"] == "admin"
        d["admin_from_env"] = (r["email"] or "").lower() in config.ADMIN_EMAILS
        out.append(d)
    return {"users": out}


@router.post("/grant-credits")
def grant_credits(body: dict, admin: dict = Depends(require_admin)):
    user_id = str(body.get("user_id", ""))
    amount = int(body.get("amount", 0))
    days = int(body.get("valid_days", 365))
    if amount <= 0 or not db.query_one("SELECT id FROM users WHERE id=?", (user_id,)):
        raise HTTPException(400, "bad_request")
    gid = credits.grant(user_id, amount, days * 86400, kind="grant_admin", ref=admin["email"])
    return {"ok": True, "grant_id": gid, "balance": credits.balance(user_id)}


@router.post("/set-plan")
def set_plan(body: dict, _: dict = Depends(require_admin)):
    user_id = str(body.get("user_id", ""))
    tier = str(body.get("tier", ""))
    cycle = str(body.get("cycle", "monthly"))
    plans.apply_plan(user_id, tier, cycle, order_id="admin")
    return {"ok": True}


@router.post("/set-status")
def set_status(body: dict, _: dict = Depends(require_admin)):
    user_id = str(body.get("user_id", ""))
    status = str(body.get("status", ""))
    if status not in ("active", "suspended"):
        raise HTTPException(400, "bad_status")
    with db.tx() as conn:
        conn.execute("UPDATE users SET status=?, session_epoch=session_epoch+1 WHERE id=?", (status, user_id))
    return {"ok": True}


@router.get("/stats")
def stats(_: dict = Depends(require_admin)):
    day_ago = time.time() - 86400
    return {
        "users": db.query_one("SELECT COUNT(*) AS n FROM users")["n"],
        "paid_orders": db.query_one("SELECT COUNT(*) AS n FROM orders WHERE status='paid'")["n"],
        "revenue_cents": db.query_one(
            "SELECT COALESCE(SUM(amount_cents),0) AS n FROM orders WHERE status='paid'"
        )["n"],
        "calls_24h": db.query_one("SELECT COUNT(*) AS n FROM usage_log WHERE created>?", (day_ago,))["n"],
        "credits_24h": db.query_one(
            "SELECT COALESCE(SUM(credits),0) AS n FROM usage_log WHERE created>?", (day_ago,)
        )["n"],
    }


@router.post("/set-role")
def set_role(body: dict, user: dict = Depends(require_admin)):
    """Grant or revoke admin rights.

    Three guards, each protecting against a way of locking everyone out:
      * you cannot demote yourself — the usual way an admin panel loses its
        last admin is someone testing the button on their own account;
      * the last remaining admin cannot be demoted at all;
      * an admin who comes from ADMIN_EMAILS cannot be demoted here, because
        the env would grant it straight back on the next request and the UI
        would be lying about what it just did.
    """
    target_id = str(body.get("user_id", "")).strip()
    make_admin = bool(body.get("admin"))
    if not target_id:
        raise HTTPException(400, "user_id_required")
    if target_id == user["id"] and not make_admin:
        raise HTTPException(400, "cannot_demote_self")

    row = db.query_one("SELECT id, email, role FROM users WHERE id=?", (target_id,))
    if row is None:
        raise HTTPException(404, "user_not_found")

    if not make_admin:
        if (row["email"] or "").lower() in config.ADMIN_EMAILS:
            raise HTTPException(400, "admin_from_env")
        others = db.query_one(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND id<>? AND status<>'deleted'", (target_id,)
        )
        env_admins = len(config.ADMIN_EMAILS)
        if int((others["n"] if others else 0) or 0) + env_admins == 0:
            raise HTTPException(400, "last_admin")

    db.query("UPDATE users SET role=? WHERE id=?", ("admin" if make_admin else "user", target_id))
    return {"ok": True, "user_id": target_id, "admin": make_admin}
