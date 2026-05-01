"""
Backfill Lords oral question classification for existing sessions.

The structural classifier has no reliable HRS signal for Lords OQs — they land
in 'other' (or occasionally 'committee_stage' for Grand Committee sessions).
This script uses the opening-phrase signal: every Lords OQ begins with
"To ask His Majesty's Government..." — constitutionally mandated phrasing.

Reclassifies matching sessions from 'other' / 'committee_stage' / 'debate'
to 'oral_questions'. Safe to re-run: already-correct sessions are skipped.

Usage:
    cd c:\\Users\\marky\\hansard_app
    python scripts/backfill_lords_oq_classify.py [--dry-run]
"""

import sys
import argparse

sys.path.insert(0, "c:/Users/marky/hansard_app")
from flask_app import app
from extensions import db
from hansard_archive.models import HansardSession, HansardContribution, DEBATE_TYPE_ORAL_QUESTIONS
from sqlalchemy import func


def run(dry_run: bool = False):
    with app.app_context():
        # Find the first attributed contribution per Lords session
        first_contrib_sub = (
            db.session.query(
                HansardContribution.session_id,
                func.min(HansardContribution.speech_order).label("min_order"),
            )
            .filter(HansardContribution.member_name.isnot(None))
            .group_by(HansardContribution.session_id)
            .subquery()
        )

        matches = (
            db.session.query(HansardContribution, HansardSession)
            .join(
                first_contrib_sub,
                (HansardContribution.session_id == first_contrib_sub.c.session_id)
                & (HansardContribution.speech_order == first_contrib_sub.c.min_order),
            )
            .join(HansardSession, HansardSession.id == HansardContribution.session_id)
            .filter(
                HansardSession.house == "Lords",
                HansardSession.is_container == False,
                HansardSession.debate_type != DEBATE_TYPE_ORAL_QUESTIONS,
                HansardContribution.speech_text.ilike("To ask His Majesty%"),
            )
            .all()
        )

        print(f"Found {len(matches)} Lords sessions to reclassify as oral_questions.")

        from_counts: dict[str, int] = {}
        for contrib, session in matches:
            from_counts[session.debate_type] = from_counts.get(session.debate_type, 0) + 1

        print("Current debate_type breakdown of matches:")
        for dtype, cnt in sorted(from_counts.items(), key=lambda x: -x[1]):
            print(f"  {dtype}: {cnt}")
        print()

        if not dry_run:
            updated = 0
            for contrib, session in matches:
                session.debate_type = DEBATE_TYPE_ORAL_QUESTIONS
                updated += 1
            db.session.commit()
            print(f"Done. Reclassified {updated} sessions to oral_questions.")
        else:
            print(f"(DRY RUN — {len(matches)} sessions would be reclassified)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
