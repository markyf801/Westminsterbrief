"""
Tests for the Early Day Motions pages (/edms, /edms/<id>).

Parliament's API is never called: edms.requests.get is patched.

Run: python -m pytest tests/test_edms.py -v
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import edms

PROJECT_ROOT = Path(__file__).parents[1]


def _member(mnis_id, name, party="Labour", colour="d50000"):
    return {"MnisId": mnis_id, "Name": name, "Party": party,
            "Constituency": f"{name} Central", "PartyColour": colour}


def _edm(edm_id=101, title="Hospice funding", count=3, text=None):
    return {
        "Id": edm_id, "Status": 0, "UIN": 500 + edm_id, "UINWithAmendmentSuffix": str(500 + edm_id),
        "Title": title,
        "MotionText": text or "That this House recognises the vital work of hospices; "
                              "calls on the Government to provide sustainable NHS funding.",
        "PrimarySponsor": _member(1, "Alice Example"),
        "DateTabled": "2026-10-19T00:00:00", "SponsorsCount": count,
        "AmendmentToMotionId": None, "StatutoryInstrumentTitle": None,
    }


def _list_body(rows, total):
    return {"PagingInfo": {"Skip": 0, "Take": 20, "Total": total},
            "StatusCode": 200, "Success": True, "Errors": [], "Response": rows}


def _detail_body():
    raw = _edm()
    raw["SponsorsCount"] = 0  # the detail endpoint leaves this at 0
    raw["Sponsors"] = [
        {"MemberId": 1, "Member": _member(1, "Alice Example"), "SponsoringOrder": 1,
         "CreatedWhen": "2026-10-19T10:00:00", "IsWithdrawn": False},
        {"MemberId": 2, "Member": _member(2, "Bob Sample", "Liberal Democrat", "faa61a"),
         "SponsoringOrder": 2, "CreatedWhen": "2026-10-19T11:00:00", "IsWithdrawn": False},
        {"MemberId": 3, "Member": _member(3, "Carol Supporter", "Conservative", "0087dc"),
         "SponsoringOrder": 7, "CreatedWhen": "2026-10-20T09:30:00", "IsWithdrawn": False},
        {"MemberId": 4, "Member": _member(4, "Dan Withdrawn"), "SponsoringOrder": None,
         "CreatedWhen": "2026-10-20T09:00:00", "IsWithdrawn": True},
    ]
    raw["Amendments"] = []
    return {"PagingInfo": None, "StatusCode": 200, "Success": True, "Errors": [], "Response": raw}


def _response(body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    return resp


@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from feature_flags import feature_enabled

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    test_app.config.update({"TESTING": True, "SECRET_KEY": "test-secret", "LOGIN_DISABLED": True})
    lm = LoginManager()
    lm.init_app(test_app)
    lm.user_loader(lambda uid: None)

    @test_app.context_processor
    def inject_globals():
        return {"admin_authenticated": False, "app_version": "test"}

    test_app.jinja_env.globals["feature_enabled"] = feature_enabled
    test_app.register_blueprint(edms.edms_bp)
    return test_app


@pytest.fixture
def client(app):
    edms._cache.clear()
    yield app.test_client()
    edms._cache.clear()


class TestEdmList:
    def test_list_renders_items(self, client):
        rows = [_edm(101, "Hospice funding", 3), _edm(102, "Bus services in rural areas", 12)]
        with patch("edms.requests.get", return_value=_response(_list_body(rows, 2))) as get:
            r = client.get("/edms")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Hospice funding" in html and "Bus services in rural areas" in html
        assert 'href="/edms/101"' in html
        assert "Alice Example" in html and "Labour" in html
        assert "12 signatures" in html
        assert "19 October 2026" in html
        assert "Early Day Motions (EDMs) are motions" in html
        assert '<meta name="robots" content="index, follow">' in html
        assert '"@type": "CollectionPage"' in html
        params = get.call_args.kwargs["params"]
        assert params["parameters.orderBy"] == "DateTabledDesc"
        assert params["parameters.statuses"] == "Published"
        assert get.call_args.kwargs["timeout"] <= 8

    def test_list_is_cached(self, client):
        with patch("edms.requests.get", return_value=_response(_list_body([_edm()], 1))) as get:
            client.get("/edms")
            client.get("/edms")
        assert get.call_count == 1

    def test_search_passes_term_and_is_noindex(self, client):
        with patch("edms.requests.get", return_value=_response(_list_body([_edm()], 1))) as get:
            r = client.get("/edms?q=hospice")
        assert r.status_code == 200
        assert get.call_args.kwargs["params"]["parameters.searchTerm"] == "hospice"
        html = r.get_data(as_text=True)
        assert '<meta name="robots" content="noindex, follow">' in html
        assert "matching" in html

    def test_sort_by_signatures(self, client):
        with patch("edms.requests.get", return_value=_response(_list_body([_edm()], 1))) as get:
            r = client.get("/edms?sort=signatures")
        assert r.status_code == 200
        assert get.call_args.kwargs["params"]["parameters.orderBy"] == "SignatureCountDesc"

    def test_unknown_sort_falls_back_to_newest(self, client):
        with patch("edms.requests.get", return_value=_response(_list_body([_edm()], 1))) as get:
            client.get("/edms?sort=bogus")
        assert get.call_args.kwargs["params"]["parameters.orderBy"] == "DateTabledDesc"

    def test_page_past_end_is_404(self, client):
        with patch("edms.requests.get", return_value=_response(_list_body([], 25))):
            r = client.get("/edms?page=3")  # 25 results = 2 pages
        assert r.status_code == 404

    def test_page_past_cap_is_404_without_api_call(self, client):
        with patch("edms.requests.get") as get:
            r = client.get(f"/edms?page={edms._MAX_PAGES + 1}")
        assert r.status_code == 404
        get.assert_not_called()

    def test_api_timeout_shows_friendly_page_that_retries(self, client):
        with patch("edms.requests.get", side_effect=requests.Timeout("timed out")):
            r = client.get("/edms?q=carers")
        assert r.status_code == 503
        html = r.get_data(as_text=True)
        assert "taking a while to answer" in html
        assert 'http-equiv="refresh" content="10;url=/edms?q=carers&amp;retry=1"' in html
        assert "noindex" in html

    def test_timeout_stops_auto_retrying(self, client):
        with patch("edms.requests.get", side_effect=requests.Timeout("timed out")):
            r = client.get("/edms?q=carers&retry=2")
        assert r.status_code == 503
        html = r.get_data(as_text=True)
        assert 'http-equiv="refresh"' not in html
        assert 'href="/edms?q=carers"' in html

    def test_api_http_error_shows_friendly_page(self, client):
        with patch("edms.requests.get", return_value=_response({}, status=500)):
            r = client.get("/edms")
        assert r.status_code == 503
        html = r.get_data(as_text=True)
        assert "can’t reach Parliament’s Early Day Motions service" in html
        assert 'http-equiv="refresh"' not in html


class TestEdmDetail:
    def test_detail_renders_signatories(self, client):
        with patch("edms.requests.get", return_value=_response(_detail_body())):
            r = client.get("/edms/101")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Hospice funding" in html
        assert "vital work of hospices" in html
        assert "Who has signed (3)" in html          # withdrawn signature excluded
        assert "Bob Sample" in html and "Carol Supporter" in html
        assert "Dan Withdrawn" not in html
        assert "1 MP has since withdrawn" in html
        assert "Primary sponsor" in html
        assert "20 October 2026" in html
        assert "https://edm.parliament.uk/early-day-motion/101" in html
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/edms/101">' in html
        assert '<meta name="robots" content="index, follow">' in html
        assert '"@type": "Article"' in html
        assert 'href="/brief/health-and-social-care"' in html

    def test_detail_is_cached(self, client):
        with patch("edms.requests.get", return_value=_response(_detail_body())) as get:
            client.get("/edms/101")
            client.get("/edms/101")
        assert get.call_count == 1

    def test_missing_edm_is_404(self, client):
        body = {"PagingInfo": None, "StatusCode": 404, "Success": True, "Errors": [], "Response": None}
        with patch("edms.requests.get", return_value=_response(body)):
            r = client.get("/edms/999999")
        assert r.status_code == 404

    def test_api_failure_shows_friendly_page(self, client):
        with patch("edms.requests.get", side_effect=requests.ConnectionError("down")):
            r = client.get("/edms/101")
        assert r.status_code == 503
        assert "edm.parliament.uk" in r.get_data(as_text=True)


class TestHelpers:
    def test_related_topics_keyword_match(self):
        slugs = [t["slug"] for t in edms.related_topics("Rural bus services", "calls for more buses")]
        assert slugs[0] == "transport"

    def test_related_topics_none_for_unrelated_text(self):
        assert edms.related_topics("Congratulations to a local choir", "") == []

    def test_cache_is_bounded(self):
        edms._cache.clear()
        try:
            for i in range(edms._CACHE_MAX_ENTRIES + 10):
                edms._cache_set(("k", i), i)
            assert len(edms._cache) == edms._CACHE_MAX_ENTRIES
        finally:
            edms._cache.clear()
