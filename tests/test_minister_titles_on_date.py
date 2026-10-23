"""
Minister titles as held on the date of the answer or speech (customer scenario
reports, 23 Oct 2026: Stephen Morgan shown as a Government Whip on Defra
answers months after that post ended; Emma Reynolds shown with her previous
post; the Research Tool projecting current posts back).

Titles come from our own tables only: member_profile.government_posts (dated
Members API Biography history) and gov_minister. The never-refreshed
cached_member.ministerial_role snapshot is no longer used.

SQLite in-memory; no HTTP.

Run: python -m pytest tests/test_minister_titles_on_date.py -v
"""
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

import ministers

PROJECT_ROOT = Path(__file__).parents[1]

DEFRA = "Department for Environment, Food and Rural Affairs"

# Shapes as stored by scripts/refresh_members.py:_posts (Members API Biography)
MORGAN_POSTS = [
    {"name": f"Parliamentary Under-Secretary ({DEFRA})", "start": "2026-07-22", "end": None, "info": DEFRA},
    {"name": f"Minister of State ({DEFRA})", "start": "2026-06-12", "end": "2026-07-22", "info": DEFRA},
    {"name": "Government Whip, Lord Commissioner of HM Treasury", "start": "2025-09-07",
     "end": "2026-06-12", "info": "Whips Office (Commons)"},
    {"name": "Parliamentary Under-Secretary (Department for Education)", "start": "2024-07-09",
     "end": "2025-09-07", "info": "Department for Education"},
]
REYNOLDS_POSTS = [
    {"name": "Chief Secretary to the Treasury", "start": "2026-07-20", "end": None, "info": "HM Treasury"},
    {"name": f"Secretary of State for {DEFRA[len('Department for '):]}", "start": "2025-09-05",
     "end": "2026-07-20", "info": DEFRA},
]
BLAKE_POSTS = [
    {"name": "Parliamentary Under-Secretary (Housing, Communities and Local Government)",
     "start": "2026-07-22", "end": None, "info": "Ministry of Housing, Communities and Local Government"},
    {"name": "Baroness in Waiting (HM Household) (Whip)", "start": "2024-07-11", "end": None,
     "info": "HM Household"},
]
EAGLE_POSTS = [
    {"name": f"Secretary of State for {DEFRA[len('Department for '):]}", "start": "2026-07-20",
     "end": None, "info": DEFRA},
    {"name": f"Minister of State ({DEFRA})", "start": "2025-09-06", "end": "2026-06-12", "info": DEFRA},
]
DFE = "Department for Education"
# Real shapes (Members API Biography, 23 Oct 2026)
PHILLIPSON_POSTS = [
    {"name": "Minister for Women and Equalities", "start": "2026-07-20", "end": None,
     "info": "Ministry of Housing, Communities and Local Government"},
    {"name": "Minister for Women and Equalities", "start": "2024-07-08", "end": "2026-07-20", "info": DFE},
    {"name": "Secretary of State for Education", "start": "2024-07-05", "end": "2026-07-20", "info": DFE},
]
SMITH_POSTS = [
    {"name": "Minister of State (Department for Work and Pensions)", "start": "2025-09-06", "end": None,
     "info": "Department for Work and Pensions"},
    {"name": "Minister of State (Minister for Women and Equalities) ", "start": "2025-03-04",
     "end": "2026-07-21", "info": DFE},
    {"name": "Lords Spokesperson (Equalities)", "start": "2024-10-08", "end": "2025-03-04",
     "info": "Government Equalities Office"},
    {"name": "Minister of State (Education)", "start": "2024-07-06", "end": None, "info": DFE},
]
CHANCELLOR_POSTS = [{"name": "Chancellor of the Exchequer", "start": "2024-07-05", "end": None,
                     "info": "HM Treasury"}]
STALE_WHIP = "Government Whip, Lord Commissioner of HM Treasury"


@pytest.fixture
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db

    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401
    from hansard_archive.views import archive_bp, brief_bp
    from hansard_archive.slugs import slugify_theme
    from feature_flags import feature_enabled

    a = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    a.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", SQLALCHEMY_TRACK_MODIFICATIONS=False,
                    TESTING=True, SECRET_KEY="test", LOGIN_DISABLED=True)
    _db.init_app(a)
    lm = LoginManager()
    lm.init_app(a)
    lm.user_loader(lambda uid: None)
    a.context_processor(lambda: {"admin_authenticated": False, "app_version": "test"})
    a.jinja_env.filters["slugify"] = slugify_theme
    a.jinja_env.globals["feature_enabled"] = feature_enabled
    a.register_blueprint(archive_bp)
    a.register_blueprint(brief_bp)
    ministers.clear_cache()
    with a.app_context():
        _db.create_all()
        yield a
        _db.session.remove()
        _db.drop_all()
    ministers.clear_cache()


@pytest.fixture
def no_http(monkeypatch):
    def blocked(self, method, url, *a, **kw):
        raise AssertionError(f"unexpected HTTP call: {method} {url}")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def _member(member_id, name, house="Commons", posts=None, detailed=True, party="Labour",
            constituency="Portsmouth South", stale_role=None):
    from cache_models import CachedMember, MemberProfile
    from extensions import db
    db.session.add(CachedMember(member_id=member_id, name=name, party=party, house=house,
                                constituency=constituency if house == "Commons" else "",
                                ministerial_role=stale_role))
    db.session.add(MemberProfile(member_id=member_id, slug=f"m-{member_id}", name=name, house=house,
                                 is_current=True, government_posts=posts,
                                 details_refreshed_at=datetime(2026, 10, 18) if detailed else None))
    db.session.commit()


def _gov_row(member_id, name, department, role, start, house="Commons"):
    from cache_models import GovMinister
    from extensions import db
    db.session.add(GovMinister(department=department, role_title=role, member_name=name,
                               name_norm=ministers.name_key(name), mnis_id=member_id, house=house,
                               rank=1, start_date=start, source="parliament",
                               fetched_at=datetime(2026, 10, 23, 6, 15)))
    db.session.commit()
    ministers.clear_cache()


# ── post_on_date / pq_answering_role ──────────────────────────────────────────

class TestPostOnDate:
    def test_morgan_defra_answers_get_post_held_that_day(self, app, no_http):
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS, stale_role=STALE_WHIP)
        # gov_minister is still empty (refresh_ministers not yet run on production)
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 6, 25)) == f"Minister of State ({DEFRA})"
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 10, 16)) == \
            f"Parliamentary Under-Secretary ({DEFRA})"

    def test_changeover_day_gives_the_new_post(self, app):
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS)
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 7, 22)) == \
            f"Parliamentary Under-Secretary ({DEFRA})"

    def test_commons_whip_post_never_shown_on_a_department_answer(self, app):
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS)
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 3, 1)) is None

    def test_reynolds_treasury_answer_after_move(self, app):
        _member(4077, "Emma Reynolds", posts=REYNOLDS_POSTS, constituency="Wycombe")
        assert ministers.pq_answering_role(4077, "HM Treasury", date(2026, 9, 8)) == \
            "Chief Secretary to the Treasury"
        assert ministers.pq_answering_role(4077, DEFRA, date(2026, 5, 1)).startswith("Secretary of State")

    def test_lords_whip_answering_for_another_department(self, app):
        _member(4912, "Baroness Blake of Leeds", house="Lords", posts=BLAKE_POSTS)
        assert ministers.pq_answering_role(
            4912, "Ministry of Housing, Communities and Local Government", date(2026, 9, 2)) == \
            "Parliamentary Under-Secretary (Housing, Communities and Local Government)"
        assert ministers.pq_answering_role(4912, "Department for Education", date(2026, 9, 2)) == \
            "Baroness in Waiting (HM Household) (Whip)"

    def test_no_dated_record_and_empty_table_gives_no_title(self, app):
        _member(4653, "Stephen Morgan", posts=None, detailed=False, stale_role=STALE_WHIP)
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 10, 16)) is None

    def test_gov_minister_fills_a_post_newer_than_the_weekly_history(self, app):
        # History fetched 18 Oct; a same-department promotion on 20 Oct is only in
        # gov_minister (daily) so far.
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS)
        _gov_row(4653, "Stephen Morgan", DEFRA, f"Minister of State ({DEFRA})", date(2026, 10, 20))
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 10, 21)) == f"Minister of State ({DEFRA})"
        assert ministers.pq_answering_role(4653, DEFRA, date(2026, 10, 16)) == \
            f"Parliamentary Under-Secretary ({DEFRA})"

    def test_stale_gov_minister_row_does_not_override_history(self, app):
        # refresh_ministers failed and kept an old table: Reynolds still listed at Defra
        _member(4077, "Emma Reynolds", posts=REYNOLDS_POSTS, constituency="Wycombe")
        _gov_row(4077, "Emma Reynolds", DEFRA, f"Secretary of State for {DEFRA[15:]}", date(2025, 9, 5))
        assert ministers.pq_answering_role(4077, DEFRA, date(2026, 10, 1)) is None
        assert ministers.pq_answering_role(4077, DEFRA, date(2026, 5, 1)).startswith("Secretary of State")

    def test_gov_minister_alone_needs_start_on_or_before_answer(self, app):
        _gov_row(5305, "Georgia Gould", "Department for Education", "Minister of State (Education)",
                 date(2025, 9, 6))
        assert ministers.pq_answering_role(5305, "Department for Education", date(2026, 1, 5)) == \
            "Minister of State (Education)"
        assert ministers.pq_answering_role(5305, "Department for Education", date(2025, 3, 1)) is None

    def test_debate_speech_prefers_departmental_post_without_department(self, app):
        _member(4912, "Baroness Blake of Leeds", house="Lords", posts=BLAKE_POSTS)
        assert ministers.post_on_date(4912, "2026-09-02") == \
            "Parliamentary Under-Secretary (Housing, Communities and Local Government)"
        assert ministers.post_on_date(4912, "2025-12-15") == "Baroness in Waiting (HM Household) (Whip)"

    def test_phillipson_secretary_of_state_not_women_and_equalities(self, app):
        _member(4046, "Bridget Phillipson", posts=PHILLIPSON_POSTS, constituency="Houghton and Sunderland South")
        assert ministers.pq_answering_role(4046, DFE, date(2025, 1, 10)) == "Secretary of State for Education"
        assert ministers.post_on_date(4046, "2025-01-10") == "Secretary of State for Education"
        assert ministers.post_on_date(4046, "2025-01-10", DFE) == "Secretary of State for Education"
        # Her Women and Equalities answers still get that post
        assert ministers.pq_answering_role(4046, "Women and Equalities", date(2025, 1, 10)) == \
            "Minister for Women and Equalities"
        # Both DfE posts ended 20 Jul 2026 (end day not counted as held)
        assert ministers.pq_answering_role(4046, DFE, date(2026, 7, 20)) is None

    def test_smith_of_malvern_main_education_post(self, app):
        _member(269, "Baroness Smith of Malvern", house="Lords", posts=SMITH_POSTS)
        for day in (date(2025, 1, 15), date(2025, 6, 10), date(2026, 1, 20), date(2026, 9, 10)):
            assert ministers.pq_answering_role(269, DFE, day) == "Minister of State (Education)", day
            assert ministers.post_on_date(269, day) == "Minister of State (Education)", day
        assert ministers.pq_answering_role(269, "Department for Work and Pensions", date(2026, 1, 20)) == \
            "Minister of State (Department for Work and Pensions)"

    def test_post_rank(self):
        assert ministers.post_rank("Secretary of State for Education") == 0
        assert ministers.post_rank("Minister of State (Education)") == 1
        assert ministers.post_rank("Parliamentary Under-Secretary of State (Minister for Children)") == 2
        assert ministers.post_rank("Minister of State (Minister for Women and Equalities)") == 5
        assert ministers.post_rank("Lords Spokesperson (Equalities)") == 5
        assert ministers.post_rank("Deputy Leader of the House of Commons") == 1
        assert ministers.post_rank("Deputy Leader of the House of Lords") == 1
        assert ministers.post_rank("Leader of the House of Commons") == 0
        assert ministers.post_rank("Deputy Prime Minister") == 0

    def test_treasury_answering_body_name(self, app):
        _member(4264, "Rachel Reeves", posts=CHANCELLOR_POSTS, constituency="Leeds West and Pudsey")
        _member(4077, "Emma Reynolds", posts=REYNOLDS_POSTS, constituency="Wycombe")
        assert ministers.pq_answering_role(4264, "Treasury", date(2026, 9, 1)) == "Chancellor of the Exchequer"
        assert ministers.pq_answering_role(4077, "Treasury", date(2026, 9, 1)) == \
            "Chief Secretary to the Treasury"

    def test_treasury_from_gov_minister_only(self, app):
        _gov_row(4264, "Rachel Reeves", "HM Treasury", "Chancellor of the Exchequer", date(2024, 7, 5))
        assert ministers.pq_answering_role(4264, "Treasury", date(2026, 9, 1)) == "Chancellor of the Exchequer"

    def test_department_aliases(self):
        k = ministers.department_key
        assert k("Treasury") == k("HM Treasury")
        assert k("Leader of the House") == k("Office of the Leader of the House of Commons")
        assert k("Leader of the House of Lords") == k("Office of the Leader of the House of Lords")
        assert k("Attorney General's Office") == k("Attorney General’s Office")
        assert k("Women and Equalities") == k("Government Equalities Office")

    def test_one_query_for_many_members(self, app):
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS)
        _member(1, "Backbencher", posts=[])
        _member(2, "Never fetched", posts=None, detailed=False)
        h = ministers.post_histories([4653, 1, 2, None])
        assert set(h) == {4653, 1}
        assert ministers.post_on_date(1, "2026-01-01", histories=h) is None


# ── Pages ─────────────────────────────────────────────────────────────────────

def _answered_pq(uin, answering_id, answer_date, asking_id=None, asking_name=None,
                 answering_name="Stephen Morgan", body=DEFRA):
    from extensions import db
    from hansard_archive.models import HaPQ
    db.session.add(HaPQ(uin=uin, question_text="To ask the Secretary of State, what steps are being taken.",
                        tabled_date=date(2026, 6, 10), chamber="Commons", is_answered=True,
                        answer_text="<p>An answer.</p>", answer_date=answer_date,
                        asking_mnis_id=asking_id, asking_member=asking_name,
                        answering_mnis_id=answering_id, answering_member=answering_name,
                        answering_body=body))
    db.session.commit()


class TestPqPage:
    def test_whip_title_gone_post_on_answer_date_shown(self, app, no_http):
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS, stale_role=STALE_WHIP)
        _answered_pq("11212", 4653, date(2026, 10, 16))
        html = app.test_client().get("/archive/pq/11212").get_data(as_text=True)
        assert "Government Whip" not in html
        assert f"Parliamentary Under-Secretary ({DEFRA})" in html

    def test_meta_description_uses_cached_name(self, app):
        _member(5299, "Monica Harding", posts=[], party="Liberal Democrat", constituency="Esher and Walton")
        _answered_pq("820", None, date(2026, 5, 20), asking_id=5299, asking_name=None, answering_name=None)
        html = app.test_client().get("/archive/pq/820").get_data(as_text=True)
        assert "tabled by Monica Harding to" in html

    def test_no_dated_record_shows_constituency_not_stale_role(self, app):
        _member(4653, "Stephen Morgan", posts=None, detailed=False, stale_role=STALE_WHIP)
        _answered_pq("19869", 4653, date(2026, 9, 1))
        html = app.test_client().get("/archive/pq/19869").get_data(as_text=True)
        assert "Government Whip" not in html
        assert "MP for Portsmouth South" in html

    def test_missing_names_filled_from_our_member_cache(self, app):
        _member(5299, "Monica Harding", posts=[], party="Liberal Democrat", constituency="Esher and Walton")
        _member(4520, "Matthew Pennycook", posts=[], constituency="Greenwich and Woolwich")
        _answered_pq("816", 4520, date(2026, 5, 20), asking_id=5299, asking_name=None,
                     answering_name=None, body="Ministry of Housing, Communities and Local Government")
        html = app.test_client().get("/archive/pq/816").get_data(as_text=True)
        assert '<span class="sd-speaker-name">Monica Harding</span>' in html
        assert '<span class="sd-speaker-name">Matthew Pennycook</span>' in html


# ── Research Tool and Written Questions Scanner rows ──────────────────────────

class TestResearchToolRows:
    def test_speech_gets_post_held_on_speech_date(self, app, monkeypatch):
        import debate_scanner
        _member(491, "Angela Eagle", posts=EAGLE_POSTS, constituency="Wallasey")
        monkeypatch.setattr(debate_scanner, "get_minister_list", lambda: {
            "by_norm": {"angela eagle": f"Secretary of State for {DEFRA[15:]}"}})
        rows = [{"speaker_name": "Angela Eagle", "member_id": 491, "hdate": "2026-01-21", "dept": ""},
                {"speaker_name": "Angela Eagle", "member_id": 491, "hdate": "2026-07-01", "dept": ""},
                {"speaker_name": "Angela Eagle", "member_id": 491, "hdate": "2026-09-01", "dept": ""}]
        debate_scanner.flag_minister_rows(rows)
        assert rows[0]["minister_role"] == f"Minister of State ({DEFRA})" and rows[0]["is_minister"]
        # between posts: not a minister that day, though she is one now
        assert rows[1]["minister_role"] == "" and rows[1]["is_minister"] is False
        assert rows[2]["minister_role"].startswith("Secretary of State") and rows[2]["is_minister"]

    def test_speaker_without_dated_record_keeps_flag_but_no_projected_title(self, app, monkeypatch):
        import debate_scanner
        monkeypatch.setattr(debate_scanner, "get_minister_list", lambda: {
            "by_norm": {"chris bryant": "Cabinet Minister"}})
        rows = [{"speaker_name": "Chris Bryant", "member_id": None, "hdate": "2026-05-20"}]
        debate_scanner.flag_minister_rows(rows)
        assert rows[0]["is_minister"] is True and rows[0]["minister_role"] == ""
        assert rows[0]["minister_unverified"] is True

    def test_no_history_label_only_after_gov_minister_start(self, app, monkeypatch):
        import debate_scanner
        _gov_row(5305, "Georgia Gould", DFE, "Minister of State (Education)", date(2025, 9, 6))
        monkeypatch.setattr(debate_scanner, "get_minister_list", lambda: {
            "by_norm": {"georgia gould": "Minister of State (Education)"}})
        rows = [{"speaker_name": "Georgia Gould", "member_id": 5305, "hdate": "2025-03-01"},
                {"speaker_name": "Georgia Gould", "member_id": 5305, "hdate": "2026-03-01"}]
        debate_scanner.flag_minister_rows(rows)
        assert rows[0]["is_minister"] and rows[0]["minister_unverified"] and rows[0]["minister_role"] == ""
        assert rows[1]["minister_role"] == "Minister of State (Education)"
        assert rows[1]["minister_unverified"] is False

    def test_smith_of_malvern_research_rows(self, app, monkeypatch):
        import debate_scanner
        _member(269, "Baroness Smith of Malvern", house="Lords", posts=SMITH_POSTS)
        monkeypatch.setattr(debate_scanner, "get_minister_list", lambda: {"by_norm": {}})
        rows = [{"speaker_name": "Baroness Smith of Malvern", "member_id": 269, "hdate": d, "dept": dept}
                for d in ("2025-01-15", "2025-06-10", "2026-01-20", "2026-09-10") for dept in (DFE, "")]
        debate_scanner.flag_minister_rows(rows)
        assert {r["minister_role"] for r in rows} == {"Minister of State (Education)"}

    def test_unverified_minister_not_counted_for_group_badge(self):
        import debate_scanner
        unverified = {"speaker_name": "A", "is_minister": True, "minister_unverified": True,
                      "debate_title": "Cats", "hdate": "2026-05-20", "listurl": "u1"}
        verified = dict(unverified, speaker_name="B", minister_unverified=False, listurl="u2")
        groups = debate_scanner._group_by_debate([unverified])
        assert groups and not any(g["has_minister"] for g in groups)
        groups = debate_scanner._group_by_debate([unverified, verified])
        assert any(g["has_minister"] for g in groups)

    def test_unverified_minister_has_no_label_on_page(self):
        from flask import Flask
        a = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
        src = (PROJECT_ROOT / "templates" / "debate_scanner.html").read_text(encoding="utf-8")
        lines = src.splitlines()
        i = next(n for n, ln in enumerate(lines) if "debate-speech-role" in ln and "r.minister_role" in ln)
        tpl = a.jinja_env.from_string(lines[i].strip() + lines[i + 1].strip())  # if / elif ... endif
        with a.app_context():
            unverified = tpl.render(r={"is_minister": True, "minister_unverified": True,
                                       "minister_role": "", "speaker_party": "Lab"})
            verified = tpl.render(r={"is_minister": True, "minister_unverified": False,
                                     "minister_role": "Minister of State (Education)"})
        assert "Minister" not in unverified and "Lab" in unverified
        assert "Minister of State (Education)" in verified

    def test_wq_scanner_batch_makes_one_history_query(self, app, monkeypatch):
        import hansard
        import ministers as m
        from hansard_archive.models import HaPQ
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS)
        for i in range(5):
            _answered_pq(f"3{i}", 4653, date(2026, 10, 1 + i))
        calls = []
        real = m.post_histories
        monkeypatch.setattr(m, "post_histories", lambda ids: calls.append(ids) or real(ids))
        rows = HaPQ.query.all()
        histories = hansard._answer_histories(rows)
        results = [hansard._pq_result(pq, histories) for pq in rows]
        assert len(calls) == 1
        assert {r["answering_minister_role"] for r in results} == {f"Parliamentary Under-Secretary ({DEFRA})"}

    def test_wq_scanner_row_role_on_answer_date(self, app):
        from hansard import _pq_result
        from hansard_archive.models import HaPQ
        _member(4653, "Stephen Morgan", posts=MORGAN_POSTS, stale_role=STALE_WHIP)
        _answered_pq("24664", 4653, date(2026, 6, 25), answering_name=None)
        r = _pq_result(HaPQ.query.filter_by(uin="24664").one())
        assert r["answering_minister_role"] == f"Minister of State ({DEFRA})"
        assert r["answering_minister"] == "Stephen Morgan"
