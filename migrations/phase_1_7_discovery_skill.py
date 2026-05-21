"""
Phase 1.7 migration: discovery skill columns + StatPublicationAuditLog table.

upgrade(engine) — adds three discovery columns to ha_stat_producer, eight
  authorisation workflow columns to ha_stat_publication, and creates the new
  ha_stat_publication_audit_log table. Uses ADD COLUMN IF NOT EXISTS and
  CREATE TABLE IF NOT EXISTS so it is safe to run even after db.create_all()
  or the flask_app.py startup block has already applied the changes.

downgrade(engine) — removes Phase 1.7 additions only. Drops the audit log
  table and removes the new columns. Does NOT touch any other table, column,
  or data introduced in Phase 1.6 or earlier.

This module is used exclusively by the test suite and for developer reference.
On Railway, new tables are created by db.create_all() at startup; the new
columns on existing tables are added by the Phase 1.7 inline block in flask_app.py.
"""

from __future__ import annotations

from sqlalchemy import text


def upgrade(engine) -> None:
    """Add Phase 1.7 discovery columns and audit log table."""
    with engine.begin() as conn:
        # New columns on ha_stat_producer
        for col, defn in [
            ("discovery_status",         "TEXT NOT NULL DEFAULT 'pending'"),
            ("discovery_completed_at",   "TIMESTAMP"),
            ("discovery_failure_reason", "TEXT"),
        ]:
            try:
                conn.execute(text(
                    f"ALTER TABLE ha_stat_producer ADD COLUMN {col} {defn}"
                ))
            except Exception:
                pass  # column already exists

        # New columns on ha_stat_publication
        for col, defn in [
            ("authorisation_status", "TEXT NOT NULL DEFAULT 'candidate'"),
            ("subject_area",         "TEXT"),
            ("discovered_at",        "TIMESTAMP"),
            ("authorised_at",        "TIMESTAMP"),
            ("authorised_by",        "TEXT"),
            ("declined_at",          "TIMESTAMP"),
            ("declined_by",          "TEXT"),
            ("decline_reason",       "TEXT"),
        ]:
            try:
                conn.execute(text(
                    f"ALTER TABLE ha_stat_publication ADD COLUMN {col} {defn}"
                ))
            except Exception:
                pass  # column already exists

        # New table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ha_stat_publication_audit_log (
                id             INTEGER PRIMARY KEY,
                publication_id INTEGER NOT NULL
                    REFERENCES ha_stat_publication(id) ON DELETE CASCADE,
                changed_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                old_status     TEXT      NOT NULL,
                new_status     TEXT      NOT NULL,
                change_reason  TEXT,
                recorded_by    TEXT      NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_stat_pub_audit_pub
            ON ha_stat_publication_audit_log (publication_id)
        """))


def downgrade(engine) -> None:
    """Remove Phase 1.7 additions — discovery columns and audit log table."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ha_stat_publication_audit_log"))

        # SQLite < 3.35 does not support DROP COLUMN; wrap each in try/except
        for col in ("discovery_status", "discovery_completed_at",
                    "discovery_failure_reason"):
            try:
                conn.execute(text(
                    f"ALTER TABLE ha_stat_producer DROP COLUMN {col}"
                ))
            except Exception:
                pass

        for col in ("authorisation_status", "subject_area", "discovered_at",
                    "authorised_at", "authorised_by", "declined_at",
                    "declined_by", "decline_reason"):
            try:
                conn.execute(text(
                    f"ALTER TABLE ha_stat_publication DROP COLUMN {col}"
                ))
            except Exception:
                pass
