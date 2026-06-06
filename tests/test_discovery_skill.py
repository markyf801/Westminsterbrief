"""
Tests for Phase 1.7 schema additions and the discovery skill.

Covers:
  - New fields on StatProducer accept valid values; reject invalid via CHECK
  - New fields on StatPublication accept valid values; reject invalid via CHECK
  - StatPublicationAuditLog: append-only, FK enforced
  - Strategy selection: right strategy per producer type
  - ManualStrategy fallback raises RuntimeError
  - OnsApiStrategy, GovUkSearchStrategy, DirectPageParserStrategy — mock HTTP
  - LLM classifier: valid candidate, is_publication=false, invalid JSON, 3-retry failure
  - DB write: slug generated, duplicate skipped (idempotent), candidate row inserted
  - Migration: upgrade/downgrade round-trip

Run: python -m pytest tests/test_discovery_skill.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Shared fixtures
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
# Schema: StatProducer new fields
# ---------------------------------------------------------------------------

class TestStatProducerPhase17Fields:

    def test_default_discovery_status_is_pending(self, test_app, db):
        with test_app.app_context():
            p = _make_producer(db, "schema-test-pending")
            assert p.discovery_status == "pending"
            assert p.discovery_completed_at is None
            assert p.discovery_failure_reason is None
            db.session.rollback()

    def test_discovery_status_valid_values(self, test_app, db):
        with test_app.app_context():
            for status in ("pending", "in_progress", "completed", "failed"):
                p = _make_producer(db, f"schema-ds-{status}", discovery_status=status)
                assert p.discovery_status == status
            db.session.rollback()

    def test_discovery_status_invalid_value_raises(self, test_app, db):
        import pytest
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            # Don't use _make_producer here — it flushes, which would fire the
            # CHECK constraint before we enter pytest.raises.
            p = StatProducer(
                slug="schema-ds-bad",
                name="Producer schema-ds-bad",
                producer_type="central_department",
                web_root_url="https://www.gov.uk/",
                licence="OGL_v3",
                authorisation_status="candidate",
                licence_evidence_raw_url="https://example.com/copyright",
                discovery_status="unknown_value",
            )
            db.session.add(p)
            with pytest.raises(IntegrityError):
                db.session.flush()
            db.session.rollback()


# ---------------------------------------------------------------------------
# Schema: StatPublication new fields
# ---------------------------------------------------------------------------

class TestStatPublicationPhase17Fields:

    def test_default_authorisation_status_is_candidate(self, test_app, db):
        with test_app.app_context():
            producer = _make_producer(db, "pub-schema-prod")
            from hansard_archive.models import StatPublication
            pub = StatPublication(
                producer_id=producer.id,
                slug="test-pub",
                name="Test Publication",
                url="https://example.com/test",
            )
            db.session.add(pub)
            db.session.flush()
            assert pub.authorisation_status == "candidate"
            assert pub.subject_area is None
            assert pub.discovered_at is None
            db.session.rollback()

    def test_authorisation_status_valid_values(self, test_app, db):
        with test_app.app_context():
            producer = _make_producer(db, "pub-auth-status-prod")
            from hansard_archive.models import StatPublication
            for i, status in enumerate(
                ("candidate", "under_review", "authorised", "declined", "paused")
            ):
                pub = StatPublication(
                    producer_id=producer.id,
                    slug=f"auth-status-{i}",
                    name=f"Pub {i}",
                    url="https://example.com/",
                    authorisation_status=status,
                )
                db.session.add(pub)
                db.session.flush()
                assert pub.authorisation_status == status
            db.session.rollback()

    def test_authorisation_status_invalid_raises(self, test_app, db):
        import pytest
        from sqlalchemy.exc import IntegrityError
        with test_app.app_context():
            producer = _make_producer(db, "pub-auth-bad-prod")
            from hansard_archive.models import StatPublication
            pub = StatPublication(
                producer_id=producer.id,
                slug="bad-status",
                name="Bad",
                url="https://example.com/",
                authorisation_status="not_valid",
            )
            db.session.add(pub)
            with pytest.raises(IntegrityError):
                db.session.flush()
            db.session.rollback()


# ---------------------------------------------------------------------------
# Schema: StatPublicationAuditLog
# ---------------------------------------------------------------------------

class TestStatPublicationAuditLog:

    def test_fk_enforced(self, test_app, db):
        import pytest
        from sqlalchemy.exc import IntegrityError
        with test_app.app_context():
            from hansard_archive.models import StatPublicationAuditLog
            entry = StatPublicationAuditLog(
                publication_id=999999,  # non-existent
                old_status="candidate",
                new_status="authorised",
                recorded_by="test",
            )
            db.session.add(entry)
            with pytest.raises(IntegrityError):
                db.session.flush()
            db.session.rollback()

    def test_no_update_or_delete_methods(self, test_app, db):
        """Append-only enforcement: model must not expose update/delete."""
        from hansard_archive.models import StatPublicationAuditLog
        assert not hasattr(StatPublicationAuditLog, "update")
        assert not hasattr(StatPublicationAuditLog, "delete")

    def test_audit_entry_created_on_status_change(self, test_app, db):
        with test_app.app_context():
            producer = _make_producer(db, "audit-pub-prod")
            from hansard_archive.models import StatPublication, StatPublicationAuditLog
            pub = StatPublication(
                producer_id=producer.id,
                slug="audit-test-pub",
                name="Audit Test",
                url="https://example.com/",
                authorisation_status="candidate",
            )
            db.session.add(pub)
            db.session.flush()
            pub_id = pub.id

            initial_count = (
                db.session.query(StatPublicationAuditLog)
                .filter_by(publication_id=pub_id)
                .count()
            )
            pub._recorded_by = "test@example.com"
            pub.authorisation_status = "authorised"
            db.session.flush()

            entries = (
                db.session.query(StatPublicationAuditLog)
                .filter_by(publication_id=pub_id)
                .order_by(StatPublicationAuditLog.id)
                .all()
            )
            assert len(entries) == initial_count + 1
            last = entries[-1]
            assert last.old_status == "candidate"
            assert last.new_status == "authorised"
            assert last.recorded_by == "test@example.com"
            db.session.rollback()

    def test_no_audit_entry_for_unrelated_field_change(self, test_app, db):
        with test_app.app_context():
            producer = _make_producer(db, "audit-no-entry-prod")
            from hansard_archive.models import StatPublication, StatPublicationAuditLog
            pub = StatPublication(
                producer_id=producer.id,
                slug="no-audit-pub",
                name="No Audit",
                url="https://example.com/",
            )
            db.session.add(pub)
            db.session.flush()
            pub_id = pub.id

            initial_count = (
                db.session.query(StatPublicationAuditLog)
                .filter_by(publication_id=pub_id)
                .count()
            )
            # Changing description should NOT create audit entry
            pub.description = "Updated description"
            db.session.flush()

            final_count = (
                db.session.query(StatPublicationAuditLog)
                .filter_by(publication_id=pub_id)
                .count()
            )
            assert final_count == initial_count
            db.session.rollback()


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

class TestStrategySelection:

    def _mock_producer(self, slug="generic", producer_type="central_department",
                       web_root_url="https://www.gov.uk/"):
        p = MagicMock()
        p.slug = slug
        p.producer_type = producer_type
        p.web_root_url = web_root_url
        return p

    def test_ons_strategy_for_ons_producer(self):
        from hansard_archive.discovery.strategies import select_strategy, OnsApiStrategy
        p = self._mock_producer(slug="office-for-national-statistics")
        assert isinstance(select_strategy(p), OnsApiStrategy)

    def test_govuk_strategy_for_central_department(self):
        from hansard_archive.discovery.strategies import select_strategy, GovUkSearchStrategy
        p = self._mock_producer(slug="dept-for-education", producer_type="central_department")
        assert isinstance(select_strategy(p), GovUkSearchStrategy)

    def test_direct_page_strategy_for_non_govuk(self):
        from hansard_archive.discovery.strategies import select_strategy, DirectPageParserStrategy
        p = self._mock_producer(
            slug="ucas",
            producer_type="other_public_body",
            web_root_url="https://www.ucas.com/",
        )
        assert isinstance(select_strategy(p), DirectPageParserStrategy)

    def test_manual_strategy_fallback(self):
        """ManualStrategy always claims it can handle; always raises on fetch."""
        from hansard_archive.discovery.strategies import ManualStrategy
        strategy = ManualStrategy()
        p = self._mock_producer()
        assert strategy.can_handle(p)
        with pytest.raises(RuntimeError, match="No automated discovery strategy"):
            strategy.fetch_candidates(p)


# ---------------------------------------------------------------------------
# Strategy fetch_candidates — mock HTTP
# ---------------------------------------------------------------------------

class TestOnsApiStrategy:

    def test_returns_candidate_items(self):
        from hansard_archive.discovery.strategies import OnsApiStrategy, CandidateItem
        strategy = OnsApiStrategy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "items": [
                {
                    "title": "Consumer Price Inflation",
                    "uri": "/economy/inflation/bulletins/consumerpriceinflation/latest",
                    "description": "Monthly measure of inflation.",
                    "links": {"latest_version": {"href": "https://api.beta.ons.gov.uk/v1/data/inflation"}},
                },
            ]
        }
        with patch("requests.get", return_value=mock_resp):
            p = MagicMock()
            p.slug = "office-for-national-statistics"
            items = strategy.fetch_candidates(p)
        assert len(items) == 1
        assert items[0].title == "Consumer Price Inflation"
        assert isinstance(items[0], CandidateItem)

    def test_raises_on_request_failure(self):
        # Contract (corrected 5 Jun 2026): a fetch FAILURE must raise
        # StrategyFetchError, not swallow to [] — a swallowed failure is
        # indistinguishable from a genuine empty result and silently marks the
        # producer completed/re-discovered while blind. A genuine empty (HTTP 200,
        # no items) still returns []; only a hard failure raises.
        from hansard_archive.discovery.strategies import OnsApiStrategy, StrategyFetchError
        import requests as req_mod
        import pytest
        strategy = OnsApiStrategy()
        with patch("requests.get", side_effect=req_mod.RequestException("timeout")):
            p = MagicMock()
            p.slug = "office-for-national-statistics"
            with pytest.raises(StrategyFetchError):
                strategy.fetch_candidates(p)


class TestGovUkSearchStrategy:

    def test_returns_candidate_items(self):
        from hansard_archive.discovery.strategies import GovUkSearchStrategy, CandidateItem
        strategy = GovUkSearchStrategy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"title": "Schools Census", "link": "/government/statistics/schools-census",
                 "description": "Annual school census", "public_timestamp": "2026-01-15"}
            ]
        }
        p = MagicMock()
        p.slug = "department-for-education"
        p.producer_type = "central_department"
        p.web_root_url = "https://www.gov.uk/government/organisations/department-for-education"
        with patch("requests.get", return_value=mock_resp):
            items = strategy.fetch_candidates(p)
        assert len(items) == 1
        assert items[0].title == "Schools Census"
        assert items[0].url.startswith("https://www.gov.uk")
        assert isinstance(items[0], CandidateItem)

    def test_sends_order_newest_param(self):
        """API call must include order=newest so most-recent release is stored on first write."""
        from hansard_archive.discovery.strategies import GovUkSearchStrategy
        strategy = GovUkSearchStrategy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        p = MagicMock()
        p.slug = "department-for-education"
        p.producer_type = "central_department"
        p.web_root_url = "https://www.gov.uk/"
        with patch("requests.get", return_value=mock_resp) as mock_get:
            strategy.fetch_candidates(p)
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["order"] == "newest"

    def test_paginates_when_full_page(self):
        from hansard_archive.discovery.strategies import GovUkSearchStrategy
        full_page  = {"results": [{"title": f"Pub {i}", "link": f"/pub{i}"} for i in range(100)]}
        empty_page = {"results": []}
        responses  = [MagicMock(json=MagicMock(return_value=full_page)),
                      MagicMock(json=MagicMock(return_value=empty_page))]
        p = MagicMock()
        p.slug = "some-dept"
        p.producer_type = "central_department"
        p.web_root_url = "https://www.gov.uk/"
        strategy = GovUkSearchStrategy()
        with patch("requests.get", side_effect=responses):
            items = strategy.fetch_candidates(p)
        assert len(items) == 100


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

class TestClassifier:

    def _mock_gemini_response(self, payload: dict) -> MagicMock:
        import json
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]
        }
        return mock_resp

    def _producer(self):
        p = MagicMock()
        p.name = "Test Producer"
        p.producer_type = "central_department"
        p.web_root_url = "https://example.com/"
        return p

    def _candidate(self):
        return {
            "title": "Annual Schools Census",
            "url": "https://example.com/schools-census",
            "date_hints": ["2026"],
            "description_hint": "Annual data on schools and pupils.",
        }

    def test_valid_publication_returns_result(self):
        from hansard_archive.discovery.classifier import classify_candidate
        result_payload = {
            "is_publication": True,
            "name": "Annual Schools Census",
            "description": "Annual data on schools and pupils in England.",
            "update_cadence": "annual",
            "subject_area": "Education",
        }
        mock_resp = self._mock_gemini_response(result_payload)
        with patch("requests.post", return_value=mock_resp):
            result = classify_candidate(self._candidate(), self._producer(), "fake-key")
        assert result is not None
        assert result["name"] == "Annual Schools Census"
        assert result["update_cadence"] == "annual"

    def test_is_publication_false_returns_none(self):
        from hansard_archive.discovery.classifier import classify_candidate
        result_payload = {"is_publication": False, "reason": "This is a press release."}
        mock_resp = self._mock_gemini_response(result_payload)
        with patch("requests.post", return_value=mock_resp):
            result = classify_candidate(self._candidate(), self._producer(), "fake-key")
        assert result is None

    def test_invalid_json_returns_none(self):
        from hansard_archive.discovery.classifier import classify_candidate
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "not-json-at-all"}]}}]
        }
        with patch("requests.post", return_value=mock_resp):
            result = classify_candidate(self._candidate(), self._producer(), "fake-key")
        assert result is None

    def test_all_retries_failed_returns_none(self):
        from hansard_archive.discovery.classifier import classify_candidate
        import requests as req_mod
        with patch("requests.post", side_effect=req_mod.RequestException("timeout")):
            with patch("time.sleep"):   # don't actually sleep in tests
                result = classify_candidate(self._candidate(), self._producer(), "fake-key")
        assert result is None

    def test_string_null_cadence_normalised_to_none(self):
        """Gemini sometimes returns the string 'null' for update_cadence instead of
        JSON null. The classifier must normalise this to Python None so it doesn't
        violate the ha_stat_publication ck_stat_pub_cadence CHECK constraint."""
        from hansard_archive.discovery.classifier import classify_candidate
        result_payload = {
            "is_publication": True,
            "name": "Some Statistics",
            "description": "Some description.",
            "update_cadence": "null",   # string "null" from LLM — the bug case
            "subject_area": "Economy",
        }
        mock_resp = self._mock_gemini_response(result_payload)
        with patch("requests.post", return_value=mock_resp):
            result = classify_candidate(self._candidate(), self._producer(), "fake-key")
        assert result is not None
        assert result["update_cadence"] is None   # must be None, not the string "null"

    def test_string_null_cadence_is_db_writable(self, test_app, db):
        """A classifier result with update_cadence=None must be writable to
        ha_stat_publication without raising CheckViolation."""
        with test_app.app_context():
            producer = _make_producer(db, "cadence-null-prod",
                                      authorisation_status="authorised")
            from hansard_archive.models import StatPublication
            from datetime import datetime
            pub = StatPublication(
                producer_id=producer.id,
                slug="null-cadence-pub",
                name="Null Cadence Publication",
                url="https://example.com/null-cadence",
                update_cadence=None,      # must not raise ck_stat_pub_cadence
                authorisation_status="candidate",
                discovered_at=datetime.utcnow(),
            )
            db.session.add(pub)
            db.session.flush()   # would raise CheckViolation if "null" string slipped through
            assert pub.update_cadence is None
            db.session.rollback()

    def test_missing_required_field_returns_none(self):
        from hansard_archive.discovery.classifier import classify_candidate
        result_payload = {
            "is_publication": True,
            "name": "",          # required, but empty
            "description": "Desc",
        }
        mock_resp = self._mock_gemini_response(result_payload)
        with patch("requests.post", return_value=mock_resp):
            result = classify_candidate(self._candidate(), self._producer(), "fake-key")
        assert result is None


# ---------------------------------------------------------------------------
# DB write (run_discovery integration — mock both HTTP and LLM)
# ---------------------------------------------------------------------------

class TestRunDiscovery:

    def _make_auth_producer(self, db, slug: str):
        return _make_producer(db, slug, authorisation_status="authorised",
                              discovery_status="pending")

    def test_writes_candidate_row(self, test_app, db):
        with test_app.app_context():
            p = self._make_auth_producer(db, "run-disc-write")
            from hansard_archive.models import StatPublication
            from hansard_archive.discovery.strategies import CandidateItem

            candidates = [CandidateItem(
                title="Schools Census",
                url="https://example.com/schools-census",
            )]
            lm_result = {
                "is_publication": True,
                "name": "Schools Census",
                "description": "Annual schools data.",
                "update_cadence": "annual",
                "subject_area": "Education",
            }
            with patch("hansard_archive.discovery.strategies.OnsApiStrategy.can_handle",
                       return_value=False), \
                 patch("hansard_archive.discovery.strategies.GovUkSearchStrategy.can_handle",
                       return_value=True), \
                 patch("hansard_archive.discovery.strategies.GovUkSearchStrategy.fetch_candidates",
                       return_value=candidates), \
                 patch("hansard_archive.discovery.classifier.classify_candidate",
                       return_value=lm_result):
                from hansard_archive.discovery import run_discovery
                summary = run_discovery(p, db.session, "fake-key")

            assert summary["written"] == 1
            assert summary["skipped"] == 0
            pub = db.session.query(StatPublication).filter_by(producer_id=p.id).first()
            assert pub is not None
            assert pub.authorisation_status == "candidate"
            assert pub.name == "Schools Census"
            assert pub.discovered_at is not None
            db.session.rollback()

    def test_idempotent_on_duplicate_slug(self, test_app, db):
        with test_app.app_context():
            p = self._make_auth_producer(db, "run-disc-idem")
            from hansard_archive.models import StatPublication
            from hansard_archive.discovery.strategies import CandidateItem
            from datetime import datetime

            # Pre-existing row
            existing = StatPublication(
                producer_id=p.id, slug="schools-census",
                name="Schools Census", url="https://example.com/",
                authorisation_status="candidate", discovered_at=datetime.utcnow(),
            )
            db.session.add(existing)
            db.session.flush()

            candidates = [CandidateItem(title="Schools Census", url="https://example.com/")]
            lm_result = {
                "is_publication": True, "name": "Schools Census",
                "description": "Desc.", "update_cadence": "annual",
            }
            with patch("hansard_archive.discovery.strategies.OnsApiStrategy.can_handle",
                       return_value=False), \
                 patch("hansard_archive.discovery.strategies.GovUkSearchStrategy.can_handle",
                       return_value=True), \
                 patch("hansard_archive.discovery.strategies.GovUkSearchStrategy.fetch_candidates",
                       return_value=candidates), \
                 patch("hansard_archive.discovery.classifier.classify_candidate",
                       return_value=lm_result):
                from hansard_archive.discovery import run_discovery
                summary = run_discovery(p, db.session, "fake-key")

            assert summary["written"] == 0
            assert summary["skipped"] == 1
            db.session.rollback()


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------

class TestPhase17Migration:

    def test_upgrade_creates_audit_log_table(self):
        from sqlalchemy import create_engine, inspect, text
        engine = create_engine("sqlite:///:memory:")
        # First apply Phase 1.6 so prerequisite tables exist
        from migrations.phase_1_6_source_registry import upgrade as up16
        up16(engine)
        from migrations.phase_1_7_discovery_skill import upgrade as up17
        up17(engine)
        inspector = inspect(engine)
        assert "ha_stat_publication_audit_log" in inspector.get_table_names()
        cols_producer = [c["name"] for c in inspector.get_columns("ha_stat_producer")]
        assert "discovery_status" in cols_producer
        assert "discovery_completed_at" in cols_producer
        assert "discovery_failure_reason" in cols_producer
        cols_pub = [c["name"] for c in inspector.get_columns("ha_stat_publication")]
        for col in ("authorisation_status", "subject_area", "discovered_at",
                    "authorised_at", "authorised_by", "declined_at",
                    "declined_by", "decline_reason"):
            assert col in cols_pub

    def test_downgrade_removes_audit_log_table(self):
        from sqlalchemy import create_engine, inspect
        engine = create_engine("sqlite:///:memory:")
        from migrations.phase_1_6_source_registry import upgrade as up16
        up16(engine)
        from migrations.phase_1_7_discovery_skill import upgrade as up17, downgrade as dn17
        up17(engine)
        dn17(engine)
        inspector = inspect(engine)
        assert "ha_stat_publication_audit_log" not in inspector.get_table_names()
        # Phase 1.6 tables must still exist
        assert "ha_stat_producer" in inspector.get_table_names()
        assert "ha_stat_publication" in inspector.get_table_names()

    def test_round_trip_upgrade_downgrade_upgrade(self):
        from sqlalchemy import create_engine, inspect
        engine = create_engine("sqlite:///:memory:")
        from migrations.phase_1_6_source_registry import upgrade as up16
        from migrations.phase_1_7_discovery_skill import upgrade as up17, downgrade as dn17
        up16(engine)
        up17(engine)
        dn17(engine)
        up17(engine)  # second upgrade should be idempotent
        inspector = inspect(engine)
        assert "ha_stat_publication_audit_log" in inspector.get_table_names()
