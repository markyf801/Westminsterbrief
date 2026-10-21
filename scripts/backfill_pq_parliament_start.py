"""
Backfill Written Questions (ha_pq) back to the start of the current Parliament.

Replaces the BLOCKED scripts/backfill_pq_pre_window.py (branch beta only, never
on master), which upserted by UIN (INC-008), kept its checkpoint in a local
file (lost with a Railway container) and walked oldest-first.

Rules
-----
- INSERT ONLY. Uses ingest_pq_date_range (identity: api_id, then (tabled_date,
  uin) — never uin alone), with its row write wrapped so that a question
  already stored is SKIPPED and never changed, even when Parliament has since
  corrected it (the log counts those as "stored, differs"; the PQ cron's own
  passes keep recent rows fresh). A row whose identity disagrees with the API
  (identity_mismatch) stops the run with exit 5 and rolls back the uncommitted
  page; so does any 'updated' outcome, which the wrapper makes impossible.
- The full record (individual endpoint) is fetched for every new question,
  with retries. If it still fails, the day is not finished (the page is rolled
  back), so no truncated bulk answer is ever stored by this script. A 404 from
  the individual endpoint (question removed at source) keeps the bulk record,
  as the cron does.
- Newest-first, one tabled day at a time, so ha_pq stays contiguous at every
  stopping point. Frontier checkpoint in ha_cron_run (service name below),
  saved as soon as a run starts and after every day.
- Tagging (on by default) one PQ at a time with tag_pq, limited to the backfill
  range, after each day and for any leftovers at the start of a run, so the PQ
  cron (which tags every untagged PQ with no cap) never finds a large backlog.
  --max-tag caps PQs sent to Gemini per run. A PQ that cannot be tagged is
  skipped after 2 counted failures and never blocks the run (see TagBudget in
  scripts/backfill_parliament_start.py); only a confirmed Gemini outage stops it.
- Same cron-window guard as scripts/backfill_parliament_start.py, checked
  before each tabled day and before each tagging call (a busy day, ~600 PQs at
  ~1.2 s each, takes ~12 minutes — inside the 20-minute lookahead). It will not
  start while the debates backfill is running.

Exit codes: 0 finished / stopped cleanly · 1 a day failed after retries or a
database error · 2 API refused (401/403/429) or GEMINI_API_KEY missing ·
3 tagging failed (Gemini down, or too many untaggable PQs) · 4 another backfill
is running · 5 identity mismatch — nothing changed, investigate before re-running.

Usage
-----
  python scripts/backfill_pq_parliament_start.py --dry-run          # bulk only, fast
  python scripts/backfill_pq_parliament_start.py --max-days 3
  python scripts/backfill_pq_parliament_start.py --max-days 40 --max-minutes 360
See docs/ops/backfill-parliament-start.md for the run order and checks.
"""

from __future__ import annotations

import argparse
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
from backfill_parliament_start import (  # noqa: E402
    TagBudget,
    WindowStop,
    _reset_db,
    _save_checkpoint,
    blocked_window,
    check_tagger_api,
    load_state,
    run_active,
)
from extensions import db  # noqa: E402
from hansard_archive import pq_ingestor  # noqa: E402
from hansard_archive.models import HaCronRun, HaPQ, HaPQTheme  # noqa: E402
from hansard_archive.tagger import tag_pq  # noqa: E402

PARLIAMENT_FIRST_SITTING = date(2024, 7, 9)
DEFAULT_UNTIL = date(2025, 5, 5)      # ha_pq floor was 2025-05-06 (INC-008 repair)
SERVICE_NAME = debates_backfill.PQ_SERVICE_NAME

DEFAULT_DELAY = 0.5                   # seconds between WQ API requests
INDIVIDUAL_ATTEMPTS = 3
INDIVIDUAL_BACKOFF = (5, 20)
DAY_ATTEMPTS = 2
DAY_RETRY_PAUSE = 60

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
    """An existing row would be changed, or its identity disagrees — stop."""


@dataclass
class RunStats:
    days: int = 0
    inserted: int = 0
    stored_same: int = 0
    stored_differs: int = 0
    mismatches: int = 0
    tagged: int = 0
    conflicts: list = field(default_factory=list)
    differs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Wrappers around pq_ingestor for the duration of one day (no edits there)
# ---------------------------------------------------------------------------

def make_fetch_individual(sleep):
    """pq_ingestor._fetch_individual with retries; the cron's version swallows errors."""
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


def make_insert_only_upsert(orig_upsert, stats: RunStats, day: date):
    """
    pq_ingestor.upsert_pq that never changes a stored row: a question already
    stored under a consistent identity is reported 'unchanged' and left alone.
    New questions and identity disagreements go to the real upsert_pq — it
    inserts the first and writes nothing for the second (identity_mismatch).
    """
    def upsert(fields: dict, *, text_complete: bool = True, dry_run: bool = False):
        existing, matched_by, ambiguous = pq_ingestor.find_existing_pq(
            fields.get("api_id"), fields.get("tabled_date"), fields.get("uin"))
        if existing is None or ambiguous:
            return orig_upsert(fields, text_complete=text_complete, dry_run=dry_run)
        if matched_by == "api_id":
            consistent = (pq_ingestor._same_uin(existing.uin, fields["uin"])
                          and existing.tabled_date == fields["tabled_date"])
        else:                                     # (tabled_date, uin) match
            consistent = not (existing.api_id and fields.get("api_id")
                              and existing.api_id != fields["api_id"])
        if not consistent:
            return orig_upsert(fields, text_complete=text_complete, dry_run=True)
        if pq_ingestor._planned_changes(existing, fields, matched_by, text_complete):
            stats.stored_differs += 1
            if len(stats.differs) < 20:
                stats.differs.append(f"{day} api_id={fields.get('api_id')} uin={fields['uin']} "
                                     f"row={existing.id}")
        else:
            stats.stored_same += 1
        return pq_ingestor.UpsertOutcome("unchanged", pq_id=existing.id, detail="stored — kept")
    return upsert


# ---------------------------------------------------------------------------
# One tabled day
# ---------------------------------------------------------------------------

def process_day(day: date, dry_run: bool, delay: float, stats: RunStats, sleep) -> dict:
    def strict(fields, outcome):
        if outcome.action in ("updated", "identity_mismatch"):
            stats.mismatches += 1
            stats.conflicts.append(
                f"{day} {outcome.action} api_id={fields.get('api_id')} uin={fields.get('uin')} "
                f"row={outcome.pq_id} {outcome.detail or sorted(outcome.changes)}")
            if not dry_run:
                raise WouldUpdate(stats.conflicts[-1])

    orig_fetch, orig_upsert = pq_ingestor._fetch_individual, pq_ingestor.upsert_pq
    pq_ingestor._fetch_individual = make_fetch_individual(sleep)
    pq_ingestor.upsert_pq = make_insert_only_upsert(orig_upsert, stats, day)
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
        pq_ingestor._fetch_individual, pq_ingestor.upsert_pq = orig_fetch, orig_upsert
    if result["failed"] or result["page_errors"]:
        raise DayIncomplete(f"failed={result['failed']} page_errors={result['page_errors']}")
    return result


def process_day_with_retry(day, dry_run, delay, stats, sleep) -> dict:
    last = ""
    for attempt in range(1, DAY_ATTEMPTS + 1):
        before = (stats.stored_same, stats.stored_differs, len(stats.differs))
        try:
            return process_day(day, dry_run, delay, stats, sleep)
        except (DayIncomplete, requests.RequestException) as exc:
            last = str(exc)
        except OperationalError as exc:
            last = f"database: {exc}"
            _reset_db()
        stats.stored_same, stats.stored_differs = before[0], before[1]
        del stats.differs[before[2]:]
        print(f"[pq-backfill]   {day} attempt {attempt}/{DAY_ATTEMPTS} incomplete: {last[:300]}",
              flush=True)
        if attempt < DAY_ATTEMPTS:
            sleep(DAY_RETRY_PAUSE)
    raise DayIncomplete(last)


# ---------------------------------------------------------------------------
# Tagging — only PQs in the backfill range, one at a time
# ---------------------------------------------------------------------------

def _untagged_pq_ids(since: date, until: date, limit: int, exclude=()) -> list[int]:
    tagged = db.session.query(HaPQTheme.pq_id)
    q = (db.session.query(HaPQ.id)
         .filter(HaPQ.tabled_date >= since, HaPQ.tabled_date <= until,
                 HaPQ.question_text.isnot(None),
                 ~HaPQ.id.in_(tagged)))
    if exclude:
        q = q.filter(~HaPQ.id.in_(list(exclude)))
    return [r[0] for r in q.order_by(HaPQ.tabled_date.desc(), HaPQ.id).limit(limit).all()]


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
        missing = check_tagger_api()
        if missing:
            print(f"[pq-backfill] hansard_archive.tagger is missing {missing} — this script needs "
                  "updating before it can tag", flush=True)
            return EXIT_TAG_FAILED

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
    prev, resume_state, newest_state = load_state(SERVICE_NAME)
    budget = TagBudget(args.max_tag, api_key, sleep, window_check,
                       tag_fn=lambda pq_id: tag_pq(pq_id, api_key=api_key),
                       failures=newest_state.get("tag_failures") or {}, label="PQ", kind="pq")

    frontier: date | None = None
    if (not args.no_resume and resume_state.get("until") == args.until.isoformat()
            and resume_state.get("frontier")):
        frontier = date.fromisoformat(resume_state["frontier"])
        print(f"[pq-backfill] resuming: frontier {frontier}", flush=True)

    run_rec = None
    state = {"since": args.since.isoformat(), "until": args.until.isoformat(),
             "frontier": frontier.isoformat() if frontier else None}
    last_beat = [time.monotonic()]

    def save(status: str | None = None) -> None:
        if run_rec is None:
            return
        state["frontier"] = frontier.isoformat() if frontier else None
        state["tag_failures"] = budget.failures
        stats.tagged = budget.succeeded
        run_stats = debates_backfill.RunStats(days=stats.days, new_sessions=stats.inserted,
                                              tagged=budget.succeeded)
        _save_checkpoint(run_rec, state, run_stats, status=status)
        last_beat[0] = time.monotonic()

    if not args.dry_run:
        if prev and prev.status == "running":
            prev.status = "failed"
            prev.finished_at = prev.finished_at or now()
        run_rec = HaCronRun(service_name=SERVICE_NAME, started_at=now(), days_window=0,
                            status="running")
        db.session.add(run_rec)
        db.session.commit()
        save()                               # resumable from the first moment

    def guard() -> str | None:
        if run_rec is not None and time.monotonic() - last_beat[0] > debates_backfill.HEARTBEAT_EVERY_SECONDS:
            save()
        return window_check()

    budget.window_check = guard

    def finish(code: int, reason: str) -> int:
        if code != EXIT_TAG_FAILED:
            budget.settle()
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
        print(f"[pq-backfill]   already stored, kept as is: {stats.stored_same} same, "
              f"{stats.stored_differs} differ from the API", flush=True)
        for d in stats.differs:
            print(f"[pq-backfill]     differs: {d}", flush=True)
        print(f"[pq-backfill]   identity mismatches: {stats.mismatches}", flush=True)
        for c in stats.conflicts[:20]:
            print(f"[pq-backfill]     {c}", flush=True)
        print(f"[pq-backfill]   tagged: {budget.succeeded} (skipped as untaggable: "
              f"{len(budget.skipped)})", flush=True)
        print(f"[pq-backfill]   frontier: {frontier}", flush=True)
        return code

    def tag_range(since: date, until: date) -> tuple[int, str] | None:
        try:
            budget.tag(_untagged_pq_ids(since, until, budget.remaining, exclude=budget.excluded))
        except WindowStop as exc:
            return EXIT_OK, f"cron window: {exc} — re-run later"
        except RuntimeError as exc:
            return EXIT_TAG_FAILED, str(exc)
        return None

    started = time.monotonic()
    if tagging:
        stop = tag_range(args.since, args.until)          # leftovers from earlier runs
        if stop:
            return finish(*stop)

    day = (frontier - timedelta(days=1)) if frontier else args.until
    while day >= args.since:
        if args.max_days and stats.days >= args.max_days:
            return finish(EXIT_OK, f"--max-days {args.max_days} reached")
        if args.max_minutes and (time.monotonic() - started) / 60 >= args.max_minutes:
            return finish(EXIT_OK, f"--max-minutes {args.max_minutes} reached")
        if tagging and budget.remaining <= 0:
            return finish(EXIT_OK, f"--max-tag {args.max_tag} reached")
        window = window_check()
        if window:
            return finish(EXIT_OK, f"cron window: {window} — re-run later")

        day_t = time.monotonic()
        try:
            res = process_day_with_retry(day, args.dry_run, delay, stats, sleep)
        except WouldUpdate as exc:
            return finish(EXIT_CONFLICT, f"identity mismatch — nothing written for it: {exc}")
        except ApiRefused as exc:
            return finish(EXIT_REFUSED, f"API refused on {day}: {exc}")
        except DayIncomplete as exc:
            return finish(EXIT_FAILED, f"{day} failed after {DAY_ATTEMPTS} attempts: {exc}")

        stats.days += 1
        stats.inserted += res["inserted"]
        if not args.dry_run:
            frontier = day
            try:
                save()
            except OperationalError as exc:
                _reset_db()
                return finish(EXIT_FAILED, f"could not save checkpoint after {day}: {exc}")
        print(f"[pq-backfill] {day} inserted={res['inserted']} already_stored={res['unchanged']} "
              f"({time.monotonic() - day_t:.0f}s)", flush=True)

        if tagging and res["inserted"]:
            stop = tag_range(day, day)
            if stop:
                return finish(*stop)
        day -= timedelta(days=1)

    if args.dry_run and stats.mismatches:
        return finish(EXIT_CONFLICT, "dry run predicts identity mismatches — see list")
    return finish(EXIT_OK, "range complete")


def main(argv: list[str] | None = None, app=None) -> int:
    args = build_parser().parse_args(argv)
    if app is None:
        from flask_app import app
    with app.app_context():
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
