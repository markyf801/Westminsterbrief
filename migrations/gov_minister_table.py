"""
Ministers migration: the gov_minister table (current ministers by department),
filled daily by scripts/refresh_ministers.py.

Run once on Railway as a one-shot service (the web service and crons run with
SKIP_MIGRATIONS=1, so db.create_all() never creates this table):

    python migrations/gov_minister_table.py              # dry run: prints target DB + SQL
    python migrations/gov_minister_table.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, so re-running is a no-op and it
is safe if db.create_all() already made the table (e.g. locally).

Until the table has rows the site keeps its old behaviour (the file-cached
GOV.UK fetch), so deploying the code before running this is safe.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests/test_ministers.py against SQLite.
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
        ("Create gov_minister", f"""
            CREATE TABLE IF NOT EXISTS gov_minister (
                id               {serial_pk},
                department       VARCHAR(200) NOT NULL,
                govuk_department VARCHAR(200),
                role_title       VARCHAR(500) NOT NULL,
                govuk_role_title VARCHAR(500),
                member_name      VARCHAR(300) NOT NULL,
                govuk_name       VARCHAR(300),
                name_norm        VARCHAR(300) NOT NULL,
                mnis_id          INTEGER,
                house            VARCHAR(20),
                rank             INTEGER NOT NULL DEFAULT 0,
                is_cabinet       BOOLEAN NOT NULL DEFAULT FALSE,
                start_date       DATE,
                source           VARCHAR(20) NOT NULL,
                source_url       VARCHAR(500),
                fetched_at       TIMESTAMP NOT NULL
            )
        """),
        ("Index gov_minister.department",
         "CREATE INDEX IF NOT EXISTS ix_gov_minister_department ON gov_minister (department)"),
        ("Index gov_minister.name_norm",
         "CREATE INDEX IF NOT EXISTS ix_gov_minister_name_norm ON gov_minister (name_norm)"),
        ("Index gov_minister.mnis_id",
         "CREATE INDEX IF NOT EXISTS ix_gov_minister_mnis_id ON gov_minister (mnis_id)"),
    ]


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the gov_minister table")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[minister-migration] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[minister-migration] Server host: {row[0]}, database: {row[1]}", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[minister-migration] OK  {label}", flush=True)
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM gov_minister")).scalar()
        print(f"[minister-migration] Done. gov_minister rows: {n}", flush=True)


if __name__ == "__main__":
    main()
