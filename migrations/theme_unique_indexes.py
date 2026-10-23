"""
Unique index on ha_session_theme (session_id, theme, theme_type), so a topic
tag can't be stored twice for the same debate.

RUN scripts/dedupe_theme_rows.py --execute FIRST: the index can't be built
while duplicates exist. This script checks and refuses (exit 1) if any remain.

Deploy order:
  1. the tagger fix (write_session_tags: each tag once, per-session lock)
     deployed to the web service AND every cron service (a push to master
     redeploys them all; check none is still on an older build)
  2. python scripts/dedupe_theme_rows.py              (dry run; review)
  3. python scripts/dedupe_theme_rows.py --execute
  4. python migrations/theme_unique_indexes.py        (dry run: 0 duplicates)
  5. python migrations/theme_unique_indexes.py --execute

Run once on Railway as a one-shot service (SKIP_MIGRATIONS=1, Dockerfile.cron):

    python migrations/theme_unique_indexes.py              # dry run: target, duplicates, SQL, index state
    python migrations/theme_unique_indexes.py --execute    # apply

Additive only: touches no rows. Built with CREATE UNIQUE INDEX CONCURRENTLY
(no write lock on the table). ha_pq_theme needs nothing: its primary key
(pq_id, theme) already prevents duplicates.

The app does not need this index: the tagger writes each tag once and takes a
per-session advisory lock while writing (hansard_archive/tagger.py
write_session_tags). The index is the database-level backstop for anything
else that writes the table.

If a CONCURRENTLY build is interrupted (or hits a duplicate inserted during the
build) it leaves an INVALID index, and IF NOT EXISTS would skip it silently on
a re-run. So afterwards (also when the build raised an error) the script
checks pg_index.indisvalid and exits 1 if the index is missing or INVALID.
A failed CONCURRENTLY build of a unique index can be left "ready but invalid":
queries don't use it, but it still rejects duplicate inserts until it is
dropped. Tag writers then fail on a duplicate instead of storing it, which the
tagger never attempts. To fix: DROP INDEX CONCURRENTLY
uq_ha_session_theme_session_theme_type (Mark's call — CLAUDE.md "Destructive
operations"), run the dedupe again, then re-run this script.

Idempotent (IF NOT EXISTS). No downgrade. Postgres only: on SQLite it does nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

INDEX = "uq_ha_session_theme_session_theme_type"
CREATE_SQL = (f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
              "ON ha_session_theme (session_id, theme, theme_type)")
DUPLICATES_SQL = """
    SELECT COUNT(*) FROM (
        SELECT 1 FROM ha_session_theme
        GROUP BY session_id, theme, theme_type
        HAVING COUNT(*) > 1
    ) d
"""
_VALID_SQL = """
    SELECT i.indisvalid FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relname = :name
"""


def index_valid(conn, name: str = INDEX) -> bool | None:
    """True / False from pg_index.indisvalid; None when the index does not exist."""
    row = conn.execute(text(_VALID_SQL), {"name": name}).first()
    return None if row is None else bool(row[0])


def duplicate_groups(conn) -> int:
    return conn.execute(text(DUPLICATES_SQL)).scalar() or 0


def apply(engine) -> str | None:
    """Build the index (autocommit: CONCURRENTLY can't run in a transaction).
    Returns a problem description, or None when the index exists and is valid."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        dups = duplicate_groups(conn)
        if dups:
            return (f"{dups} duplicate (session_id, theme, theme_type) group(s) in ha_session_theme: "
                    "run scripts/dedupe_theme_rows.py --execute first")
        error = ""
        try:
            conn.execute(text(CREATE_SQL))
        except Exception as e:  # noqa: BLE001 - e.g. a duplicate inserted during the build
            error = f"the build failed ({type(e).__name__}: {str(e)[:300]}); "
    with engine.connect() as conn:
        valid = index_valid(conn)
    if error or valid is not True:
        state = "missing" if valid is None else "INVALID" if valid is False else "valid"
        fix = (f"Drop it with DROP INDEX CONCURRENTLY {INDEX}; run the dedupe again, then re-run "
               "this script." if valid is not None else "Run the dedupe again, then re-run this script.")
        return f"{error}{INDEX} is {state} after the build. {fix}"
    return None


def main(argv=None, engine=None) -> int:
    parser = argparse.ArgumentParser(description="Unique index on ha_session_theme tags")
    parser.add_argument("--execute", action="store_true", help="Actually run the SQL (default: dry run)")
    args = parser.parse_args(argv)

    ctx = None
    if engine is None:
        os.environ.setdefault("SKIP_MIGRATIONS", "1")
        from flask_app import app
        from extensions import db
        ctx = app.app_context()
        ctx.push()
        engine = db.engine
    try:
        print(f"[theme-index] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name != "postgresql":
            print("[theme-index] Not Postgres: nothing to do.", flush=True)
            return 0
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            with engine.connect() as conn:
                print(f"-- duplicate groups now: {duplicate_groups(conn)} (must be 0)", flush=True)
                print(f"{CREATE_SQL}", flush=True)
                state = {None: "not there yet", True: "exists, valid",
                         False: "exists, INVALID"}[index_valid(conn)]
                print(f"--   {INDEX}: {state}", flush=True)
            return 0
        problem = apply(engine)
        if problem:
            print(f"[theme-index] FAILED: {problem}", flush=True)
            return 1
        print(f"[theme-index] OK  {INDEX} exists and is valid. Done.", flush=True)
        return 0
    finally:
        if ctx is not None:
            ctx.pop()


if __name__ == "__main__":
    sys.exit(main())
