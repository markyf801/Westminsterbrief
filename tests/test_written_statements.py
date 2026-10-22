"""
Tests for written ministerial statements: parsing and policy areas, the ingest
script (HTTP mocked), DB-backed routes (/written-statements, detail, sitemap),
the Debate Prep switch to the DB, and the migration (SQLite).

Run: python -m pytest tests/test_written_statements.py -v
"""
import copy
import os
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import consultations  # noqa: E402
import ingest_written_statements as ing  # noqa: E402
import written_statements as wsm  # noqa: E402

NOW = datetime(2026, 10, 22, 12, 0)
TODAY = date(2026, 10, 22)


# ── Fake API payloads (detail shape) ──────────────────────────────────────────

def _stmt(sid, uin, made, house, body_id, body_name, member_id, member_name, title, text,
          role="Minister", party="Labour", linked=None, attachments=None, notice=0):
    return {
        "id": sid, "memberId": member_id,
        "member": {"id": member_id, "name": member_name, "party": party, "partyAbbreviation": "Lab"},
        "memberRole": role, "uin": uin, "dateMade": f"{made}T00:00:00",
        "answeringBodyId": body_id, "answeringBodyName": body_name, "title": title, "text": text,
        "house": house, "noticeNumber": notice,
        "hasAttachments": bool(attachments), "attachments": attachments or [],
        "hasLinkedStatements": bool(linked), "linkedStatements": linked,
    }


SCHOOLS_C = _stmt(
    101, "HCWS100", "2026-10-21", "Commons", 60, "Department for Education ", 4000, "Bridget Phillipson",
    "School funding update",
    "<p>Today I am announcing &amp; confirming new funding for schools and pupils.</p>"
    "<script>alert(1)</script><p onclick=\"x()\">Second paragraph.</p>"
    "<table><tr><th scope=\"col\">Region</th></tr><tr><td>North</td></tr></table>",
    role="Secretary of State for Education", notice=3,
    linked=[{"linkedStatementId": 102, "linkType": "JointStatement", "linkDate": "2026-10-21T00:00:00"}],
    attachments=[
        {"url": "https://qna.files.parliament.uk/ws-attachments/101/original/Funding report.pdf",
         "title": "Funding report", "fileType": "pdf", "fileSizeBytes": 159119},
        {"url": "javascript:alert(1)", "title": "Bad", "fileType": "pdf", "fileSizeBytes": 1},
    ],
)
SCHOOLS_L = _stmt(
    102, "HLWS50", "2026-10-21", "Lords", 60, "Department for Education", 347, "Baroness Smith of Malvern",
    "School funding update",
    "<p>My honourable friend has made the following statement:</p><p>Today I am announcing new "
    "funding for schools and pupils.</p>",
    linked=[{"linkedStatementId": 101, "linkType": "JointStatement", "linkDate": "2026-10-21T00:00:00"}],
)
RAIL = _stmt(103, "HCWS90", "2026-10-14", "Commons", 27, "Department for Transport", 4100, "Heidi Alexander",
             "Rail franchising", "<p>Rail services will return to public ownership.</p>")
AUDIT = _stmt(104, "HCWS1", "2024-07-30", "Commons", 14, "Treasury", 4200, "Rachel Reeves",
              "Public spending audit", "<p>The spending audit found pressures.</p>")
NEW_SESSION = _stmt(105, "HCWS1", "2026-05-20", "Commons", 1, "Home Office", 4300, "Yvette Cooper",
                    "Immigration rules", "<p>Changes to the asylum and visa rules.</p>")
ALL = (SCHOOLS_C, SCHOOLS_L, RAIL, AUDIT, NEW_SESSION)


def _list_shape(s):
    v = copy.deepcopy(s)
    v["member"] = None
    v["text"] = "Today I am announcing...".ljust(30)
    return {"value": v, "links": [{"rel": "self", "href": f"/Statements/{s['id'] + 9000}"}]}


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def _fake_api(statements):
    calls = {"list": [], "detail": []}

    def get(url, params=None, timeout=None):
        if url == ing.API_BASE:
            calls["list"].append(dict(params))
            rows = [s for s in statements
                    if s["dateMade"][:10] >= params["madeWhenFrom"]
                    and ("madeWhenTo" not in params or s["dateMade"][:10] <= params["madeWhenTo"])]
            rows.sort(key=lambda s: s["dateMade"], reverse=True)
            page = rows[params["skip"]:params["skip"] + params["take"]]
            return _resp({"totalResults": len(rows), "results": [_list_shape(s) for s in page]})
        sid = int(url.rsplit("/", 1)[1])
        calls["detail"].append(sid)
        assert params["expandMember"] == "true"
        found = next((s for s in statements if s["id"] == sid), None)
        return _resp({"value": copy.deepcopy(found), "links": []}) if found else _resp(None, 404)

    return get, calls


def _no_sleep(_seconds):
    return None


# ── Pure functions ────────────────────────────────────────────────────────────

class TestParse:
    def test_sanitises_and_keeps_structure(self):
        v = ing.parse(SCHOOLS_C)
        assert "<script" not in v["text_html"] and "onclick" not in v["text_html"]
        assert "<table>" in v["text_html"] and '<th scope="col">' in v["text_html"]
        assert v["text_plain"].startswith("Today I am announcing & confirming new funding")
        assert "\n\nSecond paragraph." in v["text_plain"] and "alert" in v["text_plain"]  # script text kept as text
        assert v["answering_body_name"] == "Department for Education"     # trailing space trimmed
        assert v["member_name"] == "Bridget Phillipson" and v["member_party"] == "Labour"
        assert v["date_made"] == date(2026, 10, 21) and v["notice_number"] == 3
        assert v["linked_statements"] == [{"id": 102, "link_type": "JointStatement", "link_date": "2026-10-21"}]
        assert v["attachments"] == [{
            "url": "https://qna.files.parliament.uk/ws-attachments/101/original/Funding%20report.pdf",
            "title": "Funding report", "file_type": "pdf", "file_size": 159119}]
        assert "text" not in v["raw_data"]

    def test_lords_notice_zero_and_missing_member(self):
        raw = {**SCHOOLS_L, "member": None}
        v = ing.parse(raw)
        assert v["notice_number"] is None and v["member_name"] is None and v["member_id"] == 347
        assert v["linked_statements"][0]["id"] == 101

    def test_policy_areas_from_department_and_keywords(self):
        assert wsm.policy_areas_for("School funding update", "funding for schools", 60)[0] == "education"
        assert wsm.policy_areas_for("Rail franchising", "Rail services", 27) == ["transport"]
        # No department mapping (Prime Minister): keywords alone must reach the threshold
        assert wsm.policy_areas_for("Engagements", "A visit abroad.", 23) == []
        assert "immigration-and-asylum" in wsm.policy_areas_for("Asylum rules", "asylum and visa", 23)

    def test_url_dates(self):
        assert wsm.url_date(date(2026, 10, 1)) == "1-october-2026"
        assert wsm.parse_url_date("1-october-2026") == date(2026, 10, 1)
        assert wsm.parse_url_date("31-february-2026") is None
        assert wsm.parse_url_date("2026-10-01") is None


# ── App / DB fixtures ─────────────────────────────────────────────────────────

def _seed_related(db_):
    from hansard_archive.models import HansardSession, HansardSessionTheme, THEME_TYPE_POLICY_AREA
    s = HansardSession(ext_id="s1", title="School Funding", date=date(2026, 10, 14), house="Commons",
                       slug="school-funding-1")
    db_.session.add(s)
    db_.session.flush()
    db_.session.add(HansardSessionTheme(session_id=s.id, theme="Education, training and skills",
                                        theme_type=THEME_TYPE_POLICY_AREA))


@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db
    from feature_flags import feature_enabled
    import cache_models  # noqa: F401  (ha_bill_sponsor FK target)
    import hansard_archive.models  # noqa: F401

    old_flag = os.environ.get(wsm.FLAG)
    os.environ[wsm.FLAG] = "live"

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    test_app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True, "SECRET_KEY": "test-secret", "LOGIN_DISABLED": True,
        "RATELIMIT_ENABLED": False,
    })
    _db.init_app(test_app)
    lm = LoginManager()
    lm.init_app(test_app)
    lm.user_loader(lambda uid: None)

    @test_app.context_processor
    def inject_globals():
        return {"admin_authenticated": False, "app_version": "test"}

    test_app.jinja_env.globals["feature_enabled"] = feature_enabled
    test_app.register_blueprint(wsm.written_statements_bp)

    with test_app.app_context():
        _db.create_all()
        for s in ALL:
            ing.upsert(ing.parse(s), NOW)
        _seed_related(_db)
        _db.session.commit()

    with patch.object(consultations, "_utcnow", return_value=NOW):
        yield test_app

    with test_app.app_context():
        _db.drop_all()
    if old_flag is None:
        os.environ.pop(wsm.FLAG, None)
    else:
        os.environ[wsm.FLAG] = old_flag


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


# ── Routes ────────────────────────────────────────────────────────────────────

class TestList:
    def test_latest_first(self, client):
        html = client.get("/written-statements").get_data(as_text=True)
        assert _INDEX in html and "<strong>5</strong> statements" in html
        assert html.index("School funding update") < html.index("Rail franchising") < \
            html.index("Immigration rules") < html.index("Public spending audit")
        assert 'href="/written-statements/21-october-2026/hcws100"' in html
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/written-statements">' in html
        assert '"@type": "CollectionPage"' in html
        assert "Open Parliament Licence v3.0" in html

    def test_filters(self, client):
        html = client.get("/written-statements?house=lords").get_data(as_text=True)
        assert "<strong>1</strong> statement" in html and "HLWS50" in html and _NOINDEX in html
        html = client.get("/written-statements?dept=27").get_data(as_text=True)
        assert "Rail franchising" in html and "School funding" not in html
        assert "Department for Education (2)" in html                  # options ignore the dept filter
        html = client.get("/written-statements?from=2026-10-01&to=2026-10-15").get_data(as_text=True)
        assert "Rail franchising" in html and "School funding" not in html and "Immigration" not in html
        html = client.get("/written-statements?from=not-a-date").get_data(as_text=True)
        assert "<strong>5</strong> statements" in html                  # bad date ignored

    def test_search(self, client):
        html = client.get("/written-statements?q=franchising").get_data(as_text=True)
        assert "Rail franchising" in html and "School funding" not in html and _NOINDEX in html
        html = client.get("/written-statements?q=zzzz").get_data(as_text=True)
        assert "No written statements match" in html

    def test_pagination_bounds(self, client):
        assert client.get("/written-statements?page=2").status_code == 404
        assert client.get("/written-statements?page=51").status_code == 404

    def test_hidden_flag_is_404(self, client, monkeypatch):
        monkeypatch.setenv(wsm.FLAG, "hidden")
        assert client.get("/written-statements").status_code == 404
        assert client.get("/written-statements/21-october-2026/hcws100").status_code == 404


class TestDetail:
    def test_detail_page(self, client):
        r = client.get("/written-statements/21-october-2026/hcws100")
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and _INDEX in html
        assert "School funding update" in html and "Bridget Phillipson" in html
        assert "Secretary of State for Education" in html and "Department for Education" in html
        assert "<script>alert" not in html and "onclick" not in html
        assert "<table>" in html and "Second paragraph." in html
        assert 'href="https://questions-statements.parliament.uk/written-statements/detail/2026-10-21/HCWS100"' in html
        assert 'href="https://members.parliament.uk/member/4000"' in html
        assert "Funding%20report.pdf" in html and "PDF · 155 KB" in html and "javascript:" not in html
        assert "Open Parliament Licence v3.0" in html
        assert '"@type": "Article"' in html and '"datePublished": "2026-10-21"' in html
        assert ('<link rel="canonical" href="https://westminsterbrief.co.uk'
                '/written-statements/21-october-2026/hcws100">') in html

    def test_linked_statement_in_other_house(self, client):
        html = client.get("/written-statements/21-october-2026/hcws100").get_data(as_text=True)
        assert 'Same statement made in the House of Lords: <a href="/written-statements/21-october-2026/hlws50">' in html
        assert 'href="/written-statements/21-october-2026/hlws50"' in html

    def test_related_by_policy_area(self, client):
        html = client.get("/written-statements/21-october-2026/hcws100").get_data(as_text=True)
        assert 'href="/brief/education"' in html
        assert 'href="/archive/debate/14-october-2026/school-funding-1"' in html

    def test_redirects_and_404(self, client):
        r = client.get("/written-statements/2026-10-21/HCWS100")
        assert r.status_code == 301 and r.headers["Location"].endswith("/written-statements/21-october-2026/hcws100")
        r = client.get("/written-statements/21-October-2026/HCWS100")
        assert r.status_code == 301
        assert client.get("/written-statements/22-october-2026/hcws100").status_code == 404
        assert client.get("/written-statements/nonsense/hcws100").status_code == 404

    def test_same_uin_different_sessions(self, client):
        assert "Public spending audit" in client.get("/written-statements/30-july-2024/hcws1").get_data(as_text=True)
        assert "Immigration rules" in client.get("/written-statements/20-may-2026/hcws1").get_data(as_text=True)


class TestSitemap:
    def test_sitemap(self, app):
        with app.app_context():
            xml = wsm.build_sitemap_xml()
        assert "https://westminsterbrief.co.uk/written-statements/21-october-2026/hcws100" in xml
        assert "/written-statements/30-july-2024/hcws1</loc>" in xml
        assert xml.count("<url>") == 5 and "<lastmod>2026-10-22</lastmod>" in xml


# ── Debate Prep ───────────────────────────────────────────────────────────────

class TestDebatePrep:
    def test_reads_from_db(self, app):
        import debate_scanner as ds
        with app.test_request_context("/debate_prep"):
            with patch.object(ds, "fetch_parliament_wms") as live:
                rows = ds._prep_parl_statements("school funding for pupils", "20260101..20261022")
            live.assert_not_called()
        titles = [r["debate_title"] for r in rows]
        assert titles == ["School funding update", "School funding update"]
        assert rows[0]["listurl"] in ("/written-statements/21-october-2026/hlws50",
                                      "/written-statements/21-october-2026/hcws100")
        assert rows[0]["source"] == "wms" and rows[0]["is_minister"] is True
        assert "<p>" not in rows[0]["body_clean"]

    def test_date_range_and_relevance(self, app):
        import debate_scanner as ds
        with app.test_request_context("/debate_prep"):
            assert ds.fetch_db_wms("school funding", "20261101..20261130") == []
            assert [r["debate_title"] for r in ds.fetch_db_wms("rail franchising", "")] == ["Rail franchising"]

    def test_links_to_parliament_when_flag_hidden(self, app, monkeypatch):
        import debate_scanner as ds
        monkeypatch.setenv(wsm.FLAG, "hidden")
        with app.test_request_context("/debate_prep"):
            rows = ds.fetch_db_wms("rail franchising", "")
        assert rows[0]["listurl"].startswith("https://questions-statements.parliament.uk/")

    def test_falls_back_to_live_when_table_empty(self, ingest_app):
        import debate_scanner as ds
        with ingest_app.test_request_context("/debate_prep"):
            with patch.object(ds, "fetch_parliament_wms", return_value=[{"debate_title": "Live"}]) as live:
                rows = ds._prep_parl_statements("school funding", "20260101..20261022")
        live.assert_called_once()
        assert rows == [{"debate_title": "Live"}]


# ── Ingest ────────────────────────────────────────────────────────────────────

@pytest.fixture
def ingest_app():
    """Fresh empty DB per ingest test."""
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db
    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401

    a = Flask(__name__)
    a.config.update({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                     "SQLALCHEMY_TRACK_MODIFICATIONS": False, "SECRET_KEY": "x"})
    _db.init_app(a)
    LoginManager().init_app(a)
    with a.app_context():
        _db.create_all()
        yield a
        _db.session.remove()
        _db.drop_all()


class TestIngest:
    def _run(self, statements, start=date(2024, 7, 4), **kw):
        get, calls = _fake_api(statements)
        with patch.object(ing.requests, "get", side_effect=get):
            summary = ing.ingest(start, api=ing.Api(sleep=_no_sleep), today=TODAY, **kw)
        return summary, calls

    def test_backfill_then_daily_rerun(self, ingest_app):
        s, calls = self._run(ALL)
        assert s["listed"] == 5 and s["inserted"] == 5 and s["failed"] == 0 and s["list_complete"]
        assert calls["list"][0]["sessionStatus"] == "Any" and calls["list"][0]["take"] == 100
        assert ing.WrittenStatement.query.count() == 5
        row = ing.db.session.get(ing.WrittenStatement, 101)
        assert row.policy_areas[0] == "education" and row.text_plain.startswith("Today I am announcing")

        s, calls = self._run(ALL)
        # made in the last 2 days -> refreshed; the rest unchanged -> skipped
        assert s["planned_new"] == 0 and s["planned_recent"] == 2 and s["planned_skip"] == 3
        assert sorted(calls["detail"]) == [101, 102]
        assert s["inserted"] == 0 and s["updated"] == 0 and s["unchanged"] == 2

    def test_daily_window_lists_only_recent(self, ingest_app):
        s, calls = self._run(ALL, start=date(2026, 10, 15))
        assert s["listed"] == 2 and calls["list"][0]["madeWhenFrom"] == "2026-10-15"

    def test_changed_title_is_refetched_and_bumps_updated_at(self, ingest_app):
        first = datetime(2026, 10, 1, 9, 0)
        ing.upsert(ing.parse(RAIL), first)
        ing.db.session.commit()
        row, created, changed = ing.upsert(ing.parse(RAIL), datetime(2026, 10, 2))
        ing.db.session.commit()
        assert not created and not changed and row.updated_at == first

        renamed = {**RAIL, "title": "Rail franchising: correction"}
        s, calls = self._run([renamed])
        assert s["planned_changed"] == 1 and calls["detail"] == [103] and s["updated"] == 1
        row = ing.db.session.get(ing.WrittenStatement, 103)
        assert row.title == "Rail franchising: correction" and row.updated_at > first

    def test_new_linked_correction_triggers_refetch(self, ingest_app):
        self._run([RAIL])
        corrected = {**RAIL, "linkedStatements": [{"linkedStatementId": 999, "linkType": "CorrectedStatement",
                                                   "linkDate": "2026-10-20T00:00:00"}]}
        s, _ = self._run([corrected])
        assert s["planned_changed"] == 1
        assert ing.db.session.get(ing.WrittenStatement, 103).linked_statements[0]["link_type"] == "CorrectedStatement"

    def test_dry_run_writes_nothing(self, ingest_app):
        s, calls = self._run(ALL, dry_run=True)
        assert s["planned_new"] == 5 and s["inserted"] == 0
        assert calls["detail"] == [] and ing.WrittenStatement.query.count() == 0

    def test_limit_caps_detail_fetches(self, ingest_app):
        s, calls = self._run(ALL, limit=2)
        assert len(calls["detail"]) == 2 and s["inserted"] == 2

    def test_paging(self, ingest_app, monkeypatch):
        monkeypatch.setattr(ing, "LIST_PAGE_SIZE", 2)
        s, calls = self._run(ALL)
        assert s["listed"] == 5 and len(calls["list"]) == 3 and s["inserted"] == 5

    def test_retry_then_success(self):
        api = ing.Api(sleep=_no_sleep)
        responses = [requests.ConnectionError("boom"), _resp(None, 503), _resp({"ok": True})]
        with patch.object(ing.requests, "get", side_effect=responses):
            body = api.get(ing.API_BASE)
        assert body == {"ok": True} and api.ok == 1 and api.failed == 0

    def test_429_stops_the_run(self, ingest_app):
        get, calls = _fake_api(ALL)

        def throttled(url, params=None, timeout=None):
            if url != ing.API_BASE and calls["detail"]:
                return _resp(None, 429)
            return get(url, params, timeout)

        with patch.object(ing.requests, "get", side_effect=throttled):
            s = ing.ingest(date(2024, 7, 4), api=ing.Api(sleep=_no_sleep), today=TODAY)
        assert s["rate_limited"] and s["inserted"] == 1 and len(calls["detail"]) == 1
        assert ing.mostly_failed(s)

    def test_permanent_4xx_not_retried(self):
        api = ing.Api(sleep=_no_sleep)
        with patch.object(ing.requests, "get", return_value=_resp(None, 404)) as g:
            assert api.get(ing.API_BASE) is None
        assert g.call_count == 1 and api.failed == 1

    def test_all_requests_failing_is_reported(self, ingest_app):
        with patch.object(ing.requests, "get", return_value=_resp(None, 503)):
            s = ing.ingest(date(2026, 10, 15), api=ing.Api(sleep=_no_sleep), today=TODAY)
        assert s["listed"] == 0 and not s["list_complete"]
        assert ing.mostly_failed(s)


class TestMigration:
    def test_upgrade_is_idempotent_on_sqlite(self):
        from sqlalchemy import create_engine, inspect, text
        sys.path.insert(0, str(PROJECT_ROOT))
        from migrations.written_statement_tables import steps, upgrade

        engine = create_engine("sqlite:///:memory:")
        upgrade(engine)
        upgrade(engine)
        insp = inspect(engine)
        assert "ha_written_statement" in insp.get_table_names()
        model_cols = {c.name for c in wsm.WrittenStatement.__table__.columns}
        assert {c["name"] for c in insp.get_columns("ha_written_statement")} == model_cols
        model_idx = {i.name for i in wsm.WrittenStatement.__table__.indexes}
        assert {i["name"] for i in insp.get_indexes("ha_written_statement")} == model_idx
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO ha_written_statement (id, uin, date_made, house, title) "
                              "VALUES (1, 'HCWS1', '2026-10-21', 'Commons', 'T')"))
        pg = [label for label, _ in steps("postgresql")]
        assert "GIN index on ha_written_statement.search_tsv" in pg
        assert len(steps("sqlite")) == len(pg) - 2
