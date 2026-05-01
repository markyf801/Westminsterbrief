"""
Hansard Archive — Postgres full-text search index setup.

Adds tsvector columns + GIN indexes to ha_session and ha_contribution.
Safe to re-run: uses IF NOT EXISTS / catches 'already exists' errors.

Run once against Railway Postgres, then never again (GENERATED ALWAYS AS
STORED keeps new rows indexed automatically).

Usage:
  python scripts/add_fts_index.py          # dry run (prints SQL, no execute)
  python scripts/add_fts_index.py --execute

Environment:
  DATABASE_URL  — must be set to a Postgres connection string.
  If not set, script exits with a clear error (SQLite has no tsvector support).
"""

import argparse
import os
import sys

sys.path.insert(0, ".")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add FTS tsvector columns + GIN indexes")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or "postgresql" not in db_url.lower():
        sys.exit(
            "ERROR: DATABASE_URL not set or is not a Postgres URL.\n"
            "       This script requires Postgres — tsvector is not available on SQLite.\n"
            "       Set DATABASE_URL to your Railway Postgres connection string and retry."
        )

    from flask_app import app
    from extensions import db
    from sqlalchemy import text

    steps = [
        # ha_session: generated tsvector on title (weight A)
        (
            "Add title_tsv column to ha_session",
            """
            ALTER TABLE ha_session
            ADD COLUMN IF NOT EXISTS title_tsv tsvector
                GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, ''))) STORED
            """,
        ),
        (
            "GIN index on ha_session.title_tsv",
            """
            CREATE INDEX IF NOT EXISTS ix_ha_session_title_tsv
            ON ha_session USING GIN (title_tsv)
            """,
        ),
        # ha_contribution: generated tsvector on speech_text (weight B)
        (
            "Add speech_tsv column to ha_contribution",
            """
            ALTER TABLE ha_contribution
            ADD COLUMN IF NOT EXISTS speech_tsv tsvector
                GENERATED ALWAYS AS (to_tsvector('english', coalesce(speech_text, ''))) STORED
            """,
        ),
        (
            "GIN index on ha_contribution.speech_tsv",
            """
            CREATE INDEX IF NOT EXISTS ix_ha_contribution_speech_tsv
            ON ha_contribution USING GIN (speech_tsv)
            """,
        ),
    ]

    if not args.execute:
        print("=== DRY RUN — pass --execute to apply ===\n")
        for label, sql in steps:
            print(f"-- {label}")
            print(sql.strip())
            print()
        print("=== end dry run ===")
        return

    print(f"[fts] Connecting to Postgres...", flush=True)
    with app.app_context():
        conn = db.engine.connect()
        try:
            for label, sql in steps:
                print(f"[fts] {label}...", flush=True)
                try:
                    conn.execute(text(sql.strip()))
                    conn.commit()
                    print(f"[fts]   OK", flush=True)
                except Exception as exc:
                    conn.rollback()
                    err = str(exc).lower()
                    if "already exists" in err or "duplicate column" in err:
                        print(f"[fts]   already exists, skipping", flush=True)
                    else:
                        print(f"[fts]   ERROR: {exc}", flush=True)
                        raise
        finally:
            conn.close()

    print("[fts] Done.", flush=True)


if __name__ == "__main__":
    main()
