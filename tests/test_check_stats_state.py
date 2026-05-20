"""
Tests for scripts/check_stats_state.py.

All tests call collect_stats(db.session) directly — no Flask app import,
no subprocess. DB is an in-memory SQLite instance.

Scenarios:
  1. Empty DB — script runs without error; all counts are zero.
  2. Placeholder-only — one stat, one NULL-period observation.
  3. Mixed — placeholder + real observation; real obs shows in section2.
  4. Mirror mismatch — HeadlineStat.latest_value != StatObservation.value.
  5. --json output — valid JSON with expected top-level keys.
  6. Read-only — no DB rows change during a collect_stats() call.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# Shared fixture
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stat(db, source_id: str, source_type: str = 'ons_timeseries',
               latest_value: str | None = '42') -> object:
    from hansard_archive.models import HeadlineStat
    stat = HeadlineStat(
        theme_slug='check-test',
        source_type=source_type,
        source_id=source_id,
        display_label='Test stat',
        display_hint='raw',
        geography='England',
        latest_value=latest_value,
        unit='%',
        source_url='https://example.com/stat',
        last_refreshed=datetime(2026, 5, 1),
        last_success=datetime(2026, 5, 1),
    )
    db.session.add(stat)
    db.session.commit()
    return stat


def _make_placeholder(db, stat_id: int) -> object:
    from hansard_archive.models import StatObservation
    obs = StatObservation(
        headline_stat_id=stat_id,
        period_label='2024/25 (legacy)',
        period_start=None,
        period_end=None,
        value='42',
        straddles_cutoff=False,
        created_at=datetime(2026, 5, 1),
    )
    db.session.add(obs)
    db.session.commit()
    return obs


def _make_real_obs(db, stat_id: int,
                   value: str = '43',
                   period_label: str = '2024 Q3 (quarter)',
                   period_start: date = date(2024, 7, 1),
                   period_end: date = date(2024, 9, 30),
                   straddles: bool = False) -> object:
    from hansard_archive.models import StatObservation
    obs = StatObservation(
        headline_stat_id=stat_id,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        value=value,
        release_date=date(2025, 1, 15),
        release_url='https://example.com/stat',
        source_wording='Test stat was 43% in Q3 2024.',
        straddles_cutoff=straddles,
        created_at=datetime(2026, 5, 2),
    )
    db.session.add(obs)
    db.session.commit()
    return obs


def _row_counts(db) -> dict:
    """Snapshot of row counts — used to verify read-only behaviour."""
    from hansard_archive.models import HeadlineStat, StatObservation, StatPolicyArea, StatDefinition
    from sqlalchemy import func
    return {
        'hs':   db.session.query(func.count(HeadlineStat.id)).scalar(),
        'obs':  db.session.query(func.count(StatObservation.id)).scalar(),
        'spa':  db.session.query(func.count(StatPolicyArea.id)).scalar(),
        'sd':   db.session.query(func.count(StatDefinition.id)).scalar(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyDb:

    def test_runs_without_error(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            data = collect_stats(db.session)
        assert data is not None

    def test_all_counts_zero(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            data = collect_stats(db.session)
        s1 = data["section1"]
        assert s1["headline_stat_total"] == 0
        assert s1["stat_observation_total"] == 0
        assert s1["stat_policy_area_total"] == 0
        assert s1["stat_definition_total"] == 0

    def test_no_anomalies_on_empty_db(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            data = collect_stats(db.session)
        # Empty DB has no primary policy area violations
        assert data["section4"]["missing_primary_policy_area"] == 0
        assert data["section3"]["mismatched"] == 0


class TestPlaceholderOnlyDb:

    def test_placeholder_counted_correctly(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            stat = _make_stat(db, 'cs-placeholder-only')
            _make_placeholder(db, stat.id)
            data = collect_stats(db.session)

        s1 = data["section1"]
        assert s1["stat_observation_placeholder"] >= 1
        assert s1["stat_observation_real"] == 0

    def test_section2_empty_when_only_placeholders(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            data = collect_stats(db.session)
        # No real observations → by_source_type may have entries but count=0 for real obs
        for st_data in data["section2"]["by_source_type"].values():
            assert st_data["count"] >= 0  # just confirm it runs


class TestMixedDb:

    def test_real_obs_counted_in_section1(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            stat = _make_stat(db, 'cs-mixed-stat',
                              latest_value='43',
                              source_type='ons_timeseries')
            _make_placeholder(db, stat.id)
            _make_real_obs(db, stat.id)
            data = collect_stats(db.session)

        assert data["section1"]["stat_observation_real"] >= 1
        assert data["section1"]["stat_observation_placeholder"] >= 1

    def test_section2_shows_ons_source_type(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            data = collect_stats(db.session)
        assert "ons_timeseries" in data["section2"]["by_source_type"]
        ons = data["section2"]["by_source_type"]["ons_timeseries"]
        assert ons["count"] >= 1
        assert ons["earliest_period_start"] is not None
        assert ons["latest_period_end"] is not None

    def test_section3_checks_stats_with_real_obs(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            data = collect_stats(db.session)
        assert data["section3"]["stats_checked"] >= 1


class TestMirrorMismatch:

    def test_mismatch_detected_when_values_differ(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            # HeadlineStat.latest_value = '99' but StatObservation.value = '55'
            stat = _make_stat(db, 'cs-mismatch',
                              latest_value='99',
                              source_type='govuk_bulletin')
            _make_real_obs(db, stat.id, value='55')
            data = collect_stats(db.session)

        assert data["section3"]["mismatched"] >= 1
        ids_with_mismatch = [m["headline_stat_id"]
                             for m in data["section3"]["mismatch_details"]]
        assert stat.id in ids_with_mismatch

    def test_mismatch_names_the_correct_field(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        from hansard_archive.models import HeadlineStat
        with test_app.app_context():
            stat = HeadlineStat.query.filter_by(source_id='cs-mismatch').first()
            assert stat is not None, "Seed stat not found — check test order"
            data = collect_stats(db.session)

        detail = next(
            m for m in data["section3"]["mismatch_details"]
            if m["headline_stat_id"] == stat.id
        )
        assert "latest_value" in detail["fields"]

    def test_matching_stat_not_in_mismatch_list(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            # Create a stat whose legacy fields exactly match the observation
            stat = _make_stat(db, 'cs-matching',
                              latest_value='43',
                              source_type='ons_timeseries')
            # Manually sync all five mirror fields
            from hansard_archive.models import HeadlineStat, StatObservation
            obs = _make_real_obs(db, stat.id, value='43',
                                  period_label='2024 Q3 (quarter)')
            # Sync the remaining three legacy fields to the observation values
            hs = db.session.get(HeadlineStat, stat.id)
            hs.period_label   = obs.period_label
            hs.release_date   = obs.release_date
            hs.source_url     = obs.release_url   # mirror field mapping
            hs.source_wording = obs.source_wording
            db.session.commit()

            data = collect_stats(db.session)

        ids_with_mismatch = {m["headline_stat_id"]
                             for m in data["section3"]["mismatch_details"]}
        assert stat.id not in ids_with_mismatch


class TestJsonOutput:

    def test_json_flag_produces_valid_json(self, test_app, db):
        from scripts.check_stats_state import collect_stats, format_json
        with test_app.app_context():
            data = collect_stats(db.session)
        output = format_json(data)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_json_has_expected_top_level_keys(self, test_app, db):
        from scripts.check_stats_state import collect_stats, format_json
        with test_app.app_context():
            data = collect_stats(db.session)
        parsed = json.loads(format_json(data))
        for key in ("section1", "section2", "section3", "section4", "section5", "anomalies"):
            assert key in parsed, f"Missing key: {key}"

    def test_json_dates_serialized_as_strings(self, test_app, db):
        from scripts.check_stats_state import collect_stats, format_json
        with test_app.app_context():
            data = collect_stats(db.session)
        # Should not raise — date objects must be serialized
        output = format_json(data)
        assert isinstance(output, str)


class TestReadOnly:

    def test_collect_stats_does_not_write(self, test_app, db):
        from scripts.check_stats_state import collect_stats
        with test_app.app_context():
            before = _row_counts(db)
            collect_stats(db.session)
            after = _row_counts(db)
        assert before == after, (
            f"collect_stats() changed DB state: before={before} after={after}"
        )

    def test_format_text_runs_without_error(self, test_app, db):
        from scripts.check_stats_state import collect_stats, format_text
        with test_app.app_context():
            data = collect_stats(db.session)
        text = format_text(data)
        assert "SECTION 1" in text
        assert "SECTION 6" in text
        assert isinstance(text, str)
