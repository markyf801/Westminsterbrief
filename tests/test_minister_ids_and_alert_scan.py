"""
Minister member ids and the My Alerts Hansard scan, both served from our own DB.

- get_dept_minister_member_ids resolves department ministers to Parliament member
  ids from member_profile / cached_member, with the Members API as a last resort
- find_hansard_mentions searches ha_contribution for a keyword since a date and
  returns internal archive links, one per session
- /run_manual_scan adds Hansard alerts from that search, once per topic and URL

Run: python -m pytest tests/test_minister_ids_and_alert_scan.py -v
"""

import json
from datetime import date, timedelta

import pytest
import requests

import debate_scanner


@pytest.fixture
def no_http(monkeypatch):
    attempted = []

    def blocked(self, method, url, *a, **kw):
        attempted.append(url)
        raise AssertionError(f"unexpected HTTP call: {method} {url}")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    return attempted


@pytest.fixture
def app():
    from flask import Flask
    from extensions import db

    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", TESTING=True)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _profile(member_id, name, house, current=True):
    from cache_models import MemberProfile
    return MemberProfile(member_id=member_id, slug=f"m-{member_id}", name=name,
                         house=house, is_current=current)


def _minister_data(names, dept="Department for Education"):
    return {"by_dept": {dept: [{"name": n, "role": "Minister"} for n in names]}}


class TestDeptMinisterMemberIds:
    @pytest.fixture(autouse=True)
    def cache_file(self, tmp_path, monkeypatch):
        path = tmp_path / "minister_cache.json"
        monkeypatch.setattr(debate_scanner, "MINISTER_CACHE_FILE", str(path))
        return path

    def test_resolves_from_member_profile_without_http(self, app, no_http, cache_file):
        from extensions import db
        db.session.add_all([_profile(4046, "Bridget Phillipson", "Commons"),
                            _profile(9001, "Lord Example of Somewhere", "Lords")])
        db.session.commit()
        data = _minister_data(["Bridget Phillipson", "Lord Example of Somewhere"])

        result = debate_scanner.get_dept_minister_member_ids("Department for Education", data)

        assert result == [
            {"parliament_id": 4046, "name": "Bridget Phillipson", "role": "Minister", "is_lord": False},
            {"parliament_id": 9001, "name": "Lord Example of Somewhere", "role": "Minister", "is_lord": True},
        ]
        assert no_http == []
        saved = json.loads(cache_file.read_text())
        assert saved["member_ids"]["Bridget Phillipson"]["parliament_id"] == 4046

    def test_normalised_name_match(self, app, no_http):
        from extensions import db
        db.session.add(_profile(4046, "Bridget Phillipson", "Commons"))
        db.session.commit()
        data = _minister_data(["Rt Hon Bridget Phillipson MP"])

        result = debate_scanner.get_dept_minister_member_ids("Department for Education", data)

        assert [r["parliament_id"] for r in result] == [4046]

    def test_ignores_former_members(self, app, monkeypatch):
        from extensions import db
        db.session.add(_profile(1, "Jane Doe", "Commons", current=False))
        db.session.commit()
        monkeypatch.setattr(debate_scanner, "_resolve_parliament_id", lambda name: (None, None))

        result = debate_scanner.get_dept_minister_member_ids(
            "Department for Education", _minister_data(["Jane Doe"]))

        assert result == []

    def test_ambiguous_normalised_match_falls_back_to_members_api(self, app, monkeypatch):
        from extensions import db
        db.session.add_all([_profile(1, "Sir John Smith", "Commons"),
                            _profile(2, "Dr John Smith", "Commons")])
        db.session.commit()
        calls = []

        def fake_resolve(name):
            calls.append(name)
            return 3, "Commons"

        monkeypatch.setattr(debate_scanner, "_resolve_parliament_id", fake_resolve)
        result = debate_scanner.get_dept_minister_member_ids(
            "Department for Education", _minister_data(["John Smith"]))

        assert calls == ["John Smith"]
        assert [r["parliament_id"] for r in result] == [3]

    def test_verified_override_and_cached_ids_need_no_lookup(self, app, no_http):
        data = _minister_data(["Baroness Smith of Malvern", "Cached Minister"])
        data["member_ids"] = {"Cached Minister": {"parliament_id": 77, "is_lord": False}}

        result = debate_scanner.get_dept_minister_member_ids("Department for Education", data)

        assert [(r["name"], r["parliament_id"], r["is_lord"]) for r in result] == [
            ("Baroness Smith of Malvern", 269, True), ("Cached Minister", 77, False)]

    def test_failed_lookup_is_cached_and_not_retried(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(debate_scanner, "_resolve_parliament_id",
                            lambda name: calls.append(name) or (None, None))
        data = _minister_data(["Nobody Known"])

        debate_scanner.get_dept_minister_member_ids("Department for Education", data)
        debate_scanner.get_dept_minister_member_ids("Department for Education", data)

        assert calls == ["Nobody Known"]


def _seed_sessions(db):
    from hansard_archive.models import HansardContribution, HansardSession
    today = date.today()
    recent = HansardSession(ext_id="a-1", title="Student Loans", date=today - timedelta(days=1),
                            house="Commons", debate_type="debate", slug="student-loans-1",
                            is_container=False)
    old = HansardSession(ext_id="a-2", title="Old Debate", date=today - timedelta(days=60),
                         house="Lords", debate_type="debate", slug="old-debate-2", is_container=False)
    container = HansardSession(ext_id="a-3", title="Education", date=today, house="Commons",
                               debate_type="debate", slug="education-3", is_container=True)
    db.session.add_all([recent, old, container])
    db.session.flush()
    db.session.add_all([
        HansardContribution(session_id=recent.id, member_name="A Member", member_id=1,
                            speech_text="Tuition fees  are\nrising.", speech_order=1),
        HansardContribution(session_id=recent.id, member_name="B Member", member_id=2,
                            speech_text="More on tuition fees.", speech_order=2),
        HansardContribution(session_id=old.id, member_name="C Member", member_id=3,
                            speech_text="Tuition fees long ago.", speech_order=1),
        HansardContribution(session_id=container.id, member_name="D Member", member_id=4,
                            speech_text="Tuition fees in a container.", speech_order=1),
    ])
    db.session.commit()
    return recent


class TestFindHansardMentions:
    def test_one_hit_per_session_since_date_with_internal_url(self, app):
        from extensions import db
        recent = _seed_sessions(db)
        since = date.today() - timedelta(days=7)

        hits = debate_scanner.find_hansard_mentions("Tuition Fees", since)

        d = recent.date
        month = d.strftime("%B").lower()
        assert hits == [{
            "title": "Student Loans",
            "url": f"/archive/debate/{d.day}-{month}-{d.year}/student-loans-1",
            "snippet": "Tuition fees are rising.",
            "speaker": "A Member",
            "date": d.isoformat(),
        }]

    def test_blank_keyword_returns_nothing(self, app):
        assert debate_scanner.find_hansard_mentions("  ", date.today()) == []


class TestManualScan:
    @pytest.fixture
    def fa(self, monkeypatch):
        import flask_app as _fa
        from extensions import db
        for key in ("NEWS_API_KEY", "GEMINI_API_KEY", "BSKY_HANDLE", "BSKY_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        with _fa.app.app_context():
            db.create_all()
            _seed_sessions(db)
            user = _fa.User(email="scan@example.com", password_hash="x",
                            has_completed_onboarding=True)
            db.session.add(user)
            db.session.flush()
            topic = _fa.TrackedTopic(keyword="tuition fees", department="All Departments",
                                     user_id=user.id)
            db.session.add(topic)
            db.session.commit()
            ids = (user.id, topic.id)
        yield _fa, ids
        # Remove only this test's rows: the app's in-memory DB is shared with other modules.
        from hansard_archive.models import HansardContribution, HansardSession
        with _fa.app.app_context():
            _fa.Alert.query.filter_by(topic_id=ids[1]).delete()
            _fa.TrackedTopic.query.filter_by(id=ids[1]).delete()
            _fa.User.query.filter_by(id=ids[0]).delete()
            sids = [s.id for s in HansardSession.query.filter(
                HansardSession.ext_id.in_(["a-1", "a-2", "a-3"]))]
            HansardContribution.query.filter(HansardContribution.session_id.in_(sids)).delete()
            HansardSession.query.filter(HansardSession.id.in_(sids)).delete()
            db.session.commit()
            db.session.remove()

    def test_adds_hansard_alert_once(self, fa, no_http):
        _fa, (user_id, topic_id) = fa
        client = _fa.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True

        assert client.post("/run_manual_scan").status_code == 302
        assert client.post("/run_manual_scan").status_code == 302

        with _fa.app.app_context():
            alerts = _fa.Alert.query.filter_by(topic_id=topic_id).all()
            assert [(a.source, a.title, a.speaker) for a in alerts] == [
                ("Hansard", "Student Loans", "A Member")]
            assert alerts[0].url.startswith("/archive/debate/")
        assert no_http == []
