"""
Phase 1.6 migration: source registry tables + HeadlineStat FK columns.

upgrade(engine) — creates the three new tables and adds the two FK columns to
  headline_stat. Uses CREATE TABLE IF NOT EXISTS and ADD COLUMN IF NOT EXISTS
  so it is safe to run even if db.create_all() has already created the tables.

downgrade(engine) — removes Phase 1.6 additions only. Drops the three new
  tables and removes the two FK columns from headline_stat. Does NOT touch
  any other table or column.

This module is used exclusively by the test suite and for developer reference.
On Railway, the new tables are created by db.create_all() at startup; the FK
columns on headline_stat are added by the Phase 1.6 inline block in flask_app.py.
"""

from __future__ import annotations

from sqlalchemy import text


def upgrade(engine) -> None:
    """Create Phase 1.6 tables and add FK columns to headline_stat."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ha_stat_producer (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                short_name TEXT,
                producer_type TEXT NOT NULL
                    CHECK (producer_type IN (
                        'central_department','executive_agency','ndpb',
                        'regulator','devolved_administration','devolved_body',
                        'gss_producer','other_public_body')),
                parent_id INTEGER REFERENCES ha_stat_producer(id) ON DELETE SET NULL,
                web_root_url TEXT NOT NULL,
                contact_email TEXT,
                description TEXT,
                authorisation_status TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (authorisation_status IN (
                        'candidate','under_review','authorised','declined','paused')),
                authorisation_reason TEXT,
                licence TEXT NOT NULL
                    CHECK (licence IN (
                        'OGL_v3','Crown_Copyright_other','HESA_Open',
                        'HESA_Commercial','UCAS','Custom_Open',
                        'Custom_Restrictive','Unverified')),
                licence_evidence_raw_url TEXT,
                licence_evidence_wayback_url TEXT,
                reviewed_by TEXT,
                reviewed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_stat_producer_slug UNIQUE (slug)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ha_stat_publication (
                id INTEGER PRIMARY KEY,
                producer_id INTEGER NOT NULL
                    REFERENCES ha_stat_producer(id) ON DELETE RESTRICT,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                description TEXT,
                update_cadence TEXT
                    CHECK (update_cadence IS NULL OR update_cadence IN (
                        'daily','weekly','monthly','quarterly','annual',
                        'biennial','ad_hoc','one_off')),
                first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_stat_publication_producer_slug UNIQUE (producer_id, slug)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ha_stat_licence_audit_log (
                id INTEGER PRIMARY KEY,
                producer_id INTEGER NOT NULL
                    REFERENCES ha_stat_producer(id) ON DELETE CASCADE,
                changed_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                old_licence              TEXT,
                new_licence              TEXT NOT NULL,
                old_evidence_raw_url     TEXT,
                new_evidence_raw_url     TEXT,
                old_evidence_wayback_url TEXT,
                new_evidence_wayback_url TEXT,
                change_reason            TEXT,
                recorded_by              TEXT
            )
        """))
        for col, defn in [("producer_id", "INTEGER"), ("publication_id", "INTEGER")]:
            try:
                conn.execute(text(f"ALTER TABLE headline_stat ADD COLUMN {col} {defn}"))
            except Exception:
                pass  # column already exists


def downgrade(engine) -> None:
    """Remove Phase 1.6 tables and headline_stat FK columns."""
    with engine.begin() as conn:
        for col in ("producer_id", "publication_id"):
            try:
                conn.execute(text(f"ALTER TABLE headline_stat DROP COLUMN {col}"))
            except Exception:
                pass  # SQLite < 3.35 does not support DROP COLUMN
        conn.execute(text("DROP TABLE IF EXISTS ha_stat_licence_audit_log"))
        conn.execute(text("DROP TABLE IF EXISTS ha_stat_publication"))
        conn.execute(text("DROP TABLE IF EXISTS ha_stat_producer"))
