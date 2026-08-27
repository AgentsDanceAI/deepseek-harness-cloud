"""SQLite / PostgreSQL dual-backend layer.

Callers write sqlite-style SQL (`?` placeholders, `with tx():` transactions,
`row["col"]` access) and this layer
translates for Postgres. Constructs that do not translate (INSERT OR REPLACE,
strftime, AUTOINCREMENT) are banned in app code; schema below sticks to the
portable subset.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_local = threading.local()
_pg_pool = None
_init_lock = threading.Lock()
_initialized = False

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        session_epoch INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        last_login REAL NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS email_codes (
        email TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        purpose TEXT NOT NULL,
        expires REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL
    )""",
    # API keys provide a dedicated credential for OpenAI-compatible clients.
    # Store only the SHA-256 digest; plaintext is returned once at creation and
    # can be replaced by creating a new key. This limits credential exposure if
    # the database is disclosed.
    """CREATE TABLE IF NOT EXISTS api_keys (
        id         TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        key_hash   TEXT NOT NULL UNIQUE,
        prefix     TEXT NOT NULL,          -- 明文前 12 位, 供列表页辨认是哪一把
        label      TEXT NOT NULL DEFAULT '',
        created    REAL NOT NULL,
        last_used  REAL,
        revoked    INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT '',
        token_hash TEXT UNIQUE NOT NULL,
        epoch INTEGER NOT NULL DEFAULT 0,
        revoked INTEGER NOT NULL DEFAULT 0,
        last_seen REAL NOT NULL DEFAULT 0,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS device_codes (
        device_code_hash TEXT PRIMARY KEY,
        user_code TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        user_id TEXT NOT NULL DEFAULT '',
        client_info TEXT NOT NULL DEFAULT '{}',
        expires REAL NOT NULL,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        user_id TEXT PRIMARY KEY,
        tier TEXT NOT NULL,
        cycle TEXT NOT NULL,
        started REAL NOT NULL,
        expires REAL NOT NULL,
        updated REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS credit_grants (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        amount INTEGER NOT NULL,
        remaining INTEGER NOT NULL,
        expires REAL NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS usage_log (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        uncached_input INTEGER NOT NULL DEFAULT 0,
        cache_read INTEGER NOT NULL DEFAULT 0,
        output INTEGER NOT NULL DEFAULT 0,
        credits INTEGER NOT NULL DEFAULT 0,
        request_id TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        item TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'CNY',
        status TEXT NOT NULL DEFAULT 'pending',
        provider_ref TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        paid_at REAL NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS consents (
        user_id TEXT NOT NULL,
        doc TEXT NOT NULL,
        version TEXT NOT NULL,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS kv (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    )""",
    # Organisations. Seats are what a company buys; the shared credit pool is
    # stored in credit_grants under the ORG id, so the bucket/expiry logic is
    # the same code that serves individuals (see credits._pools).
    """CREATE TABLE IF NOT EXISTS orgs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        seats INTEGER NOT NULL DEFAULT 1,
        seats_expires REAL NOT NULL DEFAULT 0,
        default_credit_cap INTEGER,
        default_minute_cap INTEGER,
        created REAL NOT NULL
    )""",
    # `credit_cap` / `minute_cap`: per-member ceilings for the shared pools.
    # NULL means "use the org default"; 0 means "blocked". Without these, one
    # person can spend the team's month in a day — the reason pooled billing
    # gets called unfair.
    """CREATE TABLE IF NOT EXISTS org_members (
        org_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member',
        joined REAL NOT NULL,
        credit_cap INTEGER,
        minute_cap INTEGER,
        PRIMARY KEY (org_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS org_invites (
        code TEXT PRIMARY KEY,
        org_id TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '',
        expires REAL NOT NULL,
        used_by TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    )""",
    # Purchased workspace minutes. Machine time is metered separately from
    # credits (a container costs RAM, not tokens), so it gets its own buckets —
    # same expiry-bucket shape as credit_grants, different unit.
    """CREATE TABLE IF NOT EXISTS minute_grants (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        amount INTEGER NOT NULL,
        remaining INTEGER NOT NULL,
        expires REAL NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    )""",
    # Cloud-workspace passes. The free allowance is derived from usage_log, so
    # only the purchased windows need storing; several may overlap (a renewal
    # bought early), and the latest `expires` wins.
    """CREATE TABLE IF NOT EXISTS work_passes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        started REAL NOT NULL,
        expires REAL NOT NULL,
        price INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT '',
        ref TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    )""",
    # 视频生成作业。聊天是一个请求打完就结束, 视频要几十秒到几分钟 —— 用户会
    # 关掉页面、会换设备, 所以作业状态必须落库, 不能只活在一次请求的生命周期里。
    #
    # credits 是**提交时预扣**的额度 (见 media.py 的说明), refunded 让退款幂等:
    # 轮询是客户端驱动的, 同一个失败作业会被查很多次, 不加这个会退很多次钱。
    """CREATE TABLE IF NOT EXISTS video_jobs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        model TEXT NOT NULL,
        upstream_task_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        prompt TEXT NOT NULL DEFAULT '',
        duration INTEGER NOT NULL DEFAULT 0,
        resolution TEXT NOT NULL DEFAULT '',
        credits INTEGER NOT NULL DEFAULT 0,
        refunded INTEGER NOT NULL DEFAULT 0,
        url TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        updated REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_grants_user ON credit_grants(user_id, expires)",
    "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_email_codes ON email_codes(email, purpose)",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id, revoked)",
    "CREATE INDEX IF NOT EXISTS idx_passes_user ON work_passes(user_id, expires)",
    "CREATE INDEX IF NOT EXISTS idx_mgrants_user ON minute_grants(user_id, expires)",
    "CREATE INDEX IF NOT EXISTS idx_vjobs_user ON video_jobs(user_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_org_invites_org ON org_invites(org_id)",
]


class _PgConn:
    """Adapts a psycopg connection to the sqlite calling convention."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        cur = self._conn.cursor()
        # psycopg reads % as the start of a placeholder, so a literal % in the
        # SQL — a LIKE pattern such as `LIKE 'dl_%'` — makes it reject the whole
        # statement with "only '%s', '%b', '%t' are allowed as placeholders".
        # Doubling literal percents is how psycopg wants them escaped. Order
        # matters: escape first, then swap ? for %s, or the %s we just wrote
        # would be escaped too.
        cur.execute(sql.replace("%", "%%").replace("?", "%s"), params)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


def _sqlite_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def _postgres_conn():
    global _pg_pool
    if _pg_pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool  # type: ignore[import-not-found]

        _pg_pool = ConnectionPool(
            config.POSTGRES_DSN,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
        )
    return _pg_pool


@contextmanager
def tx():
    """Transaction scope. Commits on success, rolls back on exception."""
    ensure_schema()
    if config.DB_BACKEND == "postgres":
        pool = _postgres_conn()
        with pool.connection() as raw:
            yield _PgConn(raw)
    else:
        conn = _sqlite_conn()
        with conn:
            yield conn


def query(sql: str, params: tuple = ()) -> list:
    """Run a statement and return its rows (empty list for writes).

    Callers use this for both SELECTs and one-off writes. SQLite is happy to
    fetchall() a DELETE (it just returns nothing); psycopg raises
    ProgrammingError("the last operation didn't produce records"). Swallowing
    that here keeps the two backends interchangeable, which is the whole point
    of this layer — the alternative is auditing every call site forever.
    """
    with tx() as conn:
        cur = conn.execute(sql, params)
        try:
            return list(cur.fetchall())
        except Exception as exc:  # noqa: BLE001 — narrow check below
            if "didn't produce records" in str(exc) or "no results to fetch" in str(exc):
                return []
            raise


def query_one(sql: str, params: tuple = ()):
    rows = query(sql, params)
    return rows[0] if rows else None


# Columns added to tables that already exist in deployed databases. CREATE TABLE
# IF NOT EXISTS is a no-op once the table is there, so new columns need their own
# idempotent step. (table, column, DDL type) — applied only when absent.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("org_members", "credit_cap", "INTEGER"),
    ("org_members", "minute_cap", "INTEGER"),
    ("orgs", "default_credit_cap", "INTEGER"),
    ("orgs", "default_minute_cap", "INTEGER"),
]


def _apply_migrations(execute, columns_of) -> None:
    for table, column, coltype in MIGRATIONS:
        try:
            if column in columns_of(table):
                continue
            execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception:  # noqa: BLE001 — a missing table is fine; SCHEMA creates it
            continue


def ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if config.DB_BACKEND == "postgres":
            pool = _postgres_conn()
            with pool.connection() as raw:
                for stmt in SCHEMA:
                    raw.execute(_pg_schema(stmt))

                def pg_columns(table: str) -> set:
                    cur = raw.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name=%s", (table,)
                    )
                    return {r[0] for r in cur.fetchall()}

                _apply_migrations(lambda s: raw.execute(_pg_schema(s)), pg_columns)
                raw.commit()
        else:
            conn = _sqlite_conn()
            with conn:
                for stmt in SCHEMA:
                    conn.execute(stmt)

                def sqlite_columns(table: str) -> set:
                    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

                _apply_migrations(conn.execute, sqlite_columns)
        _initialized = True


def _pg_schema(stmt: str) -> str:
    return stmt.replace("REAL", "DOUBLE PRECISION").replace("INTEGER", "BIGINT")


def now() -> float:
    return time.time()
