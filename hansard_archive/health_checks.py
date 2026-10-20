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

Sitting-day rule (no Parliament calendar in the repo)
-----------------------------------------------------
"Gap" = the number of weekdays (Mon-Fri) strictly between the newest row's
date and today, ignoring any day inside ``RECESS_PERIODS``. A gap above the
threshold (default 4) is a WARN. Example: last session Thursday, report runs
the following Friday -> Fri, Mon, Tue, Wed, Thu = 5 -> WARN. Today is never
counted (the report runs before the day's sittings). Without recess dates the
check will WARN during long recesses; add the dates to ``RECESS_PERIODS`` from
the parliament.uk recess-dates page to silence that.

Cron "active hours"
-------------------
Cron ages are measured only over the days the job is scheduled to run, so a
Mon-Fri job checked on Monday morning isn't flagged for the weekend. E.g. the
last Mon-Fri run on Friday 14:00, checked Monday 07:30, is 10h + 7.5h = 17.5
active hours old.
"""

from __future__ import annotations

import html
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

# Inclusive (start, end) ISO dates when neither House sits. Owner-maintained:
# copy from https://www.parliament.uk/about/faqs/house-of-commons-faqs/business-faq-page/recess-dates/
# Days in these ranges don't count towards "weekdays without new data".
RECESS_PERIODS: list[tuple[str, str]] = []

_DAYS = {
    "daily": {0, 1, 2, 3, 4, 5, 6},
    "mon-fri": {0, 1, 2, 3, 4},
    "mon-thu": {0, 1, 2, 3},
    "mon": {0},
    "fri": {4},
    "sun": {6},
}

# All thresholds in one place. Hours are "active hours" for the job's days
# (see module docstring); "daily" means plain clock hours. A fail value of None
# means the check can WARN but never FAIL.
CONFIG: dict = {
    "archive": {
        "max_weekday_gap": 4,
        "houses": ["Commons", "Lords"],
    },
    "archive_crons": {
        # ha_cron_run.service_name -> schedule (see docs/ops/railway-services.md)
        "morning-catchup": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50},
        "daytime-mth": {"days": "mon-thu", "warn_hours": 26, "fail_hours": 50},
        "daytime-fri": {"days": "fri", "warn_hours": 26, "fail_hours": None},
    },
    "pq": {
        "max_weekday_gap": 4,             # newest tabled_date per chamber
        "chambers": ["Commons", "Lords"],
        "write_warn_hours": 26,           # newest updated_at/ingested_at, Mon-Fri hours
        "write_fail_hours": 50,
        "heading_window_days": 14,
        "missing_heading_warn_pct": 5.0,
        "missing_heading_min_count": 10,
        "answers_window_weekdays": 3,     # answered in the last N sitting weekdays
    },
    "pq_crons": {
        "known": {
            "pq-morning": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50},
            "pq-afternoon": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50},
            "pq-monday": {"days": "mon", "warn_hours": 26, "fail_hours": 50},
        },
        # Any other "pq-*" service seen in the last N days is checked as Mon-Fri.
        "discover_days": 30,
        "discovered_default": {"days": "mon-fri", "warn_hours": 26, "fail_hours": 50},
        "ignore": ["backfill", "manual"],
    },
    "cron_recent_failures_hours": 24,     # failed runs in this window are listed in the detail
    "cron_stuck_hours": 3,                # latest run still "running" after this -> WARN
    "bills": {"warn_hours": 48, "fail_hours": 96},
    "edms": {"warn_hours": 48, "fail_hours": 96},
    "member_profile": {"warn_hours": 8 * 24, "fail_hours": None},
    "member_biography": {"warn_hours": 35 * 24, "fail_hours": None},
    "cached_member": {"warn_hours": 8 * 24, "fail_hours": 15 * 24, "min_fresh_rows": 400},
    "headline_stats": {"warn_hours": 8 * 24, "fail_hours": None},
    "upcoming_releases": {"warn_hours": 72, "fail_hours": None},
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
                 recess_periods: list[tuple[str, str]] | None = None):
        self.conn = conn
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
               group_col: str, groups: list[str], max_gap: int) -> CheckResult:
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
        gap = weekday_gap(ctx, latest)
        newest = latest if newest is None or latest > newest else newest
        flag = ""
        if gap > max_gap:
            worst = min(worst, WARN, key=_STATUS_ORDER.get)
            flag = f" ({gap} weekdays without new data)"
        parts.append(f"{g}: {latest.isoformat()}{flag}")
    detail = "; ".join(parts)
    if worst != OK:
        detail += f" - limit {max_gap} weekdays; expected during recess (see RECESS_PERIODS)"
    return CheckResult(name, worst, detail, newest)


def check_archive_sessions(ctx: HealthContext) -> CheckResult:
    cfg = ctx.config["archive"]
    return _gap_check(ctx, "Hansard archive: newest session", "ha_session", "date",
                      "house", cfg["houses"], cfg["max_weekday_gap"])


def _cron_service_check(ctx: HealthContext, label: str, service: str, rule: dict) -> CheckResult:
    t = ctx.table("ha_cron_run")
    last_ok = _coerce(ctx.scalar(
        sa.select(sa.func.max(sa.func.coalesce(t.c.finished_at, t.c.started_at)))
        .where(t.c.service_name == service, t.c.status == "ok")
    ))
    since = ctx.now - timedelta(hours=ctx.config["cron_recent_failures_hours"])
    recent_failed = ctx.scalar(
        sa.select(sa.func.count()).select_from(t)
        .where(t.c.service_name == service, t.c.status == "failed", t.c.started_at >= since)
    ) or 0
    if last_ok is None:
        any_runs = ctx.scalar(sa.select(sa.func.count()).select_from(t).where(t.c.service_name == service)) or 0
        if any_runs:
            return CheckResult(label, FAIL, f"{any_runs} run(s) recorded, none succeeded")
        return CheckResult(label, WARN, "no runs recorded in ha_cron_run (check service name / schedule)")
    res = _freshness(label, last_ok, ctx.now, rule["warn_hours"], rule.get("fail_hours"), rule["days"])
    res.detail = res.detail.replace("last update", "last ok run")
    if recent_failed:
        res.detail += f"; {recent_failed} failed run(s) in last {ctx.config['cron_recent_failures_hours']}h"
    latest_status, latest_start = ctx.conn.execute(
        sa.select(t.c.status, t.c.started_at).where(t.c.service_name == service)
        .order_by(t.c.started_at.desc()).limit(1)
    ).one()
    latest_start = _coerce(latest_start)
    stuck = (latest_status == "running" and latest_start is not None
             and ctx.now - latest_start > timedelta(hours=ctx.config["cron_stuck_hours"]))
    if latest_status == "failed" or stuck:
        res.detail += "; latest run failed" if latest_status == "failed" else "; latest run never finished"
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
                      "chamber", cfg["chambers"], cfg["max_weekday_gap"])


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
    services = dict(cfg["known"])
    for svc in sorted(seen):
        if svc not in services and svc not in cfg["ignore"]:
            services[svc] = cfg["discovered_default"]
    return [_cron_service_check(ctx, f"PQ cron: {svc}", svc, rule) for svc, rule in services.items()]


def check_pq_headings(ctx: HealthContext) -> CheckResult:
    name = "Written questions: missing headings"
    if not ctx.has_table("ha_pq"):
        return _skipped_table(name, "ha_pq")
    cfg = ctx.config["pq"]
    t = ctx.table("ha_pq")
    cutoff = ctx.today - timedelta(days=cfg["heading_window_days"])
    total, missing = ctx.conn.execute(sa.select(
        sa.func.count(),
        sa.func.sum(sa.case((sa.or_(t.c.heading.is_(None), t.c.heading == ""), 1), else_=0)),
    ).where(t.c.tabled_date >= cutoff)).one()
    total, missing = total or 0, missing or 0
    pct = (missing / total * 100) if total else 0.0
    status = OK
    if missing >= cfg["missing_heading_min_count"] and pct >= cfg["missing_heading_warn_pct"]:
        status = WARN
    return CheckResult(name, status,
                       f"{missing} of {total} tabled in last {cfg['heading_window_days']} days "
                       f"({pct:.1f}%), warn at >={cfg['missing_heading_warn_pct']}%")


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
    return CheckResult(name, WARN, f"no answers dated since {since.isoformat()} "
                                   f"(last {n} sitting weekdays); expected only in recess", latest)


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


def check_member_profiles(ctx: HealthContext) -> CheckResult:
    return _table_freshness(ctx, "Members: profiles", "member_profile", "member_profile")


def check_member_biographies(ctx: HealthContext) -> CheckResult:
    return _table_freshness(ctx, "Members: biographies", "member_biography", "member_biography")


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


def check_headline_stats(ctx: HealthContext) -> CheckResult:
    # last_success, not last_refreshed: stats_refresh bumps last_refreshed on failure too (SWR).
    return _table_freshness(ctx, "Stats: headline refresh", "headline_stat", "headline_stats", "last_success")


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
    check_bills,
    check_edms,
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
