"""
Divisions migration: ha_division + ha_division_vote tables and indexes.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates these tables):

    python migrations/division_tables.py              # dry run: prints target DB + SQL
    python migrations/division_tables.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, so re-running is a no-op and
it is safe if db.create_all() already made the tables (e.g. locally).

Indexes are created without CONCURRENTLY because the tables are new and empty
when this runs; on a re-run IF NOT EXISTS skips them without taking a lock.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

Size (Oct 2026 estimate): about 1,100 divisions and 420,000 vote rows for the
Parliament elected on 4 July 2024, roughly 75-100 MB including indexes, growing
by about 40-50 MB a year.

upgrade(engine) is also used by tests/test_divisions.py against SQLite.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def steps(dialect: str) -> list[tuple[str, str]]:
    """(label, SQL) pairs for the given SQLAlchemy dialect name."""
    serial_pk = "SERIAL PRIMARY KEY" if dialect == "postgresql" else "INTEGER PRIMARY KEY"
    return [
        ("Create ha_division", f"""
            CREATE TABLE IF NOT EXISTS ha_division (
                id                       {serial_pk},
                house                    VARCHAR(10) NOT NULL,
                parliament_division_id   INTEGER NOT NULL,
                number                   INTEGER,
                date                     DATE NOT NULL,
                title                    TEXT NOT NULL,
                motion_text              TEXT,
                outcome_text             TEXT,
                ayes_count               INTEGER NOT NULL DEFAULT 0,
                noes_count               INTEGER NOT NULL DEFAULT 0,
                is_deferred              BOOLEAN NOT NULL DEFAULT FALSE,
                official_url             TEXT NOT NULL,
                hansard_debate_ext_id    VARCHAR(64),
                hansard_division_ext_id  VARCHAR(64),
                hansard_debate_title     TEXT,
                ha_session_id            INTEGER,
                source_updated           TIMESTAMP,
                votes_hash               VARCHAR(40),
                vote_rows                INTEGER NOT NULL DEFAULT 0,
                raw_data                 JSON,
                ingested_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_ha_division_house_pid UNIQUE (house, parliament_division_id)
            )
        """),
        ("Create ha_division_vote", """
            CREATE TABLE IF NOT EXISTS ha_division_vote (
                division_id  INTEGER NOT NULL REFERENCES ha_division(id) ON DELETE CASCADE,
                member_id    INTEGER NOT NULL,
                vote         VARCHAR(4) NOT NULL,
                member_name  TEXT NOT NULL,
                list_as      TEXT,
                party        TEXT,
                member_from  TEXT,
                is_teller    BOOLEAN NOT NULL DEFAULT FALSE,
                proxy_name   TEXT,
                PRIMARY KEY (division_id, member_id, vote)
            )
        """),
        ("Index ha_division.date",
         "CREATE INDEX IF NOT EXISTS idx_ha_division_date ON ha_division (date)"),
        ("Index ha_division (house, date)",
         "CREATE INDEX IF NOT EXISTS idx_ha_division_house_date ON ha_division (house, date)"),
        ("Index ha_division.ha_session_id",
         "CREATE INDEX IF NOT EXISTS idx_ha_division_session ON ha_division (ha_session_id)"),
        # Votes by division: the primary key (division_id, member_id, vote).
        ("Index ha_division_vote (member_id, division_id) — votes by member",
         "CREATE INDEX IF NOT EXISTS idx_ha_division_vote_member "
         "ON ha_division_vote (member_id, division_id)"),
    ]


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create division (recorded vote) tables")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[division-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
                size = conn.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar()
            print(f"[division-migration] Server host: {row[0]}, database: {row[1]}, size now: {size}", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[division-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            n_div = conn.execute(text("SELECT COUNT(*) FROM ha_division")).scalar()
            n_vote = conn.execute(text("SELECT COUNT(*) FROM ha_division_vote")).scalar()
        print(f"[division-migration] Done. ha_division rows: {n_div}, ha_division_vote rows: {n_vote}",
              flush=True)


if __name__ == "__main__":
    main()
