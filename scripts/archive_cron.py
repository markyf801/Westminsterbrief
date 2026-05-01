"""
Hansard Archive incremental ingestion — cron entry point.

Ingest the last N days for both Commons and Lords. Designed to run hourly
during sitting-day windows so new Hansard content is picked up within ~1 hour
of publication. Safe to re-run: sessions already in the DB are skipped.

Usage:
  python scripts/archive_cron.py              # default: last 3 days
  python scripts/archive_cron.py --days 1     # yesterday only
  python scripts/archive_cron.py --days 7     # wider catch-up

Environment:
  DATABASE_URL — if not set, uses local SQLite (intelligence.db)
"""

import sys
import argparse
from datetime import date, timedelta

sys.path.insert(0, ".")

from flask_app import app
from hansard_archive.ingestion import ingest_date_range


def main() -> None:
    parser = argparse.ArgumentParser(description="Hansard Archive incremental cron")
    parser.add_argument("--days", type=int, default=3,
                        help="Lookback window in days (default: 3)")
    args = parser.parse_args()

    today = date.today()
    start = today - timedelta(days=args.days - 1)

    print(f"[cron] Hansard incremental ingest: {start} → {today} ({args.days} days)", flush=True)

    with app.app_context():
        for house in ("Commons", "Lords"):
            print(f"[cron] === {house} ===", flush=True)
            result = ingest_date_range(start, today, house=house, verbose=True)
            print(
                f"[cron] {house} done — "
                f"{result['total_sessions']} new sessions across "
                f"{result['sitting_days']} sitting days "
                f"({result['errors']} errors)",
                flush=True,
            )

    print("[cron] Complete.", flush=True)


if __name__ == "__main__":
    main()
