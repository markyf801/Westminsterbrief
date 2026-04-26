"""
End-to-end and idempotency tests for the stakeholder directory pipeline.

Run: python -m pytest stakeholder_directory/tests/test_end_to_end.py -v
"""
import pytest
from datetime import date
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / 'fixtures'
FIXTURE_CSV = FIXTURES_DIR / 'dfe_meetings_q1_2025.csv'
DEPT = 'department_for_education'
SOURCE_URL = 'https://www.gov.uk/government/collections/dfe-ministers-transparency-data/e2e'


# ---------------------------------------------------------------------------
# End-to-end idempotency
# ---------------------------------------------------------------------------

class TestEndToEndIdempotency:
    """Full pipeline (ingest + normalise) run twice against the same fixture.
    All DB counts must be identical after the second run.
    """

    def _run_once(self, app):
        from stakeholder_directory.ingesters.ministerial_meetings import ingest_ministerial_meetings
        from stakeholder_directory.normalisation.normaliser import normalise_pending_staging
        ing = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        norm = normalise_pending_staging('staging_ministerial_meeting')
        return ing, norm

    def _snapshot(self, db):
        from stakeholder_directory.models import Organisation, Engagement, Alias, Flag
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting
        return {
            'orgs': db.session.query(Organisation).count(),
            'engagements': db.session.query(Engagement).count(),
            'aliases': db.session.query(Alias).count(),
            'flags': db.session.query(Flag).count(),
            'staging_total': db.session.query(StagingMinisterialMeeting).count(),
        }

    def test_second_run_changes_no_counts(self, app):
        from extensions import db

        self._run_once(app)
        snap1 = self._snapshot(db)

        self._run_once(app)
        snap2 = self._snapshot(db)

        assert snap1 == snap2, f"DB changed on second run: {snap1} → {snap2}"

    def test_second_run_rows_staged_is_zero(self, app):
        self._run_once(app)
        result2, _ = self._run_once(app)
        assert result2.rows_processed > 0
        assert result2.rows_staged == 0

    def test_no_duplicate_engagements(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement

        self._run_once(app)
        self._run_once(app)

        # Each engagement must have a unique (organisation_id, source_url, engagement_date)
        rows = db.session.query(
            Engagement.organisation_id,
            Engagement.source_url,
            Engagement.engagement_date,
        ).all()
        assert len(rows) == len(set(rows)), "Duplicate engagement records found"


# ---------------------------------------------------------------------------
# Ingestion log idempotency
# ---------------------------------------------------------------------------

class TestIngestionLogIdempotency:
    """One pipeline run = one sd_ingestion_run row, even across multiple CSVs."""

    def test_single_run_produces_one_log_row(self, app):
        from extensions import db
        from stakeholder_directory.models import IngestionRun
        from stakeholder_directory.pipeline import run_pipeline

        run_pipeline(
            csv_pairs=[(FIXTURE_CSV, SOURCE_URL)],
            department=DEPT,
            app=app,
        )
        assert db.session.query(IngestionRun).count() == 1

    def test_two_runs_produce_two_log_rows(self, app):
        from extensions import db
        from stakeholder_directory.models import IngestionRun
        from stakeholder_directory.pipeline import run_pipeline

        run_pipeline(csv_pairs=[(FIXTURE_CSV, SOURCE_URL)], department=DEPT, app=app)
        run_pipeline(csv_pairs=[(FIXTURE_CSV, SOURCE_URL + '/run2')], department=DEPT, app=app)
        assert db.session.query(IngestionRun).count() == 2

    def test_multi_csv_run_produces_one_log_row(self, app):
        """Processing N CSVs in a single pipeline call creates exactly one log row."""
        from extensions import db
        from stakeholder_directory.models import IngestionRun
        from stakeholder_directory.pipeline import run_pipeline

        run_pipeline(
            csv_pairs=[
                (FIXTURE_CSV, SOURCE_URL + '/a'),
                (FIXTURE_CSV, SOURCE_URL + '/b'),
            ],
            department=DEPT,
            app=app,
        )
        assert db.session.query(IngestionRun).count() == 1

    def test_log_row_captures_correct_counts(self, app):
        from extensions import db
        from stakeholder_directory.models import IngestionRun
        from stakeholder_directory.pipeline import run_pipeline

        run_pipeline(
            csv_pairs=[(FIXTURE_CSV, SOURCE_URL)],
            department=DEPT,
            app=app,
        )
        # run_pipeline closes its own app context, detaching the returned ORM object.
        # Re-query from the test session instead.
        row = db.session.query(IngestionRun).order_by(IngestionRun.id.desc()).first()
        assert row is not None
        assert row.rows_ingested > 0
        assert row.organisations_created > 0
        assert row.engagements_created > 0
        assert row.duration_seconds >= 0
        assert row.department == DEPT
        assert row.errors is None or isinstance(row.errors, list)

    def test_second_run_log_shows_zero_new_orgs(self, app):
        """Second run against same data: log row has 0 new orgs/engagements."""
        from extensions import db
        from stakeholder_directory.models import IngestionRun
        from stakeholder_directory.pipeline import run_pipeline

        run_pipeline(csv_pairs=[(FIXTURE_CSV, SOURCE_URL)], department=DEPT, app=app)
        run_pipeline(csv_pairs=[(FIXTURE_CSV, SOURCE_URL)], department=DEPT, app=app)

        row2 = db.session.query(IngestionRun).order_by(IngestionRun.id.desc()).first()
        assert row2.organisations_created == 0
        assert row2.engagements_created == 0


# ---------------------------------------------------------------------------
# Per-row atomicity
# ---------------------------------------------------------------------------

class TestNormaliserAtomicity:
    """Mid-row failures must leave no partial writes in the database."""

    def test_mid_row_failure_leaves_no_orphaned_org(self, app, monkeypatch):
        """If commit_staging_record raises after org creation, the org must be
        rolled back by the savepoint — no orphaned organisation should exist."""
        from extensions import db
        from stakeholder_directory.ingesters.ministerial_meetings import ingest_ministerial_meetings
        from stakeholder_directory.normalisation import normaliser
        from stakeholder_directory.models import Organisation, Engagement

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)

        call_count = {'n': 0}

        original = normaliser.commit_staging_record

        def fail_on_first(staging_row, org, dry_run=False):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise RuntimeError("injected failure for atomicity test")
            return original(staging_row, org, dry_run=dry_run)

        monkeypatch.setattr(normaliser, 'commit_staging_record', fail_on_first)

        from stakeholder_directory.normalisation.normaliser import normalise_pending_staging
        result = normalise_pending_staging('staging_ministerial_meeting')

        assert len(result.errors) == 1, "first row should have errored"

        # The org that was flush()'d for the failing row must have been rolled back
        orgs = db.session.query(Organisation).all()
        engagements = db.session.query(Engagement).all()

        # Every org must have at least one engagement — no orphans from partial writes
        org_ids_with_engagements = {e.organisation_id for e in engagements}
        orphaned = [o for o in orgs if o.id not in org_ids_with_engagements]
        assert orphaned == [], f"Orphaned orgs after mid-row failure: {[o.canonical_name for o in orphaned]}"
