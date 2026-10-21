"""
GOV.UK consultations migration: consultation + consultation_topic tables,
indexes, and the Postgres full-text search column.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates these tables):

    python migrations/consultation_tables.py              # dry run: prints target DB + SQL
    python migrations/consultation_tables.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS and ADD COLUMN IF NOT EXISTS,
so re-running is a no-op. Safe if db.create_all() already made the tables
(e.g. locally): the FTS column is added to the existing table.

Only creates new tables; touches no existing table. There is no downgrade:
dropping tables is Mark's call (CLAUDE.md "Destructive operations").

upgrade(engine) is also used by tests/test_consultations.py against SQLite
(Postgres-only steps are skipped there).
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
        ("Create consultation", f"""
            CREATE TABLE IF NOT EXISTS consultation (
                id                 {serial_pk},
                content_id         VARCHAR(64) NOT NULL UNIQUE,
                base_path          TEXT NOT NULL UNIQUE,
                slug               VARCHAR(255) NOT NULL UNIQUE,
                title              TEXT NOT NULL,
                summary            TEXT,
                body_text          TEXT,
                status             VARCHAR(20) NOT NULL,
                opening_date       TIMESTAMP,
                closing_date       TIMESTAMP,
                lead_org_slug      VARCHAR(200),
                lead_org_title     TEXT,
                organisations      JSON,
                policy_areas       JSON,
                taxons             JSON,
                response_url       TEXT,
                response_email     TEXT,
                response_postal    TEXT,
                response_form_url  TEXT,
                documents          JSON,
                outcome_url        TEXT,
                outcome_summary    TEXT,
                outcome_documents  JSON,
                applies_to         JSON,
                is_withdrawn       BOOLEAN NOT NULL DEFAULT FALSE,
                govuk_updated_at   TIMESTAMP,
                first_published_at TIMESTAMP,
                raw_data           JSON,
                ingested_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Create consultation_topic", f"""
            CREATE TABLE IF NOT EXISTS consultation_topic (
                id              {serial_pk},
                consultation_id INTEGER NOT NULL REFERENCES consultation(id) ON DELETE CASCADE,
                policy_area     VARCHAR(100) NOT NULL,
                source          VARCHAR(20) NOT NULL,
                score           INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_consultation_topic UNIQUE (consultation_id, policy_area)
            )
        """),
        ("Index consultation.status",
         "CREATE INDEX IF NOT EXISTS idx_consultation_status ON consultation (status)"),
        ("Index consultation.closing_date",
         "CREATE INDEX IF NOT EXISTS idx_consultation_closing ON consultation (closing_date)"),
        ("Index consultation.lead_org_slug",
         "CREATE INDEX IF NOT EXISTS idx_consultation_lead_org ON consultation (lead_org_slug)"),
        ("Index consultation_topic.consultation_id",
         "CREATE INDEX IF NOT EXISTS ix_consultation_topic_consultation_id "
         "ON consultation_topic (consultation_id)"),
        ("Index consultation_topic.policy_area",
         "CREATE INDEX IF NOT EXISTS idx_consultation_topic_area ON consultation_topic (policy_area)"),
    ]
    if is_pg:
        # Same pattern as ha_edm.search_tsv (migrations/edm_tables.py).
        out += [
            ("Add consultation.search_tsv (generated tsvector)", """
                ALTER TABLE consultation
                ADD COLUMN IF NOT EXISTS search_tsv tsvector
                    GENERATED ALWAYS AS (
                        to_tsvector('english',
                            coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' ||
                            coalesce(body_text, ''))
                    ) STORED
            """),
            ("GIN index on consultation.search_tsv",
             "CREATE INDEX IF NOT EXISTS ix_consultation_search_tsv "
             "ON consultation USING GIN (search_tsv)"),
        ]
    return out


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create GOV.UK consultation tables")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[consultation-migration] Target: {engine.url.render_as_string(hide_password=True)}",
              flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[consultation-migration] Server host: {row[0]}, database: {row[1]}", flush=True)
        else:
            print("[consultation-migration] Not Postgres: the full-text search column is skipped "
                  "(search uses LIKE).", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[consultation-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            n_c = conn.execute(text("SELECT COUNT(*) FROM consultation")).scalar()
            n_t = conn.execute(text("SELECT COUNT(*) FROM consultation_topic")).scalar()
        print(f"[consultation-migration] Done. consultation rows: {n_c}, "
              f"consultation_topic rows: {n_t}", flush=True)


if __name__ == "__main__":
    main()
