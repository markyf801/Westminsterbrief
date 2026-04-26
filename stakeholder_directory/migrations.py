"""
Migration script for the stakeholder directory tables.

Run standalone (create tables):
    python stakeholder_directory/migrations.py

Sync CHECK constraints after editing a YAML vocab file:
    python -m stakeholder_directory.migrations --sync-vocab

Call from app at startup:
    from stakeholder_directory.migrations import run_migrations
    run_migrations(app)

Uses db.create_all() for table creation (idempotent).
Uses migrate_check_constraints() for constraint updates (explicit only —
do not call on every startup; only when YAML files have changed).
"""
import os
import re
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Table creation (idempotent)
# ---------------------------------------------------------------------------

def _add_column_if_missing(engine, table_name: str, column_name: str, column_type: str) -> None:
    """Add a column to an existing table if it is not already present.

    Safe on both SQLite and PostgreSQL. No-ops if the table or column already exists.
    """
    from sqlalchemy import text, inspect as sa_inspect
    try:
        insp = sa_inspect(engine)
        if not insp.has_table(table_name):
            return  # table will be created with the column by db.create_all()
        existing = [c['name'] for c in insp.get_columns(table_name)]
        if column_name not in existing:
            with engine.begin() as conn:
                conn.execute(text(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}'
                ))
            print(f'  Added column {column_name} to {table_name}')
    except Exception as exc:
        print(f'  WARNING: Could not add column {column_name} to {table_name}: {exc}')


def run_migrations(app):
    """Create all stakeholder_directory tables. Safe to call on every startup."""
    from extensions import db
    import stakeholder_directory.models  # noqa: F401 — registers Organisation, Alias, Engagement, Flag, IngestionRun
    import stakeholder_directory.ingesters.staging  # noqa: F401 — registers staging models

    with app.app_context():
        db.create_all()
        # Committee evidence columns added to Engagement in Prompt 6 (May 2026).
        # These ALTER TABLE calls are no-ops if the columns already exist (fresh DB)
        # or if the table was created after the model update.
        _add_column_if_missing(db.engine, 'sd_engagement', 'committee_id', 'INTEGER')
        _add_column_if_missing(db.engine, 'sd_engagement', 'committee_name', 'VARCHAR(200)')
        print('[stakeholder_directory] tables: OK (created or already existed)')


# ---------------------------------------------------------------------------
# CHECK constraint sync (explicit only, run after editing YAML vocab files)
# ---------------------------------------------------------------------------

def _extract_quoted_values(sql_text: str) -> set[str]:
    """Return the set of single-quoted string literals in a SQL fragment."""
    return set(re.findall(r"'([^']+)'", sql_text))


def _current_constraint_values_sqlite(conn, table_name: str, constraint_name: str) -> set[str] | None:
    """Read the current IN-list values for a named CHECK constraint from sqlite_master."""
    from sqlalchemy import text
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),
        {'n': table_name}
    ).fetchone()
    if not row:
        return None
    m = re.search(
        rf'CONSTRAINT {re.escape(constraint_name)} CHECK \((.+?)\)',
        row[0], re.DOTALL | re.IGNORECASE
    )
    return _extract_quoted_values(m.group(1)) if m else None


def _current_constraint_values_pg(conn, constraint_name: str) -> set[str] | None:
    """Read the current IN-list values for a named CHECK constraint from PostgreSQL."""
    from sqlalchemy import text
    row = conn.execute(
        text("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c WHERE c.conname = :n"),
        {'n': constraint_name}
    ).fetchone()
    return _extract_quoted_values(row[0]) if row else None


def _rebuild_sqlite_table(engine, model_class) -> None:
    """
    Full table rebuild for SQLite (ALTER CONSTRAINT is not supported).
    Steps: create new table with updated DDL, copy data, drop old, rename.
    Re-creates all indexes after rename.
    """
    from sqlalchemy import text
    from sqlalchemy.schema import CreateTable, CreateIndex

    table = model_class.__table__
    tmp = table.name + '_sync_tmp'
    cols = ', '.join(f'"{c.name}"' for c in table.columns)

    create_ddl = str(CreateTable(table).compile(dialect=engine.dialect)).strip()
    # Replace first occurrence of the table name to get the temp-table DDL
    create_tmp_ddl = create_ddl.replace(table.name, tmp, 1)

    with engine.begin() as conn:
        conn.execute(text(create_tmp_ddl))
        conn.execute(text(f'INSERT INTO "{tmp}" ({cols}) SELECT {cols} FROM "{table.name}"'))
        conn.execute(text(f'DROP TABLE "{table.name}"'))
        conn.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{table.name}"'))
        for idx in table.indexes:
            conn.execute(text(str(CreateIndex(idx).compile(dialect=engine.dialect))))

    print(f'  Rebuilt {table.name} (SQLite full-table rebuild)')


def _update_pg_constraint(engine, table_name: str, constraint_name: str, check_expr: str) -> None:
    """Drop and re-add a single CHECK constraint on PostgreSQL."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE "{table_name}" DROP CONSTRAINT IF EXISTS "{constraint_name}"'
        ))
        conn.execute(text(
            f'ALTER TABLE "{table_name}" ADD CONSTRAINT "{constraint_name}" CHECK ({check_expr})'
        ))
    print(f'  Updated constraint {constraint_name} on {table_name}')


def migrate_check_constraints(app) -> None:
    """
    Compare each managed CHECK constraint against the current YAML vocabularies.
    Rebuild (SQLite) or ALTER (PostgreSQL) any constraint whose values have drifted.

    Call explicitly after editing a vocab YAML file — do NOT call on every startup.
    The operator controls when this runs so a YAML edit in production does not
    trigger a surprise table rebuild.
    """
    from sqlalchemy import CheckConstraint as SACheckConstraint
    from extensions import db
    import stakeholder_directory.models as sd

    # Only models that carry CHECK constraints over vocab columns
    models_with_constraints = [sd.Organisation, sd.Engagement, sd.Flag]

    with app.app_context():
        dialect = db.engine.dialect.name
        tables_to_rebuild: set = set()   # SQLite: rebuild the whole table
        pg_updates: list = []            # PostgreSQL: (table, name, expr) per constraint

        with db.engine.connect() as conn:
            for model_class in models_with_constraints:
                table = model_class.__table__
                for constraint in table.constraints:
                    if not isinstance(constraint, SACheckConstraint) or not constraint.name:
                        continue
                    expected = _extract_quoted_values(str(constraint.sqltext))
                    if not expected:
                        continue  # no IN list to compare (shouldn't happen)

                    if dialect == 'sqlite':
                        current = _current_constraint_values_sqlite(
                            conn, table.name, constraint.name
                        )
                    else:
                        current = _current_constraint_values_pg(conn, constraint.name)

                    if current is None:
                        print(f'  WARNING: {constraint.name} not found — run migrations first')
                        continue

                    if current != expected:
                        added = sorted(expected - current)
                        removed = sorted(current - expected)
                        parts = []
                        if added:
                            parts.append(f'+{added}')
                        if removed:
                            parts.append(f'-{removed}')
                        print(f'  Drift: {constraint.name} ({", ".join(parts)})')

                        if dialect == 'sqlite':
                            tables_to_rebuild.add(model_class)
                        else:
                            check_expr = str(
                                constraint.sqltext.compile(dialect=db.engine.dialect)
                            )
                            pg_updates.append((table.name, constraint.name, check_expr))

        if not tables_to_rebuild and not pg_updates:
            print('[stakeholder_directory] All CHECK constraints up to date.')
            return

        if dialect == 'sqlite':
            for model_class in tables_to_rebuild:
                _rebuild_sqlite_table(db.engine, model_class)
        else:
            for table_name, cname, expr in pg_updates:
                _update_pg_constraint(db.engine, table_name, cname, expr)

        print('[stakeholder_directory] Constraint sync complete.')


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    from dotenv import load_dotenv
    from flask import Flask
    from extensions import db

    load_dotenv()

    parser = argparse.ArgumentParser(description='Stakeholder directory migrations')
    parser.add_argument(
        '--sync-vocab',
        action='store_true',
        help='Sync CHECK constraints with current YAML vocab files (run after editing config/*.yaml)',
    )
    args = parser.parse_args()

    _app = Flask(__name__, root_path=_PROJECT_ROOT)
    _db_url = os.environ.get(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(_PROJECT_ROOT, 'intelligence.db')
    )
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

    _app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'migrations-dev-key')
    db.init_app(_app)

    if args.sync_vocab:
        migrate_check_constraints(_app)
    else:
        run_migrations(_app)
