"""
========================================================================
BLOCKED — DO NOT RUN — see INC-008 (cross-session UIN collision).
Pending identity-key decision.

WQ UINs reset per parliamentary session and are NOT globally unique.
Because ha_pq.uin is UNIQUE, this script's upsert-by-UIN collides July-2024
questions with already-present current-session rows and OVERWRITES them
(corrupted 690 production rows on 13 Jun 2026 — see docs/incident-log.md).
Do not run until PQ identity is re-keyed (api_id or composite key); that is
a separate scoping pass. Left in the repo so the block is repo-enforced.
========================================================================

Pre-window PQ answer backfill — ingest Written Questions tabled between
9 July 2024 (first sitting of the current Parliament) and the current
earliest tabled_date in ha_pq, WITH full answers.

This closes the gap left when the archive used a 12-month rolling window.
Retention posture (locked 11 Jun 2026): Westminster Brief holds the full
record of the current Parliament back to 9 July 2024 — nothing prunes.

WHAT IT DOES
  - Reads the floor at runtime: SELECT MIN(tabled_date) FROM ha_pq.
  - Walks [2024-07-09, floor] in chunks, calling the existing
    hansard_archive.pq_ingestor.ingest_pq_date_range per chunk. That
    function paginates the bulk WQ API, upserts by UIN (idempotent),
    fetches the individual endpoint per row for full answer + question
    text, and sanitises HTML (tables/lists preserved). We REUSE it —
    no re-implementation of API paging.
  - INSERT-only in effect: it adds rows back toward 9 July 2024. No
    schema changes, no migrations, no DROP/DELETE/TRUNCATE.

THEME TAGGING — DECIDED (brief, 11 Jun 2026): this script NEVER runs
inline Gemini tagging. Theme tagging is a SEPARATE follow-up pass after
verification. --no-tag is the default and only behaviour; the flag is
accepted for cron-interface parity but is a no-op here.

CRON COEXISTENCE: the daily PQ cron (7-day rolling window) may run
during this backfill. That is acceptable — the date ranges are disjoint
and the upsert is idempotent by UIN. Do NOT start this backfill within
30 minutes of a scheduled PQ cron window.

CONCURRENCY GUARDRAIL (decision #2, 11 Jun 2026): this must NOT run
concurrently with the Hansard debates backfill. The PQ pre-window
backfill runs FIRST; the Hansard backfill stays queued behind it.
Reason: Railway connection-pool pressure + API courtesy to Parliament.

DETACHED EXECUTION ONLY: multi-hour job — never run in an interactive
terminal (it will die on disconnect). Preferred: a Railway one-shot
service tracking master. Alternative: nohup against the production
DATABASE_URL. Capture full logs BEFORE tearing down any one-shot
service.

Resume behaviour: on interrupt (SIGKILL, power loss, idle-connection
death) restart with the same command — it resumes from the
last-completed chunk recorded in scripts/backfill_pq_pre_window.checkpoint.
On clean completion the checkpoint file is removed.

Usage:
  python scripts/backfill_pq_pre_window.py --dry-run   # count only, no writes
  python scripts/backfill_pq_pre_window.py --test      # first chunk only
  python scripts/backfill_pq_pre_window.py             # full detached run

Required env:
  DATABASE_URL — Postgres (falls back to local SQLite if unset)
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

import hansard_archive.pq_ingestor as pq_ingestor
from hansard_archive.pq_ingestor import ingest_pq_date_range

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pre-window] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger(__name__)

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_REQUEST_TIMEOUT = 60

# First sitting of the current Parliament — start of the retention window.
_WINDOW_START = date(2024, 7, 9)

# Individual-endpoint delay (Stage B value — 0 429s observed). Floor 0.3s.
# Applied by overriding the ingestor's module-level constant before the run,
# so the reused ingest_pq_date_range honours it without re-implementation.
_DEFAULT_DELAY = 0.5
_MIN_DELAY = 0.3

# Default chunk width. Day-level chunking gives the finest checkpoint
# granularity; 3-day windows cut bulk round-trips. Either is sanctioned by
# the brief — 3 is a reasonable middle (≤3 days re-done on resume).
_DEFAULT_CHUNK_DAYS = 3

_CHECKPOINT = os.path.join(os.path.dirname(__file__), "backfill_pq_pre_window.checkpoint")


# ---------------------------------------------------------------------------
# Checkpoint helpers (mirrors scripts/backfill_pq_questions.py)
# ---------------------------------------------------------------------------

def _read_checkpoint() -> date | None:
    if os.path.exists(_CHECKPOINT):
        try:
            with open(_CHECKPOINT) as f:
                val = date.fromisoformat(f.read().strip())
            _log.info("Resuming from checkpoint: last completed chunk ended %s", val)
            return val
        except (ValueError, OSError):
            pass
    return None


def _write_checkpoint(chunk_end: date) -> None:
    try:
        with open(_CHECKPOINT, "w") as f:
            f.write(chunk_end.isoformat())
    except OSError as exc:
        _log.warning("Could not write checkpoint: %s", exc)


def _clear_checkpoint() -> None:
    try:
        if os.path.exists(_CHECKPOINT):
            os.unlink(_CHECKPOINT)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Range / count helpers
# ---------------------------------------------------------------------------

def _get_floor(db, HaPQ) -> date | None:
    """Current earliest tabled_date in ha_pq (the floor we backfill up to)."""
    return db.session.query(func.min(HaPQ.tabled_date)).scalar()


def _count_range(date_from: date, date_to: date) -> int | None:
    """Total WQs tabled in [date_from, date_to] per the bulk endpoint's
    totalResults field. One lightweight call (take=1); no individual fetches,
    no DB writes. Returns None if the count field is unavailable."""
    try:
        resp = requests.get(WQ_API_BASE, params={
            "tabledWhenFrom": date_from.isoformat(),
            "tabledWhenTo": date_to.isoformat(),
            "take": 1,
            "skip": 0,
        }, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("totalResults")
    except Exception as exc:
        _log.error("Count query failed for %s..%s: %s", date_from, date_to, exc)
        return None


def _iter_chunks(start: date, end: date, chunk_days: int):
    """Yield (chunk_start, chunk_end) windows of chunk_days, inclusive of end."""
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def run_backfill(db, HaPQ, *, test_mode: bool, chunk_days: int) -> None:
    floor = _get_floor(db, HaPQ)
    if floor is None:
        _log.error("ha_pq is empty (MIN(tabled_date) is NULL). Aborting — "
                   "run the normal ingest/backfill first.")
        return

    _log.info("Current floor (MIN tabled_date): %s", floor)

    # Range is [2024-07-09, floor] inclusive. Including floor itself is the
    # intended small overlap (those rows are already present; upsert-by-UIN
    # makes the re-ingest harmless).
    if floor <= _WINDOW_START:
        _log.info("No gap — floor %s is at or before window start %s. Nothing to do.",
                  floor, _WINDOW_START)
        return

    resume_after = _read_checkpoint() if not test_mode else None
    chunk_start = (resume_after + timedelta(days=1)) if resume_after else _WINDOW_START
    if chunk_start > floor:
        _log.info("Checkpoint (%s) is already at/after floor (%s). Nothing to do.",
                  resume_after, floor)
        _clear_checkpoint()
        return

    _log.info("Backfilling %s → %s in %d-day chunks (resume from %s)%s",
              chunk_start, floor, chunk_days, chunk_start,
              " — TEST MODE (first chunk only)" if test_mode else "")

    tot_ins = tot_upd = tot_err = 0
    chunks_done = 0

    for c_start, c_end in _iter_chunks(chunk_start, floor, chunk_days):
        # Per-chunk connection resilience: on OperationalError, dispose the
        # engine and retry the chunk once (mirrors backfill_pq_answers.py).
        for attempt in range(2):
            try:
                result = ingest_pq_date_range(c_start, c_end, verbose=True,
                                              skip_answer_fetch=False)
                break
            except OperationalError as exc:
                _log.warning("OperationalError on chunk %s..%s (attempt %d): %s — "
                             "disposing engine and retrying once.",
                             c_start, c_end, attempt + 1, exc)
                db.session.rollback()
                db.engine.dispose()
                if attempt == 1:
                    _log.error("Chunk %s..%s failed after retry — leaving checkpoint "
                               "before this chunk so a re-run resumes here.",
                               c_start, c_end)
                    result = None
        if result is None:
            # Do not advance the checkpoint past a chunk that did not complete.
            return

        tot_ins += result["inserted"]
        tot_upd += result["updated"]
        tot_err += result["errors"]
        chunks_done += 1

        if not test_mode:
            _write_checkpoint(c_end)

        _log.info("Chunk %s..%s done — chunk(ins=%d upd=%d err=%d) | "
                  "cumulative(ins=%d upd=%d err=%d) across %d chunk(s)",
                  c_start, c_end,
                  result["inserted"], result["updated"], result["errors"],
                  tot_ins, tot_upd, tot_err, chunks_done)

        if test_mode:
            _log.info("TEST MODE: stopping after first chunk.")
            break

    if not test_mode:
        _clear_checkpoint()
        _log.info("=== Pre-window backfill complete === "
                  "inserted=%d updated=%d errors=%d", tot_ins, tot_upd, tot_err)
    else:
        _log.info("=== Test chunk complete === "
                  "inserted=%d updated=%d errors=%d", tot_ins, tot_upd, tot_err)


def run_dry_run(db, HaPQ, chunk_days: int) -> None:
    floor = _get_floor(db, HaPQ)
    if floor is None:
        _log.error("ha_pq is empty (MIN(tabled_date) is NULL).")
        return
    _log.info("Current floor (MIN tabled_date): %s", floor)
    if floor <= _WINDOW_START:
        _log.info("No gap — floor %s is at or before window start %s. Nothing to do.",
                  floor, _WINDOW_START)
        return

    _log.info("Range to backfill: %s → %s (inclusive)", _WINDOW_START, floor)
    total = _count_range(_WINDOW_START, floor)
    if total is None:
        _log.warning("Could not read totalResults from the bulk endpoint — "
                     "cannot report an expected row count.")
        return

    _log.info("Expected rows in range (bulk totalResults): %d", total)
    _log.info("Brief estimate: 30,000–50,000.")
    if total > 100_000 or total < 15_000:
        _log.warning("Count %d is >2x off the 30k–50k estimate — PAUSE and report. "
                     "A HIGH count may be genuine (post-election PQ surge, "
                     "summer/autumn 2024) — report it, do not assume a query error.",
                     total)
    else:
        _log.info("Count is within a sane band of the estimate.")

    # First-chunk preview (what --test will process).
    first_start, first_end = next(_iter_chunks(_WINDOW_START, floor, chunk_days))
    first_count = _count_range(first_start, first_end)
    _log.info("First chunk (--test will process): %s..%s — ~%s rows",
              first_start, first_end,
              first_count if first_count is not None else "unknown")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-window PQ answer backfill (9 Jul 2024 → current floor).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count expected rows only — no API fetches of answers, no writes")
    parser.add_argument("--test", action="store_true",
                        help="Process the first chunk only (no checkpoint written)")
    parser.add_argument("--no-tag", action="store_true",
                        help="Accepted for cron-interface parity. This script NEVER tags "
                             "inline (decision #2, 11 Jun 2026); tagging is a separate pass.")
    parser.add_argument("--chunk-days", type=int, default=_DEFAULT_CHUNK_DAYS,
                        help=f"Chunk width in days (default: {_DEFAULT_CHUNK_DAYS})")
    parser.add_argument("--delay", type=float, default=_DEFAULT_DELAY,
                        help=f"Individual-endpoint delay in seconds "
                             f"(default: {_DEFAULT_DELAY}, floor: {_MIN_DELAY})")
    args = parser.parse_args()

    if args.chunk_days < 1:
        parser.error("--chunk-days must be >= 1")

    delay = max(args.delay, _MIN_DELAY)
    if delay != args.delay:
        _log.warning("Requested delay %.2fs is below floor — clamped to %.2fs",
                     args.delay, delay)

    # Override the ingestor's inter-request delay (default 0.3s) for this run.
    # ingest_pq_date_range reads this module global at call time.
    pq_ingestor._INTER_REQUEST_DELAY = delay
    _log.info("Individual-endpoint delay set to %.2fs", delay)

    from flask_app import app
    from extensions import db
    from hansard_archive.models import HaPQ

    with app.app_context():
        # Log which DB we are pointed at (mirrors ingest_pq_cron.py) — important
        # for the detached run log so it is unambiguous this hit production.
        from sqlalchemy import text as sqla_text
        safe_url = db.engine.url.render_as_string(hide_password=True)
        _log.info("DB engine URL (password hidden): %s", safe_url)
        try:
            row = db.session.execute(
                sqla_text("SELECT inet_server_addr()::text, current_database()")
            ).fetchone()
            _log.info("DB host: %s, database: %s", row[0], row[1])
        except Exception as exc:
            _log.info("Could not query DB host (likely SQLite): %s", exc)

        if args.dry_run:
            run_dry_run(db, HaPQ, chunk_days=args.chunk_days)
        else:
            run_backfill(db, HaPQ, test_mode=args.test, chunk_days=args.chunk_days)


if __name__ == "__main__":
    main()
