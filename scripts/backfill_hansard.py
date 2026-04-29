"""
Hansard Archive backfill script — Phase 2A Week 1.

Ingests Hansard sessions for the last N days (Commons only by default).
Safe to re-run: sessions already in the DB are skipped.

Usage:
  python scripts/backfill_hansard.py              # last 90 days (default)
  python scripts/backfill_hansard.py --days 30    # last 30 days
  python scripts/backfill_hansard.py --days 1     # yesterday only (useful for testing)
  python scripts/backfill_hansard.py --probe      # test overview endpoint on one recent date

Environment:
  DATABASE_URL — if not set, defaults to local SQLite (intelligence.db)
  No other env vars required for ingestion (Hansard API is public).
"""

import argparse
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")  # run from project root

from flask_app import app
from hansard_archive.ingestion import ingest_date, ingest_date_range
from hansard_archive.models import HansardSession


def probe_overview_endpoint(test_date: date) -> None:
    """
    Test whether the Hansard overview endpoint returns usable data for a known sitting day.
    Prints the raw API response to help diagnose any issues.
    """
    import requests

    url = f"https://hansard-api.parliament.uk/overview/Commons/{test_date.isoformat()}.json"
    print(f"[probe] GET {url}")
    try:
        resp = requests.get(url, timeout=15)
        print(f"[probe] Status: {resp.status_code}")
        print(f"[probe] Content-Type: {resp.headers.get('content-type', 'unknown')}")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                print(f"[probe] Response is a list with {len(data)} items")
                if data:
                    print(f"[probe] First item keys: {list(data[0].keys())}")
                    print(f"[probe] First item: {data[0]}")
            elif isinstance(data, dict):
                print(f"[probe] Response is a dict with keys: {list(data.keys())}")
                for k, v in data.items():
                    if isinstance(v, list):
                        print(f"[probe]   {k!r}: list of {len(v)} items")
                        if v:
                            print(f"[probe]   first item keys: {list(v[0].keys()) if isinstance(v[0], dict) else type(v[0])}")
                    else:
                        print(f"[probe]   {k!r}: {v!r}")
            else:
                print(f"[probe] Unexpected response type: {type(data)}")
        else:
            print(f"[probe] Response body: {resp.text[:500]}")
    except Exception as e:
        print(f"[probe] ERROR: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Hansard archive")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days to backfill (default: 90)",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Test the overview API endpoint on a recent known sitting day and exit",
    )
    args = parser.parse_args()

    # --probe: test the overview endpoint before committing to a full backfill
    if args.probe:
        # Use a recent Monday as the test date — reliably a sitting day
        today = date.today()
        days_since_monday = today.weekday()
        last_monday = today - timedelta(days=days_since_monday + 7)
        print(f"[probe] Testing overview endpoint for {last_monday} (last Monday)")
        probe_overview_endpoint(last_monday)
        return

    # Normal backfill
    end_date = date.today() - timedelta(days=1)    # yesterday
    start_date = end_date - timedelta(days=args.days - 1)

    print(f"[backfill] Commons sessions from {start_date} to {end_date} ({args.days} days)")
    print(f"[backfill] Sessions already in DB will be skipped")
    print()

    with app.app_context():
        before_count = HansardSession.query.count()
        summary = ingest_date_range(start_date, end_date, house="Commons", verbose=True)
        after_count = HansardSession.query.count()

    print()
    print("=" * 60)
    print(f"[backfill] Done.")
    print(f"[backfill]   Date range:    {summary['total_days']} days checked")
    print(f"[backfill]   Sitting days:  {summary['sitting_days']}")
    print(f"[backfill]   New sessions:  {summary['total_sessions']}")
    print(f"[backfill]   Errors:        {summary['errors']}")
    print(f"[backfill]   DB total now:  {after_count} sessions ({after_count - before_count} added)")
    print("=" * 60)


if __name__ == "__main__":
    main()
