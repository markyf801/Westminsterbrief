"""
Chunked catch-up (scripts/ingest_pq_cron.py --answered-from/--answered-to
--chunk-days), the ingestor's retry / smaller-page / rate-limit rules, and the
scheduled cron still tagging after an API failure.

SQLite in-memory only; Parliament's API, the tagger and email are mocked.
"""

import importlib.util
import re
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
D = date.fromisoformat


@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from sqlalchemy import event
    from extensions import db as _db

    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    test_app = Flask(__name__)
    test_app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True,
    })
    _db.init_app(test_app)
    # Make pysqlite SAVEPOINTs behave as on Postgres (see test_pq_ingestor_identity).
    with test_app.app_context():
        engine = _db.engine

        @event.listens_for(engine, "connect")
        def _no_pysqlite_begin(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None

        @event.listens_for(engine, "begin")
        def _explicit_begin(conn):
            conn.exec_driver_sql("BEGIN")

    yield test_app


@pytest.fixture
def db(app):
    from extensions import db as _db
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def cron(app, monkeypatch):
    """The cron script as a module, with flask_app stubbed to the test app."""
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=app))
    spec = importlib.util.spec_from_file_location(
        "ingest_pq_cron_chunked_under_test", PROJECT_ROOT / "scripts" / "ingest_pq_cron.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    alerts = []
    monkeypatch.setattr(mod, "_send_failure_alert",
                        lambda service, subject, body: alerts.append((subject, body)))
    mod.alerts = alerts
    return mod


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Crash(BaseException):
    """Stands in for the container being killed mid-run."""


def _value(api_id, tabled, answered=None, answer=None):
    return {"value": {
        "id": api_id, "uin": str(api_id), "heading": "Schools",
        "questionText": "To ask the Secretary of State, what steps are being taken.",
        "answerText": answer, "dateTabled": f"{tabled}T00:00:00",
        "dateAnswered": f"{answered}T00:00:00" if answered else None,
        "house": "Commons", "askingMemberId": None, "askingMember": None,
        "answeringMemberId": None, "answeringMember": None,
        "answeringBodyName": "Department for Testing", "answeringBodyId": 7,
        "answerIsHolding": False, "isWithdrawn": False,
    }}


@pytest.fixture
def wq(monkeypatch):
    """Fake WQ API. state["rows"] are served by date window with skip/take and
    totalResults; state["behaviour"](params, n) may return a _Resp (or raise)
    for list call n (1-based) instead."""
    import requests
    import time
    from hansard_archive.pq_ingestor import WQ_API_BASE

    state = {"rows": [], "calls": [], "individual": {}, "individual_calls": [],
             "behaviour": None, "sleeps": []}

    def _matching(params):
        for kind, key in (("answered", "dateAnswered"), ("tabled", "dateTabled"),
                          ("corrected", "dateAnswerCorrected")):
            if f"{kind}WhenFrom" in params:
                lo, hi = params[f"{kind}WhenFrom"], params[f"{kind}WhenTo"]
                return [r for r in state["rows"]
                        if r["value"].get(key) and lo <= r["value"][key][:10] <= hi]
        raise AssertionError(f"no date window in {params}")

    def fake_get(url, params=None, timeout=None):
        if url == WQ_API_BASE:
            state["calls"].append(dict(params))
            if state["behaviour"] is not None:
                resp = state["behaviour"](params, len(state["calls"]))
                if resp is not None:
                    return resp
            rows = _matching(params)
            skip, take = params["skip"], params["take"]
            return _Resp({"totalResults": len(rows), "results": rows[skip:skip + take]})
        api_id = int(url.rsplit("/", 1)[1])
        state["individual_calls"].append(api_id)
        if api_id not in state["individual"]:
            return _Resp({}, state.get("individual_status", 404))
        return _Resp({"value": state["individual"][api_id]})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: state["sleeps"].append(s))
    return state


def _windows(calls, kind="answered"):
    return [(c[f"{kind}WhenFrom"], c[f"{kind}WhenTo"]) for c in calls]


def _run(cron, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["ingest_pq_cron.py", *args])
    try:
        cron.main()
    except SystemExit as exc:
        return exc.code
    return 0


CATCHUP = ("--answered-from", "2026-09-01", "--answered-to", "2026-09-21",
           "--chunk-days", "7", "--service-name", "pq-catchup", "--no-tag",
           "--skip-answer-fetch")
CHUNKS = [("2026-09-15", "2026-09-21"), ("2026-09-08", "2026-09-14"),
          ("2026-09-01", "2026-09-07")]


def _three_weeks():
    return [_value(1000 + i, "2026-08-20", answered=f"2026-09-{i + 1:02d}",
                   answer="<p>Answer</p>") for i in range(21)]


def _runs():
    """Snapshot of every run record, oldest first (ends the read transaction)."""
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    out = [types.SimpleNamespace(id=r.id, status=r.status, notes=r.notes,
                                 started_at=r.started_at, ingested=r.sessions_ingested)
           for r in HaCronRun.query.order_by(HaCronRun.id).all()]
    _db.session.rollback()
    return out


# ── chunk plan and progress notes ────────────────────────────────────────────

@pytest.mark.parametrize("start,end,days", [
    ("2025-05-01", "2026-10-23", 7), ("2026-09-01", "2026-09-21", 7),
    ("2026-09-01", "2026-09-01", 7), ("2026-02-25", "2026-03-03", 1),
    ("2026-01-01", "2026-01-31", 31), ("2026-01-01", "2026-02-01", 31),
])
def test_chunks_cover_the_period_exactly_once_newest_first(start, end, days):
    from hansard_archive.pq_chunks import plan_chunks
    chunks = plan_chunks(D(start), D(end), days)
    assert chunks[0][1] == D(end) and chunks[-1][0] == D(start)
    for frm, to in chunks:
        assert frm <= to and (to - frm).days + 1 <= days
    for newer, older in zip(chunks, chunks[1:]):
        assert older[1] == newer[0] - timedelta(days=1)       # no gap, no overlap
    covered = sorted(d for frm, to in chunks for d in
                     (frm + timedelta(days=i) for i in range((to - frm).days + 1)))
    assert covered == [D(start) + timedelta(days=i)
                       for i in range((D(end) - D(start)).days + 1)]


def test_catchup_plan_for_the_stale_answer_backlog():
    from hansard_archive.pq_chunks import plan_chunks
    chunks = plan_chunks(D("2025-05-01"), D("2026-10-23"), 7)
    assert len(chunks) == 78
    assert chunks[0] == (D("2026-10-17"), D("2026-10-23"))
    assert chunks[-1] == (D("2025-05-01"), D("2025-05-02"))   # oldest one is short


def test_progress_notes_round_trip():
    from hansard_archive.pq_chunks import ChunkProgress, last_beat, parse_progress
    prog = ChunkProgress.fresh("answered", D("2025-05-01"), D("2026-10-23"), 7)
    prog.advance_past(prog.chunks()[0])
    prog.mark(prog.chunks()[0], False)
    prog.add_short(prog.chunks()[0])
    prog.totals.update(ins=3, upd=40, same=900, fetch_failed=2)
    beat = datetime(2026, 10, 23, 12, 34, 56)
    notes = prog.format(beat)
    back = parse_progress(notes + " | tagging: eligible=0")
    assert back == prog and last_beat(notes) == beat
    assert back.remaining()[0] == prog.chunks()[1] and back.failed_chunks() == [prog.chunks()[0]]
    assert back.done_count() == 0 and not back.is_complete()
    assert parse_progress("quick-check: nothing new") is None


# ── chunked runs ─────────────────────────────────────────────────────────────

def test_chunked_run_walks_each_window_shallowly(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    wq["rows"] = _three_weeks()

    assert _run(cron, monkeypatch, *CATCHUP) == 0

    assert _windows(wq["calls"]) == CHUNKS
    assert {c["skip"] for c in wq["calls"]} == {0} and {c["take"] for c in wq["calls"]} == {500}
    assert HaPQ.query.count() == 21
    db.session.rollback()
    [run] = _runs()
    assert run.status == "ok" and run.ingested == 21
    assert run.notes.startswith("pq-chunked: answered 2026-09-01..2026-09-21 every 7d "
                                "newest-first | next=end | done=3/3 | failed=- | short=-")


def test_stale_answer_is_completed_from_the_individual_record(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    db.session.add(HaPQ(api_id=1, uin="1", tabled_date=D("2026-08-20"), is_answered=False,
                        question_text="To ask the Secretary of State, what steps are being taken.",
                        is_holding=False, is_withdrawn=False))
    db.session.commit()
    wq["rows"] = [_value(1, "2026-08-20", answered="2026-09-03", answer="<p>Short</p>")]
    wq["individual"] = {1: {"questionText": "To ask the Secretary of State, what steps are "
                                            "being taken.",
                            "answerText": "<p>The full answer.</p>", "answerIsHolding": False,
                            "isWithdrawn": False, "dateAnswered": "2026-09-03T00:00:00"}}
    args = [a for a in CATCHUP if a != "--skip-answer-fetch"]

    assert _run(cron, monkeypatch, *args) == 0

    row = HaPQ.query.filter_by(api_id=1).one()
    assert row.answer_text == "<p>The full answer.</p>" and row.answer_date == D("2026-09-03")
    assert wq["individual_calls"] == [1]


def test_crashed_run_resumes_from_the_chunk_it_was_on(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    wq["rows"] = _three_weeks()

    def crash_on_second_chunk(params, n):
        if params["answeredWhenFrom"] == "2026-09-08":
            raise _Crash()
    wq["behaviour"] = crash_on_second_chunk
    with pytest.raises(_Crash):
        _run(cron, monkeypatch, *CATCHUP)
    db.session.rollback()
    [crashed] = _runs()
    assert crashed.status == "running" and "| next=2026-09-08 |" in crashed.notes

    # Two hours later, with no heartbeat since, the same command is run again.
    old = datetime.utcnow() - timedelta(hours=3)
    HaCronRun.query.filter_by(id=crashed.id).update({
        "started_at": old,
        "notes": re.sub(r"beat=\S+", f"beat={old:%Y-%m-%dT%H:%M:%S}", crashed.notes)})
    db.session.commit()
    wq["behaviour"], wq["calls"] = None, []

    assert _run(cron, monkeypatch, *CATCHUP) == 0

    assert _windows(wq["calls"]) == CHUNKS[1:]           # the first chunk is not walked again
    first, second = _runs()
    assert first.status == "failed" and "abandoned, resumed by run" in first.notes
    assert second.status == "ok" and "| next=end | done=3/3 | failed=- |" in second.notes


def test_same_plan_still_running_is_not_started_twice(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    from hansard_archive.pq_chunks import ChunkProgress
    prog = ChunkProgress.fresh("answered", D("2026-09-01"), D("2026-09-21"), 7)
    db.session.add(HaCronRun(service_name="pq-catchup", days_window=21, status="running",
                             started_at=datetime.utcnow() - timedelta(hours=6),
                             notes=prog.format(datetime.utcnow() - timedelta(minutes=5))))
    db.session.commit()

    assert _run(cron, monkeypatch, *CATCHUP) == 0         # refused, but not a failure
    assert wq["calls"] == [] and cron.alerts == []
    refused = _runs()[-1]
    assert refused.status == "skipped"
    assert refused.notes.startswith("chunked catch-up not started: run id 1") \
        and "--take-over" in refused.notes


def test_take_over_resumes_a_run_killed_by_a_deploy(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    from hansard_archive.pq_chunks import ChunkProgress
    wq["rows"] = _three_weeks()
    prog = ChunkProgress.fresh("answered", D("2026-09-01"), D("2026-09-21"), 7)
    prog.advance_past(prog.chunks()[0])
    db.session.add(HaCronRun(service_name="pq-catchup", days_window=21, status="running",
                             started_at=datetime.utcnow() - timedelta(minutes=30),
                             notes=prog.format(datetime.utcnow() - timedelta(minutes=5))))
    db.session.commit()

    assert _run(cron, monkeypatch, *CATCHUP, "--take-over") == 0

    assert _windows(wq["calls"]) == CHUNKS[1:]
    old, new = _runs()
    assert old.status == "failed" and "abandoned, resumed by run" in old.notes
    assert new.status == "ok" and "| next=end | done=3/3 |" in new.notes


def test_failed_chunk_is_logged_retried_and_does_not_stop_the_run(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    wq["rows"] = _three_weeks()
    tries = {"n": 0}

    def middle_chunk_down_on_first_walk(params, n):
        if params["answeredWhenFrom"] == "2026-09-08" and tries["n"] < 7:
            tries["n"] += 1          # 5 attempts + 2 at the first smaller page
            return _Resp({"title": "error", "status": 500}, 500)
    wq["behaviour"] = middle_chunk_down_on_first_walk

    assert _run(cron, monkeypatch, *CATCHUP) == 0

    walked = _windows(wq["calls"])
    assert walked.index(CHUNKS[2]) > walked.index(CHUNKS[1])    # the run went on
    assert walked[-1] == CHUNKS[1]                              # ...then retried it
    assert HaPQ.query.count() == 21
    db.session.rollback()
    run = _runs()[-1]
    assert run.status == "ok" and "| failed=- |" in run.notes


def test_three_failed_chunks_in_a_row_stop_and_a_rerun_carries_on(db, wq, cron, monkeypatch):
    wq["rows"] = [_value(1000 + i, "2025-12-01", answered=str(D("2026-01-01") + timedelta(days=i)),
                         answer="<p>A</p>") for i in range(35)]
    args = ("--answered-from", "2026-01-01", "--answered-to", "2026-02-04", "--chunk-days", "7",
            "--service-name", "pq-catchup", "--no-tag", "--skip-answer-fetch")
    wq["behaviour"] = lambda params, n: _Resp({}, 503)

    assert _run(cron, monkeypatch, *args) == 1

    walked = sorted(set(_windows(wq["calls"])), reverse=True)
    assert walked == [("2026-01-29", "2026-02-04"), ("2026-01-22", "2026-01-28"),
                      ("2026-01-15", "2026-01-21")]           # stopped after 3 of 5
    [run] = _runs()
    assert run.status == "failed"
    assert "| next=2026-01-08 |" in run.notes
    assert "failed=2026-01-29,2026-01-22,2026-01-15" in run.notes
    assert "3 chunks in a row failed" in run.notes
    assert len(cron.alerts) == 1

    wq["behaviour"], wq["calls"] = None, []
    assert _run(cron, monkeypatch, *args) == 0
    assert _windows(wq["calls"]) == [("2026-01-08", "2026-01-14"), ("2026-01-01", "2026-01-07"),
                                     ("2026-01-29", "2026-02-04"), ("2026-01-22", "2026-01-28"),
                                     ("2026-01-15", "2026-01-21")]
    assert "| next=end | done=5/5 | failed=- |" in _runs()[-1].notes


def test_rate_limit_stops_the_run_at_once(db, wq, cron, monkeypatch):
    wq["rows"] = _three_weeks()
    wq["behaviour"] = lambda params, n: _Resp({}, 429)

    assert _run(cron, monkeypatch, *CATCHUP) == 1

    assert len(wq["calls"]) == 1 and wq["sleeps"] == []         # never retried
    run = _runs()[-1]
    assert "PQRateLimitedError" in run.notes and "| failed=2026-09-15 |" in run.notes


def test_max_chunks_stops_cleanly_and_the_next_run_carries_on(db, wq, cron, monkeypatch):
    wq["rows"] = _three_weeks()
    assert _run(cron, monkeypatch, *CATCHUP, "--max-chunks", "1") == 0
    assert _windows(wq["calls"]) == CHUNKS[:1]
    assert _runs()[-1].status == "ok"
    wq["calls"] = []
    assert _run(cron, monkeypatch, *CATCHUP) == 0
    assert _windows(wq["calls"]) == CHUNKS[1:]


def test_dry_run_reads_progress_but_writes_nothing(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    wq["rows"] = _three_weeks()
    assert _run(cron, monkeypatch, *[a for a in CATCHUP if a != "--service-name"
                                     and a != "pq-catchup"], "--dry-run", "--max-chunks", "1") == 0
    assert _windows(wq["calls"]) == CHUNKS[:1]
    assert HaPQ.query.count() == 0 and _runs() == []


def test_shortfall_walks_the_chunk_again_and_counts_the_union(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    wq["rows"] = _three_weeks()
    walks = {"n": 0}

    def drop_a_row_on_first_walk(params, n):
        if params["answeredWhenFrom"] == "2026-09-15":
            walks["n"] += 1
            if walks["n"] == 1:          # rows shifted: one of the 7 never served
                rows = [r for r in wq["rows"]
                        if "2026-09-15" <= r["value"]["dateAnswered"][:10] <= "2026-09-21"]
                return _Resp({"totalResults": 7, "results": rows[1:]})
    wq["behaviour"] = drop_a_row_on_first_walk

    assert _run(cron, monkeypatch, *CATCHUP) == 0

    assert walks["n"] == 2
    assert HaPQ.query.count() == 21
    db.session.rollback()
    assert "| short=- |" in _runs()[-1].notes


def test_lasting_shortfall_is_listed_not_failed(db, wq, cron, monkeypatch):
    wq["rows"] = _three_weeks()

    def claims_more(params, n):
        if params["answeredWhenFrom"] == "2026-09-01":
            return _Resp({"totalResults": 9, "results": wq["rows"][:7]})
    wq["behaviour"] = claims_more

    assert _run(cron, monkeypatch, *CATCHUP) == 0
    assert _windows(wq["calls"]).count(CHUNKS[2]) == 2
    run = _runs()[-1]
    assert run.status == "ok" and "| short=2026-09-01 |" in run.notes


def test_bad_arguments_are_refused(cron, monkeypatch):
    for args in (("--answered-from", "2026-09-01", "--answered-to", "2026-09-21"),   # manual
                 ("--answered-from", "2026-09-01", "--service-name", "pq-catchup"),
                 ("--answered-from", "2026-09-01", "--answered-to", "2026-09-21",
                  "--tabled-from", "2026-09-01", "--tabled-to", "2026-09-21",
                  "--service-name", "pq-catchup"),
                 ("--answered-from", "2026-09-21", "--answered-to", "2026-09-01",
                  "--service-name", "pq-catchup"),
                 ("--chunk-days", "7",)):
        assert _run(cron, monkeypatch, *args) == 2, args


# ── soft lock ────────────────────────────────────────────────────────────────

def test_long_chunked_run_keeps_blocking_quick_checks_while_it_beats(db, cron):
    from hansard_archive.models import HaCronRun
    from hansard_archive.pq_chunks import ChunkProgress
    prog = ChunkProgress.fresh("answered", D("2025-05-01"), D("2026-10-23"), 7)
    now = datetime.utcnow()
    live = HaCronRun(service_name="pq-catchup", days_window=541, status="running",
                     started_at=now - timedelta(hours=9),
                     notes=prog.format(now - timedelta(minutes=4)))
    dead = HaCronRun(service_name="pq-catchup2", days_window=541, status="running",
                     started_at=now - timedelta(hours=9),
                     notes=prog.format(now - timedelta(hours=3)))
    db.session.add_all([live, dead])
    db.session.commit()
    assert [r.service_name for r in cron._active_runs(None, quick_only=False)] == ["pq-catchup"]
    assert cron._active_runs(None, quick_only=True) == []


# ── ingestor: retries, smaller pages, refusals ───────────────────────────────

def _ingest(**kw):
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    kw.setdefault("skip_answer_fetch", True)
    return ingest_pq_date_range(D("2026-09-01"), D("2026-09-21"), verbose=False,
                                date_field="answered", **kw)


def test_page_retried_with_growing_pauses_then_succeeds(db, wq):
    import requests
    wq["rows"] = _three_weeks()

    def flaky(params, n):
        if n == 1:
            return _Resp({"title": "error", "status": 500}, 500)
        if n == 2:
            raise requests.Timeout("read timed out")
    wq["behaviour"] = flaky

    res = _ingest()

    assert res["inserted"] == 21 and res["page_errors"] == 0 and len(wq["calls"]) == 3
    first, second = [s for s in wq["sleeps"] if s >= 1][:2]
    assert 15 <= first <= 18 and 30 <= second <= 36             # backoff plus jitter


def test_five_attempts_then_smaller_pages_with_skip_moved_on(db, wq, monkeypatch):
    import hansard_archive.pq_ingestor as ing
    from hansard_archive.models import HaPQ
    monkeypatch.setattr(ing, "_PAGE_SIZE", 10)
    monkeypatch.setattr(ing, "_FALLBACK_PAGE_SIZE", 5)
    wq["rows"] = _three_weeks()[:15]
    wq["behaviour"] = lambda params, n: _Resp({}, 500) if params["take"] == 10 else None

    res = _ingest()

    assert res["inserted"] == 15 and res["page_errors"] == 0
    assert HaPQ.query.count() == 15
    pages = [(c["skip"], c["take"]) for c in wq["calls"]]
    assert pages == [(0, 10)] * 5 + [(0, 5), (5, 5)] + [(10, 10)] * 5 + [(10, 5)]
    pauses = sorted({round(s) for s in wq["sleeps"] if s >= 1})
    assert pauses[0] >= 15 and max(wq["sleeps"]) <= 144


def test_partly_fetched_page_is_an_error_but_paging_goes_on(db, wq, monkeypatch):
    import hansard_archive.pq_ingestor as ing
    monkeypatch.setattr(ing, "_PAGE_SIZE", 10)
    monkeypatch.setattr(ing, "_FALLBACK_PAGE_SIZE", 5)
    wq["rows"] = _three_weeks()[:20]
    wq["behaviour"] = lambda params, n: (_Resp({}, 500) if params["take"] == 10
                                         or (params["skip"], params["take"]) == (5, 5) else None)

    res = _ingest()

    assert res["inserted"] == 15 and res["page_errors"] == 1
    assert (10, 5) in [(c["skip"], c["take"]) for c in wq["calls"]]


@pytest.mark.parametrize("status,exc_name", [(429, "PQRateLimitedError"),
                                             (404, "PQPageFailureError")])
def test_4xx_is_never_retried(db, wq, status, exc_name):
    import hansard_archive.pq_ingestor as ing
    wq["rows"] = _three_weeks()
    wq["behaviour"] = lambda params, n: _Resp({}, status)
    with pytest.raises(getattr(ing, exc_name)) as exc:
        _ingest()
    assert type(exc.value).__name__ == exc_name
    assert len(wq["calls"]) == 1 and exc.value.counts["page_errors"] == 1


def test_individual_record_429_stops_the_pass(db, wq, monkeypatch):
    import requests
    import hansard_archive.pq_ingestor as ing
    wq["rows"] = _three_weeks()
    real_get = requests.get

    def get(url, params=None, timeout=None):
        if url != ing.WQ_API_BASE:
            wq["individual_calls"].append(url)
            return _Resp({}, 429)
        return real_get(url, params=params, timeout=timeout)
    monkeypatch.setattr(requests, "get", get)

    with pytest.raises(ing.PQRateLimitedError):
        _ingest(skip_answer_fetch=False)
    assert len(wq["individual_calls"]) == 1


def test_individual_records_failing_in_a_row_stop_the_pass(db, wq):
    import hansard_archive.pq_ingestor as ing
    wq["rows"] = _three_weeks()
    wq["individual_status"] = 503
    with pytest.raises(ing.PQPageFailureError) as exc:
        _ingest(skip_answer_fetch=False)
    assert "10 individual record requests failed in a row" in str(exc.value)
    assert exc.value.counts["full_fetch_failed"] == 10 and exc.value.counts["inserted"] == 9


def test_request_timeout_allows_slow_pages():
    import hansard_archive.pq_ingestor as ing
    assert ing._REQUEST_TIMEOUT[1] >= 60


# ── the scheduled cron ───────────────────────────────────────────────────────

def _tagger(monkeypatch):
    import hansard_archive.tagger as tagger
    calls = []
    monkeypatch.setattr(tagger, "tag_pq_all_untagged",
                        lambda verbose=True, limit=None: calls.append(limit)
                        or {"tagged": 0, "errors": 0})
    return calls


def test_default_sweep_is_unchanged(db, wq, cron, monkeypatch):
    """Same three windows and pages as before; full records only where needed:
    the new question and the newly answered one, not the stored complete one."""
    from hansard_archive.models import HaPQ
    today_d = date.today()
    today, week_ago = today_d.isoformat(), (today_d - timedelta(days=6)).isoformat()
    db.session.add(HaPQ(api_id=1, uin="1", tabled_date=today_d, question_text=_SHORT_Q,
                        is_answered=False, is_holding=False, is_withdrawn=False))
    db.session.add(HaPQ(api_id=2, uin="2", tabled_date=today_d, question_text=_SHORT_Q,
                        is_answered=False, is_holding=False, is_withdrawn=False))
    db.session.commit()
    answered = _value(2, today, answered=today, answer="<p>Cut answer</p>")
    wq["rows"] = [_value(1, today), answered, _value(3, today)]
    full = {"questionText": _SHORT_Q, "answerIsHolding": False, "isWithdrawn": False}
    wq["individual"] = {2: {**full, "answerText": "<p>Full answer</p>",
                            "dateAnswered": f"{today}T00:00:00"}, 3: full}

    assert _run(cron, monkeypatch, "--service-name", "pq-cron-morning", "--no-tag") == 0

    assert [(next(k for k in c if k.endswith("WhenFrom")), c[next(k for k in c if
             k.endswith("WhenFrom"))], c["skip"], c["take"]) for c in wq["calls"]] == [
        ("tabledWhenFrom", week_ago, 0, 500), ("answeredWhenFrom", week_ago, 0, 500),
        ("correctedWhenFrom", week_ago, 0, 500)]
    assert all(next(v for k, v in c.items() if k.endswith("WhenTo")) == today
               for c in wq["calls"])
    assert sorted(wq["individual_calls"]) == [2, 3]          # not 1: already complete
    assert HaPQ.query.filter_by(api_id=2).one().answer_text == "<p>Full answer</p>"
    db.session.rollback()
    [run] = _runs()
    assert run.status == "ok"
    assert run.notes == ("passes: tabled ins=1 upd=2 full_fetched=2 fetch_failed=0 missing=0; "
                         "answered ins=0 upd=0 full_fetched=0 fetch_failed=0 missing=0; "
                         "corrected ins=0 upd=0 full_fetched=0 fetch_failed=0 missing=0")


def test_full_refresh_fetches_every_tabled_row(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    today_d = date.today()
    db.session.add(HaPQ(api_id=1, uin="1", tabled_date=today_d, question_text=_SHORT_Q,
                        is_answered=False, is_holding=False, is_withdrawn=False))
    db.session.commit()
    wq["rows"] = [_value(1, today_d.isoformat())]
    wq["individual"] = {1: {"questionText": _SHORT_Q, "answerIsHolding": False,
                            "isWithdrawn": False}}
    assert _run(cron, monkeypatch, "--service-name", "pq-cron-monday", "--no-tag",
                "--full-refresh") == 0
    assert wq["individual_calls"] == [1]


def test_answered_pass_failing_still_tags_what_was_stored(db, wq, cron, monkeypatch):
    tagged = _tagger(monkeypatch)
    wq["rows"] = [_value(1, date.today().isoformat())]
    wq["behaviour"] = lambda params, n: (_Resp({}, 503) if "answeredWhenFrom" in params else None)

    assert _run(cron, monkeypatch, "--service-name", "pq-morning", "--skip-answer-fetch") == 1

    assert tagged == [200]                                       # smaller limit after an abort
    assert not any("correctedWhenFrom" in c for c in wq["calls"])   # dead API: stop calling it
    run = _runs()[-1]
    assert run.status == "failed" and "PQPageFailureError" in run.notes
    assert len(cron.alerts) == 1


def test_identity_mismatch_abort_still_skips_tagging(db, cron, monkeypatch):
    from hansard_archive.pq_ingestor import PQIdentityMismatchError
    tagged = _tagger(monkeypatch)

    def aborting(*a, **k):
        raise PQIdentityMismatchError("too many", {"identity_mismatch": 6})
    monkeypatch.setattr(cron, "ingest_pq_date_range", aborting)

    assert _run(cron, monkeypatch, "--service-name", "pq-morning") == 1
    assert tagged == []


# ── tabled pass: full record only when needed ────────────────────────────────

_FULL_Q = ("To ask the Secretary of State for Transport, what assessment her Department has "
           "made of the potential merits of extending the zero-emission HGV grant scheme to "
           "small hauliers operating fewer than five vehicles in rural areas, and whether "
           "she plans to publish that assessment before the next Spending Review.")
_CUT_Q = _FULL_Q[:255]           # what the bulk endpoint serves: cut, no marker
_SHORT_Q = "To ask the Secretary of State, what steps are being taken."


def _tabled_row(api_id, text):
    row = _value(api_id, "2026-09-02")
    row["value"]["questionText"] = text
    return row


def _stored(db, api_id, text):
    from hansard_archive.models import HaPQ
    db.session.add(HaPQ(api_id=api_id, uin=str(api_id), tabled_date=D("2026-09-02"),
                        question_text=text, is_answered=False, heading="Schools",
                        is_holding=False, is_withdrawn=False))
    db.session.commit()


def _tabled_pass():
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    return ingest_pq_date_range(D("2026-09-01"), D("2026-09-07"), verbose=False,
                                fetch_only_new_answers=True)


def test_question_text_rule():
    from hansard_archive.pq_ingestor import _question_text_complete as ok
    assert len(_FULL_Q) > 255
    assert ok(_FULL_Q, _CUT_Q.rstrip(), 255)          # full text stored
    assert not ok(_CUT_Q.rstrip(), _CUT_Q.rstrip(), 255)   # only the cut copy stored
    assert not ok(None, _CUT_Q, 255)
    assert ok(_SHORT_Q, _SHORT_Q, len(_SHORT_Q))       # short: bulk is the whole text
    assert not ok(_SHORT_Q, _SHORT_Q + " Edited.", len(_SHORT_Q) + 8)


def test_tabled_pass_skips_rows_already_complete(db, wq):
    _stored(db, 1, _FULL_Q)
    _stored(db, 2, _SHORT_Q)
    wq["rows"] = [_tabled_row(1, _CUT_Q), _tabled_row(2, _SHORT_Q)]

    from hansard_archive.models import HaPQ
    res = _tabled_pass()

    assert wq["individual_calls"] == [] and res["failed"] == 0
    assert HaPQ.query.filter_by(api_id=1).one().question_text == _FULL_Q   # never the cut copy


def test_tabled_pass_fetches_new_rows_and_cut_question_text(db, wq):
    from hansard_archive.models import HaPQ
    _stored(db, 1, _CUT_Q.rstrip())                   # stored from a bulk-only pass
    _stored(db, 2, _SHORT_Q)
    wq["rows"] = [_tabled_row(1, _CUT_Q), _tabled_row(2, _SHORT_Q + " Amended."),
                  _tabled_row(3, _SHORT_Q)]
    full = {"answerIsHolding": False, "isWithdrawn": False}
    wq["individual"] = {1: {**full, "questionText": _FULL_Q},
                        2: {**full, "questionText": _SHORT_Q + " Amended."},
                        3: {**full, "questionText": _SHORT_Q}}

    res = _tabled_pass()

    assert sorted(wq["individual_calls"]) == [1, 2, 3]
    assert res["inserted"] == 1 and res["updated"] == 2
    assert HaPQ.query.filter_by(api_id=1).one().question_text == _FULL_Q
    assert HaPQ.query.filter_by(api_id=2).one().question_text == _SHORT_Q + " Amended."


def test_scheduled_tabled_pass_makes_no_full_fetch_for_stored_rows(db, wq, cron, monkeypatch):
    today = date.today().isoformat()
    from hansard_archive.models import HaPQ
    db.session.add(HaPQ(api_id=1, uin="1", tabled_date=date.today(), question_text=_FULL_Q,
                        is_answered=False, is_holding=False, is_withdrawn=False))
    db.session.commit()
    row = _value(1, today)
    row["value"]["questionText"] = _CUT_Q
    wq["rows"] = [row]

    assert _run(cron, monkeypatch, "--service-name", "pq-cron-morning", "--no-tag") == 0
    assert wq["individual_calls"] == []


def test_cut_and_trimmed_question_is_repaired(db, wq):
    """A bulk text cut at 255 and then trimmed (254) is still "possibly cut": a
    stored copy equal to it is fetched and replaced by the full text."""
    from hansard_archive.models import HaPQ
    cut = (_FULL_Q[:254] + " " + _FULL_Q[255:])[:255].rstrip()     # ends in a space at 255
    assert len(cut) == 254
    _stored(db, 1, cut)
    wq["rows"] = [_tabled_row(1, cut)]
    wq["individual"] = {1: {"questionText": _FULL_Q, "answerIsHolding": False,
                            "isWithdrawn": False}}
    _tabled_pass()
    assert wq["individual_calls"] == [1]
    assert HaPQ.query.filter_by(api_id=1).one().question_text == _FULL_Q


@pytest.mark.parametrize("length", [245, 250, 255])
def test_possibly_cut_band(length):
    from hansard_archive.pq_ingestor import _question_text_complete as ok
    bulk = _FULL_Q[:length]
    assert not ok(bulk, bulk, length)
    assert ok(_FULL_Q, bulk, length)


def test_bulk_only_fields_update_when_the_fetch_is_skipped(db, wq):
    from hansard_archive.models import HaPQ
    _stored(db, 1, _FULL_Q)
    r = _tabled_row(1, _CUT_Q)
    r["value"].update(heading="New heading", askingMember={"name": "Jane Doe"},
                      askingMemberId=42, answeringBodyName="Department for Transfers",
                      isWithdrawn=True)
    wq["rows"] = [r]
    _tabled_pass()
    row = HaPQ.query.filter_by(api_id=1).one()
    assert wq["individual_calls"] == []
    assert row.heading == "New heading" and row.asking_member == "Jane Doe"
    assert row.answering_body == "Department for Transfers" and row.is_withdrawn is True
    assert row.question_text == _FULL_Q


def test_holding_answer_replaced_is_fetched_by_the_tabled_pass(db, wq):
    from hansard_archive.models import HaPQ
    db.session.add(HaPQ(api_id=1, uin="1", tabled_date=D("2026-09-02"), question_text=_SHORT_Q,
                        is_answered=True, answer_text="<p>I will reply as soon as possible.</p>",
                        answer_date=D("2026-09-05"), is_holding=True, is_withdrawn=False))
    db.session.commit()
    r = _tabled_row(1, _SHORT_Q)
    r["value"].update(answerText="<p>Real answer</p>", dateAnswered="2026-09-05T00:00:00",
                      answerIsHolding=False)
    wq["rows"] = [r]
    wq["individual"] = {1: {"questionText": _SHORT_Q, "answerText": "<p>Real answer full</p>",
                            "answerIsHolding": False, "dateAnswered": "2026-09-05T00:00:00"}}
    _tabled_pass()
    assert wq["individual_calls"] == [1]
    assert "Real answer full" in HaPQ.query.one().answer_text


def test_progress_is_reported_inside_a_long_page(db, wq):
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    wq["rows"] = [_value(3000 + i, "2026-09-02") for i in range(120)]
    calls = []
    ingest_pq_date_range(D("2026-09-01"), D("2026-09-07"), verbose=False,
                         skip_answer_fetch=True, on_progress=lambda skip, n: calls.append(n))
    assert calls == [49, 99, 120]                         # every 50 rows, then the page end


def test_chunked_heartbeat_is_saved_mid_page(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    monkeypatch.setattr(cron, "_HEARTBEAT_SECONDS", 0)
    wq["rows"] = [_value(3000 + i, "2026-08-20", answered="2026-09-16", answer="<p>A</p>")
                  for i in range(120)]
    saved = []
    real_save = cron._save_progress
    monkeypatch.setattr(cron, "_save_progress",
                        lambda run_id, notes: saved.append(notes) or real_save(run_id, notes))
    assert _run(cron, monkeypatch, *CATCHUP) == 0
    first_chunk = [n for n in saved if "| next=2026-09-15 |" in n]
    assert len(first_chunk) >= 4      # start, two mid-page beats, end of page
    assert HaCronRun.query.count() == 1


def test_chunk_with_failed_full_fetches_is_retried(db, wq, cron, monkeypatch):
    wq["rows"] = [_value(1, "2026-08-20", answered="2026-09-16", answer="<p>A</p>")]
    wq["individual_status"] = 503
    args = [a for a in CATCHUP if a != "--skip-answer-fetch"]
    assert _run(cron, monkeypatch, *args) == 1
    assert wq["individual_calls"] == [1, 1]                  # walk + retry at the end
    assert "| failed=2026-09-15 |" in _runs()[-1].notes


def test_identity_mismatches_are_limited_across_the_whole_run(db, wq, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    rows = []
    for day in range(5):                                     # 5 per chunk: under the chunk limit
        for j in range(5):
            api_id = 5000 + day * 10 + j
            db.session.add(HaPQ(api_id=api_id, uin=f"X{api_id}", tabled_date=D("2026-08-20"),
                                question_text=_SHORT_Q, is_answered=False,
                                is_holding=False, is_withdrawn=False))
            rows.append(_value(api_id, "2026-08-20", answered=f"2026-09-0{day + 1}",
                               answer="<p>A</p>"))
    db.session.commit()
    wq["rows"] = rows
    args = ("--answered-from", "2026-09-01", "--answered-to", "2026-09-05", "--chunk-days", "1",
            "--service-name", "pq-catchup", "--no-tag", "--skip-answer-fetch")

    assert _run(cron, monkeypatch, *args) == 1

    assert len(set(_windows(wq["calls"]))) == 5
    run = _runs()[-1]
    assert "25 identity mismatches in this run (limit 20)" in run.notes


def test_progress_notes_are_never_cut(db, wq, cron, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x" * 5000)
    wq["rows"] = _three_weeks()
    monkeypatch.setattr(cron, "_tag_at_end", boom)
    assert _run(cron, monkeypatch, *CATCHUP) == 1
    run = _runs()[-1]
    assert run.notes.startswith("pq-chunked: answered") and "| beat=" in run.notes
    assert "| next=end | done=3/3 |" in run.notes and len(run.notes) < 2500


def test_too_many_chunks_are_refused(cron, monkeypatch):
    assert _run(cron, monkeypatch, "--answered-from", "2025-01-01", "--answered-to",
                "2026-10-23", "--chunk-days", "1", "--service-name", "pq-catchup") == 2


def test_retry_time_for_one_dead_page_is_bounded(db, wq):
    import hansard_archive.pq_ingestor as ing
    wq["rows"] = _three_weeks()
    wq["behaviour"] = lambda params, n: _Resp({}, 503)
    with pytest.raises(ing.PQPageFailureError):
        _ingest(max_failed_pages=1)
    # 5 attempts at 500, then two smaller pages tried twice each: 9 requests.
    assert len(wq["calls"]) == 9
    assert sum(wq["sleeps"]) <= (15 + 30 + 60 + 120 + 15 + 15) * 1.2 + 5


def test_time_budget_stops_the_pass_and_keeps_what_was_written(db, wq):
    import time
    import hansard_archive.pq_ingestor as ing
    wq["rows"] = _three_weeks()
    with pytest.raises(ing.PQTimeBudgetError):
        _ingest(deadline=time.monotonic() - 1)
    assert wq["calls"] == []


def test_scheduled_sweep_over_budget_stops_and_still_tags(db, wq, cron, monkeypatch):
    import time
    tagged = _tagger(monkeypatch)
    wq["rows"] = [_value(1, date.today().isoformat())]
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 600                    # ten minutes pass between every look
        return clock["t"]
    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    assert _run(cron, monkeypatch, "--service-name", "pq-cron-early", "--skip-answer-fetch",
                "--max-minutes", "15") == 1

    assert wq["calls"] == [] and tagged == [200]        # stopped before the first page
    assert "PQTimeBudgetError" in _runs()[-1].notes


def test_individual_404s_are_counted_apart_and_ten_in_a_row_stop_the_pass(db, wq):
    import hansard_archive.pq_ingestor as ing
    from hansard_archive.models import HaPQ
    wq["rows"] = [_value(1000 + i, "2026-08-20", answered="2026-09-03", answer="<p>A</p>")
                  for i in range(30)]                        # no individual records: all 404
    with pytest.raises(ing.PQPageFailureError) as exc:
        _ingest(skip_answer_fetch=False)
    counts = exc.value.counts
    assert counts["full_missing"] == 10 and counts["full_fetched"] == 0
    assert counts["full_fetch_failed"] == 0 and counts["inserted"] == 9
    assert HaPQ.query.count() == 9


def test_one_individual_404_is_not_a_failure(db, wq):
    wq["rows"] = _three_weeks()[:3]
    full = {"questionText": "To ask the Secretary of State, what steps are being taken.",
            "answerText": "<p>Full</p>", "answerIsHolding": False}
    wq["individual"] = {1000: full, 1002: full}              # 1001 has gone
    res = _ingest(skip_answer_fetch=False)
    assert (res["full_fetched"], res["full_missing"], res["full_fetch_failed"]) == (2, 1, 0)
    assert res["errors"] == 0


def test_budget_running_out_mid_page_keeps_the_rows_done(db, wq, monkeypatch):
    import hansard_archive.pq_ingestor as ing
    from hansard_archive.models import HaPQ
    wq["rows"] = _three_weeks()
    wq["individual"] = {1000 + i: {"questionText": "To ask the Secretary of State, what steps "
                                                   "are being taken.",
                                   "answerText": "<p>Full</p>", "answerIsHolding": False}
                        for i in range(21)}
    clock = {"t": 0.0}
    monkeypatch.setattr(ing.time, "monotonic", lambda: clock["t"])
    real = ing._fetch_individual

    def slow_fetch(api_id):
        clock["t"] += 10                                     # each full record takes 10 s
        return real(api_id)
    monkeypatch.setattr(ing, "_fetch_individual", slow_fetch)

    with pytest.raises(ing.PQTimeBudgetError) as exc:
        ing.ingest_pq_date_range(D("2026-09-01"), D("2026-09-21"), verbose=False,
                                 date_field="answered", deadline=55)
    db.session.rollback()
    rows = HaPQ.query.all()
    assert exc.value.counts["inserted"] == 6 and len(rows) == 6    # 0,10,..50 s: six fetched
    assert all(r.answer_text == "<p>Full</p>" for r in rows)


def test_tagging_is_limited_per_run(db, wq, cron, monkeypatch):
    tagged = _tagger(monkeypatch)
    wq["rows"] = [_value(1, date.today().isoformat())]
    assert _run(cron, monkeypatch, "--service-name", "pq-cron-early",
                "--skip-answer-fetch") == 0
    assert tagged == [500]


@pytest.mark.parametrize("extra", [("--quick-check",),
                                   ("--answered-from", "2026-09-01", "--answered-to",
                                    "2026-09-21", "--service-name", "pq-catchup")])
def test_full_refresh_only_with_a_full_sweep(cron, monkeypatch, extra):
    assert _run(cron, monkeypatch, "--full-refresh", *extra) == 2


def test_bad_budget_env_value_does_not_break_import(monkeypatch, cron):
    import importlib.util
    monkeypatch.setenv("PQ_RUN_BUDGET_MINUTES", "fifty")
    monkeypatch.setenv("PQ_TAG_LIMIT", "-3")
    spec = importlib.util.spec_from_file_location(
        "ingest_pq_cron_env_test", PROJECT_ROOT / "scripts" / "ingest_pq_cron.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._RUN_BUDGET_MINUTES == 50 and mod._TAG_LIMIT == 500
