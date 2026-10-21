"""
When does Parliament publish written questions and answers?

Parliament's API gives dates only (dateTabled, dateAnswered, dateHoldingAnswer
are all T00:00:00), so the time of day has to come from our own observations.
Each quick-check run (scripts/ingest_pq_cron.py --quick-check) writes its API
and DB counts into ha_cron_run.notes in the format produced by
``format_counts``; the run's started_at is the observation time.

From a series of observations of the same window on the same day, an increase
in the API count means something was published between the two observations.
On the first observation of a day, API > DB means something was published
since the last sync (before the first check) — a "censored" event.

Pure functions only — scripts/pq_publication_times.py does the DB reading.
"""

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

SLOT_MINUTES = 30
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A recommendation needs this many sitting days per weekday (Mon-Thu) first.
MIN_DAYS_PER_WEEKDAY = 4

_COUNT_RE = re.compile(
    r"(?P<kind>tabled|answered) (?P<from>\d{4}-\d\d-\d\d)(?:\.\.(?P<to>\d{4}-\d\d-\d\d))?"
    r" API=(?P<api>\d+) DB=(?P<db>\d+)"
)


# ── recording format ─────────────────────────────────────────────────────────

def format_counts(checks: list[dict]) -> str:
    """'tabled 2026-10-17..2026-10-20 API=862 DB=862; answered 2026-10-20 API=165 DB=160'"""
    parts = []
    for c in checks:
        span = (c["from"].isoformat() if c["from"] == c["to"]
                else f"{c['from'].isoformat()}..{c['to'].isoformat()}")
        parts.append(f"{c['kind']} {span} API={c['api']} DB={c['db']}")
    return "; ".join(parts)


@dataclass(frozen=True)
class Observation:
    at: datetime          # UTC
    kind: str             # tabled | answered
    date_from: date
    date_to: date
    api: int
    db: int


def parse_counts(notes: str | None, at: datetime) -> list[Observation]:
    if not notes:
        return []
    out = []
    for m in _COUNT_RE.finditer(notes):
        d_from = date.fromisoformat(m["from"])
        d_to = date.fromisoformat(m["to"]) if m["to"] else d_from
        out.append(Observation(at, m["kind"], d_from, d_to, int(m["api"]), int(m["db"])))
    return out


# ── UK time ──────────────────────────────────────────────────────────────────

def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def uk_offset_hours(at_utc: datetime) -> int:
    """1 during BST (last Sun of March 01:00 UTC to last Sun of October 01:00 UTC)."""
    start = datetime.combine(_last_sunday(at_utc.year, 3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(at_utc.year, 10), datetime.min.time()) + timedelta(hours=1)
    return 1 if start <= at_utc < end else 0


def to_uk(at_utc: datetime) -> datetime:
    return at_utc + timedelta(hours=uk_offset_hours(at_utc))


def slot_of(at_uk: datetime) -> int:
    return (at_uk.hour * 60 + at_uk.minute) // SLOT_MINUTES


def slot_label(slot: int) -> str:
    m = slot * SLOT_MINUTES
    return f"{m // 60:02d}:{m % 60:02d}"


# ── events ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    category: str         # questions | answers | late_answers
    day: date             # UK date of the observation
    at_uk: datetime
    size: int
    censored: bool        # seen at the day's first check — published earlier


def _category(obs: Observation, day: date) -> str | None:
    if obs.kind == "tabled":
        return "questions"
    if obs.date_from != obs.date_to:
        return None
    return "answers" if obs.date_to == day else "late_answers"


def find_events(observations: list[Observation]) -> tuple[list[Event], dict]:
    """Publication events, plus {category: {uk_date: first check time (UK)}}."""
    series: dict = defaultdict(list)
    for obs in observations:
        at_uk = to_uk(obs.at)
        cat = _category(obs, at_uk.date())
        if cat:
            series[(cat, obs.date_from, obs.date_to, at_uk.date())].append((at_uk, obs))

    events: list[Event] = []
    first_check: dict = defaultdict(dict)
    for (cat, _f, _t, day), rows in series.items():
        rows.sort(key=lambda r: r[0])
        prev_first = first_check[cat].get(day)
        if prev_first is None or rows[0][0] < prev_first:
            first_check[cat][day] = rows[0][0]
        first_at, first = rows[0]
        if first.api > first.db:
            events.append(Event(cat, day, first_at, first.api - first.db, True))
        for (_pa, prev), (at, cur) in zip(rows, rows[1:]):
            if cur.api > prev.api:
                events.append(Event(cat, day, at, cur.api - prev.api, False))
    events.sort(key=lambda e: e.at_uk)
    return events, first_check


# ── summary ──────────────────────────────────────────────────────────────────

def _fmt(t: datetime | None) -> str:
    return t.strftime("%H:%M") if t else "-"


def _median_time(times: list[datetime]) -> str:
    if not times:
        return "-"
    mins = statistics.median(t.hour * 60 + t.minute for t in times)
    return f"{int(mins) // 60:02d}:{int(mins) % 60:02d}"


def _shortest_cover(weights: list[float], share: float) -> tuple[int, int] | None:
    total = sum(weights)
    if total <= 0:
        return None
    best = None
    for a in range(len(weights)):
        acc = 0.0
        for b in range(a, len(weights)):
            acc += weights[b]
            if acc >= share * total:
                if best is None or (b - a) < (best[1] - best[0]):
                    best = (a, b)
                break
    return best


def summarise(events: list[Event], first_check: dict, share: float = 0.95,
              margin_slots: int = 1) -> dict:
    """Per category and weekday: days seen, earliest/median/latest first-seen,
    % of days with a publication in each slot; plus a recommended window."""
    out: dict = {"categories": {}, "recommendation": None, "censored_share": {}}
    union: tuple[int, int] | None = None

    for cat in ("questions", "answers", "late_answers"):
        cat_events = [e for e in events if e.category == cat]
        days_by_wd: dict = defaultdict(set)
        for day in first_check.get(cat, {}):
            days_by_wd[day.weekday()].add(day)
        pub_days_by_wd: dict = defaultdict(set)
        first_seen: dict = {}
        slot_days: dict = defaultdict(lambda: defaultdict(set))
        weights = [0.0] * SLOTS_PER_DAY
        censored_days = set()
        for e in cat_events:
            wd = e.day.weekday()
            pub_days_by_wd[wd].add(e.day)
            first_seen.setdefault(e.day, e)
            slot_days[wd][slot_of(e.at_uk)].add(e.day)
            weights[slot_of(e.at_uk)] += e.size
            if e.censored:
                censored_days.add(e.day)

        per_wd = {}
        for wd in sorted(set(days_by_wd) | set(pub_days_by_wd)):
            pub_days = sorted(pub_days_by_wd.get(wd, ()))
            firsts = [first_seen[d].at_uk for d in pub_days]
            per_wd[WEEKDAYS[wd]] = {
                "days_checked": len(days_by_wd.get(wd, ())),
                "days_with_publication": len(pub_days),
                "first_seen_earliest": _fmt(min(firsts, key=lambda t: t.time())) if firsts else "-",
                "first_seen_median": _median_time(firsts),
                "first_seen_latest": _fmt(max(firsts, key=lambda t: t.time())) if firsts else "-",
                "slot_pct": {slot_label(s): round(100 * len(ds) / len(pub_days))
                             for s, ds in sorted(slot_days[wd].items())} if pub_days else {},
            }
        cover = _shortest_cover(weights, share)
        out["categories"][cat] = {
            "weekdays": per_wd,
            "cover": (slot_label(cover[0]), slot_label(cover[1])) if cover else None,
            "censored_days": len(censored_days),
            "days_with_publication": len(first_seen),
        }
        if cover and cat != "late_answers":
            union = cover if union is None else (min(union[0], cover[0]), max(union[1], cover[1]))

    sitting = out["categories"]["questions"]["weekdays"]
    enough = all(sitting.get(WEEKDAYS[wd], {}).get("days_with_publication", 0)
                 >= MIN_DAYS_PER_WEEKDAY for wd in range(4))
    out["enough_data"] = enough
    if union:
        start = max(0, union[0] - margin_slots)
        end = min(SLOTS_PER_DAY - 1, union[1] + margin_slots)
        out["recommendation"] = recommend_cron(start, end)
    return out


def recommend_cron(start_slot: int, end_slot: int) -> dict:
    """UK-time window -> Railway (UTC) cron lines for GMT and BST, checks at :15 and :45."""
    start_h = start_slot * SLOT_MINUTES // 60
    end_h = (end_slot * SLOT_MINUTES + SLOT_MINUTES - 1) // 60
    result = {"uk_window": f"{slot_label(start_slot)}-{slot_label(end_slot + 1) if end_slot + 1 < SLOTS_PER_DAY else '24:00'}"}
    for name, offset in (("gmt", 0), ("bst", 1)):
        lo, hi = max(0, start_h - offset), max(0, end_h - offset)
        result[f"cron_utc_{name}"] = f"15,45 {lo}-{hi} * * 1-5"
    lo = max(0, start_h - 1)
    result["cron_utc_both"] = f"15,45 {lo}-{end_h} * * 1-5"
    return result


def stored_time_estimate(rows: list[tuple[date, datetime]], same_day: bool = False) -> dict:
    """Coarse estimate from data we already have, before any quick-check runs.

    rows: (date, first stored at UTC) pairs — ha_pq (tabled_date, ingested_at)
    for questions (same_day=False: stored the next day, or Monday for Fri/Sat),
    or (answer_date, updated_at) for answers (same_day=True). The old crons ran
    only a few times a day, so each time is an UPPER bound on publication.
    Returns {UK slot label: rows}.
    """
    counts: dict = defaultdict(int)
    for d, stored_at in rows:
        if d is None or stored_at is None:
            continue
        uk = to_uk(stored_at)
        lag = (uk.date() - d).days
        if same_day:
            keep = lag == 0
        else:
            keep = lag == 1 or (lag in (2, 3) and uk.weekday() == 0)   # Fri/Sat -> Mon
        if keep:
            counts[slot_label(slot_of(uk))] += 1
    return dict(sorted(counts.items()))
