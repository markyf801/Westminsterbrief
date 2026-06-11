"""
Pre-window PQ backfill — ingest Written Questions (WITH full answers) for the
date range that predates the existing ha_pq coverage, back to the first sitting
of the current Parliament (9 July 2024).

WHY
The May 2026 answer backfill cached full answers for every PQ that was in ha_pq
at the time (the then-current ~12-month window). Under the locked retention
posture (CLAUDE.md, 11 Jun 2026) Westminster Brief holds the full current-Parliament
record from 9 July 2024. The dates BEFORE the old ~12-month floor were never
ingested. This script fills that gap.

HOW
A single date-range ingest via hansard_archive.pq_ingestor.ingest_pq_date_range —
NOT a separate answer-only pass. That function already fetches the Parliament
individual endpoint by default for every PQ (full answer + full question text +
is_holding + is_withdrawn) and sanitises answer HTML (tables preserved). We just
drive it over the missing range in resumable day/few-day chunks.

  - Floor read at runtime: MIN(tabled_date) FROM ha_pq. Range = [2024-07-09, floor]
    inclusive, plus a small overlap past the floor (upsert-by-UIN is idempotent,
    so overlap is harmless). If MIN(tabled_date) <= 2024-07-09, there is no gap.
  - Chunked (default 3-day windows) so per-chunk commits keep DB connections fresh
    (Railway kills idle Postgres connections after a few hours) and the checkpoint
    can advance cleanly.
  - Checkpoint file (scripts/backfill_pq_pre_window.checkpoint) stores the last
    completed chunk end-date. On restart the script resumes from there; the file is
    removed on clean completion. (Same pattern as backfill_pq_questions.py.)
  - OperationalError around each chunk -> rollback + db.engine.dispose() + retry once
    (same pattern as backfill_pq_answers.py).
  - Individual-endpoint request delay forced to 0.5s (the Stage B value) for API
    courtesy — see _BACKFILL_DELAY below.

THEME TAGGING: NOT run here. Tagging is a separate follow-up pass after verification
(locked decision, 11 Jun 2026). --no-tag is accepted for parity with the brief but
is the only mode — this script never invokes the tagger.

CRON COEXISTENCE: the daily PQ cron (ingest_pq_cron.py, 7-day rolling window) may
run while this backfill is in progress. That is acceptable — the date ranges are
disjoint (cron = last 7 days; backfill = mid-2024 -> old floor) and every write is
an idempotent upsert-by-UIN. To limit Railway connection-pool contention, DO NOT
start this backfill within 30 minutes of a scheduled PQ cron window (see CLAUDE.md
"Ingestion schedules" for the cron times).

EXECUTION: this is a multi-hour job — NEVER run it in an interactive terminal.
Preferred: a Railway one-shot service tracking master (runs near the DB). Capture
the full log BEFORE tearing the service down. INSERT/UPSERT only — no schema
changes, no migrations, no destructive SQL.

Usage:
  python scripts/backfill_pq_pre_window.py --dry-run     # count expected rows, no writes
  python scripts/backfill_pq_pre_window.py --test        # ingest first chunk only (writes)
  python scripts/backfill_pq_pre_window.py               # full resumable run
  python scripts/backfill_pq_pre_window.py --floor 2025-05-01   # override the floor read
  python scripts/backfill_pq_pre_window.py --chunk-days 1       # 1-day chunks instead of 3

Requires DATABASE_URL set to the Railway Postgres URL (and outbound access to the
Parliament WQ API).
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

# Reuse the live ingestor. We deliberately do NOT modify it (the daily cron shares
# it); instead we raise its inter-request delay to the briefed 0.5s for THIS process
# only, by overriding the module constant before any call.
import hansard_archive.pq_ingestor as pqi
from hansard_archive.pq_ingestor import ingest_pq_date_range

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pq-prewindow] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------
FIRST_SITTING = date(2024, 7, 9)        # first sitting of the current Parliament
_OVERLAP_DAYS = 3                       # extend past the floor; idempotent upsert
_CHUNK_DAYS_DEFAULT = 3
_BACKFILL_DELAY = 0.5                   # individual-endpoint courtesy delay (Stage B value)
_EST_LOW, _EST_HIGH = 30_000, 50_000   # expected row-count band for the gap

WQ_API_BASE = pqi.WQ_API_BASE
_REQUEST_TIMEOUT = 60

_CHECKPOINT = os.path.join(os.path.dirname(__file__), "backfill_pq_pre_window.checkpoint")

# Force the briefed 0.5s individual-endpoint delay for this process only.
pqi._INTER_REQUEST_DELAY = _BACKFILL_DELAY


# --- Checkpoint helpers (date-based; mirrors backfill_pq_questions.py) --------
def _read_checkpoint() -> date | None:
    if os.path.exists(_CHECKPOINT):
        try:
            with open(_CHECKPOINT) as f:
                val = date.fromisoformat(f.read().strip())
            _log.info("Resuming from checkpoint: last completed chunk end = %s", val)
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


# --- DB / API helpers --------------------------------------------------------
def _print_db_host(db) -> None:
    """Print which DB this process is hitting — a safety check before any write run."""
    from sqlalchemy import text as sqla_text
    safe_url = db.engine.url.render_as_string(hide_password=True)
    _log.info("DB engine URL (password hidden): %s", safe_url)
    try:
        row = db.session.execute(
            sqla_text("SELECT inet_server_addr()::text, current_database()")
        ).fetchone()
        _log.info("DB host: %s, database: %s", row[0], row[1])
    except Exception as exc:
        _log.info("Could not query DB host: %s", exc)


def _read_floor(db, HaPQ) -> date | None:
    """MIN(tabled_date) in ha_pq, with one reconnect retry."""
    for attempt in range(2):
        try:
            return db.session.query(func.min(HaPQ.tabled_date)).scalar()
        except OperationalError as exc:
            _log.warning("Floor query connection error (attempt %d): %s — reconnecting.", attempt + 1, exc)
            db.session.rollback()
            db.engine.dispose()
    return db.session.query(func.min(HaPQ.tabled_date)).scalar()


def _count_api_rows(date_from: date, date_to: date) -> int | None:
    """Return the API's totalResults for the range without ingesting anything."""
    params = {
        "tabledWhenFrom": date_from.isoformat(),
        "tabledWhenTo": date_to.isoformat(),
        "take": 1,
    }
    try:
        resp = requests.get(WQ_API_BASE, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("totalResults")
    except Exception as exc:
        _log.error("Dry-run API count failed: %s", exc)
        return None


def _resolve_range(db, HaPQ, floor_override: date | None) -> tuple[date, date] | None:
    """Resolve [start, end] for the backfill, or None if there is no gap."""
    floor = floor_override or _read_floor(db, HaPQ)
    if floor is None:
        _log.warning("ha_pq is empty (MIN(tabled_date) is NULL) — refusing to guess; aborting.")
        return None
    _log.info("Current ha_pq floor (MIN tabled_date): %s%s",
              floor, " (overridden)" if floor_override else "")
    if floor <= FIRST_SITTING:
        _log.info("Floor (%s) is already at/before first sitting (%s) — no gap, nothing to do.",
                  floor, FIRST_SITTING)
        return None
    end = floor + timedelta(days=_OVERLAP_DAYS)
    return FIRST_SITTING, end


# --- Modes -------------------------------------------------------------------
def run_dry_run(db, HaPQ, floor_override: date | None) -> None:
    rng = _resolve_range(db, HaPQ, floor_override)
    if rng is None:
        return
    start, end = rng
    _log.info("Backfill range would be: %s -> %s (incl. %d-day overlap past floor)",
              start, end, _OVERLAP_DAYS)
    total = _count_api_rows(start, end)
    if total is None:
        _log.error("Could not obtain API row count — resolve API access before proceeding.")
        return
    _log.info("API totalResults for range: %d  (estimate band: %d–%d)", total, _EST_LOW, _EST_HIGH)
    if total > _EST_HIGH * 2 or total < _EST_LOW / 2:
        _log.warning(
            "COUNT IS >2x OUTSIDE THE ESTIMATE BAND. PAUSE and report to Mark before any run. "
            "A HIGH count may be genuine (post-election PQ surge, Jul–autumn 2024) — report, don't assume error."
        )
    elif total > _EST_HIGH:
        _log.info("Count is above the band but within 2x — likely the post-election surge. Report to Mark.")
    else:
        _log.info("Count is within expectations.")
    est_hours = total * _BACKFILL_DELAY / 3600 if total else 0
    _log.info("Rough full-run time at %.1fs/row: ~%.1f hours.", _BACKFILL_DELAY, est_hours)


def _ingest_chunk(db, chunk_from: date, chunk_to: date) -> dict:
    """Call the shared ingestor for one chunk, with one reconnect retry."""
    try:
        return ingest_pq_date_range(chunk_from, chunk_to, verbose=True)
    except OperationalError as exc:
        _log.warning("Chunk %s->%s connection error: %s — reconnecting and retrying once.",
                     chunk_from, chunk_to, exc)
        db.session.rollback()
        db.engine.dispose()
        return ingest_pq_date_range(chunk_from, chunk_to, verbose=True)


def run_backfill(db, HaPQ, floor_override: date | None, chunk_days: int, test_mode: bool) -> None:
    rng = _resolve_range(db, HaPQ, floor_override)
    if rng is None:
        return
    start, end = rng

    # Resume from checkpoint (full runs only; --test always starts at the gap start).
    if not test_mode:
        cp = _read_checkpoint()
        if cp is not None:
            start = max(start, cp + timedelta(days=1))
            if start > end:
                _log.info("Checkpoint (%s) already past range end (%s) — nothing left. Clearing.", cp, end)
                _clear_checkpoint()
                return

    _log.info("=== Pre-window backfill: %s -> %s | chunk=%dd | delay=%.1fs | tagging=OFF (separate pass) ===",
              start, end, chunk_days, _BACKFILL_DELAY)
    if test_mode:
        _log.info("TEST MODE: first chunk only; checkpoint NOT written.")

    tot_ins = tot_upd = tot_err = 0
    cur = start
    while cur <= end:
        chunk_to = min(cur + timedelta(days=chunk_days - 1), end)
        _log.info("Chunk %s -> %s …", cur, chunk_to)
        result = _ingest_chunk(db, cur, chunk_to)
        tot_ins += result["inserted"]
        tot_upd += result["updated"]
        tot_err += result["errors"]
        _log.info("Chunk done — ins=%d upd=%d err=%d | running totals ins=%d upd=%d err=%d",
                  result["inserted"], result["updated"], result["errors"], tot_ins, tot_upd, tot_err)

        if not test_mode:
            _write_checkpoint(chunk_to)

        if test_mode:
            _log.info("TEST MODE: stopping after first chunk.")
            break
        cur = chunk_to + timedelta(days=1)

    _log.info("=== Backfill pass complete — inserted=%d updated=%d errors=%d ===",
              tot_ins, tot_upd, tot_err)

    if not test_mode and tot_err == 0:
        _clear_checkpoint()
    elif not test_mode:
        _log.warning("errors > 0 — checkpoint retained for inspection/resume (not cleared).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-window PQ backfill (9 Jul 2024 -> existing ha_pq floor), with full answers.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count expected rows for the range; no writes.")
    parser.add_argument("--test", action="store_true",
                        help="Ingest the first chunk only (writes); for gate spot-checks.")
    parser.add_argument("--no-tag", action="store_true",
                        help="Accepted for parity; this script never tags (separate follow-up pass).")
    parser.add_argument("--floor", type=date.fromisoformat, default=None,
                        help="Override MIN(tabled_date) (YYYY-MM-DD) instead of reading it from the DB.")
    parser.add_argument("--chunk-days", type=int, default=_CHUNK_DAYS_DEFAULT,
                        help=f"Chunk size in days (default {_CHUNK_DAYS_DEFAULT}).")
    args = parser.parse_args()

    if args.chunk_days < 1:
        parser.error("--chunk-days must be >= 1")

    from flask_app import app
    from extensions import db
    from hansard_archive.models import HaPQ

    started = datetime.utcnow()
    with app.app_context():
        _print_db_host(db)
        if args.dry_run:
            run_dry_run(db, HaPQ, args.floor)
        else:
            run_backfill(db, HaPQ, args.floor, args.chunk_days, test_mode=args.test)
    _log.info("Elapsed: %.1fs", (datetime.utcnow() - started).total_seconds())


if __name__ == "__main__":
    main()
