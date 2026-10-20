"""
scripts/backfill_pq_api_id.py — filling missing api_id by (tabled_date, uin).

SQLite in-memory; the Parliament API is mocked (no network).
"""

import importlib.util
import sys
import types
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
TEXT = "To ask the Secretary of State for Health and Social Care, whether he plans to review."


@pytest.fixture
def env(monkeypatch):
    from flask import Flask
    from extensions import db
    import cache_models  # noqa: F401  (cached_member, referenced by archive models)
    import hansard_archive.models  # noqa: F401

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True)
    db.init_app(app)
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=app))
    spec = importlib.util.spec_from_file_location(
        "backfill_pq_api_id", PROJECT_ROOT / "scripts" / "backfill_pq_api_id.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    api = {"records": {}, "status": {}, "urls": [], "sleeps": []}

    class Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, timeout=None):
        api["urls"].append(url)
        key = url[len(mod.WQ_API_BASE) + 1:]
        if key in api["status"]:
            status = api["status"][key]
            return Resp(status() if callable(status) else status, {"title": "error"})
        if key in api["records"]:
            return Resp(200, {"value": api["records"][key]})
        return Resp(404, {"title": "Not Found"})

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda s: api["sleeps"].append(s))

    with app.app_context():
        db.create_all()
        yield mod, db, api
        db.session.remove()
        db.drop_all()


def _run(mod, db, argv):
    rc = mod.main(argv)
    db.session.expire_all()
    return rc


def _pq(db, **kw):
    from hansard_archive.models import HaPQ
    base = dict(uin="6978", question_text=TEXT, tabled_date=date(2026, 6, 4),
                chamber="Commons", is_answered=False, api_id=None,
                updated_at=datetime(2026, 6, 5, 9, 0))
    base.update(kw)
    row = HaPQ(**base)
    db.session.add(row)
    db.session.commit()
    return row.id


def _record(api_id, uin="6978", tabled="2026-06-04", text=TEXT):
    return {"id": api_id, "uin": uin, "dateTabled": f"{tabled}T00:00:00",
            "questionText": text}


def _row(rid):
    from hansard_archive.models import HaPQ
    return HaPQ.query.get(rid)


def test_dry_run_default_writes_nothing(env):
    mod, db, api = env
    rid = _pq(db)
    api["records"]["2026-06-04/6978"] = _record(1912500)
    assert _run(mod, db, []) == 0
    assert _row(rid).api_id is None
    assert api["urls"] == [mod.WQ_API_BASE + "/2026-06-04/6978"]


def test_apply_writes_only_api_id(env):
    mod, db, api = env
    rid = _pq(db, uin="HL4290", tabled_date=date(2026, 10, 19), heading=None)
    api["records"]["2026-10-19/HL4290"] = _record(1950001, uin="HL4290", tabled="2026-10-19")
    assert _run(mod, db, ["--apply"]) == 0
    row = _row(rid)
    assert row.api_id == 1950001
    assert row.uin == "HL4290" and row.tabled_date == date(2026, 10, 19)
    assert row.question_text == TEXT and row.heading is None
    assert row.updated_at != datetime(2026, 6, 5, 9, 0)


def test_not_found_is_logged_and_left(env, caplog):
    mod, db, api = env
    rid = _pq(db, uin="444")
    assert _run(mod, db, ["--apply"]) == 0
    assert _row(rid).api_id is None
    assert "NOT_FOUND" in caplog.text and "uin=444" in caplog.text


def test_different_question_text_is_not_written(env, caplog):
    mod, db, api = env
    rid = _pq(db)
    api["records"]["2026-06-04/6978"] = _record(
        1912500, text="To ask the Chancellor of the Exchequer, what fiscal steps she is taking.")
    _run(mod, db, ["--apply"])
    assert _row(rid).api_id is None
    assert "TEXT_MISMATCH" in caplog.text


def test_api_returning_another_uin_is_ambiguous(env, caplog):
    mod, db, api = env
    rid = _pq(db)
    api["records"]["2026-06-04/6978"] = _record(1912500, uin="6979")
    _run(mod, db, ["--apply"])
    assert _row(rid).api_id is None
    assert "AMBIGUOUS" in caplog.text


def test_api_id_already_on_another_row_is_a_conflict(env, caplog):
    mod, db, api = env
    _pq(db, api_id=1912500)                      # same question, already identified
    rid = _pq(db)                                # its duplicate without api_id
    api["records"]["2026-06-04/6978"] = _record(1912500)
    _run(mod, db, ["--apply"])
    assert _row(rid).api_id is None
    assert "CONFLICT" in caplog.text and "dedupe_pq.py" in caplog.text


def test_two_rows_needing_the_same_api_id_only_first_written(env):
    mod, db, api = env
    first = _pq(db)
    second = _pq(db)
    api["records"]["2026-06-04/6978"] = _record(1912500)
    _run(mod, db, ["--apply"])
    assert _row(first).api_id == 1912500 and _row(second).api_id is None


def test_rate_limit_stops_at_once(env):
    mod, db, api = env
    a = _pq(db, uin="1")
    b = _pq(db, uin="2")
    api["status"]["2026-06-04/1"] = 429
    api["records"]["2026-06-04/2"] = _record(2, uin="2")
    assert _run(mod, db, ["--apply"]) == 2
    assert _row(a).api_id is None and _row(b).api_id is None
    assert len(api["urls"]) == 1


def test_server_errors_retried_then_stop_after_five_rows(env):
    mod, db, api = env
    ids = [_pq(db, uin=str(i)) for i in range(1, 8)]
    for i in range(1, 8):
        api["status"][f"2026-06-04/{i}"] = 503
    assert _run(mod, db, ["--apply"]) == 2
    assert len(api["urls"]) == 5 * mod.RETRIES             # stopped after 5 rows
    assert all(_row(r).api_id is None for r in ids)


def test_pacing_floor_and_resume_after_id(env):
    mod, db, api = env
    a = _pq(db, uin="1")
    b = _pq(db, uin="2")
    api["records"]["2026-06-04/1"] = _record(1, uin="1")
    api["records"]["2026-06-04/2"] = _record(2, uin="2")
    _run(mod, db, ["--apply", "--after-id", str(a), "--delay", "0.1"])
    assert _row(a).api_id is None and _row(b).api_id == 2
    assert api["sleeps"] and min(api["sleeps"]) >= 0.5


def test_uin_is_url_quoted(env):
    mod, db, api = env
    _pq(db, uin="HL 12/3")
    _run(mod, db, [])
    assert api["urls"] == [mod.WQ_API_BASE + "/2026-06-04/HL%2012%2F3"]


def test_texts_match_ignores_markup_and_punctuation(env):
    mod, _db, _api = env
    assert mod.texts_match(TEXT, "<p>" + TEXT.upper() + "</p>")
    assert mod.texts_match(TEXT[:70] + "...", TEXT)           # stored bulk-truncated text
    assert not mod.texts_match(TEXT, "To ask the Chancellor of the Exchequer, what steps.")
    assert not mod.texts_match("", TEXT)
