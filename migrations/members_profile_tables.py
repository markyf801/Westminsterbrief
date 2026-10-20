"""
Members-from-DB migration: member_profile, member_biography, member_name, member_name_audit.

upgrade(engine) — creates the four new tables (and their indexes) if they do
  not already exist, using the SQLAlchemy model definitions in cache_models.py
  so the DDL is correct for both SQLite and Postgres. Creates only; never
  alters or drops an existing table, and touches no other table. Safe to run
  repeatedly. Returns the names of the tables it created.

Why a script: the web service runs with SKIP_MIGRATIONS=1, so db.create_all()
does not run on production. Locally and in tests, db.create_all() creates the
same tables from the models.

Railway one-off (Dockerfile.cron, SKIP_MIGRATIONS=1, Restart: Never):
    python migrations/members_profile_tables.py            # report only
    python migrations/members_profile_tables.py --execute  # create missing tables

Apply BEFORE deploying the code that reads these tables. (The routes also
tolerate the tables being absent and fall back to cached_member.)
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect  # noqa: E402

from cache_models import MemberBiography, MemberName, MemberNameAudit, MemberProfile  # noqa: E402

TABLES = (MemberProfile.__table__, MemberBiography.__table__,
          MemberName.__table__, MemberNameAudit.__table__)


def missing_tables(engine) -> list[str]:
    existing = set(inspect(engine).get_table_names())
    return [t.name for t in TABLES if t.name not in existing]


def upgrade(engine) -> list[str]:
    """Create the members tables that are missing. Idempotent."""
    to_create = missing_tables(engine)
    for table in TABLES:
        table.create(bind=engine, checkfirst=True)
    return to_create


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--execute", action="store_true", help="create the missing tables")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", flush=True)
        return 1
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(url, pool_pre_ping=True)

    todo = missing_tables(engine)
    print(f"[migration] missing tables: {todo or 'none'}", flush=True)
    if not args.execute:
        print("[migration] report only — re-run with --execute to create them.", flush=True)
        return 0
    created = upgrade(engine)
    still_missing = missing_tables(engine)
    print(f"[migration] created: {created or 'none'}; still missing: {still_missing or 'none'}", flush=True)
    return 1 if still_missing else 0


if __name__ == "__main__":
    sys.exit(main())
