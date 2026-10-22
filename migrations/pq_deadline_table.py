"""
Written Question deadlines: the ha_pq_deadline table (date due for answer and
the named-day flag, keyed by Parliament's question id = ha_pq.api_id).

Run once on Railway as a one-shot service, BEFORE pushing the code that uses
it (the web service and crons run with SKIP_MIGRATIONS=1, so db.create_all()
never creates it there):

    python migrations/pq_deadline_table.py              # dry run: prints target DB + SQL
    python migrations/pq_deadline_table.py --execute    # apply

Idempotent: CREATE TABLE IF NOT EXISTS, so re-running is a no-op. Safe if
db.create_all() already made the table (e.g. locally). Does not touch ha_pq.

The table starts empty. The PQ crons fill it as they re-read questions (the
7-day tabled window covers everything Today's PQs shows), so after the first
PQ cron run the "Due" dates and NAMED DAY badges are back.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests/test_tracker_db.py against SQLite.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def steps(dialect: str) -> list[tuple[str, str]]:
    """(label, SQL) pairs. The same SQL works on Postgres and SQLite."""
    return [
        ("Create ha_pq_deadline", """
            CREATE TABLE IF NOT EXISTS ha_pq_deadline (
                api_id          INTEGER PRIMARY KEY,
                date_for_answer DATE,
                is_named_day    BOOLEAN,
                updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
    ]


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the ha_pq_deadline table")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[pq-deadline-migration] Target: {engine.url.render_as_string(hide_password=True)}",
              flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[pq-deadline-migration] Server host: {row[0]}, database: {row[1]}", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[pq-deadline-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ha_pq_deadline")).scalar()
        print(f"[pq-deadline-migration] Done. ha_pq_deadline rows: {n}", flush=True)


if __name__ == "__main__":
    main()
