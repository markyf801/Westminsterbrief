"""
Enrich ha_pq asking_member and answering_member names using the Parliament
Members API.

The WQ API returns member IDs as flat fields but name objects as null.
This script resolves all unique asking_mnis_id and answering_mnis_id values
where the corresponding name column is NULL.

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


def resolve_ids(mnis_ids: list[int]) -> dict[int, str]:
    """Resolve a list of member IDs to names via the Members API."""
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
    print(f"  Resolved {len(id_to_name)}/{len(mnis_ids)} names ({failed} not found).")
    return id_to_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch names but do not write to DB")
    args = parser.parse_args()

    with app.app_context():
        # --- Asking member names ---
        asking_rows = (
            db.session.query(HaPQ.asking_mnis_id)
            .filter(HaPQ.asking_mnis_id.isnot(None))
            .filter(HaPQ.asking_member.is_(None))
            .distinct()
            .all()
        )
        asking_ids = [r[0] for r in asking_rows]

        if asking_ids:
            print(f"\n[ASKING] {len(asking_ids)} unique member IDs to enrich.")
            asking_names = resolve_ids(asking_ids)
            if not args.dry_run:
                updated = 0
                for mnis_id, name in asking_names.items():
                    updated += (
                        db.session.query(HaPQ)
                        .filter(HaPQ.asking_mnis_id == mnis_id, HaPQ.asking_member.is_(None))
                        .update({"asking_member": name}, synchronize_session=False)
                    )
                db.session.commit()
                print(f"  Updated {updated} rows with asking_member names.")
        else:
            print("\n[ASKING] No nulls — all asking_member values already populated.")

        # --- Answering member names ---
        answering_rows = (
            db.session.query(HaPQ.answering_mnis_id)
            .filter(HaPQ.answering_mnis_id.isnot(None))
            .filter(HaPQ.answering_member.is_(None))
            .distinct()
            .all()
        )
        answering_ids = [r[0] for r in answering_rows]

        if answering_ids:
            print(f"\n[ANSWERING] {len(answering_ids)} unique member IDs to enrich.")
            answering_names = resolve_ids(answering_ids)
            if not args.dry_run:
                updated = 0
                for mnis_id, name in answering_names.items():
                    updated += (
                        db.session.query(HaPQ)
                        .filter(HaPQ.answering_mnis_id == mnis_id, HaPQ.answering_member.is_(None))
                        .update({"answering_member": name}, synchronize_session=False)
                    )
                db.session.commit()
                print(f"  Updated {updated} rows with answering_member names.")
        else:
            print("\n[ANSWERING] No nulls with a populated answering_mnis_id — "
                  "run ingest_pq_cron to populate IDs first.")

        if args.dry_run:
            print("\nDry run — no DB writes.")


if __name__ == "__main__":
    main()
