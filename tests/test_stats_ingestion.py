"""
Tests for Phase 1.5 stats ingestion: period parsing, StatObservation writes,
SWR behaviour, cutoff enforcement, and the get_latest_observation_for_stat()
query helper.

Run: python -m pytest tests/test_stats_ingestion.py -v

All external network calls (ONS API, Gemini) are mocked — no live requests.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# Shared test app — same pattern as test_routes.py / test_stats_schema.py
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


def _make_stat(db, source_id: str, source_type: str = 'ons_timeseries',
               theme_slug: str = 'economy', source_url: str = 'https://www.ons.gov.uk/test'):
    from hansard_archive.models import HeadlineStat
    stat = HeadlineStat(
        theme_slug=theme_slug,
        source_type=source_type,
        source_id=source_id,
        display_label='Test stat',
        display_hint='raw',
        geography='UK',
        unit='%',
        source_url=source_url,
        last_refreshed=datetime(2026, 1, 1),
        last_success=datetime(2026, 1, 1),
    )
    db.session.add(stat)
    db.session.commit()
    return stat


def _ons_api_response(value='3.2', label='2024 Q3', freq='quarters',
                      release_date='2025-01-15', title='UK GDP growth'):
    """Build a minimal ONS Beta API JSON response."""
    obs_list = [{'label': label, 'value': value}]
    return {
        'description': {
            'title': title,
            'releaseDate': release_date,
            'unit': '%',
        },
        freq: obs_list,
    }


def _gemini_extraction(figure='3.2', unit='%', period_label='Q3 2024',
                       release_date='2025-01-15', verbatim='GDP grew by 3.2% in Q3 2024.'):
    """Build a minimal Gemini extraction response."""
    extracted = {
        'figure': figure,
        'unit': unit,
        'period_label': period_label,
        'release_date': release_date,
        'verbatim': verbatim,
    }
    return {
        'candidates': [{
            'content': {
                'parts': [{'text': json.dumps(extracted)}]
            }
        }]
    }


# ---------------------------------------------------------------------------
# Period parsing — _parse_ons_period
# ---------------------------------------------------------------------------

class TestParseOnsPeriod:
    """Unit tests for the ONS period label parser."""

    def _p(self, label, freq):
        from stats_refresh import _parse_ons_period
        return _parse_ons_period(label, freq)

    def test_calendar_year(self):
        start, end = self._p('2024', 'years')
        assert start == date(2024, 1, 1)
        assert end == date(2024, 12, 31)

    def test_financial_year_slash(self):
        start, end = self._p('2023/24', 'years')
        assert start == date(2023, 4, 1)
        assert end == date(2024, 3, 31)

    def test_financial_year_dash(self):
        start, end = self._p('2023-24', 'years')
        assert start == date(2023, 4, 1)
        assert end == date(2024, 3, 31)

    def test_quarter_year_first(self):
        start, end = self._p('2024 Q3', 'quarters')
        assert start == date(2024, 7, 1)
        assert end == date(2024, 9, 30)

    def test_quarter_q_first(self):
        start, end = self._p('Q1 2025', 'quarters')
        assert start == date(2025, 1, 1)
        assert end == date(2025, 3, 31)

    def test_quarter_q4(self):
        start, end = self._p('2024 Q4', 'quarters')
        assert start == date(2024, 10, 1)
        assert end == date(2024, 12, 31)

    def test_month_abbr_year(self):
        start, end = self._p('Jan 2024', 'months')
        assert start == date(2024, 1, 1)
        assert end == date(2024, 1, 31)

    def test_month_year_abbr_reversed(self):
        start, end = self._p('2024 JAN', 'months')
        assert start == date(2024, 1, 1)
        assert end == date(2024, 1, 31)

    def test_month_feb_leap_year(self):
        start, end = self._p('Feb 2024', 'months')
        assert start == date(2024, 2, 1)
        assert end == date(2024, 2, 29)  # 2024 is a leap year

    def test_unknown_label_returns_none(self):
        start, end = self._p('unknown period', 'months')
        assert start is None
        assert end is None

    def test_unknown_freq_returns_none(self):
        from stats_refresh import _parse_ons_period
        start, end = _parse_ons_period('2024', 'decades')
        assert start is None
        assert end is None


# ---------------------------------------------------------------------------
# Period parsing — _parse_govuk_period
# ---------------------------------------------------------------------------

class TestParseGovukPeriod:
    """Unit tests for the govuk_bulletin free-text period parser."""

    def _p(self, label):
        from stats_refresh import _parse_govuk_period
        return _parse_govuk_period(label)

    def test_quarter_q_first(self):
        start, end = self._p('Q3 2024')
        assert start == date(2024, 7, 1)
        assert end == date(2024, 9, 30)

    def test_quarter_year_first(self):
        start, end = self._p('2024 Q1')
        assert start == date(2024, 1, 1)
        assert end == date(2024, 3, 31)

    def test_financial_year_slash(self):
        start, end = self._p('2023/24')
        assert start == date(2023, 4, 1)
        assert end == date(2024, 3, 31)

    def test_financial_year_long(self):
        start, end = self._p('2023/2024')
        assert start == date(2023, 4, 1)
        assert end == date(2024, 3, 31)

    def test_financial_year_dash(self):
        start, end = self._p('2023-24')
        assert start == date(2023, 4, 1)
        assert end == date(2024, 3, 31)

    def test_month_full_name(self):
        start, end = self._p('January 2026')
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 31)

    def test_month_abbreviated(self):
        start, end = self._p('Mar 2025')
        assert start == date(2025, 3, 1)
        assert end == date(2025, 3, 31)

    def test_calendar_year(self):
        start, end = self._p('2024')
        assert start == date(2024, 1, 1)
        assert end == date(2024, 12, 31)

    def test_empty_string_returns_none(self):
        assert self._p('') == (None, None)

    def test_unrecognised_returns_none(self):
        assert self._p('last year') == (None, None)

    def test_case_insensitive_quarter(self):
        start, end = self._p('q2 2025')
        assert start == date(2025, 4, 1)
        assert end == date(2025, 6, 30)


# ---------------------------------------------------------------------------
# Ingestion — ONS timeseries
# ---------------------------------------------------------------------------

class TestOnsIngestion:

    def _run_refresh(self, test_app, db, stat):
        """Run _refresh_stat inside the test app context."""
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            return _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

    def test_creates_new_observation(self, test_app, db):
        from hansard_archive.models import StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-new-obs')
            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(
                value='3.2', label='2024 Q3', freq='quarters',
                release_date='2025-01-15',
            )
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            assert result == 'ok'
            obs = StatObservation.query.filter_by(headline_stat_id=stat.id).first()
            assert obs is not None
            assert obs.value == '3.2'
            assert obs.period_start == date(2024, 7, 1)
            assert obs.period_end == date(2024, 9, 30)
            assert obs.release_date == date(2025, 1, 15)
            assert obs.straddles_cutoff is False

    def test_upserts_existing_observation(self, test_app, db):
        from hansard_archive.models import StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-upsert-obs')
            # Seed an existing observation for the same period
            existing = StatObservation(
                headline_stat_id=stat.id,
                period_start=date(2024, 7, 1),
                period_end=date(2024, 9, 30),
                value='3.0',  # stale value
                source_wording='old wording',
                straddles_cutoff=False,
            )
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id

            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(
                value='3.2', label='2024 Q3', freq='quarters',
            )
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            updated = db.session.get(StatObservation, existing_id)
            assert updated.value == '3.2'
            # No new rows created
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 1

    def test_plain_english_fields_null_on_observation(self, test_app, db):
        from hansard_archive.models import StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-null-pe')
            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(label='2024 Q3', freq='quarters')
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            obs = StatObservation.query.filter_by(headline_stat_id=stat.id).first()
            assert obs.plain_english is None
            assert obs.plain_english_generated_at is None
            assert obs.rewrite_model is None

    def test_legacy_fields_mirrored_on_success(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-mirror')
            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(
                value='1.8', label='2024 Q3', freq='quarters',
                release_date='2025-01-15',
            )
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.latest_value == '1.8'
            assert reloaded.release_date == date(2025, 1, 15)
            assert reloaded.source_wording is not None
            # Phase 1.5: plain English fields explicitly NULLed on every refresh
            assert reloaded.plain_english is None
            assert reloaded.plain_english_generated_at is None
            assert reloaded.rewrite_model is None

    def test_pre_cutoff_observation_rejected(self, test_app, db):
        from hansard_archive.models import HeadlineStat, StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-pre-cutoff')
            # Q1 2024 ends 31 March 2024 — before the 1 July 2024 cutoff
            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(
                value='2.1', label='2024 Q1', freq='quarters',
            )
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            assert result == 'ok'
            # No StatObservation row created
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0
            # But legacy field DOES update (SWR preserved)
            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.latest_value == '2.1'

    def test_straddling_observation_sets_flag(self, test_app, db):
        from hansard_archive.models import StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-straddle')
            # Financial year 2023/24: Apr 2023 – Mar 2024 → straddles cutoff Jul 2024? No.
            # Use 2023/24 as a year: start Apr 2023, end Mar 2024 → ends BEFORE cutoff → rejected
            # We need period that starts BEFORE cutoff AND ends ON/AFTER cutoff:
            # 2023/24 → Apr 2023 – Mar 2024 is fully before cutoff
            # Use a manual constructed scenario: Q2 2024 = Apr 1 – Jun 30 → fully before cutoff
            # What straddles? A year "2024" → Jan 1 – Dec 31, starts before Jul 1, ends after
            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(
                value='2.5', label='2024', freq='years',
            )
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            obs = StatObservation.query.filter_by(headline_stat_id=stat.id).first()
            assert obs is not None
            assert obs.straddles_cutoff is True
            assert obs.period_start == date(2024, 1, 1)
            assert obs.period_end == date(2024, 12, 31)

    def test_failed_fetch_only_updates_last_refreshed(self, test_app, db):
        from hansard_archive.models import HeadlineStat, StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-fail-swr')
            stat.latest_value = 'preserved-value'
            db.session.commit()

            mock_resp = MagicMock()
            mock_resp.raise_for_status.side_effect = Exception("network error")

            with patch('requests.get', return_value=mock_resp):
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            assert result == 'failed'
            # No observation written
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0
            # Legacy value preserved (SWR)
            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.latest_value == 'preserved-value'
            # last_refreshed updated (SWR)
            assert reloaded.last_refreshed is not None

    def test_manual_stat_skipped(self, test_app, db):
        from hansard_archive.models import StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'manual-skip', source_type='manual')
            with patch('requests.get') as mock_get:
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)
            mock_get.assert_not_called()
            assert result == 'skipped'
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0

    def test_unparseable_ons_period_skips_observation_but_updates_legacy(self, test_app, db):
        from hansard_archive.models import HeadlineStat, StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'ons-no-period')
            # ONS returns a label that can't be parsed as a period
            mock_resp = MagicMock()
            mock_resp.json.return_value = _ons_api_response(
                value='99', label='rolling 12 months', freq='months',
            )
            mock_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=mock_resp):
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            assert result == 'ok'
            # No StatObservation because period couldn't be parsed
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0
            # But legacy fields still updated
            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.latest_value == '99'


# ---------------------------------------------------------------------------
# Ingestion — govuk_bulletin
# ---------------------------------------------------------------------------

class TestGovukBulletinIngestion:

    def test_creates_observation_from_gemini(self, test_app, db):
        from hansard_archive.models import StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'govuk-new-obs', source_type='govuk_bulletin',
                              source_url='https://www.gov.uk/some-bulletin')
            page_resp = MagicMock()
            page_resp.text = '<html>Some bulletin content</html>'
            page_resp.raise_for_status = MagicMock()

            gemini_resp = MagicMock()
            gemini_resp.json.return_value = _gemini_extraction(
                figure='89.2', unit='%', period_label='Q3 2024',
                release_date='2025-03-01',
                verbatim='89.2% of graduates were in employment in Q3 2024.',
            )
            gemini_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=page_resp), \
                 patch('requests.post', return_value=gemini_resp):
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            assert result == 'ok'
            obs = StatObservation.query.filter_by(headline_stat_id=stat.id).first()
            assert obs is not None
            assert obs.value == '89.2'
            assert obs.period_start == date(2024, 7, 1)
            assert obs.period_end == date(2024, 9, 30)
            assert obs.source_wording == '89.2% of graduates were in employment in Q3 2024.'
            assert obs.plain_english is None

    def test_unparseable_govuk_period_skips_observation(self, test_app, db):
        from hansard_archive.models import HeadlineStat, StatObservation
        from stats_refresh import _refresh_stat
        with test_app.app_context():
            stat = _make_stat(db, 'govuk-no-period', source_type='govuk_bulletin',
                              source_url='https://www.gov.uk/some-bulletin')
            page_resp = MagicMock()
            page_resp.text = '<html>content</html>'
            page_resp.raise_for_status = MagicMock()

            gemini_resp = MagicMock()
            gemini_resp.json.return_value = _gemini_extraction(
                period_label='academic year 2024 to 2025',  # not a parseable pattern
            )
            gemini_resp.raise_for_status = MagicMock()

            with patch('requests.get', return_value=page_resp), \
                 patch('requests.post', return_value=gemini_resp):
                result = _refresh_stat(stat, gemini_key='fake-key', dry_run=False)

            assert result == 'ok'
            # No obs — period parsing failed
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0
            # Legacy fields still updated
            reloaded = db.session.get(HeadlineStat, stat.id)
            assert reloaded.latest_value == '3.2'


# ---------------------------------------------------------------------------
# Query helper — get_latest_observation_for_stat
# ---------------------------------------------------------------------------

class TestGetLatestObservationForStat:

    def test_returns_newest_period_end(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            stat = _make_stat(db, 'q-newest', theme_slug='q-newest')
            db.session.add_all([
                StatObservation(headline_stat_id=stat.id,
                                period_start=date(2024, 7, 1), period_end=date(2024, 9, 30),
                                value='old', straddles_cutoff=False),
                StatObservation(headline_stat_id=stat.id,
                                period_start=date(2024, 10, 1), period_end=date(2024, 12, 31),
                                value='new', straddles_cutoff=False),
            ])
            db.session.commit()

            result = get_latest_observation_for_stat('q-newest')
            assert result is not None
            assert result.value == 'new'

    def test_returns_none_when_stat_not_found(self, test_app, db):
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            assert get_latest_observation_for_stat('this-slug-does-not-exist') is None

    def test_returns_none_when_no_observations(self, test_app, db):
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            _make_stat(db, 'q-no-obs', theme_slug='q-no-obs')
            assert get_latest_observation_for_stat('q-no-obs') is None

    def test_excludes_straddling_by_default(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            stat = _make_stat(db, 'q-straddle-excl', theme_slug='q-straddle-excl')
            db.session.add(StatObservation(
                headline_stat_id=stat.id,
                period_start=date(2024, 1, 1), period_end=date(2024, 12, 31),
                value='straddle', straddles_cutoff=True,
            ))
            db.session.commit()

            assert get_latest_observation_for_stat('q-straddle-excl') is None

    def test_includes_straddling_when_requested(self, test_app, db):
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            stat = _make_stat(db, 'q-straddle-incl', theme_slug='q-straddle-incl')
            db.session.add(StatObservation(
                headline_stat_id=stat.id,
                period_start=date(2024, 1, 1), period_end=date(2024, 12, 31),
                value='straddle-val', straddles_cutoff=True,
            ))
            db.session.commit()

            result = get_latest_observation_for_stat('q-straddle-incl', include_straddling=True)
            assert result is not None
            assert result.value == 'straddle-val'

    def test_null_period_end_ordered_last(self, test_app, db):
        """Legacy placeholder rows (NULL period_end) should lose to real observations."""
        from hansard_archive.models import StatObservation
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            stat = _make_stat(db, 'q-null-period', theme_slug='q-null-period')
            # Placeholder from Phase 1 backfill (period_start/end both NULL)
            db.session.add(StatObservation(
                headline_stat_id=stat.id,
                period_start=None, period_end=None,
                value='placeholder', straddles_cutoff=False,
            ))
            # Real observation with a proper period
            db.session.add(StatObservation(
                headline_stat_id=stat.id,
                period_start=date(2024, 10, 1), period_end=date(2024, 12, 31),
                value='real', straddles_cutoff=False,
            ))
            db.session.commit()

            result = get_latest_observation_for_stat('q-null-period')
            assert result is not None
            assert result.value == 'real'

    def test_disambiguate_by_source_id(self, test_app, db):
        """When theme_slug matches multiple stats, source_id selects the right one."""
        from hansard_archive.models import HeadlineStat, StatObservation
        from hansard_archive.stats_queries import get_latest_observation_for_stat
        with test_app.app_context():
            shared_slug = 'q-shared-slug'
            stat_a = _make_stat(db, 'source-a', theme_slug=shared_slug)
            stat_b = _make_stat(db, 'source-b', theme_slug=shared_slug)
            db.session.add(StatObservation(
                headline_stat_id=stat_a.id,
                period_start=date(2024, 10, 1), period_end=date(2024, 12, 31),
                value='from-a', straddles_cutoff=False,
            ))
            db.session.add(StatObservation(
                headline_stat_id=stat_b.id,
                period_start=date(2024, 10, 1), period_end=date(2024, 12, 31),
                value='from-b', straddles_cutoff=False,
            ))
            db.session.commit()

            result = get_latest_observation_for_stat(shared_slug, source_id='source-b')
            assert result is not None
            assert result.value == 'from-b'
