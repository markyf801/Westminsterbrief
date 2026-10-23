"""
PQ identity (INC-008, Phase 3): ingestor matching, tripwire, tag invalidation,
tagger result mapping, and the PQ page title fallback.

SQLite in-memory only; the Parliament API and Gemini are mocked.

Note: the old model declared ha_pq.uin UNIQUE; the model now declares it
non-unique. Production never had that unique index (its idx_ha_pq_uin is a
plain btree, audit A, 2026-10-20), so these tests run without one — as
production does: a new question that reuses an old UIN is INSERTED by this
code (the old code overwrote the old row).
"""

import re
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db

    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    from hansard import hansard_bp
    from biography import biography_bp
    from tracker import tracker_bp
    from debate_scanner import debate_scanner_bp
    from mp_search import mp_search_bp
    from stakeholder_directory.views import directory_bp
    from hansard_archive.views import archive_bp, brief_bp
    from hansard_archive.slugs import slugify_theme
    from feature_flags import feature_enabled

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    test_app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "LOGIN_DISABLED": True,
    })
    _db.init_app(test_app)
    lm = LoginManager()
    lm.init_app(test_app)
    lm.user_loader(lambda uid: None)
    test_app.context_processor(lambda: {"admin_authenticated": False, "app_version": "test"})
    test_app.jinja_env.filters["slugify"] = slugify_theme
    test_app.jinja_env.globals["feature_enabled"] = feature_enabled
    for bp in (hansard_bp, biography_bp, tracker_bp, debate_scanner_bp,
               mp_search_bp, directory_bp, archive_bp, brief_bp):
        test_app.register_blueprint(bp)

    # pysqlite's own transaction handling breaks SAVEPOINT (RELEASE of the
    # first savepoint commits). SQLAlchemy's documented fix, so the per-row
    # savepoints behave as they do on Postgres.
    from sqlalchemy import event
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _pq(**kw):
    from hansard_archive.models import HaPQ
    base = dict(question_text="To ask the Secretary of State, what steps are being taken.",
                tabled_date=date(2026, 6, 4), chamber="Commons", is_answered=False)
    base.update(kw)
    return HaPQ(**base)


def _value(api_id, uin, tabled, heading=None, text="To ask the Secretary of State, what steps are being taken.",
           asking=None, asking_id=None, answer=None, house="Commons", answered=None):
    return {"value": {
        "id": api_id, "uin": uin, "heading": heading, "questionText": text,
        "answerText": answer, "dateTabled": f"{tabled}T00:00:00", "dateAnswered": f"{answered}T00:00:00" if answered else None,
        "house": house, "askingMemberId": asking_id,
        "askingMember": {"name": asking} if asking else None,
        "answeringMemberId": None, "answeringMember": None,
        "answeringBodyName": "Department for Testing", "answeringBodyId": 7,
        "answerIsHolding": False, "isWithdrawn": False,
    }}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


@pytest.fixture
def fake_api(monkeypatch):
    """Serve `state["bulk"]` from the bulk endpoint and `state["individual"]`
    (api_id -> value dict) from the individual endpoint."""
    import hansard_archive.pq_ingestor as ing

    state = {"bulk": [], "individual": {}, "bulk_params": []}

    def fake_get(url, params=None, timeout=None):
        if url == ing.WQ_API_BASE:
            state["bulk_params"].append(dict(params))
            skip = params.get("skip", 0)
            return _Resp({"results": state["bulk"][skip:skip + params["take"]]})
        api_id = int(url.rsplit("/", 1)[1])
        state.setdefault("individual_calls", []).append(api_id)
        return _Resp({"value": state["individual"].get(api_id)})

    monkeypatch.setattr(ing.requests, "get", fake_get)
    monkeypatch.setattr(ing.time, "sleep", lambda s: None)
    return state


def _ingest(**kw):
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    kw.setdefault("skip_answer_fetch", True)
    return ingest_pq_date_range(date(2026, 5, 13), date(2026, 10, 20), verbose=False, **kw)


# ── ingestor ──────────────────────────────────────────────────────────────────

def test_same_uin_two_sessions_two_rows_no_overwrite(db, fake_api):
    from hansard_archive.models import HaPQ
    old = _pq(api_id=100, uin="6978", tabled_date=date(2025, 6, 2),
              heading="Old session question", question_text="Old text about fish.")
    db.session.add(old)
    db.session.commit()

    fake_api["bulk"] = [_value(200, "6978", "2026-06-04", heading="New session question",
                               text="New text about trains.")]
    res = _ingest()

    assert res["inserted"] == 1 and res["updated"] == 0 and res["identity_mismatch"] == 0
    rows = HaPQ.query.filter_by(uin="6978").order_by(HaPQ.tabled_date).all()
    assert len(rows) == 2
    assert rows[0].api_id == 100 and rows[0].question_text == "Old text about fish."
    assert rows[0].heading == "Old session question"
    assert rows[1].api_id == 200 and rows[1].heading == "New session question"


def test_update_by_api_id_refreshes_heading_keeps_members(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=300, uin="hl4290", heading=None, chamber=None,
                       tabled_date=date(2026, 10, 19), asking_member="Baroness Test",
                       asking_mnis_id=42))
    db.session.commit()

    fake_api["bulk"] = [_value(300, "HL4290", "2026-10-19", heading="Railways: Fares",
                               asking=None, asking_id=None, house="Lords")]
    res = _ingest()

    assert res["updated"] == 1 and res["inserted"] == 0
    row = HaPQ.query.filter_by(api_id=300).one()
    assert row.heading == "Railways: Fares"
    assert row.uin == "HL4290"            # case-only difference refreshed
    assert row.chamber == "Lords"
    assert row.asking_member == "Baroness Test"   # not blanked by a null from the API
    assert row.asking_mnis_id == 42
    assert all(p.get("expandMember") == "true" for p in fake_api["bulk_params"])


def test_expand_member_names_are_stored(db, fake_api):
    from hansard_archive.models import HaPQ
    fake_api["bulk"] = [_value(301, "33618", "2026-10-12", asking="Jim Shannon", asking_id=4131)]
    _ingest()
    row = HaPQ.query.filter_by(api_id=301).one()
    assert row.asking_member == "Jim Shannon" and row.asking_mnis_id == 4131


def test_date_uin_match_fills_missing_api_id(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=None, uin="555", heading=None))
    db.session.commit()
    fake_api["bulk"] = [_value(555000, "555", "2026-06-04", heading="Schools")]
    res = _ingest()
    assert res["updated"] == 1 and res["inserted"] == 0
    row = HaPQ.query.filter_by(uin="555").one()
    assert row.api_id == 555000 and row.heading == "Schools"


def test_identity_mismatch_counted_and_row_untouched(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=400, uin="10", heading="Original", tabled_date=date(2026, 5, 13)))
    db.session.commit()
    fake_api["bulk"] = [_value(400, "11", "2026-05-13", heading="Something else")]

    res = _ingest(max_identity_mismatches=5)

    assert res["identity_mismatch"] == 1 and res["updated"] == 0 and res["inserted"] == 0
    assert res["errors"] >= 1
    row = HaPQ.query.filter_by(api_id=400).one()
    assert row.uin == "10" and row.heading == "Original"
    assert HaPQ.query.count() == 1


def test_date_uin_row_with_other_api_id_is_mismatch(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=1904612, uin="444", tabled_date=date(2026, 5, 13)))
    db.session.commit()
    fake_api["bulk"] = [_value(1721729, "444", "2026-05-13", heading="Wrong")]
    res = _ingest()
    assert res["identity_mismatch"] == 1 and res["inserted"] == 0
    assert HaPQ.query.one().api_id == 1904612


def test_tripwire_aborts_above_threshold(db, fake_api):
    from hansard_archive.models import HaPQ
    from hansard_archive.pq_ingestor import PQIdentityMismatchError
    for i in range(3):
        db.session.add(_pq(api_id=500 + i, uin=str(10 + i), tabled_date=date(2026, 5, 13)))
    db.session.add(_pq(api_id=900, uin="900", heading=None, tabled_date=date(2026, 5, 13)))
    db.session.commit()
    fake_api["bulk"] = [_value(900, "900", "2026-05-13", heading="Would be written")] + [
        _value(500 + i, str(90 + i), "2026-05-13") for i in range(3)
    ]

    with pytest.raises(PQIdentityMismatchError) as exc:
        _ingest(max_identity_mismatches=2)

    assert exc.value.counts["identity_mismatch"] == 3
    # The uncommitted batch was rolled back when the run aborted
    assert HaPQ.query.filter_by(api_id=900).one().heading is None


def test_question_text_change_clears_themes(db, fake_api):
    from hansard_archive.models import HaPQ, HaPQTheme
    pq = _pq(api_id=600, uin="600", question_text="Old wording of the question.")
    db.session.add(pq)
    db.session.flush()
    db.session.add(HaPQTheme(pq_id=pq.id, theme="fish", theme_type="specific"))
    db.session.commit()

    fake_api["bulk"] = [_value(600, "600", "2026-06-04", text="New wording (truncated")]
    fake_api["individual"] = {600: {"questionText": "New wording of the question, in full.",
                                    "answerIsHolding": False, "isWithdrawn": False}}
    res = _ingest(skip_answer_fetch=False)

    assert res["updated"] == 1
    row = HaPQ.query.filter_by(api_id=600).one()
    assert row.question_text == "New wording of the question, in full."
    assert HaPQTheme.query.filter_by(pq_id=row.id).count() == 0


def test_bulk_only_pass_never_replaces_text_or_clears_tags(db, fake_api):
    from hansard_archive.models import HaPQ, HaPQTheme
    pq = _pq(api_id=601, uin="601", heading=None, question_text="Full stored text, longer than bulk.")
    db.session.add(pq)
    db.session.flush()
    db.session.add(HaPQTheme(pq_id=pq.id, theme="fish", theme_type="specific"))
    db.session.commit()

    fake_api["bulk"] = [_value(601, "601", "2026-06-04", heading="Fish", text="Full stored text")]
    _ingest(skip_answer_fetch=True)

    row = HaPQ.query.filter_by(api_id=601).one()
    assert row.heading == "Fish"
    assert row.question_text == "Full stored text, longer than bulk."
    assert HaPQTheme.query.filter_by(pq_id=row.id).count() == 1


def test_unchanged_row_keeps_updated_at(db, fake_api):
    from hansard_archive.models import HaPQ
    stamp = datetime(2026, 6, 5, 12, 0, 0)
    db.session.add(_pq(api_id=700, uin="700", heading="Same", updated_at=stamp,
                       answering_body="Department for Testing", answering_body_id=7))
    db.session.commit()
    fake_api["bulk"] = [_value(700, "700", "2026-06-04", heading="Same")]

    res = _ingest()

    assert res["unchanged"] == 1 and res["updated"] == 0
    assert HaPQ.query.filter_by(api_id=700).one().updated_at == stamp


def test_bad_row_does_not_roll_back_the_batch(db, fake_api, monkeypatch):
    import hansard_archive.pq_ingestor as ing
    from hansard_archive.models import HaPQ

    real_upsert = ing.upsert_pq

    def flaky(fields, **kw):
        if fields["uin"] == "BAD":
            db.session.add(HaPQ(uin="BAD", question_text=None, tabled_date=date(2026, 6, 4)))
            db.session.flush()   # NOT NULL violation inside this row's savepoint
        return real_upsert(fields, **kw)

    monkeypatch.setattr(ing, "upsert_pq", flaky)
    fake_api["bulk"] = [_value(801, "801", "2026-06-04"), _value(802, "BAD", "2026-06-04"),
                        _value(803, "803", "2026-06-04")]
    res = _ingest()

    assert res["inserted"] == 2 and res["failed"] == 1
    assert {r.uin for r in HaPQ.query.all()} == {"801", "803"}


def test_dry_run_writes_nothing(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=850, uin="850", heading=None))
    db.session.commit()
    fake_api["bulk"] = [_value(850, "850", "2026-06-04", heading="Predicted"),
                        _value(851, "851", "2026-06-04")]
    seen = []
    res = _ingest(dry_run=True, on_outcome=lambda f, o: seen.append(o))

    assert res["updated"] == 1 and res["inserted"] == 1
    assert seen[0].changes["heading"] == (None, "Predicted")
    assert HaPQ.query.count() == 1
    assert HaPQ.query.one().heading is None


def test_backfill_script_dry_run_uses_ingestor(db, fake_api, monkeypatch, app):
    """scripts/backfill_pq_headings.py — dry run by default, nothing written."""
    import importlib.util
    import sys
    import types
    from hansard_archive.models import HaPQ

    db.session.add(_pq(api_id=870, uin="870", heading=None))
    db.session.commit()
    fake_api["bulk"] = [_value(870, "870", "2026-06-04", heading="Backfilled")]
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=app))

    spec = importlib.util.spec_from_file_location(
        "backfill_pq_headings", PROJECT_ROOT / "scripts" / "backfill_pq_headings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["--since", "2026-06-01", "--until", "2026-06-10"])

    assert rc == 0
    assert HaPQ.query.one().heading is None
    assert all(p["tabledWhenFrom"] >= "2026-06-01" for p in fake_api["bulk_params"])


def test_answered_pass_picks_up_late_answer(db, fake_api):
    """UIN 30428 case: tabled 9 Oct, answered 16 Oct — outside a 7-day tabled
    window by the time the answer lands. The answered-date pass fixes it."""
    from hansard_archive.models import HaPQ
    stamp = datetime(2026, 10, 10, 9, 0, 0)
    db.session.add(_pq(api_id=1946000, uin="30428", heading="Schools",
                       tabled_date=date(2026, 10, 9), is_answered=False, updated_at=stamp,
                       answering_body="Department for Testing", answering_body_id=7))
    db.session.commit()
    fake_api["bulk"] = [_value(1946000, "30428", "2026-10-09", heading="Schools",
                               answer="<p>Short bulk answer</p>", answered="2026-10-16")]
    fake_api["individual"] = {1946000: {
        "questionText": "To ask the Secretary of State, what steps are being taken.",
        "answerText": "<p>The full answer, much longer than the bulk one.</p>",
        "answerIsHolding": False, "isWithdrawn": False}}

    from hansard_archive.pq_ingestor import ingest_pq_date_range
    res = ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                               date_field="answered", fetch_only_new_answers=True)

    assert res["updated"] == 1
    assert "answeredWhenFrom" in fake_api["bulk_params"][0]
    assert "tabledWhenFrom" not in fake_api["bulk_params"][0]
    row = HaPQ.query.filter_by(api_id=1946000).one()
    assert row.is_answered is True and row.answer_date == date(2026, 10, 16)
    assert "full answer" in row.answer_text
    assert row.updated_at != stamp

    # Second run: answer already stored -> no individual fetch, nothing changes
    fake_api["individual_calls"] = []
    res = ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                               date_field="answered", fetch_only_new_answers=True)
    assert res["unchanged"] == 1 and fake_api["individual_calls"] == []
    assert "full answer" in HaPQ.query.filter_by(api_id=1946000).one().answer_text


# ── tagger ────────────────────────────────────────────────────────────────────

def _batch(db):
    from hansard_archive.models import HaPQ
    rows = [HaPQ(uin="6978", api_id=1, question_text="What support is there for topic 1 fishing?",
                 tabled_date=date(2026, 6, 4)),
            HaPQ(uin="6978", api_id=2, question_text="What support is there for topic 2 trains?",
                 tabled_date=date(2025, 6, 2)),
            HaPQ(uin="7000", api_id=3, question_text="What support is there for topic 3 schools?",
                 tabled_date=date(2026, 6, 3))]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def _result(uin, n):
    # Evidence quotes come from question n's own text, so they verify only there.
    return {"uin": uin,
            "policy_areas": [{"area": "Transport", "evidence": f"support is there for topic {n}"}],
            "topics": [{"topic": f"topic {n}", "evidence": f"support is there for topic {n}"}]}


def _run_tagger(monkeypatch, batch_result):
    import hansard_archive.tagger as tg
    calls = []
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    monkeypatch.setattr(tg, "_detect_model", lambda key: "test-model")
    monkeypatch.setattr(tg, "_gemini_json", lambda key, model, prompt, schema: (batch_result, {}))
    # Single-question retries are recorded and return nothing verified
    monkeypatch.setattr(tg, "tag_pq_input",
                        lambda pq, api_key, model=None: calls.append(int(pq.ref)) or tg.TagOutcome())
    res = tg.tag_pq_all_untagged(api_key="test-key", verbose=False)
    return res, calls


def _themes(pq_id):
    from hansard_archive.models import HaPQTheme
    return {t.theme for t in HaPQTheme.query.filter_by(pq_id=pq_id, theme_type="specific")}


def test_tagger_maps_results_by_position(db, monkeypatch):
    rows = _batch(db)   # tagger orders by tabled_date desc: 6978(2026), 7000, 6978(2025)
    ordered = sorted(rows, key=lambda r: r.tabled_date, reverse=True)
    result = [_result(r.uin, r.api_id) for r in ordered]

    res, retried = _run_tagger(monkeypatch, result)

    assert retried == [] and res["tagged"] == 3
    for r in rows:   # same UIN in two sessions still gets its own tags
        assert _themes(r.id) == {f"topic {r.api_id}"}


def test_tagger_retries_when_echoed_uin_disagrees(db, monkeypatch):
    rows = _batch(db)
    ordered = sorted(rows, key=lambda r: r.tabled_date, reverse=True)
    result = [_result(r.uin, r.api_id) for r in ordered]
    result[0], result[1] = result[1], result[0]   # model swapped two answers

    res, retried = _run_tagger(monkeypatch, result)

    assert sorted(retried) == sorted([ordered[0].id, ordered[1].id])
    assert _themes(ordered[2].id) == {f"topic {ordered[2].api_id}"}
    assert _themes(ordered[0].id) == set() and _themes(ordered[1].id) == set()


def test_tagger_rejects_evidence_from_another_question(db, monkeypatch):
    """Same UIN at two positions: a result whose quote belongs to the other
    question fails the evidence check and is retried, not written."""
    rows = _batch(db)
    ordered = sorted(rows, key=lambda r: r.tabled_date, reverse=True)
    result = [_result(r.uin, r.api_id) for r in ordered]
    result[0] = _result(ordered[0].uin, ordered[2].api_id)   # right UIN, other question's words

    res, retried = _run_tagger(monkeypatch, result)

    assert retried == [ordered[0].id]
    assert _themes(ordered[0].id) == set()


def test_tagger_retries_all_when_result_count_differs(db, monkeypatch):
    rows = _batch(db)
    res, retried = _run_tagger(monkeypatch, [_result("7000", 3)])
    assert sorted(retried) == sorted(r.id for r in rows)


# ── title fallback ────────────────────────────────────────────────────────────

def test_display_heading_fallback():
    from hansard_archive.models import pq_display_heading
    assert pq_display_heading("Railways: Fares", "anything") == "Railways: Fares"
    long_text = ("To ask the Secretary of State for Education, what assessment she has made "
                 "of the adequacy of funding for schools.")
    out = pq_display_heading(None, long_text)
    assert out.endswith("…") and len(out) <= 71
    assert long_text.startswith(out[:-1])
    assert not out[:-1].endswith(" ")
    assert pq_display_heading("  ", "Short question?") == "Short question?"
    assert pq_display_heading(None, None) == "Written Question"


def test_pq_page_title_never_bare_uin(db, app):
    db.session.add(_pq(api_id=990, uin="6978", heading=None, tabled_date=date(2026, 6, 4),
                       question_text="To ask the Secretary of State for Transport, what steps "
                                     "he is taking to improve rail punctuality in the North."))
    db.session.commit()

    html = app.test_client().get("/archive/pq/6978").get_data(as_text=True)

    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
    h1 = re.search(r'<h1 class="sd-title">(.*?)</h1>', html, re.S).group(1).strip()
    og = re.search(r'<meta property="og:title" content="(.*?)">', html, re.S).group(1)
    assert title.startswith("To ask the Secretary of State for Transport")
    assert "Written Question 6978, 4 June 2026" in title
    assert h1 != "6978" and h1.startswith("To ask the Secretary")
    assert not og.startswith("6978")
    assert '"headline": "6978"' not in html


# ── paging: failed pages, malformed payloads (review finding 1) ───────────────

class _Runaway(BaseException):
    """Not an Exception, so the ingestor's retry handler can't swallow it."""


@pytest.fixture
def paged_api(monkeypatch):
    """Bulk endpoint with 2-row pages. `state["behaviour"](skip, attempt)` may
    return a payload dict, raise, or return None for the normal page."""
    import hansard_archive.pq_ingestor as ing

    monkeypatch.setattr(ing, "_PAGE_SIZE", 2)
    monkeypatch.setattr(ing.time, "sleep", lambda s: None)
    state = {"rows": [], "skips": [], "attempts": {}, "behaviour": lambda skip, n: None,
             "status": 200, "total": None}

    def fake_get(url, params=None, timeout=None):
        if len(state["skips"]) > 200:
            raise _Runaway("ingestor kept paging")
        assert url == ing.WQ_API_BASE
        skip = params["skip"]
        state["skips"].append(skip)
        n = state["attempts"].get(skip, 0)
        state["attempts"][skip] = n + 1
        payload = state["behaviour"](skip, n)
        if payload is None:
            total = len(state["rows"]) if state["total"] is None else state["total"]
            payload = {"totalResults": total,
                       "results": state["rows"][skip:skip + params["take"]]}
        resp = _Resp(payload)
        resp.status_code = state["status"]
        return resp

    monkeypatch.setattr(ing.requests, "get", fake_get)
    return state


def _rows(n, start=5000):
    return [_value(start + i, str(start + i), "2026-06-04") for i in range(n)]


def test_every_page_failing_aborts_instead_of_looping(db, paged_api):
    import requests as _rq
    from hansard_archive.pq_ingestor import PQPageFailureError

    def down(skip, n):
        raise _rq.ConnectionError("API down")
    paged_api["behaviour"] = down

    with pytest.raises(PQPageFailureError) as exc:
        _ingest()

    assert exc.value.counts["page_errors"] == 3          # DEFAULT_MAX_FAILED_PAGES
    assert exc.value.counts["errors"] >= 3
    assert len(paged_api["skips"]) == 3 * 5               # 3 pages x 5 attempts (no fallback below 100)


def test_one_failed_page_then_recovery_is_reported(db, paged_api):
    import requests as _rq
    from hansard_archive.models import HaPQ
    paged_api["rows"] = _rows(5)                          # pages at skip 0, 2, 4

    def middle_fails(skip, n):
        if skip == 2:
            raise _rq.Timeout("slow")
    paged_api["behaviour"] = middle_fails

    res = _ingest()

    assert res["inserted"] == 3 and res["page_errors"] == 1 and res["errors"] >= 1
    assert HaPQ.query.count() == 3
    assert sorted(set(paged_api["skips"])) == [0, 2, 4]


def test_failed_last_page_stops_at_total_results(db, paged_api):
    import requests as _rq
    paged_api["rows"] = _rows(4)                          # pages at skip 0, 2

    def last_fails(skip, n):
        if skip >= 2:
            raise _rq.Timeout("slow")
    paged_api["behaviour"] = last_fails

    res = _ingest()

    assert res["inserted"] == 2 and res["page_errors"] == 1
    assert 4 not in paged_api["skips"]                     # never walked past totalResults


def test_empty_page_before_total_results_is_an_error(db, paged_api):
    paged_api["rows"] = _rows(2)
    paged_api["total"] = 6                                 # API claims more than it serves
    res = _ingest()
    assert res["inserted"] == 2 and res["page_errors"] == 1


def test_error_body_without_results_is_retried(db, paged_api):
    paged_api["rows"] = _rows(1)
    paged_api["behaviour"] = lambda skip, n: ({"status": 500, "title": "Server error"}
                                              if n == 0 else None)
    res = _ingest()
    assert res["inserted"] == 1 and res["page_errors"] == 0
    assert paged_api["attempts"][0] == 2


def test_permanent_api_error_aborts(db, paged_api):
    from hansard_archive.pq_ingestor import PQPageFailureError
    paged_api["status"] = 403
    with pytest.raises(PQPageFailureError) as exc:
        _ingest()
    assert exc.value.counts["page_errors"] == 1
    assert paged_api["skips"] == [0]


def test_mismatch_abort_does_not_count_rolled_back_writes(db, fake_api):
    from hansard_archive.pq_ingestor import PQIdentityMismatchError
    db.session.add(_pq(api_id=910, uin="910", heading=None, tabled_date=date(2026, 5, 13)))
    db.session.add(_pq(api_id=911, uin="911", tabled_date=date(2026, 5, 13)))
    db.session.commit()
    fake_api["bulk"] = [_value(910, "910", "2026-05-13", heading="Rolled back"),
                        _value(912, "912", "2026-05-13"),
                        _value(911, "999", "2026-05-13")]          # mismatch
    with pytest.raises(PQIdentityMismatchError) as exc:
        _ingest(max_identity_mismatches=0)
    c = exc.value.counts
    assert c["updated"] == 0 and c["inserted"] == 0 and c["rolled_back"] == 2


# ── answers: never lock in the truncated bulk copy (review finding 2) ─────────

_CUT = "<p>" + "x" * 252 + "..."      # 258 chars, like the bulk endpoint's copy


def test_bulk_only_insert_stores_no_answer_text(db, fake_api):
    from hansard_archive.models import HaPQ
    fake_api["bulk"] = [_value(1001, "1001", "2026-06-04", answer=_CUT, answered="2026-06-10")]
    _ingest(skip_answer_fetch=True)
    row = HaPQ.query.filter_by(api_id=1001).one()
    assert row.is_answered is True and row.answer_date == date(2026, 6, 10)
    assert row.answer_text is None


def test_bulk_only_update_marks_answered_but_writes_no_answer(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=1002, uin="1002", is_answered=False))
    db.session.commit()
    fake_api["bulk"] = [_value(1002, "1002", "2026-06-04", answer=_CUT, answered="2026-06-10")]
    _ingest(skip_answer_fetch=True)
    row = HaPQ.query.filter_by(api_id=1002).one()
    assert row.is_answered is True and row.answer_text is None


@pytest.mark.parametrize("stored", [None, "", _CUT, "<p>" + "y" * 250 + "..."])
def test_answered_pass_refetches_missing_or_truncated_answer(db, fake_api, stored):
    from hansard_archive.models import HaPQ
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    db.session.add(_pq(api_id=1003, uin="1003", is_answered=True, answer_text=stored,
                       answer_date=date(2026, 10, 15)))
    db.session.commit()
    fake_api["bulk"] = [_value(1003, "1003", "2026-06-04", answer=_CUT, answered="2026-10-15")]
    fake_api["individual"] = {1003: {
        "questionText": "To ask the Secretary of State, what steps are being taken.",
        "answerText": "<p>The complete answer.</p>", "answerIsHolding": False,
        "isWithdrawn": False}}

    ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                         date_field="answered", fetch_only_new_answers=True)

    assert fake_api["individual_calls"] == [1003]
    assert HaPQ.query.filter_by(api_id=1003).one().answer_text == "<p>The complete answer.</p>"


def test_failed_individual_fetch_leaves_answer_for_next_run(db, fake_api):
    from hansard_archive.models import HaPQ
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    fake_api["bulk"] = [_value(1004, "1004", "2026-10-10", answer=_CUT, answered="2026-10-15")]
    fake_api["individual"] = {}          # individual endpoint returns nothing
    ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                         date_field="answered", fetch_only_new_answers=True)
    row = HaPQ.query.filter_by(api_id=1004).one()
    assert row.is_answered is True and row.answer_text is None   # not the cut-off copy

    fake_api["individual_calls"] = []
    ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                         date_field="answered", fetch_only_new_answers=True)
    assert fake_api["individual_calls"] == [1004]                 # tried again


def test_looks_truncated():
    from hansard_archive.pq_ingestor import _looks_truncated
    assert _looks_truncated(_CUT)
    assert _looks_truncated("z" * 258)
    assert _looks_truncated("<p>" + "y" * 250 + "...")            # 256 chars
    assert not _looks_truncated(None) and not _looks_truncated("")
    assert not _looks_truncated("<p>A full answer.</p>")
    assert not _looks_truncated("w" * 400 + "...")


def _load_script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_headings_backfill_apply_never_writes_answer_text(db, fake_api, monkeypatch, app):
    import sys
    import types
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=1005, uin="1005", heading=None, is_answered=False))
    db.session.commit()
    fake_api["bulk"] = [
        _value(1005, "1005", "2026-06-04", heading="Fish", answer=_CUT, answered="2026-06-09"),
        _value(1006, "1006", "2026-06-04", heading="New", answer=_CUT, answered="2026-06-09"),
    ]
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=app))
    mod = _load_script("backfill_pq_headings")

    rc = mod.main(["--since", "2026-06-01", "--until", "2026-06-10", "--apply"])

    assert rc == 0
    rows = {r.api_id: r for r in HaPQ.query.all()}
    assert rows[1005].heading == "Fish" and rows[1005].is_answered is True
    assert rows[1005].answer_text is None and rows[1006].answer_text is None
    assert fake_api.get("individual_calls", []) == []


# ── cron alerting (review finding 3) ──────────────────────────────────────────

@pytest.fixture
def cron(monkeypatch, app, db):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=app))
    mod = _load_script("ingest_pq_cron")
    sent = []
    monkeypatch.setattr(mod, "_send_failure_alert",
                        lambda service, subject, body: sent.append((subject, body)))
    monkeypatch.setattr(sys, "argv", ["ingest_pq_cron.py", "--no-tag", "--service-name", "pq-test"])
    return mod, sent


def _counts(**kw):
    base = {"inserted": 0, "updated": 0, "unchanged": 5, "identity_mismatch": 0,
            "failed": 0, "page_errors": 0, "rolled_back": 0}
    base.update(kw)
    base["errors"] = base["failed"] + base["page_errors"] + base["identity_mismatch"]
    return base


def test_cron_clean_run_sends_no_alert(cron, monkeypatch):
    mod, sent = cron
    monkeypatch.setattr(mod, "ingest_pq_date_range", lambda *a, **k: _counts(inserted=2))
    mod.main()
    assert sent == []


@pytest.mark.parametrize("key", ["identity_mismatch", "failed", "page_errors"])
def test_cron_alerts_once_on_a_single_problem(cron, monkeypatch, key):
    mod, sent = cron
    monkeypatch.setattr(mod, "ingest_pq_date_range", lambda *a, **k: _counts(**{key: 1}))
    with pytest.raises(SystemExit):
        mod.main()
    assert len(sent) == 1                       # both passes had a problem: still one email
    assert f"{key}=1" in sent[0][1]


def test_cron_alert_uses_counts_from_an_aborted_pass(cron, monkeypatch):
    from hansard_archive.pq_ingestor import PQIdentityMismatchError
    mod, sent = cron
    calls = []

    def aborting(*a, **k):
        calls.append(k.get("date_field", "tabled"))
        raise PQIdentityMismatchError("too many mismatches",
                                      _counts(inserted=4, identity_mismatch=6, rolled_back=3))
    monkeypatch.setattr(mod, "ingest_pq_date_range", aborting)

    with pytest.raises(SystemExit):
        mod.main()

    assert calls == ["tabled"]                  # answered pass skipped after an abort
    assert len(sent) == 1
    body = sent[0][1]
    assert "identity_mismatch=6" in body and "rolled_back=3" in body and "inserted=4" in body
    assert "PQIdentityMismatchError" in body


def test_invalid_env_values_do_not_break_import():
    import os
    import subprocess
    import sys
    env = dict(os.environ, PQ_MAX_FAILED_PAGES="three", PQ_MAX_IDENTITY_MISMATCHES="-1")
    out = subprocess.run(
        [sys.executable, "-c",
         "import hansard_archive.pq_ingestor as m;"
         "print(m.DEFAULT_MAX_FAILED_PAGES, m.DEFAULT_MAX_IDENTITY_MISMATCHES)"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["3", "5"]


# ── re-review: stale holding answers, commit per page ─────────────────────────

_FULL = {"questionText": "To ask the Secretary of State, what steps are being taken.",
         "answerText": "<p>The substantive answer.</p>", "answerIsHolding": False,
         "isWithdrawn": False}


def _holding_row(db, api_id):
    db.session.add(_pq(api_id=api_id, uin=str(api_id), is_answered=True, is_holding=True,
                       answer_text="<p>I will reply as soon as possible.</p>",
                       answer_date=date(2026, 9, 10)))
    db.session.commit()


def _later_answer(api_id):
    v = _value(api_id, str(api_id), "2026-06-04", answer=_CUT, answered="2026-10-15")
    v["value"]["answerIsHolding"] = False
    return v


def test_bulk_only_pass_keeps_holding_answer_metadata(db, fake_api):
    """Holding answer stored, real answer published later: a bulk-only pass
    must not move answer_date/is_holding without the text, or the answer
    would look up to date and never be re-fetched."""
    from hansard_archive.models import HaPQ
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    _holding_row(db, 1101)
    fake_api["bulk"] = [_later_answer(1101)]

    _ingest(skip_answer_fetch=True)          # e.g. the headings backfill

    row = HaPQ.query.filter_by(api_id=1101).one()
    assert row.answer_date == date(2026, 9, 10) and row.is_holding is True
    assert "as soon as possible" in row.answer_text

    fake_api["individual"] = {1101: _FULL}
    ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                         date_field="answered", fetch_only_new_answers=True)
    db.session.expire_all()
    row = HaPQ.query.filter_by(api_id=1101).one()
    assert fake_api["individual_calls"] == [1101]
    assert row.answer_text == "<p>The substantive answer.</p>"
    assert row.answer_date == date(2026, 10, 15) and row.is_holding is False


def test_failed_full_fetch_keeps_holding_answer_for_next_run(db, fake_api):
    from hansard_archive.models import HaPQ
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    _holding_row(db, 1102)
    fake_api["bulk"] = [_later_answer(1102)]
    fake_api["individual"] = {}              # individual endpoint fails

    for _ in range(2):
        ingest_pq_date_range(date(2026, 10, 14), date(2026, 10, 20), verbose=False,
                             date_field="answered", fetch_only_new_answers=True)
    db.session.expire_all()
    row = HaPQ.query.filter_by(api_id=1102).one()
    assert row.answer_date == date(2026, 9, 10) and row.is_holding is True
    assert fake_api["individual_calls"] == [1102, 1102]     # still re-tried


def test_bulk_only_pass_sets_answer_date_when_no_answer_stored(db, fake_api):
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=1103, uin="1103", is_answered=False))
    db.session.commit()
    fake_api["bulk"] = [_later_answer(1103)]
    _ingest(skip_answer_fetch=True)
    row = HaPQ.query.filter_by(api_id=1103).one()
    assert row.is_answered is True and row.answer_date == date(2026, 10, 15)
    assert row.answer_text is None


def test_commits_after_every_page(db, paged_api, monkeypatch):
    paged_api["rows"] = _rows(5)                          # pages at skip 0, 2, 4
    commits = []
    real_commit = db.session.commit

    def counting_commit():
        commits.append(1)
        real_commit()
    monkeypatch.setattr(db.session, "commit", counting_commit)
    res = _ingest()
    assert res["inserted"] == 5
    assert len(commits) == 3


def test_stage_b_uses_the_ingestor_truncation_rule(db, monkeypatch):
    import dotenv
    from hansard_archive.models import HaPQ
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # never read a .env
    mod = _load_script("backfill_pq_answers")
    stored = {
        1201: _CUT,                                     # exactly the cap
        1202: "<p>" + "y" * 250 + "...",                # near the cap, ends in "..."
        1203: None,                                     # answered, no text yet
        1204: "<p>A complete short answer.</p>",       # fine
        1205: "w" * 400 + "...",                        # long, not a bulk copy
    }
    for api_id, text in stored.items():
        db.session.add(_pq(api_id=api_id, uin=str(api_id), is_answered=True, answer_text=text,
                           answer_date=date(2026, 9, 1)))
    db.session.commit()
    fetched = []

    def fake_full(api_id):
        fetched.append(api_id)
        return "<p>" + "z" * 600 + "</p>", False, False, date(2026, 9, 3)
    monkeypatch.setattr(mod, "_get_full_answer", fake_full)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    mod.stage_b(db, HaPQ)

    assert sorted(fetched) == [1201, 1202, 1203]
    db.session.expire_all()
    row = HaPQ.query.filter_by(api_id=1203).one()
    assert row.answer_text.startswith("<p>zzz") and row.answer_date == date(2026, 9, 3)


# ── holding answers replaced and corrected answers (accuracy review) ──────────
# Real shapes (live API, 22 Oct 2026): a holding answer has answerIsHolding=true
# and dateAnswered = dateHoldingAnswer; the real answer keeps dateHoldingAnswer
# and sets dateAnswered — the same date when it lands on the same day. A
# correction keeps dateAnswered and sets dateAnswerCorrected + answerIsCorrection.

_Q = "To ask the Secretary of State, what steps are being taken."


def _answered_value(api_id, uin, tabled, answer, answered, holding=False, corrected=None):
    v = _value(api_id, uin, tabled, answer=answer, answered=answered)
    v["value"].update({
        "answerIsHolding": holding,
        "answerIsCorrection": corrected is not None,
        "dateAnswerCorrected": f"{corrected}T00:00:00" if corrected else None,
    })
    return v


def _full(answer, answered, holding=False, corrected=None, held=None):
    return {"questionText": _Q, "answerText": answer, "answerIsHolding": holding,
            "isWithdrawn": False, "dateAnswered": f"{answered}T00:00:00",
            "dateHoldingAnswer": f"{held}T00:00:00" if held else None,
            "answerIsCorrection": corrected is not None,
            "dateAnswerCorrected": f"{corrected}T00:00:00" if corrected else None}


def _window(date_field, lo=date(2026, 10, 16), hi=date(2026, 10, 22)):
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    return ingest_pq_date_range(lo, hi, verbose=False, date_field=date_field,
                                fetch_only_new_answers=True)


def test_holding_answer_replaced_same_day_is_refetched(db, fake_api):
    """Tabled 2 Oct (outside the tabled window); holding answer and the real
    answer both dated 21 Oct. answer_date is unchanged, so only the holding
    flag shows the answer changed."""
    from hansard_archive.models import HaPQ
    holding_text = "<p>I will reply to the hon. Member as soon as possible.</p>"
    db.session.add(_pq(api_id=1949532, uin="35177", tabled_date=date(2026, 10, 2),
                       is_answered=True, is_holding=True, answer_date=date(2026, 10, 21),
                       answer_text=holding_text))
    db.session.commit()
    fake_api["bulk"] = [_answered_value(1949532, "35177", "2026-10-02",
                                        "<p>The real answer.</p>", "2026-10-21")]
    fake_api["individual"] = {1949532: _full("<p>The real answer, in full.</p>",
                                             "2026-10-21", held="2026-10-21")}

    res = _window("answered")

    assert fake_api["individual_calls"] == [1949532]
    assert res["updated"] == 1
    row = HaPQ.query.filter_by(api_id=1949532).one()
    assert row.is_holding is False
    assert row.answer_text == "<p>The real answer, in full.</p>"
    assert row.answer_date == date(2026, 10, 21)

    fake_api["individual_calls"] = []
    res = _window("answered")
    assert res["unchanged"] == 1 and fake_api["individual_calls"] == []


def test_correction_a_week_later_is_refetched_by_corrected_pass(db, fake_api):
    """Answered 8 Oct, corrected 15 Oct: dateAnswered stays 8 Oct, so the
    answered window (16-22 Oct) never sees it; the corrected window does."""
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=1933528, uin="23043", tabled_date=date(2026, 9, 28),
                       is_answered=True, answer_date=date(2026, 10, 8),
                       answer_text="<p>Was not entitled to a payment.</p>"))
    db.session.commit()
    fake_api["bulk"] = [_answered_value(1933528, "23043", "2026-09-28",
                                        "<p>Was eligible for and received a payment.</p>",
                                        "2026-10-08", corrected="2026-10-15")]
    fake_api["individual"] = {1933528: _full("<p>Was eligible for and received a payment.</p>",
                                             "2026-10-08", corrected="2026-10-15")}

    res = _window("corrected")

    params = fake_api["bulk_params"][0]
    assert params["correctedWhenFrom"] == "2026-10-16" and params["correctedWhenTo"] == "2026-10-22"
    assert "answeredWhenFrom" not in params and "tabledWhenFrom" not in params
    assert fake_api["individual_calls"] == [1933528] and res["updated"] == 1
    row = HaPQ.query.filter_by(api_id=1933528).one()
    assert row.answer_text == "<p>Was eligible for and received a payment.</p>"
    assert row.answer_corrected_date == date(2026, 10, 15)
    assert row.answer_date == date(2026, 10, 8)            # original answer date kept

    fake_api["individual_calls"] = []
    res = _window("corrected")
    assert res["unchanged"] == 1 and fake_api["individual_calls"] == []


def test_unchanged_answer_is_not_refetched(db, fake_api):
    from hansard_archive.models import HaPQ
    same = dict(answering_body="Department for Testing", answering_body_id=7)
    db.session.add(_pq(api_id=1950000, uin="36000", tabled_date=date(2026, 10, 1),
                       is_answered=True, is_holding=False, answer_date=date(2026, 10, 20),
                       answer_text="<p>A complete answer.</p>", **same))
    db.session.add(_pq(api_id=1950001, uin="36001", tabled_date=date(2026, 9, 1),
                       is_answered=True, answer_date=date(2026, 9, 10),
                       answer_corrected_date=date(2026, 10, 18),
                       answer_text="<p>A corrected answer.</p>", **same))
    db.session.commit()

    fake_api["bulk"] = [_answered_value(1950000, "36000", "2026-10-01",
                                        "<p>A complete answer.</p>", "2026-10-20")]
    res = _window("answered")
    assert res["unchanged"] == 1

    fake_api["bulk"] = [_answered_value(1950001, "36001", "2026-09-01",
                                        "<p>A corrected answer.</p>", "2026-09-10",
                                        corrected="2026-10-18")]
    res = _window("corrected")
    assert res["unchanged"] == 1
    assert fake_api.get("individual_calls", []) == []
    assert HaPQ.query.filter_by(api_id=1950001).one().answer_corrected_date == date(2026, 10, 18)


def test_failed_fetch_of_a_correction_keeps_old_values_and_retries(db, fake_api):
    """Without the full record neither the text nor the correction date
    changes, so the next run still sees the difference and tries again."""
    from hansard_archive.models import HaPQ
    db.session.add(_pq(api_id=1938330, uin="27363", tabled_date=date(2026, 9, 4),
                       is_answered=True, answer_date=date(2026, 9, 14),
                       answer_text="<p>Old answer.</p>"))
    db.session.commit()
    fake_api["bulk"] = [_answered_value(1938330, "27363", "2026-09-04", "<p>New answer.</p>",
                                        "2026-09-14", corrected="2026-10-20")]
    fake_api["individual"] = {}

    _window("corrected")
    row = HaPQ.query.filter_by(api_id=1938330).one()
    assert row.answer_text == "<p>Old answer.</p>" and row.answer_corrected_date is None

    fake_api["individual_calls"] = []
    _window("corrected")
    assert fake_api["individual_calls"] == [1938330]


def test_unknown_date_field_is_rejected(db, fake_api):
    from hansard_archive.pq_ingestor import ingest_pq_date_range
    with pytest.raises(ValueError):
        ingest_pq_date_range(date(2026, 10, 1), date(2026, 10, 2), verbose=False,
                             date_field="withdrawn")


def test_pq_page_shows_holding_and_corrected_answers(db, app):
    db.session.add(_pq(api_id=1, uin="35741", is_answered=True, is_holding=True,
                       answer_date=date(2026, 10, 21), answer_text="<p>I will reply shortly.</p>"))
    db.session.add(_pq(api_id=2, uin="23043", is_answered=True, answer_date=date(2026, 9, 8),
                       answer_corrected_date=date(2026, 10, 20),
                       answer_text="<p>The corrected answer.</p>"))
    db.session.add(_pq(api_id=3, uin="36000", is_answered=True, answer_date=date(2026, 10, 20),
                       answer_text="<p>A plain answer.</p>"))
    db.session.commit()
    client = app.test_client()

    holding = client.get("/archive/pq/35741").get_data(as_text=True)
    assert "Holding answer" in holding and "will reply in full later" in holding
    assert "Corrected" not in holding

    corrected = client.get("/archive/pq/23043").get_data(as_text=True)
    assert "Corrected on 20 October 2026" in corrected
    assert "This answer was corrected on 20 October 2026" in corrected
    assert "Holding answer" not in corrected

    plain = client.get("/archive/pq/36000").get_data(as_text=True)
    assert "Holding answer" not in plain and "Corrected" not in plain
    assert "Answered" in plain
