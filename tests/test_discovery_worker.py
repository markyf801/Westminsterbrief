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

    def test_resumes_in_progress_producers(self, test_app, db):
        """in_progress producers are picked up for resumption, not skipped."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            # Ensure no other pending authorised producers remain
            db.session.query(StatProducer).filter_by(
                authorisation_status="authorised",
                discovery_status="pending",
            ).update({"discovery_status": "completed"})
            db.session.commit()

            p = _make_producer(db, "wkr-inprog-resume",
                               authorisation_status="authorised",
                               discovery_status="in_progress")
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            assert result is not None
            assert result.slug == "wkr-inprog-resume"
            # Status stays in_progress — already claimed, not re-set
            assert result.discovery_status == "in_progress"
            db.session.rollback()

    def test_returns_none_when_no_pending(self, test_app, db):
        """Returns None gracefully when the queue is empty."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            # Mark all pending AND in_progress as completed so nothing is claimable
            db.session.query(StatProducer).filter(
                StatProducer.authorisation_status == "authorised",
                StatProducer.discovery_status.in_(["pending", "in_progress"]),
            ).update({"discovery_status": "completed"}, synchronize_session=False)
            db.session.commit()

            result = _pick_next_producer(db.session, StatProducer)
            assert result is None
            db.session.rollback()

    def test_picks_by_id_order(self, test_app, db):
        """When multiple pending producers exist, returns the one with the lowest id."""
        with test_app.app_context():
            from scripts.discovery_worker import _pick_next_producer
            from hansard_archive.models import StatProducer

            # Clear any in_progress from earlier tests so they don't interfere
            db.session.query(StatProducer).filter(
                StatProducer.discovery_status.in_(["pending", "in_progress"]),
            ).update({"discovery_status": "completed"}, synchronize_session=False)
            db.session.commit()

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
# NullPool configuration
# ---------------------------------------------------------------------------

class TestNullPool:

    def test_configure_nullpool_sets_engine_to_nullpool(self):
        """_configure_nullpool replaces the cached engine with a NullPool engine."""
        from flask import Flask
        from extensions import db as _db
        from sqlalchemy.pool import NullPool
        from scripts.discovery_worker import _configure_nullpool

        app = Flask(__name__)
        app.config.update({
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SECRET_KEY": "test",
            "TESTING": True,
        })
        _db.init_app(app)

        _configure_nullpool(app, _db)

        with app.app_context():
            assert isinstance(_db.engine.pool, NullPool)


# ---------------------------------------------------------------------------
# Batch processing and resume
# ---------------------------------------------------------------------------

class TestBatchProcessing:

    def _make_candidate(self, title="Test Publication", url="https://example.com/pub"):
        from hansard_archive.discovery.strategies import CandidateItem
        return CandidateItem(
            title=title,
            url=url,
            date_hints=[],
            description_hint="A statistical publication.",
        )

    def _fake_classify_result(self, name="Test Publication"):
        return {
            "name":           name,
            "description":    "A test publication.",
            "update_cadence": "annual",
            "subject_area":   "education",
        }

    def test_candidates_processed_count_updated_after_run(self, test_app, db):
        """After a full run, candidates_processed_count equals the total fetched."""
        with test_app.app_context():
            p = _make_producer(db, "wkr-batch-count",
                               authorisation_status="authorised",
                               discovery_status="in_progress")
            db.session.commit()

            candidates = [self._make_candidate(title=f"Pub {i}", url=f"https://e.com/{i}")
                          for i in range(55)]

            classify_results = [self._fake_classify_result(name=f"Publication {i}")
                                 for i in range(55)]
            classify_iter = iter(classify_results)

            with patch("hansard_archive.discovery.strategies.select_strategy") as mock_strat, \
                 patch("hansard_archive.discovery.classifier.classify_candidate",
                       side_effect=lambda *a, **kw: next(classify_iter)):
                mock_strat.return_value.fetch_candidates.return_value = candidates
                from hansard_archive.discovery import run_discovery
                summary = run_discovery(p, db.session, gemini_key="fake")

            db.session.refresh(p)
            assert p.candidates_processed_count == 55
            assert summary["written"] == 55
            assert summary["fetched"] == 55
            db.session.rollback()

    def test_resume_skips_already_processed_candidates(self, test_app, db):
        """When candidates_processed_count=50, the first 50 candidates are not passed to the LLM."""
        with test_app.app_context():
            p = _make_producer(db, "wkr-batch-resume",
                               authorisation_status="authorised",
                               discovery_status="in_progress",
                               candidates_processed_count=50)
            db.session.commit()

            candidates = [self._make_candidate(title=f"Pub {i}", url=f"https://e.com/r{i}")
                          for i in range(100)]

            classify_call_count = {"n": 0}

            def counting_classify(cand_dict, producer, key):
                classify_call_count["n"] += 1
                return self._fake_classify_result(name=f"Resumed pub {classify_call_count['n']}")

            with patch("hansard_archive.discovery.strategies.select_strategy") as mock_strat, \
                 patch("hansard_archive.discovery.classifier.classify_candidate",
                       side_effect=counting_classify):
                mock_strat.return_value.fetch_candidates.return_value = candidates
                from hansard_archive.discovery import run_discovery
                summary = run_discovery(p, db.session, gemini_key="fake")

            assert classify_call_count["n"] == 50
            assert summary["resume_offset"] == 50
            assert summary["fetched"] == 100
            db.session.rollback()

    def test_invalid_update_cadence_is_nulled_not_written(self, test_app, db):
        """LLM returning an invalid update_cadence (e.g. 'biannual') must not raise — set NULL."""
        with test_app.app_context():
            from hansard_archive.models import StatPublication

            p = _make_producer(db, "wkr-bad-cadence",
                               authorisation_status="authorised",
                               discovery_status="in_progress")
            db.session.commit()

            candidates = [self._make_candidate(title="Bad Cadence Pub", url="https://e.com/bad")]

            bad_result = {
                "name":           "Bad Cadence Pub",
                "description":    "A publication.",
                "update_cadence": "biannual",  # invalid — not in ck_stat_pub_cadence
                "subject_area":   "welfare",
            }

            with patch("hansard_archive.discovery.strategies.select_strategy") as mock_strat, \
                 patch("hansard_archive.discovery.classifier.classify_candidate",
                       return_value=bad_result):
                mock_strat.return_value.fetch_candidates.return_value = candidates
                from hansard_archive.discovery import run_discovery
                summary = run_discovery(p, db.session, gemini_key="fake")

            assert summary["written"] == 1
            pub = db.session.query(StatPublication).filter_by(
                producer_id=p.id).first()
            assert pub is not None
            assert pub.update_cadence is None  # nulled, not 'biannual'
            db.session.rollback()
