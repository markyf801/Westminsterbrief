"""
Refresh ministerial roles for members who have answered Written Questions.

Queries distinct answering_mnis_id values from ha_pq, fetches each member's
Biography from the Parliament Members API, and stores their current government
post in cached_member.ministerial_role.

Only fetches biography for members who appear as answering ministers — typically
50–200 distinct IDs rather than all 1,424 cached members.

Usage:
    python scripts/refresh_minister_roles.py           # all answering members
    python scripts/refresh_minister_roles.py --dry-run # show what would be updated

Run cadence: weekly alongside member-cache-refresh (or on-demand after a reshuffle).

Note: stores the CURRENT government post. If a minister is reshuffled after
answering a question, the stored role reflects their current post, not the one
they held at answer time. Acceptable for Westminster Brief's 12-month archive.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

load_dotenv()

_BIOGRAPHY_URL = "https://members-api.parliament.uk/api/Members/{id}/Biography"
_REQUEST_TIMEOUT = 10
_INTER_REQUEST_DELAY = 0.3


def _fetch_current_govt_post(member_id: int) -> str | None:
    """Return the name of the member's current government post, or None."""
    try:
        resp = requests.get(
            _BIOGRAPHY_URL.format(id=member_id),
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        posts = (resp.json().get("value") or {}).get("governmentPosts") or []
        # Current post has endDate == None; take the most recent if none current
        current = [p for p in posts if p.get("endDate") is None]
        if current:
            return current[0].get("name") or None
        # No current post — member is no longer a minister
        return None
    except Exception:
        return None


def refresh(dry_run: bool = False) -> None:
    from flask_app import app
    from cache_models import CachedMember
    from extensions import db
    from hansard_archive.models import HaPQ
    from sqlalchemy import func

    with app.app_context():
        # Distinct answering member IDs across all PQs
        rows = (db.session.query(HaPQ.answering_mnis_id)
                .filter(HaPQ.answering_mnis_id.isnot(None))
                .distinct()
                .all())
        member_ids = [r[0] for r in rows]
        print(f"[minister-roles] Distinct answering member IDs: {len(member_ids)}", flush=True)

        updated = cleared = skipped = errors = 0

        for member_id in member_ids:
            role = _fetch_current_govt_post(member_id)
            time.sleep(_INTER_REQUEST_DELAY)

            cached = CachedMember.get(member_id)
            if not cached:
                skipped += 1
                continue

            old_role = cached.ministerial_role

            if dry_run:
                if role != old_role:
                    print(f"  {cached.name}: {old_role!r} -> {role!r}", flush=True)
                continue

            if role != old_role:
                cached.ministerial_role = role
                try:
                    db.session.commit()
                    if role:
                        updated += 1
                        print(f"  [set]   {cached.name}: {role}", flush=True)
                    else:
                        cleared += 1
                        print(f"  [clear] {cached.name}: was {old_role!r}", flush=True)
                except Exception as exc:
                    db.session.rollback()
                    errors += 1
                    print(f"  [err]   {cached.name}: {exc}", flush=True)

        if not dry_run:
            print(
                f"[minister-roles] Done. updated={updated} cleared={cleared} "
                f"skipped={skipped} errors={errors}",
                flush=True,
            )
        else:
            print("[minister-roles] Dry run complete — no changes committed.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    refresh(dry_run=args.dry_run)
