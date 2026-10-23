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
- Checkpoint: after each completed (day, house) the frontier (earliest fully-done day,
  per house) is saved in ``ha_cron_run`` (service_name below), so a Railway
  one-off can be re-run and carries on where it stopped. No local files needed.
- Tagging (on by default): each day's new sessions are tagged straight after
  ingest, and any untagged sessions left in the range are tagged first. The
  per-run cap ``--max-tag`` (default 600) stops the whole run cleanly when
  reached, so raise it for the full backfill (e.g. --max-tag 5000). Tagging
  here keeps a large untagged backlog away from the hourly archive cron, which
  tags at most 200 sessions a run (env ARCHIVE_TAG_CAP), newest first.
  A session that cannot be tagged is not retried within a run, and after
  TAG_MAX_ATTEMPTS counted failures (kept in the checkpoint) it is skipped for
  good, so it never blocks the backfill. Failures only stop the run when a
  Gemini health check confirms the API itself is failing (see TagBudget).
- Cron windows: by default the run will not start a (day, house) or a tagging
  call inside, or within 20 minutes of, a production cron window (the shared
  schedule in hansard_archive/cron_windows.py) and stops cleanly instead. In
  practice it runs on weekday evenings/nights and at weekends.
- Every loop is bounded: days by the date range and --max-days, fetch and day
  retries by fixed attempt counts, tagging by --max-tag and a consecutive
  failure limit. Repeated API failure exits non-zero.

Exit codes: 0 finished or stopped cleanly (cap / time / cron window) ·
1 a day failed after retries, or a database error · 2 API refused (401/403/429)
or GEMINI_API_KEY missing · 3 tagging stopped (Gemini down, or too many
untaggable sessions) · 4 another backfill run looks active.

Usage
-----
  python scripts/backfill_parliament_start.py --dry-run --max-days 3
  python scripts/backfill_parliament_start.py --max-days 7
  python scripts/backfill_parliament_start.py --max-days 31 --max-minutes 300
  python scripts/backfill_parliament_start.py --tag-only --max-tag 500
  python scripts/backfill_parliament_start.py --since 2024-07-09 --until 2025-04-29

Railway one-off: build from Dockerfile.cron, restart policy Never,
SKIP_MIGRATIONS=1, DATABASE_URL (Variable Reference), GEMINI_API_KEY.
Run order, checks and estimates: docs/ops/backfill-parliament-start.md.
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
# Shared production cron schedule; re-exported for backfill_pq_parliament_start.py.
from hansard_archive.cron_windows import LOOKAHEAD_MINUTES, blocked_window  # noqa: E402,F401
from hansard_archive.models import (  # noqa: E402
    HaCronRun,
    HansardContribution,
    HansardSession,
    HansardSessionTheme,
)
from hansard_archive import tagger  # noqa: E402
from hansard_archive.tagger import tag_session  # noqa: E402

PARLIAMENT_FIRST_SITTING = date(2024, 7, 9)
DEFAULT_UNTIL = date(2025, 4, 29)
SERVICE_NAME = "backfill-parliament-start"
PQ_SERVICE_NAME = "backfill-pq-parliament-start"   # scripts/backfill_pq_parliament_start.py
HOUSES = ("Commons", "Lords")
TAG_SKIP_TYPES = ("other",)          # same default as tagger.tag_all_untagged

FETCH_ATTEMPTS = 3                   # per session JSON fetch
FETCH_BACKOFF = (5, 20)              # seconds before attempts 2 and 3
DAY_ATTEMPTS = 2                     # per (day, house)
DAY_RETRY_PAUSE = 60                 # seconds
DAY_PAUSE = 2                        # seconds between days (polite pacing)
TAG_PAUSE = 0.4                      # seconds between Gemini calls
MAX_CONSECUTIVE_TAG_FAILURES = 5
TAG_MAX_ATTEMPTS = 2                 # counted failures before the backfill skips an item
MAX_SKIPPED_ITEMS = 200
HEARTBEAT_STALE_MINUTES = 20
HEARTBEAT_EVERY_SECONDS = 300
CRON_LOOKAHEAD_MINUTES = LOOKAHEAD_MINUTES

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_TAG_FAILED = 3
EXIT_BUSY = 4

_FETCH_FAIL_PREFIX = "[archive] failed to fetch session"
_HTTP_STATUS_RE = re.compile(r"\b([45]\d\d) (?:Client|Server) Error")
_REFUSED_STATUSES = {401, 403, 429}


class ApiRefused(Exception):
    """The API refused us (401/403/429) — stop at once, never retry (CLAUDE.md)."""


class DayIncomplete(Exception):
    """A (day, house) did not ingest completely; it is safe to retry."""


class WindowStop(Exception):
    """A production cron window is due — stop cleanly before doing more work."""


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

def _untagged_ids(since: date, until: date, limit: int, house: str | None = None,
                  exclude=()) -> list[int]:
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
    if exclude:
        q = q.filter(~HansardSession.id.in_(list(exclude)))
    return [r[0] for r in q.order_by(HansardSession.date.desc(), HansardSession.id).limit(limit).all()]


# What this script needs from hansard_archive.tagger. Checked before a run
# starts, so a renamed helper fails loudly instead of looking like an outage.
_TAGGER_API = ("_gemini_json", "_RESPONSE_SCHEMA", "resolve_model", "GeminiCallFailed",
               "_given_up_ids", "tag_session", "tag_pq")

_PROBE_PROMPT = (
    "Tag this UK parliamentary debate. Session title: School funding. House: Commons.\n"
    "Transcript: The Minister answered questions on school funding and the recruitment "
    "of teachers in rural areas."
)


def check_tagger_api() -> str | None:
    """None if the tagger has everything this script calls, else what is missing."""
    missing = [name for name in _TAGGER_API if not hasattr(tagger, name)]
    return ", ".join(missing) if missing else None


def gemini_healthy(api_key: str) -> bool:
    """
    One small Gemini call through the tagger's own path (_gemini_json with the
    production response schema). Tells 'the API is failing' apart from 'these
    particular items cannot be tagged'. Only an API failure counts as down —
    a missing helper is a bug and raises (check_tagger_api runs first).
    """
    try:
        model = tagger.resolve_model(api_key)
        tagger._gemini_json(api_key, model, _PROBE_PROMPT, tagger._RESPONSE_SCHEMA)
        return True
    except tagger.GeminiCallFailed as exc:
        print(f"[backfill]   Gemini health check failed: {exc}", flush=True)
        return False


def _given_up(kind: str) -> set[int]:
    """Items the crons' tagger has stopped trying (ha_tag_attempt), if that table exists."""
    q = tagger._given_up_ids(kind)
    return {r[0] for r in q.all()} if q is not None else set()


class TaggingStopped(RuntimeError):
    """Tagging cannot continue (Gemini failing / rejecting requests, or too many bad items)."""


class TagBudget:
    """
    Tags items one at a time with a per-run cap, and never lets one item that
    cannot be tagged block the backfill.

    The tagger returns 0 when Gemini answered but nothing was writable (it also
    counts that in ha_tag_attempt), and raises GeminiCallFailed when the call
    itself failed:

    - 0 → the item's fault: counted against it now (``failures``, kept in the
      checkpoint) and not offered again this run; after TAG_MAX_ATTEMPTS the
      backfill skips it for good. Items the crons gave up on (ha_tag_attempt)
      are skipped too.
    - GeminiCallFailed(final=True) (bad request / key / model) → stop the run at
      once; not held against the item.
    - other GeminiCallFailed / unexpected errors → the item is not retried this
      run. After MAX_CONSECUTIVE_TAG_FAILURES in a row a health check decides:
      Gemini working → the items were the problem (counted, carry on); Gemini
      failing → stop the run, nothing counted.
    - more than MAX_SKIPPED_ITEMS permanently skipped items stops the run so
      someone looks at them.
    """

    def __init__(self, cap: int, api_key: str, sleep, window_check=lambda: None,
                 tag_fn=None, probe=None, failures: dict | None = None, label="session",
                 kind="session") -> None:
        self.cap = cap
        self.used = 0
        self.api_key = api_key
        self.sleep = sleep
        self.window_check = window_check
        self.tag_fn = tag_fn or (lambda item_id: tag_session(item_id, api_key=self.api_key))
        self.probe = probe or (lambda: gemini_healthy(self.api_key))
        self.failures: dict[str, int] = dict(failures or {})
        self.label = label
        self.kind = kind
        self.failed_this_run: set[int] = set()
        self.streak: list[int] = []
        self.succeeded = 0
        self._given_up: set[int] | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    @property
    def skipped(self) -> set[int]:
        return {int(k) for k, n in self.failures.items() if n >= TAG_MAX_ATTEMPTS}

    @property
    def given_up(self) -> set[int]:
        if self._given_up is None:
            self._given_up = _given_up(self.kind) if self.api_key else set()
        return self._given_up

    @property
    def excluded(self) -> set[int]:
        return self.skipped | self.given_up | self.failed_this_run

    def _count(self, item_ids) -> None:
        for item_id in item_ids:
            key = str(item_id)
            self.failures[key] = self.failures.get(key, 0) + 1
        if len(self.skipped) > MAX_SKIPPED_ITEMS:
            raise TaggingStopped(f"{len(self.skipped)} {self.label}s cannot be tagged "
                                 f"(limit {MAX_SKIPPED_ITEMS}) — look at them before continuing")

    def settle(self) -> None:
        """End of run: decide whether trailing API-type failures were the items."""
        if self.streak and self.probe():
            try:
                self._count(self.streak)
            except TaggingStopped as exc:
                print(f"[backfill]   {exc}", flush=True)
        self.streak = []

    def tag(self, ids: list[int]) -> int:
        """Tag items until the budget runs out. Raises TaggingStopped, or
        WindowStop when a cron window is due."""
        tagged = 0
        for item_id in ids:
            if self.remaining <= 0:
                break
            if item_id in self.excluded:
                continue
            window = self.window_check()
            if window:
                raise WindowStop(window)
            self.sleep(TAG_PAUSE)
            self.used += 1
            try:
                count = self.tag_fn(item_id)
            except tagger.GeminiCallFailed as exc:
                if exc.final:
                    raise TaggingStopped(f"Gemini rejected the request ({exc}) — check "
                                         "GEMINI_API_KEY / TAGGER_MODEL; nothing counted") from exc
                print(f"[backfill]   tag API error {self.label} {item_id}: {exc}", flush=True)
                count = None
            except Exception as exc:
                _reset_db()
                print(f"[backfill]   tag error {self.label} {item_id}: {exc}", flush=True)
                count = None
            if count:
                tagged += 1
                self.succeeded += 1
                self._count(self.streak)        # Gemini works: the earlier errors were the items
                self.streak = []
                continue
            self.failed_this_run.add(item_id)
            if count == 0:                      # Gemini answered; nothing writable
                print(f"[backfill]   tag: nothing writable for {self.label} {item_id}", flush=True)
                self._count([item_id])
                continue
            self.streak.append(item_id)
            if len(self.streak) >= MAX_CONSECUTIVE_TAG_FAILURES:
                if not self.probe():
                    raise TaggingStopped(f"{len(self.streak)} tagging API errors in a row and the "
                                         "Gemini health check failed — check GEMINI_API_KEY / quota")
                self._count(self.streak)
                self.streak = []
        return tagged


# ---------------------------------------------------------------------------
# Checkpoint (ha_cron_run row, notes = JSON)
# ---------------------------------------------------------------------------

def _has_frontier(state: dict) -> bool:
    f = state.get("frontier")
    return any(f.values()) if isinstance(f, dict) else bool(f)


def load_state(service_name: str = SERVICE_NAME) -> tuple[HaCronRun | None, dict, dict]:
    """
    (latest run record, state to resume from, latest saved state).

    The resume state is the newest record that saved a frontier — a run that
    died before its first save (notes NULL) or never finished a day must not
    send the next run back to --until. The latest saved state carries the
    tagging-failure counts.
    """
    recs = (HaCronRun.query.filter_by(service_name=service_name)
            .order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).limit(200).all())
    latest = recs[0] if recs else None
    resume: dict = {}
    newest: dict = {}
    for rec in recs:
        try:
            state = json.loads(rec.notes) if rec.notes else None
        except ValueError:
            state = None
        if not isinstance(state, dict):
            continue
        if not newest:
            newest = state
        if _has_frontier(state):
            resume = state
            break
    return latest, resume, newest


def run_active(service_name: str, now: datetime) -> datetime | None:
    """Heartbeat of the latest run of `service_name` if it is still running and fresh."""
    rec = (HaCronRun.query.filter_by(service_name=service_name)
           .order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first())
    if not rec or rec.status != "running" or not rec.notes:
        return None
    try:
        beat = datetime.fromisoformat(json.loads(rec.notes).get("heartbeat", ""))
    except (ValueError, TypeError, AttributeError):
        return None
    return beat if now - beat < timedelta(minutes=HEARTBEAT_STALE_MINUTES) else None


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
    if api_key:
        missing = check_tagger_api()
        if missing:
            print(f"[backfill] hansard_archive.tagger is missing {missing} — this script needs "
                  "updating before it can tag", flush=True)
            return EXIT_TAG_FAILED
    if args.no_tag and not args.dry_run:
        print("[backfill] WARNING: --no-tag. The hourly archive cron tags untagged sessions "
              "(up to ARCHIVE_TAG_CAP, default 200, a run, newest first), so it will work "
              "through these over its next runs.", flush=True)

    mode = "DRY RUN" if args.dry_run else ("TAG ONLY" if args.tag_only else "INGEST")
    print(f"[backfill] === START {mode} === {args.since} -> {args.until} newest-first, "
          f"houses={','.join(houses)} max_days={args.max_days or '-'} "
          f"max_minutes={args.max_minutes or '-'} tag={'off' if not tagging else args.max_tag}",
          flush=True)
    _print_existing_coverage(args.since, args.until)

    stats = RunStats()

    def window_check() -> str | None:
        return None if args.ignore_cron_windows else blocked_window(now())

    # --- busy / resume --------------------------------------------------------
    prev, resume_state, newest_state = load_state()
    for service in (SERVICE_NAME, PQ_SERVICE_NAME):
        beat = None if args.dry_run else run_active(service, now())
        if beat:
            print(f"[backfill] {service} looks active (heartbeat {beat}) — not starting", flush=True)
            return EXIT_BUSY
    if not args.dry_run and prev and prev.status == "running":
        # The last run was killed (redeploy, container stop) — close its record.
        prev.status = "failed"
        prev.finished_at = prev.finished_at or now()
        db.session.commit()
        print(f"[backfill] previous run id={prev.id} never finished — marked failed (interrupted)",
              flush=True)

    budget = TagBudget(args.max_tag, api_key, sleep, window_check,
                       failures=newest_state.get("tag_failures") or {})
    if budget.skipped:
        print(f"[backfill] {len(budget.skipped)} session(s) skipped by tagging after "
              f"{TAG_MAX_ATTEMPTS} failures (ids in the checkpoint)", flush=True)

    frontier: dict[str, date | None] = {h: None for h in houses}
    same_range = resume_state.get("until") == args.until.isoformat()
    if not args.no_resume and same_range:
        for h in houses:
            v = (resume_state.get("frontier") or {}).get(h)
            frontier[h] = date.fromisoformat(v) if v else None
        print(f"[backfill] resuming: frontier {resume_state.get('frontier')}", flush=True)
    elif resume_state and not args.no_resume:
        print(f"[backfill] saved checkpoint is for until={resume_state.get('until')}, not "
              f"{args.until} — starting fresh", flush=True)

    def next_day(h: str) -> date:
        return (frontier[h] - timedelta(days=1)) if frontier[h] else args.until

    def frontier_json() -> dict:
        return {h: (frontier[h].isoformat() if frontier[h] else None) for h in houses}

    run_rec: HaCronRun | None = None
    if args.tag_only and same_range:
        state = dict(resume_state)               # carry the frontier through a tag-only run
    else:
        state = {"since": args.since.isoformat(), "until": args.until.isoformat(),
                 "frontier": frontier_json()}
    if not args.dry_run:
        run_rec = HaCronRun(service_name=SERVICE_NAME, started_at=now(), days_window=0,
                            status="running")
        db.session.add(run_rec)
        db.session.commit()
        state["tag_failures"] = budget.failures
        _save_checkpoint(run_rec, state, stats)     # first save at once: resumable from here

    # --- tag-only mode ------------------------------------------------------
    if args.tag_only:
        if args.dry_run:
            left = len(_untagged_ids(args.since, args.until, 1_000_000, exclude=budget.excluded))
            print(f"[backfill] === END TAG ONLY DRY RUN === untagged eligible={left} "
                  f"(a run would tag up to {min(left, args.max_tag)})", flush=True)
            return EXIT_OK
        code, reason = EXIT_OK, "tag-only finished"
        try:
            ids = _untagged_ids(args.since, args.until, args.max_tag, exclude=budget.excluded)
            print(f"[backfill] {len(ids)} untagged eligible session(s) selected", flush=True)
            budget.tag(ids)
        except WindowStop as exc:
            reason = f"cron window: {exc} — re-run later"
        except RuntimeError as exc:
            code, reason = EXIT_TAG_FAILED, str(exc)
        if code == EXIT_OK:
            budget.settle()
        stats.tagged = budget.succeeded
        state["tag_failures"] = budget.failures
        state["stop_reason"] = reason
        try:
            _save_checkpoint(run_rec, state, stats, status="ok" if code == EXIT_OK else "failed")
        except Exception as exc:
            print(f"[backfill] could not save final checkpoint: {exc}", flush=True)
            code = code or EXIT_FAILED
        left = len(_untagged_ids(args.since, args.until, 1_000_000, exclude=budget.excluded))
        print(f"[backfill] === END TAG ONLY === exit={code} reason: {reason} tagged={stats.tagged} "
              f"still_untagged={left} skipped={len(budget.skipped)}", flush=True)
        return code

    def finish(code: int, reason: str) -> int:
        stats.stop_reason = reason
        stats.tagged = budget.succeeded
        stats.frontier = frontier_json()
        if code != EXIT_TAG_FAILED:
            budget.settle()
        if run_rec is not None:
            state["frontier"] = stats.frontier
            state["tag_failures"] = budget.failures
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
        print(f"[backfill]   tagging skipped (bad):   {len(budget.skipped)}", flush=True)
        print(f"[backfill]   frontier:                {stats.frontier}", flush=True)
        if resume_at >= args.since:
            print(f"[backfill]   next run starts at:      {resume_at}", flush=True)
        else:
            print("[backfill]   range complete", flush=True)
        return code

    started = time.monotonic()
    last_beat = [time.monotonic()]

    def save_progress() -> None:
        state["frontier"] = frontier_json()
        state["tag_failures"] = budget.failures
        stats.tagged = budget.succeeded
        _save_checkpoint(run_rec, state, stats)
        last_beat[0] = time.monotonic()

    def guard() -> str | None:
        """Cron-window check, plus a heartbeat during long tagging stretches."""
        if run_rec is not None and time.monotonic() - last_beat[0] > HEARTBEAT_EVERY_SECONDS:
            save_progress()
        return window_check()

    budget.window_check = guard

    # --- catch-up tagging for days already done --------------------------------
    if tagging:
        try:
            leftover = _untagged_ids(args.since, args.until, budget.remaining,
                                     exclude=budget.excluded)
            if leftover:
                print(f"[backfill] tagging {len(leftover)} untagged session(s) left from earlier runs",
                      flush=True)
                budget.tag(leftover)
        except WindowStop as exc:
            return finish(EXIT_OK, f"cron window: {exc} — re-run later")
        except RuntimeError as exc:
            return finish(EXIT_TAG_FAILED, str(exc))

    day = max(next_day(h) for h in houses)
    while day >= args.since:
        if args.max_days and stats.days >= args.max_days:
            return finish(EXIT_OK, f"--max-days {args.max_days} reached")
        if args.max_minutes and (time.monotonic() - started) / 60 >= args.max_minutes:
            return finish(EXIT_OK, f"--max-minutes {args.max_minutes} reached")
        if tagging and budget.remaining <= 0:
            return finish(EXIT_OK, f"--max-tag {args.max_tag} reached")

        for h in houses:
            if day > next_day(h):
                continue                        # this house already has this day
            window = window_check()
            if window:
                return finish(EXIT_OK, f"cron window: {window} — re-run later")
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

            if not args.dry_run:
                frontier[h] = day               # ingest complete; tagging catches up later
            if res.sitting:
                stats.sitting_house_days += 1
                stats.new_sessions += res.new
                stats.existing_sessions += res.existing
                stats.contributions += res.contributions
                stats.chars += res.chars
            tagged_now = 0
            if tagging and res.sitting:
                try:
                    tagged_now = budget.tag(_untagged_ids(day, day, budget.remaining, house=h,
                                                          exclude=budget.excluded))
                except WindowStop as exc:
                    return finish(EXIT_OK, f"cron window: {exc} — re-run later")
                except RuntimeError as exc:
                    return finish(EXIT_TAG_FAILED, str(exc))

            if res.sitting:
                print(f"[backfill] {day} {h:<7} sessions={res.walked} new={res.new} "
                      f"existing={res.existing} contributions={res.contributions} "
                      f"tagged={tagged_now} 404s={res.not_found} "
                      f"({time.monotonic() - day_t:.0f}s)", flush=True)
            if run_rec is not None:
                try:
                    save_progress()             # checkpoint per (day, house)
                except OperationalError as exc:
                    return finish(EXIT_FAILED, f"could not save checkpoint after {day} {h}: {exc}")

        stats.days += 1
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
