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
        self.fail_bio_for = set()
        self.calls = []

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
    monkeypatch.setattr(refresh_members, "fetch_wikipedia",
                        lambda mid: (f"Wikipedia text for {mid}.", f"https://en.wikipedia.org/wiki/M{mid}", True))
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
        assert MemberProfile.query.get(269).slug == "baroness-smith-of-malvern"

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

    def test_wikipedia_change_regenerates(self, app, fake, gemini, monkeypatch):
        _run(regenerate_bios=True)
        monkeypatch.setattr(refresh_members, "fetch_wikipedia",
                            lambda mid: ("Edited article.", "https://en.wikipedia.org/wiki/X", True))
        assert _run(regenerate_bios=True)["bio_generated"] == 2

    def test_wikipedia_fetch_error_keeps_bio(self, app, fake, gemini, monkeypatch):
        from cache_models import MemberBiography
        _run(regenerate_bios=True)
        before = MemberBiography.query.get(4046).bio_text
        monkeypatch.setattr(refresh_members, "fetch_wikipedia", lambda mid: ("", None, False))
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
                                  committees=[], interests=[], top_policy_areas=[], wq_departments=[])
        prompt = member_profiles.bio_prompt(member_profiles.bio_inputs(profile, "", None))
        assert "civil servant" not in prompt.lower()
        assert "policy professionals" in prompt
        assert "likely future behaviour" in prompt


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
        assert "/members/269/baroness-smith-of-malvern" in xml
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
    assert sorted(mig.upgrade(engine)) == ["member_biography", "member_profile"]
    assert mig.upgrade(engine) == []
    assert {"cached_member", "member_profile", "member_biography"} <= set(inspect(engine).get_table_names())
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

