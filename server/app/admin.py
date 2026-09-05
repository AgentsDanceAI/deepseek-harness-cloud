"""Admin endpoints. Authorization: the resolved user must be an admin
(ADMIN_EMAILS env or users.role='admin')."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from . import config, credits, db, plans, products, work_access
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


#: 扣积分的调用种类。grant_* 是发放、refund 是退款、workspace 是机时 (不扣积分) ——
#: 都不算"消耗", 算进去数字会对不上。
_SPEND_KINDS = ("llm", "search", "image", "video", "embedding")
#: 不经工作台的调用 (桌面端设备, 或网页/App 里用会话直接发的) 归到这一栏。
USAGE_DESKTOP = "desktop"
#: 带 device_id 但设备行已经不在的调用: 产品追不回来了, 单列一栏, 不冒充桌面端。
USAGE_UNATTRIBUTED = "unattributed"
_NON_PRODUCT = (USAGE_DESKTOP, USAGE_UNATTRIBUTED)


@router.get("/usage")
def usage(user_id: str = "", days: int = 30, _: dict = Depends(require_admin)):
    """按产品拆开的消耗: 机时 + 积分 + 调用次数。user_id 空 = 全站。

    两种资源的归属方式不同, 因为记账时留下的线索不同:
      * 机时: 回收器每分钟给每个工作台记一行 (kind=workspace, model=work:<产品>),
        产品就写在 model 里 —— 直接按它分组。
      * 积分: 网关按调用记账, 行上只有 device_id; 工作台的凭据是一台 platform=cloud
        的设备, 它的 workspace 列是工作台键 (u_xxx~<产品>, 默认产品没有 ~) ——
        经设备找到产品。桌面端/App 的设备没有 workspace, 归到 "desktop"。
    """
    days = max(0, min(int(days), 3650))
    since = time.time() - days * 86400 if days else 0.0
    agg: dict[str, dict[str, int]] = {}

    def bucket(pid: str) -> dict[str, int]:
        return agg.setdefault(pid, {"minutes": 0, "credits": 0, "calls": 0})

    scope = "AND user_id=?" if user_id else ""
    args: tuple = (user_id,) if user_id else ()
    for r in db.query(
        f"SELECT model, COUNT(*) AS n FROM usage_log WHERE kind=? AND created>? {scope} GROUP BY model",
        (work_access.MINUTE_KIND, since, *args),
    ):
        model = r["model"] or ""
        pid = model[len("work:") :] if model.startswith("work:") else products.DEFAULT
        bucket(pid)["minutes"] += int(r["n"] or 0)

    marks = ",".join("?" * len(_SPEND_KINDS))
    scope_u = "AND u.user_id=?" if user_id else ""
    # 归属线索分三种: 没有 device_id 的是网页/App 里凭会话直接发的; 有 device_id 且设备
    # 还在的, 按设备的 workspace 归产品 (桌面设备没有 workspace → 桌面端); 有 device_id
    # 但设备行没了的, 是早先铸币时"只留最近两份、其余删掉"清理掉的工作台凭据 —— 产品
    # 追不回来。2026-09-04 线上 30 天里这类行占积分消耗的四分之一, 混进桌面端会误导。
    ws_expr = (
        "CASE WHEN u.device_id IS NULL OR u.device_id='' THEN '' "
        "WHEN d.id IS NULL THEN '?' ELSE COALESCE(d.workspace,'') END"
    )
    for r in db.query(
        f"SELECT {ws_expr} AS ws, COUNT(*) AS calls, COALESCE(SUM(u.credits),0) AS credits "
        "FROM usage_log u LEFT JOIN devices d ON d.id=u.device_id "
        f"WHERE u.kind IN ({marks}) AND u.created>? {scope_u} GROUP BY 1",
        (*_SPEND_KINDS, since, *args),
    ):
        ws = r["ws"] or ""
        if not ws:
            pid = USAGE_DESKTOP
        elif ws == "?":
            pid = USAGE_UNATTRIBUTED
        else:
            pid = products.split_key(ws)[1]
        b = bucket(pid)
        b["credits"] += int(r["credits"] or 0)
        b["calls"] += int(r["calls"] or 0)

    reg = products.registry()
    items = [
        {"id": pid, "name": reg[pid].name if pid in reg else ("" if pid in _NON_PRODUCT else pid), **b}
        for pid, b in agg.items()
        if any(b.values())
    ]
    # 产品按花费降序; 两个非产品桶固定压在最后, 别让"桌面端"顶在产品报表的第一行。
    items.sort(
        key=lambda x: (
            _NON_PRODUCT.index(x["id"]) + 1 if x["id"] in _NON_PRODUCT else 0,
            -x["credits"],
            -x["minutes"],
            x["id"],
        )
    )
    totals = {k: sum(i[k] for i in items) for k in ("minutes", "credits", "calls")}
    return {"days": days, "user_id": user_id, "products": items, "totals": totals}


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
