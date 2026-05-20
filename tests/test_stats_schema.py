"""
Tests for Phase 1 stats schema: StatDefinition, StatPolicyArea, StatObservation,
new HeadlineStat fields, and stats_queries helpers.

Uses a fresh in-memory SQLite app (same pattern as test_routes.py) to avoid
touching the real DB or running flask_app.py's startup sequence.

Run: python -m pytest tests/test_stats_schema.py -v
"""

from __future__ import annotations

import pytest
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# App fixture — shared across all tests in the module
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models  # noqa: F401 — registers all tables
    import cache_models             # noqa: F401 — CachedMember referenced by ha_bill_sponsor FK
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


def _make_stat(db, source_id: str, theme_slug: str = 'education',
               latest_value: str | None = '8.9m') -> 'HeadlineStat':
    """Helper — create a HeadlineStat with a unique source_id."""
    from hansard_archive.models import HeadlineStat
    stat = HeadlineStat(
        theme_slug=theme_slug,
        source_type='govuk_bulletin',
        source_id=source_id,
        display_label='Test stat',
        display_hint='raw',
        geography='England',
        latest_value=latest_value,
        unit='pupils',
        source_url='https://example.com',
        last_refreshed=datetime(2026, 5, 1, 9, 0),
        last_success=datetime(2026, 5, 1, 9, 0),
    )
    db.session.add(stat)
    db.session.commit()
    return stat


# ---------------------------------------------------------------------------
# HeadlineStat — new Phase 1 fields
# ---------------------------------------------------------------------------

class TestHeadlineStatPhase1Fields:

    def test_new_fields_default_null(self, test_app, db):
        with test_app.app_context():
            stat = _make_stat(db, 'hs-null-fields')
            assert stat.producer_url is None
            assert stat.methodology_url is None
            assert stat.definition_id is None
            assert stat.release_type is None
            assert stat.sub_topic_slug is None

    def test_new_fields_persist(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        with test_app.app_context():
            stat = _make_stat(db, 'hs-persist-fields')
            stat.producer_url = 'https://example.com/stat'
            stat.methodology_url = 'https://example.com/methodology'
            stat.release_type = 'headline'
            stat.sub_topic_slug = 'higher-education'
            db.session.commit()

            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.producer_url == 'https://example.com/stat'
            assert reloaded.release_type == 'headline'
            assert reloaded.sub_topic_slug == 'higher-education'

    def test_release_type_values(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        with test_app.app_context():
            stat = _make_stat(db, 'hs-release-type-vals')
            for rt in ('headline', 'reference', 'analysis', 'ad_hoc'):
                stat.release_type = rt
                db.session.commit()
                assert db.session.get(HeadlineStat, stat.id).release_type == rt

    def test_release_type_nullable(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        with test_app.app_context():
            stat = _make_stat(db, 'hs-release-type-null')
            stat.release_type = None
            db.session.commit()
            assert db.session.get(HeadlineStat, stat.id).release_type is None


# ---------------------------------------------------------------------------
# StatDefinition
# ---------------------------------------------------------------------------

class TestStatDefinition:

    def test_create_and_retrieve(self, test_app, db):
        from hansard_archive.models import StatDefinition
        with test_app.app_context():
            defn = StatDefinition(
                slug='highly-skilled-employment-soc-1-3',
                name='Highly skilled employment (SOC 1–3)',
                producer='HESA',
                description='Share of graduates in SOC groups 1–3, 15 months after graduation.',
                cohort_definition='Domiciled UK graduates, 15 months post-graduation',
                denominator='All respondents in employment',
            )
            db.session.add(defn)
            db.session.commit()

            retrieved = StatDefinition.query.filter_by(
                slug='highly-skilled-employment-soc-1-3'
            ).first()
            assert retrieved is not None
            assert retrieved.name == 'Highly skilled employment (SOC 1–3)'
            assert retrieved.producer == 'HESA'
            assert retrieved.created_at is not None

    def test_slug_unique_constraint(self, test_app, db):
        from hansard_archive.models import StatDefinition
        with test_app.app_context():
            now = datetime.utcnow()
            d1 = StatDefinition(slug='dup-slug-test', name='First',
                                created_at=now, updated_at=now)
            db.session.add(d1)
            db.session.commit()

            d2 = StatDefinition(slug='dup-slug-test', name='Second',
                                created_at=now, updated_at=now)
            db.session.add(d2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_fk_to_headline_stat(self, test_app, db):
        from hansard_archive.models import HeadlineStat, StatDefinition
        with test_app.app_context():
            defn = StatDefinition(slug='test-defn-fk', name='Test Definition',
                                  created_at=datetime.utcnow(), updated_at=datetime.utcnow())
            db.session.add(defn)
            db.session.flush()

            stat = _make_stat(db, 'hs-defn-fk-test')
            stat.definition_id = defn.id
            db.session.commit()

            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.definition is not None
            assert reloaded.definition.slug == 'test-defn-fk'


# ---------------------------------------------------------------------------
# StatPolicyArea
# ---------------------------------------------------------------------------

class TestStatPolicyArea:

    def test_create_primary_policy_area(self, test_app, db):
        from hansard_archive.models import StatPolicyArea
        with test_app.app_context():
            stat = _make_stat(db, 'hs-spa-primary')
            spa = StatPolicyArea(headline_stat_id=stat.id,
                                 theme_slug='education', is_primary=True)
            db.session.add(spa)
            db.session.commit()

            retrieved = StatPolicyArea.query.filter_by(
                headline_stat_id=stat.id, theme_slug='education'
            ).first()
            assert retrieved is not None
            assert retrieved.is_primary is True

    def test_secondary_policy_area(self, test_app, db):
        from hansard_archive.models import StatPolicyArea
        with test_app.app_context():
            stat = _make_stat(db, 'hs-spa-secondary')
            primary = StatPolicyArea(headline_stat_id=stat.id,
                                     theme_slug='education', is_primary=True)
            secondary = StatPolicyArea(headline_stat_id=stat.id,
                                       theme_slug='children-and-families', is_primary=False)
            db.session.add_all([primary, secondary])
            db.session.commit()

            areas = StatPolicyArea.query.filter_by(headline_stat_id=stat.id).all()
            assert len(areas) == 2
            assert sum(a.is_primary for a in areas) == 1

    def test_unique_constraint_stat_theme(self, test_app, db):
        from hansard_archive.models import StatPolicyArea
        with test_app.app_context():
            stat = _make_stat(db, 'hs-spa-unique')
            a1 = StatPolicyArea(headline_stat_id=stat.id,
                                theme_slug='unique-area', is_primary=True)
            db.session.add(a1)
            db.session.flush()
            a2 = StatPolicyArea(headline_stat_id=stat.id,
                                theme_slug='unique-area', is_primary=False)
            db.session.add(a2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_cascade_delete_with_stat(self, test_app, db):
        from hansard_archive.models import StatPolicyArea
        with test_app.app_context():
            stat = _make_stat(db, 'hs-spa-cascade')
            spa = StatPolicyArea(headline_stat_id=stat.id,
                                 theme_slug='housing', is_primary=True)
            db.session.add(spa)
            db.session.commit()

            stat_id = stat.id
            db.session.delete(stat)
            db.session.commit()
            assert StatPolicyArea.query.filter_by(headline_stat_id=stat_id).count() == 0


# ---------------------------------------------------------------------------
# StatObservation
# ---------------------------------------------------------------------------

class TestStatObservation:

    def test_create_observation(self, test_app, db):
        from hansard_archive.models import StatObservation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-create')
            obs = StatObservation(
                headline_stat_id=stat.id,
                period_label='2024/25',
                period_start=date(2024, 8, 1),
                period_end=date(2025, 7, 31),
                value='9.1m',
                release_date=date(2026, 3, 1),
                release_url='https://example.com/release',
                straddles_cutoff=False,
            )
            db.session.add(obs)
            db.session.commit()

            retrieved = db.session.get(StatObservation, obs.id)
            assert retrieved.value == '9.1m'
            assert retrieved.period_label == '2024/25'
            assert retrieved.straddles_cutoff is False

    def test_unique_constraint_period(self, test_app, db):
        from hansard_archive.models import StatObservation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-unique')
            kwargs = dict(headline_stat_id=stat.id,
                          period_start=date(2024, 9, 1),
                          period_end=date(2025, 8, 31),
                          straddles_cutoff=False)
            db.session.add(StatObservation(**kwargs))
            db.session.flush()
            db.session.add(StatObservation(**kwargs))
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_straddles_cutoff_flag(self, test_app, db):
        from hansard_archive.models import StatObservation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-straddle')
            obs = StatObservation(headline_stat_id=stat.id,
                                  period_start=date(2024, 1, 1),
                                  period_end=date(2024, 12, 31),
                                  value='8.8m', straddles_cutoff=True)
            db.session.add(obs)
            db.session.commit()
            assert db.session.get(StatObservation, obs.id).straddles_cutoff is True

    def test_cascade_delete_with_stat(self, test_app, db):
        from hansard_archive.models import StatObservation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-cascade')
            obs = StatObservation(headline_stat_id=stat.id,
                                  period_start=date(2024, 8, 1),
                                  period_end=date(2025, 7, 31),
                                  straddles_cutoff=False)
            db.session.add(obs)
            db.session.commit()

            db.session.delete(stat)
            db.session.commit()
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0


# ---------------------------------------------------------------------------
# stats_queries pure-function helpers (no DB needed)
# ---------------------------------------------------------------------------

class TestCutoffHelpers:

    def test_cutoff_date(self):
        from hansard_archive.stats_queries import cutoff_date
        assert cutoff_date() == date(2024, 7, 1)

    def test_is_straddling_true(self):
        from hansard_archive.stats_queries import is_straddling
        assert is_straddling(date(2024, 1, 1), date(2024, 12, 31)) is True

    def test_is_straddling_false_entirely_after_cutoff(self):
        from hansard_archive.stats_queries import is_straddling
        # period_start == cutoff → NOT straddling
        assert is_straddling(date(2024, 7, 1), date(2024, 12, 31)) is False

    def test_is_straddling_false_entirely_before_cutoff(self):
        from hansard_archive.stats_queries import is_straddling
        assert is_straddling(date(2023, 1, 1), date(2024, 6, 30)) is False

    def test_is_straddling_none_dates_returns_false(self):
        from hansard_archive.stats_queries import is_straddling
        assert is_straddling(None, date(2025, 1, 1)) is False
        assert is_straddling(date(2024, 1, 1), None) is False
        assert is_straddling(None, None) is False


# ---------------------------------------------------------------------------
# stats_queries — DB-backed helpers
# ---------------------------------------------------------------------------

class TestStatsQueryHelpers:

    def test_get_headline_stat_with_value(self, test_app, db):
        from hansard_archive.stats_queries import get_headline_stat
        with test_app.app_context():
            _make_stat(db, 'hs-query-has-val', theme_slug='environment', latest_value='42%')
            result = get_headline_stat('environment')
            assert result is not None
            assert result.source_id == 'hs-query-has-val'

    def test_get_headline_stat_no_value_returns_none(self, test_app, db):
        from hansard_archive.stats_queries import get_headline_stat
        with test_app.app_context():
            _make_stat(db, 'hs-query-no-val', theme_slug='transport', latest_value=None)
            assert get_headline_stat('transport') is None

    def test_get_latest_observation_returns_newest_period(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-latest-obs')
            db.session.add_all([
                StatObservation(headline_stat_id=stat.id, period_label='2023/24',
                                period_start=date(2023, 8, 1), period_end=date(2024, 7, 31),
                                value='old', straddles_cutoff=False),
                StatObservation(headline_stat_id=stat.id, period_label='2024/25',
                                period_start=date(2024, 8, 1), period_end=date(2025, 7, 31),
                                value='new', straddles_cutoff=False),
            ])
            db.session.commit()

            latest = get_latest_observation(stat)
            assert latest is not None
            assert latest.value == 'new'

    def test_get_latest_observation_excludes_straddling_by_default(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-exclude-straddle')
            db.session.add(StatObservation(headline_stat_id=stat.id,
                                           period_start=date(2023, 8, 1),
                                           period_end=date(2024, 12, 31),
                                           value='straddle', straddles_cutoff=True))
            db.session.commit()
            assert get_latest_observation(stat) is None

    def test_get_latest_observation_includes_straddling_when_requested(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-include-straddle')
            db.session.add(StatObservation(headline_stat_id=stat.id,
                                           period_start=date(2023, 8, 1),
                                           period_end=date(2024, 12, 31),
                                           value='straddle-incl', straddles_cutoff=True))
            db.session.commit()

            latest = get_latest_observation(stat, include_straddling=True)
            assert latest is not None
            assert latest.value == 'straddle-incl'

    def test_get_latest_observation_none_when_no_observations(self, test_app, db):
        from hansard_archive.stats_queries import get_latest_observation
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-none')
            assert get_latest_observation(stat) is None

    def test_get_observations_limit(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_observations_for_stat
        with test_app.app_context():
            stat = _make_stat(db, 'hs-obs-limit')
            for i in range(5):
                db.session.add(StatObservation(
                    headline_stat_id=stat.id,
                    period_start=date(2024, 7 + i, 1),
                    period_end=date(2024, 7 + i, 28),
                    value=str(i), straddles_cutoff=False,
                ))
            db.session.commit()

            rows = get_observations_for_stat(stat, limit=3)
            assert len(rows) == 3
