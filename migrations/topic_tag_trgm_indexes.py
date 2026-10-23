"""
Trigram indexes on topic tags, so /brief/<slug> pages can find tags that
CONTAIN the topic ("support for ukraine" on /brief/ukraine) without reading the
whole tag table.

Run once on Railway as a one-shot service, like migrations/tagging_support.py:

    python migrations/topic_tag_trgm_indexes.py              # dry run: prints target DB + SQL
    python migrations/topic_tag_trgm_indexes.py --execute    # apply

What it does (additive only — touches no rows):
  1. CREATE EXTENSION IF NOT EXISTS pg_trgm (ships with Postgres; needs a role
     allowed to create extensions — Railway's default postgres user is).
  2. GIN trigram indexes on lower(theme) for ha_pq_theme and ha_session_theme,
     built with CREATE INDEX CONCURRENTLY (no write lock). They serve the
     regex pre-filter in views._contained_topic_names.

The site works without them: each topic page's first view in a worker is just
slower (a full scan of the tag tables), then cached for 30 minutes.

If a CONCURRENTLY build is interrupted it leaves an INVALID index. Check with
    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
and, if one of these is listed, DROP INDEX CONCURRENTLY it by name (Mark's
call — CLAUDE.md "Destructive operations") and re-run this script.

Idempotent (IF NOT EXISTS). No downgrade. Postgres only: on SQLite it does nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def steps(dialect: str) -> list[tuple[str, str]]:
    if dialect != "postgresql":
        return []
    return [
        ("Enable pg_trgm", "CREATE EXTENSION IF NOT EXISTS pg_trgm"),
        ("Trigram index lower(ha_pq_theme.theme)",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ha_pq_theme_lower_theme_trgm "
         "ON ha_pq_theme USING gin (lower(theme) gin_trgm_ops)"),
        ("Trigram index lower(ha_session_theme.theme)",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ha_session_theme_lower_theme_trgm "
         "ON ha_session_theme USING gin (lower(theme) gin_trgm_ops)"),
    ]


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
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return
        for label, sql in planned:
            # CREATE INDEX CONCURRENTLY can't run inside a transaction.
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(sql))
            print(f"[trgm-migration] OK  {label}", flush=True)
        print("[trgm-migration] Done.", flush=True)


if __name__ == "__main__":
    main()
