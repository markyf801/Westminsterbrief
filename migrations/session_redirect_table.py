"""
Debate URL redirects: the ha_session_redirect table (old slug -> the session
that replaced it), written by scripts/delete_duplicate_debates.py so a deleted
duplicate's /archive/debate/<date>/<slug> URL 301s to the copy we kept.

Run once on Railway as a one-shot service, BEFORE running
scripts/delete_duplicate_debates.py --execute (which refuses to run without the
table). The web service and crons run with SKIP_MIGRATIONS=1, so
db.create_all() never creates it there; until it exists the debate page simply
404s unknown slugs, as before.

    python migrations/session_redirect_table.py              # dry run: prints target DB + SQL
    python migrations/session_redirect_table.py --execute    # apply

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, so re-running is a no-op. Safe
if db.create_all() already made the table (e.g. locally). Touches no other table.

Only creates. There is no downgrade: dropping tables is Mark's call (CLAUDE.md
"Destructive operations").

upgrade(engine) is also used by tests/test_duplicate_debates.py against SQLite.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def steps(dialect: str) -> list[tuple[str, str]]:
    """(label, SQL) pairs. The same SQL works on Postgres and SQLite."""
    return [
        ("Create ha_session_redirect", """
            CREATE TABLE IF NOT EXISTS ha_session_redirect (
                old_slug       VARCHAR(200) PRIMARY KEY,
                new_session_id INTEGER NOT NULL,
                created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """),
        ("Index ha_session_redirect.new_session_id", """
            CREATE INDEX IF NOT EXISTS ix_ha_session_redirect_new_session_id
                ON ha_session_redirect (new_session_id)
        """),
    ]


def upgrade(engine) -> None:
    """Apply every step in one transaction."""
    with engine.begin() as conn:
        for _label, sql in steps(engine.dialect.name):
            conn.execute(text(sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the ha_session_redirect table")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run the SQL (default: dry run)")
    args = parser.parse_args()

    os.environ.setdefault("SKIP_MIGRATIONS", "1")
    from flask_app import app
    from extensions import db

    with app.app_context():
        engine = db.engine
        print(f"[session-redirect-migration] Target: "
              f"{engine.url.render_as_string(hide_password=True)}", flush=True)
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(text("SELECT inet_server_addr()::text, current_database()")).fetchone()
            print(f"[session-redirect-migration] Server host: {row[0]}, database: {row[1]}", flush=True)

        planned = steps(engine.dialect.name)
        if not args.execute:
            print("=== DRY RUN — pass --execute to apply ===\n", flush=True)
            for label, sql in planned:
                print(f"-- {label}\n{sql.strip()}\n", flush=True)
            return

        upgrade(engine)
        for label, _ in planned:
            print(f"[session-redirect-migration] OK  {label}", flush=True)

        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ha_session_redirect")).scalar()
        print(f"[session-redirect-migration] Done. ha_session_redirect rows: {n}", flush=True)


if __name__ == "__main__":
    main()
