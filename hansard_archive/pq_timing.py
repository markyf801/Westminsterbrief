"""
Written Question answer times: the "Answer timing" facts on each question
page and the department figures behind /written-questions/answer-times.

Definitions (validated against the Procedure Committee's published counts for
17 July to 6 December 2024; see docs/plans/pq-pages-plan-2026-10-24.md):

- Commons named-day question: on time = a substantive answer (not a holding
  answer) on or before the named day (ha_pq_deadline.date_for_answer).
- Commons ordinary question: Parliament's expectation is an answer "within five
  working days of being tabled". We count a substantive answer on or before
  date_for_answer + 4 working days, which reproduces the Committee's counts to
  within about a percentage point.
- Lords: answers are expected within 10 working days of tabling (Companion to
  the Standing Orders, 6.41). No official per-department figures exist, so the
  Lords is shown as facts only, never in the table.
- Withdrawn questions are excluded. A question with no substantive answer
  counts as not on time once its expected date has passed; before that it is
  pending and left out of the rates.

Everything is behind FEATURE_PQ_ANSWER_TIMES (hidden / beta / live).
"""
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from flask_login import current_user
from sqlalchemy import and_, func

from extensions import db
from feature_flags import feature_enabled
from hansard_archive.models import HaPQ, HaPQDeadline
from hansard_archive.working_days import add_working_days, covered, working_days_between

log = logging.getLogger(__name__)

FLAG = "FEATURE_PQ_ANSWER_TIMES"
ORDINARY_EXTRA_WORKING_DAYS = 4
LORDS_EXPECTED_WORKING_DAYS = 10
MIN_QUESTIONS = 30
BENCHMARK_PERCENT = 85
CACHE_TTL = 6 * 3600
FIRST_PERIOD_START = date(2024, 7, 1)
# State Opening of the current session (docs/ops/railway-services.md).
CURRENT_SESSION_START = date(2026, 5, 13)
CURRENT_SESSION_LABEL = "2026–27"

ON_TIME, AFTER, PENDING = "on_time", "after", "pending"

PROCEDURE_COMMITTEE_URL = "https://committees.parliament.uk/committee/126/procedure-committee/"

# Our rules applied to Parliament's data for 17 July - 6 December 2024 against
# the Committee's published counts: (department, named ours, named official,
# ordinary ours, ordinary official), each (on time, questions).
VALIDATION_ROWS = [
    ("Department for Work and Pensions", (206, 265), (206, 265), (678, 808), (673, 808)),
    ("Department of Health and Social Care", (387, 645), (387, 644), (1711, 2226), (1702, 2227)),
    ("Department for Environment, Food and Rural Affairs", (181, 338), (189, 338), (719, 971), (712, 971)),
    ("Ministry of Defence", (201, 257), (202, 257), (598, 737), (591, 737)),
]


def answer_times_enabled() -> bool:
    return feature_enabled(FLAG, current_user)


def answer_times_public() -> bool:
    """True only when the flag is 'live' (for the sitemap, whose cached XML
    must not depend on who happened to request it)."""
    return os.environ.get(FLAG, "hidden").lower().strip() == "live"


def today() -> date:
    """Today's date (one place, so tests can fix it)."""
    return date.today()


def fmt_day(d: date) -> str:
    """'Thu 12 March 2026'."""
    return f"{d.strftime('%a')} {d.day} {d.strftime('%B %Y')}"


def fmt_date(d: date) -> str:
    """'12 March 2026'."""
    return f"{d.day} {d.strftime('%B %Y')}"


def _wd(n: int) -> str:
    return f"{n} working day" + ("" if n == 1 else "s")


def pct(on_time: int, total: int) -> float | None:
    return round(100.0 * on_time / total, 1) if total else None


def fmt_pct(p: float | None) -> str:
    if p is None:
        return "–"
    return f"{p:.1f}%"


# ── Classification (shared by the page and the table) ─────────────────────────

def expected_by(is_named_day: bool, date_for_answer: date) -> date:
    """The last day a Commons answer counts as on time / within the expected time."""
    if is_named_day:
        return date_for_answer
    return add_working_days(date_for_answer, ORDINARY_EXTRA_WORKING_DAYS)


def classify_commons(is_named_day: bool, date_for_answer: date,
                     substantive_answer: date | None, today: date) -> str:
    """ON_TIME / AFTER / PENDING for one Commons question.

    substantive_answer is None when the question is unanswered or its latest
    answer is a holding answer.
    """
    deadline = expected_by(is_named_day, date_for_answer)
    if substantive_answer is not None:
        return ON_TIME if substantive_answer <= deadline else AFTER
    return AFTER if today > deadline else PENDING


# ── Feature 1: the facts on one question page ─────────────────────────────────

def deadline_for(pq: HaPQ) -> HaPQDeadline | None:
    """The question's ha_pq_deadline row: one primary-key lookup. None if
    there is no row or the table can't be read (e.g. not yet created)."""
    if not pq.api_id:
        return None
    try:
        return db.session.get(HaPQDeadline, pq.api_id)
    except Exception as e:
        db.session.rollback()
        log.warning("ha_pq_deadline unreadable, answer timing skipped: %s", str(e)[:120])
        return None


def pq_answer_timing(pq: HaPQ, deadline: HaPQDeadline | None, today: date) -> dict | None:
    """{'facts': [str], 'verdict': str | None, 'note': str | None, 'house': ...}
    or None when nothing reliable can be said (withdrawn, no due date for a
    Commons question, inconsistent dates, dates outside the working-day
    calendar)."""
    tabled = pq.tabled_date
    if pq.is_withdrawn or tabled is None or not covered(tabled):
        return None
    answered = bool(pq.is_answered and pq.answer_date)
    if pq.is_answered and not pq.answer_date:
        return None
    ans = pq.answer_date if answered else None
    if ans is not None and (ans < tabled or not covered(ans)):
        return None
    if ans is None and not covered(today):
        return None
    holding = answered and bool(pq.is_holding)

    if pq.chamber == "Lords":
        return _lords_timing(tabled, ans, holding, today)
    if pq.chamber != "Commons" or deadline is None:
        return None
    dfa, named = deadline.date_for_answer, deadline.is_named_day
    if dfa is None or named is None or dfa < tabled or not covered(dfa):
        return None
    if named:
        return _named_day_timing(dfa, ans, holding, today)
    return _ordinary_timing(tabled, dfa, ans, holding, today)


def _holding_fact(ans: date) -> str:
    return f"holding answer given on {fmt_day(ans)}; no full answer recorded yet"


def _after_tabling(tabled: date, ans: date) -> str:
    if ans == tabled:
        return "the day it was tabled"
    n = working_days_between(tabled, ans)
    return f"{_wd(n)} after tabling" if n else "the same working day as tabling"


def _named_day_timing(dfa, ans, holding, today) -> dict:
    facts = ["Named-day question", f"due {fmt_day(dfa)}"]
    if holding:
        facts.append(_holding_fact(ans))
    elif ans is not None:
        if ans == dfa:
            facts.append(f"answered {fmt_day(ans)}, on the named day")
        elif ans < dfa:
            facts.append(f"answered {fmt_day(ans)}, before the named day")
        else:
            n = working_days_between(dfa, ans)
            facts.append(f"answered {fmt_day(ans)} ({_wd(n)} after the named day)" if n
                         else f"answered {fmt_day(ans)}, after the named day")
    elif today > dfa:
        n = working_days_between(dfa, today)
        facts.append(f"awaiting answer, {_wd(n)} after the named day" if n
                     else "awaiting answer, after the named day")
    else:
        facts.append("not yet answered")
    return {"house": "Commons", "kind": "named", "facts": facts, "verdict": None, "note": None}


def _ordinary_timing(tabled, dfa, ans, holding, today) -> dict:
    facts = ["Ordinary question", f"tabled {fmt_day(tabled)}"]
    verdict = None
    if holding:
        facts.append(_holding_fact(ans))
    elif ans is not None:
        facts.append(f"answered {fmt_day(ans)} ({_after_tabling(tabled, ans)})")
        verdict = ("within the expected time" if classify_commons(False, dfa, ans, today) == ON_TIME
                   else "after the expected time")
    else:
        n = working_days_between(tabled, today)
        if classify_commons(False, dfa, None, today) == AFTER:
            facts.append(f"awaiting answer, {_wd(n)} since tabling")
            verdict = "after the expected time"
        else:
            facts.append(f"not yet answered ({_wd(n)} since tabling)")
    return {"house": "Commons", "kind": "ordinary", "facts": facts, "verdict": verdict, "note": None}


def _lords_timing(tabled, ans, holding, today) -> dict:
    facts = [f"Tabled {fmt_day(tabled)}"]
    if holding:
        facts.append(_holding_fact(ans))
    elif ans is not None:
        facts.append(f"answered {fmt_day(ans)} ({_after_tabling(tabled, ans)})")
    else:
        facts.append(f"not yet answered ({_wd(working_days_between(tabled, today))} since tabling)")
    return {"house": "Lords", "kind": "lords", "facts": facts, "verdict": None,
            "note": f"The House of Lords expects answers within {LORDS_EXPECTED_WORKING_DAYS} working days of tabling."}


# ── Periods ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date
    end: date
    to_date: bool = False   # still running: recent questions may be pending


_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, 1)


def _range_label(start: date, end: date) -> str:
    return f"{fmt_date(start)} to {fmt_date(end)}"


def last_full_months(today: date, months: int, key: str) -> Period:
    end = _month_start(today) - timedelta(days=1)
    start = _month_start(today, months)
    return Period(key, _range_label(start, end), start, end)


def _quarters(today: date, data_from: date | None, data_to: date | None) -> list[Period]:
    out = []
    if data_from is None or data_to is None:
        return out
    q_start = FIRST_PERIOD_START
    while q_start <= today:
        y, m = q_start.year, q_start.month
        nxt = date(y + (m + 3 > 12), (m + 3 - 1) % 12 + 1, 1)
        q_end = nxt - timedelta(days=1)
        if q_end >= data_from and q_start <= data_to and covered(q_end):
            running = q_end >= today
            end = min(q_end, today)
            label = f"{_MONTH_NAMES[m]} to {_MONTH_NAMES[q_end.month]} {y}" + (" (to date)" if running else "")
            out.append(Period(f"{y}-q{(m - 1) // 3 + 1}", label, q_start, end, running))
        q_start = nxt
    return out


_range_cache: dict = {"ts": 0.0, "value": (None, None)}


def _commons_data_range() -> tuple[date | None, date | None]:
    """(first, last) Commons tabled date from FIRST_PERIOD_START; cached."""
    now = time.time()
    if now - _range_cache["ts"] < CACHE_TTL:
        return _range_cache["value"]
    value = (db.session.query(func.min(HaPQ.tabled_date), func.max(HaPQ.tabled_date))
             .filter(HaPQ.chamber == "Commons", HaPQ.tabled_date >= FIRST_PERIOD_START)
             .one())
    _range_cache.update(ts=now, value=(value[0], value[1]))
    return _range_cache["value"]


def default_period(today: date) -> Period:
    return last_full_months(today, 3, "last-3-months")


def available_periods(today: date) -> list[Period]:
    """Default first, then current session, last 12 months, quarters newest first."""
    first, last = _commons_data_range()
    periods = [default_period(today)]
    if CURRENT_SESSION_START <= today:
        periods.append(Period("session", f"Current session ({CURRENT_SESSION_LABEL}, from "
                                         f"{fmt_date(CURRENT_SESSION_START)})",
                              CURRENT_SESSION_START, today, True))
    periods.append(last_full_months(today, 12, "last-12-months"))
    periods.extend(reversed(_quarters(today, first, last)))
    return periods


# ── Feature 2: department figures for a period ────────────────────────────────

def _new_row(body: str) -> dict:
    return {"department": body,
            "named": {ON_TIME: 0, AFTER: 0, PENDING: 0},
            "ordinary": {ON_TIME: 0, AFTER: 0, PENDING: 0},
            "days": Counter()}


def _median(counter: Counter) -> float | None:
    """Median of a {value: count} distribution (mean of the middle two if even)."""
    n = sum(counter.values())
    if not n:
        return None
    lo_i, hi_i = (n - 1) // 2, n // 2
    lo = hi = None
    seen = 0
    for value in sorted(counter):
        seen += counter[value]
        if lo is None and lo_i < seen:
            lo = value
        if hi_i < seen:
            hi = value
            break
    return (lo + hi) / 2


def compute_department_figures(start: date, end: date, today: date) -> dict:
    """Per-department figures for Commons questions tabled start..end.

    One GROUP BY query collapses questions sharing a department, type and set
    of dates into one row with a count, so Python classifies a few thousand
    distinct date combinations rather than every question (working-day
    arithmetic is not portable SQL). A second COUNT query finds questions
    with no due date in our data, reported in the notes.
    """
    t0 = time.perf_counter()
    base = and_(HaPQ.chamber == "Commons", HaPQ.is_withdrawn.is_(False),
                HaPQ.tabled_date >= start, HaPQ.tabled_date <= end)
    combos = (db.session.query(HaPQ.answering_body, HaPQDeadline.is_named_day, HaPQ.tabled_date,
                               HaPQDeadline.date_for_answer, HaPQ.answer_date,
                               HaPQ.is_answered, HaPQ.is_holding, func.count(HaPQ.id))
              .join(HaPQDeadline, HaPQDeadline.api_id == HaPQ.api_id)
              .filter(base, HaPQ.answering_body.isnot(None),
                      HaPQDeadline.date_for_answer.isnot(None), HaPQDeadline.is_named_day.isnot(None))
              .group_by(HaPQ.answering_body, HaPQDeadline.is_named_day, HaPQ.tabled_date,
                        HaPQDeadline.date_for_answer, HaPQ.answer_date,
                        HaPQ.is_answered, HaPQ.is_holding)
              .all())
    no_due_date = (db.session.query(func.count(HaPQ.id))
                   .outerjoin(HaPQDeadline, HaPQDeadline.api_id == HaPQ.api_id)
                   .filter(base, (HaPQDeadline.api_id.is_(None))
                           | HaPQDeadline.date_for_answer.is_(None)
                           | HaPQDeadline.is_named_day.is_(None)
                           | HaPQ.answering_body.is_(None))
                   .scalar()) or 0

    rows: dict[str, dict] = {}
    for body, named, tabled, dfa, ans, is_answered, is_holding, n in combos:
        if not covered(dfa):
            no_due_date += n
            continue
        substantive = ans if (is_answered and ans and not is_holding) else None
        row = rows.setdefault(body, _new_row(body))
        outcome = classify_commons(bool(named), dfa, substantive, today)
        row["named" if named else "ordinary"][outcome] += n
        if substantive is not None and substantive >= tabled:
            row["days"][working_days_between(tabled, substantive)] += n

    departments = []
    pending = 0
    for row in rows.values():
        out = {"department": row["department"]}
        for kind in ("named", "ordinary"):
            c = row[kind]
            decided = c[ON_TIME] + c[AFTER]
            out[kind] = {"questions": decided, "on_time": c[ON_TIME], "pct": pct(c[ON_TIME], decided),
                         "pending": c[PENDING]}
            pending += c[PENDING]
        out["questions"] = out["named"]["questions"] + out["ordinary"]["questions"]
        out["median_days"] = _median(row["days"])
        departments.append(out)
    elapsed = time.perf_counter() - t0
    log.info("pq answer times %s..%s: %d combos, %d departments, %.3fs",
             start, end, len(combos), len(departments), elapsed)
    return {"departments": departments, "pending": pending, "no_due_date": no_due_date,
            "computed_at": datetime.utcnow(), "seconds": elapsed}


_figures_cache: dict[tuple, tuple[float, dict]] = {}


def department_figures(period: Period, today: date) -> dict:
    """compute_department_figures, cached per (start, end) for CACHE_TTL."""
    key = (period.start, period.end)
    now = time.time()
    hit = _figures_cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = compute_department_figures(period.start, period.end, today)
    if len(_figures_cache) > 64:
        _figures_cache.clear()
    _figures_cache[key] = (now, value)
    return value


def clear_caches() -> None:
    _figures_cache.clear()
    _range_cache.update(ts=0.0, value=(None, None))


def department_record(answering_body: str | None, today: date) -> dict | None:
    """The department's figures for the default period, for one question page,
    keeping only categories with at least MIN_QUESTIONS questions. None if
    neither qualifies or the figures can't be computed."""
    if not answering_body:
        return None
    period = default_period(today)
    try:
        figures = department_figures(period, today)
    except Exception as e:
        db.session.rollback()
        log.warning("pq answer times: department figures unavailable: %s", str(e)[:120])
        return None
    for d in figures["departments"]:
        if d["department"] == answering_body:
            named = d["named"] if d["named"]["questions"] >= MIN_QUESTIONS else None
            ordinary = d["ordinary"] if d["ordinary"]["questions"] >= MIN_QUESTIONS else None
            if not (named or ordinary):
                return None
            return {"department": answering_body, "period": period, "named": named, "ordinary": ordinary}
    return None


SORTS = ("named", "ordinary", "median", "department")


def sort_departments(departments: list[dict], sort: str) -> list[dict]:
    """Default 'named': highest named-day on-time rate first. Departments with
    no figure for the sort column go last; ties by name."""
    def key(d):
        if sort == "department":
            return (0, 0, d["department"])
        if sort == "median":
            v = d["median_days"]
            return (v is None, v if v is not None else 0, d["department"])
        v = d["ordinary" if sort == "ordinary" else "named"]["pct"]
        return (v is None, -(v or 0), d["department"])
    return sorted(departments, key=key)


def validation_table() -> list[dict]:
    out = []
    for dept, n_ours, n_off, o_ours, o_off in VALIDATION_ROWS:
        out.append({"department": dept,
                    "named_ours": n_ours, "named_official": n_off,
                    "named_ours_pct": pct(*n_ours), "named_official_pct": pct(*n_off),
                    "ordinary_ours": o_ours, "ordinary_official": o_off,
                    "ordinary_ours_pct": pct(*o_ours), "ordinary_official_pct": pct(*o_off)})
    return out
