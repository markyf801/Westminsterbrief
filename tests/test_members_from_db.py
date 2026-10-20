"""
Members served from our own database (member_profile / member_biography).

- scripts/refresh_members.py upserts profiles idempotently from mocked Parliament responses
- biographies regenerate only when their inputs change (Gemini mocked)
- /members, /mp_search, /biography, the autocomplete and the Word export render from the
  DB with no outbound HTTP
- routes still work before the migration has created the new tables

Run: python -m pytest tests/test_members_from_db.py -v
"""

import io
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import refresh_members  # noqa: E402
import member_profiles  # noqa: E402


# ── Fake Parliament / Wikimedia ───────────────────────────────────────────────

def _member(mid, name, party, house, seat):
    return {
        "id": mid, "nameDisplayAs": name, "nameListAs": name, "nameFullTitle": name,
        "latestParty": {"name": party, "abbreviation": party[:3], "backgroundColour": "d50000"},
        "latestHouseMembership": {
            "membershipFrom": seat, "house": 2 if house == "Lords" else 1,
            "membershipStartDate": "2010-05-06T00:00:00", "membershipEndDate": None,
            "membershipStatus": {"statusIsActive": True, "statusDescription": "Current Member"},
        },
        "thumbnailUrl": f"https://members-api.parliament.uk/api/Members/{mid}/Thumbnail",
    }


class FakeParliament:
    """Routes refresh_members._get_json calls to canned data; records every URL."""

    def __init__(self):
        self.members = {
            4046: _member(4046, "Bridget Phillipson", "Labour", "Commons", "Houghton and Sunderland South"),
            269: _member(269, "Baroness Smith of Malvern", "Labour", "Lords", "Life peer"),
        }
        self.bio = {
            4046: {"governmentPosts": [{"name": "Secretary of State for Education",
                                        "startDate": "2024-07-05T00:00:00", "endDate": None}],
                   "oppositionPosts": [], "otherPosts": [],
                   "committeeMemberships": [{"name": "Public Accounts Committee",
                                             "startDate": "2017-09-11T00:00:00",
                                             "endDate": "2019-11-06T00:00:00"}]},
            269: {"governmentPosts": [{"name": "Minister of State (Department for Work and Pensions)",
                                       "startDate": "2025-09-06T00:00:00", "endDate": None}],
                  "oppositionPosts": [], "otherPosts": [], "committeeMemberships": []},
        }
        self.interests = {4046: [{"name": "Category 1", "interests": [{"interest": "Author royalties"}]}],
                          269: []}
        self.synopsis = {
            4046: "The Rt Hon Bridget Phillipson is the Labour MP for Houghton and Sunderland South.",
            269: "Baroness Smith of Malvern's full title is The Rt Hon. the Baroness Smith of Malvern. "
                 "Her name is Jacqui Smith, and she is a current member of the House of Lords.",
        }
        self.wikidata = {
            4046: {"status": "matched", "qid": "Q575337", "name": "Bridget Phillipson", "given": "Bridget",
                   "family": "Phillipson", "article": "https://en.wikipedia.org/wiki/Bridget_Phillipson",
                   "education": ["Hertford College"], "occupations": ["politician"]},
            269: {"status": "matched", "qid": "Q237217", "name": "Jacqui Smith", "given": "Jacqui",
                  "family": "Smith", "article": "https://en.wikipedia.org/wiki/Jacqui_Smith",
                  "education": ["Hertford College"], "occupations": ["politician", "teacher"]},
        }
        self.wiki_text = "Wikipedia text."
        self.fail_bio_for = set()
        self.fail_synopsis = False
        self.calls = []
        self.wikidata_calls = []

    def fetch_wikidata(self, member_ids):
        self.wikidata_calls.append(list(member_ids))
        return {m: dict(self.wikidata.get(m) or {"status": "not_found"}) for m in member_ids}

    def fetch_extract(self, url):
        return f"{self.wiki_text} ({url})", True

    def __call__(self, url, params=None, delay=0):
        self.calls.append(url)
        base = refresh_members.MEMBERS_API
        if url == f"{base}/Search":
            items = [{"value": v} for v in self.members.values()]
            skip = params.get("skip", 0)
            return {"totalResults": len(items), "items": items[skip:skip + 20]}
        mid = int(url[len(base) + 1:].split("/")[0])
        if url.endswith("/Biography"):
            return None if mid in self.fail_bio_for else {"value": self.bio[mid]}
        if url.endswith("/Contact"):
            return {"value": [{"type": "Parliamentary office", "line1": "House of Commons",
                               "postcode": "SW1A 0AA", "phone": "020 7219 0000",
                               "email": f"m{mid}@parliament.uk"}]}
        if url.endswith("/RegisteredInterests"):
            return {"value": self.interests[mid]}
        if url.endswith("/Synopsis"):
            return None if self.fail_synopsis else {"value": self.synopsis[mid]}
        return {"value": self.members[mid]}


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly on any real HTTP request; record attempted URLs."""
    attempted = []

    def blocked(self, method, url, *args, **kwargs):
        attempted.append(url)
        raise requests.ConnectionError(f"network blocked in tests: {url}")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    return attempted


# ── App fixture ───────────────────────────────────────────────────────────────

def _make_app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db

    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    import cache_models  # noqa: F401
    from biography import biography_bp
    from mp_search import mp_search_bp
    from hansard_archive.views import archive_bp, brief_bp
    from hansard_archive.slugs import slugify_theme
    from feature_flags import feature_enabled

    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"),
                static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", TESTING=True,
                      SECRET_KEY="test", LOGIN_DISABLED=True, RATELIMIT_ENABLED=False)
    db.init_app(app)
    lm = LoginManager()
    lm.init_app(app)
    lm.user_loader(lambda uid: None)
    app.context_processor(lambda: {"admin_authenticated": False, "app_version": "test"})
    app.jinja_env.filters["slugify"] = slugify_theme
    app.jinja_env.globals["feature_enabled"] = feature_enabled
    for bp in (biography_bp, mp_search_bp, archive_bp, brief_bp):
        app.register_blueprint(bp)
    return app


def _seed_archive(db):
    from hansard_archive.models import (HansardContribution, HansardSession,
                                        HansardSessionTheme, HaPQ, THEME_TYPE_POLICY_AREA)
    s = HansardSession(ext_id="m-1", title="Schools Funding", date=date(2026, 9, 1), house="Commons",
                       debate_type="debate", slug="schools-funding", contributions_ingested=True,
                       is_container=False)
    db.session.add(s)
    db.session.flush()
    db.session.add_all([
        HansardContribution(session_id=s.id, member_name="Bridget Phillipson (Lab)", member_id=4046,
                            speech_text="I beg to move.", speech_order=1),
        HansardContribution(session_id=s.id, member_name="Bridget Phillipson (Lab)", member_id=4046,
                            speech_text="And further.", speech_order=2),
        HansardSessionTheme(session_id=s.id, theme="Education", theme_type=THEME_TYPE_POLICY_AREA),
        HaPQ(uin="M001", question_text="What steps on schools?", tabled_date=date(2026, 8, 1),
             asking_member="Bridget Phillipson", asking_mnis_id=4046,
             answering_body="Department for Education", answering_body_id=60, is_answered=True),
        HaPQ(uin="M002", question_text="An old question.", tabled_date=date(2024, 1, 1),
             asking_member="Bridget Phillipson", asking_mnis_id=4046,
             answering_body="HM Treasury", answering_body_id=14, is_answered=True),
    ])
    db.session.commit()


@pytest.fixture
def app():
    from extensions import db
    app = _make_app()
    with app.app_context():
        db.create_all()
        _seed_archive(db)
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def fake(monkeypatch):
    f = FakeParliament()
    monkeypatch.setattr(refresh_members, "_get_json", f)
    monkeypatch.setattr(refresh_members, "GEMINI_DELAY", 0)
    monkeypatch.setattr(refresh_members, "fetch_wikidata", f.fetch_wikidata)
    monkeypatch.setattr(refresh_members, "fetch_wikipedia_extract", f.fetch_extract)
    return f


@pytest.fixture
def gemini(monkeypatch):
    calls = []

    def fake_generate(prompt, api_key):
        calls.append(prompt)
        return f"## Background\nGenerated text #{len(calls)}\n## Summary\n- A **fact**."

    monkeypatch.setattr(member_profiles, "generate_bio_text", fake_generate)
    return calls


TODAY = date(2026, 10, 20)


def _run(**kw):
    return refresh_members.run(today=TODAY, api_key="test-key", **kw)


# ── Refresh: upserts ──────────────────────────────────────────────────────────

class TestRefreshUpsert:
    def test_first_run_inserts_then_unchanged(self, app, fake):
        from cache_models import MemberProfile
        stats = _run()
        assert stats["inserted"] == 2 and stats["exit_code"] == 0
        p = MemberProfile.query.get(4046)
        assert p.slug == "bridget-phillipson-houghton-and-sunderland-south"
        assert p.current_role == "Secretary of State for Education"
        assert p.committees[0]["name"] == "Public Accounts Committee"
        assert p.interests == [{"category": "Category 1", "description": "Author royalties"}]
        assert p.contact[0]["email"] == "m4046@parliament.uk"
        # activity from our own DB, not the API
        assert (p.wq_total, p.wq_12m, p.sessions_12m, p.contributions_12m) == (2, 1, 1, 2)
        assert p.top_policy_areas == [{"theme": "Education", "sessions": 1}]
        assert p.wq_departments == [{"department": "Department for Education", "questions": 1}]
        assert MemberProfile.query.get(269).slug == "jacqui-smith-of-malvern"

        stats2 = _run()
        assert (stats2["inserted"], stats2["updated"], stats2["unchanged"]) == (0, 0, 2)

    def test_change_is_update_and_slug_is_stable(self, app, fake):
        from cache_models import MemberProfile
        _run()
        fake.members[4046]["latestParty"]["name"] = "Independent"
        fake.members[4046]["nameDisplayAs"] = "Dame Bridget Phillipson"
        stats = _run()
        assert (stats["updated"], stats["unchanged"]) == (1, 1)
        p = MemberProfile.query.get(4046)
        assert p.party == "Independent"
        assert p.slug == "bridget-phillipson-houghton-and-sunderland-south"  # stable-first

    def test_dry_run_predicts_without_writing(self, app, fake):
        from cache_models import MemberProfile
        stats = _run(dry_run=True)
        assert stats["inserted"] == 2
        assert MemberProfile.query.count() == 0

    def test_failed_detail_fetch_keeps_stored_details(self, app, fake):
        from cache_models import MemberProfile
        _run()
        fake.fail_bio_for = {4046}
        fake.bio[4046]["governmentPosts"] = []
        stats = _run()
        assert stats["detail_failed"] == 1
        assert MemberProfile.query.get(4046).current_role == "Secretary of State for Education"

    def test_member_id_and_limit(self, app, fake):
        from cache_models import MemberProfile
        stats = _run(member_ids=[269])
        assert stats["inserted"] == 1 and MemberProfile.query.count() == 1
        assert not any(u.endswith("/Search") for u in fake.calls)
        stats = _run(limit=1)
        # the never-detailed member (4046) goes first
        assert stats["inserted"] == 1 and MemberProfile.query.get(4046) is not None

    def test_mostly_failing_exits_non_zero(self, app, fake):
        fake.fail_bio_for = {4046, 269}
        assert _run()["exit_code"] == 1

    def test_listing_failure_exits_non_zero(self, app, monkeypatch):
        monkeypatch.setattr(refresh_members, "_get_json", lambda *a, **k: None)
        assert _run()["exit_code"] == 1


# ── Refresh: biographies ──────────────────────────────────────────────────────

class TestBioRegeneration:
    def test_regenerates_only_on_input_change(self, app, fake, gemini):
        from cache_models import MemberBiography
        stats = _run(regenerate_bios=True)
        assert stats["bio_generated"] == 2 and len(gemini) == 2
        bio = MemberBiography.query.get(4046)
        assert bio.model_used == member_profiles.BIO_MODEL
        assert bio.prompt_version == member_profiles.PROMPT_VERSION
        assert any("wikipedia" in s["url"] for s in bio.sources)

        stats = _run(regenerate_bios=True)
        assert stats["bio_unchanged"] == 2 and len(gemini) == 2

        fake.interests[4046] = [{"name": "Category 1", "interests": [{"interest": "New directorship"}]}]
        stats = _run(regenerate_bios=True)
        assert (stats["bio_generated"], stats["bio_unchanged"]) == (1, 1) and len(gemini) == 3

    def test_wikipedia_change_regenerates(self, app, fake, gemini):
        _run(regenerate_bios=True)
        fake.wiki_text = "Edited article."
        assert _run(regenerate_bios=True)["bio_generated"] == 2

    def test_wikipedia_fetch_error_keeps_bio(self, app, fake, gemini, monkeypatch):
        from cache_models import MemberBiography
        _run(regenerate_bios=True)
        before = MemberBiography.query.get(4046).bio_text
        monkeypatch.setattr(refresh_members, "fetch_wikipedia_extract", lambda url: ("", False))
        stats = _run(regenerate_bios=True)
        assert stats["bio_skipped"] == 2 and len(gemini) == 2
        assert MemberBiography.query.get(4046).bio_text == before

    def test_dry_run_counts_without_calling_ai(self, app, fake, gemini):
        _run()
        stats = _run(dry_run=True, regenerate_bios=True)
        assert stats["bio_would_generate"] == 2 and gemini == []

    def test_permanent_ai_error_stops_run(self, app, fake, monkeypatch):
        def denied(prompt, key):
            raise member_profiles.GeminiPermanentError("Gemini HTTP 429")
        monkeypatch.setattr(member_profiles, "generate_bio_text", denied)
        stats = _run(regenerate_bios=True)
        assert stats["stopped"] and stats["exit_code"] == 1

    def test_generation_failure_keeps_previous_bio(self, app, fake, gemini, monkeypatch):
        from cache_models import MemberBiography
        _run(regenerate_bios=True)
        fake.interests[4046] = []

        def broken(prompt, key):
            raise RuntimeError("Gemini HTTP 400 (request rejected)")
        monkeypatch.setattr(member_profiles, "generate_bio_text", broken)
        stats = _run(regenerate_bios=True)
        bio = MemberBiography.query.get(4046)
        assert stats["bio_failed"] == 1 and bio.bio_text.startswith("## Background")
        assert "400" in bio.last_error

    def test_prompt_is_neutral(self):
        from types import SimpleNamespace
        profile = SimpleNamespace(name="X", house="Commons", party="Lab", membership_from="Y",
                                  membership_start=None, government_posts=[], opposition_posts=[],
                                  committees=[], interests=[], top_policy_areas=[], wq_departments=[],
                                  representations=[], house_memberships=[], wikidata_id=None,
                                  wikidata_facts=None)
        prompt = member_profiles.bio_prompt(member_profiles.bio_inputs(profile, "", None))
        assert "civil servant" not in prompt.lower()
        assert "policy professionals" in prompt
        assert "likely future behaviour" in prompt


# ── Names, slugs and Wikipedia status ─────────────────────────────────────────

def _name(mid):
    from cache_models import MemberName
    return MemberName.query.get(mid)


class TestNames:
    def test_wikidata_hit(self, app, fake):
        _run()
        n = _name(269)
        assert (n.personal_name, n.source, n.confidence) == ("Jacqui Smith", "wikidata", "high")
        assert (n.given_name, n.family_name) == ("Jacqui", "Smith")
        assert n.peerage_title == "Baroness Smith of Malvern" and n.wikidata_id == "Q237217"
        assert n.parliament_name == "Jacqui Smith" and n.source_url.endswith("Q237217")
        mp = _name(4046)
        assert (mp.personal_name, mp.peerage_title, mp.confidence) == ("Bridget Phillipson", None, "high")

    def test_wikidata_miss_falls_back_to_parliament(self, app, fake):
        fake.wikidata = {}
        _run()
        peer, mp = _name(269), _name(4046)
        assert (peer.personal_name, peer.source, peer.confidence) == ("Jacqui Smith", "parliament", "medium")
        assert peer.given_name is None and peer.family_name is None   # not split: that would be a guess
        assert (mp.personal_name, mp.source, mp.confidence) == ("Bridget Phillipson", "parliament", "high")

    def test_conflict_recorded_and_parliament_used(self, app, fake):
        fake.wikidata[269]["name"] = "Jacqueline Jones"
        stats = _run(audit_names=True)
        n = _name(269)
        assert (n.personal_name, n.source, n.confidence) == ("Jacqui Smith", "parliament", "low")
        assert "conflict" in n.audit_notes and "Jacqueline Jones" in n.audit_notes
        assert stats["names"]["conflicts"] == 1

    def test_no_name_anywhere_keeps_title_slug(self, app, fake):
        from cache_models import MemberProfile
        fake.wikidata = {}
        fake.synopsis[269] = "Baroness Smith of Malvern is a member of the House of Lords."
        stats = _run(audit_names=True)
        assert _name(269).personal_name is None and stats["names"]["missing"] == 1
        p = MemberProfile.query.get(269)
        assert (p.slug, p.slug_source) == ("baroness-smith-of-malvern", "title")

    def test_slug_upgrades_and_old_slug_301s(self, app, fake):
        from cache_models import MemberProfile
        fake.wikidata = {}
        fake.synopsis[269] = "No name sentence here."
        _run()
        assert MemberProfile.query.get(269).slug == "baroness-smith-of-malvern"
        fake.wikidata = FakeParliament().wikidata
        fake.synopsis[269] = FakeParliament().synopsis[269]
        stats = _run(audit_names=True)
        assert stats["names"]["slugs_changed"] == 1
        assert MemberProfile.query.get(269).slug == "jacqui-smith-of-malvern"
        r = app.test_client().get("/members/269/baroness-smith-of-malvern")
        assert r.status_code == 301 and r.headers["Location"].endswith("/members/269/jacqui-smith-of-malvern")
        # stable-first: a later name change does not move a personal-name slug
        fake.wikidata[269]["name"] = "Jacqueline Smith"
        _run(audit_names=True)
        assert MemberProfile.query.get(269).slug == "jacqui-smith-of-malvern"

    def test_weekly_run_only_checks_new_members(self, app, fake):
        _run()
        assert fake.wikidata_calls == [[269, 4046]]
        _run()
        assert fake.wikidata_calls == [[269, 4046]]          # nothing new: no Wikidata call
        _run(audit_names=True)
        assert len(fake.wikidata_calls) == 2

    def test_audit_report_counts_changes_and_wiki_gaps(self, app, fake):
        from cache_models import MemberNameAudit
        _run()
        fake.wikidata[4046]["name"] = "Bridget M. Phillipson"
        fake.wikidata[269]["article"] = None
        stats = _run(audit_names=True)
        counts = stats["names"]
        assert counts["changed"] == 1 and counts["wiki_not_found"] == 1 and counts["wiki_found"] == 1
        row = MemberNameAudit.query.one()
        assert row.counts["changed"] == 1
        issues = {(g["member_id"], g["issue"]) for g in row.gaps}
        assert {(4046, "changed"), (269, "wikipedia_not_found")} <= issues
        _run(dry_run=True, audit_names=True)
        assert MemberNameAudit.query.count() == 1             # dry run writes no audit row

    def test_manual_name_not_overwritten(self, app, fake):
        from extensions import db
        _run()
        n = _name(269)
        n.source, n.personal_name = "manual", "Jacqui Smith (corrected)"
        db.session.commit()
        stats = _run(audit_names=True)
        assert _name(269).personal_name == "Jacqui Smith (corrected)" and stats["names"]["manual"] == 1

    def test_lookup_error_keeps_existing_name(self, app, fake, monkeypatch):
        _run()
        monkeypatch.setattr(refresh_members, "fetch_wikidata",
                            lambda ids: {m: {"status": "error"} for m in ids})
        fake.fail_synopsis = True
        stats = _run(audit_names=True)
        assert _name(269).personal_name == "Jacqui Smith" and stats["names"]["lookup_errors"] == 2
        assert stats["exit_code"] == 1                        # Wikidata down for everyone: mostly failing

    def test_wiki_status_recorded(self, app, fake):
        from cache_models import MemberProfile
        fake.wikidata[269]["article"] = None
        fake.wikidata[4046] = {"status": "ambiguous", "qids": ["Q1", "Q2"]}
        _run(audit_names=True)
        assert MemberProfile.query.get(269).wiki_status == "not_found"
        p = MemberProfile.query.get(4046)
        assert p.wiki_status == "ambiguous" and p.wikipedia_url is None and p.wiki_checked_at

    def test_fallback_sources_when_no_wikipedia(self, app, fake, gemini):
        from cache_models import MemberBiography, MemberProfile
        fake.wikidata[269]["article"] = None
        _run(regenerate_bios=True)
        prompt = next(p for p in gemini if "Jacqui Smith" in p)
        assert "No Wikipedia article was matched" in prompt
        assert "Hertford College" in prompt and "teacher" in prompt
        labels = [s["label"] for s in MemberBiography.query.get(269).sources]
        assert any(lbl.startswith("Wikidata") for lbl in labels)
        assert not any(lbl.startswith("Wikipedia") for lbl in labels)
        assert MemberProfile.query.get(269).wiki_status == "not_found"
        labels_mp = [s["label"] for s in MemberBiography.query.get(4046).sources]
        assert any(lbl.startswith("Wikipedia") for lbl in labels_mp)

    def test_wikidata_error_skips_bio(self, app, fake, gemini, monkeypatch):
        _run()
        monkeypatch.setattr(refresh_members, "fetch_wikidata",
                            lambda ids: {m: {"status": "error"} for m in ids})
        stats = _run(regenerate_bios=True)
        assert stats["bio_skipped"] == 2 and gemini == []


def _binding(**kw):
    return {k: {"value": v} for k, v in kw.items()}


def test_parse_wikidata():
    rows = [
        _binding(mnis="3652", item="http://www.wikidata.org/entity/Q908568",
                 label="Brenda Hale, Baroness Hale of Richmond", givens="Brenda", families="Hale",
                 education="Girton College|Richmond School", occupations="judge"),
        _binding(mnis="1", item="http://www.wikidata.org/entity/Q1", label="A B"),
        _binding(mnis="1", item="http://www.wikidata.org/entity/Q2", label="A B"),
    ]
    out = refresh_members.parse_wikidata(rows, [3652, 1, 5486])
    assert out[3652]["name"] == "Brenda Hale" and out[3652]["family"] == "Hale"
    assert out[3652]["education"] == ["Girton College", "Richmond School"] and out[3652]["article"] is None
    assert out[1]["status"] == "ambiguous" and out[5486]["status"] == "not_found"


@pytest.mark.parametrize("synopsis,expected", [
    ("Lord Addington's full title is The Lord Addington. His name is Dominic Bryce Hubbard, and he is",
     "Dominic Bryce Hubbard"),
    ("Her name is Jacqui Smith, and she is a current member", "Jacqui Smith"),
    ("The Rt Hon Bridget Phillipson is the Labour MP for X.", None),
])
def test_synopsis_name(synopsis, expected):
    assert member_profiles.synopsis_name(synopsis) == expected


@pytest.mark.parametrize("title,personal,expected", [
    ("Baroness Smith of Malvern", "Jacqui Smith", "jacqui-smith-of-malvern"),
    ("Baroness Hale of Richmond", "Brenda Hale", "brenda-hale-of-richmond"),
    ("Lord Addington", "Dominic Hubbard", "dominic-hubbard-baron"),
    ("Baroness Kennedy", "Jane Kennedy", "jane-kennedy-baroness"),
    ("The Lord Bishop of Blackburn", "Philip North", "philip-north-bishop-of-blackburn"),
    ("The Lord Bishop of Blackburn", None, "the-lord-bishop-of-blackburn"),
])
def test_peer_slug_from_personal_name(title, personal, expected):
    assert member_profiles.member_slug(title, "Lords", "Life peer", personal) == expected


# ── Routes: DB only, no network ───────────────────────────────────────────────

@pytest.fixture
def seeded(app, fake, gemini):
    from cache_models import CachedMember
    from extensions import db
    _run(regenerate_bios=True)
    db.session.add(CachedMember(member_id=777, name="Newly Elected", party="Green",
                                constituency="Somewhere", house="Commons", image_url=""))
    db.session.commit()
    return app


PROFILE_URL = "/members/4046/bridget-phillipson-houghton-and-sunderland-south"


class TestRoutesFromDb:
    def test_network_blocker_blocks(self, no_network):
        with pytest.raises(requests.ConnectionError):
            requests.get("https://members-api.parliament.uk/api/Members/4046", timeout=1)
        assert no_network == ["https://members-api.parliament.uk/api/Members/4046"]

    def test_profile_page(self, seeded, no_network):
        client = seeded.test_client()
        r = client.get("/members/4046")
        assert r.status_code == 301 and r.headers["Location"].endswith(PROFILE_URL)
        r = client.get(PROFILE_URL)
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "Bridget Phillipson" in html and "Secretary of State for Education" in html
        assert member_profiles.AI_LABEL in html and "Generated text" in html
        assert f'<link rel="canonical" href="https://westminsterbrief.co.uk{PROFILE_URL}">' in html
        assert '"@type": "Person"' in html and 'content="index, follow"' in html
        assert "Last updated" in html or "last updated" in html
        assert no_network == []

    def test_wrong_slug_redirects(self, seeded, no_network):
        r = seeded.test_client().get("/members/4046/old-slug")
        assert r.status_code == 301 and r.headers["Location"].endswith(PROFILE_URL)

    def test_unstored_member_falls_back_noindex(self, seeded, no_network):
        client = seeded.test_client()
        r = client.get("/members/777", follow_redirects=True)
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and "Newly Elected" in html
        assert 'content="noindex, follow"' in html and "Full profile not stored yet" in html
        assert client.get("/members/999999").status_code == 404
        assert no_network == []

    def test_mp_search_post(self, seeded, no_network):
        r = seeded.test_client().post("/mp_search", data={"member_id": "4046"})
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "Secretary of State for Education" in html and "Author royalties" in html
        assert "Generated text" in html and member_profiles.AI_LABEL in html
        assert "M001" in html
        assert no_network == []

    def test_mp_search_unknown_member(self, seeded, no_network):
        r = seeded.test_client().post("/mp_search", data={"member_id": "999999"})
        assert r.status_code == 200 and "weekly refresh" in r.get_data(as_text=True)
        assert no_network == []

    def test_biography_finder_redirects(self, seeded, no_network):
        client = seeded.test_client()
        r = client.post("/biography", data={"member_id": "4046"})
        assert r.status_code == 303 and r.headers["Location"].endswith(PROFILE_URL)
        r = client.get("/biography?member_id=4046")
        assert r.status_code == 301
        assert client.get("/biography").status_code == 200
        assert no_network == []

    def test_search_and_api_biography(self, seeded, no_network):
        client = seeded.test_client()
        ids = [r["id"] for r in client.get("/api/search_members?q=phill").get_json()["results"]]
        assert ids == [4046]
        assert [r["id"] for r in client.get("/api/search_members?q=newly").get_json()["results"]] == [777]
        data = client.post("/api/biography", json={"member_id": 4046}).get_json()
        assert data["available"] and "Generated text" in data["biography"]
        assert client.post("/api/biography", json={"member_id": 777}).get_json()["available"] is False
        assert no_network == []

    @pytest.mark.parametrize("term", ["Jacq", "jacqui smith", "Smith of Mal", "baroness smith", "Malvern"])
    def test_search_matches_either_name_and_shows_both(self, seeded, no_network, term):
        results = seeded.test_client().get(f"/api/search_members?q={term}").get_json()["results"]
        assert {"id": 269, "text": "Baroness Smith of Malvern (Jacqui Smith)"} in results
        assert no_network == []

    def test_search_mp_partial(self, seeded, no_network):
        results = seeded.test_client().get("/api/search_members?q=Phillips").get_json()["results"]
        assert results == [{"id": 4046, "text": "Bridget Phillipson"}]

    def test_search_before_names_filled(self, seeded, no_network):
        from cache_models import MemberName
        from extensions import db
        MemberName.query.delete()
        db.session.commit()
        results = seeded.test_client().get("/api/search_members?q=Smith of Mal").get_json()["results"]
        assert results == [{"id": 269, "text": "Baroness Smith of Malvern"}]

    def test_peer_page_shows_personal_name(self, seeded, no_network):
        html = seeded.test_client().get("/members/269/jacqui-smith-of-malvern").get_data(as_text=True)
        assert "Baroness Smith of Malvern (Jacqui Smith)" in html
        assert '"name": "Jacqui Smith"' in html and '"alternateName": "Baroness Smith of Malvern"' in html

    def test_word_export_uses_stored_text(self, seeded, no_network):
        from docx import Document
        client = seeded.test_client()
        r = client.post("/export_biography_word",
                        data={"member_id": "4046", "bio_text": "FAKE POSTED TEXT",
                              "image_url": "http://169.254.169.254/latest"})
        assert r.status_code == 200 and r.headers["X-Robots-Tag"] == "noindex"
        text = "\n".join(p.text for p in Document(io.BytesIO(r.data)).paragraphs)
        assert "Generated text" in text and "FAKE POSTED TEXT" not in text
        assert member_profiles.AI_LABEL in text
        assert client.get("/members/4046/profile.docx").status_code == 200
        # only Parliament's own image host is ever contacted (for the photo)
        assert all(u.startswith(member_profiles.PARLIAMENT_IMAGE_PREFIX) for u in no_network)

    def test_mp_word_export_ignores_foreign_photo_url(self, seeded, no_network):
        import json
        payload = {"member_header": {"id": 4046, "name": "Bridget Phillipson",
                                     "photo_url": "http://169.254.169.254/latest"},
                   "profile_data": None, "questions": [], "speeches": []}
        r = seeded.test_client().post("/download_mp_word", data={"export_data": json.dumps(payload)})
        assert r.status_code == 200 and no_network == []

    def test_sitemap_members(self, seeded, no_network):
        xml = seeded.test_client().get("/sitemap-members.xml").get_data(as_text=True)
        assert f"https://westminsterbrief.co.uk{PROFILE_URL}" in xml
        assert "/members/269/jacqui-smith-of-malvern" in xml
        assert "/members/777/" not in xml


class TestBeforeMigration:
    """Code deployed before the migration runs must not 500."""

    def test_routes_without_new_tables(self, no_network):
        from cache_models import CachedMember, MemberBiography, MemberProfile
        from extensions import db
        app = _make_app()
        with app.app_context():
            db.create_all()
            MemberBiography.__table__.drop(db.engine)
            MemberProfile.__table__.drop(db.engine)
            db.session.add(CachedMember(member_id=777, name="Newly Elected", party="Green",
                                        constituency="Somewhere", house="Commons", image_url=""))
            db.session.commit()
            client = app.test_client()
            assert client.get("/members/777", follow_redirects=True).status_code == 200
            assert client.post("/mp_search", data={"member_id": "777"}).status_code == 200
            assert client.get("/api/search_members?q=newly").get_json()["results"][0]["id"] == 777
            assert "<urlset" in client.get("/sitemap-members.xml").get_data(as_text=True)
            db.session.remove()
            db.drop_all()
        assert no_network == []


# ── Migration, slugs, rendering ───────────────────────────────────────────────

def test_migration_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    sys.path.insert(0, str(PROJECT_ROOT / "migrations"))
    import members_profile_tables as mig
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE cached_member (id INTEGER PRIMARY KEY, name TEXT)"))
    assert sorted(mig.upgrade(engine)) == ["member_biography", "member_name", "member_name_audit",
                                           "member_profile"]
    assert mig.upgrade(engine) == []
    assert {"cached_member", "member_profile", "member_biography", "member_name",
            "member_name_audit"} <= set(inspect(engine).get_table_names())
    assert [c["name"] for c in inspect(engine).get_columns("cached_member")] == ["id", "name"]


@pytest.mark.parametrize("name,house,seat,expected", [
    ("Sir Keir Starmer", "Commons", "Holborn and St Pancras", "keir-starmer-holborn-and-st-pancras"),
    ("Dr Rosena Allin-Khan", "Commons", "Tooting", "rosena-allin-khan-tooting"),
    ("Llinos Medi", "Commons", "Ynys Môn", "llinos-medi-ynys-mon"),
    ("Baroness Smith of Malvern", "Lords", "Life peer", "baroness-smith-of-malvern"),
    ("Lord O'Donnell", "Lords", "Life peer", "lord-odonnell"),
])
def test_member_slug(name, house, seat, expected):
    assert member_profiles.member_slug(name, house, seat) == expected


def test_render_bio_html_escapes():
    html = str(member_profiles.render_bio_html("## Head\n- <script>x</script> **b**\ntext"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<h3>Head</h3>" in html and "<strong>b</strong>" in html and "<p>text</p>" in html

