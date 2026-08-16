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


def member_usage(org_id: str, since: float) -> list[dict]:
    """Per-member spend since `since` — the answer to 'who used the pool'."""
    rows = db.query(
        "SELECT u.email, COALESCE(SUM(l.credits),0) AS credits, COUNT(l.id) AS calls "
        "FROM org_members m JOIN users u ON u.id=m.user_id "
        "LEFT JOIN usage_log l ON l.user_id=m.user_id AND l.created>? "
        "WHERE m.org_id=? GROUP BY u.email ORDER BY credits DESC", (since, org_id))
    return [dict(r) for r in rows]


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


def grant_pool(org_id: str, amount: int, ttl_s: float, ref: str = "") -> str:
    """Top up the shared pool. Same ledger primitive as a personal grant."""
    return credits.grant(org_id, amount, ttl_s, kind="grant_team", ref=ref)


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
