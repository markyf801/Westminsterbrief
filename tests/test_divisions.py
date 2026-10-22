"""
Tests for divisions (recorded votes): the ingest script (HTTP mocked), the
DB-backed routes (/divisions, /divisions/<house>/<id>), the member-page votes
section, the sitemap and the migration (SQLite).

Run: python -m pytest tests/test_divisions.py -v
"""
import os
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import ingest_divisions as ing  # noqa: E402


# ── Fake API payloads ─────────────────────────────────────────────────────────

def _c_member(mid, name, party="Labour", seat=None, proxy=None, list_as=None):
    return {"MemberId": mid, "Name": name, "Party": party, "MemberFrom": seat or f"{name} North",
            "ListAs": list_as or name, "ProxyName": proxy}


def _l_member(mid, name, party="Crossbench"):
    return {"memberId": mid, "name": name, "listAs": name, "memberFrom": "Life peer", "party": party}


COMMONS_DETAIL = {
    "DivisionId": 2462, "Date": "2026-10-21T19:20:00", "PublicationUpdated": "2026-10-21T19:35:24",
    "Number": 104, "IsDeferred": False, "Title": "Opposition day motion:  Support for British farmers",
    "AyeCount": 3, "NoCount": 2,
    "AyeTellers": [_c_member(10, "Tess Teller", "Liberal Democrat")],
    "NoTellers": [_c_member(11, "Ned Teller")],
    "Ayes": [_c_member(1, "Alice Aye", "Liberal Democrat", list_as="Aye, Alice"),
             _c_member(2, "Bob Both", "Independent"),
             _c_member(3, "Carol Proxy", "Conservative", proxy="Dan Proxyholder")],
    "Noes": [_c_member(4, "Zed No", list_as="No, Zed"), _c_member(2, "Bob Both", "Independent")],
    "NoVoteRecorded": [_c_member(99, "Absent Member")],
}
COMMONS_DEFERRED = {
    "DivisionId": 2461, "Date": "2026-10-21T00:00:00", "PublicationUpdated": "2026-10-21T15:51:54",
    "Number": 103, "IsDeferred": True, "Title": "Railways Regulations 2026",
    "AyeCount": 1, "NoCount": 0, "AyeTellers": None, "NoTellers": None,
    "Ayes": [_c_member(1, "Alice Aye", "Liberal Democrat")], "Noes": [], "NoVoteRecorded": [],
}
LORDS_ROW = {
    "divisionId": 3728, "date": "2026-10-20T00:00:00", "number": 3, "notes": None,
    "title": "Armed Forces Bill", "authoritativeContentCount": 2, "authoritativeNotContentCount": 1,
    "amendmentMotionNotes": "<p>The Earl of Minto moved amendment 56, after clause 51, to insert the new "
                            "clause <em>Overseas operations</em>. The House divided:</p>",
    "contentTellers": [_l_member(500, "Baroness Teller of Lobby", "Conservative")],
    "notContentTellers": [],
    "contents": [_l_member(500, "Baroness Teller of Lobby", "Conservative"), _l_member(501, "Lord Content")],
    "notContents": [_l_member(502, "Lord Notcontent", "Labour")],
}

HANSARD = {
    "Commons": [
        {"Date": "2026-10-21T19:20:00", "Number": "104", "House": "Commons", "IsCommitteeDivision": False,
         "DebateSection": "Support for Farmers", "DebateSectionExtId": "AAAA-FARMERS",
         "ExternalId": "DIV-104", "TextBeforeVote": "<em><em>The House divided:</em></em>",
         "TextAfterVote": "<em>Question accordingly negatived.</em>"},
        {"Date": "2026-10-21T00:00:00", "Number": "103", "House": "Commons", "IsCommitteeDivision": False,
         "DebateSection": "Railways", "DebateSectionExtId": "DIV-103", "ExternalId": "DIV-103",
         "TextBeforeVote": "That the draft Railways Regulations 2026 be approved.<br/><br/>"
                           "<em><em>The House divided:</em></em>",
         "TextAfterVote": "<em>Question accordingly agreed to.</em>"},
        {"Date": "2026-10-21T10:00:00", "Number": "104", "House": "Commons", "IsCommitteeDivision": True,
         "DebateSection": "Some Bill (Second sitting)", "DebateSectionExtId": "PBC", "ExternalId": "PBC-1"},
    ],
    "Lords": [
        {"Date": "2026-10-20T20:10:00", "Number": "3", "House": "Lords", "IsCommitteeDivision": False,
         "DebateSection": "Armed Forces Bill", "DebateSectionExtId": "lords-not-held",
         "ExternalId": "LDIV-3", "TextBeforeVote": "", "TextAfterVote": ""},
    ],
}


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class FakeParliament:
    """requests.get stand-in for the three APIs."""

    def __init__(self, commons=(COMMONS_DETAIL, COMMONS_DEFERRED), lords=(LORDS_ROW,), hansard=None,
                 fail=None):
        self.commons = list(commons)
        self.lords = list(lords)
        self.hansard = hansard if hansard is not None else HANSARD
        self.fail = fail or {}          # url substring -> status code
        self.calls: list[str] = []

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        for part, status in self.fail.items():
            if part in url:
                return _resp(None, status)
        params = params or {}
        if url.endswith("/data/divisions.json/search"):
            skip, take = params["queryParameters.skip"], params["queryParameters.take"]
            rows = [{k: ([] if k in ("Ayes", "Noes", "NoVoteRecorded") else v) for k, v in d.items()}
                    for d in self.commons]
            return _resp(rows[skip:skip + take])
        if "/data/division/" in url:
            pid = int(url.rsplit("/", 1)[1].split(".")[0])
            found = next((d for d in self.commons if d["DivisionId"] == pid), None)
            return _resp(found) if found else _resp(None, 404)
        if url.endswith("/data/Divisions/search"):
            return _resp(self.lords[params["skip"]:params["skip"] + params["take"]])
        if url.endswith("/search/divisions.json"):
            rows = self.hansard.get(params["queryParameters.house"], [])
            skip, take = params["queryParameters.skip"], params["queryParameters.take"]
            return _resp({"Results": rows[skip:skip + take], "TotalResultCount": len(rows)})
        return _resp(None, 404)

    def count(self, part: str) -> int:
        return sum(part in c for c in self.calls)


START, END = date(2026, 10, 8), date(2026, 10, 22)


def _run(fake, **kw):
    with patch.object(ing.requests, "get", fake):
        return ing.ingest(START, END, api=ing.Api(sleep=lambda s: None), **kw)


# ── App / DB fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db
    from feature_flags import feature_enabled
    import cache_models  # noqa: F401  (ha_bill_sponsor FK target)
    import hansard_archive.models as ham
    import divisions
    import divisions_models  # noqa: F401

    old_flag = os.environ.get("FEATURE_DIVISIONS")
    os.environ["FEATURE_DIVISIONS"] = "live"

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
    test_app.register_blueprint(divisions.divisions_bp)

    with test_app.app_context():
        _db.create_all()
        farmers = ham.HansardSession(ext_id="AAAA-FARMERS", title="Support for Farmers", date=date(2026, 10, 21),
                                     house="Commons", debate_type="debate", slug="support-for-farmers-1",
                                     hansard_url="https://hansard.parliament.uk/Commons/2026-10-21/debates/AAAA-FARMERS/x")
        _db.session.add(farmers)
        _db.session.flush()
        _db.session.add(ham.HansardSessionTheme(session_id=farmers.id, theme="Environment",
                                                theme_type=ham.THEME_TYPE_POLICY_AREA))
        # Same date and title as the Lords debate, but the Hansard id differs: the
        # unique-title fallback should link it.
        # A debate with the same heading as the deferred division's Hansard entry:
        # must not be linked to it.
        _db.session.add(ham.HansardSession(ext_id="RAIL-QS", title="Railways", date=date(2026, 10, 21),
                                           house="Commons", debate_type="oral_questions", slug="railways-3"))
        _db.session.add(ham.HansardSession(ext_id="LORDS-REAL-ID", title="Armed Forces Bill",
                                           date=date(2026, 10, 20), house="Lords", debate_type="debate",
                                           slug="armed-forces-bill-2"))
        _db.session.commit()
        summary = _run(FakeParliament())
        assert summary["created"] == 3, summary

    yield test_app

    with test_app.app_context():
        _db.drop_all()
    if old_flag is None:
        os.environ.pop("FEATURE_DIVISIONS", None)
    else:
        os.environ["FEATURE_DIVISIONS"] = old_flag


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


def _div(house, pid):
    from divisions_models import HaDivision
    return HaDivision.query.filter_by(house=house, parliament_division_id=pid).one()


_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


# ── Ingest ────────────────────────────────────────────────────────────────────

class TestIngest:
    def test_commons_division_and_votes(self, ctx):
        from divisions_models import HaDivisionVote
        d = _div("Commons", 2462)
        assert d.title == "Opposition day motion: Support for British farmers"   # whitespace collapsed
        assert (d.number, d.date, d.ayes_count, d.noes_count) == (104, date(2026, 10, 21), 3, 2)
        assert d.official_url == "https://votes.parliament.uk/Votes/Commons/Division/2462"
        assert d.source_updated == datetime(2026, 10, 21, 19, 35, 24)
        assert "Ayes" not in d.raw_data and d.raw_data["DivisionId"] == 2462
        votes = {(v.member_id, v.vote): v for v in HaDivisionVote.query.filter_by(division_id=d.id)}
        # 3 ayes + 2 noes + 2 tellers; Bob voted in both lobbies; no-vote list not stored
        assert len(votes) == 7 and d.vote_rows == 7
        assert (2, "aye") in votes and (2, "no") in votes
        assert votes[(10, "aye")].is_teller and votes[(11, "no")].is_teller
        assert not votes[(1, "aye")].is_teller
        assert votes[(3, "aye")].proxy_name == "Dan Proxyholder"
        assert votes[(1, "aye")].list_as == "Aye, Alice"
        assert all(m != 99 for m, _ in votes)

    def test_lords_division_tellers_merged(self, ctx):
        from divisions_models import HaDivisionVote
        d = _div("Lords", 3728)
        assert (d.ayes_count, d.noes_count, d.number) == (2, 1, 3)
        assert d.motion_text == ("The Earl of Minto moved amendment 56, after clause 51, to insert the new "
                                 "clause Overseas operations.")
        votes = HaDivisionVote.query.filter_by(division_id=d.id).all()
        assert len(votes) == 3                                  # teller listed in contents too: one row
        teller = next(v for v in votes if v.member_id == 500)
        assert teller.is_teller and teller.vote == "aye" and teller.member_from == "Life peer"

    def test_hansard_link_by_ext_id_and_title_fallback(self, ctx):
        from hansard_archive.models import HansardSession
        farmers = _div("Commons", 2462)
        assert farmers.hansard_debate_ext_id == "AAAA-FARMERS"
        assert farmers.outcome_text == "Question accordingly negatived."
        assert farmers.motion_text is None                        # only "The House divided:" boilerplate
        assert farmers.ha_session_id == HansardSession.query.filter_by(ext_id="AAAA-FARMERS").one().id
        deferred = _div("Commons", 2461)
        assert deferred.is_deferred
        assert deferred.motion_text == "That the draft Railways Regulations 2026 be approved."
        assert deferred.ha_session_id is None                     # own Hansard section, not a debate
        assert deferred.hansard_debate_ext_id is None and deferred.hansard_division_ext_id == "DIV-103"
        lords = _div("Lords", 3728)
        assert lords.ha_session_id == HansardSession.query.filter_by(ext_id="LORDS-REAL-ID").one().id

    def test_committee_division_with_same_number_ignored(self, ctx):
        assert _div("Commons", 2462).hansard_division_ext_id == "DIV-104"

    def test_rerun_is_idempotent_and_skips_detail_fetches(self, ctx):
        from divisions_models import HaDivision, HaDivisionVote
        before = (HaDivision.query.count(), HaDivisionVote.query.count())
        fake = FakeParliament()
        s = _run(fake)
        assert fake.count("/data/division/") == 0
        assert (s["created"], s["updated"], s["unchanged"]) == (0, 0, 3)
        assert (HaDivision.query.count(), HaDivisionVote.query.count()) == before
        assert not ing.mostly_failed(s)

    def test_publication_updated_triggers_refetch(self, ctx):
        from divisions_models import HaDivisionVote
        changed = {**COMMONS_DETAIL, "PublicationUpdated": "2026-10-22T09:00:00",
                   "AyeCount": 2, "Ayes": COMMONS_DETAIL["Ayes"][:2]}
        fake = FakeParliament(commons=(changed, COMMONS_DEFERRED))
        try:
            s = _run(fake)
            assert fake.calls.count(f"{ing.COMMONS_API}/data/division/2462.json") == 1
            assert s["updated"] == 1
            d = _div("Commons", 2462)
            assert d.ayes_count == 2 and d.vote_rows == 6
            assert HaDivisionVote.query.filter_by(division_id=d.id, member_id=3).count() == 0
        finally:
            _run(FakeParliament())                                # restore for later tests
        assert _div("Commons", 2462).vote_rows == 7

    def test_dry_run_writes_nothing(self, ctx):
        from divisions_models import HaDivision
        new = {**COMMONS_DEFERRED, "DivisionId": 2470, "Number": 105}
        fake = FakeParliament(commons=(COMMONS_DETAIL, COMMONS_DEFERRED, new))
        s = _run(fake, dry_run=True)
        assert s["planned_new"] == 1 and s["planned_unchanged"] == 3
        assert fake.count("/data/division/") == 0 and fake.count("hansard-api") == 0
        assert HaDivision.query.filter_by(parliament_division_id=2470).count() == 0

    def test_429_stops_the_run(self, ctx):
        new = {**COMMONS_DEFERRED, "DivisionId": 2471, "Number": 106}
        fake = FakeParliament(commons=(new,), fail={"/data/division/": 429})
        s = _run(fake)
        assert s["rate_limited"] and ing.mostly_failed(s)
        assert fake.count("/data/division/") == 1                 # no retry on 429
        assert fake.count("lordsvotes") == 0                      # run stopped

    def test_server_errors_retried_then_counted(self, ctx):
        fake = FakeParliament(fail={"commonsvotes": 503, "lordsvotes": 503, "hansard-api": 503})
        s = _run(fake)
        assert fake.count("commonsvotes") == ing.MAX_ATTEMPTS
        assert not s["list_complete"] and ing.mostly_failed(s)

    def test_permanent_error_not_retried(self, ctx):
        fake = FakeParliament(fail={"lordsvotes": 400})
        _run(fake, houses=("Lords",))
        assert fake.count("lordsvotes") == 1

    def test_window(self):
        assert ing.resolve_window(14, None, date(2026, 10, 22)) == (date(2026, 10, 8), date(2026, 10, 22))
        assert ing.resolve_window(14, date(2024, 7, 4), date(2026, 10, 22))[0] == date(2024, 7, 4)

    def test_plain_text(self):
        assert ing.plain_text("That the Bill be read.<br/><br/><em><em>The House divided:</em></em>") \
            == "That the Bill be read."
        assert ing.plain_text("<p>A &amp; B</p>") == "A & B"
        assert ing.plain_text(None) == ""


# ── Routes ────────────────────────────────────────────────────────────────────

class TestDivisionList:
    def test_list_newest_first(self, client):
        r = client.get("/divisions")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert html.index("Support for British farmers") < html.index("Armed Forces Bill")
        assert 'href="/divisions/commons/2462"' in html and 'href="/divisions/lords/3728"' in html
        assert "Ayes 3, Noes 2: majority of 1 for the Ayes" in html
        assert "Contents 2, Not contents 1: majority of 1 for the Contents" in html
        assert "Deferred division" in html
        assert 'href="/brief/agriculture-environment-and-rural-affairs"' in html
        assert "Open Parliament Licence" in html
        assert _INDEX in html and '"@type": "CollectionPage"' in html

    def test_house_filter(self, client):
        html = client.get("/divisions?house=lords").get_data(as_text=True)
        assert "Armed Forces Bill" in html and "British farmers" not in html
        assert _NOINDEX in html

    def test_date_filter(self, client):
        html = client.get("/divisions?from=2026-10-21&to=2026-10-21").get_data(as_text=True)
        assert "British farmers" in html and "Armed Forces Bill" not in html

    def test_bad_date_ignored(self, client):
        assert client.get("/divisions?from=notadate").status_code == 200

    def test_title_search(self, client):
        html = client.get("/divisions?q=railways").get_data(as_text=True)
        assert "Railways Regulations" in html and "British farmers" not in html
        html = client.get("/divisions?q=zebra").get_data(as_text=True)
        assert "No divisions match" in html

    def test_policy_area_filter(self, client):
        html = client.get("/divisions?area=agriculture-environment-and-rural-affairs").get_data(as_text=True)
        assert "British farmers" in html and "Armed Forces Bill" not in html

    def test_member_filter_shows_their_vote(self, client):
        html = client.get("/divisions?member=2").get_data(as_text=True)
        assert "Votes by Bob Both" in html
        assert "British farmers" in html and "Railways" not in html
        html = client.get("/divisions?member=10").get_data(as_text=True)
        assert "Voted Aye (teller)" in html
        html = client.get("/divisions?member=502").get_data(as_text=True)
        assert "Voted Not content" in html

    def test_page_past_end_is_404(self, client):
        assert client.get("/divisions?page=2").status_code == 404
        assert client.get("/divisions?page=51").status_code == 404

    def test_hidden_flag_is_404(self, client, monkeypatch):
        monkeypatch.setenv("FEATURE_DIVISIONS", "hidden")
        assert client.get("/divisions").status_code == 404
        assert client.get("/divisions/commons/2462").status_code == 404


class TestDivisionDetail:
    def test_commons_detail(self, client):
        r = client.get("/divisions/commons/2462")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Opposition day motion: Support for British farmers" in html
        assert "Hansard records: “Question accordingly negatived.”" in html
        assert "Ayes (4 listed)" in html and "Noes (3 listed)" in html   # incl. tellers
        assert "Liberal Democrat (2)" in html
        assert "(teller)" in html
        assert "proxy vote cast by Dan Proxyholder" in html
        assert 'href="/divisions?member=4"' in html
        assert 'href="/archive/debate/21-october-2026/support-for-farmers-1"' in html
        assert "https://votes.parliament.uk/Votes/Commons/Division/2462" in html
        assert "do not include the tellers" in html
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/divisions/commons/2462">' in html
        assert _INDEX in html and '"@type": "WebPage"' in html
        assert html.index("Alice Aye") < html.index("Tess Teller")    # Liberal Democrats sorted by list name

    def test_lords_detail(self, client):
        html = client.get("/divisions/lords/3728").get_data(as_text=True)
        assert "Contents (2 listed)" in html and "Not contents (1 listed)" in html
        assert "The Earl of Minto moved amendment 56" in html
        assert "include the tellers" in html
        assert 'href="/archive/debate/20-october-2026/armed-forces-bill-2"' in html

    def test_deferred_not_in_archive(self, client):
        html = client.get("/divisions/commons/2461").get_data(as_text=True)
        assert "The debate" not in html and "railways-3" not in html
        assert "ballot paper" in html

    def test_house_case_redirect_and_404s(self, client):
        r = client.get("/divisions/Commons/2462")
        assert r.status_code == 301 and r.headers["Location"].endswith("/divisions/commons/2462")
        assert client.get("/divisions/lords/2462").status_code == 404
        assert client.get("/divisions/senate/2462").status_code == 404
        assert client.get("/divisions/commons/9999").status_code == 404


class TestMemberVotes:
    def test_member_votes_global(self, app):
        import divisions
        with app.test_request_context("/members/1/alice"):
            mv = divisions.member_votes(1)
        assert mv["total"] == 2 and mv["all_url"] == "/divisions?member=1"
        assert [r["path"] for r in mv["rows"]] == ["/divisions/commons/2462", "/divisions/commons/2461"]
        assert mv["rows"][0]["vote"] == "Aye"

    def test_partial_renders(self, app):
        from flask import render_template_string
        tpl = ('{% if divisions_member_votes is defined %}{% set member_votes = divisions_member_votes(1) %}'
               '{% if member_votes is not none %}{% include "divisions/_member_votes.html" %}{% endif %}{% endif %}')
        with app.test_request_context("/members/1/alice"):
            html = render_template_string(tpl)
        assert 'href="/divisions/commons/2462"' in html
        assert "All 2 recorded votes by this member" in html

    def test_hidden_flag_returns_none(self, app, monkeypatch):
        import divisions
        monkeypatch.setenv("FEATURE_DIVISIONS", "hidden")
        with app.test_request_context("/members/1/alice"):
            assert divisions.member_votes(1) is None

    def test_member_profile_template_without_blueprint(self):
        """member_profile.html must not break where the divisions blueprint isn't registered."""
        src = (PROJECT_ROOT / "templates" / "member_profile.html").read_text(encoding="utf-8")
        assert "{% if divisions_member_votes is defined %}" in src


class TestSitemap:
    def test_sitemap_lists_every_division(self, ctx):
        import divisions
        xml = divisions.build_sitemap_xml()
        assert "<loc>https://westminsterbrief.co.uk/divisions/commons/2462</loc>" in xml
        assert "<loc>https://westminsterbrief.co.uk/divisions/lords/3728</loc>" in xml
        assert xml.count("<url>") == 3


# ── Migration ─────────────────────────────────────────────────────────────────

class TestMigration:
    def test_upgrade_creates_tables_and_is_idempotent(self, tmp_path):
        sys.path.insert(0, str(PROJECT_ROOT / "migrations"))
        import division_tables
        engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
        division_tables.upgrade(engine)
        division_tables.upgrade(engine)
        insp = inspect(engine)
        assert {"ha_division", "ha_division_vote"} <= set(insp.get_table_names())
        cols = {c["name"] for c in insp.get_columns("ha_division_vote")}
        assert {"division_id", "member_id", "vote", "member_name", "list_as", "party",
                "member_from", "is_teller", "proxy_name"} == cols
        assert "idx_ha_division_vote_member" in {i["name"] for i in insp.get_indexes("ha_division_vote")}
        engine.dispose()

    def test_migration_matches_model_columns(self):
        sys.path.insert(0, str(PROJECT_ROOT / "migrations"))
        import division_tables
        from divisions_models import HaDivision, HaDivisionVote
        sql = " ".join(s for _, s in division_tables.steps("postgresql"))
        for model in (HaDivision, HaDivisionVote):
            for col in model.__table__.columns:
                assert f" {col.name} " in sql, col.name
