"""
Written Question answer times (FEATURE_PQ_ANSWER_TIMES): the working-day
calendar, the classification rules, the "Answer timing" block on question
pages, the department figures and /written-questions/answer-times.

Uses the real flask_app on in-memory SQLite. "Today" is fixed at Saturday
24 October 2026, so the default period is 1 July to 30 September 2026.

Run: python -m pytest tests/test_pq_answer_times.py -v
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from hansard_archive import pq_timing as pt
from hansard_archive import working_days as wd

TODAY = date(2026, 10, 24)
FLAG = "FEATURE_PQ_ANSWER_TIMES"
DFE = "Department for Education"
CO = "Cabinet Office"


# ── Working days ──────────────────────────────────────────────────────────────

class TestWorkingDays:
    def test_weekends_are_not_working_days(self):
        assert wd.is_working_day(date(2026, 3, 13))        # Friday
        assert not wd.is_working_day(date(2026, 3, 14))    # Saturday
        assert not wd.is_working_day(date(2026, 3, 15))    # Sunday

    @pytest.mark.parametrize("d", [date(2024, 8, 26), date(2025, 4, 18), date(2026, 4, 6),
                                   date(2026, 12, 28), date(2027, 12, 27), date(2027, 12, 28)])
    def test_bank_holidays_are_not_working_days(self, d):
        assert not wd.is_working_day(d)

    def test_eight_bank_holidays_a_year_all_on_weekdays(self):
        for year in range(2024, 2028):
            days = [d for d in wd.BANK_HOLIDAYS_ENGLAND_WALES if d.year == year]
            assert len(days) == 8, year
            assert all(d.weekday() < 5 for d in days)

    def test_add_working_days_skips_weekend(self):
        assert wd.add_working_days(date(2026, 3, 12), 4) == date(2026, 3, 18)   # Thu + 4 = Wed
        assert wd.add_working_days(date(2026, 3, 12), 0) == date(2026, 3, 12)

    def test_add_working_days_skips_easter(self):
        # Thu 2 April 2026 + 1: Good Friday 3rd and Easter Monday 6th skipped
        assert wd.add_working_days(date(2026, 4, 2), 1) == date(2026, 4, 7)

    def test_year_boundary(self):
        # Wed 23 Dec 2026 + 3: skips 25 Dec, weekend, 28 Dec (substitute), 1 Jan 2027
        assert wd.add_working_days(date(2026, 12, 23), 3) == date(2026, 12, 30)
        assert wd.add_working_days(date(2026, 12, 23), 5) == date(2027, 1, 4)
        assert wd.working_days_between(date(2026, 12, 23), date(2027, 1, 4)) == 5

    def test_working_days_between(self):
        assert wd.working_days_between(date(2026, 3, 9), date(2026, 3, 11)) == 2   # Mon to Wed
        assert wd.working_days_between(date(2026, 3, 13), date(2026, 3, 16)) == 1  # Fri to Mon
        assert wd.working_days_between(date(2026, 3, 13), date(2026, 3, 13)) == 0
        assert wd.working_days_between(date(2026, 3, 13), date(2026, 3, 10)) == 0

    def test_covered_range(self):
        assert wd.covered(date(2024, 1, 1)) and wd.covered(date(2027, 12, 31))
        assert not wd.covered(date(2023, 12, 31)) and not wd.covered(date(2028, 1, 1))

    def test_calendar_is_extended_before_it_runs_out(self):
        """Tripwire: add the next year's bank holidays (gov.uk/bank-holidays) when this fails."""
        assert wd.covered(date.today() + timedelta(days=90))


# ── Classification ────────────────────────────────────────────────────────────

class TestClassify:
    DFA = date(2026, 3, 12)   # Thursday

    def test_named_day_on_time_and_after(self):
        assert pt.classify_commons(True, self.DFA, date(2026, 3, 12), TODAY) == pt.ON_TIME
        assert pt.classify_commons(True, self.DFA, date(2026, 3, 10), TODAY) == pt.ON_TIME
        assert pt.classify_commons(True, self.DFA, date(2026, 3, 13), TODAY) == pt.AFTER

    def test_ordinary_within_and_after(self):
        # Thu 12 March + 4 working days = Wed 18 March
        assert pt.classify_commons(False, self.DFA, date(2026, 3, 18), TODAY) == pt.ON_TIME
        assert pt.classify_commons(False, self.DFA, date(2026, 3, 19), TODAY) == pt.AFTER

    def test_unanswered_pending_until_the_date_passes(self):
        assert pt.classify_commons(True, self.DFA, None, date(2026, 3, 12)) == pt.PENDING
        assert pt.classify_commons(True, self.DFA, None, date(2026, 3, 13)) == pt.AFTER
        assert pt.classify_commons(False, self.DFA, None, date(2026, 3, 18)) == pt.PENDING
        assert pt.classify_commons(False, self.DFA, None, date(2026, 3, 19)) == pt.AFTER


# ── The facts on one question page (no database) ──────────────────────────────

def _pq(chamber="Commons", tabled=date(2026, 3, 9), answer=None, holding=False, withdrawn=False, **kw):
    return SimpleNamespace(chamber=chamber, tabled_date=tabled, answer_date=answer,
                           is_answered=answer is not None, is_holding=holding, is_withdrawn=withdrawn,
                           api_id=kw.get("api_id", 1), answering_body=kw.get("body", DFE))


def _dl(dfa, named):
    return SimpleNamespace(date_for_answer=dfa, is_named_day=named)


def _line(t):
    return " · ".join(t["facts"]) + (f" · {t['verdict']}" if t["verdict"] else "")


class TestPageTiming:
    def test_named_day_after(self):
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 20)), _dl(date(2026, 3, 12), True), TODAY)
        assert _line(t) == ("Named-day question · due Thu 12 March 2026 · "
                            "answered Fri 20 March 2026 (6 working days after the named day)")

    def test_named_day_on_and_before(self):
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 12)), _dl(date(2026, 3, 12), True), TODAY)
        assert _line(t).endswith("answered Thu 12 March 2026, on the named day")
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 11)), _dl(date(2026, 3, 12), True), TODAY)
        assert _line(t).endswith("answered Wed 11 March 2026, before the named day")

    def test_named_day_unanswered(self):
        t = pt.pq_answer_timing(_pq(), _dl(date(2026, 3, 12), True), date(2026, 3, 12))
        assert _line(t) == "Named-day question · due Thu 12 March 2026 · not yet answered"
        t = pt.pq_answer_timing(_pq(), _dl(date(2026, 3, 12), True), date(2026, 3, 17))
        assert _line(t).endswith("awaiting answer, 3 working days after the named day")

    def test_ordinary_within(self):
        # tabled Mon 9 March, date for answer Wed 11 March, expected by Tue 17 March
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 17)), _dl(date(2026, 3, 11), False), TODAY)
        assert _line(t) == ("Ordinary question · tabled Mon 9 March 2026 · answered Tue 17 March 2026 "
                            "(6 working days after tabling) · within the expected time")

    def test_ordinary_after(self):
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 18)), _dl(date(2026, 3, 11), False), TODAY)
        assert _line(t).endswith("(7 working days after tabling) · after the expected time")

    def test_ordinary_unanswered(self):
        t = pt.pq_answer_timing(_pq(), _dl(date(2026, 3, 11), False), date(2026, 3, 13))
        assert _line(t).endswith("not yet answered (4 working days since tabling)")
        assert t["verdict"] is None
        t = pt.pq_answer_timing(_pq(), _dl(date(2026, 3, 11), False), date(2026, 3, 18))
        assert _line(t).endswith("awaiting answer, 7 working days since tabling · after the expected time")

    def test_same_day_answer(self):
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 9)), _dl(date(2026, 3, 11), False), TODAY)
        assert "(the day it was tabled)" in _line(t)

    @pytest.mark.parametrize("named", [True, False])
    def test_holding_answer_is_not_judged(self, named):
        t = pt.pq_answer_timing(_pq(answer=date(2026, 3, 20), holding=True),
                                _dl(date(2026, 3, 12), named), TODAY)
        assert "holding answer given on Fri 20 March 2026; no full answer recorded yet" in _line(t)
        assert t["verdict"] is None
        assert "after" not in _line(t) and "on time" not in _line(t)

    def test_lords_facts_only(self):
        t = pt.pq_answer_timing(_pq(chamber="Lords", answer=date(2026, 3, 25)), None, TODAY)
        assert _line(t) == "Tabled Mon 9 March 2026 · answered Wed 25 March 2026 (12 working days after tabling)"
        assert t["verdict"] is None
        assert t["note"] == "The House of Lords expects answers within 10 working days of tabling."

    def test_withdrawn_shows_nothing(self):
        assert pt.pq_answer_timing(_pq(withdrawn=True, answer=date(2026, 3, 12)),
                                   _dl(date(2026, 3, 12), True), TODAY) is None

    def test_commons_without_deadline_row_shows_nothing(self):
        assert pt.pq_answer_timing(_pq(answer=date(2026, 3, 12)), None, TODAY) is None
        assert pt.pq_answer_timing(_pq(answer=date(2026, 3, 12)), _dl(None, None), TODAY) is None

    def test_inconsistent_or_uncovered_dates_show_nothing(self):
        assert pt.pq_answer_timing(_pq(answer=date(2026, 3, 1)), _dl(date(2026, 3, 12), True), TODAY) is None
        assert pt.pq_answer_timing(_pq(tabled=date(2023, 6, 1), answer=date(2023, 6, 8)),
                                   _dl(date(2023, 6, 5), True), TODAY) is None


# ── Database fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fa():
    import flask_app as _fa
    from extensions import db
    _fa.app.config["TESTING"] = True
    with _fa.app.app_context():
        db.create_all()
    return _fa


_next_id = [1000]


def _add(db, body, tabled, answer=None, *, chamber="Commons", dfa=None, named=None,
         holding=False, withdrawn=False, deadline=True, n=1):
    from hansard_archive.models import HaPQ, HaPQDeadline
    for _ in range(n):
        _next_id[0] += 1
        api_id = _next_id[0]
        db.session.add(HaPQ(uin=str(api_id), tabled_date=tabled, heading=f"Q {api_id}",
                            question_text="To ask the Secretary of State a question.",
                            chamber=chamber, answering_body=body, answer_date=answer,
                            is_answered=answer is not None, is_holding=holding,
                            is_withdrawn=withdrawn, api_id=api_id,
                            answer_text="An answer." if answer else None))
        if deadline and chamber == "Commons":
            db.session.add(HaPQDeadline(api_id=api_id, date_for_answer=dfa, is_named_day=named))
    db.session.commit()


MON, WED, THU = date(2026, 9, 7), date(2026, 9, 9), date(2026, 9, 10)


def _seed(db):
    # Education: named day due Thu 10 Sep; 30 answered on the day (3 working days
    # after tabling), 10 on Fri 18 Sep (9). One holding answer after the named day
    # counts as not on time.
    _add(db, DFE, MON, THU, dfa=THU, named=True, n=30)
    _add(db, DFE, MON, date(2026, 9, 18), dfa=THU, named=True, n=10)
    _add(db, DFE, MON, date(2026, 9, 14), dfa=THU, named=True, holding=True)
    # Ordinary: date for answer Wed 9 Sep, expected by Tue 15 Sep.
    _add(db, DFE, MON, date(2026, 9, 15), dfa=WED, named=False, n=28)
    _add(db, DFE, MON, date(2026, 9, 16), dfa=WED, named=False, n=7)
    # Left out: withdrawn; no deadline row; Lords; tabled outside the period.
    _add(db, DFE, MON, THU, dfa=THU, named=True, withdrawn=True)
    _add(db, DFE, MON, THU, deadline=False)
    _add(db, DFE, MON, date(2026, 9, 30), chamber="Lords")
    _add(db, DFE, date(2026, 6, 30), date(2026, 7, 20), dfa=date(2026, 7, 2), named=True, n=5)
    # Cabinet Office: 5 questions, all named-day on time.
    _add(db, CO, MON, THU, dfa=THU, named=True, n=5)
    # Current quarter: one pending (due Mon 26 Oct), one past due (Wed 21 Oct).
    _add(db, DFE, date(2026, 10, 22), dfa=date(2026, 10, 26), named=True)
    _add(db, DFE, date(2026, 10, 19), dfa=date(2026, 10, 21), named=True)


@pytest.fixture
def client(fa, monkeypatch):
    from extensions import db
    from hansard_archive.models import HaPQ, HaPQDeadline, HaPQTheme
    monkeypatch.setattr(pt, "today", lambda: TODAY)
    monkeypatch.setenv(FLAG, "live")
    monkeypatch.delenv("PQ_INDEX_FROM", raising=False)

    def _clear():
        HaPQTheme.query.delete()
        HaPQ.query.delete()
        HaPQDeadline.query.delete()
        db.session.commit()
        pt.clear_caches()
        fa._sitemap_core_cache.update(xml=None, ts=0.0)

    with fa.app.app_context():
        _clear()
        _seed(db)
        yield fa.app.test_client()
        db.session.rollback()
        _clear()


def _dept(figures, name):
    return next(d for d in figures["departments"] if d["department"] == name)


# ── Department figures ────────────────────────────────────────────────────────

class TestDepartmentFigures:
    def test_default_period_is_last_three_full_months(self):
        p = pt.default_period(TODAY)
        assert (p.start, p.end) == (date(2026, 7, 1), date(2026, 9, 30))
        assert p.label == "1 July 2026 to 30 September 2026"

    def test_known_answers(self, client):
        f = pt.compute_department_figures(date(2026, 7, 1), date(2026, 9, 30), TODAY)
        d = _dept(f, DFE)
        assert d["named"] == {"questions": 41, "on_time": 30, "pct": 73.2, "pending": 0}
        assert d["ordinary"] == {"questions": 35, "on_time": 28, "pct": 80.0, "pending": 0}
        assert d["questions"] == 76
        # working days to a full answer: 3 x30, 6 x28, 7 x7, 9 x10 -> median 6
        assert d["median_days"] == 6
        assert f["no_due_date"] == 1
        co = _dept(f, CO)
        assert co["named"]["pct"] == 100.0 and co["questions"] == 5

    def test_pending_left_out_and_past_due_counted(self, client):
        f = pt.compute_department_figures(date(2026, 10, 1), TODAY, TODAY)
        d = _dept(f, DFE)
        assert d["named"] == {"questions": 1, "on_time": 0, "pct": 0.0, "pending": 1}
        assert f["pending"] == 1

    def test_cached(self, client, monkeypatch):
        p = pt.default_period(TODAY)
        first = pt.department_figures(p, TODAY)
        monkeypatch.setattr(pt, "compute_department_figures",
                            lambda *a: pytest.fail("should have used the cache"))
        assert pt.department_figures(p, TODAY) is first

    def test_department_record_needs_30_questions(self, client):
        rec = pt.department_record(DFE, TODAY)
        assert rec["named"]["pct"] == 73.2 and rec["ordinary"]["pct"] == 80.0
        assert pt.department_record(CO, TODAY) is None
        assert pt.department_record("Nonexistent Office", TODAY) is None

    def test_median_even_count(self):
        from collections import Counter
        assert pt._median(Counter({2: 1, 5: 1})) == 3.5
        assert pt._median(Counter()) is None

    def test_sort_default_highest_named_day_first(self):
        rows = [{"department": n, "named": {"pct": p}, "ordinary": {"pct": None}, "median_days": None}
                for n, p in (("B", 50.0), ("A", 90.0), ("C", None), ("D", 50.0))]
        assert [r["department"] for r in pt.sort_departments(rows, "named")] == ["A", "B", "D", "C"]
        assert [r["department"] for r in pt.sort_departments(rows, "department")] == ["A", "B", "C", "D"]

    def test_periods(self, client):
        keys = [p.key for p in pt.available_periods(TODAY)]
        assert keys[:3] == ["last-3-months", "session", "last-12-months"]
        # quarters with data, newest first: data runs 30 June to 22 October 2026
        assert keys[3:] == ["2026-q4", "2026-q3", "2026-q2"]
        q4 = pt.available_periods(TODAY)[3]
        assert q4.label == "October to December 2026 (to date)" and q4.end == TODAY


# ── Question page ─────────────────────────────────────────────────────────────

def _page(client, path):
    r = client.get(path)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _path_of(chamber, answered=True):
    from hansard_archive.models import HaPQ
    q = HaPQ.query.filter(HaPQ.chamber == chamber, HaPQ.answering_body == DFE,
                          HaPQ.answer_date == date(2026, 9, 18 if chamber == "Commons" else 30))
    return q.first().url_path


class TestQuestionPage:
    def test_named_day_block_with_department_line(self, client):
        html = _page(client, _path_of("Commons"))
        assert 'id="answer-timing"' in html
        assert ("Named-day question · due Thu 10 September 2026 · answered Fri 18 September 2026 "
                "(6 working days after the named day)") in html
        assert ("Department for Education, questions tabled 1 July 2026 to 30 September 2026: "
                "73.2% of named-day questions answered on time (30 of 41); "
                "80.0% of ordinary questions answered within the expected time (28 of 35).") in " ".join(html.split())
        assert 'href="/written-questions/answer-times">Compare departments' in html

    def test_lords_block_has_no_department_line(self, client):
        html = _page(client, _path_of("Lords"))
        assert "Tabled Mon 7 September 2026 · answered Wed 30 September 2026 (17 working days after tabling)" in html
        assert "The House of Lords expects answers within 10 working days of tabling." in html
        assert "Compare departments" not in html

    @pytest.mark.parametrize("state", ["hidden", "beta"])
    def test_not_public_unless_live(self, client, monkeypatch, state):
        monkeypatch.setenv(FLAG, state)
        html = _page(client, _path_of("Commons"))
        assert 'id="answer-timing"' not in html

    def test_beta_visible_to_admin(self, client, monkeypatch):
        monkeypatch.setenv(FLAG, "beta")
        with client.session_transaction() as s:
            s["admin_authenticated"] = True
        assert 'id="answer-timing"' in _page(client, _path_of("Commons"))

    def test_unexpected_error_leaves_the_block_out(self, client, monkeypatch):
        def boom(*a):
            raise ValueError("unexpected")
        monkeypatch.setattr(pt, "pq_answer_timing", boom)
        html = _page(client, _path_of("Commons"))
        assert 'id="answer-timing"' not in html

    def test_missing_deadline_table_shows_nothing(self, client, fa):
        from extensions import db
        from hansard_archive.models import HaPQDeadline
        path = _path_of("Commons")
        HaPQDeadline.__table__.drop(db.engine)
        try:
            html = _page(client, path)
            assert 'id="answer-timing"' not in html
        finally:
            db.session.rollback()
            HaPQDeadline.__table__.create(db.engine)


# ── League table ──────────────────────────────────────────────────────────────

URL = "/written-questions/answer-times"


class TestLeagueTable:
    def test_renders_default_period(self, client):
        html = _page(client, URL)
        flat = " ".join(html.split())
        assert "<h1" in html and "Written question answer times by department" in html
        assert "questions tabled 1 July 2026 to 30 September 2026" in flat
        main = flat[flat.index('id="pat-main"'):flat.index('id="pat-small"')]
        assert ("<td>Department for Education</td> <td>41</td> <td>73.2% <span class=\"pat-n\">(30)</span></td> "
                "<td>35</td> <td>80.0% <span class=\"pat-n\">(28)</span></td> <td>6</td>") in main
        assert CO not in main
        assert '<link rel="canonical" href="https://westminsterbrief.co.uk/written-questions/answer-times">' in html
        assert '"@type": "Dataset"' in html
        assert 'content="index, follow"' in html

    def test_small_department_listed_separately(self, client):
        flat = " ".join(_page(client, URL).split())
        small = flat[flat.index('id="pat-small"'):]
        assert "<td>Cabinet Office</td>" in small
        assert "100.0% <span class=\"pat-n\">(5)</span> †" in small

    def test_methodology(self, client):
        flat = " ".join(_page(client, URL).split())
        assert 'id="methodology"' in flat
        assert "85% as its benchmark" in flat
        assert "206 of 265 (77.7%)" in flat and "1,702 of 2,227 (76.4%)" in flat
        assert "https://committees.parliament.uk/committee/126/procedure-committee/" in flat
        assert "Companion to the Standing Orders, 6.41" in flat
        assert "Figures calculated" in flat
        for word in ("failed", "late", "poor"):
            assert f" {word} " not in flat.lower()

    def test_period_selector(self, client):
        html = _page(client, URL + "?period=2026-q4")
        flat = " ".join(html.split())
        assert "questions tabled 1 October 2026 to 24 October 2026 (period still running)" in flat
        assert "1 question not yet answered and not yet past the expected date is left out" in flat
        assert 'value="2026-q4" selected' in html
        assert 'content="noindex, follow"' in html

    def test_unknown_period_404(self, client):
        assert client.get(URL + "?period=1999-q1").status_code == 404

    def test_sort_and_csv(self, client):
        assert client.get(URL + "?sort=median").status_code == 200
        assert client.get(URL + "?sort=nonsense").status_code == 200
        r = client.get(URL + "?format=csv")
        assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/csv")
        body = r.get_data(as_text=True)
        assert "Department for Education,41,30,73.2,35,28,80.0,6.0,2026-07-01,2026-09-30" in body

    def test_csv_neutralises_formulas(self, client):
        from extensions import db
        _add(db, '=HYPERLINK("http://x","y")', MON, THU, dfa=THU, named=True)
        pt.clear_caches()
        body = client.get(URL + "?format=csv").get_data(as_text=True)
        assert '"\'=HYPERLINK(""http://x"",""y"")",' in body
        assert "\n=HYPERLINK" not in body

    @pytest.mark.parametrize("state", ["hidden", "beta", ""])
    def test_not_public_unless_live(self, client, monkeypatch, state):
        monkeypatch.setenv(FLAG, state)
        assert client.get(URL).status_code == 404

    def test_flag_unset_is_404(self, client, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        assert client.get(URL).status_code == 404

    def test_beta_visible_to_admin(self, client, monkeypatch):
        monkeypatch.setenv(FLAG, "beta")
        with client.session_transaction() as s:
            s["admin_authenticated"] = True
        assert client.get(URL).status_code == 200

    def test_missing_deadline_table(self, client):
        from extensions import db
        from hansard_archive.models import HaPQDeadline
        HaPQDeadline.__table__.drop(db.engine)
        try:
            html = _page(client, URL)
            assert "The figures are not available at the moment." in html
        finally:
            db.session.rollback()
            HaPQDeadline.__table__.create(db.engine)

    def test_sitemap_only_when_live(self, client, fa, monkeypatch):
        assert "/written-questions/answer-times" in client.get("/sitemap-core.xml").get_data(as_text=True)
        monkeypatch.setenv(FLAG, "beta")
        fa._sitemap_core_cache.update(xml=None, ts=0.0)
        with client.session_transaction() as s:
            s["admin_authenticated"] = True     # even the owner's request must not add it
        assert "/written-questions/answer-times" not in client.get("/sitemap-core.xml").get_data(as_text=True)

    def test_tab_on_scanner_only_when_enabled(self, client, monkeypatch):
        assert 'href="/written-questions/answer-times"' in client.get("/written-questions").get_data(as_text=True)
        monkeypatch.setenv(FLAG, "hidden")
        assert 'href="/written-questions/answer-times"' not in client.get("/written-questions").get_data(as_text=True)
