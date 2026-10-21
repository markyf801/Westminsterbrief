"""
Committees + APPGs migration: committee, committee_membership,
committee_inquiry, committee_inquiry_link, committee_publication, appg,
appg_officer tables and their indexes. New tables only; nothing existing is
altered.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates these tables):

    python migrations/committees_appgs_tables.py              # dry run: prints target DB + SQL
    python migrations/committees_appgs_tables.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, so re-running is a no-op and it
is safe if db.create_all() already made the tables (e.g. locally).

Indexes are created without CONCURRENTLY because the tables are new and empty
when this runs.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests/test_committees.py against SQLite.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TABLES = ("committee", "committee_membership", "committee_inquiry", "committee_inquiry_link",
          "committee_publication", "appg", "appg_officer")


def steps(dialect: str) -> list[tuple[str, str]]:
    """(label, SQL) pairs for the given SQLAlchemy dialect name."""
    serial_pk = "SERIAL PRIMARY KEY" if dialect == "postgresql" else "INTEGER PRIMARY KEY"
    return [
        ("Create committee", """
            CREATE TABLE IF NOT EXISTS committee (
                id              INTEGER PRIMARY KEY,
                name            TEXT NOT NULL,
                slug            TEXT,
                house           VARCHAR(10),
                category        VARCHAR(20),
                committee_types JSON,
                parent_id       INTEGER,
                parent_name     TEXT,
                purpose         TEXT,
                contact_email   TEXT,
                contact_phone   TEXT,
                contact_address TEXT,
                start_date      DATE,
                end_date        DATE,
                is_current      BOOLEAN NOT NULL DEFAULT TRUE,
                show_on_website BOOLEAN NOT NULL DEFAULT TRUE,
                topic_slugs     TEXT,
                raw_data        JSON,
                ingested_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Create committee_membership", f"""
            CREATE TABLE IF NOT EXISTS committee_membership (
                id            {serial_pk},
                committee_id  INTEGER NOT NULL REFERENCES committee(id) ON DELETE CASCADE,
                member_id     INTEGER,
                name          TEXT NOT NULL,
                party         TEXT,
                member_from   TEXT,
                house         VARCHAR(10),
                role          TEXT NOT NULL DEFAULT 'Member',
                roles         JSON,
                is_chair      BOOLEAN NOT NULL DEFAULT FALSE,
                is_lay_member BOOLEAN NOT NULL DEFAULT FALSE,
                is_ex_officio BOOLEAN NOT NULL DEFAULT FALSE,
                start_date    DATE,
                end_date      DATE
            )
        """),
        ("Create committee_inquiry", """
            CREATE TABLE IF NOT EXISTS committee_inquiry (
                id                 INTEGER PRIMARY KEY,
                title              TEXT NOT NULL,
                slug               TEXT,
                business_type      VARCHAR(60),
                is_inquiry         BOOLEAN NOT NULL DEFAULT FALSE,
                status             VARCHAR(10) NOT NULL DEFAULT 'Open',
                open_date          DATE,
                close_date         DATE,
                scope              TEXT,
                evidence_open      BOOLEAN NOT NULL DEFAULT FALSE,
                evidence_deadline  TIMESTAMP,
                submission_periods JSON,
                next_oral_evidence TIMESTAMP,
                latest_report      JSON,
                topic_slugs        TEXT,
                list_hash          VARCHAR(64),
                raw_data           JSON,
                detail_fetched_at  TIMESTAMP,
                ingested_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Create committee_inquiry_link", """
            CREATE TABLE IF NOT EXISTS committee_inquiry_link (
                committee_id INTEGER NOT NULL REFERENCES committee(id) ON DELETE CASCADE,
                inquiry_id   INTEGER NOT NULL REFERENCES committee_inquiry(id) ON DELETE CASCADE,
                PRIMARY KEY (committee_id, inquiry_id)
            )
        """),
        ("Create committee_publication", f"""
            CREATE TABLE IF NOT EXISTS committee_publication (
                id                {serial_pk},
                publication_id    INTEGER NOT NULL,
                committee_id      INTEGER NOT NULL REFERENCES committee(id) ON DELETE CASCADE,
                inquiry_id        INTEGER,
                inquiry_title     TEXT,
                publication_type  VARCHAR(40),
                description       TEXT NOT NULL,
                publication_date  TIMESTAMP,
                paper_number      VARCHAR(40),
                document_url      TEXT,
                response_deadline DATE,
                ingested_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_committee_publication UNIQUE (publication_id, committee_id)
            )
        """),
        ("Create appg", f"""
            CREATE TABLE IF NOT EXISTS appg (
                id                      {serial_pk},
                register_slug           VARCHAR(200) NOT NULL UNIQUE,
                title                   TEXT NOT NULL,
                short_name              TEXT NOT NULL,
                slug                    TEXT,
                category                VARCHAR(40),
                purpose                 TEXT,
                secretariat             TEXT,
                website                 TEXT,
                registered_contact_name TEXT,
                agm_date                DATE,
                benefits                JSON,
                topic_slugs             TEXT,
                register_edition        DATE,
                first_seen_edition      DATE,
                is_current              BOOLEAN NOT NULL DEFAULT TRUE,
                source_url              TEXT,
                content_hash            VARCHAR(64),
                ingested_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_refreshed          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Create appg_officer", f"""
            CREATE TABLE IF NOT EXISTS appg_officer (
                id                    {serial_pk},
                appg_id               INTEGER NOT NULL REFERENCES appg(id) ON DELETE CASCADE,
                role                  TEXT NOT NULL,
                is_chair              BOOLEAN NOT NULL DEFAULT FALSE,
                is_registered_contact BOOLEAN NOT NULL DEFAULT FALSE,
                name                  TEXT NOT NULL,
                party                 TEXT,
                member_id             INTEGER,
                match_confidence      VARCHAR(10) NOT NULL DEFAULT 'none',
                sort_order            INTEGER NOT NULL DEFAULT 0
            )
        """),
        ("Index committee.is_current",
         "CREATE INDEX IF NOT EXISTS idx_committee_current ON committee (is_current)"),
        ("Index committee.house",
         "CREATE INDEX IF NOT EXISTS idx_committee_house ON committee (house)"),
        ("Index committee_membership.committee_id",
         "CREATE INDEX IF NOT EXISTS idx_committee_membership_committee ON committee_membership (committee_id)"),
        ("Index committee_membership.member_id",
         "CREATE INDEX IF NOT EXISTS idx_committee_membership_member ON committee_membership (member_id)"),
        ("Index committee_inquiry.status",
         "CREATE INDEX IF NOT EXISTS idx_committee_inquiry_status ON committee_inquiry (status)"),
        ("Index committee_inquiry evidence_open + deadline",
         "CREATE INDEX IF NOT EXISTS idx_committee_inquiry_evidence "
         "ON committee_inquiry (evidence_open, evidence_deadline)"),
        ("Index committee_inquiry_link.inquiry_id",
         "CREATE INDEX IF NOT EXISTS idx_committee_inquiry_link_inquiry ON committee_inquiry_link (inquiry_id)"),
        ("Index committee_publication committee + date",
         "CREATE INDEX IF NOT EXISTS idx_committee_publication_committee_date "
         "ON committee_publication (committee_id, publication_date)"),
        ("Index appg.is_current",
         "CREATE INDEX IF NOT EXISTS idx_appg_current ON appg (is_current)"),
        ("Index appg_officer.appg_id",
         "CREATE INDEX IF NOT EXISTS idx_appg_officer_appg ON appg_officer (appg_id)"),
        ("Index appg_officer.member_id",
         "CREATE INDEX IF NOT EXISTS idx_appg_officer_member ON appg_officer (member_id)"),
    ]


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create committee and APPG tables")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[committees-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[committees-migration] Server host: {row[0]}, database: {row[1]}", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[committees-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            counts = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in TABLES}
        print("[committees-migration] Done. Rows: "
              + ", ".join(f"{t}={n}" for t, n in counts.items()), flush=True)


if __name__ == "__main__":
    main()
