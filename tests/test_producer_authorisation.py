"""
Tests for producer-level authorisation UI — Phase 1.7 gap fill.

Covers:
- StatProducerAuthLog model: creation, FK, append-only shape
- Authorise route: status transition, reviewed_by/at set, audit log written
- Decline route: status transition, reason stored, audit log written
- Non-candidate guard: authorising an already-authorised producer is a no-op
- Admin authentication guard: unauthenticated requests redirect

Run: python -m pytest tests/test_producer_authorisation.py -v
"""

import pytest
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# App fixture — uses the real flask_app with in-memory SQLite so all admin
# routes are available without re-registering them manually.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    import os
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("SKIP_MIGRATIONS", "0")

    import flask_app as _fa
    _fa.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    _fa.app.config["TESTING"] = True
    _fa.app.config["LOGIN_DISABLED"] = True

    from extensions import db as _db
    with _fa.app.app_context():
        _db.create_all()

    return _fa.app


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helper: seed a candidate producer
# ---------------------------------------------------------------------------

def _seed_producer(app, slug="test-producer", auth_status="candidate"):
    from hansard_archive.models import StatProducer
    from extensions import db as _db
    with app.app_context():
        existing = StatProducer.query.filter_by(slug=slug).first()
        if existing:
            _db.session.delete(existing)
            _db.session.commit()
        p = StatProducer(
            slug=slug,
            name="Test Producer",
            producer_type="central_department",
            web_root_url="https://example.gov.uk/",
            licence="OGL_v3",
            licence_evidence_raw_url="https://example.gov.uk/copyright",
            authorisation_status=auth_status,
        )
        _db.session.add(p)
        _db.session.commit()
        return p.id


def _authenticated_client(client):
    """Open a session that passes the admin_authenticated check."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["admin_email"] = "test-admin@example.com"
    return client


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestStatProducerAuthLogModel:
    def test_create_auth_log_entry(self, app):
        from hansard_archive.models import StatProducerAuthLog
        from extensions import db as _db
        producer_id = _seed_producer(app, slug="model-test-producer")
        with app.app_context():
            entry = StatProducerAuthLog(
                producer_id=producer_id,
                changed_at=datetime.utcnow(),
                old_status="candidate",
                new_status="authorised",
                recorded_by="tester",
            )
            _db.session.add(entry)
            _db.session.commit()
            fetched = StatProducerAuthLog.query.filter_by(producer_id=producer_id).first()
            assert fetched is not None
            assert fetched.old_status == "candidate"
            assert fetched.new_status == "authorised"
            assert fetched.recorded_by == "tester"

    def test_auth_log_has_no_update_or_delete_methods(self, app):
        from hansard_archive.models import StatProducerAuthLog
        assert not hasattr(StatProducerAuthLog, "update")
        assert not hasattr(StatProducerAuthLog, "delete")

    def test_auth_log_cascade_delete_with_producer(self, app):
        from hansard_archive.models import StatProducer, StatProducerAuthLog
        from extensions import db as _db
        producer_id = _seed_producer(app, slug="cascade-test-producer")
        with app.app_context():
            entry = StatProducerAuthLog(
                producer_id=producer_id,
                changed_at=datetime.utcnow(),
                old_status="candidate",
                new_status="authorised",
                recorded_by="tester",
            )
            _db.session.add(entry)
            _db.session.commit()
            log_id = entry.id
            producer = StatProducer.query.get(producer_id)
            _db.session.delete(producer)
            _db.session.commit()
            assert StatProducerAuthLog.query.get(log_id) is None


# ---------------------------------------------------------------------------
# Route tests — authentication guard
# ---------------------------------------------------------------------------

class TestAdminAuthGuard:
    def test_authorise_requires_admin_session(self, app, client):
        producer_id = _seed_producer(app, slug="auth-guard-producer")
        resp = client.post(f"/admin/producers/{producer_id}/authorise")
        assert resp.status_code == 302
        assert "/admin" in resp.headers["Location"]

    def test_decline_requires_admin_session(self, app, client):
        producer_id = _seed_producer(app, slug="auth-guard-producer-2")
        resp = client.post(f"/admin/producers/{producer_id}/decline")
        assert resp.status_code == 302
        assert "/admin" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Route tests — authorise action
# ---------------------------------------------------------------------------

class TestAuthoriseRoute:
    def test_authorise_candidate_sets_status(self, app, client):
        from hansard_archive.models import StatProducer
        producer_id = _seed_producer(app, slug="authorise-test-1")
        _authenticated_client(client)
        resp = client.post(f"/admin/producers/{producer_id}/authorise")
        assert resp.status_code == 302
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.authorisation_status == "authorised"

    def test_authorise_sets_reviewed_by_and_at(self, app, client):
        from hansard_archive.models import StatProducer
        producer_id = _seed_producer(app, slug="authorise-test-2")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/authorise")
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.reviewed_by == "test-admin@example.com"
            assert p.reviewed_at is not None

    def test_authorise_writes_audit_log(self, app, client):
        from hansard_archive.models import StatProducerAuthLog
        producer_id = _seed_producer(app, slug="authorise-test-3")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/authorise")
        with app.app_context():
            log = StatProducerAuthLog.query.filter_by(producer_id=producer_id).first()
            assert log is not None
            assert log.old_status == "candidate"
            assert log.new_status == "authorised"
            assert log.recorded_by == "test-admin@example.com"

    def test_authorise_non_candidate_is_noop(self, app, client):
        from hansard_archive.models import StatProducer, StatProducerAuthLog
        producer_id = _seed_producer(app, slug="authorise-noop-test", auth_status="authorised")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/authorise")
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.authorisation_status == "authorised"
            assert StatProducerAuthLog.query.filter_by(producer_id=producer_id).count() == 0


# ---------------------------------------------------------------------------
# Route tests — decline action
# ---------------------------------------------------------------------------

class TestDeclineRoute:
    def test_decline_candidate_sets_status(self, app, client):
        from hansard_archive.models import StatProducer
        producer_id = _seed_producer(app, slug="decline-test-1")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/decline",
                    data={"decline_reason": "Not needed yet"})
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.authorisation_status == "declined"

    def test_decline_stores_reason(self, app, client):
        from hansard_archive.models import StatProducer
        producer_id = _seed_producer(app, slug="decline-test-2")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/decline",
                    data={"decline_reason": "Licence unclear"})
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.authorisation_reason == "Licence unclear"

    def test_decline_writes_audit_log(self, app, client):
        from hansard_archive.models import StatProducerAuthLog
        producer_id = _seed_producer(app, slug="decline-test-3")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/decline",
                    data={"decline_reason": "Out of scope"})
        with app.app_context():
            log = StatProducerAuthLog.query.filter_by(producer_id=producer_id).first()
            assert log is not None
            assert log.old_status == "candidate"
            assert log.new_status == "declined"
            assert log.change_reason == "Out of scope"

    def test_decline_without_reason_is_valid(self, app, client):
        from hansard_archive.models import StatProducer, StatProducerAuthLog
        producer_id = _seed_producer(app, slug="decline-no-reason")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/decline", data={"decline_reason": ""})
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.authorisation_status == "declined"
            log = StatProducerAuthLog.query.filter_by(producer_id=producer_id).first()
            assert log.change_reason is None

    def test_decline_non_candidate_is_noop(self, app, client):
        from hansard_archive.models import StatProducer, StatProducerAuthLog
        producer_id = _seed_producer(app, slug="decline-noop", auth_status="authorised")
        _authenticated_client(client)
        client.post(f"/admin/producers/{producer_id}/decline",
                    data={"decline_reason": "test"})
        with app.app_context():
            p = StatProducer.query.get(producer_id)
            assert p.authorisation_status == "authorised"
            assert StatProducerAuthLog.query.filter_by(producer_id=producer_id).count() == 0
