"""
Ministers stored in our own DB: scripts/refresh_ministers.py (HTTP mocked),
the readers in ministers.py, the Research Tool callers in debate_scanner.py
and the migration (SQLite).

Run: python -m pytest tests/test_ministers.py -v
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import refresh_ministers as rm  # noqa: E402
import debate_scanner  # noqa: E402
import ministers  # noqa: E402


# ── Fake API payloads ─────────────────────────────────────────────────────────

def _holder(mnis_id, name, start="2025-09-06T00:00:00", end=None, lords=False):
    return {"member": {"value": {"id": mnis_id, "nameDisplayAs": name,
                                 "latestHouseMembership": {"house": 2 if lords else 1}}},
            "startDate": start, "endDate": end}


def _post(post_id, name, order, holders, depts):
    return {"value": {"id": post_id, "name": name, "order": order, "postHolders": holders,
                      "governmentDepartments": [{"name": d} for d in depts]}}


DEPARTMENTS = [
    {"value": {"id": 99, "name": "Department for Education"}},
    {"value": {"id": 50, "name": "HM Treasury"}},
    {"value": {"id": 18, "name": "Department for Environment, Food and Rural Affairs"}},
    {"value": {"id": 48, "name": "HM Household"}},
]

SOS_EDUCATION = _post(829, "Secretary of State for Education", 1,
                      [_holder(4263, "Lucy Powell", "2026-07-20T00:00:00")], ["Department for Education"])
CHANCELLOR = _post(34, "Chancellor of the Exchequer", 1,
                   [_holder(400, "John Healey", "2026-07-20T00:00:00")], ["HM Treasury"])

POSTS = {
    99: [
        SOS_EDUCATION,
        _post(947, "Minister of State (Education)", 2,
              [_holder(5305, "Georgia Gould"),
               _holder(269, "Baroness Smith of Malvern", "2024-07-06T00:00:00", lords=True),
               _holder(1, "Former Minister", "2024-07-06T00:00:00", end="2025-09-06T00:00:00")],
              ["Department for Education"]),
        _post(949, "Parliamentary Under-Secretary (Department for Education)", 3,
              [_holder(5033, "Josh MacAlister", "2025-09-07T00:00:00")], ["Department for Education"]),
    ],
    50: [
        CHANCELLOR,
        _post(1400, "Exchequer Secretary (Cabinet Office) (Jointly with HM Treasury)", 5,
              [_holder(5282, "Dan Tomlinson", "2026-07-22T00:00:00")], ["Cabinet Office", "HM Treasury"]),
    ],
    18: [
        _post(704, "Secretary of State for Environment, Food and Rural Affairs", 1,
              [_holder(1234, "Dame Angela Eagle", "2026-07-20T00:00:00")],
              ["Department for Environment, Food and Rural Affairs"]),
    ],
    48: [
        _post(1500, "Baroness in Waiting (HM Household) (Whip)", 8,
              [_holder(4912, "Baroness Blake of Leeds", "2024-07-20T00:00:00", lords=True)], ["HM Household"]),
    ],
}
CABINET_POSTS = [SOS_EDUCATION, CHANCELLOR]


def _gperson(title, slug, roles):
    return {"title": title, "base_path": f"/government/people/{slug}",
            "links": {"role_appointments": [
                {"title": f"{title} - {role}", "details": {"current": current, "started_on": started}}
                for role, current, started in roles]}}


def _gdept(title, people, roles):
    return {"title": title, "links": {"ordered_ministers": people,
                                      "ordered_roles": [{"title": r} for r in roles]}}


GOVUK = {"links": {
    "ordered_cabinet_ministers": [{"title": "The Rt Hon Lucy Powell MP"}, {"title": "The Rt Hon John Healey MP"}],
    "ordered_also_attends_cabinet": [],
    "ordered_ministerial_departments": [
        _gdept("Department for Education", [
            _gperson("The Rt Hon Lucy Powell MP", "lucy-powell", [
                ("Secretary of State for Education", True, "2026-07-20T00:00:00+01:00"),
                ("Leader of the House of Commons", False, "2024-07-05T00:00:00+01:00")]),
            _gperson("Georgia Gould OBE MP", "georgia-gould", [
                ("Minister of State (Minister for School Standards)", True, "2025-09-06T00:00:00+01:00")]),
            _gperson("The Rt Hon Baroness Smith of Malvern", "jacqui-smith", [
                ("Minister of State (Minister for Skills)", True, "2026-07-22T00:00:00+01:00")]),
            _gperson("Josh MacAlister OBE MP", "josh-macalister", [
                ("Parliamentary Under-Secretary of State (Minister for Children and Families)", True,
                 "2025-09-07T00:00:00+01:00")]),
        ], ["Secretary of State for Education", "Minister of State (Minister for School Standards)",
            "Minister of State (Minister for Skills)",
            "Parliamentary Under-Secretary of State (Minister for Children and Families)"]),
        _gdept("HM Treasury", [
            _gperson("The Rt Hon John Healey MP", "john-healey", [
                ("Chancellor of the Exchequer", True, "2026-07-20T00:00:00+01:00")]),
            _gperson("Daniel Tomlinson MP", "daniel-tomlinson", [
                ("Exchequer Secretary to the Treasury (Minister for Growth)", True, "2026-07-22T00:00:00+01:00"),
                ("Parliamentary Secretary", True, "2026-07-22T00:00:00+01:00")]),
            _gperson("The Rt Hon James Murray MP", "james-murray", [
                ("Financial Secretary to the Treasury", True, "2026-07-21T00:00:00+01:00")]),
        ], ["Chancellor of the Exchequer", "Exchequer Secretary to the Treasury (Minister for Growth)",
            "Financial Secretary to the Treasury"]),
        _gdept("Department for Environment, Food & Rural Affairs", [
            _gperson("The Rt Hon Dame Angela Eagle DBE MP", "angela-eagle", [
                ("Secretary of State for Environment, Food and Rural Affairs", True, "2026-07-20T00:00:00+01:00")]),
        ], ["Secretary of State for Environment, Food and Rural Affairs"]),
    ],
}}


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class FakeSession:
    """requests.Session stand-in serving the Members API and GOV.UK payloads."""

    def __init__(self, posts=None, govuk=None, fail=None):
        self.headers = {}
        self.calls = []
        self.posts = POSTS if posts is None else posts
        self.govuk = GOVUK if govuk is None else govuk
        self.fail = fail or {}   # url substring -> list of status codes to return first

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for marker, statuses in self.fail.items():
            if marker in url and statuses:
                return _resp(None, statuses.pop(0))
        if url.endswith("/Posts/Departments/Government"):
            return _resp(DEPARTMENTS)
        if url.endswith("/Posts/GovernmentPosts"):
            if params and "departmentId" in params:
                return _resp(self.posts.get(params["departmentId"], []))
            return _resp(CABINET_POSTS)
        if url == rm.GOVUK_MINISTERS_API:
            return _resp(self.govuk)
        raise AssertionError(f"unexpected URL {url}")


def _api(**kw):
    return rm.Api(session=FakeSession(**kw), delay=0, sleep=lambda _s: None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app(monkeypatch):
    from flask import Flask
    from extensions import db
    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401

    # The fixture government is small; the real thresholds are ~60 people / 15 departments.
    monkeypatch.setattr(rm, "MIN_PEOPLE", 5)
    monkeypatch.setattr(rm, "MIN_DEPARTMENTS", 3)
    a = Flask(__name__)
    a.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", SQLALCHEMY_TRACK_MODIFICATIONS=False,
                    TESTING=True)
    db.init_app(a)
    ministers.clear_cache()
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()
    ministers.clear_cache()


@pytest.fixture
def no_http(monkeypatch):
    attempted = []

    def blocked(self, method, url, *a, **kw):
        attempted.append(url)
        raise AssertionError(f"unexpected HTTP call: {method} {url}")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    return attempted


def _stored():
    from cache_models import GovMinister
    return {(r.department, r.member_name): r for r in GovMinister.query.all()}


def _parsed():
    depts = [(d["value"]["id"], d["value"]["name"]) for d in DEPARTMENTS]
    parl = rm.parse_parliament(depts, CABINET_POSTS, POSTS)
    govuk_depts, cabinet = rm.parse_govuk(GOVUK)
    return rm.merge(parl, govuk_depts, cabinet, [n for _i, n in depts])


# ── Parsing and merging ───────────────────────────────────────────────────────

class TestMerge:
    def test_rows_and_sources(self):
        rows = {(r["department"], r["member_name"]): r for r in _parsed()}
        gould = rows[("Department for Education", "Georgia Gould")]
        assert gould["role_title"] == "Minister of State (Education)"
        assert gould["govuk_role_title"] == "Minister of State (Minister for School Standards)"
        assert gould["mnis_id"] == 5305 and gould["source"] == "parliament+govuk"
        assert gould["source_url"] == "https://www.gov.uk/government/people/georgia-gould"
        assert gould["start_date"] == date(2025, 9, 6)
        smith = rows[("Department for Education", "Baroness Smith of Malvern")]
        assert smith["house"] == "Lords" and smith["mnis_id"] == 269
        assert ("Department for Education", "Former Minister") not in rows

    def test_govuk_role_for_this_department_only(self):
        rows = {(r["department"], r["member_name"]): r for r in _parsed()}
        assert rows[("Department for Education", "Lucy Powell")]["govuk_role_title"] == \
            "Secretary of State for Education"

    def test_dan_and_daniel_matched_within_department(self):
        rows = {(r["department"], r["member_name"]): r for r in _parsed()}
        tomlinson = rows[("HM Treasury", "Dan Tomlinson")]
        assert tomlinson["source"] == "parliament+govuk"
        assert tomlinson["govuk_name"] == "Daniel Tomlinson MP"
        assert ("HM Treasury", "Daniel Tomlinson") not in rows

    def test_govuk_only_minister_kept_without_id(self):
        rows = {(r["department"], r["member_name"]): r for r in _parsed()}
        murray = rows[("HM Treasury", "James Murray")]
        assert murray["source"] == "govuk" and murray["mnis_id"] is None
        assert murray["role_title"] == "Financial Secretary to the Treasury"
        assert murray["rank"] >= 900

    def test_ampersand_department_maps_to_parliament_name(self):
        rows = _parsed()
        eagle = [r for r in rows if r["member_name"] == "Angela Eagle"]
        assert len(eagle) == 1
        assert eagle[0]["department"] == "Department for Environment, Food and Rural Affairs"
        assert eagle[0]["govuk_department"] == "Department for Environment, Food & Rural Affairs"
        assert eagle[0]["source"] == "parliament+govuk"

    def test_whips_kept_from_parliament_and_cabinet_marked(self):
        rows = {(r["department"], r["member_name"]): r for r in _parsed()}
        assert rows[("HM Household", "Baroness Blake of Leeds")]["source"] == "parliament"
        assert rows[("Department for Education", "Lucy Powell")]["is_cabinet"] is True
        assert rows[("Department for Education", "Georgia Gould")]["is_cabinet"] is False

    def test_post_nominals_stripped_for_matching(self):
        assert ministers.name_key("The Rt Hon Lucy Rigby KC MP") == "lucy rigby"
        assert ministers.name_key("Lord Lemos CMG") == "lemos"
        assert ministers.name_key("The Rt Hon Dame Angela Eagle DBE MP") == "angela eagle"


# ── Refresh run ───────────────────────────────────────────────────────────────

class TestRefresh:
    def test_first_run_writes_rows_and_cron_record(self, app):
        from cache_models import MemberProfile
        from extensions import db
        from hansard_archive.models import HaCronRun
        db.session.add(MemberProfile(member_id=4031, slug="james-murray", name="James Murray",
                                     house="Commons", is_current=True))
        db.session.commit()

        assert rm.run(_api()) == 0

        rows = _stored()
        assert rows[("HM Treasury", "James Murray")].mnis_id == 4031  # from our own member table
        assert rows[("Department for Education", "Georgia Gould")].fetched_at is not None
        run = HaCronRun.query.one()
        assert run.service_name == "minister-refresh" and run.status == "ok"
        assert "inserted=" in run.notes

    def test_second_run_updates_and_deletes_by_id(self, app):
        rm.run(_api())
        before = _stored()
        gould_id = before[("Department for Education", "Georgia Gould")].id
        posts = dict(POSTS)
        posts[99] = [POSTS[99][0], _post(947, "Minister of State (Education)", 2,
                                         [_holder(5305, "Georgia Gould")], ["Department for Education"]),
                     POSTS[99][2]]

        govuk_depts = [dict(d) for d in GOVUK["links"]["ordered_ministerial_departments"]]
        dfe = govuk_depts[0]
        dfe["links"] = dict(dfe["links"], ordered_ministers=[
            p for p in dfe["links"]["ordered_ministers"] if "Malvern" not in p["title"]])
        govuk = {"links": dict(GOVUK["links"], ordered_ministerial_departments=govuk_depts)}

        summary = rm.refresh(_api(posts=posts, govuk=govuk))

        after = _stored()
        assert ("Department for Education", "Baroness Smith of Malvern") not in after
        assert after[("Department for Education", "Georgia Gould")].id == gould_id
        assert summary["deleted"] == 1 and summary["inserted"] == 0
        assert "Baroness Smith of Malvern" in summary["people_removed"]

    def test_implausibly_few_people_keeps_previous_list(self, app, monkeypatch):
        from hansard_archive.models import HaCronRun
        rm.run(_api())
        count = len(_stored())
        thin = {99: [SOS_EDUCATION], 50: [], 18: [], 48: []}

        assert rm.run(_api(posts=thin, govuk={"links": {}})) == 1

        assert len(_stored()) == count
        failed = HaCronRun.query.filter_by(status="failed").one()
        assert "NOT WRITTEN" in failed.notes

    def test_shrinking_list_keeps_previous_list(self, app):
        rm.run(_api())
        posts = dict(POSTS)
        posts[99] = [SOS_EDUCATION]
        summary = rm.refresh(_api(posts=posts, govuk={"links": {
            "ordered_ministerial_departments": GOVUK["links"]["ordered_ministerial_departments"][1:]}}))
        assert not summary["ok"]
        assert any("under 70%" in p for p in summary["problems"])
        assert ("Department for Education", "Georgia Gould") in _stored()

    def test_server_error_after_retry_keeps_previous_list(self, app):
        rm.run(_api())
        count = len(_stored())
        api = _api(fail={"gov.uk": [500, 503]})

        assert rm.run(api) == 1
        assert len(_stored()) == count

    def test_retry_once_then_success(self, app):
        api = _api(fail={"gov.uk": [503]})
        assert rm.run(api) == 0
        assert sum(1 for url, _p in api.session.calls if "gov.uk" in url) == 2

    def test_429_stops_without_retry(self, app):
        api = _api(fail={"Departments/Government": [429]})
        assert rm.run(api) == 1
        assert len(api.session.calls) == 1
        assert _stored() == {}

    def test_dry_run_writes_nothing(self, app):
        from hansard_archive.models import HaCronRun
        summary = rm.refresh(_api(), dry_run=True)
        assert summary["ok"] and summary["inserted"] == summary["rows"]
        assert _stored() == {}
        assert HaCronRun.query.count() == 0


# ── Readers ───────────────────────────────────────────────────────────────────

@pytest.fixture
def stored(app):
    assert rm.run(_api()) == 0
    ministers.clear_cache()
    return app


class TestReaders:
    def test_load_minister_list_shape(self, stored, no_http):
        data = ministers.load_minister_list()
        assert data["_source"] == "db"
        dfe = data["by_dept"]["Department for Education"]
        assert [m["name"] for m in dfe] == ["Lucy Powell", "Georgia Gould", "Baroness Smith of Malvern",
                                            "Josh MacAlister"]
        assert dfe[1] == {"name": "Georgia Gould", "role": "Minister of State (Education)",
                          "parliament_id": 5305, "is_lord": False}
        assert "HM Household" not in data["by_dept"]
        assert data["by_norm"]["blake of leeds"] == "Baroness in Waiting (HM Household) (Whip)"
        assert data["by_norm"]["daniel tomlinson"] == data["by_norm"]["dan tomlinson"]
        assert data["by_id"]["4263"] == "Secretary of State for Education"
        assert data["member_ids"]["Baroness Smith of Malvern"] == {"parliament_id": 269, "is_lord": True}

    def test_get_minister_list_reads_table_without_http(self, stored, no_http):
        assert debate_scanner.get_minister_list()["_source"] == "db"
        assert no_http == []

    def test_get_minister_list_falls_back_when_table_empty(self, app, monkeypatch):
        monkeypatch.setattr(debate_scanner, "_get_minister_list_live", lambda: {"by_norm": {"x": "y"}})
        assert debate_scanner.get_minister_list() == {"by_norm": {"x": "y"}}

    def test_dept_member_ids_from_table_no_http_no_file(self, stored, no_http, tmp_path, monkeypatch):
        path = tmp_path / "minister_cache.json"
        monkeypatch.setattr(debate_scanner, "MINISTER_CACHE_FILE", str(path))
        data = debate_scanner.get_minister_list()

        result = debate_scanner.get_dept_minister_member_ids("HM Treasury", data)

        # James Murray has no id (not in our member tables here): skipped, no API search
        assert [(r["name"], r["parliament_id"]) for r in result] == [
            ("John Healey", 400), ("Dan Tomlinson", 5282)]
        assert not path.exists()

    def test_verify_government_speaker_from_table(self, stored, no_http):
        from cache_models import MemberProfile
        from extensions import db
        db.session.add(MemberProfile(member_id=4912, slug="b", name="Baroness Blake", house="Lords",
                                     is_current=True))
        db.session.commit()
        assert debate_scanner.verify_government_speaker("Georgia Gould") == {
            "confirmed": True, "role": "Minister of State (Education)"}
        # Hansard short name: resolved through our member tables, then by member id
        assert debate_scanner.verify_government_speaker("Baroness Blake")["confirmed"] is True
        assert debate_scanner.verify_government_speaker("A Backbencher") == {"confirmed": False, "role": ""}


class TestPqAnsweringRole:
    def test_role_in_answering_department(self, stored):
        assert ministers.pq_answering_role(5305, "Department for Education", date(2026, 1, 5)) == \
            "Minister of State (Education)"

    def test_other_department_gives_none(self, stored):
        assert ministers.pq_answering_role(5305, "Cabinet Office", date(2026, 1, 5)) is None

    def test_answer_before_post_began_gives_none(self, stored):
        assert ministers.pq_answering_role(4263, "Department for Education", date(2025, 3, 1)) is None

    def test_ampersand_department_name(self, stored):
        assert ministers.pq_answering_role(
            1234, "Department for Environment, Food & Rural Affairs", date(2026, 8, 1)) == \
            "Secretary of State for Environment, Food and Rural Affairs"

    def test_empty_tables_give_no_title(self, app):
        # No stale fallback (cached_member.ministerial_role): no record, no title
        assert ministers.pq_answering_role(5305, "Department for Education", date(2026, 1, 5)) is None

    def test_table_status(self, stored):
        s = ministers.table_status()
        assert s["rows"] == len(_stored()) and s["departments"] == 4
        assert s["without_member_id"] == 1  # James Murray


# ── Migration ─────────────────────────────────────────────────────────────────

class TestMigration:
    def test_upgrade_is_idempotent_and_matches_model(self):
        from sqlalchemy import create_engine, inspect
        sys.path.insert(0, str(PROJECT_ROOT))
        from cache_models import GovMinister
        from migrations.gov_minister_table import upgrade

        engine = create_engine("sqlite:///:memory:")
        upgrade(engine)
        upgrade(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("gov_minister")}
        assert cols == {c.name for c in GovMinister.__table__.columns}
