"""
scripts/ingest_pq_cron.py --quick-check: count-only pre-check, narrow passes,
run-log notes and alert gating.

SQLite in-memory only; Parliament's API, the tagger and email are mocked.
"""

import importlib.util
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


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
        "ingest_pq_cron_under_test", PROJECT_ROOT / "scripts" / "ingest_pq_cron.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    alerts = []
    monkeypatch.setattr(mod, "_send_failure_alert",
                        lambda service, subject, body: alerts.append(subject))
    mod.alerts = alerts
    return mod


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _value(api_id, uin, tabled, answer=None, answered=None):
    return {"value": {
        "id": api_id, "uin": uin, "heading": "Schools",
        "questionText": "To ask the Secretary of State, what steps are being taken.",
        "answerText": answer, "dateTabled": f"{tabled}T00:00:00",
        "dateAnswered": f"{answered}T00:00:00" if answered else None,
        "house": "Commons", "askingMemberId": None, "askingMember": None,
        "answeringMemberId": None, "answeringMember": None,
        "answeringBodyName": "Department for Testing", "answeringBodyId": 7,
        "answerIsHolding": False, "isWithdrawn": False,
    }}


@pytest.fixture
def api(monkeypatch):
    """Fake WQ API. Count calls (take=1) answer from the bulk lists' lengths
    unless state["fail"] is set; bulk calls serve the lists."""
    import requests
    import time
    from hansard_archive.pq_ingestor import WQ_API_BASE

    state = {"tabled": [], "answered": [], "individual": {},
             "count_calls": [], "bulk_calls": [], "fail": False, "bulk_status": 200,
             "count_status": 200}

    def _matching(params):
        if "tabledWhenFrom" in params:
            lo, hi, key = params["tabledWhenFrom"], params["tabledWhenTo"], "dateTabled"
            rows = state["tabled"]
        else:
            lo, hi, key = params["answeredWhenFrom"], params["answeredWhenTo"], "dateAnswered"
            rows = state["answered"]
        return [r for r in rows if r["value"][key] and lo <= r["value"][key][:10] <= hi]

    def fake_get(url, params=None, timeout=None):
        if state["fail"]:
            raise requests.ConnectionError("API down")
        if url == WQ_API_BASE:
            rows = _matching(params)
            if params["take"] == 1:
                state["count_calls"].append(dict(params))
                if state["count_status"] != 200:
                    return _Resp({}, state["count_status"])
                return _Resp({"totalResults": len(rows), "results": rows[:1]})
            state["bulk_calls"].append(dict(params))
            if state["bulk_status"] != 200:
                return _Resp({}, state["bulk_status"])
            skip = params.get("skip", 0)
            return _Resp({"results": rows[skip:skip + params["take"]]})
        api_id = int(url.rsplit("/", 1)[1])
        return _Resp({"value": state["individual"].get(api_id)})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return state


def _pq(**kw):
    from hansard_archive.models import HaPQ
    base = dict(question_text="To ask the Secretary of State, what steps are being taken.",
                heading="Schools", chamber="Commons", is_answered=False,
                answering_body="Department for Testing", answering_body_id=7,
                is_holding=False, is_withdrawn=False)
    base.update(kw)
    return HaPQ(**base)


def _run(cron, monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", ["ingest_pq_cron.py", "--quick-check",
                                      "--service-name", "pq-quickcheck", "--no-tag", *extra])
    try:
        cron.main()
    except SystemExit as exc:
        return exc.code
    return 0


def _last_run():
    """Snapshot of the newest run record. Ends the read transaction: the test
    shares one SQLite connection with the script's own session."""
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    run = HaCronRun.query.order_by(HaCronRun.id.desc()).first()
    snap = types.SimpleNamespace(status=run.status, notes=run.notes,
                                 sessions_ingested=run.sessions_ingested,
                                 sessions_tagged=run.sessions_tagged)
    _db.session.rollback()
    return snap


def _seed_in_sync(db, api):
    """Two tabled questions (one answered yesterday) — DB and API agree."""
    db.session.add(_pq(api_id=1, uin="100", tabled_date=YESTERDAY))
    db.session.add(_pq(api_id=2, uin="101", tabled_date=YESTERDAY, is_answered=True,
                       answer_date=YESTERDAY, answer_text="<p>Answer</p>"))
    db.session.commit()
    api["tabled"] = [_value(1, "100", YESTERDAY),
                     _value(2, "101", YESTERDAY, answer="<p>Answer</p>", answered=YESTERDAY)]
    api["answered"] = [api["tabled"][1]]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_nothing_new_exits_without_full_fetch(db, api, cron, monkeypatch, capsys):
    _seed_in_sync(db, api)

    assert _run(cron, monkeypatch) == 0

    assert api["bulk_calls"] == []
    assert len(api["count_calls"]) == 3          # tabled window + answered today + yesterday
    assert all(c["take"] == 1 for c in api["count_calls"])
    run = _last_run()
    assert run.status == "ok" and run.sessions_ingested == 0
    assert run.notes.startswith("quick-check: nothing new (")
    assert f"answered {YESTERDAY} API=1 DB=1" in run.notes
    assert "[pq-cron] quick-check: nothing new (" in capsys.readouterr().out
    assert cron.alerts == []


def test_new_tabled_runs_only_the_tabled_window(db, api, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    _seed_in_sync(db, api)
    api["tabled"].append(_value(3, "102", YESTERDAY))
    api["individual"] = {3: {"questionText": "To ask the Secretary of State, the full text.",
                             "answerIsHolding": False, "isWithdrawn": False}}

    assert _run(cron, monkeypatch) == 0

    assert len(api["bulk_calls"]) == 1
    call = api["bulk_calls"][0]
    assert call["tabledWhenFrom"] == (TODAY - timedelta(days=3)).isoformat()
    assert call["tabledWhenTo"] == TODAY.isoformat()
    assert HaPQ.query.filter_by(api_id=3).one().question_text.endswith("the full text.")
    run = _last_run()
    assert run.status == "ok" and run.sessions_ingested == 1
    assert run.notes.startswith("quick-check: ran tabled ")


def test_new_answer_runs_only_the_answered_day(db, api, cron, monkeypatch):
    from hansard_archive.models import HaPQ
    _seed_in_sync(db, api)
    answered = _value(1, "100", YESTERDAY, answer="<p>Bulk</p>", answered=TODAY)
    api["tabled"][0] = answered
    api["answered"].append(answered)
    api["individual"] = {1: {"questionText": "To ask the Secretary of State, what steps are being taken.",
                             "answerText": "<p>The full answer.</p>",
                             "answerIsHolding": False, "isWithdrawn": False}}

    assert _run(cron, monkeypatch) == 0

    assert len(api["bulk_calls"]) == 1
    call = api["bulk_calls"][0]
    assert call["answeredWhenFrom"] == TODAY.isoformat() == call["answeredWhenTo"]
    assert "tabledWhenFrom" not in call
    row = HaPQ.query.filter_by(api_id=1).one()
    assert row.is_answered and row.answer_date == TODAY and "full answer" in row.answer_text
    assert _last_run().notes.startswith(f"quick-check: ran answered {TODAY}..{TODAY}")


def test_new_rows_are_tagged(db, api, cron, monkeypatch):
    import hansard_archive.tagger as tagger
    _seed_in_sync(db, api)
    api["tabled"].append(_value(3, "102", YESTERDAY))
    calls = []
    monkeypatch.setattr(tagger, "tag_pq_all_untagged",
                        lambda verbose=True: calls.append(1) or {"tagged": 1, "errors": 0})
    monkeypatch.setattr(sys, "argv", ["x", "--quick-check", "--service-name", "pq-quickcheck",
                                      "--skip-answer-fetch"])
    cron.main()
    assert calls == [1] and _last_run().sessions_tagged == 1


def test_api_failure_exits_nonzero_without_fallback_and_alerts_on_second(db, api, cron, monkeypatch):
    _seed_in_sync(db, api)
    api["fail"] = True

    assert _run(cron, monkeypatch) == 1
    assert api["bulk_calls"] == []
    run = _last_run()
    assert run.status == "failed" and run.notes.startswith("quick-check: WQ API count failed")
    assert cron.alerts == []                       # 1st failure: held

    assert _run(cron, monkeypatch) == 1
    assert len(cron.alerts) == 1                   # 2nd in a row: one email

    assert _run(cron, monkeypatch) == 1
    assert len(cron.alerts) == 1                   # still down: no more email

    api["fail"] = False
    assert _run(cron, monkeypatch) == 0            # recovered
    api["fail"] = True
    assert _run(cron, monkeypatch) == 1
    assert len(cron.alerts) == 1                   # new streak, 1st failure: held


def test_skips_while_another_pq_run_is_running(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    _seed_in_sync(db, api)
    api["tabled"].append(_value(3, "102", YESTERDAY))
    db.session.add(HaCronRun(service_name="pq-morning", started_at=datetime.utcnow(),
                             days_window=7, status="running"))
    db.session.commit()

    assert _run(cron, monkeypatch) == 0
    assert api["count_calls"] == [] and api["bulk_calls"] == []
    run = _last_run()
    assert run.status == "skipped"               # checked nothing: not an "ok" run
    assert run.notes.startswith("quick-check: skipped (pq-morning running")


def test_long_full_sweep_still_blocks(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    _seed_in_sync(db, api)
    db.session.add(HaCronRun(service_name="pq-morning",
                             started_at=datetime.utcnow() - timedelta(minutes=90),
                             days_window=7, status="running"))
    db.session.commit()

    assert _run(cron, monkeypatch) == 0
    assert api["count_calls"] == [] and _last_run().status == "skipped"


def test_skipped_runs_do_not_reset_the_alert_streak(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    _seed_in_sync(db, api)
    now = datetime.utcnow()
    for minutes, status in ((60, "failed"), (30, "skipped")):
        db.session.add(HaCronRun(service_name="pq-quickcheck", days_window=4, status=status,
                                 started_at=now - timedelta(minutes=minutes)))
    db.session.commit()
    api["fail"] = True

    assert _run(cron, monkeypatch) == 1
    assert len(cron.alerts) == 1                 # failed, (skipped), failed: 2nd in a row


def test_stale_running_record_does_not_block(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    _seed_in_sync(db, api)
    db.session.add(HaCronRun(service_name="pq-morning",
                             started_at=datetime.utcnow() - timedelta(hours=3),
                             days_window=7, status="running"))
    db.session.commit()

    assert _run(cron, monkeypatch) == 0
    assert len(api["count_calls"]) == 3


def test_full_sweep_is_unchanged_and_makes_no_count_calls(db, api, cron, monkeypatch):
    _seed_in_sync(db, api)
    monkeypatch.setattr(sys, "argv", ["x", "--service-name", "pq-morning", "--no-tag",
                                      "--skip-answer-fetch"])
    cron.main()
    assert api["count_calls"] == []
    assert [("tabledWhenFrom" in c, "answeredWhenFrom" in c) for c in api["bulk_calls"]] == [
        (True, False), (False, True)]
    assert api["bulk_calls"][0]["tabledWhenFrom"] == (TODAY - timedelta(days=6)).isoformat()
    run = _last_run()
    assert run.status == "ok" and run.notes is None


def test_full_sweep_waits_for_a_running_quick_check(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    qc = HaCronRun(service_name="pq-quickcheck", started_at=datetime.utcnow(),
                   days_window=4, status="running", notes="quick-check")
    db.session.add(qc)
    db.session.commit()
    polls = []

    def fake_sleep(_s):
        polls.append(1)
        HaCronRun.query.filter_by(id=qc.id).update({"status": "ok"})
        db.session.commit()

    monkeypatch.setattr(cron.time, "sleep", fake_sleep)
    cron._wait_for_quick_check(exclude_id=None)
    assert polls == [1]


def test_quick_check_alert_rule(cron):
    rule = cron._quick_check_should_alert
    assert rule([]) is False                      # first run ever, first failure
    assert rule(["ok", "failed"]) is False
    assert rule(["failed"]) is True
    assert rule(["failed", "ok"]) is True
    assert rule(["failed", "failed"]) is False    # already emailed this streak
    assert rule(None) is True                     # run log unreadable: alert


def test_quick_check_notes_are_readable_by_the_publication_report(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    from hansard_archive.pq_publication import parse_counts
    _seed_in_sync(db, api)
    api["tabled"].append(_value(3, "102", YESTERDAY))
    assert _run(cron, monkeypatch, "--skip-answer-fetch") == 0
    run = HaCronRun.query.order_by(HaCronRun.id.desc()).first()
    obs = parse_counts(run.notes, run.started_at)
    db.session.rollback()
    assert [(o.kind, o.api, o.db) for o in obs] == [
        ("tabled", 3, 2), ("answered", 0, 0), ("answered", 1, 1)]


def test_truncated_answer_counts_as_missing(db, api, cron, monkeypatch):
    """A stored answer that looks like the bulk endpoint's cut-off copy is not
    counted, so the answered day is re-ingested and the full text fetched."""
    from hansard_archive.models import HaPQ
    _seed_in_sync(db, api)
    row = HaPQ.query.filter_by(api_id=2).one()
    row.answer_text = "x" * 255 + "..."          # 258 chars: the bulk cap
    db.session.commit()
    api["individual"] = {2: {"questionText": "To ask the Secretary of State, what steps are being taken.",
                             "answerText": "<p>The full answer.</p>",
                             "answerIsHolding": False, "isWithdrawn": False}}

    assert _run(cron, monkeypatch) == 0

    assert f"answered {YESTERDAY} API=1 DB=0" in _last_run().notes
    assert len(api["bulk_calls"]) == 1
    assert api["bulk_calls"][0]["answeredWhenFrom"] == YESTERDAY.isoformat()
    assert "full answer" in HaPQ.query.filter_by(api_id=2).one().answer_text


def test_aborted_narrow_pass_fails_the_run_and_keeps_the_counts(db, api, cron, monkeypatch):
    _seed_in_sync(db, api)
    api["tabled"].append(_value(3, "102", YESTERDAY))
    api["bulk_status"] = 403                     # permanent error: PQPageFailureError

    assert _run(cron, monkeypatch) == 1

    assert len(api["bulk_calls"]) == 1           # no retry on a permanent error
    run = _last_run()
    assert run.status == "failed"
    assert run.notes.startswith("quick-check: ran tabled ")
    assert "API=3 DB=2" in run.notes and "PQPageFailureError" in run.notes
    assert cron.alerts == []                     # 1st failure in a row: held


@pytest.mark.parametrize("status", [429, 403])
def test_count_call_not_retried_on_4xx(db, api, cron, monkeypatch, status):
    _seed_in_sync(db, api)
    api["count_status"] = status

    assert _run(cron, monkeypatch) == 1

    assert len(api["count_calls"]) == 1 and api["bulk_calls"] == []
    assert f"HTTP {status} (not retried)" in _last_run().notes


def test_count_call_retried_once_on_5xx(db, api, cron, monkeypatch):
    _seed_in_sync(db, api)
    api["count_status"] = 503

    assert _run(cron, monkeypatch) == 1

    assert len(api["count_calls"]) == 2           # first call + one retry


def test_new_question_found_when_db_has_more_rows_than_the_api(db, api, cron, monkeypatch):
    """An extra row on our side (DB > API) must not hide a new question: the
    API count rising since the last successful check triggers the pass."""
    from hansard_archive.models import HaPQ
    _seed_in_sync(db, api)
    db.session.add(_pq(api_id=99, uin="199", tabled_date=YESTERDAY))   # not in the API
    db.session.commit()

    assert _run(cron, monkeypatch) == 0
    assert _last_run().notes.startswith("quick-check: nothing new (")   # API=2 DB=3

    api["tabled"].append(_value(3, "102", YESTERDAY))                    # API=3 DB=3
    assert _run(cron, monkeypatch, "--skip-answer-fetch") == 0

    assert len(api["bulk_calls"]) == 1
    assert HaPQ.query.filter_by(api_id=3).count() == 1
    assert _last_run().notes.startswith("quick-check: ran tabled ")


def test_grown_rule(cron):
    checks = [{"kind": "tabled", "from": YESTERDAY, "to": TODAY, "api": 5, "db": 6}]
    assert cron._grown(checks, {}) == []
    assert cron._grown(checks, {("tabled", YESTERDAY, TODAY): 5}) == []
    assert cron._grown(checks, {("tabled", YESTERDAY, TODAY): 4}) == checks
    assert cron._grown(checks, {("tabled", YESTERDAY - timedelta(days=1), TODAY): 4}) == []


def test_baseline_ignores_failed_runs(db, api, cron, monkeypatch):
    from hansard_archive.models import HaCronRun
    db.session.add(HaCronRun(service_name="pq-quickcheck", started_at=datetime.utcnow() - timedelta(minutes=60),
                             days_window=4, status="ok",
                             notes=f"quick-check: nothing new (tabled {YESTERDAY}..{TODAY} API=4 DB=4)"))
    db.session.add(HaCronRun(service_name="pq-quickcheck", started_at=datetime.utcnow() - timedelta(minutes=30),
                             days_window=4, status="failed",
                             notes=f"quick-check: ran tabled (tabled {YESTERDAY}..{TODAY} API=5 DB=4) | boom"))
    db.session.commit()
    assert cron._last_ok_api_counts("pq-quickcheck", None) == {("tabled", YESTERDAY, TODAY): 4}


def test_dry_run_writes_nothing(db, api, cron, monkeypatch, capsys):
    from hansard_archive.models import HaCronRun, HaPQ
    _seed_in_sync(db, api)
    api["tabled"].append(_value(3, "102", YESTERDAY))
    api["individual"] = {3: {"questionText": "To ask the Secretary of State, the full text.",
                             "answerIsHolding": False, "isWithdrawn": False}}

    assert _run(cron, monkeypatch, "--dry-run") == 0

    assert len(api["bulk_calls"]) == 1
    assert HaPQ.query.filter_by(api_id=3).count() == 0
    assert HaCronRun.query.count() == 0
    db.session.rollback()
    out = capsys.readouterr().out
    assert "quick-check: something new (" in out and "inserted=1" in out


def test_dry_run_needs_quick_check(cron, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cron.main()
    assert exc.value.code == 2
