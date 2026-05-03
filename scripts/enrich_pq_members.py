"""
Enrich ha_pq asking_member names using the Parliament Members API.

The WQ API returns askingMemberId as a flat field but askingMember.name
as null. This script fetches names from the Members API for all unique
asking_mnis_id values where asking_member is currently NULL.

Usage:
    python scripts/enrich_pq_members.py [--dry-run]

Requires DATABASE_URL set to the Railway Postgres URL.
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

MEMBERS_API = "https://members-api.parliament.uk/api/Members/{}"
_REQUEST_TIMEOUT = 15
_INTER_REQUEST_DELAY = 0.2   # 200ms between requests — Members API is lightweight


def fetch_member_name(mnis_id: int) -> str | None:
    """Return displayAs name for a member ID, or None on error."""
    url = MEMBERS_API.format(mnis_id)
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        value = data.get("value") or {}
        return (value.get("nameDisplayAs") or "").strip() or None
    except Exception as exc:
        print(f"  [WARN] Members API error for id={mnis_id}: {exc}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch names but do not write to DB")
    args = parser.parse_args()

    with app.app_context():
        # Find all unique asking_mnis_id values where name is missing
        rows = (
            db.session.query(HaPQ.asking_mnis_id)
            .filter(HaPQ.asking_mnis_id.isnot(None))
            .filter(HaPQ.asking_member.is_(None))
            .distinct()
            .all()
        )
        mnis_ids = [r[0] for r in rows]

        if not mnis_ids:
            print("No nulls to enrich — all asking_member values are populated.")
            return

        print(f"Found {len(mnis_ids)} unique member IDs to enrich.")

        id_to_name: dict[int, str] = {}
        failed = 0

        for i, mnis_id in enumerate(mnis_ids, 1):
            name = fetch_member_name(mnis_id)
            if name:
                id_to_name[mnis_id] = name
                print(f"  [{i}/{len(mnis_ids)}] {mnis_id} → {name}")
            else:
                failed += 1
                print(f"  [{i}/{len(mnis_ids)}] {mnis_id} → NOT FOUND")
            time.sleep(_INTER_REQUEST_DELAY)

        print(f"\nResolved {len(id_to_name)}/{len(mnis_ids)} names ({failed} not found).")

        if args.dry_run:
            print("Dry run — no DB writes.")
            return

        # Batch-update in groups of 500
        updated = 0
        for mnis_id, name in id_to_name.items():
            count = (
                db.session.query(HaPQ)
                .filter(HaPQ.asking_mnis_id == mnis_id, HaPQ.asking_member.is_(None))
                .update({"asking_member": name}, synchronize_session=False)
            )
            updated += count

        db.session.commit()
        print(f"Updated {updated} rows with asking_member names.")


if __name__ == "__main__":
    main()
