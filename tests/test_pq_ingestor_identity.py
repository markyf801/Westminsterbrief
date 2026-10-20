"""
PQ identity (INC-008, Phase 3): ingestor matching, tripwire, tag invalidation,
tagger result mapping, and the PQ page title fallback.

SQLite in-memory only; the Parliament API and Gemini are mocked.

Note: the old model had UNIQUE on ha_pq.uin, which would make the "same UIN in
two sessions" test impossible. The model now declares uin non-unique; that
changes only freshly created databases (create_all never alters an existing
table). Production's unique index must be dropped by hand after this code is
deployed — docs/plans/pq-identity-phase3-sql.md, section 4.
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
    rows = [HaPQ(uin="6978", api_id=1, question_text="Fish?", tabled_date=date(2026, 6, 4)),
            HaPQ(uin="6978", api_id=2, question_text="Trains?", tabled_date=date(2025, 6, 2)),
            HaPQ(uin="7000", api_id=3, question_text="Schools?", tabled_date=date(2026, 6, 3))]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def _run_tagger(monkeypatch, batch_result, individual=None):
    import hansard_archive.tagger as tg
    calls = []
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    monkeypatch.setattr(tg, "_gemini_tag_batch", lambda key, prompt: batch_result)
    monkeypatch.setattr(tg, "tag_pq", lambda pq_id, api_key=None: calls.append(pq_id) or 1)
    res = tg.tag_pq_all_untagged(api_key="test-key", verbose=False)
    return res, calls


def _themes(pq_id):
    from hansard_archive.models import HaPQTheme
    return {t.theme for t in HaPQTheme.query.filter_by(pq_id=pq_id, theme_type="specific")}


def test_tagger_maps_results_by_position(db, monkeypatch):
    rows = _batch(db)   # tagger orders by tabled_date desc: 6978(2026), 7000, 6978(2025)
    ordered = sorted(rows, key=lambda r: r.tabled_date, reverse=True)
    result = [{"uin": r.uin, "policy_areas": [], "themes": [f"topic {r.api_id}"]} for r in ordered]

    res, retried = _run_tagger(monkeypatch, result)

    assert retried == [] and res["tagged"] == 3
    for r in rows:   # same UIN in two sessions still gets its own tags
        assert _themes(r.id) == {f"topic {r.api_id}"}


def test_tagger_retries_when_echoed_uin_disagrees(db, monkeypatch):
    rows = _batch(db)
    ordered = sorted(rows, key=lambda r: r.tabled_date, reverse=True)
    result = [{"uin": r.uin, "policy_areas": [], "themes": [f"topic {r.api_id}"]} for r in ordered]
    result[0], result[1] = result[1], result[0]   # model swapped two answers

    res, retried = _run_tagger(monkeypatch, result)

    assert sorted(retried) == sorted([ordered[0].id, ordered[1].id])
    assert _themes(ordered[2].id) == {f"topic {ordered[2].api_id}"}
    assert _themes(ordered[0].id) == set() and _themes(ordered[1].id) == set()


def test_tagger_retries_all_when_result_count_differs(db, monkeypatch):
    rows = _batch(db)
    res, retried = _run_tagger(monkeypatch, [{"uin": "7000", "policy_areas": [], "themes": ["x"]}])
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
