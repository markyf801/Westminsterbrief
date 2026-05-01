"""
Backfill HansardSession.department for all existing oral questions sessions.

For each hs_6bDepartment container in the DB, fetches its full JSON from the
Hansard API, extracts ChildDebates[n].Overview.ExtId for each child, and sets
department = container.title on matching sessions.

Safe to re-run: sessions already attributed are skipped.

Usage:
    cd c:\\Users\\marky\\hansard_app
    python scripts/backfill_departments.py [--dry-run]
"""

import sys
import time
import argparse
import requests

# Flask app context
sys.path.insert(0, "c:/Users/marky/hansard_app")
from flask_app import app
from extensions import db
from hansard_archive.models import HansardSession

HANSARD_API_BASE = "https://hansard-api.parliament.uk"
_REQUEST_TIMEOUT = 15
_INTER_REQUEST_DELAY = 0.4


def _fetch_child_ext_ids(container_ext_id: str) -> list[str]:
    """Fetch the container's full JSON and extract child session ExtIds."""
    url = f"{HANSARD_API_BASE}/debates/debate/{container_ext_id}.json"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ERROR fetching {container_ext_id[:20]}...: {e}")
        return []

    data = resp.json()
    child_ids = []
    for child in data.get("ChildDebates", []):
        child_overview = child.get("Overview") or {}
        child_id = (
            child_overview.get("ExtId")
            or child.get("ExternalId")
            or child.get("DebateSectionExtId")
            or ""
        )
        if child_id:
            child_ids.append(str(child_id))
    return child_ids


def run(dry_run: bool = False):
    with app.app_context():
        containers = (
            HansardSession.query
            .filter(HansardSession.hrs_tag.ilike("hs_6bdepartment"))
            .order_by(HansardSession.date, HansardSession.id)
            .all()
        )
        print(f"Found {len(containers)} hs_6bDepartment containers to process.")

        total_attributed = 0
        total_skipped = 0
        total_not_found = 0

        for i, container in enumerate(containers, 1):
            dept_name = container.title
            print(f"[{i}/{len(containers)}] {container.date} | {dept_name}", end=" ... ", flush=True)

            child_ext_ids = _fetch_child_ext_ids(container.ext_id)
            time.sleep(_INTER_REQUEST_DELAY)

            if not child_ext_ids:
                print("no children")
                continue

            attributed = 0
            skipped = 0
            not_found = 0

            for child_id in child_ext_ids:
                session = HansardSession.query.filter_by(
                    ext_id=child_id, is_container=False
                ).first()
                if session is None:
                    not_found += 1
                    continue
                if session.department:
                    skipped += 1
                    continue
                if not dry_run:
                    session.department = dept_name
                attributed += 1

            if not dry_run:
                db.session.commit()

            total_attributed += attributed
            total_skipped += skipped
            total_not_found += not_found
            print(f"attributed={attributed} skipped={skipped} not_found={not_found}")

        print()
        print(f"Done. Total attributed: {total_attributed}, already set: {total_skipped}, "
              f"not in DB: {total_not_found}")
        if dry_run:
            print("(DRY RUN — no changes committed)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would be set without writing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
