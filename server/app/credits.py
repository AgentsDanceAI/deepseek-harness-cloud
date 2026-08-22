"""Credit ledger.

Grants are buckets with expiry (credit_grants); spending decrements bucket
`remaining` soonest-expiry-first inside one transaction. Balance is the sum of
unexpired remaining. In-flight streams are allowed to finish even when the
balance hits zero mid-request (the full overdraft is recorded against the last
bucket), matching the "never kill running work" principle.
"""

from __future__ import annotations

import time

from . import config, db, security


def grant(user_id: str, amount: int, ttl_s: float, kind: str, ref: str = "") -> str:
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    gid = security.new_id("grant_")
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO credit_grants (id, user_id, amount, remaining, expires, kind, ref, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (gid, user_id, amount, amount, now + ttl_s, kind, ref, now),
        )
    return gid


def _pools(user_id: str) -> list[str]:
    """Ledger holders this person draws on, in spend order.

    An organisation's shared credits live in the same table under the ORG id,
    so one bucket implementation serves both. The org pool is drawn FIRST: the
    company bought it for work like this, and a member's own top-up should be
    what is left over, not what subsidises the team.
    """
    row = db.query_one("SELECT org_id FROM org_members WHERE user_id=? LIMIT 1", (user_id,))
    org_id = row["org_id"] if row is not None else None
    return [org_id, user_id] if org_id else [user_id]


def balance(user_id: str) -> int:
    """Spendable total: the org pool (if any) plus this person's own credits."""
    now = time.time()
    total = 0
    for holder in _pools(user_id):
        row = db.query_one(
            "SELECT COALESCE(SUM(remaining),0) AS bal FROM credit_grants WHERE user_id=? AND expires>?",
            (holder, now),
        )
        total += int(row["bal"]) if row else 0
    return total


def personal_balance(user_id: str) -> int:
    """This person's own credits only — what the console shows beside the pool."""
    row = db.query_one(
        "SELECT COALESCE(SUM(remaining),0) AS bal FROM credit_grants WHERE user_id=? AND expires>?",
        (user_id, time.time()),
    )
    return int(row["bal"]) if row else 0


def spend(
    user_id: str,
    amount: int,
    *,
    kind: str,
    model: str = "",
    device_id: str = "",
    uncached_input: int = 0,
    cache_read: int = 0,
    output: int = 0,
    request_id: str = "",
) -> None:
    """Deduct `amount` credits and record a usage_log row. Never raises for
    insufficient funds — admission control happens before the request; the
    completed request is always billed truthfully (possible overdraft)."""
    if amount < 0:
        raise ValueError("spend amount must be >= 0")
    now = time.time()
    holders = _pools(user_id)
    with db.tx() as conn:
        if config.DB_BACKEND == "postgres":
            # Serialize every ledger holder, including an organisation shared by
            # different members. Row locks are insufficient when a holder has no
            # active grants, so use transaction-scoped advisory locks.
            for holder in dict.fromkeys(holders):
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                    (f"dsh-credit:{holder}",),
                )
        left = amount
        # org pool first, then personal; soonest-expiry bucket first within each
        for holder in holders:
            if left <= 0:
                break
            rows = conn.execute(
                "SELECT id, remaining FROM credit_grants WHERE user_id=? AND expires>? AND remaining>0 "
                "ORDER BY expires ASC",
                (holder, now),
            ).fetchall()
            for row in rows:
                if left <= 0:
                    break
                take = min(int(row["remaining"]), left)
                conn.execute("UPDATE credit_grants SET remaining=remaining-? WHERE id=?", (take, row["id"]))
                left -= take
        if left > 0:
            row = conn.execute(
                "SELECT id FROM credit_grants "
                "WHERE user_id=? AND kind='overdraft' AND expires>? ORDER BY created DESC",
                (user_id, now),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE credit_grants SET remaining=remaining-? WHERE id=?",
                    (left, row["id"]),
                )
            else:
                gid = security.new_id("grant_")
                conn.execute(
                    "INSERT INTO credit_grants "
                    "(id,user_id,amount,remaining,expires,kind,ref,created) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (gid, user_id, 0, -left, now + 10 * 365 * 86400, "overdraft", "", now),
                )
        conn.execute(
            "INSERT INTO usage_log (id, user_id, device_id, kind, model, uncached_input, cache_read, "
            "output, credits, request_id, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                security.new_id("use_"),
                user_id,
                device_id,
                kind,
                model,
                uncached_input,
                cache_read,
                output,
                amount,
                request_id,
                now,
            ),
        )


def usage_since(user_id: str, since: float) -> dict:
    row = db.query_one(
        "SELECT COALESCE(SUM(credits),0) AS credits, COUNT(*) AS calls, "
        "COALESCE(SUM(uncached_input+cache_read),0) AS input_tokens, "
        "COALESCE(SUM(output),0) AS output_tokens "
        "FROM usage_log WHERE user_id=? AND created>?",
        (user_id, since),
    )
    return (
        {k: int(row[k]) for k in ("credits", "calls", "input_tokens", "output_tokens")}
        if row
        else {"credits": 0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    )


def recent_usage(user_id: str, limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT model, kind, uncached_input, cache_read, output, credits, created "
        "FROM usage_log WHERE user_id=? ORDER BY created DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(r) for r in rows]
