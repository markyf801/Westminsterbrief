"""
ha_cron_run column defaults migration.

upgrade(engine) — adds DEFAULT 0 to the three NOT NULL integer columns on
  ha_cron_run that previously had only a Python-layer ORM default. This makes
  raw SQL INSERTs (e.g. psycopg2 direct INSERT) safe when those columns are
  omitted. Uses ALTER TABLE ... ALTER COLUMN ... SET DEFAULT, which is a no-op
  if the DEFAULT is already set.

  SQLite does not support ALTER COLUMN — the try/except per column handles this
  silently. On SQLite the server_default on the model ensures db.create_all()
  produces correct DDL for new databases.

downgrade(engine) — removes the DEFAULT clause from the three columns. Does not
  affect any data or other columns.
"""

from __future__ import annotations

from sqlalchemy import text


def upgrade(engine) -> None:
    with engine.begin() as conn:
        for col in ('sessions_ingested', 'sessions_tagged', 'errors'):
            try:
                conn.execute(text(
                    f'ALTER TABLE ha_cron_run ALTER COLUMN {col} SET DEFAULT 0'
                ))
            except Exception:
                pass  # SQLite doesn't support ALTER COLUMN; already set; etc.


def downgrade(engine) -> None:
    with engine.begin() as conn:
        for col in ('sessions_ingested', 'sessions_tagged', 'errors'):
            try:
                conn.execute(text(
                    f'ALTER TABLE ha_cron_run ALTER COLUMN {col} DROP DEFAULT'
                ))
            except Exception:
                pass  # SQLite doesn't support ALTER COLUMN
