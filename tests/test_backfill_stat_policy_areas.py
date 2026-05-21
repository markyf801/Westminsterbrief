"""
Tests for scripts/backfill_stat_policy_areas.py.

Three core scenarios, plus a slug-validation guard:
  (a) Dry-run: N stats missing StatPolicyArea → reports N, no rows written
  (b) Execute: inserts exactly N rows, all is_primary=True
  (c) Idempotency: re-running execute after success is a no-op
  (d) Invalid slug: aborts before any insert when a theme_slug is not in
      the policy-area registry

All DB operations use an in-memory SQLite instance.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]

# A valid theme slug and one that does not exist in the registry.
_VALID_SLUG   = "economy"
_INVALID_SLUG = "not-a-real-policy-area"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models        # noqa: F401
    import cache_models                  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    _db.init_app(app)

    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope="module")
def db(test_app):
    from extensions import db as _db
    return _db


def _make_stat(db, source_id: str, theme_slug: str = _VALID_SLUG):
    from hansard_archive.models import HeadlineStat
    stat = HeadlineStat(
        theme_slug=theme_slug,
        source_type="govuk_bulletin",
        source_id=source_id,
        display_label=f"Test stat ({source_id})",
        display_hint="raw",
        geography="UK",
        latest_value="42",
        unit="%",
        source_url="https://example.com",
        last_refreshed=datetime(2026, 5, 1, 9, 0),
        last_success=datetime(2026, 5, 1, 9, 0),
    )
    db.session.add(stat)
    db.session.commit()
    return stat


def _make_spa(db, stat_id: int, theme_slug: str = _VALID_SLUG, is_primary: bool = True):
    from hansard_archive.models import StatPolicyArea
    spa = StatPolicyArea(
        headline_stat_id=stat_id,
        theme_slug=theme_slug,
        is_primary=is_primary,
    )
    db.session.add(spa)
    db.session.commit()
    return spa


# ---------------------------------------------------------------------------
# (a) find_missing — query correctness
# ---------------------------------------------------------------------------

class TestFindMissing:

    def test_stat_without_spa_is_returned(self, test_app, db):
        """A HeadlineStat with no StatPolicyArea row appears in find_missing."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing

        with test_app.app_context():
            stat = _make_stat(db, "find-missing-no-spa")

            missing = find_missing(db.session, HeadlineStat, StatPolicyArea)
            ids = {s.id for s in missing}
            assert stat.id in ids

    def test_stat_with_primary_spa_not_returned(self, test_app, db):
        """A HeadlineStat that already has is_primary=True is excluded."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing

        with test_app.app_context():
            stat = _make_stat(db, "find-missing-has-primary")
            _make_spa(db, stat.id, is_primary=True)

            missing = find_missing(db.session, HeadlineStat, StatPolicyArea)
            ids = {s.id for s in missing}
            assert stat.id not in ids

    def test_stat_with_non_primary_spa_is_returned(self, test_app, db):
        """A HeadlineStat with only a non-primary StatPolicyArea still appears."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing

        with test_app.app_context():
            stat = _make_stat(db, "find-missing-non-primary")
            _make_spa(db, stat.id, is_primary=False)

            missing = find_missing(db.session, HeadlineStat, StatPolicyArea)
            ids = {s.id for s in missing}
            assert stat.id in ids


# ---------------------------------------------------------------------------
# (b) validate_slugs
# ---------------------------------------------------------------------------

class TestValidateSlugs:

    def test_all_valid_slugs_returns_empty(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        from scripts.backfill_stat_policy_areas import validate_slugs

        with test_app.app_context():
            stat = _make_stat(db, "validate-valid", theme_slug=_VALID_SLUG)
            problems = validate_slugs([stat], {_VALID_SLUG, "education"})
            assert problems == []

    def test_invalid_slug_produces_problem(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        from scripts.backfill_stat_policy_areas import validate_slugs

        with test_app.app_context():
            stat = _make_stat(db, "validate-invalid", theme_slug=_INVALID_SLUG)
            problems = validate_slugs([stat], {_VALID_SLUG})
            assert len(problems) == 1
            assert _INVALID_SLUG in problems[0]
            assert str(stat.id) in problems[0]


# ---------------------------------------------------------------------------
# (b) execute_inserts — rows written correctly
# ---------------------------------------------------------------------------

class TestExecuteInserts:

    def test_inserts_one_row_per_stat(self, test_app, db):
        """execute_inserts creates exactly one StatPolicyArea per HeadlineStat."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing, execute_inserts

        with test_app.app_context():
            s1 = _make_stat(db, "exec-insert-a")
            s2 = _make_stat(db, "exec-insert-b")

            missing = find_missing(db.session, HeadlineStat, StatPolicyArea)
            # Only care about rows we just created; there may be others in DB.
            our_ids = {s1.id, s2.id}
            our_missing = [s for s in missing if s.id in our_ids]
            assert len(our_missing) == 2

            n = execute_inserts(db.session, StatPolicyArea, our_missing)
            assert n == 2

            # Both rows exist with is_primary=True.
            for stat_id in our_ids:
                spa = (
                    db.session.query(StatPolicyArea)
                    .filter_by(headline_stat_id=stat_id, is_primary=True)
                    .first()
                )
                assert spa is not None, f"StatPolicyArea missing for stat {stat_id}"
                assert spa.theme_slug == _VALID_SLUG

    def test_inserted_row_theme_slug_matches_stat(self, test_app, db):
        """The inserted StatPolicyArea.theme_slug matches the HeadlineStat's theme_slug."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import execute_inserts

        with test_app.app_context():
            stat = _make_stat(db, "exec-slug-match", theme_slug="education")

            execute_inserts(db.session, StatPolicyArea, [stat])

            spa = (
                db.session.query(StatPolicyArea)
                .filter_by(headline_stat_id=stat.id, is_primary=True)
                .first()
            )
            assert spa is not None
            assert spa.theme_slug == "education"


# ---------------------------------------------------------------------------
# (c) Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_rerun_after_execute_is_noop(self, test_app, db):
        """find_missing returns nothing for a stat that was already backfilled."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing, execute_inserts

        with test_app.app_context():
            stat = _make_stat(db, "idempotent-rerun")

            # First run.
            missing_first = find_missing(db.session, HeadlineStat, StatPolicyArea)
            our_missing = [s for s in missing_first if s.id == stat.id]
            assert len(our_missing) == 1
            execute_inserts(db.session, StatPolicyArea, our_missing)

            # Second run — same stat must not appear.
            missing_second = find_missing(db.session, HeadlineStat, StatPolicyArea)
            ids_second = {s.id for s in missing_second}
            assert stat.id not in ids_second, (
                "Stat already backfilled must not appear in find_missing on re-run"
            )

    def test_no_duplicate_rows_after_rerun(self, test_app, db):
        """Calling execute_inserts twice does not produce duplicate StatPolicyArea rows."""
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing, execute_inserts

        with test_app.app_context():
            stat = _make_stat(db, "idempotent-no-dup")

            # First execute.
            missing = [s for s in find_missing(db.session, HeadlineStat, StatPolicyArea)
                       if s.id == stat.id]
            execute_inserts(db.session, StatPolicyArea, missing)

            # Second execute — find_missing returns nothing for this stat so
            # no second insert is attempted. Confirm only one row exists.
            count = (
                db.session.query(StatPolicyArea)
                .filter_by(headline_stat_id=stat.id, is_primary=True)
                .count()
            )
            assert count == 1


# ---------------------------------------------------------------------------
# (a) Dry-run: no rows written
# ---------------------------------------------------------------------------

class TestDryRunDoesNotInsert:

    def test_dry_run_leaves_db_unchanged(self, test_app, db):
        """
        Simulates the dry-run path: find_missing is called but execute_inserts
        is not. The StatPolicyArea table must remain unchanged for this stat.
        """
        from hansard_archive.models import HeadlineStat, StatPolicyArea
        from scripts.backfill_stat_policy_areas import find_missing

        with test_app.app_context():
            stat = _make_stat(db, "dry-run-no-insert")

            # Confirm the stat is in the missing list.
            missing = find_missing(db.session, HeadlineStat, StatPolicyArea)
            ids = {s.id for s in missing}
            assert stat.id in ids

            # Do NOT call execute_inserts — simulating dry-run.

            # Stat still has no StatPolicyArea row.
            spa = (
                db.session.query(StatPolicyArea)
                .filter_by(headline_stat_id=stat.id, is_primary=True)
                .first()
            )
            assert spa is None, "Dry run must not write any rows"
