"""
Tests for GOV.UK consultations: policy-area mapping, deadlines, parsing,
DB-backed routes (/consultations, /consultations/<slug>, sitemap), the digest
helper, the ingest script (HTTP mocked) and the migration (SQLite).

Run: python -m pytest tests/test_consultations.py -v
"""
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
import ingest_consultations as ing  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0)          # 13:00 BST


# ── Fake GOV.UK payloads ──────────────────────────────────────────────────────

def _org(slug, title, acronym="", parents=()):
    return {"slug": slug, "title": title, "acronym": acronym,
            "organisation_type": "ministerial_department", "parent_organisations": list(parents),
            "link": f"/government/organisations/{slug}"}


def _taxon(base_path, title, *ancestors):
    node = {"base_path": base_path, "title": title, "links": {}}
    cur = node
    for a in ancestors:
        parent = {"base_path": a, "title": a.strip("/"), "links": {}}
        cur["links"]["parent_taxons"] = [parent]
        cur = parent
    return node


def _content(slug, title, doc_type, opening, closing, org, taxons=(), description="",
             body="<p>About this.</p>", ways=None, attachments=(), featured=(), outcome_ids=(),
             outcome_detail=None, updated="2026-10-01T09:00:00+01:00", withdrawn=None,
             content_id=None):
    details = {
        "body": body, "opening_date": opening, "closing_date": closing,
        "attachments": list(attachments), "featured_attachments": list(featured),
        "national_applicability": {"england": {"applicable": True, "label": "England"},
                                   "wales": {"applicable": False, "label": "Wales"}},
    }
    if ways:
        details["ways_to_respond"] = ways
    if outcome_ids:
        details["final_outcome_attachments"] = list(outcome_ids)
        details["final_outcome_detail"] = outcome_detail or ""
    return {
        "content_id": content_id or f"cid-{slug}",
        "base_path": f"/government/consultations/{slug}",
        "title": title, "description": description, "document_type": doc_type,
        "schema_name": "consultation", "public_updated_at": updated,
        "first_published_at": opening, "details": details,
        "withdrawn_notice": withdrawn or {},
        "links": {
            "organisations": [{"title": org["title"], "base_path": org["link"]}],
            "primary_publishing_organisation": [{"title": org["title"], "base_path": org["link"]}],
            "taxons": list(taxons),
        },
        "_search_org": org,
    }


def _attachment(i, title, kind="file", url=None, content_type="application/pdf", pages=12):
    return {"id": str(i), "title": title, "attachment_type": kind,
            "url": url or f"https://assets.publishing.service.gov.uk/media/{i}/doc.pdf",
            "content_type": content_type if kind == "file" else None, "number_of_pages": pages}


DFE = _org("department-for-education", "Department for Education", "DfE")
DFT = _org("department-for-transport", "Department for Transport", "DfT")
DESNZ = _org("department-for-energy-security-and-net-zero", "Department for Energy Security and Net Zero",
             "DESNZ")
CMA = _org("competition-and-markets-authority", "Competition and Markets Authority", "CMA",
           parents=("department-for-business-innovation-science-and-trade",))

SCHOOLS = _content(
    "school-meals", "​​Free school meals​: expanding eligibility", "open_consultation",
    "2026-10-01T09:30:00.000+01:00", "2026-10-22T23:59:00.000+01:00", DFE,
    taxons=[_taxon("/education/school-meals", "School meals", "/education")],
    description="We want views on expanding free school meals to more pupils.",
    body="<p>This consultation asks about eligibility.</p><ul><li>who qualifies</li><li>funding</li></ul>",
    ways={"link_url": "https://consult.education.gov.uk/fsm", "email": "fsm@education.gov.uk",
          "postal_address": "<p>FSM team<br>Sanctuary Buildings</p>"},
    attachments=[_attachment(1, "Consultation document"), _attachment(2, "Easy read", kind="html",
                                                                      url="/government/consultations/school-meals/easy-read")],
    featured=["1", "2"],
)
BUSES = _content(
    "rural-buses", "Rural bus services", "open_consultation",
    "2026-09-01T09:30:00.000+01:00", "2026-12-15T23:59:00.000+00:00", DFT,
    taxons=[_taxon("/transport/buses", "Buses", "/transport/local-transport", "/transport")],
    description="Proposals for better rural buses.",
    ways={"link_url": "https://dft.citizenspace.com/buses"},
)
HEAT = _content(
    "clean-heat", "Clean Heat Market Mechanism: revisions", "closed_consultation",
    "2026-06-01T09:30:00.000+01:00", "2026-09-01T23:59:00.000+01:00", DESNZ,
    taxons=[_taxon("/business-and-industry/business-regulation", "Business regulation",
                   "/business-and-industry")],
    description="Targets for heat pump credits.",
)
PARKING = _content(
    "private-parking", "Private parking code of practice", "consultation_outcome",
    "2025-07-11T09:30:00.000+01:00", "2025-09-26T23:59:00.000+01:00", DFT,
    taxons=[_taxon("/transport/parking", "Parking", "/transport/road-infrastructure", "/transport")],
    attachments=[_attachment(10, "Consultation"), _attachment(11, "Government response", kind="html",
                                                              url="/government/consultations/private-parking/outcome/response")],
    featured=["10"], outcome_ids=["11"],
    outcome_detail="<p>The government response explains the new code.</p>",
)
APPLE = _content(
    "apple-nfc", "Proposed NFC access requirement for Apple", "open_consultation",
    "2026-10-20T11:00:00.000+01:00", "2026-11-17T23:59:00.000+00:00", CMA,
    taxons=[_taxon("/business-and-industry/business-regulation", "Business regulation",
                   "/business-and-industry")],
    description="The CMA's proposed conduct requirement.", withdrawn={"explanation": "Withdrawn."},
)
ALL = (SCHOOLS, BUSES, HEAT, PARKING, APPLE)


def _search_row(content):
    return {"title": content["title"], "link": content["base_path"], "content_id": content["content_id"],
            "description": content["description"], "organisations": [content["_search_org"]],
            "public_timestamp": content["public_updated_at"],
            "start_date": content["details"]["opening_date"], "end_date": content["details"]["closing_date"]}


def _values(content):
    return ing.parse_content(content, {**_search_row(content), "_doc_type": content["document_type"]})


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def _fake_govuk(contents, gone=()):
    """requests.get stand-in for the Search and Content APIs."""
    calls = {"search": [], "content": []}

    def get(url, params=None, timeout=None, headers=None):
        if url == ing.SEARCH_URL:
            doc_type = params["filter_content_store_document_type"]
            calls["search"].append(doc_type)
            rows = [_search_row(c) for c in contents if c["document_type"] == doc_type]
            start, count = params["start"], params["count"]
            return _resp({"results": rows[start:start + count], "total": len(rows), "start": start})
        path = url[len(ing.CONTENT_URL):]
        calls["content"].append(path)
        if path in gone:
            return _resp(None, 410)
        found = next((c for c in contents if c["base_path"] == path), None)
        return _resp(found) if found else _resp(None, 404)

    return get, calls


def _no_sleep(_seconds):
    return None


# ── Pure functions ────────────────────────────────────────────────────────────

class TestPolicyAreas:
    def test_taxon_root_and_department(self):
        v = _values(SCHOOLS)
        assert v["policy_areas"][0]["slug"] == "education"
        assert v["policy_areas"][0]["score"] >= 6          # taxon 3 + department 3 + keywords

    def test_level_two_override_and_department_beats_generic_taxon(self):
        areas = consultations.assign_policy_areas(
            "Clean Heat Market Mechanism", "heat pump credits",
            [{"base_path": "/business-and-industry/business-regulation",
              "ancestors": ["/business-and-industry"]}],
            [{"slug": "department-for-energy-security-and-net-zero"}])
        slugs = [a["slug"] for a in areas]
        assert slugs[0] == "energy-and-utilities"
        assert "business-and-industry" in slugs

    def test_agency_inherits_parent_department(self):
        areas = consultations.assign_policy_areas(
            "Something", "", [], [{"slug": "competition-and-markets-authority",
                                   "parents": ["department-for-business-innovation-science-and-trade"]}])
        assert [a["slug"] for a in areas] == ["business-and-industry"]

    def test_single_keyword_does_not_link(self):
        assert consultations.assign_policy_areas("A note on schools", "", [], []) == []
        childcare = consultations.assign_policy_areas(
            "Childcare and early years funding", "", [_taxon("/childcare-parenting/childcare", "Childcare",
                                                             "/childcare-parenting")], [])
        assert childcare[0]["slug"] == "children-and-families"
        assert "education" not in [a["slug"] for a in childcare]
        assert consultations.assign_policy_areas("Schools and pupils", "", [], [])[0]["slug"] == "education"

    def test_suppressed_taxon_and_limit(self):
        assert consultations.assign_policy_areas(
            "Equal treatment", "", [{"base_path": "/society-and-culture/equality-rights-and-citizenship",
                                     "ancestors": ["/society-and-culture"]}], []) == []
        many = consultations.assign_policy_areas(
            "Schools, hospitals, rail, police and housing", "", [], [])
        assert len(many) <= 3


class TestDeadlines:
    def test_uk_local_bst_and_gmt(self):
        assert consultations.uk_local(datetime(2026, 10, 22, 22, 59)) == datetime(2026, 10, 22, 23, 59)
        assert consultations.uk_local(datetime(2026, 12, 15, 23, 59)) == datetime(2026, 12, 15, 23, 59)
        # BST ends 01:00 UTC on the last Sunday of October (25 Oct 2026)
        assert consultations.uk_local(datetime(2026, 10, 25, 0, 59)).hour == 1
        assert consultations.uk_local(datetime(2026, 10, 25, 1, 0)).hour == 1

    def test_badges(self):
        c = consultations.Consultation(status="open", is_withdrawn=False,
                                       opening_date=datetime(2026, 10, 1),
                                       closing_date=datetime(2026, 10, 22, 22, 59))
        info = consultations.deadline_info(c, NOW)
        assert info["days_left"] == 2 and info["label"] == "2 days left" and info["urgent"]
        assert info["closing_human"] == "11:59pm on 22 October 2026"
        assert consultations.deadline_info(c, datetime(2026, 10, 22, 9, 0))["label"] == "Closes today"
        assert consultations.deadline_info(c, datetime(2026, 10, 21, 9, 0))["label"] == "Closes tomorrow"
        past = consultations.deadline_info(c, datetime(2026, 10, 23, 9, 0))
        assert not past["is_open"] and past["label"] == "Closed" and past["days_left"] is None


class TestParse:
    def test_open_consultation_fields(self):
        v = _values(SCHOOLS)
        assert v["title"] == "Free school meals: expanding eligibility"          # zero-width removed
        assert v["status"] == "open"
        assert v["closing_date"] == datetime(2026, 10, 22, 22, 59)               # naive UTC
        assert v["lead_org_slug"] == "department-for-education"
        assert v["response_url"] == "https://consult.education.gov.uk/fsm"
        assert v["response_email"] == "fsm@education.gov.uk"
        assert "Sanctuary Buildings" in v["response_postal"] and "<" not in v["response_postal"]
        assert [d["title"] for d in v["documents"]] == ["Consultation document", "Easy read"]
        assert v["documents"][1]["url"].startswith("https://www.gov.uk/government/consultations/")
        assert "• who qualifies" in v["body_text"]
        assert v["applies_to"] == ["England"]
        assert v["outcome_url"] is None and v["is_withdrawn"] is False

    def test_outcome_documents_split(self):
        v = _values(PARKING)
        assert v["status"] == "outcome"
        assert [d["title"] for d in v["documents"]] == ["Consultation"]
        assert [d["title"] for d in v["outcome_documents"]] == ["Government response"]
        assert v["outcome_url"].endswith("/outcome/response")
        assert v["outcome_summary"] == "The government response explains the new code."

    def test_withdrawn_and_slug(self):
        assert _values(APPLE)["is_withdrawn"] is True
        assert ing.slug_for("/government/consultations/Some_Thing--x/") == "some-thing--x"


# ── App / DB fixtures ─────────────────────────────────────────────────────────

def _seed_related(db_):
    from hansard_archive.models import (
        HaBill, HaBillTheme, HaPQ, HaPQTheme, HansardSession, HansardSessionTheme,
        THEME_TYPE_POLICY_AREA,
    )
    s = HansardSession(ext_id="s1", title="School Funding", date=date(2026, 10, 14), house="Commons",
                       slug="school-funding-1")
    old = HansardSession(ext_id="s2", title="Old schools debate", date=date(2024, 1, 10), house="Commons",
                         slug="old-schools-2")
    pq = HaPQ(uin="12345", heading="Free School Meals", question_text="To ask about meals.",
              tabled_date=date(2026, 10, 10), answering_body="Department for Education")
    bill = HaBill(parliament_bill_id=999, title="Children's Wellbeing and Schools Bill", house_of_origin="Commons",
                  session="2026-27", slug="childrens-wellbeing-and-schools-bill", parliament_url="https://bills",
                  current_stage="Report stage")
    db_.session.add_all([s, old, pq, bill])
    db_.session.flush()
    db_.session.add_all([
        HansardSessionTheme(session_id=s.id, theme="Education, training and skills",
                            theme_type=THEME_TYPE_POLICY_AREA),
        HansardSessionTheme(session_id=old.id, theme="Education, training and skills",
                            theme_type=THEME_TYPE_POLICY_AREA),
        HaPQTheme(pq_id=pq.id, theme="Children and families", theme_type=THEME_TYPE_POLICY_AREA),
        HaBillTheme(bill_id=bill.id, theme="Education, training and skills"),
    ])


@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db
    from feature_flags import feature_enabled
    import cache_models  # noqa: F401  (ha_bill_sponsor FK target)
    import hansard_archive.models  # noqa: F401

    old_flag = os.environ.get("FEATURE_CONSULTATIONS")
    os.environ["FEATURE_CONSULTATIONS"] = "live"

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
    test_app.register_blueprint(consultations.consultations_bp)

    with test_app.app_context():
        _db.create_all()
        for content in ALL:
            ing.upsert(_values(content), NOW)
        _seed_related(_db)
        _db.session.commit()

    with patch.object(consultations, "_utcnow", return_value=NOW):
        yield test_app

    with test_app.app_context():
        _db.drop_all()
    if old_flag is None:
        os.environ.pop("FEATURE_CONSULTATIONS", None)
    else:
        os.environ["FEATURE_CONSULTATIONS"] = old_flag


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


# ── Routes ────────────────────────────────────────────────────────────────────

class TestList:
    def test_default_is_open_closing_soonest_first(self, client):
        html = client.get("/consultations").get_data(as_text=True)
        assert _INDEX in html
        assert html.index("Free school meals") < html.index("Rural bus services")
        assert "Clean Heat" not in html and "Private parking" not in html
        assert "Apple" not in html                                     # withdrawn
        assert "2 days left" in html and "cons-days-urgent" in html
        assert "56 days left" in html
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/consultations">' in html
        assert '"@type": "CollectionPage"' in html

    def test_status_filters(self, client):
        closed = client.get("/consultations?status=closed").get_data(as_text=True)
        assert "Clean Heat" in closed and "Free school meals" not in closed and _NOINDEX in closed
        outcome = client.get("/consultations?status=outcome").get_data(as_text=True)
        assert "Private parking" in outcome and "Outcome published" in outcome
        everything = client.get("/consultations?status=all").get_data(as_text=True)
        assert "<strong>4</strong> consultations" in everything

    def test_department_and_area_filters(self, client):
        html = client.get("/consultations?dept=department-for-transport").get_data(as_text=True)
        assert "Rural bus services" in html and "Free school meals" not in html and _NOINDEX in html
        assert "Department for Education (1)" in html
        html = client.get("/consultations?area=education").get_data(as_text=True)
        assert "Free school meals" in html and "Rural bus services" not in html
        html = client.get("/consultations?area=not-a-real-area").get_data(as_text=True)
        assert "Rural bus services" in html                            # unknown area ignored

    def test_search(self, client):
        html = client.get("/consultations?q=rural").get_data(as_text=True)
        assert "Rural bus services" in html and "Free school meals" not in html and _NOINDEX in html
        html = client.get("/consultations?q=zzzz").get_data(as_text=True)
        assert "No consultations match" in html

    def test_pagination_bounds(self, client):
        assert client.get("/consultations?page=2").status_code == 404
        assert client.get("/consultations?page=51").status_code == 404

    def test_hidden_flag_is_404(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_CONSULTATIONS", "hidden")
        assert client.get("/consultations").status_code == 404
        assert client.get("/consultations/school-meals").status_code == 404


class TestDetail:
    def test_open_detail(self, client):
        r = client.get("/consultations/school-meals")
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and _INDEX in html
        assert "Free school meals: expanding eligibility" in html
        assert "Closes at 11:59pm on 22 October 2026" in html and "2 days left to respond" in html
        assert 'href="https://consult.education.gov.uk/fsm"' in html
        assert "mailto:fsm@education.gov.uk" in html and "Sanctuary Buildings" in html
        assert "Consultation document" in html and "PDF · 12 pages" in html
        assert "Applies to: England" in html
        assert 'href="https://www.gov.uk/government/consultations/school-meals"' in html
        assert "Open Government Licence v3.0" in html
        assert '"expires": "2026-10-22T22:59:00Z"' in html
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/consultations/school-meals">' in html

    def test_related_items_by_policy_area(self, client):
        html = client.get("/consultations/school-meals").get_data(as_text=True)
        assert 'href="/brief/education"' in html
        assert 'href="/archive/debate/14-october-2026/school-funding-1"' in html
        assert "Old schools debate" not in html                       # outside the 12-month window
        # "Children and families" has its own brief page now (owner decision 2026-10-20),
        # so a PQ tagged only with it is not related to an education consultation.
        assert 'href="/archive/pq/12345"' not in html
        assert 'href="/bill/childrens-wellbeing-and-schools-bill"' in html

    def test_closed_detail_has_no_how_to_respond(self, client):
        html = client.get("/consultations/clean-heat").get_data(as_text=True)
        assert "Closed 1 September 2026" in html and "How to respond" not in html

    def test_outcome_detail(self, client):
        html = client.get("/consultations/private-parking").get_data(as_text=True)
        assert "Outcome" in html and "Government response" in html
        assert "The government response explains the new code." in html

    def test_withdrawn_is_noindex(self, client):
        html = client.get("/consultations/apple-nfc").get_data(as_text=True)
        assert _NOINDEX in html and "withdrawn or removed" in html

    def test_case_redirect_and_404(self, client):
        r = client.get("/consultations/School-Meals")
        assert r.status_code == 301 and r.headers["Location"].endswith("/consultations/school-meals")
        assert client.get("/consultations/nope").status_code == 404


class TestSitemapAndHelper:
    def test_sitemap_excludes_withdrawn(self, app):
        with app.app_context():
            xml = consultations.build_sitemap_xml()
        assert "/consultations/school-meals" in xml and "/consultations/private-parking" in xml
        assert "apple-nfc" not in xml and "<lastmod>2026-10-20</lastmod>" in xml

    def test_open_consultations_helper(self, app):
        with app.app_context():
            items = consultations.open_consultations(now=NOW)
            assert [i["title"] for i in items] == ["Free school meals: expanding eligibility",
                                                   "Rural bus services"]
            assert items[0]["url"] == "https://westminsterbrief.co.uk/consultations/school-meals"
            assert items[0]["days_left"] == 2
            assert [i["title"] for i in consultations.open_consultations(["transport"], now=NOW)] == \
                ["Rural bus services"]
            assert [i["title"] for i in consultations.open_consultations(closing_within_days=7, now=NOW)] == \
                ["Free school meals: expanding eligibility"]
            grouped = consultations.open_consultations_by_area(["education", "economy"], now=NOW)
            assert len(grouped["education"]) == 1 and grouped["economy"] == []


# ── Ingest ────────────────────────────────────────────────────────────────────

@pytest.fixture
def ingest_app():
    """Fresh empty DB per ingest test."""
    from flask import Flask
    from extensions import db as _db
    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401

    a = Flask(__name__)
    a.config.update({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                     "SQLALCHEMY_TRACK_MODIFICATIONS": False})
    _db.init_app(a)
    with a.app_context():
        _db.create_all()
        yield a
        _db.session.remove()
        _db.drop_all()


class TestIngest:
    def _run(self, contents, gone=(), **kw):
        get, calls = _fake_govuk(contents, gone)
        with patch.object(ing.requests, "get", side_effect=get):
            summary = ing.ingest(api=ing.Api(sleep=_no_sleep), today=date(2026, 10, 20), **kw)
        return summary, calls

    def test_full_run_then_only_open_refreshed(self, ingest_app):
        s, calls = self._run(ALL)
        assert s["listed"] == 5 and s["inserted"] == 5 and s["failed"] == 0
        assert s["listed_open"] == 3 and s["open_by_department"][0] == ("Department for Education", 1)
        assert ing.Consultation.query.count() == 5
        assert ing.ConsultationTopic.query.count() >= 5
        assert calls["search"] == ["open_consultation", "closed_consultation", "consultation_outcome"]

        s, calls = self._run(ALL)
        assert s["planned_new"] == 0 and s["planned_skip"] == 2
        # open ones are always refreshed; the withdrawn one counts as changed
        assert s["planned_open_refresh"] == 2 and s["planned_changed"] == 1
        assert s["inserted"] == 0 and s["updated"] == 3
        assert ing.Consultation.query.count() == 5

    def test_unchanged_refresh_keeps_updated_at(self, ingest_app):
        content = ALL[0]
        first = datetime(2026, 10, 1, 9, 0)
        later = datetime(2026, 10, 20, 9, 0)
        ing.upsert(_values(content), first)
        ing.db.session.commit()
        c, created = ing.upsert(_values(content), later)
        ing.db.session.commit()
        assert not created and c.updated_at == first and c.last_refreshed == later
        changed = {**_values(content), "title": "A new title"}
        c, _ = ing.upsert(changed, later)
        ing.db.session.commit()
        assert c.updated_at == later

    def test_status_change_and_newer_timestamp_refetch(self, ingest_app):
        self._run(ALL)
        moved = {**SCHOOLS, "document_type": "closed_consultation",
                 "public_updated_at": "2026-10-23T09:00:00+01:00"}
        s, calls = self._run([moved, BUSES, HEAT, PARKING, APPLE])
        assert "/government/consultations/school-meals" in calls["content"]
        row = ing.Consultation.query.filter_by(slug="school-meals").one()
        assert row.status == "closed"

    def test_stale_open_gone_is_marked_withdrawn(self, ingest_app):
        self._run(ALL)
        s, _ = self._run([BUSES, HEAT, PARKING, APPLE],
                         gone=("/government/consultations/school-meals",))
        assert s["planned_stale_open"] == 1 and s["withdrawn"] == 1
        assert ing.Consultation.query.filter_by(slug="school-meals").one().is_withdrawn is True

    def test_dry_run_writes_nothing(self, ingest_app):
        s, calls = self._run(ALL, dry_run=True)
        assert s["planned_new"] == 5 and s["inserted"] == 0
        assert calls["content"] == [] and ing.Consultation.query.count() == 0

    def test_limit_caps_detail_fetches(self, ingest_app):
        s, calls = self._run(ALL, limit=2)
        assert len(calls["content"]) == 2 and s["inserted"] == 2

    def test_slug_clash_gets_suffix(self, ingest_app):
        twin = {**BUSES, "content_id": "other-id-123", "base_path": "/government/consultations/x/rural-buses"}
        self._run([BUSES, twin])
        slugs = sorted(c.slug for c in ing.Consultation.query.all())
        assert slugs == ["rural-buses", "rural-buses-other-id"]

    def test_retry_then_success(self):
        api = ing.Api(sleep=_no_sleep)
        responses = [requests.ConnectionError("boom"), _resp(None, 503), _resp({"ok": True})]
        with patch.object(ing.requests, "get", side_effect=responses):
            body, status = api.get("https://www.gov.uk/api/content/x")
        assert body == {"ok": True} and status == 200 and api.ok == 1 and api.failed == 0

    def test_all_requests_failing_is_reported(self, ingest_app):
        with patch.object(ing.requests, "get", return_value=_resp(None, 503)):
            s = ing.ingest(api=ing.Api(sleep=_no_sleep), today=date(2026, 10, 20))
        assert s["listed"] == 0 and not s["list_complete"]
        assert ing.mostly_failed(s)


class TestMigration:
    def test_upgrade_is_idempotent_on_sqlite(self):
        from sqlalchemy import create_engine, inspect, text
        sys.path.insert(0, str(PROJECT_ROOT))
        from migrations.consultation_tables import steps, upgrade

        engine = create_engine("sqlite:///:memory:")
        upgrade(engine)
        upgrade(engine)
        insp = inspect(engine)
        assert {"consultation", "consultation_topic"} <= set(insp.get_table_names())
        model_cols = {c.name for c in consultations.Consultation.__table__.columns}
        assert {c["name"] for c in insp.get_columns("consultation")} == model_cols
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO consultation (content_id, base_path, slug, title, status) "
                              "VALUES ('c1', '/p', 's', 'T', 'open')"))
            conn.execute(text("INSERT INTO consultation_topic (consultation_id, policy_area, source) "
                              "VALUES (1, 'education', 'taxon')"))
        pg = [label for label, _ in steps("postgresql")]
        assert "GIN index on consultation.search_tsv" in pg
        assert len(steps("sqlite")) == len(pg) - 2
