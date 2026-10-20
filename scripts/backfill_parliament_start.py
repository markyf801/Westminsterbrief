"""
Backfill the Hansard archive (debates, statements, Westminster Hall, contributions)
back to the start of the current Parliament.

The current Parliament first sat on Tuesday 9 July 2024 (Election of Speaker,
confirmed against the Hansard API). The archive's existing floor is 30 April 2025,
so the default range is 9 July 2024 -> 29 April 2025.

How it works
------------
- Walks the range NEWEST-FIRST, one calendar day at a time, Commons then Lords.
  Newest-first keeps the archive contiguous at every stopping point, so the
  live "Archive from ..." label (MIN(date) of ha_session) is always true.
- Each (day, house) is ingested with the existing ``ingest_date`` — the same
  code, classification and container rules as the hourly cron. This script
  changes none of that, so fixes to classification land here automatically.
- Idempotent: ``ingest_date`` skips sessions whose Hansard ext_id is already
  stored, and commits session-by-session. Re-running a day is always safe.
- Completeness check: a day only counts as done when every session the chain
  walk found is in the database and no session fetch failed. Session fetches
  are retried (the Hansard API times out regularly). A day that still fails is
  retried once, then the run stops with exit code 1 — the frontier never skips
  a day.
- Checkpoint: after each completed day the frontier (earliest fully-done day,
  per house) is saved in ``ha_cron_run`` (service_name below), so a Railway
  one-off can be re-run and carries on where it stopped. No local files needed.
- Tagging (on by default): each day's new sessions are tagged straight after
  ingest, and any untagged sessions left in the range are tagged first. The
  per-run cap ``--max-tag`` stops the run cleanly when reached, so a large
  untagged backlog never builds up for the hourly cron's unlimited tagger.
- Cron windows: by default the run will not start a day inside, or within
  15 minutes of, a production cron window (see _CRON_WINDOWS) and stops
  cleanly instead.
- Every loop is bounded: days by the date range and --max-days, fetch and day
  retries by fixed attempt counts, tagging by --max-tag and a consecutive
  failure limit. Repeated API failure exits non-zero.

Exit codes: 0 finished or stopped cleanly (cap / time / cron window) ·
1 a day failed after retries, or a database error · 2 API refused (401/403/429)
or GEMINI_API_KEY missing · 3 repeated tagging failures · 4 another backfill run
looks active.

Usage
-----
  python scripts/backfill_parliament_start.py --dry-run --max-days 3
  python scripts/backfill_parliament_start.py --max-days 7
  python scripts/backfill_parliament_start.py --max-days 31 --max-minutes 300
  python scripts/backfill_parliament_start.py --tag-only --max-tag 500
  python scripts/backfill_parliament_start.py --since 2024-07-09 --until 2025-04-29

Railway one-off: build from Dockerfile.cron, restart policy Never,
SKIP_MIGRATIONS=1, DATABASE_URL (Variable Reference), GEMINI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from sqlalchemy import func  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from extensions import db  # noqa: E402
from hansard_archive import ingestion  # noqa: E402
from hansard_archive.models import (  # noqa: E402
    HaCronRun,
    HansardContribution,
    HansardSession,
    HansardSessionTheme,
)
from hansard_archive.tagger import tag_session  # noqa: E402

PARLIAMENT_FIRST_SITTING = date(2024, 7, 9)
DEFAULT_UNTIL = date(2025, 4, 29)
SERVICE_NAME = "backfill-parliament-start"
HOUSES = ("Commons", "Lords")
TAG_SKIP_TYPES = ("other",)          # same default as tagger.tag_all_untagged

FETCH_ATTEMPTS = 3                   # per session JSON fetch
FETCH_BACKOFF = (5, 20)              # seconds before attempts 2 and 3
DAY_ATTEMPTS = 2                     # per (day, house)
DAY_RETRY_PAUSE = 60                 # seconds
DAY_PAUSE = 2                        # seconds between days (polite pacing)
TAG_PAUSE = 0.4                      # seconds between Gemini calls
MAX_CONSECUTIVE_TAG_FAILURES = 5
HEARTBEAT_STALE_MINUTES = 20
CRON_LOOKAHEAD_MINUTES = 15

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_TAG_FAILED = 3
EXIT_BUSY = 4

# Production cron windows, UTC (docs/ops/railway-services.md, CLAUDE.md).
# (weekdays Mon=0, start, end, label). Deliberately wide: weekday daytime is
# blocked whole because the archive cron runs hourly 11:00-23:00 Mon-Thu.
_ALL_DAYS = frozenset(range(7))
_CRON_WINDOWS = (
    (_ALL_DAYS, "02:55", "03:20", "backup-cron-r2 (03:00)"),
    (frozenset({6}), "01:55", "02:20", "member-cache-refresh (Sun 02:00)"),
    (_ALL_DAYS, "05:55", "07:20", "rediscovery (06:00) / bills (06:30)"),
    (frozenset({0, 1, 2, 3}), "07:55", "23:30",
     "archive 08:00 + hourly 11:00-23:00, PQ 08:30/09:30/14:00"),
    (frozenset({4}), "07:55", "19:30", "archive 08:00 + hourly Fri 09:00-19:00, PQ crons"),
)

_FETCH_FAIL_PREFIX = "[archive] failed to fetch session"
_HTTP_STATUS_RE = re.compile(r"\b([45]\d\d) (?:Client|Server) Error")
_REFUSED_STATUSES = {401, 403, 429}


class ApiRefused(Exception):
    """The API refused us (401/403/429) — stop at once, never retry (CLAUDE.md)."""


class DayIncomplete(Exception):
    """A (day, house) did not ingest completely; it is safe to retry."""


@dataclass
class DayResult:
    sitting: bool = False
    walked: int = 0
    new: int = 0
    existing: int = 0
    contributions: int = 0
    chars: int = 0
    not_found: int = 0


@dataclass
class RunStats:
    days: int = 0
    sitting_house_days: int = 0
    new_sessions: int = 0
    existing_sessions: int = 0
    contributions: int = 0
    chars: int = 0
    tagged: int = 0
    tag_no_text: int = 0
    stop_reason: str = ""
    frontier: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cron-window guard
# ---------------------------------------------------------------------------

def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def blocked_window(now: datetime, lookahead_minutes: int = CRON_LOOKAHEAD_MINUTES) -> str | None:
    """Label of the cron window `now` (UTC) is in, or will be within the lookahead."""
    for moment in (now, now + timedelta(minutes=lookahead_minutes)):
        for days, start, end, label in _CRON_WINDOWS:
            if moment.weekday() not in days:
                continue
            if _hhmm(start) <= (moment.hour, moment.minute) < _hhmm(end):
                return label
    return None


# ---------------------------------------------------------------------------
# Observation wrappers around the ingestion module (no edits to ingestion.py)
# ---------------------------------------------------------------------------

class _Observer:
    def __init__(self) -> None:
        self.walked: set[str] | None = None
        self.failures: list[tuple[str, str]] = []
        self.not_found: list[str] = []
        self.messages: list[str] = []


class _FetchFailureHandler(logging.Handler):
    def __init__(self, observer: _Observer) -> None:
        super().__init__(level=logging.WARNING)
        self.observer = observer

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith(_FETCH_FAIL_PREFIX):
            self.observer.messages.append(msg)


def _http_status(text: str) -> int | None:
    m = _HTTP_STATUS_RE.search(text or "")
    return int(m.group(1)) if m else None


@contextmanager
def observe_ingestion(sleep=time.sleep):
    """
    Wrap ingestion._fetch_session_full with retries and failure capture, and
    ingestion._collect_all_sessions_for_date to record which ext_ids a day's
    chain walk found. Both are restored on exit.

    _fetch_session_full swallows every error and returns ({}, [], []), which
    would silently drop sessions from a backfilled day. The cron self-heals by
    re-walking its window every hour; a backfill does not, so we retry here and
    record anything that still failed.
    """
    for name in ("_fetch_session_full", "_collect_all_sessions_for_date", "ingest_date",
                 "_get_seeds_for_date"):
        if not hasattr(ingestion, name):
            raise RuntimeError(f"hansard_archive.ingestion.{name} is missing — "
                               "the backfill wrapper needs updating")

    obs = _Observer()
    orig_fetch = ingestion._fetch_session_full
    orig_collect = ingestion._collect_all_sessions_for_date
    logger = logging.getLogger(ingestion.__name__)
    handler = _FetchFailureHandler(obs)
    old_level = logger.level
    if logger.getEffectiveLevel() > logging.WARNING:
        logger.setLevel(logging.WARNING)
    logger.addHandler(handler)

    def fetch_with_retry(ext_id):
        last_msg = ""
        for attempt in range(FETCH_ATTEMPTS):
            seen = len(obs.messages)
            result = orig_fetch(ext_id)
            if result[0]:
                return result
            if len(obs.messages) == seen:
                return result            # no error, just no Overview — nothing to retry
            last_msg = obs.messages[-1]
            status = _http_status(last_msg)
            if status in _REFUSED_STATUSES:
                raise ApiRefused(last_msg)
            if status == 404:
                obs.not_found.append(ext_id)
                return result
            if attempt < FETCH_ATTEMPTS - 1:
                sleep(FETCH_BACKOFF[min(attempt, len(FETCH_BACKOFF) - 1)])
        obs.failures.append((ext_id, last_msg))
        return {}, [], []

    def collect_and_record(seeds, target_date):
        results, dept_map = orig_collect(seeds, target_date)
        obs.walked = set(results.keys())
        return results, dept_map

    ingestion._fetch_session_full = fetch_with_retry
    ingestion._collect_all_sessions_for_date = collect_and_record
    try:
        yield obs
    finally:
        ingestion._fetch_session_full = orig_fetch
        ingestion._collect_all_sessions_for_date = orig_collect
        logger.removeHandler(handler)
        logger.setLevel(old_level)


# ---------------------------------------------------------------------------
# One (day, house)
# ---------------------------------------------------------------------------

def _raise_if_refused(exc: requests.RequestException) -> None:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) or _http_status(str(exc))
    if status in _REFUSED_STATUSES:
        raise ApiRefused(f"{status}: {exc}") from exc


def _existing_ext_ids(ext_ids: set[str]) -> set[str]:
    found: set[str] = set()
    ids = list(ext_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        found.update(r[0] for r in db.session.query(HansardSession.ext_id)
                     .filter(HansardSession.ext_id.in_(chunk)).all())
    return found


def _stored_contributions(day: date, house: str) -> tuple[int, int]:
    row = (db.session.query(func.count(HansardContribution.id),
                            func.coalesce(func.sum(func.length(HansardContribution.speech_text)), 0))
           .join(HansardSession, HansardSession.id == HansardContribution.session_id)
           .filter(HansardSession.date == day, HansardSession.house == house)
           .one())
    return int(row[0] or 0), int(row[1] or 0)


def _dry_run_day(day: date, house: str, sleep) -> DayResult:
    with observe_ingestion(sleep=sleep) as obs:
        try:
            seeds = ingestion._get_seeds_for_date(day, house)
        except requests.RequestException as exc:
            _raise_if_refused(exc)
            raise DayIncomplete(f"seed search failed: {exc}") from exc
        if not seeds:
            return DayResult()
        results, _ = ingestion._collect_all_sessions_for_date(seeds, day)
    if obs.failures:
        raise DayIncomplete(f"{len(obs.failures)} session fetch(es) failed: {obs.failures[0][1]}")
    existing = _existing_ext_ids(set(results))
    res = DayResult(sitting=bool(results), walked=len(results), existing=len(existing),
                    new=len(results) - len(existing), not_found=len(obs.not_found))
    for ext_id, (_overview, contribs) in results.items():
        if ext_id not in existing:
            res.contributions += len(contribs)
            res.chars += sum(len(c.get("speech_text") or "") for c in contribs)
    return res


def _ingest_day(day: date, house: str, sleep, verbose: bool) -> DayResult:
    with observe_ingestion(sleep=sleep) as obs:
        try:
            new = ingestion.ingest_date(day, house=house, verbose=verbose)
        except requests.RequestException as exc:
            _raise_if_refused(exc)
            raise DayIncomplete(f"seed search failed: {exc}") from exc
    if obs.walked is None:
        return DayResult()                       # no seeds: not a sitting day
    if obs.failures:
        raise DayIncomplete(f"{len(obs.failures)} session fetch(es) failed: {obs.failures[0][1]}")
    present = _existing_ext_ids(obs.walked)
    missing = obs.walked - present
    if missing:
        raise DayIncomplete(f"{len(missing)} walked session(s) not stored, e.g. {sorted(missing)[:3]}")
    contribs, chars = _stored_contributions(day, house)
    return DayResult(sitting=bool(obs.walked), walked=len(obs.walked), new=new,
                     existing=len(obs.walked) - new, contributions=contribs, chars=chars,
                     not_found=len(obs.not_found))


def process_day(day: date, house: str, dry_run: bool, sleep, verbose: bool) -> DayResult:
    """Ingest (or predict) one (day, house) with bounded retries."""
    last_error = ""
    for attempt in range(1, DAY_ATTEMPTS + 1):
        try:
            if dry_run:
                return _dry_run_day(day, house, sleep)
            return _ingest_day(day, house, sleep, verbose)
        except ApiRefused:
            raise
        except (DayIncomplete, requests.RequestException) as exc:
            last_error = str(exc)
        except OperationalError as exc:
            last_error = f"database: {exc}"
            _reset_db()
        print(f"[backfill]   {day} {house} attempt {attempt}/{DAY_ATTEMPTS} incomplete: "
              f"{last_error[:300]}", flush=True)
        if attempt < DAY_ATTEMPTS:
            sleep(DAY_RETRY_PAUSE)
    raise DayIncomplete(last_error)


def _reset_db() -> None:
    try:
        db.session.rollback()
    except Exception as exc:
        print(f"[backfill]   rollback failed ({exc}); disposing pool", flush=True)
    try:
        db.engine.dispose()
    except Exception as exc:
        print(f"[backfill]   engine dispose failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

def _untagged_ids(since: date, until: date, limit: int, house: str | None = None) -> list[int]:
    tagged = db.session.query(HansardSessionTheme.session_id)
    has_text = db.session.query(HansardContribution.session_id)
    q = (db.session.query(HansardSession.id)
         .filter(HansardSession.date >= since, HansardSession.date <= until,
                 HansardSession.is_container.is_(False),
                 ~HansardSession.debate_type.in_(TAG_SKIP_TYPES),
                 ~HansardSession.id.in_(tagged),
                 HansardSession.id.in_(has_text)))
    if house:
        q = q.filter(HansardSession.house == house)
    return [r[0] for r in q.order_by(HansardSession.date.desc(), HansardSession.id).limit(limit).all()]


class TagBudget:
    def __init__(self, cap: int, api_key: str, sleep) -> None:
        self.cap = cap
        self.used = 0
        self.api_key = api_key
        self.sleep = sleep
        self.consecutive_failures = 0

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    def tag(self, session_ids: list[int]) -> int:
        """Tag sessions until the budget runs out. Raises after repeated failures."""
        tagged = 0
        for sid in session_ids:
            if self.remaining <= 0:
                break
            self.sleep(TAG_PAUSE)
            self.used += 1
            try:
                count = tag_session(sid, api_key=self.api_key)
            except Exception as exc:
                _reset_db()
                print(f"[backfill]   tag error session {sid}: {exc}", flush=True)
                count = 0
            if count > 0:
                tagged += 1
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                print(f"[backfill]   tag FAIL session {sid}", flush=True)
                if self.consecutive_failures >= MAX_CONSECUTIVE_TAG_FAILURES:
                    raise RuntimeError(f"{self.consecutive_failures} tagging failures in a row "
                                       "— check GEMINI_API_KEY / quota")
        return tagged


# ---------------------------------------------------------------------------
# Checkpoint (ha_cron_run row, notes = JSON)
# ---------------------------------------------------------------------------

def _load_checkpoint() -> tuple[HaCronRun | None, dict]:
    rec = (HaCronRun.query.filter_by(service_name=SERVICE_NAME)
           .order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first())
    if not rec or not rec.notes:
        return rec, {}
    try:
        state = json.loads(rec.notes)
    except ValueError:
        return rec, {}
    return rec, state if isinstance(state, dict) else {}


def _save_checkpoint(run: HaCronRun, state: dict, stats: RunStats, status: str | None = None) -> None:
    state["heartbeat"] = datetime.utcnow().isoformat(timespec="seconds")
    for attempt in range(2):
        try:
            run.notes = json.dumps(state)
            run.days_window = stats.days
            run.sessions_ingested = stats.new_sessions
            run.sessions_tagged = stats.tagged
            if status:
                run.status = status
                run.finished_at = datetime.utcnow()
            db.session.commit()
            return
        except OperationalError:
            _reset_db()
            if attempt:
                raise
            run = db.session.merge(run)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill Hansard back to the start of the current Parliament")
    p.add_argument("--since", type=_parse_date, default=PARLIAMENT_FIRST_SITTING,
                   help=f"Earliest day, inclusive (default {PARLIAMENT_FIRST_SITTING})")
    p.add_argument("--until", type=_parse_date, default=DEFAULT_UNTIL,
                   help=f"Latest day, inclusive (default {DEFAULT_UNTIL})")
    p.add_argument("--house", choices=("Commons", "Lords", "both"), default="both")
    p.add_argument("--dry-run", action="store_true",
                   help="Walk the API and predict new vs already-stored sessions; write nothing")
    p.add_argument("--max-days", type=int, default=0,
                   help="Stop after this many calendar days (0 = no limit)")
    p.add_argument("--max-minutes", type=int, default=0,
                   help="Stop before starting a new day after this many minutes (0 = no limit)")
    p.add_argument("--no-tag", action="store_true", help="Ingest only; do not call Gemini")
    p.add_argument("--tag-only", action="store_true",
                   help="Only tag untagged sessions already stored in the range")
    p.add_argument("--max-tag", type=int, default=600,
                   help="Gemini tagging calls allowed this run (default 600)")
    p.add_argument("--no-resume", action="store_true",
                   help="Ignore the saved frontier and start at --until")
    p.add_argument("--ignore-cron-windows", action="store_true",
                   help="Run even inside production cron windows (local testing only)")
    p.add_argument("--verbose", action="store_true", help="Per-session ingest lines")
    return p


def _print_existing_coverage(since: date, until: date) -> None:
    rows = (db.session.query(HansardSession.date, HansardSession.house)
            .filter(HansardSession.date >= since, HansardSession.date <= until).all())
    floor = db.session.query(func.min(HansardSession.date)).scalar()
    print(f"[backfill] archive floor now: {floor}", flush=True)
    if not rows:
        print("[backfill] no sessions stored in the range yet", flush=True)
        return
    by_month: dict[tuple[str, str], int] = {}
    for d, h in rows:
        key = (d.strftime("%Y-%m"), h)
        by_month[key] = by_month.get(key, 0) + 1
    print("[backfill] already stored in range (month, house, sessions):", flush=True)
    for (m, h), n in sorted(by_month.items()):
        print(f"[backfill]   {m} {h:<7} {n}", flush=True)


def run(args: argparse.Namespace, sleep=time.sleep, now=datetime.utcnow) -> int:
    houses = HOUSES if args.house == "both" else (args.house,)
    if args.since > args.until:
        print("[backfill] --since is after --until", flush=True)
        return EXIT_FAILED
    if args.since < PARLIAMENT_FIRST_SITTING:
        print(f"[backfill] WARNING: --since {args.since} is before the current Parliament "
              f"({PARLIAMENT_FIRST_SITTING})", flush=True)

    tagging = not args.dry_run and not args.no_tag
    api_key = ""
    if tagging or (args.tag_only and not args.dry_run):
        from flask import current_app
        api_key = current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("[backfill] GEMINI_API_KEY not set — set it or pass --no-tag", flush=True)
            return EXIT_REFUSED
    if args.no_tag and not args.dry_run:
        print("[backfill] WARNING: --no-tag. The hourly archive cron tags EVERY untagged session "
              "with no cap, so it will pick these up on its next run.", flush=True)

    mode = "DRY RUN" if args.dry_run else ("TAG ONLY" if args.tag_only else "INGEST")
    print(f"[backfill] === START {mode} === {args.since} -> {args.until} newest-first, "
          f"houses={','.join(houses)} max_days={args.max_days or '-'} "
          f"max_minutes={args.max_minutes or '-'} tag={'off' if not tagging else args.max_tag}",
          flush=True)
    _print_existing_coverage(args.since, args.until)

    stats = RunStats()
    budget = TagBudget(args.max_tag, api_key, sleep)

    # --- tag-only mode ------------------------------------------------------
    if args.tag_only:
        if args.dry_run:
            left = len(_untagged_ids(args.since, args.until, 1_000_000))
            print(f"[backfill] === END TAG ONLY DRY RUN === untagged eligible={left} "
                  f"(a run would tag up to {min(left, args.max_tag)})", flush=True)
            return EXIT_OK
        try:
            ids = _untagged_ids(args.since, args.until, args.max_tag)
            print(f"[backfill] {len(ids)} untagged eligible session(s) selected", flush=True)
            stats.tagged = budget.tag(ids)
        except RuntimeError as exc:
            print(f"[backfill] STOP: {exc}", flush=True)
            return EXIT_TAG_FAILED
        left = len(_untagged_ids(args.since, args.until, 1_000_000))
        print(f"[backfill] === END TAG ONLY === tagged={stats.tagged} still_untagged={left}", flush=True)
        return EXIT_OK

    # --- resume -------------------------------------------------------------
    prev, state = _load_checkpoint()
    if (not args.dry_run and prev and prev.status == "running" and state.get("heartbeat")):
        try:
            beat = datetime.fromisoformat(state["heartbeat"])
        except ValueError:
            beat = None
        if beat and now() - beat < timedelta(minutes=HEARTBEAT_STALE_MINUTES):
            print(f"[backfill] another backfill run looks active (heartbeat {beat}) — not starting",
                  flush=True)
            return EXIT_BUSY

    frontier: dict[str, date | None] = {h: None for h in houses}
    if not args.no_resume and state.get("until") == args.until.isoformat():
        for h in houses:
            v = (state.get("frontier") or {}).get(h)
            frontier[h] = date.fromisoformat(v) if v else None
        print(f"[backfill] resuming: frontier {state.get('frontier')}", flush=True)
    elif state and not args.no_resume:
        print(f"[backfill] saved checkpoint is for until={state.get('until')}, not {args.until} — "
              "starting fresh", flush=True)

    def next_day(h: str) -> date:
        return (frontier[h] - timedelta(days=1)) if frontier[h] else args.until

    run_rec: HaCronRun | None = None
    state = {"since": args.since.isoformat(), "until": args.until.isoformat(),
             "frontier": {h: (frontier[h].isoformat() if frontier[h] else None) for h in houses}}
    if not args.dry_run:
        run_rec = HaCronRun(service_name=SERVICE_NAME, started_at=now(), days_window=0,
                            status="running")
        db.session.add(run_rec)
        db.session.commit()

    def finish(code: int, reason: str) -> int:
        stats.stop_reason = reason
        stats.frontier = {h: (frontier[h].isoformat() if frontier[h] else None) for h in houses}
        if run_rec is not None:
            state["frontier"] = stats.frontier
            state["stop_reason"] = reason
            try:
                _save_checkpoint(run_rec, state, stats, status="ok" if code == EXIT_OK else "failed")
            except Exception as exc:
                print(f"[backfill] could not save final checkpoint: {exc}", flush=True)
                code = code or EXIT_FAILED
        resume_at = max(next_day(h) for h in houses)
        print("[backfill] " + "=" * 60, flush=True)
        print(f"[backfill] === END {mode} === exit={code} reason: {reason}", flush=True)
        print(f"[backfill]   calendar days processed: {stats.days}", flush=True)
        print(f"[backfill]   sitting (day, house):    {stats.sitting_house_days}", flush=True)
        label = "would insert:" if args.dry_run else "new sessions:"
        print(f"[backfill]   {label:<25}{stats.new_sessions}", flush=True)
        print(f"[backfill]   already stored:          {stats.existing_sessions}", flush=True)
        print(f"[backfill]   contributions:           {stats.contributions}", flush=True)
        print(f"[backfill]   speech text (MB):        {stats.chars / 1_000_000:.1f}", flush=True)
        print(f"[backfill]   tagged:                  {stats.tagged}", flush=True)
        print(f"[backfill]   frontier:                {stats.frontier}", flush=True)
        if resume_at >= args.since:
            print(f"[backfill]   next run starts at:      {resume_at}", flush=True)
        else:
            print("[backfill]   range complete", flush=True)
        return code

    started = time.monotonic()

    # --- catch-up tagging for days already done --------------------------------
    if tagging:
        try:
            leftover = _untagged_ids(args.since, args.until, budget.remaining)
            if leftover:
                print(f"[backfill] tagging {len(leftover)} untagged session(s) left from earlier runs",
                      flush=True)
                stats.tagged += budget.tag(leftover)
        except RuntimeError as exc:
            return finish(EXIT_TAG_FAILED, str(exc))

    day = max(next_day(h) for h in houses)
    while day >= args.since:
        if args.max_days and stats.days >= args.max_days:
            return finish(EXIT_OK, f"--max-days {args.max_days} reached")
        if args.max_minutes and (time.monotonic() - started) / 60 >= args.max_minutes:
            return finish(EXIT_OK, f"--max-minutes {args.max_minutes} reached")
        if not args.ignore_cron_windows:
            window = blocked_window(now())
            if window:
                return finish(EXIT_OK, f"cron window: {window} — re-run later")
        if tagging and budget.remaining <= 0:
            return finish(EXIT_OK, f"--max-tag {args.max_tag} reached")

        for h in houses:
            if day > next_day(h):
                continue                        # this house already has this day
            day_t = time.monotonic()
            try:
                res = process_day(day, h, args.dry_run, sleep, args.verbose)
            except ApiRefused as exc:
                return finish(EXIT_REFUSED, f"API refused on {day} {h}: {exc}")
            except DayIncomplete as exc:
                return finish(EXIT_FAILED, f"{day} {h} failed after {DAY_ATTEMPTS} attempts: {exc}")
            except OperationalError as exc:
                _reset_db()
                return finish(EXIT_FAILED, f"database error on {day} {h}: {exc}")

            tagged_now = 0
            if tagging and res.sitting:
                try:
                    tagged_now = budget.tag(_untagged_ids(day, day, budget.remaining, house=h))
                except RuntimeError as exc:
                    return finish(EXIT_TAG_FAILED, str(exc))
                stats.tagged += tagged_now

            if res.sitting:
                stats.sitting_house_days += 1
                stats.new_sessions += res.new
                stats.existing_sessions += res.existing
                stats.contributions += res.contributions
                stats.chars += res.chars
                print(f"[backfill] {day} {h:<7} sessions={res.walked} new={res.new} "
                      f"existing={res.existing} contributions={res.contributions} "
                      f"tagged={tagged_now} 404s={res.not_found} "
                      f"({time.monotonic() - day_t:.0f}s)", flush=True)
            if not args.dry_run:
                frontier[h] = day

        stats.days += 1
        if run_rec is not None:
            state["frontier"] = {h: (frontier[h].isoformat() if frontier[h] else None) for h in houses}
            try:
                _save_checkpoint(run_rec, state, stats)
            except OperationalError as exc:
                return finish(EXIT_FAILED, f"could not save checkpoint after {day}: {exc}")
        day -= timedelta(days=1)
        sleep(DAY_PAUSE)

    return finish(EXIT_OK, "range complete")


def main(argv: list[str] | None = None, app=None) -> int:
    args = build_parser().parse_args(argv)
    if app is None:
        from flask_app import app
    with app.app_context():
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
