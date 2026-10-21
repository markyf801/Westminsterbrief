"""
Backfill Written Questions (ha_pq) back to the start of the current Parliament.

Replaces the BLOCKED scripts/backfill_pq_pre_window.py (branch beta only, never
on master), which upserted by UIN (INC-008), kept its checkpoint in a local
file (lost with a Railway container) and walked oldest-first.

Rules
-----
- INSERT ONLY. Uses ingest_pq_date_range (identity: api_id, then (tabled_date,
  uin) — never uin alone). Every row outcome is checked as it happens: any
  ``updated`` or ``identity_mismatch`` stops the run at once and rolls back the
  uncommitted page, so an existing row is never changed. Re-running a day that
  this script already inserted gives ``unchanged`` and is fine.
- The full record (individual endpoint) is fetched for every question, with
  retries. If it still fails the day is not finished (the page is rolled back),
  so no truncated bulk answer is ever stored by this script. A 404 from the
  individual endpoint (question removed at source) keeps the bulk record, the
  same as the cron.
- Newest-first, one tabled day at a time, so ha_pq stays contiguous at every
  stopping point. Frontier checkpoint in ha_cron_run (service name below).
- Tagging (on by default) in capped batches through tag_pq_all_untagged after
  each day, so the PQ cron (which tags every untagged PQ with no cap) never
  finds a large backlog. --max-tag caps PQs sent to Gemini per run.
- Same cron-window guard as scripts/backfill_parliament_start.py, and it will
  not start while that backfill (debates) is running.

Exit codes: 0 finished / stopped cleanly · 1 a day failed after retries or a
database error · 2 API refused (401/403/429) or GEMINI_API_KEY missing ·
3 tagging failed · 4 another backfill is running · 5 a row would be UPDATED or
an identity mismatch was seen — nothing changed, investigate before re-running.

Usage
-----
  python scripts/backfill_pq_parliament_start.py --dry-run          # bulk only, fast
  python scripts/backfill_pq_parliament_start.py --max-days 3
  python scripts/backfill_pq_parliament_start.py --max-days 40 --max-minutes 360
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
from sqlalchemy import func  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import backfill_parliament_start as debates_backfill  # noqa: E402
from backfill_parliament_start import _reset_db, blocked_window, run_active  # noqa: E402
from extensions import db  # noqa: E402
from hansard_archive import pq_ingestor  # noqa: E402
from hansard_archive.models import HaCronRun, HaPQ, HaPQTheme  # noqa: E402
from hansard_archive.tagger import tag_pq_all_untagged  # noqa: E402

PARLIAMENT_FIRST_SITTING = date(2024, 7, 9)
DEFAULT_UNTIL = date(2025, 5, 5)      # ha_pq floor was 2025-05-06 (INC-008 repair)
SERVICE_NAME = debates_backfill.PQ_SERVICE_NAME

DEFAULT_DELAY = 0.5                   # seconds between WQ API requests
INDIVIDUAL_ATTEMPTS = 3
INDIVIDUAL_BACKOFF = (5, 20)
DAY_ATTEMPTS = 2
DAY_RETRY_PAUSE = 60
TAG_CHUNK = 200                       # PQs per tag_pq_all_untagged call
MAX_OUTSIDE_BACKLOG = 500             # untagged PQs newer than the range
MAX_STALLED_TAG_CALLS = 3

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_TAG_FAILED = 3
EXIT_BUSY = 4
EXIT_CONFLICT = 5


class ApiRefused(Exception):
    pass


class DayIncomplete(Exception):
    pass


class WouldUpdate(Exception):
    """An existing row would be changed — the run must stop."""


@dataclass
class RunStats:
    days: int = 0
    inserted: int = 0
    unchanged: int = 0
    would_update: int = 0
    mismatches: int = 0
    tagged: int = 0
    conflicts: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Individual-endpoint fetch with retries (replaces pq_ingestor._fetch_individual
# for the duration of a run; the cron's version swallows every error)
# ---------------------------------------------------------------------------

def make_fetch_individual(sleep):
    def fetch(api_id: int) -> dict | None:
        url = f"{pq_ingestor.WQ_API_BASE}/{api_id}"
        last = ""
        for attempt in range(INDIVIDUAL_ATTEMPTS):
            try:
                resp = requests.get(url, timeout=pq_ingestor._REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json().get("value") or {}
                if resp.status_code in (401, 403, 429):
                    raise ApiRefused(f"individual endpoint {resp.status_code} for id={api_id}")
                if resp.status_code == 404:
                    return None                    # removed at source: keep the bulk record
                last = f"HTTP {resp.status_code}"
            except ApiRefused:
                raise
            except Exception as exc:              # timeouts, resets, bad JSON
                last = str(exc)
            if attempt < INDIVIDUAL_ATTEMPTS - 1:
                sleep(INDIVIDUAL_BACKOFF[min(attempt, len(INDIVIDUAL_BACKOFF) - 1)])
        raise DayIncomplete(f"individual fetch failed for id={api_id}: {last}")
    return fetch


# ---------------------------------------------------------------------------
# One tabled day
# ---------------------------------------------------------------------------

def process_day(day: date, dry_run: bool, delay: float, stats: RunStats, sleep) -> dict:
    def strict(fields, outcome):
        if outcome.action in ("updated", "identity_mismatch"):
            stats.conflicts.append(
                f"{day} {outcome.action} api_id={fields.get('api_id')} uin={fields.get('uin')} "
                f"row={outcome.pq_id} {outcome.detail or sorted(outcome.changes)}")
            if not dry_run:
                raise WouldUpdate(stats.conflicts[-1])

    orig = pq_ingestor._fetch_individual
    pq_ingestor._fetch_individual = make_fetch_individual(sleep)
    try:
        result = pq_ingestor.ingest_pq_date_range(
            day, day, verbose=False, dry_run=dry_run,
            skip_answer_fetch=dry_run,        # dry run: bulk records only (fast)
            max_identity_mismatches=1_000_000 if dry_run else 0,
            request_delay=delay, on_outcome=strict, date_field="tabled")
    except (WouldUpdate, DayIncomplete, ApiRefused):
        db.session.rollback()               # drop this page's uncommitted writes
        raise
    except pq_ingestor.PQIngestAborted as exc:
        db.session.rollback()
        raise DayIncomplete(str(exc)) from exc
    finally:
        pq_ingestor._fetch_individual = orig
    if result["failed"] or result["page_errors"]:
        raise DayIncomplete(f"failed={result['failed']} page_errors={result['page_errors']}")
    return result


def process_day_with_retry(day, dry_run, delay, stats, sleep) -> dict:
    last = ""
    for attempt in range(1, DAY_ATTEMPTS + 1):
        try:
            return process_day(day, dry_run, delay, stats, sleep)
        except (DayIncomplete, requests.RequestException) as exc:
            last = str(exc)
        except OperationalError as exc:
            last = f"database: {exc}"
            _reset_db()
        print(f"[pq-backfill]   {day} attempt {attempt}/{DAY_ATTEMPTS} incomplete: {last[:300]}",
              flush=True)
        if attempt < DAY_ATTEMPTS:
            sleep(DAY_RETRY_PAUSE)
    raise DayIncomplete(last)


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

def _untagged_count(since: date | None = None, until: date | None = None) -> int:
    tagged = db.session.query(HaPQTheme.pq_id)
    q = db.session.query(func.count(HaPQ.id)).filter(~HaPQ.id.in_(tagged))
    if since:
        q = q.filter(HaPQ.tabled_date >= since)
    if until:
        q = q.filter(HaPQ.tabled_date <= until)
    return int(q.scalar() or 0)


class TagBudget:
    """tag_pq_all_untagged tags the NEWEST untagged PQs first, so each call's
    limit is (untagged newer than the range) + the chunk we want from the range."""

    def __init__(self, cap: int, api_key: str, since: date, until: date, window_check) -> None:
        self.cap, self.api_key = cap, api_key
        self.since, self.until = since, until
        self.window_check = window_check
        self.used = 0
        self.succeeded = 0

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    def tag_backlog(self) -> str | None:
        """Tag untagged PQs in the range until none are left or the budget runs
        out. Returns a stop reason, or None when the range backlog is clear."""
        stalled = 0
        while True:
            left = _untagged_count(self.since, self.until)
            if left == 0:
                return None
            if self.remaining <= 0:
                return "max-tag"
            window = self.window_check()
            if window:
                return f"cron window: {window}"
            outside = _untagged_count(self.until + timedelta(days=1))
            if outside > MAX_OUTSIDE_BACKLOG:
                raise RuntimeError(f"{outside} untagged PQs newer than the range — let the PQ "
                                   "cron clear them before backfilling")
            chunk = min(TAG_CHUNK, self.remaining, left)
            self.used += chunk
            result = tag_pq_all_untagged(limit=outside + chunk, api_key=self.api_key, verbose=False)
            self.succeeded += result.get("tagged", 0)
            if _untagged_count(self.since, self.until) >= left:
                stalled += 1
                if stalled >= MAX_STALLED_TAG_CALLS:
                    raise RuntimeError(f"{stalled} tagging calls in a row made no progress "
                                       "— check GEMINI_API_KEY / quota")
            else:
                stalled = 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill Written Questions to the start of the Parliament")
    p.add_argument("--since", type=date.fromisoformat, default=PARLIAMENT_FIRST_SITTING)
    p.add_argument("--until", type=date.fromisoformat, default=DEFAULT_UNTIL,
                   help=f"Latest tabled day, inclusive (default {DEFAULT_UNTIL})")
    p.add_argument("--dry-run", action="store_true",
                   help="Bulk records only; predict inserts / conflicts; write nothing")
    p.add_argument("--max-days", type=int, default=0)
    p.add_argument("--max-minutes", type=int, default=0)
    p.add_argument("--no-tag", action="store_true",
                   help="Do not tag (the PQ cron will then tag them, uncapped)")
    p.add_argument("--max-tag", type=int, default=3000, help="PQs sent to Gemini this run")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--ignore-cron-windows", action="store_true", help="Local testing only")
    return p


def run(args: argparse.Namespace, sleep=time.sleep, now=datetime.utcnow) -> int:
    if args.since > args.until:
        print("[pq-backfill] --since is after --until", flush=True)
        return EXIT_FAILED
    delay = max(args.delay, 0.3)
    tagging = not args.dry_run and not args.no_tag
    api_key = ""
    if tagging:
        from flask import current_app
        api_key = current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("[pq-backfill] GEMINI_API_KEY not set — set it or pass --no-tag", flush=True)
            return EXIT_REFUSED

    floor = db.session.query(func.min(HaPQ.tabled_date)).scalar()
    mode = "DRY RUN" if args.dry_run else "INGEST"
    print(f"[pq-backfill] === START {mode} === {args.since} -> {args.until} newest-first, "
          f"ha_pq floor now {floor}, tag={'off' if not tagging else args.max_tag}", flush=True)
    in_range = (db.session.query(func.count(HaPQ.id))
                .filter(HaPQ.tabled_date >= args.since, HaPQ.tabled_date <= args.until).scalar())
    print(f"[pq-backfill] already stored in range: {in_range}", flush=True)

    for other in (SERVICE_NAME, debates_backfill.SERVICE_NAME):
        if not args.dry_run and run_active(other, now()):
            print(f"[pq-backfill] {other} looks active — not starting", flush=True)
            return EXIT_BUSY

    def window_check() -> str | None:
        return None if args.ignore_cron_windows else blocked_window(now())

    stats = RunStats()
    budget = TagBudget(args.max_tag, api_key, args.since, args.until, window_check)

    prev = (HaCronRun.query.filter_by(service_name=SERVICE_NAME)
            .order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first())
    state: dict = {}
    if prev and prev.notes:
        try:
            state = json.loads(prev.notes)
        except ValueError:
            state = {}
    frontier: date | None = None
    if not args.no_resume and state.get("until") == args.until.isoformat() and state.get("frontier"):
        frontier = date.fromisoformat(state["frontier"])
        print(f"[pq-backfill] resuming: frontier {frontier}", flush=True)

    run_rec = None
    state = {"since": args.since.isoformat(), "until": args.until.isoformat(),
             "frontier": frontier.isoformat() if frontier else None}
    if not args.dry_run:
        if prev and prev.status == "running":
            prev.status = "failed"
            prev.finished_at = prev.finished_at or now()
        run_rec = HaCronRun(service_name=SERVICE_NAME, started_at=now(), days_window=0,
                            status="running")
        db.session.add(run_rec)
        db.session.commit()

    def save(status: str | None = None) -> None:
        if run_rec is None:
            return
        state["frontier"] = frontier.isoformat() if frontier else None
        state["heartbeat"] = now().isoformat(timespec="seconds")
        run_rec.notes = json.dumps(state)
        run_rec.days_window = stats.days
        run_rec.sessions_ingested = stats.inserted
        run_rec.sessions_tagged = budget.succeeded
        if status:
            run_rec.status = status
            run_rec.finished_at = now()
        db.session.commit()

    def finish(code: int, reason: str) -> int:
        stats.tagged = budget.succeeded
        state["stop_reason"] = reason
        try:
            save("ok" if code == EXIT_OK else "failed")
        except Exception as exc:
            _reset_db()
            print(f"[pq-backfill] could not save final checkpoint: {exc}", flush=True)
            code = code or EXIT_FAILED
        print("[pq-backfill] " + "=" * 60, flush=True)
        print(f"[pq-backfill] === END {mode} === exit={code} reason: {reason}", flush=True)
        print(f"[pq-backfill]   tabled days processed: {stats.days}", flush=True)
        print(f"[pq-backfill]   {'would insert' if args.dry_run else 'inserted'}: {stats.inserted}",
              flush=True)
        print(f"[pq-backfill]   already stored (unchanged): {stats.unchanged}", flush=True)
        print(f"[pq-backfill]   would UPDATE / identity mismatch: {stats.would_update} / "
              f"{stats.mismatches}", flush=True)
        for c in stats.conflicts[:20]:
            print(f"[pq-backfill]     {c}", flush=True)
        print(f"[pq-backfill]   tagged: {stats.tagged}", flush=True)
        print(f"[pq-backfill]   frontier: {frontier}", flush=True)
        return code

    started = time.monotonic()
    if tagging:
        try:
            reason = budget.tag_backlog()
        except RuntimeError as exc:
            return finish(EXIT_TAG_FAILED, str(exc))
        if reason:
            return finish(EXIT_OK, f"{reason} (catch-up tagging) — re-run later")

    day = (frontier - timedelta(days=1)) if frontier else args.until
    while day >= args.since:
        if args.max_days and stats.days >= args.max_days:
            return finish(EXIT_OK, f"--max-days {args.max_days} reached")
        if args.max_minutes and (time.monotonic() - started) / 60 >= args.max_minutes:
            return finish(EXIT_OK, f"--max-minutes {args.max_minutes} reached")
        window = window_check()
        if window:
            return finish(EXIT_OK, f"cron window: {window} — re-run later")

        day_t = time.monotonic()
        try:
            res = process_day_with_retry(day, args.dry_run, delay, stats, sleep)
        except WouldUpdate as exc:
            return finish(EXIT_CONFLICT, f"existing row would change — nothing written for it: {exc}")
        except ApiRefused as exc:
            return finish(EXIT_REFUSED, f"API refused on {day}: {exc}")
        except DayIncomplete as exc:
            return finish(EXIT_FAILED, f"{day} failed after {DAY_ATTEMPTS} attempts: {exc}")

        stats.days += 1
        stats.inserted += res["inserted"]
        stats.unchanged += res["unchanged"]
        stats.would_update += res["updated"]
        stats.mismatches += res["identity_mismatch"]
        if not args.dry_run:
            frontier = day
            try:
                save()
            except OperationalError as exc:
                _reset_db()
                return finish(EXIT_FAILED, f"could not save checkpoint after {day}: {exc}")
        print(f"[pq-backfill] {day} inserted={res['inserted']} unchanged={res['unchanged']} "
              f"({time.monotonic() - day_t:.0f}s)", flush=True)

        if tagging and res["inserted"]:
            try:
                reason = budget.tag_backlog()
            except RuntimeError as exc:
                return finish(EXIT_TAG_FAILED, str(exc))
            if reason:
                return finish(EXIT_OK, f"{reason} — re-run later")
        day -= timedelta(days=1)

    if args.dry_run and (stats.would_update or stats.mismatches):
        return finish(EXIT_CONFLICT, "dry run predicts updates / identity mismatches — see list")
    return finish(EXIT_OK, "range complete")


def main(argv: list[str] | None = None, app=None) -> int:
    args = build_parser().parse_args(argv)
    if app is None:
        from flask_app import app
    with app.app_context():
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
