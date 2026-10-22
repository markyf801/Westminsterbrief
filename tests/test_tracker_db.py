"""
Today's PQs (/written-questions/today, tracker.py) served from our own database.

- questions come from ha_pq (most recent day, from yesterday back, with
  unanswered questions for the department), due dates and named-day badges
  from ha_pq_deadline, names from cached_member
- "Updated from Parliament at HH:MM" from the newest successful PQ cron run
- the Word export's 3-month history is one ha_pq query
- no outbound HTTP anywhere (requests blocked at the Session level)
- the page still works before migrations/pq_deadline_table.py has run

Run: python -m pytest tests/test_tracker_db.py -v
"""

import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).parents[1]

TODAY = date.today()
D1, D2, D3 = (TODAY - timedelta(days=n) for n in (1, 2, 3))


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly on any real HTTP request; record attempted URLs."""
    attempted = []

    def blocked(self, method, url, *args, **kwargs):
        attempted.append(url)
        raise requests.ConnectionError(f"network blocked in tests: {url}")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    return attempted


@pytest.fixture
def app(monkeypatch, no_network):
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db

    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    import cache_models  # noqa: F401
    import tracker
    from feature_flags import feature_enabled

    monkeypatch.setattr(tracker, "api_key", None)   # Gemini off unless a test turns it on
    tracker._AI_CATEGORY_CACHE.clear()

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"),
                     static_folder=str(PROJECT_ROOT / "static"))
    test_app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", TESTING=True,
                           SECRET_KEY="test", LOGIN_DISABLED=True, RATELIMIT_ENABLED=False)
    db.init_app(test_app)
    lm = LoginManager()
    lm.init_app(test_app)
    lm.user_loader(lambda uid: None)
    test_app.context_processor(lambda: {"admin_authenticated": False, "app_version": "test"})
    test_app.jinja_env.globals["feature_enabled"] = feature_enabled
    test_app.register_blueprint(tracker.tracker_bp)
    # The canonical URL lives in flask_app.py and just calls morning_tracker().
    test_app.add_url_rule("/written-questions/today", "written_questions_today",
                          tracker.morning_tracker, methods=["GET", "POST"])
    with test_app.app_context():
        db.create_all()
        _seed(db)
        yield test_app
        db.session.remove()
        db.drop_all()
    tracker._AI_CATEGORY_CACHE.clear()


def _pq(uin, tabled, body_id=60, body="Department for Education", **kw):
    from hansard_archive.models import HaPQ
    base = dict(uin=uin, question_text=f"Question {uin} about school funding.",
                tabled_date=tabled, answering_body=body, answering_body_id=body_id,
                chamber="Commons", is_answered=False, is_withdrawn=False)
    base.update(kw)
    return HaPQ(**base)


CRON_FINISHED = datetime(2026, 10, 21, 8, 42)   # 09:42 BST


def _seed(db):
    from cache_models import CachedMember
    from hansard_archive.models import HaCronRun, HaPQDeadline
    db.session.add_all([
        _pq("901001", D2, api_id=1001, asking_mnis_id=4046, asking_member="B Phillipson (stored)"),
        _pq("901002", D2, api_id=1002, asking_mnis_id=999, asking_member="Fallback Name"),
        _pq("901003", D2, api_id=1003, is_answered=True, answer_text="<p>Answered.</p>"),
        _pq("901004", D2, api_id=1004, is_withdrawn=True),
        _pq("HL100", D3, api_id=1005, chamber="Lords"),
        _pq("901006", D1, api_id=1006, body_id=14, body="HM Treasury"),
        _pq("901007", TODAY, api_id=1007),
        HaPQDeadline(api_id=1001, date_for_answer=TODAY + timedelta(days=3), is_named_day=True),
        CachedMember(member_id=4046, name="Bridget Phillipson", party="Labour",
                     constituency="Houghton and Sunderland South", house="Commons"),
        HaCronRun(service_name="pq-cron-early", started_at=CRON_FINISHED - timedelta(minutes=5),
                  finished_at=CRON_FINISHED, status="ok"),
        HaCronRun(service_name="pq-cron-morning", started_at=CRON_FINISHED + timedelta(hours=1),
                  finished_at=CRON_FINISHED + timedelta(hours=1, minutes=5), status="failed"),
        HaCronRun(service_name="archive-cron", started_at=CRON_FINISHED + timedelta(hours=2),
                  finished_at=CRON_FINISHED + timedelta(hours=2, minutes=5), status="ok"),
    ])
    db.session.commit()


def _post(app, dept):
    return app.test_client().post("/written-questions/today", data={"department": dept})


# ── page ──────────────────────────────────────────────────────────────────────

def test_department_shows_latest_day_unanswered_from_db(app, no_network):
    resp = _post(app, "60")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'data-uin="901001"' in html and 'data-uin="901002"' in html
    for excluded in ("901003", "901004", "HL100", "901006", "901007"):
        assert f'data-uin="{excluded}"' not in html   # answered/withdrawn/older/other dept/today
    assert f"<strong>{D2.day} {D2:%B %Y}</strong>" in html
    assert no_network == []


def test_names_party_and_deadline(app):
    html = _post(app, "60").get_data(as_text=True)
    assert "<strong>Bridget Phillipson</strong>" in html               # cached_member wins
    assert "Labour, Houghton and Sunderland South" in html
    assert "<strong>Fallback Name</strong>" in html                    # ha_pq.asking_member
    assert "wq-type-badge wq-type-named_day" in html
    assert f"<strong>Due:</strong> {(TODAY + timedelta(days=3)).isoformat()}" in html
    assert "wq-type-badge wq-type-ordinary" in html                   # 901002: no deadline row


def test_all_departments_uses_most_recent_day(app, no_network):
    html = _post(app, "").get_data(as_text=True)
    assert 'data-uin="901006"' in html
    assert 'data-uin="901001"' not in html
    assert no_network == []


def test_unknown_department_value_is_treated_as_all(app):
    resp = _post(app, "not-a-number")
    assert resp.status_code == 200
    assert 'data-uin="901006"' in resp.get_data(as_text=True)


def test_empty_window_shows_no_questions_message(app):
    from extensions import db
    from hansard_archive.models import HaPQ
    with app.app_context():
        HaPQ.query.filter(HaPQ.answering_body_id == 60).delete()
        db.session.commit()
    html = _post(app, "60").get_data(as_text=True)
    assert "No unanswered questions found." in html
    assert "Parliament API" not in html


def test_freshness_line_uses_last_ok_pq_cron(app, no_network):
    from tracker import _pq_updated_label
    with app.app_context():
        label = _pq_updated_label()
    assert label is not None and label.startswith("09:42")
    html = app.test_client().get("/written-questions/today").get_data(as_text=True)
    assert f"Updated from Parliament at {label}." in html
    assert no_network == []


def test_freshness_label_wording(app):
    from tracker import _pq_updated_label
    with app.app_context():
        assert _pq_updated_label(now_utc=datetime(2026, 10, 21, 12, 0)) == "09:42 today"
        assert _pq_updated_label(now_utc=datetime(2026, 10, 22, 7, 0)) == "09:42 on 21 October"


def test_freshness_line_hidden_without_pq_runs(app):
    from extensions import db
    from hansard_archive.models import HaCronRun
    with app.app_context():
        HaCronRun.query.filter(HaCronRun.service_name.like("pq-%")).delete(synchronize_session=False)
        db.session.commit()
    html = app.test_client().get("/written-questions/today").get_data(as_text=True)
    assert "Updated from Parliament" not in html


def test_page_works_before_deadline_migration(app, no_network):
    from extensions import db
    from hansard_archive.models import HaPQDeadline
    with app.app_context():
        HaPQDeadline.__table__.drop(db.engine)
    resp = _post(app, "60")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'data-uin="901001"' in html
    assert "wq-type-badge wq-type-named_day" not in html and "<strong>Due:</strong>" not in html
    assert no_network == []


def test_ai_categories_still_grouped(app, monkeypatch, no_network):
    import tracker
    monkeypatch.setattr(tracker, "api_key", "test-key")
    calls = []

    def fake_gemini(key, prompt):
        calls.append(prompt)
        return '{"901001": "School Funding", "901002": "SEND"}'

    monkeypatch.setattr(tracker, "_gemini_generate", fake_gemini)
    html = _post(app, "60").get_data(as_text=True)
    _post(app, "60")
    assert 'data-theme="School Funding"' in html and 'data-theme="SEND"' in html
    assert len(calls) == 1          # cached per department + sitting day
    assert no_network == []


# ── Word export history ──────────────────────────────────────────────────────

def _docx_text(data: bytes) -> str:
    from docx import Document
    return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)


def test_word_history_from_one_db_query(app, no_network):
    from sqlalchemy import event
    from extensions import db
    with app.app_context():
        db.session.add_all([
            _pq("800001", TODAY - timedelta(days=30), asking_mnis_id=4046,
                question_text="Earlier question on school funding formulas."),
            _pq("800002", TODAY - timedelta(days=200), asking_mnis_id=4046,
                question_text="Very old question on school funding."),
            _pq("800003", TODAY - timedelta(days=20), asking_mnis_id=4046, body_id=14,
                body="HM Treasury", question_text="Treasury question on school funding."),
            _pq("800004", TODAY - timedelta(days=10), asking_mnis_id=999,
                question_text="Another member's funding question."),
        ])
        db.session.commit()
        engine = db.engine

    export = [{"date": f"{D2.day} {D2:%B %Y}", "theme": "School Funding", "questions": [
        {"member": "Bridget Phillipson", "member_id": "4046", "uin": "901001",
         "text": "Question 901001 about school funding.", "due_date": D2.isoformat(),
         "status": "UNANSWERED"},
        {"member": "Fallback Name", "member_id": "999", "uin": "901002",
         "text": "Question 901002 about school funding.", "due_date": D2.isoformat(),
         "status": "UNANSWERED"},
    ]}]
    pq_selects = []

    def count(conn, cursor, statement, params, context, executemany):
        if "FROM ha_pq" in statement:
            pq_selects.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        resp = app.test_client().post("/download_tracker_word", data={
            "export_data": json.dumps(export), "include_history": "true", "selected_dept": "60"})
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert resp.status_code == 200
    text = _docx_text(resp.data)
    assert "Earlier question on school funding formulas." in text
    assert "Very old question" not in text          # older than 3 months
    assert "Treasury question" not in text          # other department
    assert "Another member's funding question." in text
    assert text.count('"Question 901001 about school funding."') == 1   # not its own history
    assert len(pq_selects) == 1
    assert no_network == []


def test_word_export_without_history_makes_no_query(app, no_network):
    export = [{"date": "x", "theme": "T", "questions": [
        {"member": "M", "member_id": "4046", "uin": "1", "text": "t", "due_date": D2.isoformat()}]}]
    resp = app.test_client().post("/download_tracker_word",
                                  data={"export_data": json.dumps(export)})
    assert resp.status_code == 200
    assert no_network == []


# ── migration ────────────────────────────────────────────────────────────────

def test_migration_is_idempotent_on_sqlite():
    import importlib.util
    from sqlalchemy import create_engine, inspect
    spec = importlib.util.spec_from_file_location(
        "pq_deadline_table", PROJECT_ROOT / "migrations" / "pq_deadline_table.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    engine = create_engine("sqlite:///:memory:")
    mod.upgrade(engine)
    mod.upgrade(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("ha_pq_deadline")}
    assert cols == {"api_id", "date_for_answer", "is_named_day", "updated_at"}
