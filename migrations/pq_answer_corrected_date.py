"""
Written Questions: add ha_pq.answer_corrected_date (Parliament's
dateAnswerCorrected), so a corrected answer can be re-fetched and shown as
"Corrected on ...".

Run once on Railway as a one-shot service BEFORE deploying the code that uses
the column (the web service and crons run with SKIP_MIGRATIONS=1, and the
HaPQ model selects every column, so PQ pages fail until the column exists):

    python migrations/pq_answer_corrected_date.py              # dry run: prints target DB + SQL
    python migrations/pq_answer_corrected_date.py --execute    # apply

Idempotent: ADD COLUMN IF NOT EXISTS, so re-running is a no-op. A nullable
column with no default is a catalogue-only change in Postgres (no table
rewrite). lock_timeout stops it queueing behind a long query on ha_pq and
blocking the site; if it times out, run it again later.

Only adds. There is no downgrade: dropping columns is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests against SQLite.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LOCK_TIMEOUT = "5s"


def steps(dialect: str) -> list[tuple[str, str]]:
    """(label, SQL) pairs for the given SQLAlchemy dialect name."""
    if dialect == "postgresql":
        return [
            ("Set lock_timeout", f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"),
            ("Add ha_pq.answer_corrected_date",
             "ALTER TABLE ha_pq ADD COLUMN IF NOT EXISTS answer_corrected_date DATE"),
        ]
    # SQLite has no ADD COLUMN IF NOT EXISTS; upgrade() checks first.
    return [("Add ha_pq.answer_corrected_date",
             "ALTER TABLE ha_pq ADD COLUMN answer_corrected_date DATE")]


def _has_column(conn) -> bool:
    return "answer_corrected_date" in {c["name"] for c in inspect(conn).get_columns("ha_pq")}


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        if engine.dialect.name != "postgresql" and _has_column(conn):
            return
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Add ha_pq.answer_corrected_date")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[pq-corrected-migration] Target: {engine.url.render_as_string(hide_password=True)}",
              flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[pq-corrected-migration] Server host: {row[0]}, database: {row[1]}", flush=True)

        with engine.connect() as conn:
            present = _has_column(conn)
        print(f"[pq-corrected-migration] Column already present: {present}", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql}\n", flush=True)
            return

        upgrade(engine)
        with engine.connect() as conn:
            ok = _has_column(conn)
            n_rows = conn.execute(text("SELECT COUNT(*) FROM ha_pq")).scalar()
        print(f"[pq-corrected-migration] Done. Column present: {ok}; ha_pq rows: {n_rows} "
              f"(untouched; the cron fills the date for corrected answers)", flush=True)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
