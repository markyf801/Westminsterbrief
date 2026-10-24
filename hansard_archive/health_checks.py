"""
Daily site-health checks for Westminster Brief.

Used by ``scripts/daily_health_report.py``. Every check is read-only and returns
a ``CheckResult`` (name, status, detail, last success). Statuses:

  OK       — fresh / working
  WARN     — stale or odd; worth a look
  FAIL     — clearly broken
  SKIPPED  — cannot be checked here (table not created yet, R2 not configured)

A table that doesn't exist yet is SKIPPED, never FAIL, so the report can list
checks for tables that are still being built.

Tables are read with SQLAlchemy reflection rather than the ORM models, so this
module keeps working while other modules' models change.

Sitting-day rule ("no new data" checks for Hansard and written questions)
-------------------------------------------------------------------------
Sitting days come from Parliament's public What's On API, once per run, per
House: ``GET https://whatson-api.parliament.uk/calendar/proceduraldates/
{Commons|Lords}/sittingdates.json?startDate=...&endDate=...`` (read-only, no
key). Data for a sitting day is *overdue* once the following working day
(Mon-Fri) has fully passed: Hansard is published the same or next day, and
questions tabled on a sitting day appear the next working day. One or more
overdue sitting days for a House is a WARN. Example: Commons sat Thursday;
Thursday's data is due by the end of Friday, so on Monday 07:30 it must be in.
Recess days simply aren't sitting days, so recesses never warn.

If the calendar can't be fetched, the checks fall back to a weekday rule (WARN
when more than ``max_weekday_gap`` weekdays pass without new data) and say so
in the report line. ``RECESS_PERIODS`` is a manual override: days in it are
never expected to have data, under either rule.

Cron "active hours"
-------------------
Cron ages are measured only over the days the job is scheduled to run, so a
Mon-Fri job checked on Monday morning isn't flagged for the weekend. E.g. the
last Mon-Fri run on Friday 14:00, checked Monday 07:30, is 10h + 7.5h = 17.5
active hours old.

Cron runs that never finished
-----------------------------
A run writes its ``ha_cron_run`` row as "running" at the start and sets
``finished_at`` and ok/failed at the end. A run stopped by a deploy (every push
to master redeploys the crons) or a restart keeps "running" with no
``finished_at`` for ever; older than ``cron_stuck_hours`` it counts as
unfinished, not failed. It stops mattering once a later run of the same service
finishes. For the PQ sweeps (``pq_crons`` "group"), another sweep finishing ok
after it covers it, while the unfinished run is younger than the service's
``warn_hours`` (Railway skips a cron's next run while one is still running, so
a hung run must not stay covered). One-off runs (``pq_crons`` "one_off") are
listed for information only; any other ``pq-*`` name is a WARN.

Bank holidays are not known here: ``_sitting_weekdays_back`` (recent answers,
heading lag) counts them as working days.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import sqlalchemy as sa

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
PROBLEM_STATUSES = (WARN, FAIL)
_STATUS_ORDER = {FAIL: 0, WARN: 1, SKIPPED: 2, OK: 3}

# Manual override: inclusive (start, end) ISO dates on which no new Hansard/PQ
# data is expected, whatever the calendar says. Normally empty: the sitting
# calendar is fetched from Parliament's What's On API each run.
RECESS_PERIODS: list[tuple[str, str]] = []

CALENDAR_URL = "https://whatson-api.parliament.uk/calendar/proceduraldates/{house}/sittingdates.json"

_DAYS = {
    "daily": {0, 1, 2, 3, 4, 5, 6},
    "mon-fri": {0, 1, 2, 3, 4},
    "mon-thu": {0, 1, 2, 3},
    "mon": {0},
    "fri": {4},
    "sat": {5},
    "sun": {6},
}

# All thresholds in one place. Hours are "active hours" for the job's days
# (see module docstring); "daily" means plain clock hours. A fail value of None
# means the check can WARN but never FAIL.
CONFIG: dict = {
    "calendar": {
        "lookback_days": 90,              # sitting days fetched for [today - N, today]; > longest recess
        "timeout_seconds": 15,
    },
    "archive": {
        "overdue_sitting_days_warn": 1,   # calendar rule (see module docstring)
        "max_weekday_gap": 4,             # fallback rule when the calendar is unavailable
        "houses": ["Commons", "Lords"],
    },
    "archive_crons": {
        # ha_cron_run.service_name -> schedule (see docs/ops/railway-services.md)
        "morning-catchup": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50},
        "daytime-mth": {"days": "mon-thu", "warn_hours": 26, "fail_hours": 50},
        "daytime-fri": {"days": "fri", "warn_hours": 26, "fail_hours": None},
    },
    "pq": {
        "overdue_sitting_days_warn": 1,   # newest tabled_date per chamber, calendar rule
        "max_weekday_gap": 4,             # fallback rule
        "chambers": ["Commons", "Lords"],
        "write_warn_hours": 26,           # newest updated_at/ingested_at, Mon-Fri hours
        "write_fail_hours": 50,
        "heading_window_days": 14,
        # Parliament adds a question's heading 2-3 working days after tabling
        # (checked 23 Oct 2026), so the newest questions aren't counted.
        "heading_lag_working_days": 3,
        "missing_heading_warn_pct": 5.0,
        "missing_heading_min_count": 10,
        "answers_window_weekdays": 3,     # answered in the last N sitting weekdays
    },
    # Unique indexes created by hand (docs/plans/pq-identity-phase3-sql.md step 4).
    "pq_integrity": {"indexes": ["uq_ha_pq_api_id", "uq_ha_pq_date_uin"]},
    "pq_crons": {
        # ha_cron_run.service_name (the start command's --service-name, not the
        # Railway service name) -> schedule; see docs/ops/railway-services.md.
        # "group": services doing the same full 7-day sweep, so one that was
        # interrupted is covered when another finishes after it.
        # "optional": no runs yet is SKIPPED (service not created), not WARN.
        "known": {
            "pq-cron-early": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50, "group": "pq-sweep"},
            "pq-cron-morning": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50, "group": "pq-sweep"},
            "pq-cron-afternoon": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50, "group": "pq-sweep"},
            "pq-cron-monday": {"days": "mon", "warn_hours": 26, "fail_hours": 50, "group": "pq-sweep"},
            "pq-quickcheck": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50},
            "pq-cron-weekly": {"days": "sat", "warn_hours": 30, "fail_hours": None, "optional": True},
        },
        # One-off runs (started by hand): listed for information, never judged.
        "one_off": ["pq-catchup", "pq-retag"],   # pq-retag: docs/plans/pq-identity-phase3-sql.md step 7
        # Any other "pq-*" service seen in the last N days is a WARN: probably a
        # scheduled service recording a name this config doesn't know.
        "discover_days": 30,
        "ignore": ["backfill", "manual"],
    },
    # Share of recent items with at least one topic tag. Window ends yesterday so
    # items ingested this morning (tagged by the same cron run) don't skew it.
    "tagging": {"window_days": 7, "warn_pct": 90.0, "min_items": 20},
    # Archive search relies on Postgres splitting "Starmer’s" (curly apostrophe)
    # into 'starmer', which depends on the database locale (LC_CTYPE).
    "search_locale": {"sample": "Starmer’s", "expect": "starmer"},
    # pg_database_size(current_database()) against the Postgres volume size,
    # DB_VOLUME_GB on the health-report service (default below). WAL and other
    # files also live on the volume, so it is a little fuller than this.
    "db_size": {"default_volume_gb": 5, "warn_pct": 70.0, "fail_pct": 85.0},
    "cron_recent_failures_hours": 24,     # failed runs in this window are listed in the detail
    "cron_stuck_hours": 3,                # latest run still "running" after this: interrupted
    "cron_interrupted_streak_warn": 3,    # grouped service: this many unfinished runs in a row -> WARN
    "bills": {"warn_hours": 48, "fail_hours": 96},              # bills-cron daily 06:30
    "edms": {"warn_hours": 48, "fail_hours": 96},               # edm-cron daily 04:30, ha_edm.last_refreshed
    "edm_cron": {"service": "edm-cron", "warn_hours": 26, "fail_hours": 50},
    # member-profile-refresh weekly (Sun 03:00): member_profile.summary_refreshed_at
    "member_profile": {"warn_hours": 8 * 24, "fail_hours": None},
    # member-bio-refresh monthly (2nd, 04:00). Bios are only regenerated when their inputs
    # change, so the run itself is dated by its member_name_audit row (same job).
    "member_biography": {"warn_hours": 35 * 24, "fail_hours": None},
    "cached_member": {"warn_hours": 8 * 24, "fail_hours": 15 * 24, "min_fresh_rows": 400},
    # The stats-refresh job (`flask stats refresh-all`) is intentionally NOT scheduled yet,
    # so this check reports SKIPPED with its age instead of a daily WARN. Set
    # "scheduled": True once the Railway service exists.
    "headline_stats": {"scheduled": False, "warn_hours": 8 * 24, "fail_hours": None},
    "upcoming_releases": {"warn_hours": 24, "fail_hours": 72},  # upcoming-refresh daily 05:45
    "rediscovery": {"warn_hours": 48, "fail_hours": 96},
    "committee_evidence": {"service": "committee-evidence-cron", "warn_hours": 8 * 24, "fail_hours": None},
    "backup_record": {"service": "backup-r2", "warn_hours": 26, "fail_hours": 50},
    "backup_r2": {"prefix": "daily/", "warn_hours": 26, "fail_hours": 50},
    "web": {
        "base_url": "https://westminsterbrief.co.uk",
        "paths": ["/ping", "/health"],
        "timeout_seconds": 10,
        "slow_ms": 3000,
    },
    "weekly_summary_weekday": 0,          # Monday: send an all-OK summary
}

# Candidate timestamp columns for tables that don't exist yet.
_REFRESH_COLUMNS = ("last_refreshed", "refreshed_at", "updated_at", "fetched_at", "cached_at", "ingested_at")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    last_success: datetime | date | None = None


class HealthContext:
    """Shared state for one report run: DB connection, clock, config."""

    def __init__(self, conn, now: datetime, config: dict | None = None,
                 recess_periods: list[tuple[str, str]] | None = None,
                 sitting_days: dict[str, set[date]] | None = None, calendar_note: str = ""):
        """sitting_days: {"Commons": {...}, "Lords": {...}}, or None when the calendar
        couldn't be fetched (the checks then use the weekday fallback rule)."""
        self.conn = conn
        self.sitting_days = sitting_days
        self.calendar_note = calendar_note
        self.now = now
        self.today = now.date()
        self.config = config or CONFIG
        periods = RECESS_PERIODS if recess_periods is None else recess_periods
        self.recess = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in periods]
        self._inspector = sa.inspect(conn)
        self._tables: dict[str, sa.Table] = {}

    def has_table(self, name: str) -> bool:
        return self._inspector.has_table(name)

    def table(self, name: str) -> sa.Table:
        if name not in self._tables:
            self._tables[name] = sa.Table(name, sa.MetaData(), autoload_with=self.conn, resolve_fks=False)
        return self._tables[name]

    def scalar(self, stmt):
        return self.conn.execute(stmt).scalar()

    def in_recess(self, d: date) -> bool:
        return any(a <= d <= b for a, b in self.recess)


# ---------------------------------------------------------------------------
# Parliament sitting calendar
# ---------------------------------------------------------------------------

def fetch_sitting_days(http_get: Callable, today: date, houses=("Commons", "Lords"),
                       config: dict | None = None) -> tuple[dict[str, set[date]] | None, str]:
    """Sitting days per House from the What's On API, or (None, reason) on failure."""
    cfg = (config or CONFIG)["calendar"]
    start = today - timedelta(days=cfg["lookback_days"])
    result: dict[str, set[date]] = {}
    try:
        for house in houses:
            resp = http_get(CALENDAR_URL.format(house=house),
                            params={"startDate": start.isoformat(), "endDate": today.isoformat()},
                            timeout=cfg["timeout_seconds"])
            if resp.status_code != 200:
                return None, f"calendar HTTP {resp.status_code} for {house}"
            result[house] = {date.fromisoformat(row["Date"][:10]) for row in resp.json()["SittingDates"]}
    except Exception as exc:  # noqa: BLE001
        return None, f"calendar unavailable ({type(exc).__name__})"
    if not any(result.values()):
        return None, f"calendar returned no sitting days in the last {cfg['lookback_days']} days"
    return result, ""


def next_working_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def overdue_sitting_days(ctx: HealthContext, house: str, latest: date) -> list[date]:
    """Sitting days after `latest` whose publication window (next working day) has passed."""
    return sorted(d for d in ctx.sitting_days.get(house, set())
                  if d > latest and next_working_day(d) < ctx.today and not ctx.in_recess(d))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _coerce(value):
    """Normalise DB values to naive-UTC datetime / date (SQLite may return str)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def active_hours_since(ts: datetime, now: datetime, days: set[int]) -> float:
    """Hours between ts and now that fall on the given weekdays (0=Mon)."""
    if ts >= now:
        return 0.0
    total = 0.0
    day = ts.date()
    while day <= now.date():
        if day.weekday() in days:
            start = max(ts, datetime.combine(day, datetime.min.time()))
            end = min(now, datetime.combine(day + timedelta(days=1), datetime.min.time()))
            if end > start:
                total += (end - start).total_seconds() / 3600
        day += timedelta(days=1)
    return total


def weekday_gap(ctx: HealthContext, latest: date) -> int:
    """Weekdays (not in recess) strictly between latest and today."""
    gap = 0
    d = latest + timedelta(days=1)
    while d < ctx.today:
        if d.weekday() < 5 and not ctx.in_recess(d):
            gap += 1
        d += timedelta(days=1)
    return gap


def _sitting_weekdays_back(ctx: HealthContext, n: int) -> date:
    """The date n non-recess weekdays before today (today excluded)."""
    d = ctx.today
    found = 0
    for _ in range(400):
        d -= timedelta(days=1)
        if d.weekday() < 5 and not ctx.in_recess(d):
            found += 1
            if found >= n:
                break
    return d


def fmt_age(hours: float) -> str:
    if hours < 48:
        return f"{hours:.1f}h"
    days, rem = int(hours // 24), int(hours % 24)
    return f"{days}d {rem}h" if rem else f"{days}d"


def fmt_when(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return value.isoformat()


def _grade(hours: float, warn: float | None, fail: float | None) -> str:
    if fail is not None and hours > fail:
        return FAIL
    if warn is not None and hours > warn:
        return WARN
    return OK


def _freshness(name: str, ts, now: datetime, warn: float | None, fail: float | None,
               days: str = "daily", extra: str = "") -> CheckResult:
    ts = _coerce(ts)
    if ts is None:
        return CheckResult(name, WARN, f"no timestamp found{extra}")
    hours = active_hours_since(ts, now, _DAYS[days])
    status = _grade(hours, warn, fail)
    unit = "" if days == "daily" else f" ({days} hours)"
    limit = f"warn >{fmt_age(warn)}" if warn is not None else "no limit"
    return CheckResult(name, status, f"last update {fmt_age(hours)} ago{unit}, {limit}{extra}", ts)


def _skipped_table(name: str, table: str) -> CheckResult:
    return CheckResult(name, SKIPPED, f"table {table} does not exist")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _gap_check(ctx: HealthContext, name: str, table: str, date_col: str,
               group_col: str, groups: list[str], cfg: dict) -> CheckResult:
    if not ctx.has_table(table):
        return _skipped_table(name, table)
    t = ctx.table(table)
    rows = dict(ctx.conn.execute(
        sa.select(t.c[group_col], sa.func.max(t.c[date_col])).group_by(t.c[group_col])
    ).all())
    parts, worst, newest = [], OK, None
    for g in groups:
        latest = _coerce(rows.get(g))
        if isinstance(latest, datetime):
            latest = latest.date()
        if latest is None:
            parts.append(f"{g}: none")
            worst = min(worst, WARN, key=_STATUS_ORDER.get)
            continue
        newest = latest if newest is None or latest > newest else newest
        flag = ""
        if ctx.sitting_days is not None:
            overdue = overdue_sitting_days(ctx, g, latest)
            if len(overdue) >= cfg["overdue_sitting_days_warn"]:
                worst = min(worst, WARN, key=_STATUS_ORDER.get)
                shown = ", ".join(d.isoformat() for d in overdue[:3]) + (" ..." if len(overdue) > 3 else "")
                flag = f" ({len(overdue)} sitting day(s) missing: {shown})"
        else:
            gap = weekday_gap(ctx, latest)
            if gap > cfg["max_weekday_gap"]:
                worst = min(worst, WARN, key=_STATUS_ORDER.get)
                flag = f" ({gap} weekdays without new data)"
        parts.append(f"{g}: {latest.isoformat()}{flag}")
    detail = "; ".join(parts)
    if ctx.sitting_days is not None:
        detail += " [sitting calendar]"
    else:
        detail += f" [{ctx.calendar_note or 'calendar not loaded'}: weekday rule, limit {cfg['max_weekday_gap']}]"
    return CheckResult(name, worst, detail, newest)


def check_archive_sessions(ctx: HealthContext) -> CheckResult:
    cfg = ctx.config["archive"]
    return _gap_check(ctx, "Hansard archive: newest session", "ha_session", "date",
                      "house", cfg["houses"], cfg)


def _peer_ok_after(ctx: HealthContext, peers: list[str], after: datetime) -> tuple[str, datetime] | None:
    """The peer service whose ok run finished last, if it finished after `after`."""
    if not peers:
        return None
    t = ctx.table("ha_cron_run")
    row = ctx.conn.execute(
        sa.select(t.c.service_name, t.c.finished_at)
        .where(t.c.service_name.in_(peers), t.c.status == "ok", t.c.finished_at > after)
        .order_by(t.c.finished_at.desc()).limit(1)
    ).first()
    return (row[0], _coerce(row[1])) if row else None


def _cron_service_check(ctx: HealthContext, label: str, service: str, rule: dict,
                        peers: list[str] | None = None) -> CheckResult:
    """Freshness of a cron's last ok run, plus how its latest run ended.

    A run still "running" after cron_stuck_hours never finished: a deploy or
    restart stopped it (every push to master redeploys the crons), or it is
    hung. That alone is not a failure. For a service in a group (peers doing
    the same sweep), a peer that finished ok after it covers it, so it is OK
    while the unfinished run is younger than warn_hours and fewer than
    cron_interrupted_streak_warn runs in a row didn't finish. Otherwise it is
    graded by the age of its last ok run, and at least WARN.
    """
    t = ctx.table("ha_cron_run")
    is_svc = t.c.service_name == service
    last_ok, last_ok_start = (_coerce(v) for v in ctx.conn.execute(
        sa.select(sa.func.max(sa.func.coalesce(t.c.finished_at, t.c.started_at)),
                  sa.func.max(t.c.started_at))
        .where(is_svc, t.c.status == "ok")
    ).one())
    since = ctx.now - timedelta(hours=ctx.config["cron_recent_failures_hours"])
    recent_failed = ctx.scalar(
        sa.select(sa.func.count()).select_from(t)
        .where(is_svc, t.c.status == "failed", t.c.started_at >= since)
    ) or 0
    if last_ok is None:
        any_runs = ctx.scalar(sa.select(sa.func.count()).select_from(t).where(is_svc)) or 0
        if any_runs:
            return CheckResult(label, FAIL, f"{any_runs} run(s) recorded, none succeeded")
        if rule.get("optional"):
            return CheckResult(label, SKIPPED, "no runs recorded yet (service not created?)")
        return CheckResult(label, WARN, "no runs recorded in ha_cron_run (check service name / schedule)")
    res = _freshness(label, last_ok, ctx.now, rule["warn_hours"], rule.get("fail_hours"), rule["days"])
    res.detail = res.detail.replace("last update", "last ok run")
    if recent_failed:
        res.detail += f"; {recent_failed} failed run(s) in last {ctx.config['cron_recent_failures_hours']}h"
    recent_skipped = ctx.scalar(
        sa.select(sa.func.count()).select_from(t)
        .where(is_svc, t.c.status == "skipped", t.c.started_at >= since)
    ) or 0
    if recent_skipped:
        res.detail += (f"; {recent_skipped} run(s) skipped in last {ctx.config['cron_recent_failures_hours']}h "
                       f"(another PQ run in progress)")

    # Runs started after the last ok one, newest first.
    later = [(s, _coerce(st)) for s, st in ctx.conn.execute(
        sa.select(t.c.status, t.c.started_at).where(is_svc, t.c.started_at > last_ok_start)
        .order_by(t.c.started_at.desc())
    ).all()]
    stuck_after = timedelta(hours=ctx.config["cron_stuck_hours"])
    unfinished = [st for s, st in later if s == "running" and st is not None and ctx.now - st > stuck_after]
    failed_since_ok = any(s == "failed" for s, _ in later)
    latest_status, latest_start = later[0] if later else ("ok", last_ok_start)

    if latest_status == "failed":
        res.detail += "; latest run failed"
    elif unfinished and latest_start == unfinished[0]:
        age = (ctx.now - latest_start).total_seconds() / 3600
        res.detail += (f"; latest run (started {fmt_when(latest_start)}) has no finish time after "
                       f"{fmt_age(age)}: stopped by a deploy or restart, or still running")
    if unfinished and not failed_since_ok and peers:
        cover = _peer_ok_after(ctx, peers, unfinished[0])
        streak = ctx.config["cron_interrupted_streak_warn"]
        if len(unfinished) >= streak:
            res.detail += f"; {len(unfinished)} runs in a row never finished"
        # Railway skips a cron's next run while the last one is still running,
        # so a hung run is covered only while it is younger than warn_hours.
        recent = active_hours_since(unfinished[0], ctx.now, _DAYS[rule["days"]]) <= rule["warn_hours"]
        if cover and recent and len(unfinished) < streak:
            res.status = OK
            res.detail += f"; covered by {cover[0]} finishing ok at {fmt_when(cover[1])}"
        elif res.status == OK:
            res.status = WARN
    elif latest_status == "failed" or (unfinished and latest_start == unfinished[0]):
        if res.status == OK:
            res.status = WARN
    return res


def check_archive_crons(ctx: HealthContext) -> list[CheckResult]:
    if not ctx.has_table("ha_cron_run"):
        return [_skipped_table("Hansard archive cron", "ha_cron_run")]
    return [_cron_service_check(ctx, f"Archive cron: {svc}", svc, rule)
            for svc, rule in ctx.config["archive_crons"].items()]


def check_pq_tabled(ctx: HealthContext) -> CheckResult:
    cfg = ctx.config["pq"]
    return _gap_check(ctx, "Written questions: newest tabled", "ha_pq", "tabled_date",
                      "chamber", cfg["chambers"], cfg)


def check_pq_writes(ctx: HealthContext) -> CheckResult:
    name = "Written questions: last write"
    if not ctx.has_table("ha_pq"):
        return _skipped_table(name, "ha_pq")
    cfg = ctx.config["pq"]
    t = ctx.table("ha_pq")
    upd, ing = ctx.conn.execute(sa.select(sa.func.max(t.c.updated_at), sa.func.max(t.c.ingested_at))).one()
    latest = max((v for v in (_coerce(upd), _coerce(ing)) if v is not None), default=None)
    return _freshness(name, latest, ctx.now, cfg["write_warn_hours"], cfg["write_fail_hours"], "mon-fri")


def check_pq_crons(ctx: HealthContext) -> list[CheckResult]:
    if not ctx.has_table("ha_cron_run"):
        return [_skipped_table("PQ cron", "ha_cron_run")]
    cfg = ctx.config["pq_crons"]
    t = ctx.table("ha_cron_run")
    since = ctx.now - timedelta(days=cfg["discover_days"])
    seen = {r[0] for r in ctx.conn.execute(
        sa.select(t.c.service_name).where(t.c.service_name.like("pq-%"), t.c.started_at >= since).distinct()
    )}
    known = cfg["known"]
    results = []
    for svc, rule in known.items():
        peers = [s for s, r in known.items() if s != svc and rule.get("group") and r.get("group") == rule["group"]]
        results.append(_cron_service_check(ctx, f"PQ cron: {svc}", svc, rule, peers))
    for svc in sorted(seen - set(known) - set(cfg["ignore"])):
        results.append(_one_off_run(ctx, svc, listed=svc in cfg["one_off"]))
    return results


def _one_off_run(ctx: HealthContext, service: str, listed: bool) -> CheckResult:
    """How a one-off (hand-started) run's latest attempt went: report only when
    listed in "one_off", else WARN (a name the config doesn't know)."""
    t = ctx.table("ha_cron_run")
    is_svc = t.c.service_name == service
    status, started, finished = ctx.conn.execute(
        sa.select(t.c.status, t.c.started_at, t.c.finished_at).where(is_svc)
        .order_by(t.c.started_at.desc()).limit(1)
    ).one()
    last_ok = _coerce(ctx.scalar(sa.select(sa.func.max(sa.func.coalesce(t.c.finished_at, t.c.started_at)))
                                 .where(is_svc, t.c.status == "ok")))
    hours = ctx.config["cron_recent_failures_hours"]
    failed = ctx.scalar(sa.select(sa.func.count()).select_from(t).where(
        is_svc, t.c.status == "failed", t.c.started_at >= ctx.now - timedelta(hours=hours))) or 0
    started, finished = _coerce(started), _coerce(finished)
    detail = f"latest run {status}, started {fmt_when(started)}"
    if finished:
        detail += f", finished {fmt_when(finished)}"
    if failed:
        detail += f"; {failed} failed run(s) in last {hours}h"
    if listed:
        return CheckResult(f"PQ one-off: {service}", OK, detail + "; one-off run, report only", last_ok)
    return CheckResult(f"PQ cron: {service}", WARN,
                       detail + "; not in the health config (add it to CONFIG['pq_crons']['known'] "
                                "if it runs on a schedule, or to 'one_off')", last_ok)


def check_pq_headings(ctx: HealthContext) -> CheckResult:
    name = "Written questions: missing headings"
    if not ctx.has_table("ha_pq"):
        return _skipped_table(name, "ha_pq")
    cfg = ctx.config["pq"]
    t = ctx.table("ha_pq")
    cutoff = ctx.today - timedelta(days=cfg["heading_window_days"])
    # Questions tabled on the last few working days have no heading yet on
    # Parliament's side either, so they aren't counted.
    lag_from = _sitting_weekdays_back(ctx, cfg["heading_lag_working_days"])
    no_heading = sa.or_(t.c.heading.is_(None), t.c.heading == "")
    total, missing, too_new = ctx.conn.execute(sa.select(
        sa.func.sum(sa.case((t.c.tabled_date < lag_from, 1), else_=0)),
        sa.func.sum(sa.case((sa.and_(no_heading, t.c.tabled_date < lag_from), 1), else_=0)),
        sa.func.sum(sa.case((sa.and_(no_heading, t.c.tabled_date >= lag_from), 1), else_=0)),
    ).where(t.c.tabled_date >= cutoff)).one()
    total, missing, too_new = total or 0, missing or 0, too_new or 0
    pct = (missing / total * 100) if total else 0.0
    status = OK
    if missing >= cfg["missing_heading_min_count"] and pct >= cfg["missing_heading_warn_pct"]:
        status = WARN
    last_counted = lag_from - timedelta(days=1)
    while last_counted.weekday() >= 5:
        last_counted -= timedelta(days=1)
    detail = (f"{missing} of {total} tabled {cutoff.isoformat()} to {last_counted.isoformat()} "
              f"({pct:.1f}%), warn at >={cfg['missing_heading_warn_pct']}%; "
              f"{too_new} tabled since {lag_from.isoformat()} not counted (Parliament adds headings "
              f"about {cfg['heading_lag_working_days']} working days after tabling)")
    if status == WARN:
        detail += ("; check the PQ cron rows above, then fill with scripts/backfill_pq_headings.py "
                   "(docs/ops/railway-services.md)")
    return CheckResult(name, status, detail)


def check_pq_answers(ctx: HealthContext) -> CheckResult:
    name = "Written questions: recent answers"
    if not ctx.has_table("ha_pq"):
        return _skipped_table(name, "ha_pq")
    n = ctx.config["pq"]["answers_window_weekdays"]
    t = ctx.table("ha_pq")
    since = _sitting_weekdays_back(ctx, n)
    count = ctx.scalar(sa.select(sa.func.count()).select_from(t)
                       .where(t.c.is_answered.is_(True), t.c.answer_date >= since)) or 0
    latest = _coerce(ctx.scalar(sa.select(sa.func.max(t.c.answer_date)).where(t.c.is_answered.is_(True))))
    if count:
        return CheckResult(name, OK, f"{count} answered since {since.isoformat()}", latest)
    if ctx.sitting_days is not None and not any(
            since <= d < ctx.today for days in ctx.sitting_days.values() for d in days):
        return CheckResult(name, OK, f"no answers since {since.isoformat()}; neither House sat (recess)", latest)
    return CheckResult(name, WARN, f"no answers dated since {since.isoformat()} "
                                   f"(last {n} sitting weekdays); expected only in recess", latest)


def _pq_index_state(ctx: HealthContext, names: list[str]) -> dict[str, str]:
    """{index name: "ok" | "INVALID" | "not unique" | "missing"} for ha_pq."""
    dialect = ctx.conn.dialect.name
    if dialect == "postgresql":
        rows = ctx.conn.execute(sa.text(
            "SELECT c.relname, i.indisvalid, i.indisunique FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = 'ha_pq'::regclass AND c.relname = ANY(:names)"),
            {"names": names}).all()
        found = {r[0]: "INVALID" if not r[1] else "ok" if r[2] else "not unique" for r in rows}
    elif dialect == "sqlite":
        rows = ctx.conn.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'ha_pq'")).all()
        found = {r[0]: "ok" if "UNIQUE" in (r[1] or "").upper() else "not unique"
                 for r in rows if r[0] in names}
    else:
        found = {}
    return {n: found.get(n, "missing") for n in names}


def check_pq_integrity(ctx: HealthContext) -> CheckResult:
    """No question stored twice (INC-008), and the unique indexes that prevent it are in place."""
    name = "Written questions: duplicates + unique indexes"
    if not ctx.has_table("ha_pq"):
        return _skipped_table(name, "ha_pq")
    t = ctx.table("ha_pq")
    dup_api = ctx.scalar(sa.select(sa.func.count()).select_from(
        sa.select(t.c.api_id).where(t.c.api_id.isnot(None)).group_by(t.c.api_id)
        .having(sa.func.count() > 1).subquery())) or 0
    dup_date_uin = ctx.scalar(sa.select(sa.func.count()).select_from(
        sa.select(t.c.tabled_date).group_by(t.c.tabled_date, sa.func.lower(t.c.uin))
        .having(sa.func.count() > 1).subquery())) or 0
    indexes = _pq_index_state(ctx, ctx.config["pq_integrity"]["indexes"])
    status = OK
    if any(v != "ok" for v in indexes.values()):
        status = WARN
    if dup_api or dup_date_uin:
        status = FAIL
    idx = ", ".join(f"{k} {v}" for k, v in indexes.items())
    return CheckResult(name, status, f"duplicate groups: api_id {dup_api}, (tabled_date, uin) {dup_date_uin}; "
                                     f"indexes: {idx}")


def check_pq_headings_total(ctx: HealthContext) -> CheckResult:
    """Report only: every row without a heading. Older ones should fall as the heading backfill runs."""
    name = "Written questions: missing headings (all)"
    if not ctx.has_table("ha_pq"):
        return _skipped_table(name, "ha_pq")
    t = ctx.table("ha_pq")
    cutoff = ctx.today - timedelta(days=ctx.config["pq"]["heading_window_days"])
    no_heading = sa.or_(t.c.heading.is_(None), t.c.heading == "")
    total, missing, old = ctx.conn.execute(sa.select(
        sa.func.count(),
        sa.func.sum(sa.case((no_heading, 1), else_=0)),
        sa.func.sum(sa.case((sa.and_(no_heading, t.c.tabled_date < cutoff), 1), else_=0)),
    )).one()
    return CheckResult(name, OK, f"{missing or 0:,} of {total or 0:,} rows have no heading, "
                                 f"{old or 0:,} of them tabled before {cutoff.isoformat()} "
                                 f"(report only: should fall after the heading backfill)")


def _table_freshness(ctx: HealthContext, name: str, table: str, cfg_key: str,
                     column: str | None = None, count: bool = True) -> CheckResult:
    if not ctx.has_table(table):
        return _skipped_table(name, table)
    t = ctx.table(table)
    col = column or next((c for c in _REFRESH_COLUMNS if c in t.c), None)
    if col is None:
        return CheckResult(name, SKIPPED, f"{table} has no refresh timestamp column")
    cfg = ctx.config[cfg_key]
    if count:
        total, latest = ctx.conn.execute(sa.select(sa.func.count(), sa.func.max(t.c[col]))).one()
        extra = f"; {total:,} row{'s' if total != 1 else ''}"
        if not total:
            return CheckResult(name, WARN, f"{table} is empty")
    else:
        latest, extra = ctx.scalar(sa.select(sa.func.max(t.c[col]))), ""
    return _freshness(name, latest, ctx.now, cfg["warn_hours"], cfg.get("fail_hours"), extra=extra)


def check_bills(ctx: HealthContext) -> CheckResult:
    return _table_freshness(ctx, "Bills: last refresh", "ha_bill", "bills", "last_refreshed")


def check_edms(ctx: HealthContext) -> CheckResult:
    return _table_freshness(ctx, "EDMs: last refresh", "ha_edm", "edms", "last_refreshed")


def check_edm_cron(ctx: HealthContext) -> CheckResult:
    return _cron_record(ctx, "EDM cron", "edm_cron")


def check_member_profiles(ctx: HealthContext) -> CheckResult:
    name = "Members: profiles"
    if not ctx.has_table("member_profile"):
        return _skipped_table(name, "member_profile")
    cfg = ctx.config["member_profile"]
    t = ctx.table("member_profile")
    total, current, detailed, latest = ctx.conn.execute(sa.select(
        sa.func.count(),
        sa.func.sum(sa.case((t.c.is_current.is_(True), 1), else_=0)),
        sa.func.count(t.c.details_refreshed_at),
        sa.func.max(t.c.summary_refreshed_at),
    )).one()
    if not total:
        return CheckResult(name, WARN, "member_profile is empty (refresh_members.py not run yet?)")
    return _freshness(name, latest, ctx.now, cfg["warn_hours"], cfg["fail_hours"],
                      extra=f"; {total:,} profiles ({current or 0:,} current, {detailed:,} with details)")


def check_member_biographies(ctx: HealthContext) -> CheckResult:
    name = "Members: biographies"
    if not ctx.has_table("member_biography"):
        return _skipped_table(name, "member_biography")
    cfg = ctx.config["member_biography"]
    t = ctx.table("member_biography")
    total, with_text, errors, generated, attempted = ctx.conn.execute(sa.select(
        sa.func.count(), sa.func.count(t.c.bio_text), sa.func.count(t.c.last_error),
        sa.func.max(t.c.generated_at), sa.func.max(t.c.last_attempt_at),
    )).one()
    audit = None
    if ctx.has_table("member_name_audit"):
        audit = ctx.scalar(sa.select(sa.func.max(ctx.table("member_name_audit").c.run_at)))
    generated, audit = _coerce(generated), _coerce(audit)
    stamps = [v for v in (generated, _coerce(attempted), audit) if v is not None]
    if not total:
        return CheckResult(name, WARN, "member_biography is empty (bio job not run yet?)")
    extra = (f"; {with_text:,} bios in {total:,} rows, {errors:,} with last_error; "
             f"last generated {fmt_when(generated)}, last name audit {fmt_when(audit)}")
    return _freshness(name, max(stamps, default=None), ctx.now, cfg["warn_hours"], cfg["fail_hours"],
                      extra=extra)


def check_cached_members(ctx: HealthContext) -> CheckResult:
    name = "Members: cached_member refresh"
    if not ctx.has_table("cached_member"):
        return _skipped_table(name, "cached_member")
    cfg = ctx.config["cached_member"]
    t = ctx.table("cached_member")
    cutoff = ctx.now - timedelta(hours=cfg["warn_hours"])
    total, latest = ctx.conn.execute(sa.select(sa.func.count(), sa.func.max(t.c.cached_at))).one()
    fresh = ctx.scalar(sa.select(sa.func.count()).select_from(t).where(t.c.cached_at >= cutoff)) or 0
    res = _freshness(name, latest, ctx.now, cfg["warn_hours"], cfg["fail_hours"],
                     extra=f"; {fresh:,} of {total:,} refreshed in last {fmt_age(cfg['warn_hours'])}")
    if res.status == OK and fresh < cfg["min_fresh_rows"]:
        res.status = WARN
        res.detail += f" (expected >={cfg['min_fresh_rows']} from the weekly bulk refresh)"
    return res


def _tagged_share(ctx: HealthContext, name: str, item_table: str, theme_table: str, fk: str,
                  date_col: str, kind: str, extra_where) -> CheckResult:
    if not ctx.has_table(item_table) or not ctx.has_table(theme_table):
        return _skipped_table(name, theme_table if ctx.has_table(item_table) else item_table)
    cfg = ctx.config["tagging"]
    t, th = ctx.table(item_table), ctx.table(theme_table)
    start = ctx.today - timedelta(days=cfg["window_days"])
    tagged_expr = sa.exists(sa.select(th.c[fk]).where(th.c[fk] == t.c.id))
    total, tagged = ctx.conn.execute(sa.select(
        sa.func.count(),
        sa.func.sum(sa.case((tagged_expr, 1), else_=0)),
    ).where(t.c[date_col] >= start, t.c[date_col] < ctx.today, *extra_where(t))).one()
    total, tagged = total or 0, tagged or 0
    pct = (tagged / total * 100) if total else 100.0
    detail = (f"{tagged} of {total} from {start.isoformat()} to yesterday have a tag ({pct:.1f}%), "
              f"warn below {cfg['warn_pct']}%")
    if ctx.has_table("ha_tag_attempt"):
        a = ctx.table("ha_tag_attempt")
        given_up = ctx.scalar(sa.select(sa.func.count()).select_from(a).where(a.c.item_kind == kind))
        detail += f"; {given_up or 0} item(s) with failed tagging attempts recorded"
    status = OK
    if total >= cfg["min_items"] and pct < cfg["warn_pct"]:
        status = WARN
    elif total < cfg["min_items"]:
        detail += f" (fewer than {cfg['min_items']} items: not judged)"
    return CheckResult(name, status, detail)


def check_tagging_coverage(ctx: HealthContext) -> list[CheckResult]:
    """A silent tagging failure (API down, bad model, schema rejected) shows here."""
    return [
        _tagged_share(ctx, "Tagging: recent debates tagged", "ha_session", "ha_session_theme",
                      "session_id", "date", "session",
                      lambda t: (t.c.is_container.isnot(True),
                                 sa.or_(t.c.debate_type.is_(None), t.c.debate_type != "other"))),
        _tagged_share(ctx, "Tagging: recent written questions tagged", "ha_pq", "ha_pq_theme",
                      "pq_id", "tabled_date", "pq",
                      lambda t: (t.c.question_text.isnot(None),)),
    ]


def check_search_locale(ctx: HealthContext) -> CheckResult:
    """
    Archive search treats curly apostrophes as punctuation only when the
    database locale does: on en_US.utf8 (production) to_tsvector('english',
    'Starmer’s') gives 'starmer'; a restore into a "C"-locale cluster would
    silently stop most possessives matching.
    """
    name = "Search: curly apostrophes (database locale)"
    if ctx.conn.dialect.name != "postgresql":
        return CheckResult(name, SKIPPED, f"not Postgres ({ctx.conn.dialect.name}): full-text search not used")
    cfg = ctx.config["search_locale"]
    got = ctx.conn.execute(sa.text("SELECT to_tsvector('english', :w)::text"), {"w": cfg["sample"]}).scalar()
    ctype, provider = ctx.conn.execute(sa.text(
        "SELECT datctype, datlocprovider FROM pg_database WHERE datname = current_database()")).one()
    providers = {"c": "libc", "i": "icu", "b": "builtin"}
    detail = (f"to_tsvector('english', '{cfg['sample']}') = {got}; database LC_CTYPE {ctype}, "
              f"locale provider {providers.get(provider, provider)}")
    if f"'{cfg['expect']}'" not in (got or ""):
        return CheckResult(name, FAIL, f"{detail}; expected '{cfg['expect']}' - possessives won't match "
                                       "in archive search (database locale changed?)")
    return CheckResult(name, OK, detail)


def _volume_gb(default: float) -> tuple[float, str]:
    """Volume size from DB_VOLUME_GB (read at call time), and a note when the
    default is used."""
    raw = (os.environ.get("DB_VOLUME_GB") or "").strip()
    if not raw:
        return default, f"; DB_VOLUME_GB not set, assuming {default:g} GB"
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not 0 < value < float("inf"):    # also rejects nan
        return default, f"; DB_VOLUME_GB={raw!r} not understood, assuming {default:g} GB"
    return value, ""


def check_db_size(ctx: HealthContext) -> CheckResult:
    """Database size as a share of the Postgres volume: WARN at 70%, FAIL at
    85% (vacuum, index builds and restores need headroom). GB = 10^9 bytes, so
    the percentage errs high if Railway counts the volume in GiB."""
    name = "Database: disk space"
    if ctx.conn.dialect.name != "postgresql":
        return CheckResult(name, SKIPPED, f"not Postgres ({ctx.conn.dialect.name}): size not checked")
    cfg = ctx.config["db_size"]
    volume_gb, note = _volume_gb(cfg["default_volume_gb"])
    size_gb = ctx.conn.execute(sa.text("SELECT pg_database_size(current_database())")).scalar() / 1e9
    pct = 100 * size_gb / volume_gb
    # Rounded down, so the line never shows 70% or 85% on a level below it.
    detail = f"Database {size_gb:.1f} GB of {volume_gb:g} GB ({int(pct)}%){note}"
    if pct >= cfg["fail_pct"]:
        status = FAIL
    elif pct >= cfg["warn_pct"]:
        status = WARN
    else:
        return CheckResult(name, OK, detail)
    return CheckResult(name, status, f"{detail} - WARN from {cfg['warn_pct']:g}%, FAIL from "
                                     f"{cfg['fail_pct']:g}%: grow the Postgres volume in Railway")


def check_headline_stats(ctx: HealthContext) -> CheckResult:
    # last_success, not last_refreshed: stats_refresh bumps last_refreshed on failure too (SWR).
    res = _table_freshness(ctx, "Stats: headline refresh", "headline_stat", "headline_stats", "last_success")
    if not ctx.config["headline_stats"].get("scheduled", True) and res.status in PROBLEM_STATUSES:
        res.status = SKIPPED
        res.detail += " - stats-refresh not scheduled yet (intentional), info only"
    return res


def check_upcoming_releases(ctx: HealthContext) -> CheckResult:
    return _table_freshness(ctx, "Stats: upcoming releases", "upcoming_release", "upcoming_releases",
                            "last_refreshed")


def check_rediscovery(ctx: HealthContext) -> CheckResult:
    return _table_freshness(ctx, "Stats: producer rediscovery", "ha_stat_producer", "rediscovery",
                            "last_rediscovered_at", count=False)


def _cron_record(ctx: HealthContext, name: str, cfg_key: str) -> CheckResult:
    if not ctx.has_table("ha_cron_run"):
        return _skipped_table(name, "ha_cron_run")
    cfg = ctx.config[cfg_key]
    return _cron_service_check(ctx, name, cfg["service"],
                               {"days": "daily", "warn_hours": cfg["warn_hours"], "fail_hours": cfg["fail_hours"]})


def check_committee_evidence(ctx: HealthContext) -> CheckResult:
    return _cron_record(ctx, "Committee evidence cron", "committee_evidence")


def check_backup_record(ctx: HealthContext) -> CheckResult:
    return _cron_record(ctx, "Backups: cron record", "backup_record")


DB_CHECKS: list[Callable[[HealthContext], CheckResult | list[CheckResult]]] = [
    check_archive_sessions,
    check_archive_crons,
    check_pq_tabled,
    check_pq_writes,
    check_pq_crons,
    check_pq_headings,
    check_pq_answers,
    check_pq_integrity,
    check_pq_headings_total,
    check_tagging_coverage,
    check_search_locale,
    check_db_size,
    check_bills,
    check_edms,
    check_edm_cron,
    check_member_profiles,
    check_member_biographies,
    check_cached_members,
    check_headline_stats,
    check_upcoming_releases,
    check_rediscovery,
    check_committee_evidence,
    check_backup_record,
]


def run_db_checks(ctx: HealthContext) -> list[CheckResult]:
    """Run every DB check; a crashing check becomes a FAIL row, not a crash."""
    results: list[CheckResult] = []
    for fn in DB_CHECKS:
        try:
            out = fn(ctx)
            results.extend(out if isinstance(out, list) else [out])
        except Exception as exc:  # noqa: BLE001 — report and carry on
            try:
                ctx.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            label = fn.__name__.removeprefix("check_").replace("_", " ")
            results.append(CheckResult(label, FAIL, f"check error: {type(exc).__name__}: {str(exc)[:200]}"))
    return results


# ---------------------------------------------------------------------------
# Non-DB checks
# ---------------------------------------------------------------------------

R2_ENV_VARS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL")


def check_r2_backup(env: dict, now: datetime, config: dict | None = None,
                    s3_factory: Callable | None = None) -> CheckResult:
    """Newest daily/ object in R2. Reuses scripts/prune_old_backups.py helpers."""
    name = "Backups: newest R2 object"
    missing = [v for v in R2_ENV_VARS if not env.get(v)]
    if missing:
        return CheckResult(name, SKIPPED, f"R2 not configured ({', '.join(missing)} unset)")
    cfg = (config or CONFIG)["backup_r2"]
    try:
        import prune_old_backups as pob  # scripts/ must be on sys.path
        s3 = s3_factory() if s3_factory else pob.get_s3()
        bucket = env.get("R2_BUCKET_NAME", "westminsterbrief-backups")
        keys = pob.list_keys(s3, bucket, cfg["prefix"])
        dated = [(pob.parse_daily_date(k), k) for k in keys]
        dated = [(d, k) for d, k in dated if d is not None]
        if not dated:
            return CheckResult(name, FAIL, f"no objects under {cfg['prefix']}")
        _, newest_key = max(dated)
        modified = _coerce(s3.head_object(Bucket=bucket, Key=newest_key)["LastModified"])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, FAIL, f"R2 listing failed: {type(exc).__name__}: {str(exc)[:200]}")
    res = _freshness(name, modified, now, cfg["warn_hours"], cfg["fail_hours"],
                     extra=f"; {newest_key}, {len(dated)} daily object(s)")
    return res


def check_web(http_get: Callable, config: dict | None = None) -> list[CheckResult]:
    """GET /ping and /health on the live site; report status and latency."""
    import time

    cfg = (config or CONFIG)["web"]
    results = []
    for path in cfg["paths"]:
        name = f"Web: {path}"
        url = cfg["base_url"].rstrip("/") + path
        started = time.monotonic()
        try:
            resp = http_get(url, timeout=cfg["timeout_seconds"], allow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(name, FAIL, f"request failed: {type(exc).__name__}: {str(exc)[:150]}"))
            continue
        ms = (time.monotonic() - started) * 1000
        detail = f"HTTP {resp.status_code} in {ms:.0f} ms"
        if resp.status_code == 200:
            status = WARN if ms > cfg["slow_ms"] else OK
            if status == WARN:
                detail += f" (slow, warn >{cfg['slow_ms']} ms)"
        elif path == "/health" and resp.status_code == 503:
            status = WARN
            try:
                body = resp.json()
                bad = [f"{k}={v}" for k, v in body.items() if k not in ("status", "version") and v != "ok"]
                detail += " degraded: " + ", ".join(bad)[:300]
            except Exception:  # noqa: BLE001
                detail += " degraded"
        else:
            status = FAIL
        results.append(CheckResult(name, status, detail))
    return results


# ---------------------------------------------------------------------------
# Report + send policy
# ---------------------------------------------------------------------------

def count_problems(results: list[CheckResult]) -> int:
    return sum(1 for r in results if r.status in PROBLEM_STATUSES)


def subject_for(results: list[CheckResult]) -> str:
    n = count_problems(results)
    if n == 0:
        return "[Westminster Brief] Daily health: all OK"
    return f"[Westminster Brief] Daily health: {n} problem{'s' if n != 1 else ''}"


def should_send(results: list[CheckResult], now: datetime, always_send: bool = False,
                config: dict | None = None) -> tuple[bool, str]:
    """Problems -> send every day. All OK -> only the weekly summary (Monday)."""
    if count_problems(results):
        return True, "problems found"
    if always_send:
        return True, "--always-send"
    if now.weekday() == (config or CONFIG)["weekly_summary_weekday"]:
        return True, "weekly all-OK summary"
    return False, "all OK, no email on non-summary days"


def _sorted(results: list[CheckResult]) -> list[CheckResult]:
    return sorted(results, key=lambda r: _STATUS_ORDER.get(r.status, 9))


def render_text(results: list[CheckResult], now: datetime) -> str:
    lines = [
        f"Westminster Brief daily health - {now.strftime('%Y-%m-%d %H:%M')} UTC",
        f"{count_problems(results)} problem(s) across {len(results)} checks",
        "",
    ]
    width = max((len(r.name) for r in results), default=10)
    lines.append(f"{'STATUS':<8} {'CHECK':<{width}}  {'LAST SUCCESS':<20}  DETAIL")
    for r in _sorted(results):
        lines.append(f"{r.status:<8} {r.name:<{width}}  {fmt_when(r.last_success):<20}  {r.detail}")
    return "\n".join(lines)


_COLOURS = {OK: "#1b7f3b", WARN: "#a66300", FAIL: "#b00020", SKIPPED: "#666666"}


def render_html(results: list[CheckResult], now: datetime) -> str:
    esc = html.escape
    rows = "".join(
        "<tr>"
        f"<td style='padding:4px 8px;font-weight:bold;color:{_COLOURS.get(r.status, '#000')}'>{esc(r.status)}</td>"
        f"<td style='padding:4px 8px'>{esc(r.name)}</td>"
        f"<td style='padding:4px 8px;white-space:nowrap'>{esc(fmt_when(r.last_success))}</td>"
        f"<td style='padding:4px 8px'>{esc(r.detail)}</td>"
        "</tr>"
        for r in _sorted(results)
    )
    return (
        "<div style='font-family:Arial,sans-serif;font-size:14px'>"
        f"<p><strong>Westminster Brief daily health</strong> - {esc(now.strftime('%Y-%m-%d %H:%M'))} UTC<br>"
        f"{count_problems(results)} problem(s) across {len(results)} checks</p>"
        "<table style='border-collapse:collapse' border='1' cellspacing='0'>"
        "<tr><th style='padding:4px 8px'>Status</th><th style='padding:4px 8px'>Check</th>"
        "<th style='padding:4px 8px'>Last success</th><th style='padding:4px 8px'>Detail</th></tr>"
        f"{rows}</table></div>"
    )
