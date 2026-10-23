"""
/brief/<slug> topic pages: what they list and in what order.

A topic page exists when a stored tag spells the topic exactly (as before). It
then also lists items whose tags contain the topic phrase, whose title or
heading contains it, and other-House twins (Lords repeats) of those. Sessions of
many unrelated questions (PMQs etc.) come after the debates on the topic.
Overview shows the first 25 of each list with "See all"; ?show= pages one list.
SQLite in memory, no network.
"""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from hansard_archive import topic_match as tm

PROJECT_ROOT = Path(__file__).parents[1]
_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


# ── Pure matching ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug, text, hit", [
    ("cats", "Cats: Animal Welfare", True),
    ("cats", "Animal Welfare (Import of Dogs, Cats and Ferrets) Bill", True),
    ("cats", "Catapults and Antisocial Behaviour", False),
    ("ukraine", "US-Russia Deal on Diesel Exports: Effect on Ukraine", True),
    ("ukraine", "Ukrainian Refugees", False),
    ("animal-welfare", "Animal Welfare Strategy for England", True),
    ("animal-welfare", "Welfare Reform", False),
    ("special-educational-needs", "Local Authority SEND Services", True),
    ("special-educational-needs", "Special Educational Needs: Dyscalculia", True),
    ("special-educational-needs", "EHCPs: Delays", True),
    ("special-educational-needs", "Sending Asylum Seekers Abroad", False),
    ("special-educational-needs", "Send a Signal", False),     # verb, not the acronym
])
def test_title_matching(slug, text, hit):
    assert tm.title_matches(text, tm.expandable(tm.phrases_for_slug(slug))) is hit


@pytest.mark.parametrize("slug, tag, hit", [
    ("ukraine", "support for ukraine", True),
    ("ukraine", "russia-ukraine war", True),
    ("cats", "cat welfare", True),
    ("cats", "catapults", False),
    ("animal-welfare", "farm animal welfare", True),
    ("animal-welfare", "welfare reform", False),
    ("special-educational-needs", "send system reform", True),   # tags are lowercase
    ("womens-pensions", "women's pensions compensation", True),
])
def test_tag_containment(slug, tag, hit):
    assert tm.tag_matches(tag, tm.expandable(tm.phrases_for_slug(slug))) is hit


@pytest.mark.parametrize("tag", [
    "send", "send system reform", "unmet send need", "sending", "consent", "pre-send checks",
    "special educational needs funding", "ehcp delays", "sentencing and diversion",
])
def test_postgres_prefilter_regex_agrees_with_like_patterns(tag):
    """The one-regex pre-filter used on Postgres keeps the same tags as the LIKEs."""
    from fnmatch import fnmatchcase
    ps = tm.expandable(tm.phrases_for_slug("special-educational-needs"))
    like = any(fnmatchcase(tag, x.replace("%", "*")) for p in ps for x in p.tag_like_patterns())
    assert bool(re.search(tm.tag_pg_regex(ps), tag)) is like


def test_generic_and_short_phrases_are_not_expanded():
    assert tm.expandable(tm.phrases_for_slug("bill")) == []
    assert tm.expandable(tm.phrases_for_slug("nhs")) == []        # too short for LIKE
    sen = {p.text for p in tm.expandable(tm.phrases_for_slug("special-educational-needs"))}
    assert "send" in sen and "send provision" not in sen          # implied by "send"


# ── Pages ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager

    from extensions import db as _db
    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    from hansard_archive.models import (
        HansardSession, HansardSessionTheme, HaPQ, HaPQTheme,
        THEME_TYPE_POLICY_AREA, THEME_TYPE_SPECIFIC,
    )
    from hansard_archive import views
    from hansard_archive.slugs import slugify_theme
    from feature_flags import feature_enabled

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"),
                     static_folder=str(PROJECT_ROOT / "static"))
    test_app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True, "SECRET_KEY": "test", "LOGIN_DISABLED": True,
    })
    _db.init_app(test_app)
    LoginManager(test_app).user_loader(lambda uid: None)
    test_app.context_processor(lambda: {"admin_authenticated": False, "app_version": "test"})
    test_app.jinja_env.filters["slugify"] = slugify_theme
    test_app.jinja_env.globals["feature_enabled"] = feature_enabled
    test_app.register_blueprint(views.archive_bp)
    test_app.register_blueprint(views.brief_bp)

    n = [0]

    def sess(title, d, house="Commons", dtype="debate"):
        n[0] += 1
        s = HansardSession(ext_id=f"tp-{n[0]}", title=title, date=d, house=house,
                           debate_type=dtype, slug=f"tp-session-{n[0]}",
                           contributions_ingested=True, is_container=False)
        _db.session.add(s)
        _db.session.flush()
        return s

    def pq(uin, heading, d, question="A question."):
        p = HaPQ(uin=uin, heading=heading, question_text=question, tabled_date=d,
                 answering_body="Department", is_answered=True)
        _db.session.add(p)
        _db.session.flush()
        return p

    def stag(s, theme, kind=THEME_TYPE_SPECIFIC):
        _db.session.add(HansardSessionTheme(session_id=s.id, theme=theme, theme_type=kind))

    def ptag(p, theme, kind=THEME_TYPE_SPECIFIC):
        _db.session.add(HaPQTheme(pq_id=p.id, theme=theme, theme_type=kind))

    with test_app.app_context():
        _db.create_all()
        views._topic_match_cache.clear()

        # Ukraine: one exact tag makes the page exist; the rest is found by
        # containment, title, heading.
        ptag(pq("UKR01", "Ukraine: Ceasefires", date(2026, 1, 8)), "ukraine")
        ptag(pq("UKR02", None, date(2026, 6, 1), "What military aid has been given?"),
             "military aid to ukraine")
        pq("UKR03", "Ukraine: Military Aid", date(2026, 7, 1))                  # untagged
        ptag(pq("UKR04", "Ukrainian Refugees", date(2026, 7, 2)), "refugee housing")
        for i in range(28):                                                     # pager
            ptag(pq(f"UKRB{i:02d}", f"Ukraine: Aid {i}", date(2025, 1, 1) + timedelta(days=i)),
                 "ukraine")
        stag(sess("Ukraine", date(2026, 9, 1), dtype="oral_questions"), "support for ukraine")
        sess("Ukraine and Russia", date(2026, 9, 15), house="Lords", dtype="other")  # untagged
        stag(sess("Russian Shadow Fleet", date(2026, 6, 15), dtype="ministerial_statement"),
             "russia-ukraine war")
        stag(sess("Engagements", date(2026, 10, 1), dtype="pmqs"), "ukraine")      # newest, PMQs
        sess("Ukrainian Culture", date(2026, 5, 1))                              # no match

        # SEND: Commons statement tagged; its Lords repeat untagged a day later.
        wp = "Schools White Paper: Every Child Achieving and Thriving"
        stag(sess(wp, date(2026, 2, 23), dtype="ministerial_statement"), "send reforms")
        sess(wp, date(2026, 2, 24), house="Lords", dtype="other")
        sess(wp, date(2026, 6, 1), house="Lords", dtype="other")                 # too far apart
        stag(sess("Local Authority SEND Services", date(2026, 9, 14), dtype="oral_questions"),
             "send system reform")
        stag(sess("Sending Asylum Seekers Abroad", date(2026, 9, 2)), "asylum returns")
        ptag(pq("SEN01", "Special Educational Needs: Transport", date(2026, 10, 1)), "send")

        # Cats
        ptag(pq("CAT01", "Cats: Animal Welfare", date(2026, 3, 10)), "cats")
        ptag(pq("CAT02", "Pets: Microchips", date(2026, 3, 11)), "cat microchipping")
        sess("Animal Welfare (Import of Dogs, Cats and Ferrets) Bill", date(2025, 7, 4))
        stag(sess("Catapults and Antisocial Behaviour", date(2025, 12, 2)), "catapults")

        # Policy-area page ordering: PMQs newest, debate older.
        stag(sess("Engagements", date(2026, 10, 8), dtype="pmqs"), "Transport", THEME_TYPE_POLICY_AREA)
        stag(sess("Rail Franchising", date(2026, 3, 3)), "Transport", THEME_TYPE_POLICY_AREA)
        _db.session.commit()
        yield test_app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _titles(html):
    return re.findall(r'class="ah-card-title" href="[^"]+">([^<]+)<', html)


def _hrefs(html):
    return re.findall(r'class="ah-card-title" href="([^"]+)"', html)


class TestUkraine:
    def test_debates_found_by_containment_title_and_house(self, client):
        html = client.get("/brief/ukraine").get_data(as_text=True)
        titles = _titles(html)
        assert "Ukraine and Russia" in titles            # untagged Lords debate, by title
        assert "Russian Shadow Fleet" in titles          # tag contains "ukraine"
        assert "Ukrainian Culture" not in titles
        assert "4 sessions and 31 written questions on this topic" in html

    def test_pmqs_after_debates(self, client):
        titles = _titles(client.get("/brief/ukraine").get_data(as_text=True))
        debates = titles[:titles.index("Engagements") + 1]
        assert debates == ["Ukraine and Russia", "Ukraine", "Russian Shadow Fleet", "Engagements"]

    def test_questions_by_tag_containment_and_heading(self, client):
        hrefs = _hrefs(client.get("/brief/ukraine").get_data(as_text=True))
        assert "/archive/pq/ukr02" in hrefs              # tag "military aid to ukraine"
        assert "/archive/pq/ukr03" in hrefs              # untagged, heading "Ukraine: ..."
        assert "/archive/pq/ukr04" not in hrefs          # "Ukrainian" is not "Ukraine"

    def test_overview_caps_lists_with_see_all(self, client):
        html = client.get("/brief/ukraine").get_data(as_text=True)
        assert len([h for h in _hrefs(html) if h.startswith("/archive/pq/")]) == 25
        assert 'href="/brief/ukraine?show=questions">See all 31 written questions' in html
        assert "?show=debates" not in html               # all 4 debates already shown
        assert _INDEX in html

    def test_show_questions_pages(self, client):
        r = client.get("/brief/ukraine?show=questions&page=2")
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert len(_hrefs(html)) == 6
        assert _NOINDEX in html
        assert "Back to" in html
        assert client.get("/brief/ukraine?show=questions&page=3").status_code == 404

    def test_legacy_page_param_redirects_to_a_list(self, client):
        r = client.get("/brief/ukraine?page=2")
        assert r.status_code == 301
        assert r.headers["Location"].endswith("/brief/ukraine?show=questions&page=2")
        assert client.get("/brief/ukraine?page=9").status_code == 404


class TestSend:
    def test_lords_repeat_of_matched_statement_is_listed(self, client):
        html = client.get("/brief/special-educational-needs").get_data(as_text=True)
        wp = "Schools White Paper: Every Child Achieving and Thriving"
        assert _titles(html).count(wp) == 2              # Commons + Lords repeat, not the June one
        assert "Local Authority SEND Services" in _titles(html)
        assert "Sending Asylum Seekers Abroad" not in _titles(html)

    def test_show_debates_lists_only_debates(self, client):
        html = client.get("/brief/special-educational-needs?show=debates").get_data(as_text=True)
        assert "Local Authority SEND Services" in _titles(html)
        assert not any(h.startswith("/archive/pq/") for h in _hrefs(html))


class TestCats:
    def test_cat_page_finds_related_tags_and_titles(self, client):
        html = client.get("/brief/cats").get_data(as_text=True)
        titles = _titles(html)
        assert "Animal Welfare (Import of Dogs, Cats and Ferrets) Bill" in titles
        assert "Catapults and Antisocial Behaviour" not in titles
        assert "/archive/pq/cat02" in _hrefs(html)       # tag "cat microchipping"


class TestExistenceUnchanged:
    def test_title_match_alone_does_not_create_a_page(self, client):
        """No stored tag spells "ferrets": still a 404, though a title contains it."""
        assert client.get("/brief/ferrets").status_code == 404

    def test_show_list_with_no_items_is_404(self, client):
        assert client.get("/brief/cats?show=debates&page=2").status_code == 404


def test_policy_page_lists_pmqs_after_debates(client):
    titles = _titles(client.get("/brief/transport").get_data(as_text=True))
    assert titles == ["Rail Franchising", "Engagements"]
