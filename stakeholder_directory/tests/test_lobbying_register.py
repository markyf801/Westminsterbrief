"""
Tests for the lobbying register ingester.

Run: python -m pytest stakeholder_directory/tests/test_lobbying_register.py -v
"""
import csv
import io
import pytest
from datetime import date
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / 'fixtures'
SAMPLE_CSV = FIXTURES_DIR / 'lobbying_register_sample.csv'

QUARTER = '2022-Q4'
Q_START = date(2022, 10, 1)
Q_END = date(2022, 12, 31)


def _write_csv(tmp_path, rows: list[dict]) -> Path:
    """Write a CSV file with lobbying register columns and return its path."""
    path = tmp_path / 'test_register.csv'
    fieldnames = ['Registered organisation name', 'Client name']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# 1. Happy path — single CSV row → 2 staging records
# ---------------------------------------------------------------------------

class TestHappyPathFirm:
    def test_two_staging_records_per_row(self, app, tmp_path):
        """A single firm-client row → exactly 2 staging records (firm + client)."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Hanson UK'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_staged == 2
        assert result.rows_errored == 0
        assert result.errors == []

        rows = db.session.query(StagingLobbyingEntry).order_by(
            StagingLobbyingEntry.record_type
        ).all()
        assert len(rows) == 2

        client_row = next(r for r in rows if r.record_type == 'client')
        firm_row = next(r for r in rows if r.record_type == 'firm')

        assert firm_row.raw_organisation_name == 'Copper Consultancy Limited'
        assert firm_row.firm_name == 'Copper Consultancy Limited'
        assert firm_row.client_name == 'Hanson UK'
        assert firm_row.quarter == QUARTER
        assert firm_row.quarter_start_date == Q_START
        assert firm_row.processing_status == 'pending'
        assert 'Hanson UK' in (firm_row.processing_notes or '')

        assert client_row.raw_organisation_name == 'Hanson UK'
        assert client_row.firm_name == 'Copper Consultancy Limited'
        assert client_row.client_name == 'Hanson UK'
        assert 'Copper Consultancy' in (client_row.processing_notes or '')


# ---------------------------------------------------------------------------
# 2. Multi-client firm in same quarter → 6 staging records (3 firm + 3 client)
# ---------------------------------------------------------------------------

class TestMultiClientFirm:
    def test_three_clients_produce_six_rows(self, app, tmp_path):
        """Firm with 3 clients → 3 firm rows + 3 client rows = 6 total."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Public Affairs Co',
             'Client name': 'Universities UK'},
            {'Registered organisation name': 'Public Affairs Co',
             'Client name': 'Russell Group'},
            {'Registered organisation name': 'Public Affairs Co',
             'Client name': 'Association of Colleges'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_read == 3
        assert result.rows_staged == 6
        assert result.errors == []

        firm_rows = (
            db.session.query(StagingLobbyingEntry)
            .filter_by(record_type='firm').all()
        )
        client_rows = (
            db.session.query(StagingLobbyingEntry)
            .filter_by(record_type='client').all()
        )
        assert len(firm_rows) == 3
        assert len(client_rows) == 3
        client_names = sorted(r.client_name for r in client_rows)
        assert client_names == ['Association of Colleges', 'Russell Group', 'Universities UK']


# ---------------------------------------------------------------------------
# 3. Internal government filter — client matching internal govt list is skipped
# ---------------------------------------------------------------------------

class TestInternalGovtFilter:
    def test_internal_govt_client_skipped(self, app, tmp_path):
        """Client matching internal_government → client row skipped; firm row kept."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        # "Department for Education" is on the internal govt list
        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Teneo Strategy Limited',
             'Client name': 'Department for Education'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_skipped_internal_govt == 1   # client row skipped
        assert result.rows_staged == 1                  # firm row kept
        assert result.errors == []

        rows = db.session.query(StagingLobbyingEntry).all()
        assert len(rows) == 1
        assert rows[0].record_type == 'firm'
        assert rows[0].raw_organisation_name == 'Teneo Strategy Limited'

    def test_internal_govt_firm_skipped(self, app, tmp_path):
        """Firm matching internal_government → firm row skipped; client row kept."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Department for Education',
             'Client name': 'Copper Consultancy Limited'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_skipped_internal_govt == 1
        assert result.rows_staged == 1
        rows = db.session.query(StagingLobbyingEntry).all()
        assert len(rows) == 1
        assert rows[0].record_type == 'client'


# ---------------------------------------------------------------------------
# 4. Idempotency — second run produces no new rows
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_run_skips_all(self, app, tmp_path):
        """Running against the same file twice: second run stages 0 new rows."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Hanson UK'},
        ])
        result1 = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)
        result2 = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result1.rows_staged == 2
        assert result2.rows_staged == 0
        assert result2.rows_skipped_duplicate == 2
        assert db.session.query(StagingLobbyingEntry).count() == 2


# ---------------------------------------------------------------------------
# 5. Dry run — counts but writes nothing
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_counts_without_writing(self, app, tmp_path):
        """dry_run=True reports staged count but DB stays empty."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Hanson UK'},
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Oxford Properties'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END, dry_run=True)

        assert result.rows_staged == 4          # 2 rows × 2 records each
        assert result.errors == []
        assert db.session.query(StagingLobbyingEntry).count() == 0


# ---------------------------------------------------------------------------
# 6. Quarter dating — staging row carries the quarter start date
# ---------------------------------------------------------------------------

class TestQuarterDating:
    def test_quarter_start_date_stored_correctly(self, app, tmp_path):
        """quarter_start_date on staged rows equals the supplied date."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        custom_start = date(2025, 1, 1)
        custom_end = date(2025, 3, 31)
        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Policy Connect Limited',
             'Client name': 'AstraZeneca'},
        ])
        ingest_lobbying_register(
            str(path), '2025-Q1', custom_start, custom_end
        )
        rows = db.session.query(StagingLobbyingEntry).all()
        assert all(r.quarter_start_date == custom_start for r in rows)
        assert all(r.quarter_end_date == custom_end for r in rows)
        assert all(r.quarter == '2025-Q1' for r in rows)


# ---------------------------------------------------------------------------
# 7. Vocabulary check — both source types exist in the vocab
# ---------------------------------------------------------------------------

class TestVocabCheck:
    def test_lobbying_register_source_types_in_vocab(self):
        """lobbying_register and lobbying_register_client must be in SOURCE_TYPE_VALUES."""
        from stakeholder_directory.vocab import SOURCE_TYPE_VALUES
        assert 'lobbying_register' in SOURCE_TYPE_VALUES
        assert 'lobbying_register_client' in SOURCE_TYPE_VALUES


# ---------------------------------------------------------------------------
# 8. Unique constraint — same firm-client-quarter-type cannot be inserted twice
# ---------------------------------------------------------------------------

class TestUniqueConstraintEnforcement:
    def test_duplicate_within_single_run_produces_one_row(self, app, tmp_path):
        """Two identical firm-client CSV rows in one file → 2 staged, 2 skipped dup."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        # Deliberate duplicate: same firm + client appears twice
        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Hanson UK'},
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Hanson UK'},   # duplicate
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_staged == 2          # first occurrence (firm + client)
        assert result.rows_skipped_duplicate == 2   # second occurrence
        assert db.session.query(StagingLobbyingEntry).count() == 2


# ---------------------------------------------------------------------------
# 9. Empty client field → errored
# ---------------------------------------------------------------------------

class TestEmptyClientField:
    def test_empty_client_counted_as_error(self, app, tmp_path):
        """Row with firm but empty client → rows_errored=1, nothing staged."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': ''},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_errored == 1
        assert result.rows_staged == 0
        assert len(result.errors) == 1
        assert 'missing client' in result.errors[0].lower()
        assert db.session.query(StagingLobbyingEntry).count() == 0


# ---------------------------------------------------------------------------
# 10. Missing firm field → errored
# ---------------------------------------------------------------------------

class TestMissingFirmField:
    def test_empty_firm_counted_as_error(self, app, tmp_path):
        """Row with client but empty firm → rows_errored=1, nothing staged."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': '',
             'Client name': 'Hanson UK'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_errored == 1
        assert result.rows_staged == 0
        assert len(result.errors) == 1
        assert 'missing firm' in result.errors[0].lower()
        assert db.session.query(StagingLobbyingEntry).count() == 0


# ---------------------------------------------------------------------------
# 11. Placeholder client → row skipped entirely
# ---------------------------------------------------------------------------

class TestPlaceholderFilter:
    def test_intentionally_blank_client_skipped(self, app, tmp_path):
        """client_name='Intentionally Blank' → rows_skipped_placeholder=1, nothing staged."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Symbio Impact Ltd',
             'Client name': 'Intentionally Blank'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_skipped_placeholder == 1
        assert result.rows_staged == 0
        assert result.rows_errored == 0
        assert db.session.query(StagingLobbyingEntry).count() == 0

    def test_placeholder_firm_also_skipped(self, app, tmp_path):
        """firm_name matching a placeholder → row skipped."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'None',
             'Client name': 'Hanson UK'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_skipped_placeholder == 1
        assert result.rows_staged == 0
        assert db.session.query(StagingLobbyingEntry).count() == 0

    def test_placeholder_case_insensitive(self, app, tmp_path):
        """Placeholder detection is case-insensitive."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'intentionally blank'},
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'N/A'},
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'NIL RETURN'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_skipped_placeholder == 3
        assert result.rows_staged == 0
        assert db.session.query(StagingLobbyingEntry).count() == 0

    def test_real_client_alongside_placeholder_both_handled(self, app, tmp_path):
        """Placeholder row skipped; adjacent real row still staged (2 records)."""
        from stakeholder_directory.ingesters.lobbying_register import ingest_lobbying_register
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry
        from extensions import db

        path = _write_csv(tmp_path, [
            {'Registered organisation name': 'Symbio Impact Ltd',
             'Client name': 'Intentionally Blank'},
            {'Registered organisation name': 'Copper Consultancy Limited',
             'Client name': 'Hanson UK'},
        ])
        result = ingest_lobbying_register(str(path), QUARTER, Q_START, Q_END)

        assert result.rows_skipped_placeholder == 1
        assert result.rows_staged == 2
        assert db.session.query(StagingLobbyingEntry).count() == 2
