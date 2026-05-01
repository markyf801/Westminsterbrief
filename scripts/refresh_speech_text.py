"""
Targeted re-ingestion: refresh speech_text for existing sessions.

Fetches session JSON from the Hansard API and updates speech_text for all
contributions in the target window. Safe to re-run — uses UPDATE, not INSERT.

Purpose: recover paragraph breaks for contributions ingested before the
_clean_html() fix (which now preserves <p> and <br> as newlines).

Usage:
    python scripts/refresh_speech_text.py --from 2025-01-01 --to 2025-04-30
    python scripts/refresh_speech_text.py --from 2025-01-01 --to 2025-04-30 --house Lords
    python scripts/refresh_speech_text.py --dry-run --from 2025-01-01 --to 2025-01-07

The script processes sessions in date order. Sessions with no API response are
skipped and logged. Progress is printed continuously so you can interrupt
(Ctrl-C) safely — already-updated contributions are committed per session.
"""

import argparse
import re
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from datetime import date, timedelta

from flask_app import app, db
from hansard_archive.models import HansardContribution, HansardSession
from hansard_archive.ingestion import _clean_html, _flatten_items

HANSARD_API_BASE = "https://hansard-api.parliament.uk"
_DELAY = 0.4  # seconds between requests


def _fetch_raw(ext_id: str) -> dict:
    url = f"{HANSARD_API_BASE}/debates/debate/{ext_id}.json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    SKIP {ext_id[:24]}… API error: {e}", flush=True)
        return {}


def _refresh_session(session: HansardSession, dry_run: bool) -> tuple[int, int]:
    """
    Re-fetch and update speech_text for one session.
    Returns (updated_count, skipped_count).

    Matches by position (index order) rather than speech_order, because the
    existing DB has speech_order values that include slots taken by null-name
    structural items (now deleted), while fresh _flatten_items() produces
    contiguous 0-based orders for named contributions only.
    """
    data = _fetch_raw(session.ext_id)
    if not data:
        return 0, 0

    # Fresh items: only named contributions (null-name items filtered by _flatten_items)
    fresh = [c for c in _flatten_items(data, [0]) if c.get("member_name")]
    if not fresh:
        return 0, 0

    existing = (
        db.session.query(HansardContribution)
        .filter_by(session_id=session.id)
        .order_by(HansardContribution.speech_order)
        .all()
    )

    if len(fresh) != len(existing):
        # Count mismatch — log but still update what we can (positional)
        pass

    updated = skipped = 0
    for i, contrib in enumerate(existing):
        if i >= len(fresh):
            skipped += 1
            continue
        new_text = fresh[i]["speech_text"]
        if contrib.speech_text == new_text:
            skipped += 1
            continue
        if not dry_run:
            contrib.speech_text = new_text
        updated += 1

    if updated and not dry_run:
        db.session.commit()

    return updated, skipped


def main():
    parser = argparse.ArgumentParser(description="Refresh speech_text from Hansard API")
    parser.add_argument("--from", dest="date_from", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--house", default="Commons", choices=["Commons", "Lords"])
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, don't write")
    args = parser.parse_args()

    start = date.fromisoformat(args.date_from)
    end   = date.fromisoformat(args.date_to)

    with app.app_context():
        sessions = (
            HansardSession.query
            .filter(
                HansardSession.house == args.house,
                HansardSession.is_container == False,
                HansardSession.contributions_ingested == True,
                HansardSession.date >= start,
                HansardSession.date <= end,
            )
            .order_by(HansardSession.date)
            .all()
        )

        print(
            f"{'DRY RUN - ' if args.dry_run else ''}"
            f"Refreshing {len(sessions)} sessions "
            f"({args.house}, {start} to {end})",
            flush=True,
        )

        total_updated = total_skipped = 0
        for i, session in enumerate(sessions, 1):
            print(
                f"  [{i}/{len(sessions)}] {session.date} {session.title[:60]!r}",
                end=" ... ",
                flush=True,
            )
            updated, skipped = _refresh_session(session, dry_run=args.dry_run)
            total_updated += updated
            total_skipped += skipped
            print(f"updated={updated} skipped={skipped}", flush=True)
            time.sleep(_DELAY)

        print(f"\nDone. Updated={total_updated} Skipped={total_skipped}")
        if args.dry_run:
            print("(dry run — no changes written)")


if __name__ == "__main__":
    main()
