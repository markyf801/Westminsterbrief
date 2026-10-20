"""
Early Day Motions migration: ha_edm + ha_edm_sponsor tables, indexes, and the
Postgres full-text search column.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates these tables):

    python migrations/edm_tables.py              # dry run: prints target DB + SQL
    python migrations/edm_tables.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS and ADD COLUMN IF NOT EXISTS,
so re-running is a no-op. Safe if db.create_all() already made the tables
(e.g. locally): the FTS column is added to the existing table.

Indexes are created without CONCURRENTLY because the tables are new and empty
when this runs; on a re-run IF NOT EXISTS skips them without taking a lock.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests/test_edms.py against SQLite (Postgres-only
steps are skipped there).
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
    serial_pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY"
    out = [
        ("Create ha_edm", """
            CREATE TABLE IF NOT EXISTS ha_edm (
                id                        INTEGER PRIMARY KEY,
                uin                       VARCHAR(20) NOT NULL,
                title                     TEXT NOT NULL,
                motion_text               TEXT,
                status                    VARCHAR(20) NOT NULL DEFAULT 'Published',
                status_date               TIMESTAMP,
                date_tabled               DATE,
                session                   VARCHAR(20),
                primary_sponsor_member_id INTEGER,
                primary_sponsor_name      TEXT,
                primary_sponsor_party     TEXT,
                signature_count           INTEGER NOT NULL DEFAULT 0,
                amendment_to_id           INTEGER,
                amendments                JSON,
                is_prayer                 BOOLEAN NOT NULL DEFAULT FALSE,
                related_topics            JSON,
                slug                      TEXT,
                raw_data                  JSON,
                ingested_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Create ha_edm_sponsor", f"""
            CREATE TABLE IF NOT EXISTS ha_edm_sponsor (
                id               {serial_pk},
                edm_id           INTEGER NOT NULL REFERENCES ha_edm(id) ON DELETE CASCADE,
                member_id        INTEGER,
                member_name      TEXT NOT NULL,
                party            TEXT,
                constituency     TEXT,
                is_primary       BOOLEAN NOT NULL DEFAULT FALSE,
                is_sponsor       BOOLEAN NOT NULL DEFAULT FALSE,
                sponsoring_order INTEGER,
                date_signed      TIMESTAMP,
                is_withdrawn     BOOLEAN NOT NULL DEFAULT FALSE,
                withdrawn_date   TIMESTAMP
            )
        """),
        ("Index ha_edm.date_tabled",
         "CREATE INDEX IF NOT EXISTS idx_ha_edm_date_tabled ON ha_edm (date_tabled)"),
        ("Index ha_edm.signature_count",
         "CREATE INDEX IF NOT EXISTS idx_ha_edm_signature_count ON ha_edm (signature_count)"),
        ("Index ha_edm.status",
         "CREATE INDEX IF NOT EXISTS idx_ha_edm_status ON ha_edm (status)"),
        ("Index ha_edm.session",
         "CREATE INDEX IF NOT EXISTS idx_ha_edm_session ON ha_edm (session)"),
        ("Index ha_edm_sponsor.edm_id",
         "CREATE INDEX IF NOT EXISTS idx_ha_edm_sponsor_edm ON ha_edm_sponsor (edm_id)"),
        ("Index ha_edm_sponsor.member_id",
         "CREATE INDEX IF NOT EXISTS idx_ha_edm_sponsor_member ON ha_edm_sponsor (member_id)"),
    ]
    if is_pg:
        # Same pattern as ha_pq.question_tsv (scripts/add_pq_fts_index.py):
        # generated column keeps new and updated rows indexed automatically.
        out += [
            ("Add ha_edm.search_tsv (generated tsvector)", """
                ALTER TABLE ha_edm
                ADD COLUMN IF NOT EXISTS search_tsv tsvector
                    GENERATED ALWAYS AS (
                        to_tsvector('english',
                            coalesce(title, '') || ' ' || coalesce(motion_text, ''))
                    ) STORED
            """),
            ("GIN index on ha_edm.search_tsv",
             "CREATE INDEX IF NOT EXISTS ix_ha_edm_search_tsv ON ha_edm USING GIN (search_tsv)"),
        ]
    return out


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Early Day Motion tables")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[edm-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[edm-migration] Server host: {row[0]}, database: {row[1]}", flush=True)
        else:
            print("[edm-migration] Not Postgres: the full-text search column is skipped "
                  "(search uses LIKE).", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[edm-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            n_edm = conn.execute(text("SELECT COUNT(*) FROM ha_edm")).scalar()
            n_sp = conn.execute(text("SELECT COUNT(*) FROM ha_edm_sponsor")).scalar()
        print(f"[edm-migration] Done. ha_edm rows: {n_edm}, ha_edm_sponsor rows: {n_sp}", flush=True)


if __name__ == "__main__":
    main()
