"""
Tests for scripts/backfill_wayback_snapshots.py.

Covers:
  - Dry-run: lists candidates, makes no Wayback calls, writes nothing
  - Execute success: updates licence_evidence_wayback_url (and raw URL when corrected)
  - Execute failure: leaves row unchanged when Wayback returns None
  - Audit log entry created on successful update (before_update hook fires)
  - Idempotent: rows already populated are skipped; second run is a no-op
  - Rows with NULL licence_evidence_raw_url (Unverified producers) are skipped
  - URL correction applied: broken raw URL replaced with working replacement

Run: python -m pytest tests/test_backfill_wayback_snapshots.py -v
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


# ---------------------------------------------------------------------------
# Shared app / db fixtures (module-scoped in-memory SQLite)
# Identical pattern to test_stats_registry.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models       # noqa: F401
    import cache_models                 # noqa: F401
    import stakeholder_directory.models # noqa: F401
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
        from sqlalchemy import event as _sa_event
        @_sa_event.listens_for(_db.engine, "connect")
        def _set_fk_pragma(dbapi_conn, record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope="module")
def db(test_app):
    from extensions import db as _db
    return _db


# ---------------------------------------------------------------------------
# Import the module under test (after sys.path is set up)
# ---------------------------------------------------------------------------

from backfill_wayback_snapshots import (  # noqa: E402
    backfill,
    _URL_CORRECTIONS,
    _lookup_wayback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_producer(db, slug: str,
                   raw_url: str | None = "https://example.com/copyright",
                   wayback_url: str | None = None) -> "StatProducer":
    from hansard_archive.models import StatProducer
    p = StatProducer(
        slug=slug,
        name=f"Test Producer {slug}",
        producer_type="central_department",
        web_root_url="https://example.com/",
        licence="OGL_v3",
        authorisation_status="candidate",
        licence_evidence_raw_url=raw_url,
        licence_evidence_wayback_url=wayback_url,
    )
    db.session.add(p)
    db.session.flush()
    return p


def _audit_entries_for(db, producer_id: int) -> list:
    from hansard_archive.models import StatLicenceAuditLog
    return (
        db.session.query(StatLicenceAuditLog)
        .filter_by(producer_id=producer_id)
        .order_by(StatLicenceAuditLog.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRun:

    def test_dry_run_makes_no_wayback_calls(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            _make_producer(db, "dry-run-producer-1")

            with patch(
                "backfill_wayback_snapshots._lookup_wayback"
            ) as mock_lookup:
                backfill(db.session, StatProducer, execute=False)
                mock_lookup.assert_not_called()

    def test_dry_run_writes_nothing_to_db(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "dry-run-producer-2")
            original_wayback = p.licence_evidence_wayback_url

            with patch("backfill_wayback_snapshots._lookup_wayback", return_value="https://web.archive.org/fake"):
                backfill(db.session, StatProducer, execute=False)

            db.session.refresh(p)
            assert p.licence_evidence_wayback_url == original_wayback

    def test_dry_run_no_op_when_no_candidates(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            # Producer already has wayback URL
            _make_producer(db, "dry-run-already-done",
                           wayback_url="https://web.archive.org/existing")

            with patch("backfill_wayback_snapshots._lookup_wayback") as mock_lookup:
                backfill(db.session, StatProducer, execute=False)
                mock_lookup.assert_not_called()


class TestExecuteSuccess:

    def test_execute_sets_wayback_url(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "exec-success-1", raw_url="https://example.com/ogl")

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value="https://web.archive.org/web/20260101/https://example.com/ogl",
            ):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            assert p.licence_evidence_wayback_url == (
                "https://web.archive.org/web/20260101/https://example.com/ogl"
            )

    def test_execute_applies_url_correction(self, test_app, db):
        broken_url = "https://www.gov.uk/help/copyright"
        corrected_url = _URL_CORRECTIONS[broken_url]

        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "exec-url-correction", raw_url=broken_url)

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value="https://web.archive.org/web/20260101/ogl",
            ):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            assert p.licence_evidence_raw_url == corrected_url
            assert p.licence_evidence_wayback_url is not None

    def test_execute_no_url_correction_when_raw_url_is_correct(self, test_app, db):
        correct_url = "https://www.gov.scot/crown-copyright/"

        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "exec-no-correction", raw_url=correct_url)

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value="https://web.archive.org/web/20260101/scot",
            ):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            assert p.licence_evidence_raw_url == correct_url  # unchanged


class TestExecuteFailure:

    def test_failure_leaves_row_unchanged(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "exec-fail-1", raw_url="https://example.com/unarchived")

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value=None,
            ):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            assert p.licence_evidence_wayback_url is None

    def test_failure_does_not_correct_raw_url(self, test_app, db):
        broken_url = "https://www.gov.uk/help/copyright"

        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "exec-fail-url-correction", raw_url=broken_url)

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value=None,
            ):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            # Raw URL must not be corrected if Wayback lookup failed
            assert p.licence_evidence_raw_url == broken_url
            assert p.licence_evidence_wayback_url is None


class TestAuditLog:

    def test_audit_entry_created_when_wayback_url_set(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "audit-wayback-set", raw_url="https://example.com/ogl2")
            producer_id = p.id
            # The after_insert hook already wrote one entry; flush any pending state
            db.session.expire_all()

            initial_count = len(_audit_entries_for(db, producer_id))

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value="https://web.archive.org/web/20260101/x",
            ):
                backfill(db.session, StatProducer, execute=True)

            entries = _audit_entries_for(db, producer_id)
            assert len(entries) == initial_count + 1, (
                "before_update hook must write one audit entry when wayback URL is set"
            )
            new_entry = entries[-1]
            assert new_entry.new_evidence_wayback_url == "https://web.archive.org/web/20260101/x"
            assert new_entry.old_evidence_wayback_url is None

    def test_audit_entry_captures_raw_url_correction(self, test_app, db):
        broken_url = "https://www.gov.uk/help/copyright"
        corrected_url = _URL_CORRECTIONS[broken_url]

        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "audit-raw-correction", raw_url=broken_url)
            producer_id = p.id
            db.session.expire_all()

            initial_count = len(_audit_entries_for(db, producer_id))

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value="https://web.archive.org/web/20260101/ogl",
            ):
                backfill(db.session, StatProducer, execute=True)

            entries = _audit_entries_for(db, producer_id)
            assert len(entries) == initial_count + 1
            new_entry = entries[-1]
            assert new_entry.old_evidence_raw_url == broken_url
            assert new_entry.new_evidence_raw_url == corrected_url

    def test_no_audit_entry_when_wayback_lookup_fails(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "audit-no-entry-on-fail", raw_url="https://example.com/x")
            producer_id = p.id
            db.session.expire_all()

            initial_count = len(_audit_entries_for(db, producer_id))

            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value=None,
            ):
                backfill(db.session, StatProducer, execute=True)

            entries = _audit_entries_for(db, producer_id)
            assert len(entries) == initial_count, (
                "No audit entry should be written when Wayback lookup fails"
            )


class TestIdempotency:

    def test_already_populated_row_is_skipped(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(
                db, "idempotent-already-done",
                raw_url="https://example.com/ogl3",
                wayback_url="https://web.archive.org/existing",
            )

            # return_value=None: safe if other test producers are still pending;
            # they'll be skipped gracefully rather than crash on MagicMock binding.
            with patch(
                "backfill_wayback_snapshots._lookup_wayback",
                return_value=None,
            ):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            assert p.licence_evidence_wayback_url == "https://web.archive.org/existing", (
                "Producer with existing wayback URL must not be overwritten"
            )

    def test_second_run_is_no_op(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = _make_producer(db, "idempotent-second-run", raw_url="https://example.com/run2")

            wayback = "https://web.archive.org/web/20260101/run2"
            with patch("backfill_wayback_snapshots._lookup_wayback", return_value=wayback):
                backfill(db.session, StatProducer, execute=True)

            db.session.refresh(p)
            assert p.licence_evidence_wayback_url == wayback

            # Second run: no Wayback calls because the row is now populated
            with patch("backfill_wayback_snapshots._lookup_wayback") as mock_lookup:
                backfill(db.session, StatProducer, execute=True)
                mock_lookup.assert_not_called()


class TestSkipNullRawUrl:

    def test_unverified_producer_with_null_raw_url_is_skipped(self, test_app, db):
        with test_app.app_context():
            from hansard_archive.models import StatProducer
            p = StatProducer(
                slug="skip-null-raw-url",
                name="Unverified Producer",
                producer_type="regulator",
                web_root_url="https://example.com/",
                licence="Unverified",
                authorisation_status="candidate",
                licence_evidence_raw_url=None,
                licence_evidence_wayback_url=None,
            )
            db.session.add(p)
            db.session.flush()

            with patch(
                "backfill_wayback_snapshots._lookup_wayback"
            ) as mock_lookup:
                backfill(db.session, StatProducer, execute=True)
                mock_lookup.assert_not_called()

            db.session.refresh(p)
            assert p.licence_evidence_wayback_url is None


class TestUrlCorrectionsMap:

    def test_gov_uk_copyright_maps_to_nationalarchives(self):
        assert "https://www.gov.uk/help/copyright" in _URL_CORRECTIONS
        assert "nationalarchives.gov.uk" in _URL_CORRECTIONS[
            "https://www.gov.uk/help/copyright"
        ]

    def test_nhs_england_url_corrected(self):
        assert "https://www.england.nhs.uk/contact-us/privacy-notice/copyright/" in _URL_CORRECTIONS

    def test_nisra_url_corrected(self):
        assert "https://www.nisra.gov.uk/contact/crown-copyright" in _URL_CORRECTIONS

    def test_welsh_gov_url_corrected(self):
        assert "https://www.llyw.cymru/copyright-statement" in _URL_CORRECTIONS

    def test_ucas_url_corrected(self):
        assert "https://www.ucas.com/about-us/terms-and-conditions" in _URL_CORRECTIONS
        assert "terms-and-conditions-for-use-of-the-ucas-network" in _URL_CORRECTIONS[
            "https://www.ucas.com/about-us/terms-and-conditions"
        ]

    def test_all_corrected_urls_are_distinct_from_originals(self):
        for old, new in _URL_CORRECTIONS.items():
            assert old != new, f"Correction maps {old!r} to itself"
