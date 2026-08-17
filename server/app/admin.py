"""Admin endpoints. Authorization: the resolved user must be an admin
(ADMIN_EMAILS env or users.role='admin')."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from . import credits, db, plans, work_access
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
        "WHERE email LIKE ? ORDER BY created DESC LIMIT ?", (like, min(limit, 200)))
    out = []
    for r in rows:
        d = dict(r)
        d["credits"] = credits.balance(r["id"])
        d["plan"] = plans.current_plan(r["id"])["tier"]
        d["work_minutes_used"] = work_access.used_minutes(r["id"])
        d["work_minutes_included"] = work_access.included_minutes(r["id"])
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
        conn.execute("UPDATE users SET status=?, session_epoch=session_epoch+1 WHERE id=?",
                     (status, user_id))
    return {"ok": True}


@router.get("/stats")
def stats(_: dict = Depends(require_admin)):
    day_ago = time.time() - 86400
    return {
        "users": db.query_one("SELECT COUNT(*) AS n FROM users")["n"],
        "paid_orders": db.query_one("SELECT COUNT(*) AS n FROM orders WHERE status='paid'")["n"],
        "revenue_cents": db.query_one(
            "SELECT COALESCE(SUM(amount_cents),0) AS n FROM orders WHERE status='paid'")["n"],
        "calls_24h": db.query_one(
            "SELECT COUNT(*) AS n FROM usage_log WHERE created>?", (day_ago,))["n"],
        "credits_24h": db.query_one(
            "SELECT COALESCE(SUM(credits),0) AS n FROM usage_log WHERE created>?", (day_ago,))["n"],
    }
