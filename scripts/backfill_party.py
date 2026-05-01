"""
One-shot backfill: extract party abbreviation from member_name into the party column.

Safe to re-run — only updates rows where party IS NULL.
No API calls needed; all data is derived from the stored member_name string.

Run after deploying the ingestion fix so future sessions are handled automatically.
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_app import app, db
from hansard_archive.models import HansardContribution
from sqlalchemy import func

_NOT_PARTY = frozenset({
    "Maiden Speech", "Valedictory Speech", "Urgent Question", "Maiden",
    "Your Party", "Restore Britain",
})


def _extract_party(member_name: str) -> str | None:
    groups = re.findall(r'\(([^)]+)\)', member_name)
    if len(groups) >= 2:
        candidate = groups[-1].strip()
        if candidate not in _NOT_PARTY:
            return candidate
    return None


def run() -> None:
    with app.app_context():
        rows = (
            db.session.query(HansardContribution)
            .filter(HansardContribution.party.is_(None))
            .filter(HansardContribution.member_name.isnot(None))
            .all()
        )
        print(f"Rows to process: {len(rows)}")

        updated = skipped = 0
        for c in rows:
            party = _extract_party(c.member_name)
            if party:
                c.party = party
                updated += 1
            else:
                skipped += 1

        db.session.commit()
        print(f"Updated: {updated}")
        print(f"Skipped (no parseable party): {skipped}")

        dist = (
            db.session.query(HansardContribution.party, func.count(HansardContribution.id))
            .filter(HansardContribution.party.isnot(None))
            .group_by(HansardContribution.party)
            .order_by(func.count(HansardContribution.id).desc())
            .all()
        )
        print("\nParty distribution after backfill:")
        for party, count in dist:
            print(f"  {count:>6}  {party!r}")


if __name__ == "__main__":
    run()
