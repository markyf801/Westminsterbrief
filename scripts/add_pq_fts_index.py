"""
Hansard Archive — PQ full-text search index setup.

Adds a tsvector column + GIN index to ha_pq covering heading, question_text,
and answer_text. Safe to re-run: uses IF NOT EXISTS / catches 'already exists'.

Run once against Railway Postgres AFTER the 12-month backfill has completed.
GENERATED ALWAYS AS STORED keeps new rows indexed automatically after that.

Usage:
  python scripts/add_pq_fts_index.py          # dry run (prints SQL, no execute)
  python scripts/add_pq_fts_index.py --execute

Environment:
  DATABASE_URL  — must be set to a Postgres connection string.
  If not set, script exits with a clear error (SQLite has no tsvector support).
"""

import argparse
import os
import sys

sys.path.insert(0, ".")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add PQ FTS tsvector column + GIN index")
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
        (
            "Add question_tsv column to ha_pq",
            """
            ALTER TABLE ha_pq
            ADD COLUMN IF NOT EXISTS question_tsv tsvector
                GENERATED ALWAYS AS (
                    to_tsvector('english',
                        coalesce(heading, '') || ' ' ||
                        coalesce(question_text, '') || ' ' ||
                        coalesce(answer_text, ''))
                ) STORED
            """,
        ),
        (
            "GIN index on ha_pq.question_tsv",
            """
            CREATE INDEX IF NOT EXISTS ix_ha_pq_question_tsv
            ON ha_pq USING GIN (question_tsv)
            """,
        ),
    ]

    if not args.execute:
        print("=== DRY RUN — pass --execute to apply ===\n")
        from flask_app import app as _app
        from extensions import db as _db
        with _app.app_context():
            safe_url = _db.engine.url.render_as_string(hide_password=True)
            print(f"[pq-fts] Would target database: {safe_url}")
            try:
                row = _db.session.execute(
                    text("SELECT inet_server_addr()::text, current_database()")
                ).fetchone()
                print(f"[pq-fts] Server host: {row[0]}, database: {row[1]}\n")
            except Exception as e:
                print(f"[pq-fts] Could not query server host: {e}\n")
        for label, sql in steps:
            print(f"-- {label}")
            print(sql.strip())
            print()
        print("=== end dry run ===")
        return

    print("[pq-fts] Connecting to Postgres...", flush=True)
    with app.app_context():
        safe_url = db.engine.url.render_as_string(hide_password=True)
        print(f"[pq-fts] Engine URL (password hidden): {safe_url}", flush=True)
        try:
            row = db.session.execute(
                text("SELECT inet_server_addr()::text, current_database()")
            ).fetchone()
            print(f"[pq-fts] Server host: {row[0]}, database: {row[1]}", flush=True)
        except Exception as e:
            print(f"[pq-fts] Could not query server host: {e}", flush=True)

        conn = db.engine.connect()
        try:
            for label, sql in steps:
                print(f"[pq-fts] {label}...", flush=True)
                try:
                    conn.execute(text(sql.strip()))
                    conn.commit()
                    print("[pq-fts]   OK", flush=True)
                except Exception as exc:
                    conn.rollback()
                    err = str(exc).lower()
                    if "already exists" in err or "duplicate column" in err:
                        print("[pq-fts]   already exists, skipping", flush=True)
                    else:
                        print(f"[pq-fts]   ERROR: {exc}", flush=True)
                        raise
        finally:
            conn.close()

    print("[pq-fts] Done.", flush=True)


if __name__ == "__main__":
    main()
