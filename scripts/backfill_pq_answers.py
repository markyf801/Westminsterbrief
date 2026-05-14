"""
Backfill full answer text for Written Questions where the bulk ingestor
stored truncated text (Parliament bulk endpoint truncates answerText at 258 chars).

Two-stage process:
  Stage A — populate api_id on all rows that are missing it by re-fetching
             the bulk endpoint for the full archive window. Fast (~30 min).
  Stage B — fetch the individual endpoint for each answered row where
             answer_text is exactly 258 chars, and update with full text.
             Also sets is_holding and is_withdrawn from the individual record.

Usage:
  python scripts/backfill_pq_answers.py          # full run (both stages)
  python scripts/backfill_pq_answers.py --stage a # populate api_id only
  python scripts/backfill_pq_answers.py --stage b # answer backfill only
  python scripts/backfill_pq_answers.py --test    # Stage B on first 100 rows

The script is naturally resumable: Stage B's query targets rows WHERE
length(answer_text) = 258. Once a row is updated to full text it falls out
of the query automatically, so restarting after interruption is safe.

Required env vars (same as flask_app):
  DATABASE_URL (or local SQLite used automatically if unset)
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

from sqlalchemy.exc import OperationalError

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv
from sqlalchemy import func

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger(__name__)

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"

_PAGE_SIZE = 500
_PROGRESS_LOG_EVERY = 200
_REQUEST_TIMEOUT = 60

# Conservative rate for backfill — 0.5s between individual fetches = 2 req/sec.
# Increase to 0.3 (3.3 req/sec) if no 429s seen after first 1000 rows.
_BACKFILL_DELAY = 0.5
_BULK_DELAY = 0.3         # bulk endpoint pagination — same as normal ingest


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _clean(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", text).strip()


def _get_full_answer(api_id: int) -> tuple[str | None, bool, bool]:
    """Fetch individual endpoint. Returns (full_text, is_holding, is_withdrawn)."""
    for attempt in range(3):
        try:
            resp = requests.get(f"{WQ_API_BASE}/{api_id}", timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                _log.warning("429 rate limit hit. Waiting %ds before retry.", wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                _log.warning("Individual fetch id=%s returned %s", api_id, resp.status_code)
                return None, False, False
            v = resp.json().get("value") or {}
            text = _clean(_strip_html(v.get("answerText") or "")) or None
            return text, bool(v.get("answerIsHolding")), bool(v.get("isWithdrawn"))
        except Exception as exc:
            _log.warning("Individual fetch id=%s error (attempt %d): %s", api_id, attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, False, False


# ---------------------------------------------------------------------------
# Stage A — populate api_id via bulk re-pass
# ---------------------------------------------------------------------------

def stage_a(db, HaPQ, months: int = 13):
    # Called from within app.app_context() in main() — do not create a nested context.
    _log.info("=== Stage A: populating api_id via bulk re-pass (%d months) ===", months)
    updated = skipped = errors = 0

    end_date = date.today()
    start_date = end_date - timedelta(days=months * 31)

    # Walk in 30-day chunks to keep page counts manageable
    chunk_start = start_date
    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=29), end_date)
        skip = 0
        while True:
            try:
                resp = requests.get(WQ_API_BASE, params={
                    "tabledWhenFrom": chunk_start.isoformat(),
                    "tabledWhenTo": chunk_end.isoformat(),
                    "take": _PAGE_SIZE,
                    "skip": skip,
                }, timeout=_REQUEST_TIMEOUT)
                resp.raise_for_status()
            except Exception as exc:
                _log.error("Bulk fetch error %s-%s skip=%d: %s", chunk_start, chunk_end, skip, exc)
                errors += 1
                break

            results = resp.json().get("results") or []
            if not results:
                break

            for item in results:
                v = item.get("value", item)
                uin = (v.get("uin") or "").strip()
                api_id = v.get("id")
                if not uin or not api_id:
                    continue
                row = db.session.query(HaPQ).filter_by(uin=uin).first()
                if row and not row.api_id:
                    row.api_id = api_id
                    updated += 1
                else:
                    skipped += 1

            if len(results) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE
            time.sleep(_BULK_DELAY)

        chunk_start = chunk_end + timedelta(days=1)
        db.session.commit()
        _log.info("  chunk up to %s done — api_id updated=%d skipped=%d errors=%d",
                  chunk_end, updated, skipped, errors)

    db.session.commit()
    total_with_id = db.session.query(HaPQ).filter(HaPQ.api_id != None).count()
    total_without = db.session.query(HaPQ).filter(HaPQ.api_id == None).count()
    _log.info("Stage A complete. api_id populated=%d, still null=%d", total_with_id, total_without)


# ---------------------------------------------------------------------------
# Stage B — fetch full answer text via individual endpoint
# ---------------------------------------------------------------------------

_BATCH_SIZE = 100  # rows per DB round-trip — commit after each batch, no stale objects


def stage_b(db, HaPQ, test_mode: bool = False):
    # Called from within app.app_context() in main() — do not create a nested context.
    #
    # Processes rows in small batches (query → process → commit → next batch).
    # This avoids the Railway connection-timeout issue that arose when the old approach
    # loaded all 68k rows upfront: after each 50-row commit SQLAlchemy expired ALL loaded
    # objects, causing lazy-reload-triggered autoflushes on a dead connection.
    # With batch-by-ID, each batch is freshly loaded, fully committed, then discarded.
    _log.info("=== Stage B: backfilling full answer text ===")
    if test_mode:
        _log.info("TEST MODE: processing first 100 rows only.")

    _BASE_FILTER = (
        HaPQ.is_answered == True,
        HaPQ.api_id != None,
        func.length(HaPQ.answer_text) == 258,
    )

    total = db.session.query(func.count(HaPQ.id)).filter(*_BASE_FILTER).scalar()
    _log.info("Rows to process: %d", total)
    if total == 0:
        _log.info("Nothing to do.")
        return

    _log.info("Estimated time at %.1fs/req: ~%.1f hours", _BACKFILL_DELAY,
              total * _BACKFILL_DELAY / 3600)

    updated = unchanged = errors = 0
    processed = 0
    last_id = 0

    while True:
        if test_mode and processed >= 100:
            break

        fetch_limit = min(_BATCH_SIZE, 100 - processed) if test_mode else _BATCH_SIZE

        try:
            batch = (db.session.query(HaPQ)
                     .filter(*_BASE_FILTER, HaPQ.id > last_id)
                     .order_by(HaPQ.id)
                     .limit(fetch_limit)
                     .all())
        except OperationalError as exc:
            _log.warning("Connection error fetching batch (last_id=%d): %s — reconnecting.", last_id, exc)
            db.session.rollback()
            db.engine.dispose()
            continue

        if not batch:
            break

        for row in batch:
            # Capture attributes before any modification — object is freshly loaded, not expired.
            api_id = row.api_id
            current_len = len(row.answer_text or "")
            last_id = row.id

            full_text, is_holding, is_withdrawn = _get_full_answer(api_id)

            if full_text and len(full_text) > current_len:
                row.answer_text = full_text
                row.is_holding = is_holding
                row.is_withdrawn = is_withdrawn
                row.updated_at = datetime.utcnow()
                updated += 1
            elif full_text:
                row.is_holding = is_holding
                row.is_withdrawn = is_withdrawn
                row.updated_at = datetime.utcnow()
                unchanged += 1
            else:
                errors += 1

            processed += 1
            time.sleep(_BACKFILL_DELAY)

            if processed % _PROGRESS_LOG_EVERY == 0:
                pct = processed / total * 100
                eta_s = (total - processed) * _BACKFILL_DELAY
                _log.info("Progress: %d/%d (%.1f%%) — updated=%d unchanged=%d errors=%d — ETA %.1f min",
                          processed, total, pct, updated, unchanged, errors, eta_s / 60)

        # Commit the whole batch. Objects expire after this — but we never access them again.
        try:
            db.session.commit()
        except OperationalError as exc:
            _log.warning("Connection error during commit: %s — reconnecting and retrying.", exc)
            db.session.rollback()
            db.engine.dispose()
            try:
                db.session.commit()
            except Exception as exc2:
                _log.error("Retry commit also failed: %s — some rows in this batch may be lost.", exc2)
                db.session.rollback()

    _log.info("Stage B complete. updated=%d unchanged=%d errors=%d", updated, unchanged, errors)

    if test_mode:
        samples = (db.session.query(HaPQ.uin, HaPQ.answer_text)
                   .filter(HaPQ.api_id != None, func.length(HaPQ.answer_text) > 258)
                   .limit(3).all())
        _log.info("Sample updated rows:")
        for uin, ans in samples:
            _log.info("  UIN=%s  len=%d  preview: %s", uin, len(ans or ""), (ans or "")[:100])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backfill PQ answer text from Parliament individual endpoint.")
    parser.add_argument("--stage", choices=["a", "b", "both"], default="both",
                        help="Which stage to run (default: both)")
    parser.add_argument("--test", action="store_true",
                        help="Stage B test mode: process first 100 rows only")
    parser.add_argument("--months", type=int, default=13,
                        help="Months of history to cover in Stage A (default: 13)")
    args = parser.parse_args()

    from flask_app import app
    from extensions import db
    from hansard_archive.models import HaPQ

    with app.app_context():
        if args.stage in ("a", "both"):
            stage_a(db, HaPQ, months=args.months)
        if args.stage in ("b", "both"):
            stage_b(db, HaPQ, test_mode=args.test)


if __name__ == "__main__":
    main()
