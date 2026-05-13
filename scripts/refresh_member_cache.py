"""
Bulk-refresh CachedMember from the Parliament Members API.

Fetches all current MPs and Lords, upserts each into the cached_member table.
Designed to run once a week; searches then always resolve member details from
the DB rather than making per-request API calls.

Usage:
    python scripts/refresh_member_cache.py

Railway cron: member-cache-refresh
    Schedule:   0 2 * * 0   (Sunday 02:00 UTC / 02:00–03:00 BST)
    Start cmd:  python scripts/refresh_member_cache.py

Environment:
    DATABASE_URL  — Postgres (Railway Variable Reference); falls back to SQLite
"""

import os
import sys
import time
import requests

sys.path.insert(0, ".")

from flask_app import app
from cache_models import CachedMember
from extensions import db

_MEMBERS_API = "https://members-api.parliament.uk/api/Members/Search"
_INTER_REQUEST_DELAY = 0.2  # seconds — be polite to the API
# The Members API hard-caps at 20 results per page regardless of take param.
_PAGE_SIZE = 20


def _fetch_and_store_all(verbose: bool = True) -> dict:
    inserted = updated = errors = 0
    skip = 0
    total_results = None

    if verbose:
        print("[refresh] Fetching all current members from Parliament Members API...", flush=True)

    while True:
        try:
            resp = requests.get(_MEMBERS_API, params={
                "IsCurrentMember": "true",
                "take": _PAGE_SIZE,
                "skip": skip,
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"[refresh] API error at skip={skip}: {exc}", flush=True)
            errors += 1
            break

        if total_results is None:
            total_results = data.get("totalResults", 0)
            if verbose:
                print(f"[refresh] totalResults={total_results}", flush=True)

        items = data.get("items") or []
        if not items:
            break

        batch_count = 0
        for item in items:
            v = item.get("value") or {}
            member_id = v.get("id")
            if not member_id:
                continue

            name = v.get("nameDisplayAs") or "Unknown"
            party = (v.get("latestParty") or {}).get("name") or "No Party"
            membership = v.get("latestHouseMembership") or {}
            constituency = membership.get("membershipFrom") or ""
            house = "Lords" if membership.get("house") == 2 else "Commons"
            image_url = v.get("thumbnailUrl") or ""

            existing = CachedMember.query.filter_by(member_id=int(member_id)).first()
            if existing:
                updated += 1
            else:
                inserted += 1

            CachedMember.store(member_id, name, party, constituency, house, image_url)
            batch_count += 1

        skip += len(items)
        if verbose and skip % 200 == 0:
            print(f"[refresh] {skip}/{total_results} members processed...", flush=True)

        if skip >= (total_results or 0):
            break

        time.sleep(_INTER_REQUEST_DELAY)

    return {"inserted": inserted, "updated": updated, "errors": errors}


def main() -> None:
    with app.app_context():
        stats = _fetch_and_store_all()
        total = stats["inserted"] + stats["updated"]
        print(
            f"[refresh] Done. inserted={stats['inserted']} updated={stats['updated']} "
            f"errors={stats['errors']} total={total}",
            flush=True,
        )
        if stats["errors"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
