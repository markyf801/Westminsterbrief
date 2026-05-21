"""
Tests for ha_cron_run column defaults migration.

Verifies that:
  - The three target columns have server_default set on the SQLAlchemy model
  - A raw SQL INSERT omitting the three columns succeeds (uses server default)
  - An ORM insert omitting the three columns succeeds (uses Python default)
  - upgrade() and downgrade() are callable without exception
  - upgrade() is idempotent (safe to call twice)

SQLite note: ALTER COLUMN SET DEFAULT is not supported in SQLite, so
upgrade()/downgrade() silently pass on SQLite. The meaningful tests here are
the INSERT tests — they verify the server_default on the model ensures correct
DDL on fresh databases (which is the production-facing guarantee from db.create_all()).

Run: python -m pytest tests/test_ha_cron_run_defaults.py -v
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models  # noqa: F401 — registers all tables
    import cache_models             # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))
    app.config.update({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'TESTING': True,
        'SECRET_KEY': 'test',
    })
    _db.init_app(app)

    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope='module')
def db(test_app):
    from extensions import db as _db
    return _db


# ---------------------------------------------------------------------------
# Model-level tests
# ---------------------------------------------------------------------------

def test_server_default_set_on_sessions_ingested():
    from hansard_archive.models import HaCronRun
    col = HaCronRun.__table__.c['sessions_ingested']
    assert col.server_default is not None, \
        "sessions_ingested must have a server_default for raw SQL INSERT safety"


def test_server_default_set_on_sessions_tagged():
    from hansard_archive.models import HaCronRun
    col = HaCronRun.__table__.c['sessions_tagged']
    assert col.server_default is not None, \
        "sessions_tagged must have a server_default for raw SQL INSERT safety"


def test_server_default_set_on_errors():
    from hansard_archive.models import HaCronRun
    col = HaCronRun.__table__.c['errors']
    assert col.server_default is not None, \
        "errors must have a server_default for raw SQL INSERT safety"


def test_python_default_still_present():
    """Python-layer defaults must remain so ORM inserts still work without DB round-trip."""
    from hansard_archive.models import HaCronRun
    for col_name in ('sessions_ingested', 'sessions_tagged', 'errors'):
        col = HaCronRun.__table__.c[col_name]
        assert col.default is not None, \
            f"{col_name} must retain Python-layer default=0"


# ---------------------------------------------------------------------------
# INSERT tests — these are the meaningful guarantees
# ---------------------------------------------------------------------------

def test_raw_sql_insert_without_three_columns_succeeds(test_app, db):
    """
    Raw SQL INSERT omitting sessions_ingested, sessions_tagged, errors succeeds.
    This is the exact failure mode that was fixed — backup_to_r2.py used a
    raw psycopg2 INSERT that omitted these columns.

    On SQLite the server_default on the model means db.create_all() created the
    column with DEFAULT 0 in the schema, so omitting them in INSERT is valid.
    """
    from sqlalchemy import text

    with test_app.app_context():
        now = datetime.utcnow()
        db.session.execute(text(
            "INSERT INTO ha_cron_run "
            "(service_name, started_at, days_window, status) "
            "VALUES (:svc, :at, :days, :status)"
        ), {"svc": "test-raw-insert", "at": now, "days": 1, "status": "ok"})
        db.session.commit()

        row = db.session.execute(text(
            "SELECT sessions_ingested, sessions_tagged, errors "
            "FROM ha_cron_run WHERE service_name = 'test-raw-insert'"
        )).fetchone()
        assert row is not None
        assert row[0] == 0, f"sessions_ingested should default to 0, got {row[0]}"
        assert row[1] == 0, f"sessions_tagged should default to 0, got {row[1]}"
        assert row[2] == 0, f"errors should default to 0, got {row[2]}"


def test_orm_insert_without_three_columns_succeeds(test_app, db):
    """ORM insert omitting the three columns uses Python-layer defaults correctly."""
    with test_app.app_context():
        from hansard_archive.models import HaCronRun
        run = HaCronRun(
            service_name='test-orm-insert',
            started_at=datetime.utcnow(),
            days_window=3,
            status='ok',
        )
        db.session.add(run)
        db.session.commit()
        db.session.refresh(run)

        assert run.sessions_ingested == 0
        assert run.sessions_tagged == 0
        assert run.errors == 0


def test_existing_rows_unchanged_after_upgrade(test_app, db):
    """Calling upgrade() does not modify existing rows."""
    with test_app.app_context():
        from hansard_archive.models import HaCronRun
        run = HaCronRun(
            service_name='test-pre-existing',
            started_at=datetime.utcnow(),
            days_window=2,
            sessions_ingested=5,
            sessions_tagged=3,
            errors=1,
            status='ok',
        )
        db.session.add(run)
        db.session.commit()
        row_id = run.id

        from migrations.ha_cron_run_defaults import upgrade
        upgrade(db.engine)

        db.session.expire_all()
        reloaded = db.session.get(HaCronRun, row_id)
        assert reloaded.sessions_ingested == 5
        assert reloaded.sessions_tagged == 3
        assert reloaded.errors == 1


# ---------------------------------------------------------------------------
# Migration function tests
# ---------------------------------------------------------------------------

def test_upgrade_callable_without_exception(test_app, db):
    with test_app.app_context():
        from migrations.ha_cron_run_defaults import upgrade
        upgrade(db.engine)  # must not raise


def test_upgrade_idempotent(test_app, db):
    """Calling upgrade() twice must not raise."""
    with test_app.app_context():
        from migrations.ha_cron_run_defaults import upgrade
        upgrade(db.engine)
        upgrade(db.engine)  # second call must also not raise


def test_downgrade_callable_without_exception(test_app, db):
    with test_app.app_context():
        from migrations.ha_cron_run_defaults import downgrade
        downgrade(db.engine)  # must not raise
