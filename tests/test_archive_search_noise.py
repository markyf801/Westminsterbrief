"""Archive search precision: member names, phrase-first ranking, WQ filters, Research Tool terms.

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


_TITLE_TSV = "'baro':3 'baron':7 'countess':2 'duchess':6 'duke':11 'earl':10 'ladi':9 'lord':8"
_TITLE_Q = "( 'baro' | 'baron' | 'countess' | 'duchess' | 'duke' | 'earl' | 'ladi' | 'lord' )"
_PEER_RE = r"(?:Viscountess|Viscount|Countess|Baroness|Marquess|Duchess|Baron|Lord|Lady|Earl|Duke)\s+"

# As member_profile gives them: (kind, phrase, phrase tsquery,
# stems that make it removable, stems that keep it, surname)
_ROWS = [
    ("mp", "Cat Smith", "'cat' <-> 'smith'", "'cat':1", "'smith':1", "Smith"),
    ("mp", "Cat Eccles", "'cat' <-> 'eccl'", "'cat':1", "'eccl':1", "Eccles"),
    ("mp", "Kemi Badenoch", "'kemi' <-> 'badenoch'", "'kemi':1", "'badenoch':1", "Badenoch"),
    ("mp", "Liz Saville Roberts", "'liz' <-> 'savill' <-> 'robert'", "'liz':1",
     "'robert':2 'savill':1", "Saville Roberts"),
    ("mp", "Hunt", "'hunt'", "", "'hunt':1", "Hunt"),               # one word: never removed
    ("peer", "Stroud", "'stroud'", "", "'stroud':1", "Stroud"),       # Baroness Stroud
    ("peer", "Frost", "'frost'", "", "'frost':1", "Frost"),           # Lord Frost
    ("peer", "Black of Brentwood", "'black' <2> 'brentwood'", "'brentwood':1", "'black':1",
     "Black"),
    ("peer", "of Kinnoull", "'kinnoul'", "'kinnoul':1", "", ""),     # The Earl of Kinnoull
    ("seat", "Forest of Dean", "'forest' <2> 'dean'", "'dean':3 'forest':1", "", ""),
    ("seat", "Kingston upon Hull East", "'kingston' <-> 'upon' <-> 'hull' <-> 'east'",
     "'east':4 'hull':3 'kingston':1 'upon':2", "", ""),
]
_STEMS = {"cats": "cat", "kinnoull": "kinnoul", "pricing": "price", "saville": "savill"}


def _index_from(rows=_ROWS):
    st._index.update(at=None, entries=())
    with patch.object(st.db.session, "execute") as ex:
        ex.side_effect = [MagicMock(one=lambda: (_TITLE_TSV, True)),
                          MagicMock(fetchall=lambda: rows)]
        entries = st._member_index()
    st._index.update(at=None, entries=())
    return entries


def _lookup_with(q, rows=_ROWS):
    """_lookup with member_profile holding `rows` and Postgres's stemmer faked."""
    entries = _index_from(rows)
    words = st._query_words(q)
    with patch.object(st, "_member_index", return_value=entries), \
            patch.object(st, "_word_lexemes", return_value=[{_STEMS.get(w, w)} for w in words]):
        return st._lookup(words, q)


def test_index_keeps_only_forms_with_a_removable_part():
    by_phrase = {(e.kind, e.phrase): e for e in _index_from()}
    # Nothing but a surname or a peer's name: never removed
    for key in (("mp", "Hunt"), ("peer", "Stroud"), ("peer", "Frost"), ("peer", "of Kinnoull")):
        assert key not in by_phrase, key
    black = by_phrase[("peer", "Black of Brentwood")]
    assert black.regex == _PEER_RE + r"Black\s+of\s+Brentwood"   # only the form with the place
    assert black.tsquery == f"{_TITLE_Q} <-> ( 'black' <2> 'brentwood' )"
    assert (black.trigger, black.keep) == ({"brentwood"}, {"black"})
    assert by_phrase[("mp", "Cat Smith")].regex == r"Cat\s+Smith"


def test_first_name_search_removes_the_name():
    guard = _lookup_with("cats")
    assert guard.tsquery == "( 'cat' <-> 'eccl' ) | ( 'cat' <-> 'smith' )"
    assert guard.pattern == r"\m(?:Cat\s+(?:Eccles|Smith))\M"


def test_surname_or_peer_name_search_never_removes_it():
    for q in ("badenoch", "starmer", "smith", "frost", "stroud", "price", "hunt", "black",
              "saville", "roberts"):
        assert _lookup_with(q) is None, q


def test_place_and_seat_searches_remove_the_attribution():
    assert _lookup_with("brentwood").pattern == r"\m(?:" + _PEER_RE + r"Black\s+of\s+Brentwood)\M"
    assert _lookup_with("hull").pattern == (
        r"\m(?:Members?|MP)\s+for\s+(?:Kingston\s+upon\s+Hull\s+East)\M")


def test_naming_the_member_or_seat_keeps_it_searchable():
    guard = _lookup_with("cat smith")
    assert "Smith" not in guard.pattern and r"Cat\s+Eccles" in guard.pattern
    assert _lookup_with("forest of dean") is None
    assert _lookup_with("black brentwood") is None
    assert r"Forest\s+of\s+Dean" in _lookup_with("forest").pattern


def test_names_regex_groups_by_first_word():
    assert st._names_regex({"Cat Smith", "Cat Eccles", "Bill Esterson"}) == (
        r"Bill\s+Esterson|Cat\s+(?:Eccles|Smith)")


def test_lookup_none_when_no_member_matches():
    assert _lookup_with("chicken welfare") is None
    assert _lookup_with("kinnoull") is None


def test_guard_sql_rechecks_with_postgres_stemming():
    guard = st.NameGuard(tsquery="( 'cat' <-> 'smith' )", pattern=r"\mCat\s+Smith\M")
    sql = guard.sql("c.speech_tsv", "c.speech_text", "plainto_tsquery('english', :q)")
    assert sql == (
        "AND (NOT (c.speech_tsv @@ CAST(:ng_q AS tsquery)) OR to_tsvector('english', "
        "regexp_replace(c.speech_text, :ng_re, ' xname ', 'g')) @@ plainto_tsquery('english', :q))")
    assert guard.params() == {"ng_q": guard.tsquery, "ng_re": guard.pattern}


def test_name_guard_is_cached_and_backs_off_after_errors(app_ctx):
    st._cache.clear()
    st._failed["until"] = 0.0
    with patch.object(st, "_lookup", return_value=None) as lk:
        st.name_guard("cats")
        st.name_guard("cats")
    assert lk.call_count == 1
    with patch.object(st, "_lookup", side_effect=RuntimeError("no table")) as lk:
        assert st.name_guard("dogs") is None
        assert st.name_guard("ponies") is None      # not retried for 5 minutes
    assert lk.call_count == 1
    st._failed["until"] = 0.0
    st._cache.clear()


# ── SQL sent by the archive search ────────────────────────────────────────────

_GUARD = st.NameGuard(tsquery="( 'cat' <-> 'smith' )", pattern=r"\mCat\s+Smith\M")


def _sql_calls(fn, *args, guard=None, **kw):
    from hansard_archive import views
    with patch.object(views.db.session, "execute") as ex, \
            patch.object(views._st, "name_guard", return_value=guard):
        ex.return_value = MagicMock(scalar=lambda: 0, fetchall=lambda: [])
        fn(*args, **kw)
        return [(str(c.args[0]), c.args[1]) for c in ex.call_args_list]


def test_debate_search_applies_name_guard_and_phrase_first(app_ctx):
    from hansard_archive import views
    [(rows_sql, rows_p)] = _sql_calls(views._fts_search, "cats", 1, guard=_GUARD)
    assert "CAST(:ng_q AS tsquery)" in rows_sql
    assert rows_p["ng_re"] == _GUARD.pattern
    # One query: the total is a window count over the same match
    assert "COUNT(*) OVER ()" in rows_sql
    assert "ORDER BY phrase_hit DESC, final_rank DESC, date DESC" in rows_sql
    assert "(COALESCE(ps.has_phrase, false) OR tm.session_id IS NOT NULL) AS phrase_hit" in rows_sql
    assert "phraseto_tsquery('english', :q)" in rows_sql
    assert "LN(1 + COALESCE(ps.hits, 0))" in rows_sql
    # Speech text is only read for the page of results
    assert "LEFT JOIN ha_contribution bc ON bc.id = r.best_id" in rows_sql


def test_debate_search_without_guard_or_title_only(app_ctx):
    from hansard_archive import views
    calls = _sql_calls(views._fts_search, "chicken welfare", 1)
    assert all(":ng_q" not in sql for sql, _ in calls)
    with patch.object(views.db.session, "execute") as ex, \
            patch.object(views._st, "name_guard") as ng:
        ex.return_value = MagicMock(fetchall=lambda: [])
        views._fts_search("cats", 1, title_only=True)
    ng.assert_not_called()


def test_debate_search_total_comes_from_the_window_count(app_ctx):
    from hansard_archive import views
    row = (7, "Animal Rescue Centres", date(2026, 1, 26), "Commons", "debate", "s7", "",
           0.2, None, 42)
    with patch.object(views.db.session, "execute") as ex, \
            patch.object(views._st, "name_guard", return_value=None):
        ex.return_value = MagicMock(fetchall=lambda: [row])
        results, total = views._fts_search("cats", 1)
        assert total == 42 and results[0]["title"] == "Animal Rescue Centres"
        assert views._fts_search("cats", 1, skip_count=True)[1] == 0


def test_quoted_debate_search_stays_a_phrase_search(app_ctx):
    from hansard_archive import views
    [(rows_sql, p)] = _sql_calls(views._fts_search, '"gateway 2"', 1)
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
    assert "OR to_tsvector('english', coalesce(p.heading, '')) @@ plainto_tsquery" in sql


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


def test_slow_search_shows_a_friendly_message(app):
    with (
        patch("hansard_archive.views._is_postgres", return_value=True),
        patch("hansard_archive.views._fts_search",
              side_effect=RuntimeError("canceling statement due to statement timeout")),
        patch("hansard_archive.views._all_policy_areas", return_value=[]),
    ):
        html = app.test_client().get("/archive/search?q=the+government").get_data(as_text=True)
    assert "This search took too long" in html


def test_statement_timeout_is_postgres_only(app_ctx):
    from hansard_archive import views
    with patch.object(views.db.session, "execute") as ex:
        views._limit_search_time()      # SQLite: nothing sent
    ex.assert_not_called()


# ── Research Tool search terms ────────────────────────────────────────────────

def test_topic_query_adds_the_topics_own_words():
    from debate_scanner import build_topic_query
    assert build_topic_query('"UK support for Ukraine"', "UK support for Ukraine") == (
        '("UK support for Ukraine" OR UK support for Ukraine)')
    assert build_topic_query('("cat microchipping" OR "single point of search")',
                             "cat microchipping") == (
        '("cat microchipping" OR "single point of search" OR cat microchipping)')


def test_topic_query_one_word_topic_is_not_repeated():
    from debate_scanner import build_topic_query
    assert build_topic_query('("Ukraine" OR "Ukraine Defence Contact Group")', "Ukraine") == (
        '("Ukraine" OR "Ukraine Defence Contact Group")')


def test_topic_query_strips_operators_from_the_topic():
    from debate_scanner import build_topic_query
    assert build_topic_query('"x"', 'cats or -dogs & "ferrets"') == '("x" OR cats dogs ferrets)'


def test_topic_query_keeps_user_edited_terms_as_given():
    from debate_scanner import build_topic_query
    assert build_topic_query('("a b" OR "c")', "a b", add_topic_words=False) == '("a b" OR "c")'


def test_topic_query_narrow_keyword_applies_to_every_branch():
    from debate_scanner import build_topic_query
    assert build_topic_query('("a b" OR "c")', "a b", "stray") == (
        '("a b" "stray" OR "c" "stray" OR a b "stray")')


def test_wq_fetch_uses_full_text_query_on_postgres(app_ctx):
    import debate_scanner as ds
    with patch.object(ds, "_db_is_postgres", return_value=True), \
            patch("hansard_archive.models.HaPQ.query") as query:
        chain = query.filter.return_value
        chain.order_by.return_value = chain
        chain.filter.return_value = chain
        chain.count.return_value = 0
        chain.limit.return_value.all.return_value = []
        ds.fetch_wqs_from_db("cat microchipping", [])
    sql = str(query.filter.call_args.args[0])
    assert "websearch_to_tsquery('english', :kw)" in sql


# ── Real SQL on a local scratch Postgres (opt-in) ─────────────────────────────

_PG_URL = os.environ.get("WB_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="set WB_TEST_POSTGRES_URL to a local scratch Postgres")
class TestOnLocalPostgres:
    MEMBERS = [
        ("Cat Smith", "Commons", "Lancaster and Wyre"), ("Cat Eccles", "Commons", "Stourbridge"),
        ("Baroness Stroud", "Lords", "Life peer"), ("Simon Opher", "Commons", "Stroud"),
        ("Lord Sandhurst", "Lords", "Hereditary"), ("Lord Price", "Lords", "Life peer"),
        ("Lord Bird", "Lords", "Life peer"), ("Lara Bird", "Commons", "Preston"),
        ("Viscount Hailsham", "Lords", "Life peer"), ("Lord Shipley", "Lords", "Life peer"),
        ("Anna Dixon", "Commons", "Shipley"), ("Lord Walney", "Lords", "Life peer"),
        ("Lord Gold", "Lords", "Life peer"), ("Jeremy Hunt", "Commons", "Godalming and Ash"),
        ("Wera Hobhouse", "Commons", "Bath"), ("Matt Rodda", "Commons", "Reading Central"),
        ("Karl Turner", "Commons", "Kingston upon Hull East"),
        ("Sarah Green", "Commons", "Chesham and Amersham"), ("Earl Attlee", "Lords", "Hereditary"),
        ("Lord Black of Brentwood", "Lords", "Life peer"),
        ("Mrs Kemi Badenoch", "Commons", "North West Essex"),
        ("Sir Keir Starmer", "Commons", "Holborn and St Pancras"),
        ("Lord Frost", "Lords", "Life peer"), ("Alex Burghart", "Commons", "Brentwood and Ongar"),
    ]
    # (search, session title that must be found, speeches, date)
    FOUND = [
        ("cats", "Animal Rescue Centres",
         ["Rescue centres for cats need regulation.", "Many cats are abandoned."], "2026-01-26"),
        ("cats", "Pets",
         ["As the Member for Stourbridge (Cat Eccles) said, stray cats are a problem."], "2026-10-01"),
        ("stroud", "Community Hospitals",
         ["The hospital in Stroud is closing and Stroud residents deserve better."], None),
        ("sandhurst", "Officer Training", ["Cadets at Sandhurst train for 44 weeks."], None),
        ("energy price guarantee", "Household Bills",
         ["The Energy Price Guarantee helped households this winter."], None),
        ("price", "Drug Costs", ["NHS drug pricing is opaque, as Lord Price said."], None),
        ("pricing", "Medicines",
         ["Lord Price said drug prices are opaque and medicines are priced too high."], None),
        ("hailsham", "Local Roads", ["The A22 at Hailsham is congested."], None),
        ("shipley", "Rail Services", ["Trains from Shipley to Leeds are late."], None),
        ("walney", "Offshore Wind", ["The Walney Extension wind farm is huge."], None),
        ("gold command", "Policing Protests", ["Gold Command took the decision."], None),
        ("bird flu", "Poultry", ["Bird flu has spread. Avian influenza is serious."], None),
        ("hunting", "Hunting Act", ["Jeremy Hunt said foxes are still hunted illegally."], None),
        ("hunted", "Wildlife", ["Jeremy Hunt said foxes are still hunted, he told us."], None),
        ("bath", "Tourism",
         ["The hon. Member for Bath (Wera Hobhouse) knows Bath is a world heritage city."], None),
        ("reading", "Rail", ["My hon. Friend the Member for Reading Central (Matt Rodda) "
                             "wants Reading station upgraded."], None),
        ("hull", "Ports",
         ["The Member for Kingston upon Hull East (Karl Turner) praised Hull docks."], None),
        ("green", "Planning", ["Sarah Green raised the green belt."], None),
        ("attlee", "Post-war Settlement", ["The Attlee Government founded the NHS."], None),
        ("lady stroud", "Online Safety",
         ["I support the amendment of the noble Baroness, Lady Stroud."], None),
        ("cat smith", "Committee", ["Ordered, That Cat Smith be a member."], None),
        # Surnames and peers' names are how people search for a person: never removed
        ("badenoch", "Stamp Duty", ["The Leader of the Opposition, Kemi Badenoch, was right."], None),
        # (straight apostrophe: Postgres reads "Starmer’s" as one token "starmer’")
        ("starmer", "Chagos", ["Keir Starmer's surrender of the Chagos islands is a disgrace."], None),
        ("frost", "Brexit Deal", ["The deal negotiated by Lord Frost was flawed."], None),
        ("price", "Lords Business", ["My Lords, the noble Lord, Lord Price, is not in his place."],
         None),
        ("stroud", "Amendments", ["The noble Baroness, Lady Stroud, moved the amendment."], None),
        ("black", "Press", ["The noble Lord, Lord Black of Brentwood, made a fair point."], None),
        ("green", "Energy", ["As Ms Green said, energy matters."], None),
        ("factory farming", "State of Climate", ["A contributory factor is factory farming."],
         "2025-07-21"),
    ]
    # (search, session title that must NOT be found, speeches)
    NOT_FOUND = [
        ("cats", "Student Finance (Review)",
         ["Ordered, That Ann Davies, Cat Eccles, Cat Smith be members of the Committee."]),
        ("cats", "Waste Management Sites",
         ["The Member for Lancaster and Wyre (Cat Smith) has constituents nearby."]),
        ("brentwood", "Media Bill", ["Lord Black of Brentwood spoke on the press."]),
        ("hull", "Ports Committee",
         ["The Member for Kingston upon Hull East (Karl Turner) spoke."]),
        ("reading", "Transport Questions",
         ["The hon. Member for Reading Central (Matt Rodda) asked a question."]),
    ]
    EXTRA = [
        ("VE Day", ["They worked in aircraft factories and on the farms."], None),
        ("Funding for Schools", ["The settlement for schools was announced."], "2026-10-01"),
        ("Budget", ["A word on school funding before I sit down."], "2026-10-01"),
    ]
    PQS = [
        ("P1", "Cats", "Cats question", "2026-10-01", "Commons"),
        ("P2", "Cats", "Cats question", "2025-01-01", "Commons"),
        ("P3", "Medicines", "Will the Minister meet Lord Price?", "2026-10-01", "Lords"),
        ("P4", "Medicines: Prices", "What is the price of insulin?", "2026-10-01", "Commons"),
        ("P5", "Committees", "Has the Minister met Cat Smith?", "2026-10-01", "Commons"),
    ]

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
            for i, (name, house, seat) in enumerate(self.MEMBERS, 1):
                db.session.execute(text(
                    "INSERT INTO member_profile (member_id, slug, name, house, membership_from,"
                    " is_current) VALUES (:i, :s, :n, :h, :m, true)"),
                    {"i": i, "s": f"m{i}", "n": name, "h": house, "m": seat})
            sessions = ([(t, sp, d) for _, t, sp, d in self.FOUND]
                        + [(t, sp, None) for _, t, sp in self.NOT_FOUND] + self.EXTRA)
            for sid, (title, speeches, d) in enumerate(sessions, 1):
                db.session.execute(text(
                    "INSERT INTO ha_session (id, ext_id, title, date, house, debate_type,"
                    " contributions_ingested, is_container, slug) VALUES"
                    " (:id, :e, :t, :d, 'Commons', 'debate', true, false, :s)"),
                    {"id": sid, "e": f"e{sid}", "t": title, "d": d or "2026-09-01", "s": f"s{sid}"})
                for i, sp in enumerate(speeches):
                    db.session.execute(text(
                        "INSERT INTO ha_contribution (session_id, speech_text, speech_order)"
                        " VALUES (:s, :t, :o)"), {"s": sid, "t": sp, "o": i})
            for uin, heading, question, tabled, house in self.PQS:
                db.session.execute(text(
                    "INSERT INTO ha_pq (uin, heading, question_text, tabled_date, is_answered,"
                    " is_holding, is_withdrawn, chamber)"
                    " VALUES (:u, :h, :q, :d, true, false, false, :c)"),
                    {"u": uin, "h": heading, "q": question, "d": tabled, "c": house})
            db.session.commit()
            st._cache.clear()
            st._index.update(at=None, entries=())
            yield a
            st._cache.clear()
            st._index.update(at=None, entries=())
            db.session.execute(text(
                "TRUNCATE ha_contribution, ha_session_theme, ha_session, ha_pq_theme, ha_pq CASCADE"))
            db.session.execute(text("DELETE FROM member_profile"))
            db.session.commit()

    @staticmethod
    def _titles(q):
        from hansard_archive import views
        return [r["title"] for r in views._fts_search(q, 1)[0]]

    @pytest.mark.parametrize("q, title", [(q, t) for q, t, _, _ in FOUND])
    def test_real_mentions_are_found(self, pg, q, title):
        with pg.app_context():
            assert title in self._titles(q)

    @pytest.mark.parametrize("q, title", [(q, t) for q, t, _ in NOT_FOUND])
    def test_name_only_mentions_are_not(self, pg, q, title):
        with pg.app_context():
            assert title not in self._titles(q)

    def test_phrase_or_title_matches_rank_first(self, pg):
        with pg.app_context():
            factory = self._titles("factory farming")
            assert factory.index("State of Climate") < factory.index("VE Day")
            # all the words in the title count as much as the phrase in a speech
            schools = self._titles("school funding")
            assert schools.index("Funding for Schools") < schools.index("Budget")

    def test_wq_date_filter_and_name_guard(self, pg):
        from hansard_archive import views
        with pg.app_context():
            assert [p["uin"] for p in views._pq_fts_search("cats", date_from="2026-01-01")] == ["P1"]
            assert views._pq_fts_count("cats", date_to="2025-12-31") == 1
            assert {p["uin"] for p in views._pq_fts_search("price")} == {"P3", "P4"}
            assert {p["uin"] for p in views._pq_fts_search("cats")} == {"P1", "P2"}  # not P5
            assert {p["uin"] for p in views._pq_fts_search("cat smith")} == {"P5"}

    def test_policy_filter_counts_each_session_once(self, pg):
        from sqlalchemy import text
        from extensions import db
        from hansard_archive import views
        with pg.app_context():
            sid = db.session.execute(text(
                "SELECT id FROM ha_session WHERE title = 'Animal Rescue Centres'")).scalar()
            for _ in range(2):   # the same tag twice: no unique constraint stops it
                db.session.execute(text(
                    "INSERT INTO ha_session_theme (session_id, theme, theme_type)"
                    " VALUES (:s, 'Animals', 'policy_area')"), {"s": sid})
            db.session.commit()
            results, total = views._fts_search("cats", 1, policy_filter="Animals")
            assert total == 1 and [r["title"] for r in results] == ["Animal Rescue Centres"]
            db.session.execute(text("DELETE FROM ha_session_theme"))
            db.session.commit()

    def test_failed_name_lookup_keeps_the_timeout(self, pg):
        from sqlalchemy import text
        from extensions import db
        from hansard_archive import views

        def broken():
            db.session.execute(text("SELECT * FROM no_such_table_for_test"))

        with pg.app_context():
            st._cache.clear()
            st._failed["until"] = 0.0
            views._limit_search_time()
            with patch.object(st, "_member_index", side_effect=broken):
                assert st.name_guard("cats") is None
            # The savepoint undid only the lookup: the search carries on, still capped
            assert db.session.execute(text("SHOW statement_timeout")).scalar() == "8s"
            assert views._fts_search("cats", 1)[1] >= 1
            db.session.rollback()
            st._failed["until"] = 0.0
            st._cache.clear()

    def test_statement_timeout_is_set(self, pg):
        from sqlalchemy import text
        from extensions import db
        from hansard_archive import views
        with pg.app_context():
            views._limit_search_time()
            assert db.session.execute(text("SHOW statement_timeout")).scalar() == "8s"
            db.session.rollback()
