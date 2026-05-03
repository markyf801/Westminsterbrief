"""
Backfill full answer text for PQs where the bulk ingestor stored truncated text.

The Parliament WQ list endpoint truncates answerText at ~258 chars. This script
finds all answered PQs with short answer text, re-fetches each via the individual
endpoint, and updates the stored text.

Usage:
    python scripts/backfill_pq_full_answers.py [--dry-run] [--limit N]

Requires DATABASE_URL set to the Railway Postgres URL.
Estimated runtime: ~90k answered PQs × 200ms = ~5 hours for a full pass.
Use --limit to run in batches (safe to re-run — already-long answers are skipped).
"""

import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("FLASK_ENV", "production")

from flask_app import app
from extensions import db
from hansard_archive.models import HaPQ

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_REQUEST_TIMEOUT = 60
_INTER_REQUEST_DELAY = 0.25
_TRUNC_THRESHOLD = 300   # answers under this length were likely truncated by bulk endpoint
_COMMIT_EVERY = 500


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html).strip()


def _clean_ws(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", text).strip()


def fetch_full_answer(uin: str) -> str | None:
    """Fetch full answer for a given UIN via list endpoint then individual endpoint."""
    try:
        # Step 1: get internal API ID via UIN search
        resp = requests.get(WQ_API_BASE, params={"uin": uin}, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        val = results[0].get("value") or results[0]
        api_id = val.get("id")
        if not api_id:
            return None

        # Step 2: fetch individual endpoint for full text
        resp2 = requests.get(f"{WQ_API_BASE}/{api_id}", timeout=_REQUEST_TIMEOUT)
        resp2.raise_for_status()
        val2 = resp2.json().get("value") or {}
        raw = val2.get("answerText") or ""
        return _clean_ws(_strip_html(raw)) or None
    except Exception as exc:
        print(f"  [WARN] fetch failed for uin={uin}: {exc}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows to process (0 = all)")
    args = parser.parse_args()

    with app.app_context():
        query = (
            db.session.query(HaPQ)
            .filter(HaPQ.is_answered == True)
            .filter(db.func.length(HaPQ.answer_text) < _TRUNC_THRESHOLD)
            .order_by(HaPQ.id)
        )
        if args.limit:
            query = query.limit(args.limit)

        rows = query.all()
        print(f"Found {len(rows)} answered PQs with answer_text under {_TRUNC_THRESHOLD} chars.")

        if not rows:
            print("Nothing to do.")
            return

        updated = skipped = failed = 0
        pending = 0

        for i, pq in enumerate(rows, 1):
            full = fetch_full_answer(pq.uin)

            if full and len(full) > len(pq.answer_text or ""):
                print(f"  [{i}/{len(rows)}] {pq.uin}: {len(pq.answer_text or '')} → {len(full)} chars")
                if not args.dry_run:
                    pq.answer_text = full
                    pending += 1
                updated += 1
            elif full:
                skipped += 1  # full text not actually longer — already complete
            else:
                failed += 1
                print(f"  [{i}/{len(rows)}] {pq.uin}: fetch failed")

            if not args.dry_run and pending >= _COMMIT_EVERY:
                db.session.commit()
                print(f"  Committed {pending} rows (running total: {updated})")
                pending = 0

            time.sleep(_INTER_REQUEST_DELAY)

        if not args.dry_run and pending:
            db.session.commit()

        print(f"\nDone. Updated={updated} skipped={skipped} failed={failed}")
        if args.dry_run:
            print("(dry run — no writes)")


if __name__ == "__main__":
    main()
