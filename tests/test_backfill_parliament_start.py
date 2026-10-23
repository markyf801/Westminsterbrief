"""
Tests for scripts/backfill_parliament_start.py — mocked Hansard HTTP + in-memory SQLite.

Covers: newest-first ingest with per-(day, house) checkpoint, idempotent re-runs,
resume, dry run writes nothing, bounded loops on repeated API failure, 429 abort,
session-fetch retry, tag cap + catch-up, cron-window guard (also mid-tagging),
stale run records closed.

Run: python -m pytest tests/test_backfill_parliament_start.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import backfill_parliament_start as bps  # noqa: E402
from hansard_archive import ingestion, tagger  # noqa: E402

REAL_GEMINI_HEALTHY = bps.gemini_healthy

# A Saturday, so the cron-window guard never fires unless a test asks for it.
SATURDAY_NOON = datetime(2026, 10, 24, 12, 0)

# Two sitting days (Mon 2 Dec, Tue 3 Dec 2024) and a non-sitting Sunday 1 Dec.
SITTINGS = {
    ("2024-12-03", "Commons"): ["C3A", "C3B"],
    ("2024-12-03", "Lords"): ["L3A"],
    ("2024-12-02", "Commons"): ["C2A", "C2B", "C2C"],
    ("2024-12-02", "Lords"): ["L2A"],
}


def _session_json(day: str, chain: list[str], i: int) -> dict:
    ext_id = chain[i]
    return {
        "Overview": {
            "ExtId": ext_id,
            "Title": f"Debate {ext_id}",
            "Date": f"{day}T00:00:00",
            "Location": "Commons Chamber",
            "HRSTag": "hs_2cDebatedMotion",
            "NextDebateExtId": chain[i + 1] if i + 1 < len(chain) else None,
            "PreviousDebateExtId": chain[i - 1] if i > 0 else None,
        },
        "Items": [
            {"ItemType": "Contribution", "HRSTag": "hs_Para", "MemberId": 1,
             "MemberName": "Ann Example (Somewhere) (Lab)", "Value": f"<p>Speech in {ext_id}.</p>"},
            {"ItemType": "Contribution", "HRSTag": "hs_Para", "MemberId": 2,
             "MemberName": "Bob Example (Elsewhere) (Con)", "Value": "<p>A reply.</p>"},
        ],
        "ChildDebates": [],
    }


class FakeResp:
    def __init__(self, url: str, status: int = 200, payload=None):
        self.url = url
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            kind = "Client" if self.status_code < 500 else "Server"
            raise requests.HTTPError(f"{self.status_code} {kind} Error: x for url: {self.url}",
                                     response=self)


class FakeHansard:
    """Stands in for requests.get inside hansard_archive.ingestion."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_fetch: dict[str, int] = {}   # ext_id -> remaining timeouts (-1 = always)
        self.search_status: int | None = None
        self.always_error = False

    def __call__(self, url, params=None, timeout=None):
        self.calls.append(url)
        if self.always_error:
            raise requests.ConnectionError("connection reset")
        if url.endswith("/search/debates.json"):
            if self.search_status:
                return FakeResp(url, self.search_status)
            if params.get("queryParameters.searchTerm"):
                return FakeResp(url, payload={"Results": []})
            key = (params["queryParameters.startDate"], params["queryParameters.house"])
            chain = SITTINGS.get(key, [])
            results = [{"DebateSectionExtId": chain[0], "Title": "x"}] if chain else []
            return FakeResp(url, payload={"Results": results, "TotalResultCount": len(chain)})
        ext_id = url.rsplit("/", 1)[-1].removesuffix(".json")
        remaining = self.fail_fetch.get(ext_id, 0)
        if remaining:
            if remaining > 0:
                self.fail_fetch[ext_id] = remaining - 1
            raise requests.Timeout("Read timed out. (read timeout=30)")
        for (day, _house), chain in SITTINGS.items():
            if ext_id in chain:
                return FakeResp(url, payload=_session_json(day, chain, chain.index(ext_id)))
        return FakeResp(url, 404)

    def fetches(self) -> int:
        return sum(1 for u in self.calls if "/debates/debate/" in u)


@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models  # noqa: F401
    import cache_models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True, SECRET_KEY="test")
    _db.init_app(app)
    return app


@pytest.fixture
def env(test_app, monkeypatch):
    from extensions import db as _db
    fake = FakeHansard()
    monkeypatch.setattr(ingestion.requests, "get", fake)
    monkeypatch.setattr(ingestion.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake.gemini_up = True                    # what the tagging health probe reports
    monkeypatch.setattr(bps, "gemini_healthy", lambda api_key: fake.gemini_up)
    with test_app.app_context():
        _db.create_all()
        yield fake
        _db.session.remove()
        _db.drop_all()


def _run(*argv, now=SATURDAY_NOON):
    args = bps.build_parser().parse_args(
        ["--since", "2024-12-01", "--until", "2024-12-03", *argv])
    return bps.run(args, sleep=lambda s: None, now=lambda: now)


def _counts():
    from hansard_archive.models import HansardContribution, HansardSession, HansardSessionTheme
    return (HansardSession.query.count(), HansardContribution.query.count(),
            HansardSessionTheme.query.count())


def _checkpoint():
    from hansard_archive.models import HaCronRun
    rec = (HaCronRun.query.filter_by(service_name=bps.SERVICE_NAME)
           .order_by(HaCronRun.id.desc()).first())
    return rec, (json.loads(rec.notes) if rec and rec.notes else {})


@pytest.fixture
def fake_tagger(monkeypatch):
    from extensions import db as _db
    from hansard_archive.models import HansardSessionTheme
    calls = []

    def tag(session_id, api_key=None):
        calls.append(session_id)
        _db.session.add(HansardSessionTheme(session_id=session_id, theme="Education",
                                            theme_type="policy_area"))
        _db.session.commit()
        return 1

    monkeypatch.setattr(bps, "tag_session", tag)
    return calls


# ---------------------------------------------------------------------------


def test_ingests_range_and_checkpoints(env):
    assert _run("--no-tag") == bps.EXIT_OK
    assert _counts() == (7, 14, 0)
    rec, state = _checkpoint()
    assert rec.status == "ok"
    assert state["frontier"] == {"Commons": "2024-12-01", "Lords": "2024-12-01"}


def test_newest_first_order(env):
    _run("--no-tag", "--max-days", "1")
    from hansard_archive.models import HansardSession
    days = {s.date for s in HansardSession.query.all()}
    assert days == {date(2024, 12, 3)}
    _, state = _checkpoint()
    assert state["frontier"]["Commons"] == "2024-12-03"


def test_rerun_is_idempotent(env):
    _run("--no-tag")
    assert _run("--no-tag", "--no-resume") == bps.EXIT_OK
    assert _counts() == (7, 14, 0)


def test_resume_skips_completed_days(env):
    _run("--no-tag", "--max-days", "1")
    before = env.fetches()
    assert _run("--no-tag") == bps.EXIT_OK
    # Only 2 Dec sessions (3 Commons + 1 Lords) fetched on the resumed run.
    assert env.fetches() - before == 4
    assert _counts()[0] == 7


def test_dry_run_writes_nothing(env, capsys):
    from hansard_archive.models import HaCronRun
    assert _run("--dry-run") == bps.EXIT_OK
    assert _counts() == (0, 0, 0)
    assert HaCronRun.query.count() == 0
    out = capsys.readouterr().out
    assert "would insert:            7" in out


def test_dry_run_predicts_existing(env, capsys):
    _run("--no-tag", "--max-days", "1")
    capsys.readouterr()
    _run("--dry-run", "--no-resume")
    out = capsys.readouterr().out
    assert "would insert:            4" in out
    assert "already stored:          3" in out


def test_repeated_api_failure_exits_nonzero_and_is_bounded(env):
    env.always_error = True
    assert _run("--no-tag") == bps.EXIT_FAILED
    # 1st day only: DAY_ATTEMPTS x (seed search: 2 tries in _api_get)
    assert len(env.calls) == bps.DAY_ATTEMPTS * 2
    rec, state = _checkpoint()
    assert rec.status == "failed"
    assert state["frontier"] == {"Commons": None, "Lords": None}


def test_rate_limit_aborts_immediately(env):
    env.search_status = 429
    assert _run("--no-tag") == bps.EXIT_REFUSED
    assert len(env.calls) == 1


def test_transient_session_timeout_is_retried(env):
    env.fail_fetch["C3B"] = 1          # one timeout, then fine
    assert _run("--no-tag") == bps.EXIT_OK
    assert _counts()[0] == 7


def test_persistent_session_failure_stops_without_advancing(env):
    env.fail_fetch["C3B"] = -1         # always times out
    assert _run("--no-tag") == bps.EXIT_FAILED
    assert env.fetches() <= bps.DAY_ATTEMPTS * (2 + bps.FETCH_ATTEMPTS)
    _, state = _checkpoint()
    assert state["frontier"]["Commons"] is None


def test_tagging_inline_with_cap_and_catch_up(env, fake_tagger):
    assert _run("--max-tag", "2") == bps.EXIT_OK
    assert len(fake_tagger) == 2
    _, state = _checkpoint()
    assert "--max-tag 2 reached" in state["stop_reason"]
    # Next run first catches up the untagged sessions already stored, then continues.
    assert _run("--max-tag", "50") == bps.EXIT_OK
    assert _counts() == (7, 14, 7)


def _api_error(final=False):
    def tag(session_id, api_key=None):
        raise tagger.GeminiCallFailed("HTTP 503: unavailable", final=final)
    return tag


def test_repeated_api_errors_stop_only_when_gemini_is_down(env, monkeypatch):
    monkeypatch.setattr(bps, "tag_session", _api_error())
    monkeypatch.setattr(bps, "MAX_CONSECUTIVE_TAG_FAILURES", 2)
    env.gemini_up = False
    assert _run() == bps.EXIT_TAG_FAILED
    _, state = _checkpoint()
    assert state["tag_failures"] == {}           # an API outage is not held against the items
    assert state["frontier"]["Commons"] == "2024-12-03"   # the ingested day is kept


def test_api_errors_with_gemini_up_count_against_the_items(env, monkeypatch):
    monkeypatch.setattr(bps, "tag_session", _api_error())
    monkeypatch.setattr(bps, "MAX_CONSECUTIVE_TAG_FAILURES", 2)
    assert _run() == bps.EXIT_OK                 # health check passes: carry on
    _, state = _checkpoint()
    assert set(state["tag_failures"]) == {str(i) for i in range(1, 8)}


def test_final_api_error_stops_at_once_and_counts_nothing(env, monkeypatch):
    calls = []

    def tag(session_id, api_key=None):
        calls.append(session_id)
        raise tagger.GeminiCallFailed("HTTP 400: bad schema", final=True)

    monkeypatch.setattr(bps, "tag_session", tag)
    assert _run() == bps.EXIT_TAG_FAILED
    assert len(calls) == 1
    _, state = _checkpoint()
    assert state["tag_failures"] == {} and "rejected" in state["stop_reason"]


def test_items_the_crons_gave_up_on_are_skipped(env, monkeypatch):
    from extensions import db as _db
    from hansard_archive.models import HaTagAttempt
    calls = _failing_tagger(monkeypatch, bad=set())
    _db.session.add(HaTagAttempt(item_kind="session", item_id=1, attempts=tagger.TAG_MAX_ATTEMPTS))
    _db.session.commit()
    assert _run() == bps.EXIT_OK
    assert 1 not in calls and len(calls) == 6


def test_missing_tagger_helper_fails_loudly(env, monkeypatch):
    monkeypatch.delattr(tagger, "_gemini_json")
    assert _run() == bps.EXIT_TAG_FAILED
    assert env.calls == []                        # stopped before any ingest


class _GeminiResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.mark.parametrize("status,healthy", [(200, True), (503, False), (400, False)])
def test_real_health_check_with_http_mocked(monkeypatch, status, healthy):
    """The real gemini_healthy -> tagger._gemini_json path; only HTTP is faked."""
    posts = []
    ok_body = {"candidates": [{"content": {"parts": [{"text": json.dumps(
        {"policy_areas": [], "topics": []})}]}}], "usageMetadata": {}}

    def post(url, headers=None, json=None, timeout=None):
        posts.append(url)
        return _GeminiResp(status, ok_body if status == 200 else {"error": "x"})

    monkeypatch.setattr(tagger.requests, "post", post)
    monkeypatch.setattr(tagger.time, "sleep", lambda s: None)
    monkeypatch.setenv("TAGGER_MODEL", "gemini-2.5-flash-lite")
    assert bps.gemini_healthy("test-key") is healthy
    assert posts and "gemini-2.5-flash-lite:generateContent" in posts[0]
    sent = []
    monkeypatch.setattr(tagger.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: sent.append(json) or post(url))
    bps.gemini_healthy("test-key")
    assert sent[0]["generationConfig"]["responseSchema"] == tagger._RESPONSE_SCHEMA


def _failing_tagger(monkeypatch, bad: set[int]):
    from extensions import db as _db
    from hansard_archive.models import HansardSessionTheme
    calls = []

    def tag(session_id, api_key=None):
        calls.append(session_id)
        if session_id in bad:
            return 0
        _db.session.add(HansardSessionTheme(session_id=session_id, theme="Education",
                                            theme_type="policy_area"))
        _db.session.commit()
        return 1

    monkeypatch.setattr(bps, "tag_session", tag)
    return calls


def test_untaggable_session_never_blocks_the_backfill(env, monkeypatch):
    calls = _failing_tagger(monkeypatch, bad={1})     # the first session stored
    assert _run() == bps.EXIT_OK
    assert _counts() == (7, 14, 6)
    _, state = _checkpoint()
    assert state["tag_failures"] == {"1": 1}
    assert calls.count(1) == 1                         # not retried within the run
    # Later runs: one more counted attempt, then skipped for good — never exit 3.
    assert _run("--tag-only") == bps.EXIT_OK
    assert _run("--tag-only") == bps.EXIT_OK
    assert calls.count(1) == 2
    _, state = _checkpoint()
    assert state["tag_failures"] == {"1": 2}


def test_many_untaggable_in_a_row_continue_when_gemini_is_up(env, monkeypatch):
    monkeypatch.setattr(bps, "MAX_CONSECUTIVE_TAG_FAILURES", 2)
    _failing_tagger(monkeypatch, bad={1, 2, 3})
    assert _run() == bps.EXIT_OK
    assert _counts()[2] == 4
    _, state = _checkpoint()
    assert set(state["tag_failures"]) == {"1", "2", "3"}


def test_too_many_skipped_items_stop_the_run(env, monkeypatch):
    monkeypatch.setattr(bps, "MAX_SKIPPED_ITEMS", 1)
    monkeypatch.setattr(bps, "TAG_MAX_ATTEMPTS", 1)
    _failing_tagger(monkeypatch, bad={1, 2})
    assert _run() == bps.EXIT_TAG_FAILED


def test_resume_ignores_a_run_that_died_before_saving(env):
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    _run("--no-tag", "--max-days", "1")
    _db.session.add(HaCronRun(service_name=bps.SERVICE_NAME, started_at=datetime(2026, 10, 24, 12, 30),
                              status="failed", notes=None))
    _db.session.commit()
    before = env.fetches()
    assert _run("--no-tag") == bps.EXIT_OK
    assert env.fetches() - before == 4                 # only 2 Dec re-walked, not 3 Dec


def test_missing_gemini_key_refuses_before_ingest(env, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    assert _run() == bps.EXIT_REFUSED
    assert env.calls == []


def test_tag_only(env, fake_tagger):
    _run("--no-tag")
    assert _run("--tag-only", "--max-tag", "3") == bps.EXIT_OK
    assert len(fake_tagger) == 3
    assert _run("--tag-only", "--dry-run") == bps.EXIT_OK
    assert len(fake_tagger) == 3


def test_stops_in_cron_window(env):
    tuesday_noon = datetime(2026, 10, 20, 12, 0)
    assert _run("--no-tag", now=tuesday_noon) == bps.EXIT_OK
    assert env.calls == []
    _, state = _checkpoint()
    assert state["stop_reason"].startswith("cron window")


def test_active_run_blocks_second_run(env):
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    _db.session.add(HaCronRun(service_name=bps.SERVICE_NAME, started_at=SATURDAY_NOON,
                              status="running",
                              notes=json.dumps({"heartbeat": SATURDAY_NOON.isoformat()})))
    _db.session.commit()
    assert _run("--no-tag") == bps.EXIT_BUSY


@pytest.mark.parametrize("moment,blocked", [
    (datetime(2026, 10, 20, 12, 0), True),    # Tue daytime: archive hourly
    (datetime(2026, 10, 20, 23, 40), False),  # Tue late night
    (datetime(2026, 10, 21, 2, 45), True),    # backup at 03:00 inside lookahead
    (datetime(2026, 10, 23, 18, 0), True),    # Fri archive hourly
    (datetime(2026, 10, 23, 20, 0), False),   # Fri evening
    (datetime(2026, 10, 24, 6, 40), True),    # Sat bills 06:30
    (datetime(2026, 10, 24, 12, 0), False),   # Sat midday
    (datetime(2026, 10, 25, 2, 0), True),     # Sun member cache
    (datetime(2026, 10, 25, 3, 40), True),    # Sun member-profile-refresh
    (datetime(2026, 10, 24, 3, 40), False),   # Sat between backup and EDM
    (datetime(2026, 10, 24, 4, 10), True),    # EDM 04:30 inside lookahead
    (datetime(2026, 10, 24, 7, 40), True),    # Sat health report 07:30
    (datetime(2026, 10, 24, 7, 50), False),   # Sat after the morning jobs
    (datetime(2026, 5, 2, 3, 40), True),      # 2nd of month: member-bio-refresh
])
def test_blocked_window(moment, blocked):
    assert (bps.blocked_window(moment) is not None) is blocked


def test_uses_the_shared_cron_schedule():
    from hansard_archive import cron_windows
    assert bps.blocked_window is cron_windows.blocked_window
    import backfill_pq_parliament_start as bpq
    assert bpq.blocked_window is cron_windows.blocked_window


def test_wrappers_restored_after_run(env):
    orig = ingestion._fetch_session_full, ingestion._collect_all_sessions_for_date
    _run("--no-tag")
    assert (ingestion._fetch_session_full, ingestion._collect_all_sessions_for_date) == orig


def test_cron_window_during_tagging_stops_cleanly(env, fake_tagger, monkeypatch):
    # Window opens after the first tagging call, mid-way through 3 Dec Commons
    # (2 sessions): stop at once, keep the ingested day.
    def fake_window(now, lookahead_minutes=bps.CRON_LOOKAHEAD_MINUTES):
        return "test window" if fake_tagger else None

    monkeypatch.setattr(bps, "blocked_window", fake_window)
    assert _run() == bps.EXIT_OK
    assert len(fake_tagger) == 1
    rec, state = _checkpoint()
    assert state["stop_reason"].startswith("cron window: test window")
    assert state["frontier"] == {"Commons": "2024-12-03", "Lords": None}
    assert rec.sessions_tagged == 1
    # The next run tags the leftovers first, then carries on.
    monkeypatch.setattr(bps, "blocked_window", lambda now, lookahead_minutes=0: None)
    assert _run() == bps.EXIT_OK
    assert _counts() == (7, 14, 7)


def test_checkpoint_saved_per_house(env, monkeypatch):
    # Lords seed search refused on the first day: Commons for that day is kept.
    real = env.__call__

    def lords_refused(url, params=None, timeout=None):
        if url.endswith("/search/debates.json") and params.get("queryParameters.house") == "Lords":
            return FakeResp(url, 429)
        return real(url, params=params, timeout=timeout)

    monkeypatch.setattr(ingestion.requests, "get", lords_refused)
    assert _run("--no-tag") == bps.EXIT_REFUSED
    _, state = _checkpoint()
    assert state["frontier"] == {"Commons": "2024-12-03", "Lords": None}


def test_stale_running_record_is_closed(env):
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    old = HaCronRun(service_name=bps.SERVICE_NAME, started_at=datetime(2026, 10, 24, 9, 0),
                    status="running",
                    notes=json.dumps({"heartbeat": "2026-10-24T09:00:00"}))
    _db.session.add(old)
    _db.session.commit()
    assert _run("--no-tag") == bps.EXIT_OK
    assert _db.session.get(HaCronRun, old.id).status == "failed"


def test_real_tagger_with_gemini_working_never_blocks(env, monkeypatch):
    """Real tag_session and health check; only Gemini's HTTP is faked. Gemini
    answers but verifies nothing for any session: the run still completes."""
    monkeypatch.setattr(bps, "gemini_healthy", REAL_GEMINI_HEALTHY)
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps(
        {"policy_areas": [], "topics": []})}]}}], "usageMetadata": {}}
    monkeypatch.setattr(tagger.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: _GeminiResp(200, body))
    monkeypatch.setattr(tagger.time, "sleep", lambda s: None)
    monkeypatch.setenv("TAGGER_MODEL", "gemini-2.5-flash-lite")
    for _ in range(3):
        assert _run() == bps.EXIT_OK
    _, state = _checkpoint()
    assert state["stop_reason"] == "range complete"
    assert _counts()[0] == 7
    assert all(n == bps.TAG_MAX_ATTEMPTS for n in state["tag_failures"].values())


def test_session_tagged_by_another_run_is_not_a_failure(capsys):
    """tag_session returns ALREADY_TAGGED when the hourly cron tagged it meanwhile."""
    results = {1: tagger.ALREADY_TAGGED, 2: 3}
    budget = bps.TagBudget(10, "", sleep=lambda s: None, tag_fn=results.get, probe=lambda: True)
    assert budget.tag([1, 2]) == 1
    assert budget.succeeded == 1
    assert budget.failures == {} and budget.failed_this_run == set() and budget.streak == []
    assert "session 1: already tagged by another run" in capsys.readouterr().out
