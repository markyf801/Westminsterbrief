"""
Tests for the committee evidence ingester.

Run: python -m pytest stakeholder_directory/tests/test_committee_evidence.py -v
"""
import json
import pytest
import requests as req_module
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

FIXTURES_DIR = Path(__file__).parent / 'fixtures'
START = date(2026, 1, 1)
END = date(2026, 3, 31)
COMMITTEE_IDS = [203]


def _load_fixture(key: str) -> dict:
    with open(FIXTURES_DIR / 'committee_publications_sample.json') as f:
        return json.load(f)[key]


def _mock_resp(data: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = data
    if status >= 400:
        m.raise_for_status.side_effect = req_module.exceptions.HTTPError(f'HTTP {status}')
    else:
        m.raise_for_status.return_value = None
    return m


_ORAL_EMPTY = {'totalResults': 0, 'items': []}
_WRITTEN_EMPTY = {'totalResults': 0, 'items': []}


def _make_mock_get(committees_data, oral_data=None, written_data=None):
    """Return a requests.get replacement that dispatches by URL."""
    oral = oral_data if oral_data is not None else _ORAL_EMPTY
    written = written_data if written_data is not None else _WRITTEN_EMPTY

    def mock_get(url, params=None, **kwargs):
        if 'Committees' in url:
            return _mock_resp(committees_data)
        if 'OralEvidence' in url:
            return _mock_resp(oral)
        return _mock_resp(written)

    return mock_get


# ---------------------------------------------------------------------------
# 1. Oral evidence — happy path
# ---------------------------------------------------------------------------

class TestOralHappyPath:
    def test_stages_org_witness(self, app, monkeypatch):
        """Single org witness in oral evidence → 1 staging row with correct fields."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_happy_path')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, oral_data=oral))

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.rows_staged == 1
        assert result.errors == []
        rows = db.session.query(StagingCommitteeEvidence).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.raw_organisation_name == 'Universities UK'
        assert row.committee_id == 203
        assert row.committee_name == 'Education, Children and Families Committee'
        assert row.publication_type == 'oral_evidence_committee'
        assert row.publication_id == 10001
        assert row.processing_status == 'pending'
        assert row.inquiry_title == 'Student Finance Inquiry'


# ---------------------------------------------------------------------------
# 2. Written evidence — happy path
# ---------------------------------------------------------------------------

class TestWrittenHappyPath:
    def test_stages_org_submitter(self, app, monkeypatch):
        """Single org in written evidence → 1 staging row."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        written = _load_fixture('written_evidence_happy_path')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, written_data=written))

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.rows_staged == 1
        assert result.errors == []
        rows = db.session.query(StagingCommitteeEvidence).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.raw_organisation_name == 'National Union of Students'
        assert row.publication_type == 'written_evidence_committee'
        assert row.publication_id == 20001


# ---------------------------------------------------------------------------
# 3. Multi-org semicolon
# ---------------------------------------------------------------------------

class TestMultiOrgSemicolon:
    def test_semicolon_name_creates_two_rows(self, app, monkeypatch):
        """Org name 'NAHT; NASUWT' is split on '; ' into two staging rows."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_multi_org')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, oral_data=oral))

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.rows_staged == 2
        assert result.errors == []
        rows = db.session.query(StagingCommitteeEvidence).order_by(
            StagingCommitteeEvidence.raw_organisation_name
        ).all()
        names = [r.raw_organisation_name for r in rows]
        assert 'NAHT' in names
        assert 'NASUWT' in names
        # Both rows share the same publication_id
        assert rows[0].publication_id == rows[1].publication_id


# ---------------------------------------------------------------------------
# 4. Internal government rejection
# ---------------------------------------------------------------------------

class TestInternalGovtRejection:
    def test_internal_govt_org_skipped(self, app, monkeypatch):
        """'Department for Education' is on the internal-govt list → not staged."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_internal_govt')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, oral_data=oral))

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.rows_staged == 0
        assert result.rows_skipped_internal_govt == 1
        assert result.errors == []
        assert db.session.query(StagingCommitteeEvidence).count() == 0


# ---------------------------------------------------------------------------
# 5. Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_fetches_all_pages(self, app, monkeypatch):
        """totalResults=3 with page_size=2 triggers a second API call."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        page1 = _load_fixture('oral_evidence_page1')   # totalResults=3, 2 items
        page2 = _load_fixture('oral_evidence_page2')   # totalResults=3, 1 item

        oral_call_count = {'n': 0}

        def mock_get(url, params=None, **kwargs):
            if 'Committees' in url:
                return _mock_resp(committees)
            if 'OralEvidence' in url:
                oral_call_count['n'] += 1
                return _mock_resp(page1 if oral_call_count['n'] == 1 else page2)
            return _mock_resp(_WRITTEN_EMPTY)

        monkeypatch.setattr(req_module, 'get', mock_get)

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.rows_staged == 3
        assert oral_call_count['n'] == 2
        assert db.session.query(StagingCommitteeEvidence).count() == 3


# ---------------------------------------------------------------------------
# 6. Date parameters passed to API
# ---------------------------------------------------------------------------

class TestDateParams:
    def test_start_and_end_date_sent(self, app, monkeypatch):
        """StartDate and EndDate are included in the OralEvidence API request."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence

        committees = _load_fixture('committees')
        captured: list[dict] = []

        def mock_get(url, params=None, **kwargs):
            if params:
                captured.append({'url': url, 'params': dict(params)})
            if 'Committees' in url:
                return _mock_resp(committees)
            return _mock_resp(_ORAL_EMPTY)

        monkeypatch.setattr(req_module, 'get', mock_get)

        ingest_committee_evidence([203], date(2025, 5, 1), date(2026, 4, 25))

        oral_calls = [c for c in captured if 'OralEvidence' in c['url']]
        assert oral_calls, "No OralEvidence calls made"
        assert oral_calls[0]['params']['StartDate'] == '2025-05-01'
        assert oral_calls[0]['params']['EndDate'] == '2026-04-25'
        assert oral_calls[0]['params']['committeeId'] == 203


# ---------------------------------------------------------------------------
# 7. Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_run_skips_duplicates(self, app, monkeypatch):
        """Running twice against the same data: second run stages 0, skips 1."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_happy_path')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, oral_data=oral))

        result1 = ingest_committee_evidence(COMMITTEE_IDS, START, END)
        result2 = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result1.rows_staged == 1
        assert result2.rows_staged == 0
        assert result2.rows_skipped_duplicate == 1
        assert db.session.query(StagingCommitteeEvidence).count() == 1


# ---------------------------------------------------------------------------
# 8. Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_counts_but_writes_nothing(self, app, monkeypatch):
        """dry_run=True: result shows 1 would-be staged row, but DB has 0."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_happy_path')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, oral_data=oral))

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END, dry_run=True)

        assert result.rows_staged == 1
        assert db.session.query(StagingCommitteeEvidence).count() == 0
        assert result.errors == []


# ---------------------------------------------------------------------------
# 9. API 503 retry
# ---------------------------------------------------------------------------

class TestApiRetry:
    def test_503_retried_and_succeeds(self, app, monkeypatch):
        """First OralEvidence call returns 503; second returns 200 with data."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_happy_path')
        oral_call_count = {'n': 0}

        def mock_get(url, params=None, **kwargs):
            if 'Committees' in url:
                return _mock_resp(committees)
            if 'OralEvidence' in url:
                oral_call_count['n'] += 1
                if oral_call_count['n'] == 1:
                    return _mock_resp({}, status=503)
                return _mock_resp(oral)
            return _mock_resp(_WRITTEN_EMPTY)

        monkeypatch.setattr(req_module, 'get', mock_get)
        monkeypatch.setattr('time.sleep', lambda s: None)

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.rows_staged == 1
        assert oral_call_count['n'] == 2
        assert result.errors == []
        assert db.session.query(StagingCommitteeEvidence).count() == 1


# ---------------------------------------------------------------------------
# 10. Empty witness list
# ---------------------------------------------------------------------------

class TestEmptyWitnesses:
    def test_empty_witnesses_no_error_no_rows(self, app, monkeypatch):
        """Publication with witnesses=[] → publications_fetched=1, rows_staged=0, no errors."""
        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence
        from extensions import db

        committees = _load_fixture('committees')
        oral = _load_fixture('oral_evidence_empty_witnesses')
        monkeypatch.setattr(req_module, 'get', _make_mock_get(committees, oral_data=oral))

        result = ingest_committee_evidence(COMMITTEE_IDS, START, END)

        assert result.publications_fetched == 1
        assert result.rows_staged == 0
        assert result.errors == []
        assert db.session.query(StagingCommitteeEvidence).count() == 0
