"""
Written ministerial statements migration: ha_written_statement table, indexes,
and the Postgres full-text search column.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates this table):

    python migrations/written_statement_tables.py              # dry run: prints target DB + SQL
    python migrations/written_statement_tables.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS and ADD COLUMN IF NOT EXISTS,
so re-running is a no-op. Safe if db.create_all() already made the table
(e.g. locally): the FTS column is added to the existing table.

Indexes are created without CONCURRENTLY because the table is new and empty
when this runs; on a re-run IF NOT EXISTS skips them without taking a lock.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests/test_written_statements.py against
SQLite (Postgres-only steps are skipped there).
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
    is_pg = dialect == "postgresql"
    out = [
        ("Create ha_written_statement", """
            CREATE TABLE IF NOT EXISTS ha_written_statement (
                id                  INTEGER PRIMARY KEY,
                uin                 VARCHAR(20) NOT NULL,
                date_made           DATE NOT NULL,
                house               VARCHAR(10) NOT NULL,
                answering_body_id   INTEGER,
                answering_body_name TEXT,
                member_id           INTEGER,
                member_name         TEXT,
                member_party        TEXT,
                member_role         TEXT,
                title               TEXT NOT NULL,
                text_html           TEXT,
                text_plain          TEXT,
                notice_number       INTEGER,
                attachments         JSON,
                linked_statements   JSON,
                policy_areas        JSON,
                raw_data            JSON,
                ingested_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Index ha_written_statement.date_made",
         "CREATE INDEX IF NOT EXISTS idx_ha_ws_date_made ON ha_written_statement (date_made)"),
        ("Index ha_written_statement (date_made, uin)",
         "CREATE INDEX IF NOT EXISTS idx_ha_ws_date_uin ON ha_written_statement (date_made, uin)"),
        ("Index ha_written_statement.answering_body_id",
         "CREATE INDEX IF NOT EXISTS idx_ha_ws_answering_body ON ha_written_statement (answering_body_id)"),
        ("Index ha_written_statement.house",
         "CREATE INDEX IF NOT EXISTS idx_ha_ws_house ON ha_written_statement (house)"),
        ("Index ha_written_statement.member_id",
         "CREATE INDEX IF NOT EXISTS idx_ha_ws_member ON ha_written_statement (member_id)"),
    ]
    if is_pg:
        # Same pattern as ha_edm.search_tsv: generated column keeps new and
        # updated rows indexed automatically.
        out += [
            ("Add ha_written_statement.search_tsv (generated tsvector)", """
                ALTER TABLE ha_written_statement
                ADD COLUMN IF NOT EXISTS search_tsv tsvector
                    GENERATED ALWAYS AS (
                        to_tsvector('english',
                            coalesce(title, '') || ' ' || coalesce(text_plain, ''))
                    ) STORED
            """),
            ("GIN index on ha_written_statement.search_tsv",
             "CREATE INDEX IF NOT EXISTS ix_ha_ws_search_tsv ON ha_written_statement USING GIN (search_tsv)"),
        ]
    return out


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the written ministerial statements table")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[wms-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[wms-migration] Server host: {row[0]}, database: {row[1]}", flush=True)
        else:
            print("[wms-migration] Not Postgres: the full-text search column is skipped "
                  "(search uses LIKE).", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[wms-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ha_written_statement")).scalar()
        print(f"[wms-migration] Done. ha_written_statement rows: {n}", flush=True)


if __name__ == "__main__":
    main()
