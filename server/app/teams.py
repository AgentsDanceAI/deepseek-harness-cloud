"""Organisations: seats to buy, one credit pool to share.

What a company actually wants is not N personal accounts but one balance several
people draw from, with visibility into who spent what. So:

  * the shared pool is ordinary `credit_grants` rows held under the ORG id —
    `credits._pools` puts it ahead of the member's own credits when spending,
    and every charge is still logged against the member who incurred it;
  * seats bound membership. Buying seats does not create users; it sets how many
    may be in the org, so the owner can invite and rotate people freely.

One person belongs to at most one org — a shared wallet with ambiguous priority
would make "who paid for this" unanswerable, which is the question the feature
exists to answer.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from . import config, credits, db, security
from .accounts import resolve_user

router = APIRouter(prefix="/api/team", tags=["team"])

INVITE_TTL_S = 14 * 86400


# --- model -------------------------------------------------------------------

def org_of(user_id: str) -> dict | None:
    row = db.query_one(
        "SELECT o.*, m.role FROM orgs o JOIN org_members m ON m.org_id=o.id WHERE m.user_id=?",
        (user_id,))
    return dict(row) if row is not None else None


def members(org_id: str) -> list[dict]:
    rows = db.query(
        "SELECT m.user_id, m.role, m.joined, u.email FROM org_members m "
        "JOIN users u ON u.id=m.user_id WHERE m.org_id=? ORDER BY m.joined", (org_id,))
    return [dict(r) for r in rows]


def seats_used(org_id: str) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM org_members WHERE org_id=?", (org_id,))
    return int((row["n"] if row is not None else 0) or 0)


def pool_balance(org_id: str) -> int:
    row = db.query_one(
        "SELECT COALESCE(SUM(remaining),0) AS bal FROM credit_grants WHERE user_id=? AND expires>?",
        (org_id, time.time()))
    return int(row["bal"]) if row else 0


def minute_pool(org_id: str) -> int:
    """Unexpired workspace minutes held by the organisation."""
    row = db.query_one(
        "SELECT COALESCE(SUM(remaining),0) AS n FROM minute_grants WHERE user_id=? AND expires>?",
        (org_id, time.time()))
    return int((row["n"] if row is not None else 0) or 0)


def member_usage(org_id: str, since: float) -> list[dict]:
    """Per-member consumption since `since` — the answer to 'who used the pool'.

    Credits and minutes are reported separately because they are separate
    resources: tokens vs machine time. An admin sizing next month's seats needs
    both, and one number hiding the other is how pooled billing loses trust.
    """
    rows = db.query(
        "SELECT m.user_id, u.email, m.role, m.credit_cap, m.minute_cap, "
        "COALESCE(SUM(CASE WHEN l.kind!='workspace' THEN l.credits ELSE 0 END),0) AS credits, "
        "COALESCE(SUM(CASE WHEN l.kind='workspace' THEN 1 ELSE 0 END),0) AS minutes, "
        "COALESCE(SUM(CASE WHEN l.kind NOT IN ('workspace') THEN 1 ELSE 0 END),0) AS calls "
        "FROM org_members m JOIN users u ON u.id=m.user_id "
        "LEFT JOIN usage_log l ON l.user_id=m.user_id AND l.created>? "
        "WHERE m.org_id=? GROUP BY m.user_id, u.email, m.role, m.credit_cap, m.minute_cap "
        "ORDER BY credits DESC", (since, org_id))
    return [dict(r) for r in rows]


def member_row(org_id: str, user_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM org_members WHERE org_id=? AND user_id=?", (org_id, user_id))
    return dict(row) if row is not None else None


def set_member_caps(org_id: str, user_id: str, *, credit_cap: int | None = ...,
                    minute_cap: int | None = ...) -> None:
    """Ceilings for one member. None = follow the org default; 0 = blocked."""
    sets, params = [], []
    if credit_cap is not ...:
        sets.append("credit_cap=?")
        params.append(credit_cap)
    if minute_cap is not ...:
        sets.append("minute_cap=?")
        params.append(minute_cap)
    if not sets:
        return
    params += [org_id, user_id]
    with db.tx() as conn:
        conn.execute(f"UPDATE org_members SET {', '.join(sets)} WHERE org_id=? AND user_id=?",
                     tuple(params))


def _period_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def member_used(user_id: str, since: float | None = None) -> tuple[int, int]:
    """(credits, minutes) this member consumed this period."""
    since = _period_start() if since is None else since
    row = db.query_one(
        "SELECT COALESCE(SUM(CASE WHEN kind!='workspace' THEN credits ELSE 0 END),0) AS credits, "
        "COALESCE(SUM(CASE WHEN kind='workspace' THEN 1 ELSE 0 END),0) AS minutes "
        "FROM usage_log WHERE user_id=? AND created>?", (user_id, since))
    if row is None:
        return 0, 0
    return int(row["credits"] or 0), int(row["minutes"] or 0)


def effective_caps(org: dict, user_id: str) -> tuple[int | None, int | None]:
    """(credit_cap, minute_cap) for this member: own override, else org default,
    else unlimited."""
    m = member_row(org["id"], user_id) or {}
    credit_cap = m.get("credit_cap")
    minute_cap = m.get("minute_cap")
    if credit_cap is None:
        credit_cap = org.get("default_credit_cap")
    if minute_cap is None:
        minute_cap = org.get("default_minute_cap")
    return credit_cap, minute_cap


def work_state(org: dict, user_id: str, personal: dict) -> dict:
    """Workspace-minute view for a member of an organisation.

    The org pool is added ON TOP of the member's own plan allowance — joining a
    team must never take away what someone already had. The member's own ceiling
    is checked first, so hitting a cap stops THAT member and nobody else.
    """
    pool = minute_pool(org["id"])
    _credit_cap, minute_cap = effective_caps(org, user_id)
    _used_credits, used_minutes = member_used(user_id)
    total_left = int(personal["minutes_left"]) + pool
    capped_left = None if minute_cap is None else max(0, int(minute_cap) - used_minutes)
    left = total_left if capped_left is None else min(total_left, capped_left)
    blocked = None
    if capped_left is not None and capped_left <= 0:
        blocked = "member_cap"
    elif total_left <= 0:
        blocked = "work_quota"
    out = dict(personal)
    out.update({
        "scope": "org",
        "org_id": org["id"],
        "org_name": org["name"],
        "plan_name": org["name"],
        "org_pool_minutes": pool,
        "minutes_left": left,
        "member_minute_cap": minute_cap,
        "member_minutes_used": used_minutes,
        "allowed": blocked is None,
        "blocked_reason": blocked,
        "free_minutes_left": left,
    })
    return out


def create_org(owner_id: str, name: str, seats: int = 1) -> str:
    if org_of(owner_id):
        raise HTTPException(409, "already_in_org")
    org_id = security.new_id("org_")
    now = time.time()
    with db.tx() as conn:
        conn.execute("INSERT INTO orgs (id, name, owner_id, seats, seats_expires, created) "
                     "VALUES (?,?,?,?,?,?)", (org_id, name[:80], owner_id, max(1, seats), 0, now))
        conn.execute("INSERT INTO org_members (org_id, user_id, role, joined) VALUES (?,?,?,?)",
                     (org_id, owner_id, "owner", now))
    return org_id


def set_seats(org_id: str, seats: int, expires: float) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE orgs SET seats=?, seats_expires=? WHERE id=?",
                     (max(1, seats), expires, org_id))


def set_default_caps(org_id: str, *, credit_cap: int | None = ...,
                     minute_cap: int | None = ...) -> None:
    """Org-wide ceilings applied to members without their own override."""
    sets, params = [], []
    if credit_cap is not ...:
        sets.append("default_credit_cap=?")
        params.append(credit_cap)
    if minute_cap is not ...:
        sets.append("default_minute_cap=?")
        params.append(minute_cap)
    if not sets:
        return
    params.append(org_id)
    with db.tx() as conn:
        conn.execute(f"UPDATE orgs SET {', '.join(sets)} WHERE id=?", tuple(params))


def grant_pool(org_id: str, amount: int, ttl_s: float, ref: str = "") -> str:
    """Top up the shared CREDIT pool. Same ledger primitive as a personal grant."""
    return credits.grant(org_id, amount, ttl_s, kind="grant_team", ref=ref)


def grant_minute_pool(org_id: str, minutes: int, ttl_s: float, ref: str = "") -> str:
    """Top up the shared MACHINE-TIME pool — the other resource seats buy."""
    from . import work_access
    return work_access.grant_minutes(org_id, minutes, ttl_s, kind="grant_team", ref=ref)


def credit_cap_exceeded(user_id: str) -> bool:
    """True when this member has hit their own ceiling on the shared credit pool.

    Checked before a request is admitted, so hitting a cap stops that member and
    leaves the rest of the team working.
    """
    org = org_of(user_id)
    if org is None:
        return False
    credit_cap, _minute_cap = effective_caps(org, user_id)
    if credit_cap is None:
        return False
    used_credits, _used_minutes = member_used(user_id)
    return used_credits >= int(credit_cap)


def create_invite(org_id: str, email: str = "") -> str:
    code = security.new_id("inv_")[4:].upper()[:10]
    now = time.time()
    with db.tx() as conn:
        conn.execute("INSERT INTO org_invites (code, org_id, email, expires, created) "
                     "VALUES (?,?,?,?,?)", (code, org_id, email.strip().lower(), now + INVITE_TTL_S, now))
    return code


def accept_invite(code: str, user_id: str) -> str:
    row = db.query_one("SELECT * FROM org_invites WHERE code=?", (code.strip().upper(),))
    if row is None or row["used_by"] or row["expires"] < time.time():
        raise HTTPException(400, "invite_invalid")
    org_id = row["org_id"]
    org = db.query_one("SELECT seats FROM orgs WHERE id=?", (org_id,))
    if org is None:
        raise HTTPException(400, "invite_invalid")
    if org_of(user_id):
        raise HTTPException(409, "already_in_org")
    if seats_used(org_id) >= int(org["seats"]):
        raise HTTPException(409, "no_seats")
    now = time.time()
    with db.tx() as conn:
        conn.execute("INSERT INTO org_members (org_id, user_id, role, joined) VALUES (?,?,?,?)",
                     (org_id, user_id, "member", now))
        conn.execute("UPDATE org_invites SET used_by=? WHERE code=?", (user_id, row["code"]))
    return org_id


def remove_member(org_id: str, user_id: str) -> None:
    org = db.query_one("SELECT owner_id FROM orgs WHERE id=?", (org_id,))
    if org is not None and org["owner_id"] == user_id:
        raise HTTPException(400, "cannot_remove_owner")
    with db.tx() as conn:
        conn.execute("DELETE FROM org_members WHERE org_id=? AND user_id=?", (org_id, user_id))


# --- API ---------------------------------------------------------------------

def _require_owner(user: dict) -> dict:
    org = org_of(user["id"])
    if org is None:
        raise HTTPException(404, "no_org")
    if org["role"] != "owner":
        raise HTTPException(403, "not_owner")
    return org


@router.get("/me")
def team_me(user: dict = Depends(resolve_user)):
    org = org_of(user["id"])
    if org is None:
        return {"in_org": False, "seat_price": config.TEAM_SEAT_PRICE}
    lt = time.localtime()
    month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    return {
        "in_org": True,
        "org_id": org["id"],
        "name": org["name"],
        "role": org["role"],
        "seats": org["seats"],
        "seats_used": seats_used(org["id"]),
        "pool_balance": pool_balance(org["id"]),
        "members": members(org["id"]) if org["role"] == "owner" else [],
        "usage": member_usage(org["id"], month_start) if org["role"] == "owner" else [],
        "seat_price": config.TEAM_SEAT_PRICE,
    }


@router.post("/create")
def team_create(body: dict, user: dict = Depends(resolve_user)):
    name = str(body.get("name", "")).strip() or f"{user['email'].split('@')[0]} 的团队"
    org_id = create_org(user["id"], name)
    return {"ok": True, "org_id": org_id}


@router.post("/invite")
def team_invite(body: dict, user: dict = Depends(resolve_user)):
    org = _require_owner(user)
    if seats_used(org["id"]) >= int(org["seats"]):
        raise HTTPException(409, "no_seats")
    code = create_invite(org["id"], str(body.get("email", "")))
    return {"ok": True, "code": code,
            "url": f"{config.PUBLIC_BASE.rstrip('/')}/team/join?code={code}"}


@router.post("/join")
def team_join(body: dict, user: dict = Depends(resolve_user)):
    org_id = accept_invite(str(body.get("code", "")), user["id"])
    return {"ok": True, "org_id": org_id}


@router.post("/remove")
def team_remove(body: dict, user: dict = Depends(resolve_user)):
    org = _require_owner(user)
    remove_member(org["id"], str(body.get("user_id", "")))
    return {"ok": True}
