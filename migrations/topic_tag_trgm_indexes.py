"""
Trigram indexes on topic tags, so /brief/<slug> pages can find tags that
CONTAIN the topic ("support for ukraine" on /brief/ukraine) without reading the
whole tag table.

Run once on Railway as a one-shot service, like migrations/tagging_support.py:

    python migrations/topic_tag_trgm_indexes.py              # dry run: prints target DB, SQL, index state
    python migrations/topic_tag_trgm_indexes.py --execute    # apply

What it does (additive only — touches no rows):
  1. CREATE EXTENSION IF NOT EXISTS pg_trgm (ships with Postgres; needs a role
     allowed to create extensions — Railway's default postgres user is).
  2. GIN trigram indexes on lower(theme) for ha_pq_theme and ha_session_theme,
     built with CREATE INDEX CONCURRENTLY (no write lock). They serve the
     regex pre-filter in views._contained_topic_names.

The site works without them: each topic page's first view in a worker is just
slower (a full scan of the tag tables), then cached for 30 minutes.

If a CONCURRENTLY build is interrupted it leaves an INVALID index, and IF NOT
EXISTS would then skip it silently on a re-run. So after each index step the
script checks pg_index.indisvalid and stops with exit code 1 if the index is
missing or INVALID. To fix: DROP INDEX CONCURRENTLY it by name (Mark's call —
CLAUDE.md "Destructive operations") and re-run this script.

Idempotent (IF NOT EXISTS). No downgrade. Postgres only: on SQLite it does nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def steps(dialect: str) -> list[tuple[str, str, str | None]]:
    """(label, SQL, index name to check afterwards or None)."""
    if dialect != "postgresql":
        return []
    return [
        ("Enable pg_trgm", "CREATE EXTENSION IF NOT EXISTS pg_trgm", None),
        ("Trigram index lower(ha_pq_theme.theme)",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ha_pq_theme_lower_theme_trgm "
         "ON ha_pq_theme USING gin (lower(theme) gin_trgm_ops)",
         "ix_ha_pq_theme_lower_theme_trgm"),
        ("Trigram index lower(ha_session_theme.theme)",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ha_session_theme_lower_theme_trgm "
         "ON ha_session_theme USING gin (lower(theme) gin_trgm_ops)",
         "ix_ha_session_theme_lower_theme_trgm"),
    ]


_VALID_SQL = """
    SELECT i.indisvalid FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relname = :name
"""


def index_valid(conn, name: str) -> bool | None:
    """True / False from pg_index.indisvalid; None when the index does not exist."""
    row = conn.execute(text(_VALID_SQL), {"name": name}).first()
    return None if row is None else bool(row[0])


def apply(engine) -> list[str]:
    """Run every step (autocommit: CREATE INDEX CONCURRENTLY can't run in a
    transaction). Returns problems: indexes missing or INVALID after their step."""
    problems = []
    for label, sql, index in steps(engine.dialect.name):
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(sql))
            valid = index_valid(conn, index) if index else True
        if valid is not True:
            problems.append(f"{label}: {index} is {'missing' if valid is None else 'INVALID'}")
            break
        print(f"[trgm-migration] OK  {label}", flush=True)
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Trigram indexes on topic tags")
    parser.add_argument("--execute", action="store_true", help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[trgm-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        planned = steps(engine.dialect.name)
        if not planned:
            print("[trgm-migration] Not Postgres: nothing to do.", flush=True)
            return
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            with engine.connect() as conn:
                for label, sql, index in planned:
                    print(f"-- {label}\n{sql.strip()}", flush=True)
                    if index:
                        state = {None: "not there yet", True: "exists, valid",
                                 False: "exists, INVALID"}[index_valid(conn, index)]
                        print(f"--   {index}: {state}", flush=True)
                    print(flush=True)
            return
        problems = apply(engine)
        if problems:
            for p in problems:
                print(f"[trgm-migration] FAILED {p}. Drop it with DROP INDEX CONCURRENTLY "
                      "<name>; then re-run.", flush=True)
            sys.exit(1)
        print("[trgm-migration] Done.", flush=True)


if __name__ == "__main__":
    main()
