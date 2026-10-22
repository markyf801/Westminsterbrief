"""
Tagging support migration: the ha_tag_attempt table and theme indexes.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates this table):

    python migrations/tagging_support.py              # dry run: prints target DB + SQL
    python migrations/tagging_support.py --execute    # apply

What it does (additive only — touches no existing rows):
  1. CREATE TABLE IF NOT EXISTS ha_tag_attempt — per-item count of tagging
     attempts that verified no policy area, so the crons stop re-sending an
     item after TAGGER_MAX_ATTEMPTS (default 3). Until it exists the tagger
     simply doesn't skip anything.
  2. Indexes for topic pages and related panels, built with
     CREATE INDEX CONCURRENTLY (no write lock; each runs outside a
     transaction, so the script uses autocommit):
       - ha_pq_theme (theme), ha_session_theme (theme): exact topic matches
         (theme IN (...)) on /brief pages and related panels
       - lower(theme) text_pattern_ops on both: the anchored LIKE used to find
         stored spellings for a /brief/<slug> (views._specific_themes_for_slug)

If a CONCURRENTLY build is interrupted it leaves an INVALID index. Check with
    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
and, if one of these is listed, DROP INDEX CONCURRENTLY it by name (Mark's
call — CLAUDE.md "Destructive operations") and re-run this script.

Idempotent (IF NOT EXISTS). No downgrade. On SQLite (local / tests) the
Postgres-only expression indexes are skipped and CONCURRENTLY is dropped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def steps(dialect: str) -> list[tuple[str, str]]:
    is_pg = dialect == "postgresql"
    conc = "CONCURRENTLY " if is_pg else ""
    out = [
        ("Create ha_tag_attempt", """
            CREATE TABLE IF NOT EXISTS ha_tag_attempt (
                item_kind       VARCHAR(10) NOT NULL,
                item_id         INTEGER NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TIMESTAMP,
                last_reason     VARCHAR(300),
                PRIMARY KEY (item_kind, item_id)
            )
        """),
        ("Index ha_pq_theme.theme",
         f"CREATE INDEX {conc}IF NOT EXISTS ix_ha_pq_theme_theme ON ha_pq_theme (theme)"),
        ("Index ha_session_theme.theme",
         f"CREATE INDEX {conc}IF NOT EXISTS ix_ha_session_theme_theme ON ha_session_theme (theme)"),
    ]
    if is_pg:
        out += [
            ("Index lower(ha_pq_theme.theme) for LIKE",
             "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ha_pq_theme_lower_theme "
             "ON ha_pq_theme (lower(theme) text_pattern_ops)"),
            ("Index lower(ha_session_theme.theme) for LIKE",
             "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ha_session_theme_lower_theme "
             "ON ha_session_theme (lower(theme) text_pattern_ops)"),
        ]
    return out


def upgrade(engine) -> None:
    """Apply every step with autocommit (CREATE INDEX CONCURRENTLY can't run in a transaction)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create ha_tag_attempt and theme indexes")
    parser.add_argument("--execute", action="store_true", help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[tagging-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return
        for label, sql in planned:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(sql))
            print(f"[tagging-migration] OK  {label}", flush=True)
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ha_tag_attempt")).scalar()
        print(f"[tagging-migration] Done. ha_tag_attempt rows: {n}", flush=True)


if __name__ == "__main__":
    main()
