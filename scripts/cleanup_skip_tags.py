"""
Verification + cleanup pass for structural contributions that should have been
excluded by _SKIP_HRS_TAGS / _SKIP_ITEM_TYPES.

Background: HansardContribution rows don't store the HRS tag (that's on
HansardSession). Structural items (err_tablewrapper, hs_clclerks,
hs_amendmentlevel*) are all unattributed, so they land in the DB with
member_name IS NULL. Fix C (April 2026) already deleted these in bulk.

This script:
  1. Confirms null-name count is 0 (i.e. Fix C covered everything including
     the newly-classified err_tablewrapper / hs_amendmentlevel tags)
  2. If any null-name rows remain (shouldn't happen), deletes them
  3. Reports before/after counts

Usage:
    python scripts/cleanup_skip_tags.py --dry-run   # show counts only
    python scripts/cleanup_skip_tags.py             # apply if any found
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_app import app, db
from hansard_archive.models import HansardContribution
from sqlalchemy import func


def main(dry_run: bool) -> None:
    with app.app_context():
        print(f"Mode: {'DRY RUN' if dry_run else 'LIVE DELETE'}", flush=True)

        total_before = db.session.query(func.count(HansardContribution.id)).scalar()
        null_name = (
            db.session.query(func.count(HansardContribution.id))
            .filter(HansardContribution.member_name.is_(None))
            .scalar()
        )

        print(f"Total contributions:       {total_before:,}", flush=True)
        print(f"Null-name (structural):    {null_name:,}", flush=True)

        if null_name == 0:
            print("Clean — no structural rows remain. Fix C already covered err_tablewrapper, "
                  "hs_amendmentlevel*, and hs_clclerks.", flush=True)
            return

        print(f"Found {null_name:,} null-name contributions to remove.", flush=True)
        if not dry_run:
            db.session.query(HansardContribution).filter(
                HansardContribution.member_name.is_(None)
            ).delete(synchronize_session=False)
            db.session.commit()
            total_after = db.session.query(func.count(HansardContribution.id)).scalar()
            print(f"After delete: {total_after:,} (removed {total_before - total_after:,})", flush=True)
        else:
            print("(dry run — no changes committed)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
