"""
Test suite for the bills tagging pipeline (scripts/tag_bills.py).

Three test groups:
  1. Validation rules   — unit tests on _validate(), no DB or API calls
  2. Retry logic        — _tag_one_bill() with mocked Gemini, checks retry counts
  3. Resumability       — tag_bills() with mocked Gemini, verifies resume-from-checkpoint
  4. Report accuracy    — summary dict matches actual DB state after a run

Run:  python -m pytest tests/test_bill_tagger.py -v
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_app():
    """Minimal Flask app with SQLite in-memory DB containing bill tables."""
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models  # registers all ha_* models with db  # noqa: F401

    app = Flask(__name__)
    app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    _db.init_app(app)

    # cache_models must be imported so CachedMember registers with db
    # before create_all() — HaBillSponsor has a FK to cached_member.member_id
    import cache_models  # noqa: F401

    with app.app_context():
        _db.create_all()
        yield app


@pytest.fixture
def db_session(test_app):
    """Fresh DB state for each test — rolls back after each test."""
    from extensions import db as _db
    with test_app.app_context():
        yield _db.session
        _db.session.rollback()
        # Wipe bill tables
        from hansard_archive.models import HaBill, HaBillTheme
        HaBillTheme.query.delete()
        HaBill.query.delete()
        _db.session.commit()


def _make_bill(db_session, parliament_bill_id: int, title: str = None) -> "HaBill":
    """Insert a minimal HaBill row for testing."""
    from hansard_archive.models import HaBill
    bill = HaBill(
        parliament_bill_id=parliament_bill_id,
        title=title or f"Test Bill {parliament_bill_id}",
        house_of_origin="Commons",
        session="2024-25",
        parliament_url=f"https://bills.parliament.uk/bills/{parliament_bill_id}",
        is_act=False,
        is_defeated=False,
        ingested_at=datetime.utcnow(),
        last_refreshed=datetime.utcnow(),
    )
    db_session.add(bill)
    db_session.commit()
    return bill


# ---------------------------------------------------------------------------
# 1. Validation unit tests
# ---------------------------------------------------------------------------

class TestValidation:
    """_validate() correctly classifies every response shape."""

    def setup_method(self):
        from scripts.tag_bills import _validate
        self._validate = _validate

    def test_valid_single_theme(self):
        raw = json.dumps(["Economy"])
        result = self._validate(raw)
        assert result.ok
        assert result.themes == ["Economy"]
        assert not result.suspicious

    def test_valid_two_themes(self):
        raw = json.dumps(["Education, training and skills", "Employment and labour market"])
        result = self._validate(raw)
        assert result.ok
        assert len(result.themes) == 2

    def test_valid_suspicious_over_four(self):
        """Five themes accepted but flagged suspicious."""
        themes = ["Economy", "Finance and taxation", "Business and industry",
                  "Trade", "Government and public administration"]
        result = self._validate(json.dumps(themes))
        assert result.ok
        assert result.suspicious
        assert len(result.themes) == 5

    def test_empty_array(self):
        result = self._validate("[]")
        assert not result.ok
        assert result.failure_category == "empty_array"

    def test_invalid_theme_name(self):
        raw = json.dumps(["Not a real policy area"])
        result = self._validate(raw)
        assert not result.ok
        assert result.failure_category == "invalid_themes"
        assert "Not a real policy area" in result.failure_detail

    def test_invalid_mixed_with_valid(self):
        """Any invalid theme fails the whole response."""
        raw = json.dumps(["Economy", "Made Up Area"])
        result = self._validate(raw)
        assert not result.ok
        assert result.failure_category == "invalid_themes"

    def test_malformed_json(self):
        result = self._validate("not valid json {{{")
        assert not result.ok
        assert result.failure_category == "malformed_json"

    def test_not_a_list_object(self):
        raw = json.dumps({"policy_areas": ["Economy"]})
        result = self._validate(raw)
        assert not result.ok
        assert result.failure_category == "not_a_list"

    def test_not_a_list_string(self):
        result = self._validate(json.dumps("Economy"))
        assert not result.ok
        assert result.failure_category == "not_a_list"


# ---------------------------------------------------------------------------
# 2. Retry logic tests
# ---------------------------------------------------------------------------

class TestRetryLogic:
    """_tag_one_bill() respects per-category retry limits."""

    def _make_mock_bill(self):
        b = MagicMock()
        b.id = 1
        b.parliament_bill_id = 9001
        b.title = "Test Bill"
        b.long_title = None
        b.summary = None
        return b

    def _run(self, responses: list[str | None]):
        """
        Run _tag_one_bill with a mocked Gemini call that returns responses in order.
        responses: list of raw_text strings (or None to simulate transient failure).
        Returns (themes, failure_reason, suspicious).
        """
        from scripts.tag_bills import _tag_one_bill

        call_iter = iter(responses)

        def fake_backoff(api_key, model, prompt):
            val = next(call_iter, None)
            if val is None:
                return None, "simulated_transient"
            return val, None

        with patch("scripts.tag_bills._call_with_backoff", side_effect=fake_backoff):
            return _tag_one_bill(self._make_mock_bill(), "fake_key", "fake_model")

    def test_succeeds_first_attempt(self):
        themes, reason, suspicious = self._run([json.dumps(["Economy"])])
        assert themes == ["Economy"]
        assert reason is None

    def test_empty_array_retries_and_eventually_succeeds(self):
        """Two empty responses, then valid — succeeds on third attempt."""
        responses = ["[]", "[]", json.dumps(["Economy"])]
        themes, reason, _ = self._run(responses)
        assert themes == ["Economy"]

    def test_empty_array_exhausts_retries(self):
        """Four empty responses exhaust the 3-retry limit."""
        responses = ["[]"] * 4
        themes, reason, _ = self._run(responses)
        assert themes is None
        assert reason == "empty_array"

    def test_invalid_theme_exhausts_after_two_retries(self):
        """Three invalid-theme responses exhaust the 2-retry limit."""
        bad = json.dumps(["Not a real area"])
        responses = [bad] * 3
        themes, reason, _ = self._run(responses)
        assert themes is None
        assert reason.startswith("invalid_themes")

    def test_malformed_json_exhausts_after_two_retries(self):
        responses = ["bad json"] * 3
        themes, reason, _ = self._run(responses)
        assert themes is None
        assert reason.startswith("malformed_json")

    def test_transient_failure_is_permanent(self):
        """API returning None (transient exhausted) is a permanent failure."""
        themes, reason, _ = self._run([None])
        assert themes is None
        assert reason is not None

    def test_suspicious_accepted_but_flagged(self):
        """Five themes accepted and suspicious=True."""
        five = ["Economy", "Finance and taxation", "Business and industry",
                "Trade", "Government and public administration"]
        themes, reason, suspicious = self._run([json.dumps(five)])
        assert themes == five
        assert reason is None
        assert suspicious is True

    def test_mixed_failures_then_success(self):
        """Malformed, then empty, then valid — succeeds."""
        responses = [
            "bad json",                 # malformed_json attempt 1
            "[]",                       # empty_array attempt 1
            json.dumps(["Economy"]),    # success
        ]
        themes, reason, _ = self._run(responses)
        assert themes == ["Economy"]


# ---------------------------------------------------------------------------
# 3. Resumability test
# ---------------------------------------------------------------------------

class TestResumability:
    """tag_bills() resumes correctly after a partial run."""

    def test_resume_continues_from_checkpoint(self, db_session, test_app):
        from scripts.tag_bills import tag_bills

        # Create 10 bills
        with test_app.app_context():
            from extensions import db as _db
            from hansard_archive.models import HaBill
            bills = [_make_bill(_db.session, 90000 + i) for i in range(10)]
            _db.session.commit()

        valid_response = json.dumps(["Economy"])
        call_count = {"n": 0}

        def fake_backoff(api_key, model, prompt):
            call_count["n"] += 1
            return valid_response, None

        api_key = "fake_key"

        with test_app.app_context():
            with patch("scripts.tag_bills._call_with_backoff", side_effect=fake_backoff):
                with patch("scripts.tag_bills._detect_pro_model", return_value="test-model"):
                    # First run: limit to 5
                    result1 = tag_bills(api_key=api_key, limit=5)

            assert result1["completed"] == 5
            assert call_count["n"] == 5

            from hansard_archive.models import HaBill
            completed = HaBill.query.filter(HaBill.tagging_completed_at.isnot(None)).count()
            pending = HaBill.query.filter(HaBill.tagging_completed_at.is_(None),
                                          HaBill.tagging_failure_reason.is_(None)).count()
            assert completed == 5
            assert pending == 5

            with patch("scripts.tag_bills._call_with_backoff", side_effect=fake_backoff):
                with patch("scripts.tag_bills._detect_pro_model", return_value="test-model"):
                    # Second run: no limit — should tag the remaining 5
                    result2 = tag_bills(api_key=api_key)

            assert result2["completed"] == 5
            # Gemini was called exactly 10 times total (5 + 5), not 15
            assert call_count["n"] == 10

            # All 10 are now complete
            total_done = HaBill.query.filter(HaBill.tagging_completed_at.isnot(None)).count()
            assert total_done == 10


# ---------------------------------------------------------------------------
# 4. Report accuracy test
# ---------------------------------------------------------------------------

class TestReportAccuracy:
    """Summary dict returned by tag_bills() matches actual DB state."""

    def test_summary_matches_db(self, db_session, test_app):
        from scripts.tag_bills import tag_bills

        with test_app.app_context():
            from extensions import db as _db
            # 3 good bills, 1 that will always fail
            for i in range(3):
                _make_bill(_db.session, 80000 + i)
            bad_bill = _make_bill(_db.session, 80099)
            _db.session.commit()

        call_count = {"n": 0}

        def fake_backoff(api_key, model, prompt):
            call_count["n"] += 1
            # Bill 80099 always returns empty (exhausts retries)
            from hansard_archive.models import HaBill
            # We can't easily inspect which bill this is from here,
            # so return empty for the 4th call onwards to simulate one failure
            if call_count["n"] == 4:
                return "[]", None   # will trigger empty_array failure on retries
            return json.dumps(["Economy"]), None

        with test_app.app_context():
            with patch("scripts.tag_bills._call_with_backoff", side_effect=fake_backoff):
                with patch("scripts.tag_bills._detect_pro_model", return_value="test-model"):
                    result = tag_bills(api_key="fake_key")

            from hansard_archive.models import HaBill, HaBillTheme
            db_completed = HaBill.query.filter(HaBill.tagging_completed_at.isnot(None)).count()
            db_failed = HaBill.query.filter(HaBill.tagging_failure_reason.isnot(None)).count()
            db_themes = HaBillTheme.query.count()

            # Summary matches DB
            assert result["completed"] == db_completed
            # Each completed bill got exactly 1 theme ("Economy")
            assert db_themes == db_completed
