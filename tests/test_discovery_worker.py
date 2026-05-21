"""
Tests for the discovery worker (scripts/discovery_worker.py).

Covers:
  - _pick_next_producer: returns oldest pending authorised producer, claims it
  - _pick_next_producer: ignores non-authorised producers
  - _pick_next_producer: ignores already in_progress producers
  - _pick_next_producer: returns None when no pending producers
  - _process_producer: transitions to completed on success, sets discovery_completed_at
  - _process_producer: transitions to failed on exception, records failure reason
  - _process_producer: continues gracefully after run_discovery raises
  - DISCOVERY_DRY_RUN: status transitions skipped; run_discovery still called

Run: python -m pytest tests/test_discovery_worker.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Shared fixture — module-scoped in-memory SQLite
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models
    import cache_models
    import stakeholder_directory.models
    import stakeholder_directory.ingesters.staging

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
        def _fk_on(dbapi_conn, record):
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


def _make_producer(db, slug: str, **kwargs):
    from hansard_archive.models import StatProducer
    defaults = dict(
        name=f"Producer {slug}",
        producer_type="central_department",
        web_root_url="https://www.gov.uk/",
        licence="OGL_v3",
        authorisation_status="candidate",
        licence_evidence_raw_url="https://example.com/copyright",
    )
    defaults.update(kwargs)
    p = StatProducer(slug=slug, **defaults)
    db.session.add(p)
    db.session.flush()
    return p


# ---------------------------------------------------------------------------
# _pick_next_producer
# ---------------------------------------------------------------------------

class TestPickNextProducer:

    def test_claims_oldest_pending_authorised_producer(self, test_app, db):
        """Returns the oldest pending+authorised producer and sets it in_progress."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            p = _make_producer(db, "wkr-pick-1",
                               authorisation_status="authorised",
                               discovery_status="pending")
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            assert result is not None
            assert result.slug == "wkr-pick-1"
            assert result.discovery_status == "in_progress"
            db.session.rollback()

    def test_ignores_non_authorised_producers(self, test_app, db):
        """Candidate producers are not picked up even if discovery_status=pending."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            _make_producer(db, "wkr-noauth",
                           authorisation_status="candidate",
                           discovery_status="pending")
            db.session.commit()

            # Reset any claimed ones so they don't interfere
            db.session.query(StatProducer).filter_by(
                discovery_status="in_progress").update({"discovery_status": "completed"})
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            # Only non-authorised pending producers exist now
            assert result is None
            db.session.rollback()

    def test_ignores_already_in_progress_producers(self, test_app, db):
        """in_progress producers are skipped — prevents duplicate processing."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            _make_producer(db, "wkr-inprog",
                           authorisation_status="authorised",
                           discovery_status="in_progress")
            db.session.commit()

            # Ensure no pending authorised producers remain
            db.session.query(StatProducer).filter_by(
                authorisation_status="authorised",
                discovery_status="pending",
            ).update({"discovery_status": "completed"})
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            assert result is None
            db.session.rollback()

    def test_returns_none_when_no_pending(self, test_app, db):
        """Returns None gracefully when the queue is empty."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            # Mark all as completed
            db.session.query(StatProducer).filter_by(
                authorisation_status="authorised",
                discovery_status="pending",
            ).update({"discovery_status": "completed"})
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            assert result is None
            db.session.rollback()

    def test_picks_by_id_order(self, test_app, db):
        """When multiple pending producers exist, returns the one with the lowest id."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            p1 = _make_producer(db, "wkr-order-a",
                                authorisation_status="authorised",
                                discovery_status="pending")
            p2 = _make_producer(db, "wkr-order-b",
                                authorisation_status="authorised",
                                discovery_status="pending")
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            assert result is not None
            assert result.id == min(p1.id, p2.id)
            db.session.rollback()


# ---------------------------------------------------------------------------
# _process_producer
# ---------------------------------------------------------------------------

class TestProcessProducer:

    def _make_inprogress_producer(self, db, slug):
        return _make_producer(db, slug,
                              authorisation_status="authorised",
                              discovery_status="in_progress")

    def test_transitions_to_completed_on_success(self, test_app, db):
        """On successful run_discovery, producer moves to completed and timestamp set."""
        with test_app.app_context():
            from scripts.discovery_worker import _process_producer

            p = self._make_inprogress_producer(db, "wkr-proc-success")
            db.session.commit()

            fake_summary = {"fetched": 3, "written": 3}
            with patch("hansard_archive.discovery.run_discovery", return_value=fake_summary) as mock_rd:
                _process_producer(db.session, p, gemini_key="fake-key")

            db.session.refresh(p)
            assert p.discovery_status == "completed"
            assert p.discovery_completed_at is not None
            assert p.discovery_failure_reason is None
            mock_rd.assert_called_once_with(p, db.session, "fake-key")
            db.session.rollback()

    def test_transitions_to_failed_on_exception(self, test_app, db):
        """On run_discovery exception, producer moves to failed with the reason recorded."""
        with test_app.app_context():
            from scripts.discovery_worker import _process_producer

            p = self._make_inprogress_producer(db, "wkr-proc-fail")
            db.session.commit()

            with patch("hansard_archive.discovery.run_discovery",
                       side_effect=RuntimeError("Strategy not implemented")):
                _process_producer(db.session, p, gemini_key="fake-key")

            db.session.refresh(p)
            assert p.discovery_status == "failed"
            assert "Strategy not implemented" in p.discovery_failure_reason
            assert p.discovery_completed_at is None
            db.session.rollback()

    def test_failure_reason_truncated_to_1000_chars(self, test_app, db):
        """Failure reason is capped at 1000 characters to avoid oversized DB rows."""
        with test_app.app_context():
            from scripts.discovery_worker import _process_producer

            p = self._make_inprogress_producer(db, "wkr-proc-longfail")
            db.session.commit()

            long_msg = "X" * 2000
            with patch("hansard_archive.discovery.run_discovery",
                       side_effect=RuntimeError(long_msg)):
                _process_producer(db.session, p, gemini_key="fake-key")

            db.session.refresh(p)
            assert len(p.discovery_failure_reason) <= 1000
            db.session.rollback()

    def test_worker_continues_after_single_failure(self, test_app, db):
        """A failed producer does not abort subsequent processing of the next producer."""
        with test_app.app_context():
            from scripts.discovery_worker import _process_producer

            p_fail = self._make_inprogress_producer(db, "wkr-continue-fail")
            p_ok   = self._make_inprogress_producer(db, "wkr-continue-ok")
            db.session.commit()

            call_count = {"n": 0}

            def selective_run(producer, session, key):
                call_count["n"] += 1
                if producer.slug == "wkr-continue-fail":
                    raise ValueError("deliberate failure")
                return {"fetched": 1, "written": 1}

            with patch("hansard_archive.discovery.run_discovery", side_effect=selective_run):
                _process_producer(db.session, p_fail, gemini_key="k")
                _process_producer(db.session, p_ok,   gemini_key="k")

            db.session.refresh(p_fail)
            db.session.refresh(p_ok)
            assert p_fail.discovery_status == "failed"
            assert p_ok.discovery_status   == "completed"
            assert call_count["n"] == 2
            db.session.rollback()


# ---------------------------------------------------------------------------
# DRY_RUN mode
# ---------------------------------------------------------------------------

class TestDryRunMode:

    def test_dry_run_skips_status_transitions(self, test_app, db):
        """DISCOVERY_DRY_RUN=1 runs the skill but does not write status changes."""
        with test_app.app_context():
            from hansard_archive.models import StatProducer

            # Clear any pending producers left by earlier tests in this module
            db.session.query(StatProducer).filter_by(
                authorisation_status="authorised",
                discovery_status="pending",
            ).update({"discovery_status": "completed"})
            db.session.commit()

            p = _make_producer(db, "wkr-dryrun",
                               authorisation_status="authorised",
                               discovery_status="pending")
            db.session.commit()

            with patch("scripts.discovery_worker._DRY_RUN", True):
                from scripts import discovery_worker as dw
                result = dw._pick_next_producer(db.session, StatProducer)
                # In dry-run, _pick_next_producer returns the producer but does NOT flip status
                assert result is not None
                assert result.slug == "wkr-dryrun"
                # Status should still be 'pending' since dry-run skips the flush
                db.session.refresh(result)
                assert result.discovery_status == "pending"

            db.session.rollback()

    def test_dry_run_process_calls_run_discovery_but_skips_completion(self, test_app, db):
        """DISCOVERY_DRY_RUN=1: run_discovery is invoked but completed/failed are not written."""
        with test_app.app_context():
            p = _make_producer(db, "wkr-dryrun-proc",
                               authorisation_status="authorised",
                               discovery_status="in_progress")
            db.session.commit()

            called = {"run": False}

            def fake_run(producer, session, key):
                called["run"] = True
                return {}

            with patch("scripts.discovery_worker._DRY_RUN", True), \
                 patch("hansard_archive.discovery.run_discovery", side_effect=fake_run):
                from scripts import discovery_worker as dw
                dw._process_producer(db.session, p, gemini_key="k")

            db.session.refresh(p)
            assert called["run"] is True
            # Status not changed in dry-run
            assert p.discovery_status == "in_progress"
            db.session.rollback()
