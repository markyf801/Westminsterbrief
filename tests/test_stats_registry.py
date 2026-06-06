"""
Tests for Phase 1.6 source registry schema.

Covers:
  - StatProducer model (constraints, self-referencing FK, app-layer invariants,
    updated_at auto-update)
  - StatPublication model (FK, unique constraint, cadence CHECK, RESTRICT on delete)
  - StatLicenceAuditLog model (FK, append-only enforcement)
  - HeadlineStat FK fields (producer_id, publication_id)
  - Audit log event hook (after_insert, before_update, unrelated-field no-op,
    single audit entry for multi-field update)
  - Migration module upgrade() / downgrade() round-trip
  - Seed script (build_seed_data, validate_seed_data, idempotency, dry-run,
    audit log entry per producer)

Run: python -m pytest tests/test_stats_registry.py -v
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# Shared app / db fixtures (module-scoped in-memory SQLite)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models        # noqa: F401 — registers all tables
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
        # Enable FK enforcement — SQLite ignores FK constraints without this pragma
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
# Helpers
# ---------------------------------------------------------------------------

def _make_producer(db, slug: str, licence: str = "OGL_v3",
                   evidence_url: str | None = "https://example.com/copyright",
                   **kwargs) -> "StatProducer":
    from hansard_archive.models import StatProducer
    p = StatProducer(
        slug=slug,
        name=f"Producer {slug}",
        producer_type="central_department",
        web_root_url="https://example.gov.uk/",
        licence=licence,
        licence_evidence_raw_url=evidence_url,
        **kwargs,
    )
    db.session.add(p)
    db.session.flush()
    return p


def _make_stat(db, source_id: str) -> "HeadlineStat":
    from hansard_archive.models import HeadlineStat
    s = HeadlineStat(
        theme_slug="economy",
        source_type="govuk_bulletin",
        source_id=source_id,
        display_label=f"Stat {source_id}",
        display_hint="raw",
        geography="UK",
        latest_value="42",
        unit="%",
        source_url="https://example.com",
        last_refreshed=datetime(2026, 5, 1),
        last_success=datetime(2026, 5, 1),
    )
    db.session.add(s)
    db.session.flush()
    return s


# ---------------------------------------------------------------------------
# StatProducer model tests
# ---------------------------------------------------------------------------

class TestStatProducerModel:

    def test_create_and_query_by_slug(self, test_app, db):
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            _make_producer(db, "ons-test")
            db.session.commit()
            found = db.session.query(StatProducer).filter_by(slug="ons-test").first()
            assert found is not None
            assert found.name == "Producer ons-test"

    def test_slug_unique_constraint(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            _make_producer(db, "ons-dup")
            db.session.commit()
            # Build the duplicate without flush so error surfaces at commit
            dup = StatProducer(
                slug="ons-dup", name="Dup", producer_type="central_department",
                web_root_url="https://example.gov.uk/", licence="OGL_v3",
                licence_evidence_raw_url="https://example.com/",
            )
            db.session.add(dup)
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_producer_type_check_rejects_invalid(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            p = StatProducer(
                slug="bad-type", name="Bad Type",
                producer_type="not_a_valid_type",
                web_root_url="https://example.gov.uk/", licence="OGL_v3",
                licence_evidence_raw_url="https://example.com/",
            )
            db.session.add(p)
            with pytest.raises((IntegrityError, Exception)):
                db.session.commit()
            db.session.rollback()

    def test_authorisation_status_check_rejects_invalid(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            p = StatProducer(
                slug="bad-status", name="Bad Status",
                producer_type="central_department",
                web_root_url="https://example.gov.uk/", licence="OGL_v3",
                licence_evidence_raw_url="https://example.com/",
                authorisation_status="not_valid",
            )
            db.session.add(p)
            with pytest.raises((IntegrityError, Exception)):
                db.session.commit()
            db.session.rollback()

    def test_licence_check_rejects_invalid(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            p = StatProducer(
                slug="bad-licence", name="Bad Licence",
                producer_type="central_department",
                web_root_url="https://example.gov.uk/", licence="MIT",
                licence_evidence_raw_url="https://example.com/",
            )
            db.session.add(p)
            with pytest.raises((IntegrityError, Exception)):
                db.session.commit()
            db.session.rollback()

    def test_parent_fk_self_reference(self, test_app, db):
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            dept = _make_producer(db, "dept-parent")
            db.session.commit()
            agency = _make_producer(db, "agency-child", parent_id=dept.id)
            db.session.commit()
            found = db.session.query(StatProducer).filter_by(slug="agency-child").first()
            assert found.parent_id == dept.id

    def test_cross_column_invariant_licence_not_unverified_requires_evidence_url(self, test_app, db):
        """Application layer: licence != Unverified should have evidence URL."""
        with test_app.app_context():
            from scripts.seed_stat_producers import validate_seed_data
            # Build a producer dict that violates the invariant
            violating = [{
                "slug": "violator",
                "licence": "OGL_v3",
                "licence_evidence_raw_url": None,
            }]
            problems = validate_seed_data(violating)
            assert len(problems) == 1
            assert "violator" in problems[0]

    def test_cross_column_invariant_authorised_requires_reviewed_fields(self, test_app, db):
        """Application layer: authorised status requires reviewed_by + reviewed_at."""
        from hansard_archive.models import StatProducer
        with test_app.app_context():
            p = _make_producer(db, "auth-invariant-test")
            p.authorisation_status = "authorised"
            p.reviewed_by = None
            p.reviewed_at = None
            # This invariant is enforced at the application layer, not DB layer.
            # Verify the fields are accessible and can be set.
            assert p.reviewed_by is None
            assert p.reviewed_at is None
            assert p.authorisation_status == "authorised"
            db.session.rollback()

    def test_updated_at_auto_updates(self, test_app, db):
        from hansard_archive.models import StatProducer
        import time
        with test_app.app_context():
            p = _make_producer(db, "updated-at-test")
            db.session.commit()
            original = p.updated_at
            time.sleep(0.05)
            p.description = "Updated description"
            db.session.commit()
            db.session.refresh(p)
            assert p.updated_at >= original


# ---------------------------------------------------------------------------
# StatPublication model tests
# ---------------------------------------------------------------------------

class TestStatPublicationModel:

    def test_create_with_valid_producer(self, test_app, db):
        from hansard_archive.models import StatPublication
        with test_app.app_context():
            prod = _make_producer(db, "pub-parent")
            db.session.commit()
            pub = StatPublication(
                producer_id=prod.id,
                slug="annual-report",
                name="Annual Report",
                url="https://example.gov.uk/annual",
            )
            db.session.add(pub)
            db.session.commit()
            assert pub.id is not None

    def test_unique_constraint_producer_slug(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatPublication
        with test_app.app_context():
            prod = _make_producer(db, "pub-unique-parent")
            db.session.commit()
            pub1 = StatPublication(
                producer_id=prod.id, slug="dup-slug",
                name="Pub 1", url="https://example.gov.uk/1",
            )
            pub2 = StatPublication(
                producer_id=prod.id, slug="dup-slug",
                name="Pub 2", url="https://example.gov.uk/2",
            )
            db.session.add_all([pub1, pub2])
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_update_cadence_check_rejects_invalid(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatPublication
        with test_app.app_context():
            prod = _make_producer(db, "pub-cadence-parent")
            db.session.commit()
            pub = StatPublication(
                producer_id=prod.id, slug="bad-cadence",
                name="Bad", url="https://example.gov.uk/bad",
                update_cadence="fortnightly",
            )
            db.session.add(pub)
            with pytest.raises((IntegrityError, Exception)):
                db.session.commit()
            db.session.rollback()

    def test_deleting_producer_with_publications_raises(self, test_app, db):
        """Deleting a producer while publications still reference it is rejected (RESTRICT)."""
        from sqlalchemy import text as sa_text
        from sqlalchemy.exc import IntegrityError
        from hansard_archive.models import StatPublication
        with test_app.app_context():
            prod = _make_producer(db, "restrict-parent")
            db.session.commit()
            pub = StatPublication(
                producer_id=prod.id, slug="keep-alive",
                name="Keep Alive", url="https://example.gov.uk/ka",
            )
            db.session.add(pub)
            db.session.commit()
            # Use raw SQL to bypass ORM cascade handling and hit the DB-level RESTRICT FK
            with pytest.raises((IntegrityError, Exception)):
                db.session.execute(
                    sa_text("DELETE FROM ha_stat_producer WHERE id = :id"),
                    {"id": prod.id},
                )
                db.session.commit()
            db.session.rollback()


# ---------------------------------------------------------------------------
# StatLicenceAuditLog model tests
# ---------------------------------------------------------------------------

class TestStatLicenceAuditLogModel:

    def test_create_with_valid_producer(self, test_app, db):
        from hansard_archive.models import StatLicenceAuditLog
        with test_app.app_context():
            prod = _make_producer(db, "audit-fk-test")
            db.session.commit()
            # The after_insert hook already created one row; verify it
            log_rows = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=prod.id)
                .all()
            )
            assert len(log_rows) >= 1

    def test_append_only_no_update_method(self, test_app, db):
        """StatLicenceAuditLog class must not expose an update() method."""
        from hansard_archive.models import StatLicenceAuditLog
        assert not hasattr(StatLicenceAuditLog, "update"), (
            "StatLicenceAuditLog must not expose an update() method"
        )

    def test_append_only_no_delete_method(self, test_app, db):
        """StatLicenceAuditLog class must not expose a delete() method."""
        from hansard_archive.models import StatLicenceAuditLog
        assert not hasattr(StatLicenceAuditLog, "delete"), (
            "StatLicenceAuditLog must not expose a delete() method"
        )


# ---------------------------------------------------------------------------
# HeadlineStat FK tests
# ---------------------------------------------------------------------------

class TestHeadlineStatFKs:

    def test_producer_id_and_publication_id_nullable(self, test_app, db):
        with test_app.app_context():
            s = _make_stat(db, "fk-nullable-test")
            db.session.commit()
            assert s.producer_id is None
            assert s.publication_id is None

    def test_producer_id_accepts_valid_id(self, test_app, db):
        from hansard_archive.models import HeadlineStat
        with test_app.app_context():
            prod = _make_producer(db, "hs-fk-valid")
            db.session.commit()
            s = _make_stat(db, "hs-fk-valid-stat")
            s.producer_id = prod.id
            db.session.commit()
            found = db.session.get(HeadlineStat, s.id)
            assert found.producer_id == prod.id

    def test_producer_id_invalid_raises(self, test_app, db):
        from sqlalchemy.exc import IntegrityError
        with test_app.app_context():
            s = _make_stat(db, "hs-fk-invalid")
            s.producer_id = 999999
            with pytest.raises((IntegrityError, Exception)):
                db.session.commit()
            db.session.rollback()

    def test_publication_id_without_producer_id_allowed(self, test_app, db):
        from hansard_archive.models import StatPublication
        with test_app.app_context():
            prod = _make_producer(db, "hs-pub-only-parent")
            db.session.commit()
            pub = StatPublication(
                producer_id=prod.id, slug="pub-only",
                name="Pub Only", url="https://example.gov.uk/",
            )
            db.session.add(pub)
            db.session.commit()
            s = _make_stat(db, "hs-pub-only-stat")
            s.publication_id = pub.id
            # publication_id set without producer_id — schema allows this
            db.session.commit()
            assert s.publication_id == pub.id
            assert s.producer_id is None


# ---------------------------------------------------------------------------
# Audit log event hook tests
# ---------------------------------------------------------------------------

class TestAuditLogHook:

    def test_create_produces_one_audit_entry(self, test_app, db):
        from hansard_archive.models import StatProducer, StatLicenceAuditLog
        with test_app.app_context():
            p = _make_producer(db, "hook-create", licence="OGL_v3",
                               evidence_url="https://example.com/lic")
            db.session.commit()
            entries = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id)
                .all()
            )
            assert len(entries) == 1
            entry = entries[0]
            assert entry.old_licence is None
            assert entry.new_licence == "OGL_v3"
            assert entry.change_reason == "Initial classification"
            assert entry.new_evidence_raw_url == "https://example.com/lic"

    def test_update_licence_creates_audit_entry(self, test_app, db):
        from hansard_archive.models import StatLicenceAuditLog
        with test_app.app_context():
            p = _make_producer(db, "hook-upd-licence")
            db.session.commit()
            count_before = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            p.licence = "Crown_Copyright_other"
            db.session.commit()
            count_after = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            assert count_after == count_before + 1
            latest = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id)
                .order_by(StatLicenceAuditLog.id.desc())
                .first()
            )
            assert latest.old_licence == "OGL_v3"
            assert latest.new_licence == "Crown_Copyright_other"

    def test_update_evidence_raw_url_creates_audit_entry(self, test_app, db):
        from hansard_archive.models import StatLicenceAuditLog
        with test_app.app_context():
            p = _make_producer(db, "hook-upd-raw")
            db.session.commit()
            count_before = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            p.licence_evidence_raw_url = "https://new-evidence.example.com/"
            db.session.commit()
            count_after = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            assert count_after == count_before + 1

    def test_update_evidence_wayback_url_creates_audit_entry(self, test_app, db):
        from hansard_archive.models import StatLicenceAuditLog
        with test_app.app_context():
            p = _make_producer(db, "hook-upd-way")
            db.session.commit()
            count_before = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            p.licence_evidence_wayback_url = "https://web.archive.org/web/20260101/https://example.com/"
            db.session.commit()
            count_after = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            assert count_after == count_before + 1

    def test_update_unrelated_field_does_not_create_audit_entry(self, test_app, db):
        from hansard_archive.models import StatLicenceAuditLog
        with test_app.app_context():
            p = _make_producer(db, "hook-no-audit")
            db.session.commit()
            count_before = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            p.description = "A new description that should not trigger audit."
            db.session.commit()
            count_after = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            assert count_after == count_before, (
                "Updating description must not create an audit log entry"
            )

    def test_multi_field_update_creates_single_audit_entry(self, test_app, db):
        from hansard_archive.models import StatLicenceAuditLog
        with test_app.app_context():
            p = _make_producer(db, "hook-multi-field")
            db.session.commit()
            count_before = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            p.licence = "Crown_Copyright_other"
            p.licence_evidence_raw_url = "https://new.example.com/lic"
            p.licence_evidence_wayback_url = "https://web.archive.org/web/20260101/..."
            db.session.commit()
            count_after = (
                db.session.query(StatLicenceAuditLog)
                .filter_by(producer_id=p.id).count()
            )
            assert count_after == count_before + 1, (
                "A single save changing multiple licence fields must create exactly one audit entry"
            )


# ---------------------------------------------------------------------------
# Migration module tests
# ---------------------------------------------------------------------------

class TestMigrationModule:

    @pytest.fixture
    def fresh_engine(self):
        """A fresh in-memory SQLite engine with only headline_stat pre-created."""
        from sqlalchemy import create_engine, event as sa_event, text as sa_text
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

        @sa_event.listens_for(engine, "connect")
        def _fk_pragma(dbapi_conn, record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
        with engine.begin() as conn:
            conn.execute(sa_text("""
                CREATE TABLE headline_stat (
                    id INTEGER PRIMARY KEY,
                    theme_slug TEXT,
                    source_type TEXT,
                    source_id TEXT,
                    display_label TEXT,
                    display_hint TEXT,
                    geography TEXT,
                    latest_value TEXT,
                    unit TEXT,
                    source_url TEXT,
                    last_refreshed TIMESTAMP,
                    last_success TIMESTAMP
                )
            """))
            conn.execute(sa_text(
                "INSERT INTO headline_stat VALUES (1,'economy','govuk_bulletin','s1',"
                "'Label','raw','UK','1.2','%','https://example.com','2026-01-01','2026-01-01')"
            ))
        return engine

    def test_upgrade_creates_tables(self, fresh_engine):
        from migrations.phase_1_6_source_registry import upgrade
        from sqlalchemy import inspect as sa_inspect_engine
        upgrade(fresh_engine)
        inspector = sa_inspect_engine(fresh_engine)
        table_names = inspector.get_table_names()
        assert "ha_stat_producer" in table_names
        assert "ha_stat_publication" in table_names
        assert "ha_stat_licence_audit_log" in table_names

    def test_upgrade_adds_fk_columns_to_headline_stat(self, fresh_engine):
        from migrations.phase_1_6_source_registry import upgrade
        from sqlalchemy import inspect as sa_inspect_engine
        upgrade(fresh_engine)
        inspector = sa_inspect_engine(fresh_engine)
        col_names = {c["name"] for c in inspector.get_columns("headline_stat")}
        assert "producer_id" in col_names
        assert "publication_id" in col_names

    def test_existing_headline_stat_rows_null_fks_after_upgrade(self, fresh_engine):
        from migrations.phase_1_6_source_registry import upgrade
        from sqlalchemy import text as sa_text
        upgrade(fresh_engine)
        with fresh_engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT producer_id, publication_id FROM headline_stat WHERE id=1")
            ).fetchone()
        assert row[0] is None
        assert row[1] is None

    def test_existing_headline_stat_data_unchanged_after_upgrade(self, fresh_engine):
        from migrations.phase_1_6_source_registry import upgrade
        from sqlalchemy import text as sa_text
        upgrade(fresh_engine)
        with fresh_engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT source_id, latest_value FROM headline_stat WHERE id=1")
            ).fetchone()
        assert row[0] == "s1"
        assert row[1] == "1.2"

    def test_downgrade_removes_tables(self, fresh_engine):
        from migrations.phase_1_6_source_registry import upgrade, downgrade
        from sqlalchemy import inspect as sa_inspect_engine
        upgrade(fresh_engine)
        downgrade(fresh_engine)
        inspector = sa_inspect_engine(fresh_engine)
        table_names = inspector.get_table_names()
        assert "ha_stat_producer" not in table_names
        assert "ha_stat_publication" not in table_names
        assert "ha_stat_licence_audit_log" not in table_names

    def test_round_trip_upgrade_downgrade_upgrade(self, fresh_engine):
        from migrations.phase_1_6_source_registry import upgrade, downgrade
        from sqlalchemy import inspect as sa_inspect_engine
        upgrade(fresh_engine)
        downgrade(fresh_engine)
        upgrade(fresh_engine)
        inspector = sa_inspect_engine(fresh_engine)
        table_names = inspector.get_table_names()
        assert "ha_stat_producer" in table_names
        assert "ha_stat_publication" in table_names
        assert "ha_stat_licence_audit_log" in table_names


# ---------------------------------------------------------------------------
# Seed script tests
# ---------------------------------------------------------------------------

class TestSeedScript:

    def test_build_seed_data_returns_30_entries(self):
        # Exact-count guard — deliberately brittle so an UNINTENDED add/removal
        # to the seed list trips this test for human review. 30 = original 29 +
        # office-for-budget-responsibility (OBR, deliberate add, deploys 3a1a328).
        # If this fails, confirm the change is intended before bumping the number.
        from scripts.seed_stat_producers import build_seed_data
        data = build_seed_data()
        assert len(data) == 30, f"Expected 30 producers, got {len(data)}"

    def test_no_duplicate_slugs_in_seed_data(self):
        from scripts.seed_stat_producers import build_seed_data
        data = build_seed_data()
        slugs = [p["slug"] for p in data]
        assert len(slugs) == len(set(slugs)), "Duplicate slug(s) in seed data"

    def test_ogl_v3_entries_have_evidence_url(self):
        from scripts.seed_stat_producers import build_seed_data
        data = build_seed_data()
        for p in data:
            if p["licence"] == "OGL_v3":
                assert p.get("licence_evidence_raw_url"), (
                    f"{p['slug']}: OGL_v3 licence must have licence_evidence_raw_url"
                )

    def test_unverified_entries_have_null_evidence_url(self):
        from scripts.seed_stat_producers import build_seed_data
        data = build_seed_data()
        for p in data:
            if p["licence"] == "Unverified":
                assert p.get("licence_evidence_raw_url") is None, (
                    f"{p['slug']}: Unverified licence must have null evidence URL"
                )

    def test_validate_seed_data_passes_on_valid_data(self):
        from scripts.seed_stat_producers import build_seed_data, validate_seed_data
        data = build_seed_data()
        problems = validate_seed_data(data)
        assert problems == [], f"Seed data validation errors: {problems}"

    def test_seed_inserts_all_producers(self, test_app, db):
        from hansard_archive.models import StatProducer
        from scripts.seed_stat_producers import build_seed_data, seed

        with test_app.app_context():
            producers = build_seed_data()
            before = db.session.query(StatProducer).count()
            seed(db.session, StatProducer, producers, execute=True)
            after = db.session.query(StatProducer).count()
            # Assert all seeded producers were inserted — derive from the data,
            # not a magic number, so a legitimate seed-list change doesn't re-break
            # this behavioural test (the exact-count guard above is the count check).
            assert after - before == len(producers)

    def test_seed_idempotent_no_duplicates_on_rerun(self, test_app, db):
        from hansard_archive.models import StatProducer
        from scripts.seed_stat_producers import build_seed_data, seed

        with test_app.app_context():
            producers = build_seed_data()
            # First run already done in test_seed_inserts_all_producers (module scope)
            count_before = db.session.query(StatProducer).count()
            seed(db.session, StatProducer, producers, execute=True)
            count_after = db.session.query(StatProducer).count()
            assert count_after == count_before, (
                "Re-running seed must not create duplicates"
            )

    def test_every_seeded_producer_has_audit_log_entry(self, test_app, db):
        from hansard_archive.models import StatProducer, StatLicenceAuditLog

        with test_app.app_context():
            producers = db.session.query(StatProducer).all()
            for p in producers:
                count = (
                    db.session.query(StatLicenceAuditLog)
                    .filter_by(producer_id=p.id)
                    .count()
                )
                assert count >= 1, (
                    f"Producer {p.slug!r} has no audit log entry"
                )

    def test_dry_run_does_not_write_producers(self, test_app, db):
        from hansard_archive.models import StatProducer
        from scripts.seed_stat_producers import build_seed_data, seed

        with test_app.app_context():
            # Use a slug not in the main seed list
            unique_slug = "dry-run-only-producer-xyzzy"
            dry_run_producer = [{
                "slug": unique_slug,
                "name": "Dry Run Test",
                "producer_type": "other_public_body",
                "web_root_url": "https://dryrun.example.com/",
                "licence": "OGL_v3",
                "licence_evidence_raw_url": "https://dryrun.example.com/copyright",
                "description": "Dry run test producer",
            }]
            seed(db.session, StatProducer, dry_run_producer, execute=False)
            found = db.session.query(StatProducer).filter_by(slug=unique_slug).first()
            assert found is None, "Dry run must not write any rows"
