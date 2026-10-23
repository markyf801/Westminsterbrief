"""Archive search precision: member names, phrase-first ranking, WQ filters.

Most tests inspect the SQL the Postgres paths send (the suite runs on SQLite).
TestOnLocalPostgres runs the real SQL, but only when WB_TEST_POSTGRES_URL points
at a local scratch database, e.g.
    WB_TEST_POSTGRES_URL=postgresql://user@localhost:5432/scratch
It refuses any non-local host.
"""

import os
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from hansard_archive import search_terms as st

PROJECT_ROOT = Path(__file__).parents[1]


# ── search_terms ──────────────────────────────────────────────────────────────

def test_parse_query():
    assert st.parse_query('"chicken welfare"') == ("phraseto_tsquery", "chicken welfare")
    assert st.parse_query("chicken welfare") == ("plainto_tsquery", "chicken welfare")
    assert st.parse_query('""') == ("plainto_tsquery", '""')


def test_phrase_regex_escapes_and_allows_either_apostrophe():
    assert st._phrase_regex("Cat Smith") == r"Cat\s+Smith"
    assert st._phrase_regex("Liz Saville Roberts") == r"Liz\s+Saville\s+Roberts"
    assert st._phrase_regex("Dr. Who (Test)") == r"Dr\.\s+Who\s+\(Test\)"
    assert st._phrase_regex("Fergus O'Hara") == r"Fergus\s+O['’]Hara"


def test_query_words_drop_titles():
    assert st._query_words("House of Lords reform") == ["house", "reform"]
    assert st._query_words("cats") == ["cats"]
    assert st._query_words("2026") == []


_LEXEMES = {"cats": "'cat'", "kinnoull": "'kinnoul'", "welfare": "'welfar'"}


def _lookup_with(rows, q):
    """_lookup with member_profile holding `rows` and Postgres's stemmer faked."""
    words = st._query_words(q)
    lexemes = tuple(_LEXEMES.get(w, f"'{w}'") for w in words)
    st._index.update(at=None, entries=())
    with patch.object(st.db.session, "execute") as ex:
        ex.side_effect = [MagicMock(fetchone=lambda: lexemes),
                          MagicMock(fetchall=lambda: rows)]
        guard = st._lookup(words, q)
    st._index.update(at=None, entries=())
    return guard


_CAT_ROWS = [
    ("name", "Cat Smith", "Cat Smith", "'cat' <-> 'smith'"),
    ("name", "Cat Eccles", "Cat Eccles", "'cat' <-> 'eccl'"),
]


def test_lookup_builds_guard_for_member_names():
    guard = _lookup_with(_CAT_ROWS, "cats")
    assert guard.tsquery == "( 'cat' <-> 'eccl' ) | ( 'cat' <-> 'smith' )"
    assert guard.pattern == r"\m(?:Cat\s+(?:Eccles|Smith))\M"
    assert guard.word_patterns == (r"\m(?:cats|cat)(?:s|es|['’]s|['’])?\M",)


def test_lookup_only_rechecks_words_that_are_in_names():
    guard = _lookup_with(_CAT_ROWS, "cat breeding")
    assert guard.word_patterns == (r"\m(?:cat)(?:s|es|['’]s|['’])?\M",)


def test_names_regex_groups_by_first_word():
    assert st._names_regex({"Cat Smith", "Cat Eccles", "Bill Esterson", "Stansgate"}) == (
        r"Bill\s+Esterson|Cat\s+(?:Eccles|Smith)|Stansgate")


def test_lookup_keeps_a_member_the_search_names():
    guard = _lookup_with(_CAT_ROWS, "cat smith")
    assert "smith" not in guard.tsquery and "Smith" not in guard.pattern
    assert r"Cat\s+Eccles" in guard.pattern


def test_lookup_seats_and_titles():
    rows = [
        ("seat", "Matt Bishop", "Forest of Dean", "'forest' <2> 'dean'"),
        ("name", "The Earl of Kinnoull", "of Kinnoull", "'kinnoul'"),
        ("name", "Lord Black of Brentwood", "Black of Brentwood", "'black' <2> 'brentwood'"),
    ]
    guard = _lookup_with(rows, "forest black kinnoull")
    assert r"(?:Members?|MP)\s+for\s+(?:Forest\s+of\s+Dean)" in guard.pattern
    assert r"Black\s+of\s+Brentwood" in guard.pattern
    assert "Kinnoull" not in guard.pattern          # a territorial title, not a name
    # Naming the seat, or the peer with their title, keeps them searchable
    assert _lookup_with(rows[:1], "forest of dean") is None
    assert _lookup_with(rows[2:], "lord black") is None
    assert _lookup_with(rows[2:], "black") is not None


def test_lookup_none_when_no_member_matches():
    assert _lookup_with([], "chicken welfare") is None


def test_guard_sql_rechecks_only_rows_that_mention_a_name():
    guard = st.NameGuard(tsquery="( 'cat' <-> 'smith' )", pattern=r"\mCat\s+Smith\M",
                         word_patterns=("w0", "w1"))
    sql = guard.sql("c.speech_tsv", "c.speech_text")
    assert sql == (
        "AND (NOT (c.speech_tsv @@ CAST(:ng_q AS tsquery)) OR ("
        "regexp_replace(c.speech_text, :ng_re, ' xname ', 'g') ~* :ng_w0 AND "
        "regexp_replace(c.speech_text, :ng_re, ' xname ', 'g') ~* :ng_w1))")
    assert guard.params() == {"ng_q": guard.tsquery, "ng_re": guard.pattern,
                              "ng_w0": "w0", "ng_w1": "w1"}


def test_name_guard_is_cached_and_survives_errors(app_ctx):
    st._cache.clear()
    with patch.object(st, "_lookup", return_value=None) as lk:
        st.name_guard("cats")
        st.name_guard("cats")
    assert lk.call_count == 1
    st._cache.clear()
    with patch.object(st, "_lookup", side_effect=RuntimeError("no table")):
        assert st.name_guard("dogs") is None
    st._cache.clear()


# ── SQL sent by the archive search ────────────────────────────────────────────

_GUARD = st.NameGuard(tsquery="( 'cat' <-> 'smith' )", pattern=r"\mCat\s+Smith\M",
                      word_patterns=(r"\mcats?\M",))


def _sql_calls(fn, *args, guard=None, **kw):
    from hansard_archive import views
    with patch.object(views.db.session, "execute") as ex, \
            patch.object(views._st, "name_guard", return_value=guard):
        ex.return_value = MagicMock(scalar=lambda: 0, fetchall=lambda: [])
        fn(*args, **kw)
        return [(str(c.args[0]), c.args[1]) for c in ex.call_args_list]


def test_debate_search_applies_name_guard_and_phrase_first(app_ctx):
    from hansard_archive import views
    (count_sql, count_p), (rows_sql, rows_p) = _sql_calls(
        views._fts_search, "cats", 1, guard=_GUARD)
    for sql, params in ((count_sql, count_p), (rows_sql, rows_p)):
        assert "CAST(:ng_q AS tsquery)" in sql
        assert params["ng_re"] == _GUARD.pattern
    assert "ORDER BY phrase_hit DESC, final_rank DESC, date DESC" in rows_sql
    assert "phraseto_tsquery('english', :q)" in rows_sql
    assert "LN(1 + COALESCE(ps.hits, 0))" in rows_sql
    # Speech text is only read for the page of results
    assert "LEFT JOIN ha_contribution bc ON bc.id = r.best_id" in rows_sql


def test_debate_search_without_guard_or_title_only(app_ctx):
    from hansard_archive import views
    calls = _sql_calls(views._fts_search, "chicken welfare", 1)
    assert all(":ng_q" not in sql for sql, _ in calls)
    with patch.object(views._st, "name_guard") as ng:
        _sql_calls(views._fts_search, "cats", 1, title_only=True, guard=None)
        ng.assert_not_called()


def test_quoted_debate_search_stays_a_phrase_search(app_ctx):
    from hansard_archive import views
    (_, p), (rows_sql, _) = _sql_calls(views._fts_search, '"gateway 2"', 1)
    assert p["q"] == "gateway 2"
    assert "c.speech_tsv @@ phraseto_tsquery('english', :q)" in rows_sql


def test_pq_search_and_count_honour_dates_house_and_guard(app_ctx):
    from hansard_archive import views
    f = dict(date_from="2026-05-01", date_to="2026-10-23", house_filter="Lords")
    [(sql, p)] = _sql_calls(views._pq_fts_search, "ukraine", guard=_GUARD, **f)
    [(count_sql, cp)] = _sql_calls(views._pq_fts_count, "ukraine", guard=_GUARD, **f)
    for s, params in ((sql, p), (count_sql, cp)):
        assert "p.tabled_date >= :pq_from" in s and "p.tabled_date <= :pq_to" in s
        assert "p.chamber = :pq_house" in s
        assert "CAST(:ng_q AS tsquery)" in s
        assert (params["pq_from"], params["pq_to"], params["pq_house"]) == (
            "2026-05-01", "2026-10-23", "Lords")
    assert "ORDER BY phrase_hit DESC, rank DESC" in sql


def test_pq_search_without_filters_has_no_filter_sql(app_ctx):
    from hansard_archive import views
    [(sql, p)] = _sql_calls(views._pq_fts_search, "ukraine")
    assert ":pq_from" not in sql and ":pq_house" not in sql and ":ng_q" not in sql


# ── /archive/search route ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db
    import hansard_archive.models  # noqa: F401
    from hansard_archive.models import HaPQ
    from hansard_archive.views import archive_bp
    from hansard_archive.slugs import slugify_theme
    from feature_flags import feature_enabled

    a = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    a.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", TESTING=True,
                    SECRET_KEY="t", LOGIN_DISABLED=True)
    db.init_app(a)
    lm = LoginManager()
    lm.init_app(a)
    lm.user_loader(lambda uid: None)
    a.context_processor(lambda: {"admin_authenticated": False, "app_version": "test"})
    a.jinja_env.filters["slugify"] = slugify_theme
    a.jinja_env.globals["feature_enabled"] = feature_enabled
    a.register_blueprint(archive_bp)
    with a.app_context():
        db.create_all()
        db.session.add_all([
            HaPQ(uin="UKR1", heading="Ukraine: Loans", question_text="Ukraine loan question",
                 tabled_date=date(2026, 10, 9), is_answered=True, chamber="Commons"),
            HaPQ(uin="UKR2", heading="Ukraine: Military Aid", question_text="Ukraine aid question",
                 tabled_date=date(2025, 6, 1), is_answered=True, chamber="Commons"),
            HaPQ(uin="HLUKR", heading="Ukraine: Refugees", question_text="Ukraine refugees question",
                 tabled_date=date(2026, 7, 1), is_answered=True, chamber="Lords"),
        ])
        db.session.commit()
    yield a
    with a.app_context():
        db.drop_all()


@pytest.fixture
def app_ctx(app):
    with app.app_context():
        yield


def _pq_uins(html):
    import re
    return sorted(u.upper() for u in re.findall(r'href="/archive/pq/([a-z0-9]+)"', html))


def test_wq_tab_honours_the_date_filter(app):
    c = app.test_client()
    every = c.get("/archive/search?q=ukraine&tab=pq").get_data(as_text=True)
    dated = c.get("/archive/search?q=ukraine&tab=pq&from=2026-05-01&to=2026-10-23").get_data(as_text=True)
    lords = c.get("/archive/search?q=ukraine&tab=pq&house=Lords").get_data(as_text=True)
    assert _pq_uins(every) == ["HLUKR", "UKR1", "UKR2"]
    assert _pq_uins(dated) == ["HLUKR", "UKR1"]
    assert _pq_uins(lords) == ["HLUKR"]


def test_invalid_date_is_ignored_not_an_error(app):
    html = app.test_client().get("/archive/search?q=ukraine&tab=pq&from=bogus").get_data(as_text=True)
    assert "temporarily unavailable" not in html
    assert _pq_uins(html) == ["HLUKR", "UKR1", "UKR2"]


def test_postgres_route_passes_filters_to_wq_search(app):
    from hansard_archive import views
    pq = {"uin": "X1", "heading": "h", "asking_member": "", "answering_body": "",
          "is_answered": True, "url": "/archive/pq/x1", "human_date": "",
          "snippet": None, "question_text": ""}
    with (
        patch("hansard_archive.views._is_postgres", return_value=True),
        patch("hansard_archive.views._fts_search", return_value=([], 0)),
        patch("hansard_archive.views._pq_fts_search", return_value=[pq] * 20) as mock_pq,
        patch("hansard_archive.views._pq_fts_count", return_value=226) as mock_count,
        patch("hansard_archive.views._all_policy_areas", return_value=[]),
    ):
        html = app.test_client().get(
            "/archive/search?q=ukraine&from=2026-05-01&to=2026-10-23&house=Commons"
        ).get_data(as_text=True)
    expected = dict(date_from="2026-05-01", date_to="2026-10-23",
                    house_filter="Commons", policy_filter="")
    for mock in (mock_pq, mock_count):
        assert {k: mock.call_args.kwargs[k] for k in expected} == expected
    assert ('href="/written-questions?q=ukraine&amp;start_date=2026-05-01'
            '&amp;end_date=2026-10-23&amp;house_filter=Commons"') in html
    assert views._wq_scanner_qs("x", "", "", "") == "q=x"


# ── Real SQL on a local scratch Postgres (opt-in) ─────────────────────────────

_PG_URL = os.environ.get("WB_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="set WB_TEST_POSTGRES_URL to a local scratch Postgres")
class TestOnLocalPostgres:
    @pytest.fixture(scope="class")
    def pg(self):
        from flask import Flask
        from sqlalchemy import text
        from extensions import db
        import hansard_archive.models  # noqa: F401
        import cache_models  # noqa: F401

        assert urlparse(_PG_URL).hostname in ("localhost", "127.0.0.1", "::1"), \
            "WB_TEST_POSTGRES_URL must be a local scratch database"
        a = Flask(__name__)
        a.config.update(SQLALCHEMY_DATABASE_URI=_PG_URL, TESTING=True)
        db.init_app(a)
        with a.app_context():
            db.create_all()
            for stmt in (
                "ALTER TABLE ha_session ADD COLUMN IF NOT EXISTS title_tsv tsvector GENERATED"
                " ALWAYS AS (to_tsvector('english', coalesce(title, ''))) STORED",
                "ALTER TABLE ha_contribution ADD COLUMN IF NOT EXISTS speech_tsv tsvector GENERATED"
                " ALWAYS AS (to_tsvector('english', coalesce(speech_text, ''))) STORED",
                "ALTER TABLE ha_pq ADD COLUMN IF NOT EXISTS question_tsv tsvector GENERATED"
                " ALWAYS AS (to_tsvector('english', coalesce(heading, '') || ' ' ||"
                " coalesce(question_text, '') || ' ' || coalesce(answer_text, ''))) STORED",
                "TRUNCATE ha_contribution, ha_session_theme, ha_session, ha_pq_theme, ha_pq CASCADE",
                "DELETE FROM member_profile",
            ):
                db.session.execute(text(stmt))
            db.session.execute(text("""
                INSERT INTO member_profile (member_id, slug, name, house, membership_from, is_current)
                VALUES (1, 'a', 'Cat Smith', 'Commons', 'Lancaster and Wyre', true),
                       (2, 'b', 'Cat Eccles', 'Commons', 'Stourbridge', true)"""))
            sessions = [
                (1, "Animal Rescue Centres", "2026-01-26",
                 ["Rescue centres for cats need regulation.", "Many cats are abandoned."]),
                (2, "Student Finance (Review)", "2026-09-15",
                 ["Ordered, That Ann Davies, Cat Eccles, Cat Smith be members of the Committee."]),
                (3, "Waste Management Sites", "2026-07-15",
                 ["The Member for Lancaster and Wyre (Cat Smith) has constituents nearby."]),
                (4, "Pets", "2026-10-01",
                 ["As the Member for Stourbridge (Cat Eccles) said, stray cats are a problem."]),
                (5, "VE Day", "2026-10-06",
                 ["They worked in aircraft factories and on the farms."]),
                (6, "State of Climate", "2025-07-21", ["A contributory factor is factory farming."]),
            ]
            for sid, title, d, speeches in sessions:
                db.session.execute(text(
                    "INSERT INTO ha_session (id, ext_id, title, date, house, debate_type,"
                    " contributions_ingested, is_container, slug) VALUES"
                    " (:id, :e, :t, :d, 'Commons', 'debate', true, false, :s)"),
                    {"id": sid, "e": f"e{sid}", "t": title, "d": d, "s": f"s{sid}"})
                for i, sp in enumerate(speeches):
                    db.session.execute(text(
                        "INSERT INTO ha_contribution (session_id, speech_text, speech_order)"
                        " VALUES (:s, :t, :o)"), {"s": sid, "t": sp, "o": i})
            db.session.execute(text("""
                INSERT INTO ha_pq (uin, heading, question_text, tabled_date, is_answered,
                                   is_holding, is_withdrawn, chamber)
                VALUES ('P1', 'Cats', 'Cats question', '2026-10-01', true, false, false, 'Commons'),
                       ('P2', 'Cats', 'Cats question', '2025-01-01', true, false, false, 'Commons')"""))
            db.session.commit()
            st._cache.clear()
            yield a
            st._cache.clear()
            db.session.execute(text(
                "TRUNCATE ha_contribution, ha_session_theme, ha_session, ha_pq_theme, ha_pq CASCADE"))
            db.session.execute(text("DELETE FROM member_profile"))
            db.session.commit()

    def test_names_do_not_match_and_real_mentions_do(self, pg):
        from hansard_archive import views
        with pg.app_context():
            results, total = views._fts_search("cats", 1)
            titles = {r["title"] for r in results}
            assert titles == {"Animal Rescue Centres", "Pets"} and total == 2
            named = {r["title"] for r in views._fts_search("cat smith", 1)[0]}
            assert named == {"Student Finance (Review)", "Waste Management Sites"}

    def test_phrase_matches_rank_first(self, pg):
        from hansard_archive import views
        with pg.app_context():
            titles = [r["title"] for r in views._fts_search("factory farming", 1)[0]]
            assert titles == ["State of Climate", "VE Day"]

    def test_wq_date_filter(self, pg):
        from hansard_archive import views
        with pg.app_context():
            assert [p["uin"] for p in views._pq_fts_search("cats", date_from="2026-01-01")] == ["P1"]
            assert views._pq_fts_count("cats", date_to="2025-12-31") == 1
