"""
Written Question addresses: /archive/pq/<yyyy-mm-dd>/<uin> (PQ identity plan,
Phase 4). UINs restart every session, so the UIN alone can match several
questions; the old /archive/pq/<uin> address 301s to the question it has always
shown (the earliest stored). Uses the real flask_app (canonical-URL hooks,
sitemap routes) on in-memory SQLite.
"""

import re
from datetime import date, datetime

import pytest


@pytest.fixture(scope="module")
def fa():
    import flask_app as _fa
    from extensions import db
    _fa.app.config["TESTING"] = True
    with _fa.app.app_context():
        db.create_all()
    return _fa


@pytest.fixture
def client(fa):
    from extensions import db
    from hansard_archive.models import HaPQ, HaPQTheme

    def _clear():
        HaPQTheme.query.delete()
        HaPQ.query.delete()
        db.session.commit()
        fa._sitemap_pqs_month_caches.clear()
        fa._sitemap_index_cache.update(xml=None, ts=0.0)
        fa._sitemap_pq_month_summary.update(data=None, ts=0.0)

    with fa.app.app_context():
        _clear()
        yield fa.app.test_client()
        db.session.rollback()
        _clear()


def _add(uin, tabled, heading, **kw):
    from extensions import db
    from hansard_archive.models import HaPQ
    pq = HaPQ(uin=uin, tabled_date=tabled, heading=heading, chamber=kw.pop("chamber", "Commons"),
              question_text=kw.pop("question_text", f"To ask about {heading}."),
              is_answered=kw.pop("is_answered", False), **kw)
    db.session.add(pq)
    db.session.commit()
    return pq


# ── the path helper ───────────────────────────────────────────────────────────

def test_url_path_helper():
    from hansard_archive.models import pq_url_path
    assert pq_url_path(date(2026, 6, 4), "6978") == "/archive/pq/2026-06-04/6978"
    assert pq_url_path(date(2026, 1, 9), "HL1234") == "/archive/pq/2026-01-09/hl1234"
    assert pq_url_path(datetime(2026, 1, 9, 13, 5), "HL1") == "/archive/pq/2026-01-09/hl1"
    assert pq_url_path("2026-01-09", "HL1") == "/archive/pq/2026-01-09/hl1"   # raw SQLite row
    assert pq_url_path(None, "HL1") == "/archive/pq/hl1"                     # no date: old form


# ── canonical address ─────────────────────────────────────────────────────────

def test_dated_url_renders_with_matching_canonical_og_and_json_ld(client):
    _add("HL1234", date(2026, 6, 4), "Rail: North")
    r = client.get("/archive/pq/2026-06-04/hl1234")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    url = "https://westminsterbrief.co.uk/archive/pq/2026-06-04/hl1234"
    assert f'<link rel="canonical" href="{url}">' in html
    assert f'<meta property="og:url" content="{url}">' in html
    assert f'"url": "{url}"' in html                                  # JSON-LD
    assert '<h1 class="sd-title">Rail: North</h1>' in html


def test_uppercase_uin_redirects_to_lowercase(client):
    _add("HL1234", date(2026, 6, 4), "Rail: North")
    r = client.get("/archive/pq/2026-06-04/HL1234")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/archive/pq/2026-06-04/hl1234")


def test_unknown_date_and_uin_is_404(client):
    _add("6978", date(2026, 6, 4), "Rail")
    assert client.get("/archive/pq/2026-06-05/6978").status_code == 404   # right uin, wrong date
    assert client.get("/archive/pq/2026-06-04/6979").status_code == 404
    assert client.get("/archive/pq/9999").status_code == 404              # old form, unknown uin


@pytest.mark.parametrize("bad", ["2026-6-4", "2026-02-30", "20260604", "4-june-2026",
                                 "2026-06-04x", "2026-13-01"])
def test_malformed_dates_are_404(client, bad):
    _add("6978", date(2026, 6, 4), "Rail")
    assert client.get(f"/archive/pq/{bad}/6978").status_code == 404


# ── old address and repeated UINs ─────────────────────────────────────────────

def test_old_url_301s_to_dated_url(client):
    _add("HL1234", date(2026, 6, 4), "Rail: North")
    r = client.get("/archive/pq/hl1234")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/archive/pq/2026-06-04/hl1234")


@pytest.mark.parametrize("path", ["/archive/pq/hl1234", "/archive/pq/HL1234", "/archive/pq/Hl1234/",
                                  "/archive/pq/hl1234/", "/archive/pq/HL1234//"])
def test_old_url_is_one_301_in_any_case_or_trailing_slash(client, path):
    _add("HL1234", date(2026, 6, 4), "Rail: North")
    r = client.get(path)
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/archive/pq/2026-06-04/hl1234")
    final = client.get(r.headers["Location"].replace("http://localhost", ""))
    assert final.status_code == 200                               # one hop, then the page


def test_old_url_keeps_the_query_string(client):
    _add("HL1234", date(2026, 6, 4), "Rail: North")
    r = client.get("/archive/pq/HL1234/?utm_source=x&b=2")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/archive/pq/2026-06-04/hl1234?utm_source=x&b=2")


def test_old_url_unknown_uin_is_404_in_any_form(client):
    for path in ("/archive/pq/nope", "/archive/pq/NOPE/"):
        assert client.get(path).status_code == 404


def test_dated_url_with_case_and_slash_is_one_301(client):
    _add("HL1234", date(2026, 6, 4), "Rail: North")
    r = client.get("/archive/pq/2026-06-04/HL1234/?ref=1")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/archive/pq/2026-06-04/hl1234?ref=1")


def test_other_paths_need_one_redirect_for_case_and_slash(client):
    r = client.get("/Archive/Search/")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/archive/search")


def test_repeated_uin_old_url_keeps_the_question_it_always_showed(client):
    """The old address showed the earliest-stored row. A question stored later
    with the same UIN (e.g. the backfill of 2024 questions) must not take it
    over, even though its tabled date is older."""
    current = _add("6978", date(2026, 6, 4), "Rail punctuality", asking_member="A Member")
    _add("6978", date(2024, 9, 10), "Bus fares", asking_member="B Member")
    r = client.get("/archive/pq/6978")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(current.url_path)

    # Each dated address opens its own question, and lists the other one
    a = client.get("/archive/pq/2026-06-04/6978").get_data(as_text=True)
    b = client.get("/archive/pq/2024-09-10/6978").get_data(as_text=True)
    assert '<h1 class="sd-title">Rail punctuality</h1>' in a
    assert '<h1 class="sd-title">Bus fares</h1>' in b
    assert "Other written questions numbered 6978" in a
    assert a.index("Other written questions numbered") < a.index('class="sd-transcript"')   # near the top
    assert 'href="/archive/pq/2024-09-10/6978">Bus fares</a>' in a
    assert "B Member" in a
    assert 'href="/archive/pq/2026-06-04/6978">Rail punctuality</a>' in b


def test_single_question_page_has_no_same_number_list(client):
    _add("6978", date(2026, 6, 4), "Rail")
    html = client.get("/archive/pq/2026-06-04/6978").get_data(as_text=True)
    assert "Other written questions numbered" not in html


# ── sitemap ───────────────────────────────────────────────────────────────────

def test_sitemap_one_chunk_per_month_with_dated_urls(client):
    # updated_at is ignored: bulk scripts stamp it on unchanged rows
    _add("6978", date(2026, 6, 4), "Rail", is_answered=True, answer_date=date(2026, 6, 9),
         updated_at=datetime(2026, 10, 20, 8, 0))
    _add("6978", date(2024, 9, 10), "Bus fares", updated_at=datetime(2026, 10, 20, 8, 0))
    _add("HL22", date(2026, 6, 30), "Lords question", chamber="Lords", is_answered=True,
         answer_date=date(2026, 7, 2), answer_corrected_date=date(2026, 7, 9),
         updated_at=datetime(2026, 10, 20, 8, 0))

    index = client.get("/sitemap.xml").get_data(as_text=True)
    assert "https://westminsterbrief.co.uk/sitemap-pqs-2026-06.xml" in index
    assert "https://westminsterbrief.co.uk/sitemap-pqs-2024-09.xml" in index
    assert "sitemap-pqs-1.xml" not in index
    assert index.index("sitemap-pqs-2026-06.xml") < index.index("sitemap-pqs-2024-09.xml")
    chunk_lastmod = dict(re.findall(r"<loc>https://westminsterbrief.co.uk/(sitemap-pqs-[\d-]+\.xml)</loc>"
                                    r"\s*<lastmod>(.*?)</lastmod>", index))
    assert chunk_lastmod == {"sitemap-pqs-2026-06.xml": "2026-07-09",     # latest in the month
                             "sitemap-pqs-2024-09.xml": "2024-09-10"}

    june = client.get("/sitemap-pqs-2026-06.xml").get_data(as_text=True)
    locs = re.findall(r"<loc>(.*?)</loc>", june)
    assert locs == ["https://westminsterbrief.co.uk/archive/pq/2026-06-04/6978",
                    "https://westminsterbrief.co.uk/archive/pq/2026-06-30/hl22"]
    lastmods = re.findall(r"<lastmod>(.*?)</lastmod>", june)
    assert lastmods == ["2026-06-09", "2026-07-09"]       # answered / corrected after tabling

    sept = client.get("/sitemap-pqs-2024-09.xml").get_data(as_text=True)
    assert re.findall(r"<loc>(.*?)</loc>", sept) == [
        "https://westminsterbrief.co.uk/archive/pq/2024-09-10/6978"]


@pytest.mark.parametrize("path", ["/sitemap-pqs-1.xml", "/sitemap-pqs-2026-13.xml",
                                  "/sitemap-pqs-2026-6.xml", "/sitemap-pqs-1999-01.xml"])
def test_bad_sitemap_chunk_names_are_404(client, path):
    assert client.get(path).status_code == 404


def test_sitemap_month_with_no_questions_is_404(client):
    _add("6978", date(2026, 6, 4), "Rail")
    assert client.get("/sitemap-pqs-2026-06.xml").status_code == 200
    assert client.get("/sitemap-pqs-2026-05.xml").status_code == 404


# ── links elsewhere ───────────────────────────────────────────────────────────

def test_consultation_related_questions_use_dated_urls(fa, client):
    from extensions import db
    from hansard_archive.models import HaPQTheme, THEME_TYPE_POLICY_AREA
    from consultations import related_items
    pq = _add("6978", date(2026, 6, 4), "Rail")
    db.session.add(HaPQTheme(pq_id=pq.id, theme="Transport", theme_type=THEME_TYPE_POLICY_AREA))
    db.session.commit()
    out = related_items(["transport"], now=datetime(2026, 6, 10))
    assert [p["url"] for p in out["pqs"]] == ["/archive/pq/2026-06-04/6978"]


def test_selected_export_picks_the_question_by_date_and_uin(client):
    _add("6978", date(2026, 6, 4), "Rail", question_text="Question about rail punctuality.")
    _add("6978", date(2024, 9, 10), "Bus", question_text="Question about bus fares.")
    import json
    r = client.post("/questions/download_selected", data={
        "format": "csv",
        "items_json": json.dumps([{"uin": "6978", "tabled": "2024-09-10"}]),
    })
    body = r.get_data(as_text=True)
    assert "Question about bus fares." in body
    assert "Question about rail punctuality." not in body
    # A page rendered before dates were posted: the earliest-stored question
    r = client.post("/questions/download_selected", data={
        "format": "csv", "items_json": json.dumps([{"uin": "6978"}])})
    assert "Question about rail punctuality." in r.get_data(as_text=True)


def test_new_month_chunk_rebuilds_the_month_list_before_404(fa, client):
    """On the 1st of a month the cached list may not have the new month yet
    (and the other worker may already list it): rebuild once before a 404."""
    _add("6978", date(2026, 6, 4), "Rail")
    assert "2026-06" in fa._sitemap_pq_months()              # list now cached
    _add("1", date(2026, 7, 1), "First of July")
    # Just built: an unknown month doesn't force a rebuild (at most once a minute)
    assert client.get("/sitemap-pqs-2026-07.xml").status_code == 404
    fa._sitemap_pq_month_summary["ts"] -= 61
    r = client.get("/sitemap-pqs-2026-07.xml")
    assert r.status_code == 200
    assert "/archive/pq/2026-07-01/1</loc>" in r.get_data(as_text=True)
    assert "2026-07" in fa._sitemap_pq_months()
    fa._sitemap_pq_month_summary["ts"] -= 61
    assert client.get("/sitemap-pqs-2026-08.xml").status_code == 404   # still no such month
