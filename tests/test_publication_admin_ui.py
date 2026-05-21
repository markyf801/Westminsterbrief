"""
Tests for the Phase 1.7 admin publication review logic.

These tests cover the business logic that the admin routes execute — status
transitions, audit log creation, and inline edit behaviour — using the same
fresh in-memory SQLite fixture pattern as the other Phase 1.7 test modules.

The HTTP layer itself (route rendering, form parsing, redirect) is intentionally
not tested here; route smoke tests live in test_routes.py.

Covers:
  - Authorise single publication: status → authorised, audit log written
  - Decline single publication: status → declined, reason stored, audit log written
  - Bulk authorise N: all transition, N audit entries created
  - Decline without reason: allowed (decline_reason=None)
  - Non-candidate publications are skipped by bulk action guard
  - Inline edit saves fields; does NOT create an audit log entry

Run: python -m pytest tests/test_publication_admin_ui.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixture — module-scoped in-memory SQLite (same pattern as other Phase 1.7 tests)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_producer(db, slug: str, **kwargs):
    from hansard_archive.models import StatProducer
    defaults = dict(
        name=f"Producer {slug}",
        producer_type="central_department",
        web_root_url="https://www.gov.uk/",
        licence="OGL_v3",
        authorisation_status="authorised",
        licence_evidence_raw_url="https://example.com/copyright",
    )
    defaults.update(kwargs)
    p = StatProducer(slug=slug, **defaults)
    db.session.add(p)
    db.session.flush()
    return p


def _make_publication(db, producer_id: int, slug: str, **kwargs):
    from hansard_archive.models import StatPublication
    defaults = dict(
        name=f"Publication {slug}",
        url="https://example.com/",
        authorisation_status="candidate",
    )
    defaults.update(kwargs)
    pub = StatPublication(producer_id=producer_id, slug=slug, **defaults)
    db.session.add(pub)
    db.session.flush()
    return pub


def _do_bulk_action(db, action: str, pub_ids: list[int],
                    admin_user: str = "test-admin@example.com",
                    decline_reason: str | None = None) -> None:
    """Simulate the core logic of the admin_publications_bulk_action route."""
    from hansard_archive.models import StatPublication
    now = datetime.utcnow()
    for pub_id in pub_ids:
        pub = StatPublication.query.get(pub_id)
        if pub is None or pub.authorisation_status != "candidate":
            continue
        pub._recorded_by   = admin_user
        pub._change_reason = decline_reason
        if action == "authorise":
            pub.authorisation_status = "authorised"
            pub.authorised_at        = now
            pub.authorised_by        = admin_user
        else:
            pub.authorisation_status = "declined"
            pub.declined_at          = now
            pub.declined_by          = admin_user
            pub.decline_reason       = decline_reason
        db.session.flush()
    db.session.commit()


def _do_edit(db, pub_id: int, name: str, description: str | None,
             update_cadence: str | None, subject_area: str | None) -> None:
    """Simulate the core logic of the admin_publication_edit route."""
    from hansard_archive.models import StatPublication
    pub = StatPublication.query.get(pub_id)
    pub.name           = name.strip()
    pub.description    = description or None
    pub.update_cadence = update_cadence or None
    pub.subject_area   = subject_area or None
    db.session.commit()


# ---------------------------------------------------------------------------
# Authorise
# ---------------------------------------------------------------------------

class TestAdminAuthorise:

    def test_single_authorise_transitions_status(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-auth-single-prod")
            pub  = _make_publication(db, prod.id, "admin-auth-single-pub")
            db.session.commit()

            _do_bulk_action(db, "authorise", [pub.id])

            from hansard_archive.models import StatPublication
            updated = StatPublication.query.get(pub.id)
            assert updated.authorisation_status == "authorised"
            assert updated.authorised_at is not None
            assert updated.authorised_by == "test-admin@example.com"
            db.session.rollback()

    def test_single_authorise_writes_audit_log(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-auth-audit-prod")
            pub  = _make_publication(db, prod.id, "admin-auth-audit-pub")
            db.session.commit()

            _do_bulk_action(db, "authorise", [pub.id])

            from hansard_archive.models import StatPublicationAuditLog
            entries = StatPublicationAuditLog.query.filter_by(
                publication_id=pub.id).all()
            assert len(entries) == 1
            entry = entries[0]
            assert entry.old_status    == "candidate"
            assert entry.new_status    == "authorised"
            assert entry.recorded_by   == "test-admin@example.com"
            assert entry.change_reason is None
            db.session.rollback()

    def test_bulk_authorise_creates_one_audit_entry_per_pub(self, test_app, db):
        with test_app.app_context():
            prod    = _make_producer(db, "admin-bulk-auth-prod")
            pub_ids = [
                _make_publication(db, prod.id, f"admin-bulk-auth-pub-{i}").id
                for i in range(3)
            ]
            db.session.commit()

            _do_bulk_action(db, "authorise", pub_ids)

            from hansard_archive.models import StatPublication, StatPublicationAuditLog
            for pid in pub_ids:
                pub = StatPublication.query.get(pid)
                assert pub.authorisation_status == "authorised"
                assert StatPublicationAuditLog.query.filter_by(
                    publication_id=pid).count() == 1
            db.session.rollback()


# ---------------------------------------------------------------------------
# Decline
# ---------------------------------------------------------------------------

class TestAdminDecline:

    def test_single_decline_transitions_status(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-dec-single-prod")
            pub  = _make_publication(db, prod.id, "admin-dec-single-pub")
            db.session.commit()

            _do_bulk_action(db, "decline", [pub.id],
                            decline_reason="Not a statistical publication")

            from hansard_archive.models import StatPublication
            updated = StatPublication.query.get(pub.id)
            assert updated.authorisation_status == "declined"
            assert updated.declined_at is not None
            assert updated.declined_by == "test-admin@example.com"
            assert updated.decline_reason == "Not a statistical publication"
            db.session.rollback()

    def test_single_decline_writes_audit_log(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-dec-audit-prod")
            pub  = _make_publication(db, prod.id, "admin-dec-audit-pub")
            db.session.commit()

            _do_bulk_action(db, "decline", [pub.id],
                            decline_reason="Press release, not a dataset")

            from hansard_archive.models import StatPublicationAuditLog
            entries = StatPublicationAuditLog.query.filter_by(
                publication_id=pub.id).all()
            assert len(entries) == 1
            entry = entries[0]
            assert entry.old_status == "candidate"
            assert entry.new_status == "declined"
            assert "Press release" in (entry.change_reason or "")
            db.session.rollback()

    def test_decline_without_reason_allowed(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-dec-noreason-prod")
            pub  = _make_publication(db, prod.id, "admin-dec-noreason-pub")
            db.session.commit()

            _do_bulk_action(db, "decline", [pub.id], decline_reason=None)

            from hansard_archive.models import StatPublication
            updated = StatPublication.query.get(pub.id)
            assert updated.authorisation_status == "declined"
            assert updated.decline_reason is None
            db.session.rollback()


# ---------------------------------------------------------------------------
# Guard: non-candidate pubs are skipped
# ---------------------------------------------------------------------------

class TestBulkActionGuard:

    def test_already_authorised_pub_is_skipped(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-skip-prod")
            pub  = _make_publication(db, prod.id, "admin-skip-pub",
                                     authorisation_status="authorised")
            db.session.commit()

            _do_bulk_action(db, "decline", [pub.id])

            from hansard_archive.models import StatPublication, StatPublicationAuditLog
            updated = StatPublication.query.get(pub.id)
            assert updated.authorisation_status == "authorised"
            assert StatPublicationAuditLog.query.filter_by(
                publication_id=pub.id).count() == 0
            db.session.rollback()

    def test_declined_pub_is_skipped(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-skip-dec-prod")
            pub  = _make_publication(db, prod.id, "admin-skip-dec-pub",
                                     authorisation_status="declined")
            db.session.commit()

            _do_bulk_action(db, "authorise", [pub.id])

            from hansard_archive.models import StatPublication, StatPublicationAuditLog
            updated = StatPublication.query.get(pub.id)
            assert updated.authorisation_status == "declined"
            assert StatPublicationAuditLog.query.filter_by(
                publication_id=pub.id).count() == 0
            db.session.rollback()


# ---------------------------------------------------------------------------
# Inline edit
# ---------------------------------------------------------------------------

class TestInlineEdit:

    def test_edit_saves_all_fields(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-edit-prod")
            pub  = _make_publication(db, prod.id, "admin-edit-pub",
                                     name="Old Name", description="Old desc")
            db.session.commit()

            _do_edit(db, pub.id,
                     name="New Name",
                     description="New description",
                     update_cadence="monthly",
                     subject_area="Education")

            from hansard_archive.models import StatPublication
            updated = StatPublication.query.get(pub.id)
            assert updated.name           == "New Name"
            assert updated.description    == "New description"
            assert updated.update_cadence == "monthly"
            assert updated.subject_area   == "Education"
            db.session.rollback()

    def test_edit_does_not_create_audit_log_entry(self, test_app, db):
        """Edit only changes metadata — authorisation_status is unchanged, so no audit row."""
        with test_app.app_context():
            prod = _make_producer(db, "admin-edit-noaudit-prod")
            pub  = _make_publication(db, prod.id, "admin-edit-noaudit-pub")
            db.session.commit()

            _do_edit(db, pub.id,
                     name="Edited Name",
                     description="Updated",
                     update_cadence=None,
                     subject_area=None)

            from hansard_archive.models import StatPublicationAuditLog
            assert StatPublicationAuditLog.query.filter_by(
                publication_id=pub.id).count() == 0
            db.session.rollback()

    def test_edit_clears_optional_fields_when_empty(self, test_app, db):
        with test_app.app_context():
            prod = _make_producer(db, "admin-edit-clear-prod")
            pub  = _make_publication(db, prod.id, "admin-edit-clear-pub",
                                     description="Has a description",
                                     update_cadence="monthly",
                                     subject_area="Health")
            db.session.commit()

            _do_edit(db, pub.id,
                     name=pub.name,
                     description="",   # empty → None
                     update_cadence="",
                     subject_area="")

            from hansard_archive.models import StatPublication
            updated = StatPublication.query.get(pub.id)
            assert updated.description    is None
            assert updated.update_cadence is None
            assert updated.subject_area   is None
            db.session.rollback()
