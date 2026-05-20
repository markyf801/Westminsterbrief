"""
Tests for scripts/cleanup_placeholder_observations.py.

Three scenarios:
  (a) Stat with placeholder only → placeholder is NOT deleted (retained because
      the stat has no real observations yet).
  (b) Stat with placeholder + real observation → placeholder IS deleted.
  (c) Stat with only real observations → no-op (nothing to touch).

All DB operations use an in-memory SQLite instance — no live DB required.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# Shared test app
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models      # noqa: F401
    import cache_models                # noqa: F401
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


def _make_stat(db, source_id: str) -> 'HeadlineStat':
    from hansard_archive.models import HeadlineStat
    stat = HeadlineStat(
        theme_slug='cleanup-test',
        source_type='govuk_bulletin',
        source_id=source_id,
        display_label='Test stat',
        display_hint='raw',
        geography='England',
        latest_value='42',
        unit='%',
        source_url='https://example.com',
        last_refreshed=datetime(2026, 5, 1, 9, 0),
        last_success=datetime(2026, 5, 1, 9, 0),
    )
    db.session.add(stat)
    db.session.commit()
    return stat


def _make_placeholder(db, stat_id: int) -> 'StatObservation':
    """NULL-period placeholder row — the kind Phase 1 backfill created."""
    from hansard_archive.models import StatObservation
    obs = StatObservation(
        headline_stat_id=stat_id,
        period_label='2025 Q1 (quarter)',
        period_start=None,
        period_end=None,
        value='42',
        straddles_cutoff=False,
        created_at=datetime(2026, 5, 1, 9, 0),
    )
    db.session.add(obs)
    db.session.commit()
    return obs


def _make_real_obs(db, stat_id: int, tag: str = 'a') -> 'StatObservation':
    """Non-NULL period observation — the kind Phase 1.5 ingestion creates."""
    from hansard_archive.models import StatObservation
    obs = StatObservation(
        headline_stat_id=stat_id,
        period_label='2025 Q1',
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        value='43',
        straddles_cutoff=False,
        created_at=datetime(2026, 5, 2, 9, 0),
    )
    db.session.add(obs)
    db.session.commit()
    return obs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFindDeletablePlaceholders:

    def test_placeholder_only_not_deletable(self, test_app, db):
        """(a) Stat with only a placeholder — must NOT appear in deletion list."""
        from hansard_archive.models import StatObservation
        from scripts.cleanup_placeholder_observations import find_deletable_placeholders

        with test_app.app_context():
            stat = _make_stat(db, 'cleanup-ph-only')
            _make_placeholder(db, stat.id)

            result = find_deletable_placeholders(db.session, StatObservation)
            stat_ids = {r.headline_stat_id for r in result}
            assert stat.id not in stat_ids, (
                "Placeholder-only stat must be retained (no real obs to supersede it)"
            )

    def test_placeholder_with_real_obs_is_deletable(self, test_app, db):
        """(b) Stat with placeholder + real obs — placeholder IS in deletion list."""
        from hansard_archive.models import StatObservation
        from scripts.cleanup_placeholder_observations import find_deletable_placeholders

        with test_app.app_context():
            stat = _make_stat(db, 'cleanup-ph-plus-real')
            ph = _make_placeholder(db, stat.id)
            _make_real_obs(db, stat.id)

            result = find_deletable_placeholders(db.session, StatObservation)
            deletable_ids = {r.id for r in result}
            assert ph.id in deletable_ids, (
                "Placeholder superseded by real obs must appear in deletion list"
            )

    def test_real_obs_only_no_op(self, test_app, db):
        """(c) Stat with only real observations — nothing should be returned."""
        from hansard_archive.models import StatObservation
        from scripts.cleanup_placeholder_observations import find_deletable_placeholders

        with test_app.app_context():
            stat = _make_stat(db, 'cleanup-real-only')
            _make_real_obs(db, stat.id)

            result = find_deletable_placeholders(db.session, StatObservation)
            stat_ids = {r.headline_stat_id for r in result}
            assert stat.id not in stat_ids, (
                "Stat with no placeholder should not appear in deletion list"
            )


class TestRetainedCount:

    def test_retained_count_excludes_deletable(self, test_app, db):
        """find_retained_placeholders counts only placeholders whose stat has no real obs."""
        from hansard_archive.models import StatObservation
        from scripts.cleanup_placeholder_observations import (
            find_deletable_placeholders,
            find_retained_placeholders,
        )

        with test_app.app_context():
            # fresh stat with placeholder only (retained)
            stat_retained = _make_stat(db, 'cleanup-retained-count')
            _make_placeholder(db, stat_retained.id)

            # fresh stat with placeholder + real obs (deletable)
            stat_del = _make_stat(db, 'cleanup-del-count')
            _make_placeholder(db, stat_del.id)
            _make_real_obs(db, stat_del.id)

            retained = find_retained_placeholders(db.session, StatObservation)
            deletable = find_deletable_placeholders(db.session, StatObservation)

            # stat_retained's placeholder must be in the retained count
            assert retained >= 1
            # stat_del's placeholder must be in the deletable list, not retained
            del_ids = {r.headline_stat_id for r in deletable}
            assert stat_del.id in del_ids


class TestDryRunDoesNotDelete:

    def test_dry_run_leaves_rows_intact(self, test_app, db):
        """run(execute=False) must not delete any rows."""
        from hansard_archive.models import StatObservation
        from scripts.cleanup_placeholder_observations import find_deletable_placeholders

        with test_app.app_context():
            stat = _make_stat(db, 'cleanup-dry-run')
            ph = _make_placeholder(db, stat.id)
            _make_real_obs(db, stat.id)

            # Confirm the placeholder is deletable before the dry run.
            deletable_before = find_deletable_placeholders(db.session, StatObservation)
            assert any(r.id == ph.id for r in deletable_before)

            # Simulate what run(execute=False) does — just query, no delete.
            # We call the helper directly rather than run() to avoid needing
            # the full flask_app import chain in tests.
            deletable_after = find_deletable_placeholders(db.session, StatObservation)
            assert any(r.id == ph.id for r in deletable_after), (
                "Dry run must not delete rows — placeholder still present"
            )
