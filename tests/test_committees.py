"""
Tests for committees and APPGs: DB-backed routes (/committees, /appgs and
detail pages, flag off -> 404), sitemap, helpers (open calls for evidence,
member committees/APPGs), the committees ingest (HTTP mocked), APPG register
parsing (saved real snippets in tests/fixtures/appg/) and the migration.

Run: python -m pytest tests/test_committees.py -v
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "appg"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ingest_appgs  # noqa: E402
import ingest_committees  # noqa: E402

# Relative to today so the "open now" logic in the pages (which uses the real clock) agrees.
NOW = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
CARE_DEADLINE = (NOW + timedelta(days=6)).replace(hour=16)
MENTAL_DEADLINE = (NOW + timedelta(days=15)).replace(hour=23, minute=59)


# ── Fake Committees API payloads (shapes copied from the live API) ────────────

def _committee(cid, name, house="Commons", category="Select", parent=None, end=None):
    return {
        "id": cid, "name": name, "house": house, "category": {"id": 1, "name": category},
        "committeeTypes": [{"id": 1, "name": "(HC) Public Standing Orders - Departmental"}],
        "parentCommittee": {"id": parent, "name": "Parent"} if parent else None,
        "showOnWebsite": True, "startDate": "2010-07-13T00:00:00", "endDate": end,
        "contact": {"email": f"c{cid}@parliament.uk", "phone": None, "address": "House of Commons, London"},
    }


def _detail(cid, purpose):
    return {"id": cid, "purpose": purpose}


def _member(mnis, name, party="Labour", roles=("Member",), chair=False, lay=False):
    return {
        "isLayMember": lay, "id": mnis * 10, "name": f"{name} MP",
        "roles": [{"startDate": "2024-10-21T00:00:00", "endDate": None,
                   "role": {"id": 1, "name": r, "isChair": r == "Chair"}, "exOfficio": False}
                  for r in roles],
        "memberInfo": None if lay else {"mnisId": mnis, "displayAs": name, "party": party,
                                        "memberFrom": f"{name}shire", "house": "Commons", "isChair": chair},
    }


def _business(bid, title, kind="Inquiry", open_periods=(), report=None):
    return {
        "id": bid, "title": title,
        "type": {"id": 1, "name": kind, "isInquiry": kind == "Inquiry"},
        "openDate": "2026-06-02T00:00:00", "closeDate": None, "latestReport": report,
        "openSubmissionPeriods": [{"id": pid, "startDate": "2026-09-01T00:00:00", "endDate": end,
                                   "submissionType": "WrittenEvidence"} for pid, end in open_periods],
        "closedSubmissionPeriods": [], "nextOralEvidenceSession": None,
    }


def _publication(pid, cid, description, published, kind="Report", business=None):
    return {
        "id": pid, "description": description, "publicationStartDate": published,
        "type": {"id": 1, "name": kind}, "hcNumber": {"number": "HC 150"}, "hlPaper": None,
        "additionalContentUrl": None, "documents": [{"documentId": 9000 + pid}],
        "businesses": [business] if business else [],
        "governmentResponses": {"publication": [], "responseDeadline": "2026-12-01T00:00:00"},
        "committee": {"id": cid},
    }


EDUCATION = _committee(203, "Education Committee")
HEALTH = _committee(81, "Health and Social Care Committee")
LORDS = _committee(518, "Built Environment Committee", house="Lords")
BILL = _committee(900, "Social Housing Bill", category="General")

SEND = _business(9684, "Solving the SEND Crisis")
MENTAL = _business(9627, "Children and Young Peopleâ€™s Mental Health",   # API mojibake
                   open_periods=[(3854, MENTAL_DEADLINE.isoformat())])
CARE = _business(10001, "Children's social care", open_periods=[(3900, CARE_DEADLINE.isoformat())])
OPEN_ENDED = _business(8885, "Electronic voting", open_periods=[(3585, None)])

API = {
    "/Committees": [EDUCATION, HEALTH, LORDS, BILL],
    "detail": {
        203: _detail(203, "<p>The Education Committee scrutinises the work of the Department for "
                          "Education, covering schools and&nbsp;children&rsquo;s social care.</p>"),
        81: _detail(81, "<p>Scrutinises the NHS, health and social care.</p>"),
        518: _detail(518, "<p>Looks at housing and planning.</p>"),
        900: _detail(900, None),
    },
    "members": {
        203: [_member(4510, "Helen Hayes", roles=("Member", "Chair"), chair=True),
              _member(4592, "Dr Caroline Johnson", party="Conservative")],
        81: [_member(4600, "Layla Moran", party="Liberal Democrat", roles=("Chair",), chair=True),
             _member(4592, "Dr Caroline Johnson", party="Conservative"),
             _member(0, "Lay Person", lay=True)],
        518: [], 900: [],
    },
    "business": {203: [SEND, MENTAL, CARE], 81: [MENTAL], 518: [OPEN_ENDED], 900: []},
    "publications": {
        203: [_publication(54321, 203, "2nd Report - Reading for Pleasure", "2026-07-17T00:01:00",
                           business={"id": 9456, "title": "Reading for Pleasure"})],
        81: [], 518: [], 900: [],
    },
    "business_detail": {
        9684: {"scope": "<p>Support for pupils with special educational needs.</p>"},
        9627: {"scope": "<p>Mental health support in schools and the NHS for children.</p>"},
        10001: {"scope": "<p>Children's social care and fostering.</p>"},
        8885: {"scope": "<p>Should Parliament adopt electronic voting?</p>"},
    },
}


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def _paged(items, params):
    skip, take = params.get("Skip", 0), params.get("Take", 100)
    return _resp({"items": items[skip:skip + take], "totalResults": len(items)})


def _fake_committees_api(api, calls=None):
    calls = calls if calls is not None else []

    def get(url, params=None, headers=None, timeout=None):
        params = params or {}
        path = url.replace(ingest_committees.API_BASE, "")
        calls.append(path)
        parts = path.strip("/").split("/")
        if path == "/Committees":
            return _paged(api["/Committees"], params)
        if parts[0] == "Committees" and len(parts) == 2:
            return _resp(api["detail"][int(parts[1])])
        if parts[0] == "Committees" and parts[2] == "Members":
            return _paged(api["members"][int(parts[1])], params)
        if path == "/CommitteeBusiness":
            return _paged(api["business"][params["CommitteeId"]], params)
        if parts[0] == "CommitteeBusiness":
            return _resp({"id": int(parts[1]), **api["business_detail"][int(parts[1])]})
        if path == "/Publications":
            return _paged(api["publications"][params["CommitteeId"]], params)
        return _resp({}, 404)

    return get, calls


def _no_sleep(_seconds):
    return None


def _get(model, pk):
    from extensions import db
    return db.session.get(model, pk)


def _run_committees(api_data=None, calls=None, **kwargs):
    get, calls = _fake_committees_api(api_data or API, calls)
    with patch("ingest_committees.requests.get", side_effect=get):
        return ingest_committees.ingest(api=ingest_committees.Api(sleep=_no_sleep), now=NOW, **kwargs)


# ── APPG fixtures ─────────────────────────────────────────────────────────────

def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _member_index():
    idx = ingest_appgs.MemberIndex()
    idx.add(4733, ["Mims Davies"], "Conservative", "Commons")
    idx.add(5167, ["Josh Dean"], "Labour", "Commons")
    idx.add(4592, ["Dr Caroline Johnson"], "Conservative", "Commons")
    idx.add(5087, ["Anna Dixon"], "Labour", "Commons")
    idx.add(4161, ["Baroness Finlay of Llandaff"], "Crossbench", "Lords")
    idx.add(5197, ["Luke Taylor"], "Liberal Democrat", "Commons")
    idx.add(4924, ["Paul Holmes"], "Conservative", "Commons")
    return idx


YEAR_PAGE = ('<a href="https://publications.parliament.uk/pa/cm/cmallparty/261012/contents.htm">HTML Version</a>'
             '<a href="https://publications.parliament.uk/pa/cm/cmallparty/260901/contents.htm">HTML Version</a>')
INDEX_PAGE = ('<a href="/mps-lords-and-offices/standards-and-financial-interests/parliamentary-commissioner-for-'
              'standards/registers-of-interests/register-of-all-party-party-parliamentary-groups/'
              'registers-published-in-2026/">Registers published in 2026</a>'
              '<a href="/mps-lords-and-offices/x/registers-published-in-2025/">Registers published in 2025</a>')
CONTENTS = ('<a href="introduction.htm">INTRODUCTION</a><a href="#subject-groups">SUBJECT GROUPS</a>'
            '<a href="hospice-and-end-of-life-care.htm">Hospice and End of Life Care</a>'
            '<a href="youth-employment.htm">Youth Employment</a>')


def _fake_register(pages: dict, calls: list):
    def get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        for key, body in pages.items():
            if url.endswith(key):
                r = MagicMock()
                r.status_code = 200
                r.content = body.encode("utf-8")
                return r
        r = MagicMock()
        r.status_code = 404
        r.content = b""
        return r
    return get


def _register_pages(contents=CONTENTS, groups=None):
    return {
        "register-of-all-party-party-parliamentary-groups/": INDEX_PAGE,
        "registers-published-in-2026/": YEAR_PAGE,
        "261012/contents.htm": contents,
        **(groups if groups is not None else {
            "261012/hospice-and-end-of-life-care.htm": _fixture("hospice-and-end-of-life-care.html"),
            "261012/youth-employment.htm": _fixture("youth-employment.html"),
        }),
    }


def _run_appgs(pages=None, index=None, calls=None, **kwargs):
    calls = calls if calls is not None else []
    with patch("ingest_appgs.requests.get", side_effect=_fake_register(pages or _register_pages(), calls)):
        return ingest_appgs.ingest(fetch=ingest_appgs.Fetcher(sleep=_no_sleep), index=index or _member_index(),
                                   now=NOW, **kwargs)


# ── App / DB fixtures ─────────────────────────────────────────────────────────

def _make_app(register_blueprint=True):
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db
    from feature_flags import feature_enabled
    import cache_models  # noqa: F401  (ha_bill_sponsor FK target)
    import committees
    import committees_models  # noqa: F401
    import hansard_archive.models  # noqa: F401

    a = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    a.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True, "SECRET_KEY": "test-secret", "LOGIN_DISABLED": True,
    })
    _db.init_app(a)
    lm = LoginManager()
    lm.init_app(a)
    lm.user_loader(lambda uid: None)

    @a.context_processor
    def inject_globals():
        return {"admin_authenticated": False, "app_version": "test"}

    a.jinja_env.globals["feature_enabled"] = feature_enabled
    if register_blueprint:
        a.register_blueprint(committees.committees_bp)
    return a


@pytest.fixture(scope="module")
def app():
    from extensions import db as _db
    old_flag = os.environ.get("FEATURE_COMMITTEES")
    os.environ["FEATURE_COMMITTEES"] = "live"
    test_app = _make_app()
    with test_app.app_context():
        _db.create_all()
        s = _run_committees()
        assert s["committee_failures"] == 0
        _run_appgs()
    yield test_app
    with test_app.app_context():
        _db.session.remove()
        _db.drop_all()
    if old_flag is None:
        os.environ.pop("FEATURE_COMMITTEES", None)
    else:
        os.environ["FEATURE_COMMITTEES"] = old_flag


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


@pytest.fixture
def ingest_app():
    """Fresh empty DB per ingest test."""
    from extensions import db as _db
    a = _make_app(register_blueprint=False)
    with a.app_context():
        _db.create_all()
        yield a
        _db.session.remove()
        _db.drop_all()


_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


# ── Routes ────────────────────────────────────────────────────────────────────

class TestCommitteePages:
    def test_list_defaults_to_select_committees(self, client):
        html = client.get("/committees").get_data(as_text=True)
        assert "Education Committee" in html and "Built Environment Committee" in html
        assert "Social Housing Bill" not in html          # General category hidden by default
        assert _INDEX in html
        assert '"@type": "CollectionPage"' in html
        assert 'href="https://westminsterbrief.co.uk/committees"' in html

    def test_list_shows_open_calls_soonest_first(self, client):
        html = client.get("/committees").get_data(as_text=True)
        assert html.index("Children&#39;s social care") < html.index("Children and Young People’s Mental Health")
        assert html.index("Children and Young People’s Mental Health") < html.index("Electronic voting")
        assert "No deadline published" in html
        assert "https://committees.parliament.uk/call-for-evidence/3900/" in html

    def test_filters_are_noindex(self, client):
        for qs in ("?house=lords", "?type=all", "?q=mental", "?topic=education"):
            assert _NOINDEX in client.get("/committees" + qs).get_data(as_text=True), qs

    def test_house_and_type_filters(self, client):
        html = client.get("/committees?house=lords").get_data(as_text=True)
        assert "Built Environment Committee" in html and "Education Committee" not in html
        assert "Social Housing Bill" in client.get("/committees?type=all").get_data(as_text=True)

    def test_search_matches_inquiry_titles(self, client):
        html = client.get("/committees?q=SEND").get_data(as_text=True)
        assert "Education Committee" in html and "Health and Social Care Committee" not in html

    def test_topic_filter(self, client):
        html = client.get("/committees?topic=health-and-social-care").get_data(as_text=True)
        assert "Health and Social Care Committee" in html

    def test_detail_page(self, client):
        resp = client.get("/committees/203/education-committee")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Helen Hayes" in html and "Chair" in html
        assert html.index("Helen Hayes") < html.index("Dr Caroline Johnson")
        assert "https://members.parliament.uk/member/4510/" in html     # no /members route in this app
        assert "children’s social care" in html                         # purpose: HTML entities cleaned
        assert "Solving the SEND Crisis" in html
        assert "Reading for Pleasure" in html
        assert "https://committees.parliament.uk/publications/54321/documents/63321/default/" in html
        assert '"@type": "GovernmentOrganization"' in html
        assert "joint with" in html                                     # Mental health inquiry is joint
        assert _INDEX in html

    def test_short_and_wrong_slug_redirect(self, client):
        assert client.get("/committees/203").headers["Location"].endswith("/committees/203/education-committee")
        r = client.get("/committees/203/old-name")
        assert r.status_code == 301 and r.headers["Location"].endswith("/committees/203/education-committee")

    def test_unknown_committee_404(self, client):
        assert client.get("/committees/99999/x").status_code == 404


class TestAppgPages:
    def test_list(self, client):
        html = client.get("/appgs").get_data(as_text=True)
        assert "Youth Employment" in html and "Hospice and End of Life Care" in html
        assert "Chair: Mims Davies" in html
        assert _INDEX in html

    def test_search_and_filters_noindex(self, client):
        html = client.get("/appgs?q=hospice").get_data(as_text=True)
        assert "Hospice and End of Life Care" in html and "Youth Employment" not in html
        assert _NOINDEX in html
        html = client.get("/appgs?q=Youth+Employment+UK").get_data(as_text=True)   # secretariat
        assert "Youth Employment" in html

    def test_topic_filter(self, client):
        html = client.get("/appgs?topic=health-and-social-care").get_data(as_text=True)
        assert "Hospice and End of Life Care" in html and "Youth Employment" not in html

    def test_detail(self, client, app):
        from committees_models import Appg
        with app.app_context():
            appg = Appg.query.filter_by(register_slug="hospice-and-end-of-life-care").one()
            path = f"/appgs/{appg.id}/hospice-and-end-of-life-care"
        html = client.get(path).get_data(as_text=True)
        assert "Baroness Finlay of Llandaff" in html and "Crossbench" in html
        assert "https://members.parliament.uk/member/4161/" in html
        assert "Hospice UK, Marie Curie and Sue Ryder jointly act as the group" in html
        assert "Joint Secretariat" in html and "3,001-4,500" in html
        assert '"@type": "Organization"' in html
        assert "example.org" not in html and "020 0000" not in html   # no contact details republished

    def test_page_past_last_404(self, client):
        assert client.get("/appgs?page=9").status_code == 404


class TestFlagOff:
    def test_every_route_404_when_hidden(self, client):
        old = os.environ.get("FEATURE_COMMITTEES")
        os.environ["FEATURE_COMMITTEES"] = "hidden"
        try:
            for path in ("/committees", "/committees/203", "/committees/203/education-committee",
                         "/appgs", "/appgs/1", "/appgs/1/youth-employment"):
                assert client.get(path).status_code == 404, path
        finally:
            os.environ["FEATURE_COMMITTEES"] = old or "live"


class TestHelpersAndSitemap:
    def test_open_calls_for_evidence_by_topic_and_keyword(self, app):
        import committees
        with app.app_context():
            calls = committees.open_calls_for_evidence(now=NOW)
            assert [c["title"] for c in calls] == [
                "Children's social care", "Children and Young People’s Mental Health", "Electronic voting"]
            mental = calls[1]
            assert {c["name"] for c in mental["committees"]} == {"Education Committee",
                                                                  "Health and Social Care Committee"}
            assert mental["days_left"] == 15 and mental["deadline"] == MENTAL_DEADLINE
            assert [c["title"] for c in committees.open_calls_for_evidence(keyword="fostering", now=NOW)] == [
                "Children's social care"]
            health = committees.open_calls_for_evidence(topic="health-and-social-care", now=NOW)
            assert [c["inquiry_id"] for c in health] == [10001, 9627]      # "social care", "mental health"
            # After the deadline has passed the call drops out
            later = committees.open_calls_for_evidence(now=NOW + timedelta(days=7))
            assert "Children's social care" not in [c["title"] for c in later]

    def test_get_member_committees_appgs(self, app):
        import committees
        with app.app_context():
            out = committees.get_member_committees_appgs(4592)
            assert [c["name"] for c in out["committees"]] == ["Education Committee",
                                                             "Health and Social Care Committee"]
            assert [a["name"] for a in out["appgs"]] == ["Youth Employment"]
            chair = committees.get_member_committees_appgs(4510)
            assert chair["committees"][0]["role"] == "Chair" and chair["committees"][0]["is_chair"]
            assert committees.get_member_committees_appgs(123456) == {"committees": [], "appgs": []}

    def test_member_urls_link_our_page_only_for_members_we_hold(self, ingest_app):
        import committees
        from cache_models import CachedMember
        from extensions import db as _db
        ingest_app.add_url_rule("/members/<int:member_id>", "member_stub", lambda member_id: "")
        _db.session.add(CachedMember(member_id=4510, name="Helen Hayes"))
        _db.session.commit()
        with ingest_app.test_request_context():
            urls = committees.member_urls([4510, 4592, None])
        assert urls == {4510: "/members/4510", 4592: "https://members.parliament.uk/member/4592/"}

    def test_sitemap(self, app):
        import committees
        with app.app_context():
            xml = committees.build_sitemap_xml()
        assert "<loc>https://westminsterbrief.co.uk/committees</loc>" in xml
        assert "/committees/203/education-committee" in xml
        assert "/appgs/" in xml and "youth-employment" in xml

    def test_text_helpers(self):
        import committees
        assert committees.fix_mojibake("Childrenâ€™s") == "Children’s"
        assert committees.fix_mojibake("Café ok") == "Café ok"
        assert committees.clean_text("<p>One&nbsp;two</p><p>Three</p>") == "One two\n\nThree"
        assert committees.topic_slugs_for("Youth Employment", "") == ",employment-and-labour-market,"


# ── Committees ingest ─────────────────────────────────────────────────────────

class TestCommitteesIngest:
    def test_first_run_counts(self, ingest_app):
        from committees_models import (Committee, CommitteeInquiry, CommitteeInquiryLink,
                                       CommitteeMembership, CommitteePublication)
        s = _run_committees()
        assert s["committees_new"] == 4 and s["committees_updated"] == 0
        assert s["inquiries_new"] == 4 and s["inquiries_updated"] == 1    # joint inquiry seen twice
        assert s["inquiry_details"] == 4 and s["publications_new"] == 1
        assert Committee.query.count() == 4
        assert CommitteeMembership.query.count() == 5
        assert CommitteeInquiry.query.count() == 4
        assert CommitteeInquiryLink.query.filter_by(inquiry_id=9627).count() == 2
        assert CommitteePublication.query.one().response_deadline == date(2026, 12, 1)
        mh = _get(CommitteeInquiry, 9627)
        assert mh.title == "Children and Young People’s Mental Health"
        assert mh.evidence_open and mh.evidence_deadline == MENTAL_DEADLINE
        assert ",health-and-social-care," in mh.topic_slugs
        lay = CommitteeMembership.query.filter_by(is_lay_member=True).one()
        assert lay.member_id is None
        hayes = CommitteeMembership.query.filter_by(member_id=4510).one()
        assert hayes.role == "Chair" and hayes.is_chair and hayes.roles == ["Chair", "Member"]

    def test_rerun_is_idempotent_and_skips_unchanged_details(self, ingest_app):
        from committees_models import CommitteeInquiry, CommitteeMembership, CommitteePublication
        _run_committees()
        calls = []
        s = _run_committees(calls=calls)
        assert s["committees_new"] == 0 and s["committees_updated"] == 4
        assert s["inquiry_details"] == 0
        assert not any(c.startswith("/CommitteeBusiness/") for c in calls)
        assert CommitteeMembership.query.count() == 5
        assert CommitteeInquiry.query.count() == 4
        assert CommitteePublication.query.count() == 1

    def test_closed_inquiry_and_retired_committee(self, ingest_app):
        from committees_models import Committee, CommitteeInquiry
        _run_committees()
        data = {**API, "/Committees": [EDUCATION, HEALTH, LORDS],
                "business": {**API["business"], 203: [SEND, MENTAL]}}
        s = _run_committees(data)
        assert s["committees_retired"] == 1 and not _get(Committee, 900).is_current
        assert s["inquiries_closed"] == 1
        care = _get(CommitteeInquiry, 10001)
        assert care.status == "Closed" and not care.evidence_open

    def test_partial_failure_does_not_close_inquiries(self, ingest_app):
        from committees_models import CommitteeInquiry, CommitteeMembership
        _run_committees()
        get, _ = _fake_committees_api(API)

        def flaky(url, params=None, headers=None, timeout=None):
            if url.endswith("/CommitteeBusiness") and (params or {}).get("CommitteeId") == 203:
                raise requests.ConnectionError("down")
            if url.endswith("/Committees/203/Members"):
                raise requests.ConnectionError("down")
            return get(url, params, headers, timeout)

        with patch("ingest_committees.requests.get", side_effect=flaky):
            s = ingest_committees.ingest(api=ingest_committees.Api(sleep=_no_sleep), now=NOW)
        assert s["committee_failures"] >= 1 and s["inquiries_closed"] == 0
        assert _get(CommitteeInquiry, 10001).status == "Open"
        assert CommitteeMembership.query.filter_by(committee_id=203).count() == 2   # kept, not wiped

    def test_dry_run_writes_nothing(self, ingest_app):
        from committees_models import Committee, CommitteeInquiry
        calls = []
        s = _run_committees(calls=calls, dry_run=True)
        assert s["committees_new"] == 4 and s["inquiries_new"] == 5
        assert Committee.query.count() == 0 and CommitteeInquiry.query.count() == 0
        assert not any(c.startswith("/CommitteeBusiness/") for c in calls)

    def test_all_requests_failing_is_reported(self, ingest_app):
        with patch("ingest_committees.requests.get", side_effect=requests.ConnectionError("down")):
            s = ingest_committees.ingest(api=ingest_committees.Api(sleep=_no_sleep), now=NOW)
        assert s["committees_listed"] == 0 and ingest_committees.mostly_failed(s)

    def test_evidence_window(self):
        past, future = NOW - timedelta(days=19), NOW + timedelta(days=12)
        item = _business(1, "x", open_periods=[(1, past.isoformat()), (2, future.isoformat())])
        assert ingest_committees.evidence_window(item, NOW)[:2] == (True, future)
        item = _business(1, "x", open_periods=[(1, past.isoformat())])
        assert ingest_committees.evidence_window(item, NOW)[0] is False


# ── APPG parsing and ingest ───────────────────────────────────────────────────

class TestAppgParsing:
    def test_parse_youth_employment(self):
        p = ingest_appgs.parse_group(_fixture("youth-employment.html"))
        assert p["title"] == "All-Party Parliamentary Group for Youth Employment"
        assert p["category"] == "Subject Group"
        assert p["purpose"].startswith("To promote youth employment")
        assert [(o["role"], o["name"], o["party"]) for o in p["officers"]] == [
            ("Chair & Registered Contact", "Mims Davies", "Conservative"),
            ("Officer", "Josh Dean", "Labour"),
            ("Officer", "Dr Caroline Johnson", "Conservative")]
        assert p["registered_contact_name"] == "Mims Davies MP"
        assert p["secretariat"].startswith("Youth Employment UK acts as the group's secretariat")
        assert p["website"] == "https://www.youthemployment.org.uk/about-us/appg-for-youth-employment/"
        assert p["agm_date"] == date(2026, 2, 25)
        assert p["benefits"] == []

    def test_parse_benefits_and_co_chair(self):
        p = ingest_appgs.parse_group(_fixture("hospice-and-end-of-life-care.html"))
        assert [o["name"] for o in p["officers"]] == ["Anna Dixon", "Baroness Finlay of Llandaff",
                                                      "Luke Taylor", "Paul Holmes"]
        assert p["benefits"] == [{"kind": "In kind", "source": "Hospice UK",
                                  "description": "Joint Secretariat From: 19/11/2025 To: 18/11/2026",
                                  "value": "3,001-4,500", "received": "19/11/2025", "registered": "21/05/2026"}]
        assert "example.org" not in (p["secretariat"] or "")

    def test_not_a_group_page(self):
        assert ingest_appgs.parse_group("<html><body><p>Page cannot be found</p></body></html>") is None

    def test_parse_contents(self):
        groups = ingest_appgs.parse_contents(_fixture("contents.html"))
        assert groups[0] == ("africa", "Africa")
        assert ("introduction", "INTRODUCTION") not in groups
        assert all(not slug.startswith("#") for slug, _ in groups)

    def test_role_flags(self):
        assert ingest_appgs.role_flags("Chair & Registered Contact") == (True, True)
        assert ingest_appgs.role_flags("Co-Chair") == (True, False)
        assert ingest_appgs.role_flags("Vice Chair") == (False, False)
        assert ingest_appgs.role_flags("Co-Vice Chair") == (False, False)
        assert ingest_appgs.role_flags("Officer") == (False, False)

    def test_member_matching_confidence(self):
        idx = ingest_appgs.MemberIndex()
        idx.add(1, ["Dame Chi Onwurah"], "Labour", "Commons")
        idx.add(2, ["Lord Oates"], "Liberal Democrat", "Lords")
        idx.add(3, ["Baroness Smith of Malvern"], "Labour", "Lords")
        idx.add(4, ["John Smith"], "Labour", "Commons")
        idx.add(5, ["John Smith"], "Conservative", "Commons")
        idx.add(6, ["Sir Iain Duncan Smith"], "Conservative", "Commons")
        assert idx.match("Dame Chi Onwurah", "Labour") == (1, "high")
        assert idx.match("Chi Onwurah", "Labour") == (1, "medium")
        assert idx.match("Lord Oates", "Liberal Democrat") == (2, "high")
        assert idx.match("Baroness Smith", "Labour") == (3, "medium")
        assert idx.match("John Smith", "Conservative") == (5, "high")    # party disambiguates
        assert idx.match("John Smith", "") == (None, "none")             # ambiguous: never guessed
        assert idx.match("Iain Duncan-Smith", "Conservative") == (6, "medium")  # hyphen, no "Sir"
        assert idx.match("Iain Duncan Smyth", "Conservative") == (6, "low")     # fuzzy: kept, not linked
        assert idx.match("Nobody Atall", "Labour") == (None, "none")

    def test_edition_helpers(self):
        assert ingest_appgs.editions_in(YEAR_PAGE) == ["261012", "260901"]
        assert ingest_appgs.edition_date("261012") == date(2026, 10, 12)


class TestAppgIngest:
    def test_ingest_counts_and_matches(self, ingest_app):
        from committees_models import Appg, AppgOfficer
        s = _run_appgs()
        assert s["edition"] == "261012" and s["new"] == 2 and s["parse_failed"] == 0
        assert s["officers"] == 7 and s["matched_high"] == 7 and s["unmatched"] == 0
        assert Appg.query.count() == 2
        youth = Appg.query.filter_by(register_slug="youth-employment").one()
        assert youth.register_edition == date(2026, 10, 12) and youth.is_current
        assert youth.registered_contact_name == "Mims Davies MP"
        chair = AppgOfficer.query.filter_by(appg_id=youth.id, is_chair=True).one()
        assert chair.member_id == 4733 and chair.match_confidence == "high" and chair.is_registered_contact

    def test_same_edition_is_skipped_then_force_is_unchanged(self, ingest_app):
        _run_appgs()
        calls = []
        s = _run_appgs(calls=calls)
        assert s["skipped"] and not any("youth-employment" in c for c in calls)
        assert not ingest_appgs.mostly_failed(s)
        s = _run_appgs(force=True)
        assert s["unchanged"] == 2 and s["updated"] == 0

    def test_unmatched_officer_kept_and_dropped_group_retired(self, ingest_app):
        from committees_models import Appg, AppgOfficer
        _run_appgs()
        idx = _member_index()
        pages = _register_pages(
            contents=CONTENTS.replace('<a href="hospice-and-end-of-life-care.htm">Hospice and End of Life Care</a>', ""),
            groups={"261012/youth-employment.htm": _fixture("youth-employment.html").replace(
                "Mims Davies", "New Officer Name")})
        s = _run_appgs(pages=pages, index=idx, force=True)
        assert s["retired"] == 1 and s["updated"] == 1 and s["unmatched"] == 1
        assert not Appg.query.filter_by(register_slug="hospice-and-end-of-life-care").one().is_current
        kept = AppgOfficer.query.filter_by(name="New Officer Name").one()
        assert kept.member_id is None and kept.match_confidence == "none"

    def test_dry_run_writes_nothing(self, ingest_app):
        from committees_models import Appg
        s = _run_appgs(dry_run=True)
        assert s["new"] == 2 and s["officers"] == 7
        assert Appg.query.count() == 0

    def test_register_down_is_reported(self, ingest_app):
        with patch("ingest_appgs.requests.get", side_effect=requests.ConnectionError("down")):
            s = ingest_appgs.ingest(fetch=ingest_appgs.Fetcher(sleep=_no_sleep), index=_member_index(), now=NOW)
        assert s["edition"] is None and ingest_appgs.mostly_failed(s)


# ── Migration ─────────────────────────────────────────────────────────────────

class TestMigration:
    def test_upgrade_is_idempotent_on_sqlite(self):
        from sqlalchemy import create_engine, inspect, text
        sys.path.insert(0, str(PROJECT_ROOT))
        from migrations.committees_appgs_tables import TABLES, steps, upgrade

        engine = create_engine("sqlite:///:memory:")
        upgrade(engine)
        upgrade(engine)
        insp = inspect(engine)
        assert set(TABLES) <= set(insp.get_table_names())
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO committee (id, name) VALUES (1, 'C')"))
            conn.execute(text("INSERT INTO committee_membership (committee_id, name) VALUES (1, 'A')"))
            conn.execute(text("INSERT INTO appg (register_slug, title, short_name) VALUES ('x', 'T', 'X')"))
            conn.execute(text("INSERT INTO appg_officer (appg_id, role, name) VALUES (1, 'Chair', 'A')"))
        assert len(steps("postgresql")) == len(steps("sqlite"))

    def test_migration_matches_models(self):
        """Every model column exists in the migration's CREATE TABLE (so create_all and prod agree)."""
        from sqlalchemy import create_engine, inspect
        sys.path.insert(0, str(PROJECT_ROOT))
        import committees_models as m
        from migrations.committees_appgs_tables import upgrade

        engine = create_engine("sqlite:///:memory:")
        upgrade(engine)
        insp = inspect(engine)
        for model in (m.Committee, m.CommitteeMembership, m.CommitteeInquiry, m.CommitteeInquiryLink,
                      m.CommitteePublication, m.Appg, m.AppgOfficer):
            cols = {c["name"] for c in insp.get_columns(model.__tablename__)}
            assert {c.name for c in model.__table__.columns} == cols, model.__tablename__
