"""
Tests for the ministerial meetings ingester.

Run: python -m pytest stakeholder_directory/tests/test_ministerial_meetings.py -v
"""
import pytest
from datetime import date
from pathlib import Path

from stakeholder_directory.ingesters.ministerial_meetings import ingest_ministerial_meetings
from stakeholder_directory.vocab import InvalidVocabularyValueError

FIXTURES_DIR = Path(__file__).parent / 'fixtures'
FIXTURE_CSV = FIXTURES_DIR / 'dfe_meetings_q1_2025.csv'
DEPT = 'department_for_education'
SOURCE_URL = 'https://www.gov.uk/government/collections/dfe-ministers-transparency-data'

# Expected counts for the fixture CSV:
# Row 1: Universities UK (normal)              → 1 staged
# Row 2: Russell Group; Universities UK        → 2 staged (multi-org), flagged (date range)
# Row 3: Ofsted (normal)                       → 1 staged
# Row 4: HM Treasury (internal govt)           → excluded
# Row 5: National Education Union (normal)     → 1 staged
# Row 6: (empty org)                           → 1 error
# Row 7: Association of Colleges (normal)      → 1 staged
EXPECTED_PROCESSED = 7
EXPECTED_STAGED = 6
EXPECTED_EXCLUDED = 1
EXPECTED_FLAGGED = 2
EXPECTED_ERRORS = 1


# ---------------------------------------------------------------------------
# 1. Basic happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_fixture_counts(self, app):
        result = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        assert result.rows_processed == EXPECTED_PROCESSED
        assert result.rows_staged == EXPECTED_STAGED
        assert result.rows_excluded == EXPECTED_EXCLUDED
        assert result.rows_flagged == EXPECTED_FLAGGED
        assert len(result.errors) == EXPECTED_ERRORS

    def test_staged_records_in_db(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        count = db.session.query(StagingMinisterialMeeting).count()
        assert count == EXPECTED_STAGED


# ---------------------------------------------------------------------------
# 2. Multi-org row split
# ---------------------------------------------------------------------------

class TestMultiOrgSplit:
    def test_semicolon_row_produces_two_records(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)

        records = db.session.query(StagingMinisterialMeeting).filter(
            StagingMinisterialMeeting.meeting_date == date(2025, 1, 10)
        ).all()
        assert len(records) == 2
        org_names = {r.raw_organisation_name for r in records}
        assert org_names == {'Russell Group', 'Universities UK'}

    def test_multi_org_records_share_minister_and_date(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)

        records = db.session.query(StagingMinisterialMeeting).filter(
            StagingMinisterialMeeting.meeting_date == date(2025, 1, 10)
        ).all()
        ministers = {r.minister_name for r in records}
        assert ministers == {'Bridget Phillipson'}


# ---------------------------------------------------------------------------
# 3. Date range parsing
# ---------------------------------------------------------------------------

class TestDateRangeParsing:
    def test_date_range_uses_start_date(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)

        record = db.session.query(StagingMinisterialMeeting).filter(
            StagingMinisterialMeeting.meeting_date == date(2025, 1, 10)
        ).first()
        assert record is not None
        assert record.meeting_date == date(2025, 1, 10)

    def test_date_range_sets_processing_notes(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)

        records = db.session.query(StagingMinisterialMeeting).filter(
            StagingMinisterialMeeting.meeting_date == date(2025, 1, 10)
        ).all()
        for r in records:
            assert r.processing_notes is not None
            assert 'date range' in r.processing_notes.lower()


# ---------------------------------------------------------------------------
# 4. Internal government exclusion
# ---------------------------------------------------------------------------

class TestInternalGovernmentExclusion:
    def test_hm_treasury_not_in_staging(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)

        treasury_records = db.session.query(StagingMinisterialMeeting).filter(
            StagingMinisterialMeeting.raw_organisation_name.ilike('%treasury%')
        ).all()
        assert treasury_records == []

    def test_excluded_count_is_one(self, app):
        result = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        assert result.rows_excluded == 1


# ---------------------------------------------------------------------------
# 5. Empty organisation field
# ---------------------------------------------------------------------------

class TestEmptyOrgField:
    def test_empty_org_counted_as_error(self, app):
        result = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        assert len(result.errors) == 1

    def test_empty_org_error_message(self, app):
        result = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        assert 'empty organisation' in result.errors[0].lower()


# ---------------------------------------------------------------------------
# 6. Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_run_adds_no_rows(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        count_after_first = db.session.query(StagingMinisterialMeeting).count()

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL)
        count_after_second = db.session.query(StagingMinisterialMeeting).count()

        assert count_after_first == count_after_second
        assert count_after_second == EXPECTED_STAGED


# ---------------------------------------------------------------------------
# 7. Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_correct_counts(self, app):
        result = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL, dry_run=True)
        assert result.rows_staged == EXPECTED_STAGED
        assert result.rows_excluded == EXPECTED_EXCLUDED
        assert len(result.errors) == EXPECTED_ERRORS

    def test_dry_run_writes_nothing_to_db(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL, dry_run=True)
        assert db.session.query(StagingMinisterialMeeting).count() == 0


# ---------------------------------------------------------------------------
# 8. Vocabulary guard
# ---------------------------------------------------------------------------

class TestVocabularyGuard:
    def test_invalid_department_raises_error(self):
        with pytest.raises(InvalidVocabularyValueError, match='nonexistent_dept_xyz'):
            ingest_ministerial_meetings(
                FIXTURE_CSV, 'nonexistent_dept_xyz', SOURCE_URL
            )

    def test_valid_department_does_not_raise(self, app):
        result = ingest_ministerial_meetings(FIXTURE_CSV, DEPT, SOURCE_URL, dry_run=True)
        assert result.rows_processed > 0


# ---------------------------------------------------------------------------
# 9. CSV with UTF-8 BOM
# ---------------------------------------------------------------------------

class TestBomHandling:
    def test_utf8_bom_file_loads_correctly(self, tmp_path, app):
        content = (
            'Minister,Date of meeting,Organisation(s) met,Purpose of meeting\n'
            'Bridget Phillipson,8 January 2025,Universities UK,Test meeting\n'
        )
        csv_path = tmp_path / 'bom_test.csv'
        csv_path.write_bytes(b'\xef\xbb\xbf' + content.encode('utf-8'))

        result = ingest_ministerial_meetings(csv_path, DEPT, 'https://example.gov.uk/bom')
        assert result.rows_staged == 1
        assert result.errors == []

    def test_bom_file_column_headers_detected(self, tmp_path, app):
        # BOM before "Minister" should not break header detection
        content = (
            'Minister,Date of meeting,Organisation(s) met,Purpose of meeting\n'
            'Josh MacAlister,15 January 2025,Ofsted,Inspection framework\n'
        )
        csv_path = tmp_path / 'bom_headers.csv'
        csv_path.write_bytes(b'\xef\xbb\xbf' + content.encode('utf-8'))

        result = ingest_ministerial_meetings(csv_path, DEPT, 'https://example.gov.uk/bom2')
        assert result.rows_staged == 1
        assert result.rows_excluded == 0


# ---------------------------------------------------------------------------
# 10. CP1252 encoding fallback
# ---------------------------------------------------------------------------

class TestCp1252Encoding:
    def test_cp1252_file_with_pound_sign(self, tmp_path, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

        content = (
            'Minister,Date of meeting,Organisation(s) met,Purpose of meeting\r\n'
            'Bridget Phillipson,8 January 2025,Funding \xa3 Alliance,Budget\r\n'
        )
        csv_path = tmp_path / 'cp1252.csv'
        csv_path.write_bytes(content.encode('cp1252'))

        result = ingest_ministerial_meetings(
            csv_path, DEPT, 'https://example.gov.uk/cp1252'
        )
        assert result.rows_staged == 1
        assert result.errors == []

        record = db.session.query(StagingMinisterialMeeting).first()
        assert record is not None
        assert '\xa3' in record.raw_organisation_name  # £ sign preserved
