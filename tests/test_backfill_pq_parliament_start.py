"""
Tests for scripts/backfill_pq_parliament_start.py — mocked WQ API + in-memory SQLite.

Covers: insert-only newest-first ingest with checkpoint, idempotent re-run,
stored rows kept unchanged (even when the API differs), abort on identity
mismatch, failing individual fetch rolls the day back, 429 aborts, dry run
writes nothing, capped tagging, an untaggable PQ never blocks, resume ignores a
run that died before saving, refuses while the debates backfill runs.

Run: python -m pytest tests/test_backfill_pq_parliament_start.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import backfill_parliament_start as bps  # noqa: E402
import backfill_pq_parliament_start as bpq  # noqa: E402
from hansard_archive import pq_ingestor  # noqa: E402

SATURDAY_NOON = datetime(2026, 10, 24, 12, 0)
BULK = pq_ingestor.WQ_API_BASE

# Questions per tabled day; ids are Parliament api_ids.
DAYS = {
    "2024-12-03": [(5003, "HL3001"), (5004, "30001")],
    "2024-12-02": [(5001, "20001"), (5002, "20002")],
}


def _value(api_id, uin, tabled, answer="<p>Full answer text.</p>"):
    return {
        "id": api_id, "uin": uin, "heading": f"Heading {uin}",
        "questionText": f"To ask about {uin}.", "answerText": answer,
        "dateTabled": f"{tabled}T00:00:00", "dateAnswered": f"{tabled}T00:00:00",
        "house": "Commons", "askingMemberId": 1, "askingMember": {"name": "Ann Example"},
        "answeringMemberId": 2, "answeringMember": {"name": "Bob Minister"},
        "answeringBodyName": "Department for Testing", "answeringBodyId": 7,
        "answerIsHolding": False, "isWithdrawn": False,
    }


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise pq_ingestor.requests.HTTPError(f"{self.status_code} Error")


class FakeWQ:
    def __init__(self):
        self.calls: list[str] = []
        self.individual_status: dict[int, int] = {}   # api_id -> status (always)

    def __call__(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url == BULK:
            day = params["tabledWhenFrom"][:10]
            rows = DAYS.get(day, [])
            return _Resp({"totalResults": len(rows),
                          "results": [{"value": _value(i, u, day, answer="<p>Full ans...</p>")}
                                      for i, u in rows]})
        api_id = int(url.rsplit("/", 1)[-1])
        if api_id in self.individual_status:
            return _Resp(status=self.individual_status[api_id])
        for day, rows in DAYS.items():
            for i, u in rows:
                if i == api_id:
                    return _Resp({"value": _value(i, u, day)})
        return _Resp(status=404)


@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from sqlalchemy import event
    from extensions import db as _db
    import hansard_archive.models  # noqa: F401
    import cache_models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True, SECRET_KEY="test")
    _db.init_app(app)
    # Real SAVEPOINT behaviour on pysqlite (see tests/test_pq_ingestor_identity.py).
    with app.app_context():
        engine = _db.engine

        @event.listens_for(engine, "connect")
        def _no_pysqlite_begin(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None

        @event.listens_for(engine, "begin")
        def _explicit_begin(conn):
            conn.exec_driver_sql("BEGIN")
    return app


@pytest.fixture
def env(test_app, monkeypatch):
    from extensions import db as _db
    fake = FakeWQ()
    monkeypatch.setattr(pq_ingestor.requests, "get", fake)
    monkeypatch.setattr(bpq.requests, "get", fake)
    monkeypatch.setattr(pq_ingestor.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake.gemini_up = True
    monkeypatch.setattr(bps, "gemini_healthy", lambda api_key: fake.gemini_up)
    with test_app.app_context():
        _db.create_all()
        yield fake
        _db.session.remove()
        _db.drop_all()


def _tagger(monkeypatch, bad=()):
    """Stands in for tagger.tag_pq; PQs whose api_id is in `bad` cannot be tagged."""
    from extensions import db as _db
    from hansard_archive.models import HaPQ, HaPQTheme
    calls = []

    def tag(pq_id, api_key=None):
        calls.append(pq_id)
        if _db.session.get(HaPQ, pq_id).api_id in bad:
            return 0
        _db.session.add(HaPQTheme(pq_id=pq_id, theme="Education", theme_type="policy_area"))
        _db.session.commit()
        return 1

    monkeypatch.setattr(bpq, "tag_pq", tag)
    return calls


@pytest.fixture
def fake_tagger(monkeypatch):
    return _tagger(monkeypatch)


def _run(*argv, now=SATURDAY_NOON):
    args = bpq.build_parser().parse_args(["--since", "2024-12-01", "--until", "2024-12-03", *argv])
    return bpq.run(args, sleep=lambda s: None, now=lambda: now)


def _rows():
    from hansard_archive.models import HaPQ
    return {(p.api_id, p.uin, p.tabled_date.isoformat(), p.answer_text) for p in HaPQ.query.all()}


def _checkpoint():
    from hansard_archive.models import HaCronRun
    rec = (HaCronRun.query.filter_by(service_name=bpq.SERVICE_NAME)
           .order_by(HaCronRun.id.desc()).first())
    return rec, (json.loads(rec.notes) if rec and rec.notes else {})


def _existing(**kw):
    from extensions import db as _db
    from hansard_archive.models import HaPQ
    base = dict(api_id=5004, uin="30001", tabled_date=date(2024, 12, 3),
                question_text="To ask about 30001.", answer_text="<p>Full answer text.</p>",
                heading="Heading 30001", is_answered=True, chamber="Commons")
    base.update(kw)
    pq = HaPQ(**base)
    _db.session.add(pq)
    _db.session.commit()
    return pq.id


# ---------------------------------------------------------------------------


def test_inserts_full_records_newest_first(env):
    assert _run("--no-tag") == bpq.EXIT_OK
    rows = _rows()
    assert len(rows) == 4
    assert all(r[3] == "<p>Full answer text.</p>" for r in rows)     # never the bulk copy
    rec, state = _checkpoint()
    assert rec.status == "ok" and state["frontier"] == "2024-12-01"


def test_rerun_is_idempotent(env):
    _run("--no-tag")
    assert _run("--no-tag", "--no-resume") == bpq.EXIT_OK
    assert len(_rows()) == 4


def test_resume_after_max_days(env):
    _run("--no-tag", "--max-days", "1")
    assert {r[2] for r in _rows()} == {"2024-12-03"}
    assert _run("--no-tag") == bpq.EXIT_OK
    assert len(_rows()) == 4


def test_stored_row_is_kept_even_when_the_api_differs(env, capsys):
    from extensions import db as _db
    from hansard_archive.models import HaPQ
    pid = _existing(answer_text="<p>An older answer.</p>")
    before = _db.session.get(HaPQ, pid).updated_at
    assert _run("--no-tag") == bpq.EXIT_OK
    row = _db.session.get(HaPQ, pid)
    assert row.answer_text == "<p>An older answer.</p>" and row.updated_at == before
    assert HaPQ.query.count() == 4
    assert "0 same, 1 differ from the API" in capsys.readouterr().out


def _unique_indexes():
    """The hand-made production indexes (docs/plans/pq-identity-phase3-sql.md)."""
    from extensions import db as _db
    _db.session.execute(_db.text("CREATE UNIQUE INDEX uq_t_api_id ON ha_pq (api_id)"))
    _db.session.execute(_db.text(
        "CREATE UNIQUE INDEX uq_t_date_uin ON ha_pq (tabled_date, lower(uin))"))
    _db.session.commit()


def test_row_stored_meanwhile_by_another_run_is_kept_not_failed(env, monkeypatch, capsys):
    """A cron stores 5004 between our lookup and our insert: the unique index
    rejects the insert; the backfill looks again, keeps the stored row, and
    finishes the day without a retry."""
    from extensions import db as _db
    from hansard_archive.models import HaPQ
    _unique_indexes()
    pid = _existing()
    before = _db.session.get(HaPQ, pid).updated_at
    real = pq_ingestor.find_existing_pq
    blind = {"left": 2}          # the wrapper's lookup and upsert_pq's own lookup

    def racing(api_id, tabled_date, uin):
        if api_id == 5004 and blind["left"]:
            blind["left"] -= 1
            return None, None, ""
        return real(api_id, tabled_date, uin)

    monkeypatch.setattr(pq_ingestor, "find_existing_pq", racing)
    assert _run("--no-tag") == bpq.EXIT_OK
    assert HaPQ.query.count() == 4
    row = _db.session.get(HaPQ, pid)
    assert row.updated_at == before
    out = capsys.readouterr().out
    assert "stored by another run meanwhile" in out and "attempt 1/" not in out
    assert "0 same, 1 differ from the API" in out   # counted once, as stored


def test_unusable_api_record_is_skipped_not_a_failed_day(env, monkeypatch, capsys):
    from hansard_archive.models import HaPQ
    real = pq_ingestor._extract_pq_fields
    monkeypatch.setattr(pq_ingestor, "_extract_pq_fields",
                        lambda v: None if v.get("id") == 5002 else real(v))
    assert _run("--no-tag") == bpq.EXIT_OK
    assert HaPQ.query.count() == 3
    _, state = _checkpoint()
    assert state["frontier"] == "2024-12-01"
    out = capsys.readouterr().out
    assert "unusable: 2024-12-02 api_id=5002 uin=20002" in out and "attempt 1/" not in out


def test_other_row_failure_still_fails_the_day(env, monkeypatch):
    def broken(fields, *, text_complete=True, dry_run=False):
        raise RuntimeError("boom")
    monkeypatch.setattr(pq_ingestor, "upsert_pq", broken)
    assert _run("--no-tag") == bpq.EXIT_FAILED


def test_identity_mismatch_aborts(env):
    from extensions import db as _db
    from hansard_archive.models import HaPQ
    pid = _existing(uin="99999")                  # same api_id, different uin
    assert _run("--no-tag") == bpq.EXIT_CONFLICT
    assert _db.session.get(HaPQ, pid).uin == "99999"
    # The page was rolled back: the other 3 Dec question was not kept either.
    assert HaPQ.query.count() == 1
    _, state = _checkpoint()
    assert state["frontier"] is None and "identity mismatch" in state["stop_reason"]


def test_individual_fetch_failure_rolls_day_back(env):
    from hansard_archive.models import HaPQ
    env.individual_status[5004] = 503
    assert _run("--no-tag") == bpq.EXIT_FAILED
    assert HaPQ.query.count() == 0
    _, state = _checkpoint()
    assert state["frontier"] is None
    # bounded: 2 day attempts x 3 individual attempts for the failing id
    assert sum(1 for u in env.calls if u.endswith("/5004")) == bpq.DAY_ATTEMPTS * bpq.INDIVIDUAL_ATTEMPTS


def test_individual_404_keeps_bulk_record(env):
    from hansard_archive.models import HaPQ
    env.individual_status[5004] = 404
    assert _run("--no-tag") == bpq.EXIT_OK
    assert HaPQ.query.filter_by(api_id=5004).one().answer_text is None   # bulk answer never stored


def test_rate_limit_aborts(env):
    env.individual_status[5003] = 429
    assert _run("--no-tag") == bpq.EXIT_REFUSED
    assert sum(1 for u in env.calls if u.endswith("/5003")) == 1


def test_dry_run_writes_nothing(env, capsys):
    from hansard_archive.models import HaCronRun, HaPQ
    _existing(heading="An old heading")
    assert _run("--dry-run") == bpq.EXIT_OK
    assert HaPQ.query.count() == 1 and HaCronRun.query.count() == 0
    out = capsys.readouterr().out
    assert "would insert: 3" in out
    assert "0 same, 1 differ from the API" in out
    assert not any(u != BULK for u in env.calls)    # bulk records only


def test_dry_run_flags_identity_mismatch(env):
    _existing(uin="99999")
    assert _run("--dry-run") == bpq.EXIT_CONFLICT


def test_untaggable_pq_never_blocks(env, monkeypatch):
    calls = _tagger(monkeypatch, bad={5003})
    assert _run() == bpq.EXIT_OK
    from hansard_archive.models import HaPQ, HaPQTheme
    assert HaPQTheme.query.count() == 3
    bad_id = HaPQ.query.filter_by(api_id=5003).one().id
    for _ in range(3):
        assert _run() == bpq.EXIT_OK
    assert calls.count(bad_id) == 2                    # then skipped for good
    _, state = _checkpoint()
    assert state["tag_failures"] == {str(bad_id): 2}


def test_gemini_outage_stops_tagging(env, monkeypatch):
    from hansard_archive import tagger
    monkeypatch.setattr(bps, "MAX_CONSECUTIVE_TAG_FAILURES", 2)

    def down(pq_id, api_key=None):
        raise tagger.GeminiCallFailed("HTTP 503")

    monkeypatch.setattr(bpq, "tag_pq", down)
    env.gemini_up = False
    assert _run() == bpq.EXIT_TAG_FAILED
    _, state = _checkpoint()
    assert state["tag_failures"] == {}


def test_pqs_the_crons_gave_up_on_are_skipped(env, monkeypatch):
    from extensions import db as _db
    from hansard_archive import tagger
    from hansard_archive.models import HaPQ, HaTagAttempt
    calls = _tagger(monkeypatch)
    _run("--no-tag")
    bad = HaPQ.query.filter_by(api_id=5003).one().id
    _db.session.add(HaTagAttempt(item_kind="pq", item_id=bad, attempts=tagger.TAG_MAX_ATTEMPTS))
    _db.session.commit()
    assert _run() == bpq.EXIT_OK
    assert bad not in calls and len(calls) == 3


def test_resume_ignores_a_run_that_died_before_saving(env):
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    _run("--no-tag", "--max-days", "1")
    _db.session.add(HaCronRun(service_name=bpq.SERVICE_NAME, started_at=datetime(2026, 10, 24, 12, 30),
                              status="failed", notes=None))
    _db.session.commit()
    n = len(env.calls)
    assert _run("--no-tag") == bpq.EXIT_OK
    assert sum(1 for u in env.calls[n:] if u != BULK) == 2   # only 2 Dec's two questions


def test_tagging_is_capped(env, fake_tagger):
    assert _run("--max-tag", "3") == bpq.EXIT_OK
    from hansard_archive.models import HaPQTheme
    assert HaPQTheme.query.count() == 3
    _, state = _checkpoint()
    assert "max-tag" in state["stop_reason"]
    assert _run("--max-tag", "10") == bpq.EXIT_OK
    assert HaPQTheme.query.count() == 4


def test_refuses_while_debates_backfill_runs(env):
    from extensions import db as _db
    from hansard_archive.models import HaCronRun
    _db.session.add(HaCronRun(service_name="backfill-parliament-start", started_at=SATURDAY_NOON,
                              status="running",
                              notes=json.dumps({"heartbeat": SATURDAY_NOON.isoformat()})))
    _db.session.commit()
    assert _run("--no-tag") == bpq.EXIT_BUSY


def test_stops_in_cron_window(env):
    assert _run("--no-tag", now=datetime(2026, 10, 20, 8, 20)) == bpq.EXIT_OK
    assert env.calls == []
