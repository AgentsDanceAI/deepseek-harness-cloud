"""Copy an existing SQLite database into PostgreSQL.

Run once when switching DB_BACKEND from sqlite to postgres. Safe to re-run: it
refuses to touch a destination table that already has rows unless --force is
given, so a half-finished run cannot silently double every order.

    python server/scripts/migrate_sqlite_to_pg.py --sqlite /app/data/dhc.db

The destination schema is created by app.db.ensure_schema(), so the two stay in
step automatically — this script only moves rows and then PROVES it moved them
by comparing counts per table.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Order matters only for readability; there are no FK constraints in the schema.
TABLES = [
    "users",
    "email_codes",
    "devices",
    "device_codes",
    "subscriptions",
    "credit_grants",
    "minute_grants",
    "usage_log",
    "orders",
    "consents",
    "kv",
    "orgs",
    "org_members",
    "org_invites",
    "work_passes",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument(
        "--force", action="store_true", help="overwrite non-empty destination tables (deletes first)"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("DB_BACKEND", "postgres")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import db  # noqa: E402  (env must be set before import)

    db.ensure_schema()

    src = sqlite3.connect(args.sqlite)
    src.row_factory = sqlite3.Row

    moved, skipped, mismatched = {}, [], []
    for table in TABLES:
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 — fixed list
        except sqlite3.OperationalError:
            continue  # table absent in this older database
        dest_n = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
        dest_n = int((dest_n["n"] if dest_n is not None else 0) or 0)
        if dest_n and not args.force:
            skipped.append(f"{table} (destination already has {dest_n} rows)")
            continue
        if not rows:
            continue
        cols = list(rows[0].keys())
        placeholders = ",".join(["?"] * len(cols))
        collist = ",".join(cols)
        if args.dry_run:
            moved[table] = len(rows)
            continue
        with db.tx() as conn:
            if dest_n and args.force:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608
            for row in rows:
                conn.execute(
                    f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",  # noqa: S608
                    tuple(row[c] for c in cols),
                )
        moved[table] = len(rows)

    print("copied:")
    for table, n in sorted(moved.items()):
        print(f"  {table:16s} {n}")
    for s in skipped:
        print(f"  skipped {s}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    # Prove it: a migration that says "done" without checking is a guess.
    print("\nverifying row counts:")
    for table in TABLES:
        try:
            a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        except sqlite3.OperationalError:
            continue
        row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
        b = int((row["n"] if row is not None else 0) or 0)
        ok = a == b
        if not ok:
            mismatched.append(table)
        print(f"  {'OK ' if ok else 'BAD'} {table:16s} sqlite={a} postgres={b}")

    if mismatched:
        print(f"\nMISMATCH in {mismatched} — do NOT switch the backend", file=sys.stderr)
        return 1
    print("\nall tables match; safe to set DB_BACKEND=postgres")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
