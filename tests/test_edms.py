"""
Tests for Early Day Motions: DB-backed routes (/edms, /edms/<id>/<slug>,
sitemap), the ingest script (HTTP mocked) and the migration (SQLite).

Run: python -m pytest tests/test_edms.py -v
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

import ingest_edms  # noqa: E402


# ── Fake API payloads ─────────────────────────────────────────────────────────

def _member(mnis_id, name, party="Labour"):
    return {"MnisId": mnis_id, "Name": name, "Party": party, "Constituency": f"{name} Central"}


def _sponsor(mnis_id, name, order, signed, party="Labour", withdrawn=False):
    return {"MemberId": mnis_id, "Member": _member(mnis_id, name, party),
            "SponsoringOrder": None if withdrawn else order, "CreatedWhen": signed,
            "IsWithdrawn": withdrawn, "WithdrawnDate": "2026-10-20T12:00:00" if withdrawn else None}


def _detail(edm_id, title, tabled, sponsors, text="That this House notes the matter.",
            status=0, amendment_to=None, amendments=None):
    return {
        "Id": edm_id, "Status": status, "UIN": edm_id - 1000,
        "UINWithAmendmentSuffix": str(edm_id - 1000), "Title": title, "MotionText": text,
        "PrimarySponsor": sponsors[0]["Member"] if sponsors else None,
        "MemberId": sponsors[0]["MemberId"] if sponsors else None,
        "DateTabled": f"{tabled}T00:00:00", "StatusDate": f"{tabled}T18:00:00",
        "AmendmentToMotionId": amendment_to, "PrayingAgainstNegativeStatutoryInstrumentId": None,
        "SponsorsCount": 0, "Sponsors": sponsors, "Amendments": amendments or [],
    }


HOSPICE = _detail(
    2001, "Hospice funding", "2026-10-19",
    [_sponsor(1, "Alice Example", 1, "2026-10-19T10:00:00"),
     _sponsor(2, "Bob Sample", 2, "2026-10-19T11:00:00", party="Liberal Democrat"),
     _sponsor(3, "Carol Supporter", 7, "2026-10-20T09:30:00", party="Conservative"),
     _sponsor(4, "Dan Withdrawn", 8, "2026-10-20T09:00:00", withdrawn=True)],
    text="That this House recognises the vital work of hospices; calls for sustainable NHS funding.",
)
BUSES = _detail(
    2002, "Rural bus services", "2026-09-01",
    [_sponsor(5, "Erin Rural", 1, "2026-09-01T10:00:00", party="Conservative")]
    + [_sponsor(100 + i, f"MP {i}", i + 2, "2026-09-02T10:00:00") for i in range(10)],
    text="That this House calls for better rural buses.",
)
OLD = _detail(
    1500, "Anniversary of a local choir", "2025-01-10",
    [_sponsor(1, "Alice Example", 1, "2025-01-10T10:00:00")],
    text="That this House congratulates the choir.",
)
WITHDRAWN = _detail(
    2003, "Withdrawn motion", "2026-10-01",
    [_sponsor(6, "Fay Former", 1, "2026-10-01T10:00:00")], status=1,
)


def _summary_row(detail):
    """What /EarlyDayMotions/list returns for an EDM (no Sponsors; total incl. withdrawn)."""
    row = {k: v for k, v in detail.items() if k not in ("Sponsors", "Amendments")}
    row["SponsorsCount"] = len(detail["Sponsors"])
    return row


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def _fake_api(details):
    """requests.get stand-in serving list + detail endpoints from `details`."""
    calls = {"list": 0, "detail": []}

    def get(url, params=None, timeout=None):
        if url.endswith("/EarlyDayMotions/list"):
            calls["list"] += 1
            rows = [_summary_row(d) for d in details]
            skip, take = params["parameters.skip"], params["parameters.take"]
            return _resp({"PagingInfo": {"Skip": skip, "Take": take, "Total": len(rows)},
                          "StatusCode": 200, "Success": True, "Response": rows[skip:skip + take]})
        edm_id = int(url.rsplit("/", 1)[1])
        calls["detail"].append(edm_id)
        found = next((d for d in details if d["Id"] == edm_id), None)
        if found is None:
            return _resp({"StatusCode": 404, "Response": None})
        return _resp({"StatusCode": 200, "Success": True, "Response": found})

    return get, calls


# ── App / DB fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db
    from feature_flags import feature_enabled
    import cache_models  # noqa: F401  (ha_bill_sponsor FK target)
    import hansard_archive.models  # noqa: F401
    import edms

    old_flag = os.environ.get("FEATURE_EDMS")
    os.environ["FEATURE_EDMS"] = "live"

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    test_app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True, "SECRET_KEY": "test-secret", "LOGIN_DISABLED": True,
    })
    _db.init_app(test_app)
    lm = LoginManager()
    lm.init_app(test_app)
    lm.user_loader(lambda uid: None)

    @test_app.context_processor
    def inject_globals():
        return {"admin_authenticated": False, "app_version": "test"}

    test_app.jinja_env.globals["feature_enabled"] = feature_enabled
    test_app.register_blueprint(edms.edms_bp)

    with test_app.app_context():
        _db.create_all()
        now = datetime(2026, 10, 20, 12, 0)
        for d in (HOSPICE, BUSES, OLD, WITHDRAWN):
            ingest_edms.upsert_edm(d, now)
        _db.session.get(hansard_archive.models.HaEdm, 2001).related_topics = [
            {"slug": "hospice-care", "label": "Hospice care"}]
        _db.session.commit()

    yield test_app

    with test_app.app_context():
        _db.drop_all()
    if old_flag is None:
        os.environ.pop("FEATURE_EDMS", None)
    else:
        os.environ["FEATURE_EDMS"] = old_flag


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


# ── Routes ────────────────────────────────────────────────────────────────────

class TestEdmList:
    def test_list_newest_first_with_counts(self, client):
        r = client.get("/edms")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert html.index("Hospice funding") < html.index("Rural bus services") < html.index("local choir")
        assert 'href="/edms/2001/hospice-funding"' in html
        assert "3 signatures" in html           # withdrawn signature not counted
        assert "11 signatures" in html
        assert "Alice Example · Labour" in html
        assert "Tabled 19 October 2026" in html
        assert "Early Day Motions (EDMs) are motions" in html
        assert _INDEX in html
        assert '"@type": "CollectionPage"' in html

    def test_withdrawn_edm_not_listed(self, client):
        assert "Withdrawn motion" not in client.get("/edms").get_data(as_text=True)

    def test_search_matches_motion_text_and_is_noindex(self, client):
        r = client.get("/edms?q=hospices")
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "Hospice funding" in html and "Rural bus services" not in html
        assert _NOINDEX in html

    def test_search_no_results(self, client):
        r = client.get("/edms?q=zebra")
        assert r.status_code == 200
        assert "No Early Day Motions match" in r.get_data(as_text=True)

    def test_sort_by_signatures(self, client):
        html = client.get("/edms?sort=signatures").get_data(as_text=True)
        assert html.index("Rural bus services") < html.index("Hospice funding")
        assert _NOINDEX in html

    def test_member_filter(self, client):
        html = client.get("/edms?member=1").get_data(as_text=True)
        assert "signed by Alice Example" in html
        assert "Hospice funding" in html and "local choir" in html
        assert "Rural bus services" not in html

    def test_member_filter_excludes_withdrawn_signature(self, client):
        html = client.get("/edms?member=4").get_data(as_text=True)
        assert "Hospice funding" not in html

    def test_page_past_end_is_404(self, client):
        assert client.get("/edms?page=2").status_code == 404

    def test_page_past_cap_is_404(self, client):
        assert client.get("/edms?page=51").status_code == 404

    def test_hidden_flag_is_404(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_EDMS", "hidden")
        assert client.get("/edms").status_code == 404
        assert client.get("/edms/2001/hospice-funding").status_code == 404


class TestEdmDetail:
    def test_detail_renders_signatories(self, client):
        r = client.get("/edms/2001/hospice-funding")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "vital work of hospices" in html
        assert "Who has signed (3)" in html
        assert "Bob Sample" in html and "Carol Supporter" in html
        assert "Dan Withdrawn" not in html
        assert "1 MP has since withdrawn" in html
        assert "Primary sponsor" in html
        assert "20 October 2026" in html
        assert 'href="/edms?member=2"' in html
        assert "https://edm.parliament.uk/early-day-motion/2001" in html
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/edms/2001/hospice-funding">' in html
        assert _INDEX in html
        assert '"@type": "Article"' in html
        assert 'href="/brief/hospice-care"' in html               # specific topic (ingest)
        assert 'href="/brief/health-and-social-care"' in html     # keyword policy area

    def test_bare_id_redirects_to_slug(self, client):
        r = client.get("/edms/2001")
        assert r.status_code == 301
        assert r.headers["Location"].endswith("/edms/2001/hospice-funding")

    def test_wrong_slug_redirects(self, client):
        r = client.get("/edms/2001/old-title")
        assert r.status_code == 301
        assert r.headers["Location"].endswith("/edms/2001/hospice-funding")

    def test_missing_is_404(self, client):
        assert client.get("/edms/9999").status_code == 404
        assert client.get("/edms/9999/anything").status_code == 404

    def test_withdrawn_detail_is_noindex(self, client):
        html = client.get("/edms/2003/withdrawn-motion").get_data(as_text=True)
        assert _NOINDEX in html and "Withdrawn" in html


class TestSitemap:
    def test_sitemap_lists_published_by_id(self, app):
        import edms
        with app.app_context():
            xml = edms.build_sitemap_xml()
        assert xml.index("/edms/1500/") < xml.index("/edms/2001/hospice-funding")
        assert "/edms/2002/rural-bus-services" in xml
        assert "/edms/2003" not in xml
        assert "<lastmod>2026-10-20</lastmod>" in xml


# ── Ingest ────────────────────────────────────────────────────────────────────

@pytest.fixture
def ingest_app():
    """Fresh empty DB per ingest test."""
    from flask import Flask
    from extensions import db as _db
    import cache_models  # noqa: F401  (ha_bill_sponsor FK target)
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


def _no_sleep(_seconds):
    return None


class TestIngest:
    def test_upsert_counts_slug_session(self, ingest_app):
        from extensions import db
        from hansard_archive.models import HaEdm, HaEdmSponsor
        edm, created = ingest_edms.upsert_edm(HOSPICE, datetime(2026, 10, 20))
        assert created
        assert edm.signature_count == 3
        assert edm.slug == "hospice-funding"
        assert edm.session == "2026-27"
        assert edm.status == "Published"
        assert HaEdmSponsor.query.filter_by(edm_id=2001).count() == 4
        assert HaEdmSponsor.query.filter_by(edm_id=2001, is_withdrawn=True).count() == 1
        primary = HaEdmSponsor.query.filter_by(edm_id=2001, is_primary=True).one()
        assert primary.member_name == "Alice Example"
        assert HaEdmSponsor.query.filter_by(member_id=3).one().is_sponsor is False
        assert "Sponsors" not in db.session.get(HaEdm, 2001).raw_data

    def test_refresh_replaces_signatures(self, ingest_app):
        from extensions import db
        from hansard_archive.models import HaEdmSponsor
        ingest_edms.upsert_edm(HOSPICE, datetime(2026, 10, 20))
        db.session.commit()
        more = dict(HOSPICE, Sponsors=HOSPICE["Sponsors"] + [
            _sponsor(9, "New Signer", 9, "2026-10-21T09:00:00")])
        edm, created = ingest_edms.upsert_edm(more, datetime(2026, 10, 21))
        db.session.commit()
        assert not created
        assert edm.signature_count == 4
        assert HaEdmSponsor.query.filter_by(edm_id=2001).count() == 5

    def test_session_for_dates(self):
        assert ingest_edms.session_for(date(2026, 5, 13)) == "2026-27"
        assert ingest_edms.session_for(date(2026, 5, 12)) == "2024-26"
        assert ingest_edms.session_range("2024-26") == (date(2024, 7, 17), date(2026, 5, 12))

    def test_full_run_then_refresh_only_changed(self, ingest_app):
        from extensions import db
        from hansard_archive.models import HaEdm
        details = [OLD, BUSES, HOSPICE]
        get, calls = _fake_api(details)
        today = date(2026, 10, 21)
        with patch("ingest_edms.requests.get", side_effect=get):
            s1 = ingest_edms.ingest(date(2024, 7, 17), None, api=ingest_edms.Api(sleep=_no_sleep),
                                    today=today)
        assert s1["inserted"] == 3 and s1["failed"] == 0
        assert HaEdm.query.count() == 3
        assert sorted(calls["detail"]) == [1500, 2001, 2002]

        # Second run: OLD (outside 90 days, unchanged) is skipped; recent ones refreshed.
        calls["detail"].clear()
        with patch("ingest_edms.requests.get", side_effect=get):
            s2 = ingest_edms.ingest(date(2024, 7, 17), None, api=ingest_edms.Api(sleep=_no_sleep),
                                    today=today)
        assert s2["planned_skip"] == 1 and s2["updated"] == 2
        assert sorted(calls["detail"]) == [2001, 2002]

        # A new signature on OLD changes the listed total -> refetched.
        details[0] = dict(OLD, Sponsors=OLD["Sponsors"] + [
            _sponsor(7, "Late Signer", 2, "2026-10-21T08:00:00")])
        calls["detail"].clear()
        with patch("ingest_edms.requests.get", side_effect=get):
            s3 = ingest_edms.ingest(date(2024, 7, 17), None, refresh_days=0,
                                    api=ingest_edms.Api(sleep=_no_sleep), today=today)
        assert calls["detail"] == [1500]
        assert s3["planned_changed"] == 1
        assert db.session.get(HaEdm, 1500).signature_count == 2

    def test_dry_run_writes_nothing(self, ingest_app):
        from hansard_archive.models import HaEdm
        get, calls = _fake_api([HOSPICE, BUSES])
        with patch("ingest_edms.requests.get", side_effect=get):
            s = ingest_edms.ingest(date(2026, 5, 13), None, dry_run=True,
                                   api=ingest_edms.Api(sleep=_no_sleep))
        assert s["planned_new"] == 2
        assert calls["detail"] == []
        assert HaEdm.query.count() == 0

    def test_limit_caps_detail_fetches(self, ingest_app):
        get, calls = _fake_api([HOSPICE, BUSES, OLD])
        with patch("ingest_edms.requests.get", side_effect=get):
            ingest_edms.ingest(date(2024, 7, 17), None, limit=1, api=ingest_edms.Api(sleep=_no_sleep))
        assert len(calls["detail"]) == 1

    def test_retry_then_success(self):
        api = ingest_edms.Api(sleep=_no_sleep)
        ok = _resp({"Response": []})
        with patch("ingest_edms.requests.get", side_effect=[requests.Timeout("slow"), ok]):
            assert api.get("/EarlyDayMotions/list") == {"Response": []}
        assert api.ok == 1 and api.failed == 0

    def test_all_requests_failing_is_reported(self, ingest_app):
        api = ingest_edms.Api(sleep=_no_sleep)
        with patch("ingest_edms.requests.get", side_effect=requests.ConnectionError("down")):
            s = ingest_edms.ingest(date(2026, 5, 13), None, api=api)
        assert s["listed"] == 0 and not s["list_complete"]
        assert ingest_edms.mostly_failed(s)

    def test_match_topics_phrase_and_title_only_single_words(self):
        index = {
            "bus": [(("bus", "services"), "Bus services", "bus-services", 5)],
            "hospices": [(("hospices",), "Hospices", "hospices", 4)],
        }
        found = ingest_edms.match_topics("Rural transport", "calls for better bus services", index)
        assert found == [{"slug": "bus-services", "label": "Bus services"}]
        assert ingest_edms.match_topics("Funding", "support for hospices", index) == []
        assert ingest_edms.match_topics("Hospices", "", index)[0]["slug"] == "hospices"


class TestMigration:
    def test_upgrade_is_idempotent_on_sqlite(self):
        from sqlalchemy import create_engine, inspect, text
        sys.path.insert(0, str(PROJECT_ROOT))
        from migrations.edm_tables import steps, upgrade

        engine = create_engine("sqlite:///:memory:")
        upgrade(engine)
        upgrade(engine)
        insp = inspect(engine)
        assert {"ha_edm", "ha_edm_sponsor"} <= set(insp.get_table_names())
        cols = {c["name"] for c in insp.get_columns("ha_edm")}
        assert {"id", "uin", "signature_count", "slug", "raw_data", "updated_at"} <= cols
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO ha_edm (id, uin, title) VALUES (1, '1', 'T')"))
            conn.execute(text("INSERT INTO ha_edm_sponsor (edm_id, member_name) VALUES (1, 'A')"))
        pg = [label for label, _ in steps("postgresql")]
        assert "GIN index on ha_edm.search_tsv" in pg
        assert len(steps("sqlite")) == len(pg) - 2
