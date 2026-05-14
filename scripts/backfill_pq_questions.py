"""
Stage C backfill: refresh question_text from Parliament individual endpoint.

The Parliament bulk WQ endpoint truncates questionText (and answerText).
Stage B fixed answer_text; Stage C fixes question_text for all existing rows.
Both fields are updated from the same individual endpoint call.

Run after Stage B completes.

Usage:
  python scripts/backfill_pq_questions.py          # full run
  python scripts/backfill_pq_questions.py --test   # first 100 rows only

Resume behaviour: on interrupt (SIGKILL, power loss), restart and the script
resumes from a checkpoint file (backfill_pq_questions.checkpoint in scripts/).
On clean completion the checkpoint file is removed.

Rate: 0.5 req/sec — same as Stage B.
Estimated time: ~82k rows × 0.5s ≈ 11.5 hours.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from hansard_archive.html_sanitizer import sanitize_answer_html

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [stage-c] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger(__name__)

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_REQUEST_TIMEOUT = 60
_BACKFILL_DELAY = 0.5
_BATCH_SIZE = 100
_PROGRESS_LOG_EVERY = 200

_CHECKPOINT = os.path.join(os.path.dirname(__file__), "backfill_pq_questions.checkpoint")


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _clean(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", text).strip()


def _get_full_record(api_id: int):
    """Fetch individual endpoint. Returns (question_text, answer_text, is_holding, is_withdrawn)."""
    for attempt in range(3):
        try:
            resp = requests.get(f"{WQ_API_BASE}/{api_id}", timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                _log.warning("429 rate limit — waiting %ds.", wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                _log.warning("Individual fetch id=%s returned %s", api_id, resp.status_code)
                return None, None, False, False
            v = resp.json().get("value") or {}
            q_text = _clean(_strip_html(v.get("questionText") or "")) or None
            a_html = sanitize_answer_html(v.get("answerText") or "")
            return q_text, a_html, bool(v.get("answerIsHolding")), bool(v.get("isWithdrawn"))
        except Exception as exc:
            _log.warning("Fetch id=%s error (attempt %d): %s", api_id, attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, None, False, False


def _read_checkpoint() -> int:
    if os.path.exists(_CHECKPOINT):
        try:
            with open(_CHECKPOINT) as f:
                val = int(f.read().strip())
            _log.info("Resuming from checkpoint: last_id=%d", val)
            return val
        except (ValueError, OSError):
            pass
    return 0


def _write_checkpoint(last_id: int):
    try:
        with open(_CHECKPOINT, "w") as f:
            f.write(str(last_id))
    except OSError as exc:
        _log.warning("Could not write checkpoint: %s", exc)


def _clear_checkpoint():
    try:
        if os.path.exists(_CHECKPOINT):
            os.unlink(_CHECKPOINT)
    except OSError:
        pass


def stage_c(db, HaPQ, test_mode: bool = False):
    # Called from within app.app_context() in main().
    _log.info("=== Stage C: refreshing question_text (and answer_text) ===")
    if test_mode:
        _log.info("TEST MODE: processing first 100 rows only.")

    total = (db.session.query(func.count(HaPQ.id))
             .filter(HaPQ.api_id != None)
             .scalar())
    _log.info("Rows with api_id: %d", total)
    if total == 0:
        _log.info("Nothing to do.")
        return

    _log.info("Estimated time at %.1fs/req: ~%.1f hours", _BACKFILL_DELAY,
              total * _BACKFILL_DELAY / 3600)

    last_id = 0 if test_mode else _read_checkpoint()
    updated_q = updated_a = errors = 0
    processed = 0

    while True:
        if test_mode and processed >= 100:
            break

        fetch_limit = min(_BATCH_SIZE, 100 - processed) if test_mode else _BATCH_SIZE

        try:
            batch = (db.session.query(HaPQ)
                     .filter(HaPQ.api_id != None, HaPQ.id > last_id)
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
            api_id = row.api_id
            old_q_len = len(row.question_text or "")
            old_a_len = len(row.answer_text or "")
            last_id = row.id

            q_text, a_html, is_holding, is_withdrawn = _get_full_record(api_id)

            if q_text and len(q_text) != old_q_len:
                row.question_text = q_text
                updated_q += 1

            # Always update answer_text with sanitised HTML regardless of
            # length change — migrating from flat plain text to HTML storage.
            if a_html:
                row.answer_text = a_html
                updated_a += 1

            # Always update these flags from the individual endpoint
            row.is_holding = is_holding
            row.is_withdrawn = is_withdrawn
            row.updated_at = datetime.utcnow()

            if not q_text and not a_html:
                errors += 1

            processed += 1
            time.sleep(_BACKFILL_DELAY)

            if processed % _PROGRESS_LOG_EVERY == 0:
                pct = processed / total * 100
                eta_s = (total - processed) * _BACKFILL_DELAY
                _log.info(
                    "Progress: %d/%d (%.1f%%) — q_updated=%d a_updated=%d errors=%d — ETA %.1f min",
                    processed, total, pct, updated_q, updated_a, errors, eta_s / 60,
                )

        try:
            db.session.commit()
            if not test_mode:
                _write_checkpoint(last_id)
        except OperationalError as exc:
            _log.warning("Connection error during commit: %s — reconnecting and retrying.", exc)
            db.session.rollback()
            db.engine.dispose()
            try:
                db.session.commit()
                if not test_mode:
                    _write_checkpoint(last_id)
            except Exception as exc2:
                _log.error("Retry commit failed: %s — rows in this batch may be lost.", exc2)
                db.session.rollback()

        if test_mode and processed >= 100:
            break

    _log.info(
        "Stage C complete. processed=%d q_updated=%d a_updated=%d errors=%d",
        processed, updated_q, updated_a, errors,
    )

    if not test_mode:
        _clear_checkpoint()

    if test_mode:
        samples = (db.session.query(HaPQ.uin, HaPQ.question_text, HaPQ.answer_text)
                   .filter(HaPQ.api_id != None)
                   .limit(3).all())
        _log.info("Sample rows:")
        for uin, q, a in samples:
            _log.info("  UIN=%s  q_len=%d  a_len=%d", uin, len(q or ""), len(a or ""))


def main():
    parser = argparse.ArgumentParser(description="Stage C: refresh question_text from individual endpoint.")
    parser.add_argument("--test", action="store_true", help="Process first 100 rows only")
    args = parser.parse_args()

    from flask_app import app
    from extensions import db
    from hansard_archive.models import HaPQ

    with app.app_context():
        stage_c(db, HaPQ, test_mode=args.test)


if __name__ == "__main__":
    main()
