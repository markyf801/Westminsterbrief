"""
One-shot SQL UPDATE pass — Lords classification Fixes 1, 2, 3.

Fix 1: other -> statutory_instrument  (67 sessions: title matches SI regex)
Fix 2: debate -> statutory_instrument  (75 sessions: (Amendment) SIs miscategorised)
Fix 3: committee_stage -> other        (74 sessions: AoB inside Grand Committee)

Run once, then discard. ingestion.py already updated for future sessions.
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_app import app, db
from hansard_archive.models import HansardSession

_MADE_SI_RE = re.compile(r"\b(regulations|orders?|rules)\s+\d{4}\b", re.IGNORECASE)

_PROCEDURAL_TITLE_STARTS = (
    "arrangement of business",
    "business of the house",
    "oaths and affirmations",
    "retirement of a member",
    "retirements of members",
    "lord speaker's statement",
    "standing orders",
    "clerk of the parliaments",
    "leave of absence",
    "deaths of members",
    "message from the king",
    "royal assent",
)


def collect_ids():
    fix1_ids = []  # other -> statutory_instrument
    fix2_ids = []  # debate -> statutory_instrument
    fix3_ids = []  # committee_stage -> other  (AoB in Grand Committee)

    lords_sessions = (
        db.session.query(HansardSession)
        .filter(
            HansardSession.house == "Lords",
            HansardSession.is_container == False,
        )
        .all()
    )

    for s in lords_sessions:
        t = (s.title or "").strip().lower()

        if s.debate_type in ("other", "debate"):
            if _MADE_SI_RE.search(t):
                if s.debate_type == "other":
                    fix1_ids.append(s.id)
                else:
                    fix2_ids.append(s.id)

        if s.debate_type == "committee_stage":
            if any(t.startswith(p) for p in _PROCEDURAL_TITLE_STARTS):
                fix3_ids.append(s.id)

    return fix1_ids, fix2_ids, fix3_ids


def apply_fixes(fix1_ids, fix2_ids, fix3_ids):
    updated = 0

    if fix1_ids:
        db.session.query(HansardSession).filter(
            HansardSession.id.in_(fix1_ids)
        ).update({"debate_type": "statutory_instrument"}, synchronize_session=False)
        updated += len(fix1_ids)
        print(f"  Fix 1: {len(fix1_ids)} other -> statutory_instrument")

    if fix2_ids:
        db.session.query(HansardSession).filter(
            HansardSession.id.in_(fix2_ids)
        ).update({"debate_type": "statutory_instrument"}, synchronize_session=False)
        updated += len(fix2_ids)
        print(f"  Fix 2: {len(fix2_ids)} debate -> statutory_instrument")

    if fix3_ids:
        db.session.query(HansardSession).filter(
            HansardSession.id.in_(fix3_ids)
        ).update({"debate_type": "other"}, synchronize_session=False)
        updated += len(fix3_ids)
        print(f"  Fix 3: {len(fix3_ids)} committee_stage -> other (AoB in GC)")

    db.session.commit()
    return updated


def verify(pre_other, pre_si, pre_cs):
    from sqlalchemy import func

    rows = (
        db.session.query(HansardSession.debate_type, func.count())
        .filter(
            HansardSession.house == "Lords",
            HansardSession.is_container == False,
        )
        .group_by(HansardSession.debate_type)
        .all()
    )
    post = {dt: c for dt, c in rows}

    print("\nPost-fix Lords distribution (non-container):")
    for dt, c in sorted(post.items(), key=lambda x: -x[1]):
        print(f"  {dt:<30} {c:>5}")

    post_si = post.get("statutory_instrument", 0)
    post_other = post.get("other", 0)
    post_cs = post.get("committee_stage", 0)
    total = sum(post.values())

    print(f"\nDelta check:")
    print(f"  statutory_instrument: {pre_si} -> {post_si}  (expected +{len(fix1_ids)+len(fix2_ids)})")
    print(f"  other:                {pre_other} -> {post_other}  (expected -{len(fix1_ids)} from Fix1, +{len(fix3_ids)} from Fix3)")
    print(f"  committee_stage:      {pre_cs} -> {post_cs}  (expected -{len(fix3_ids)})")
    print(f"  other rate:           {post_other/total*100:.1f}%  (was {pre_other/(pre_other+sum(v for k,v in rows if k != 'other' and k in post))*100:.1f}% approx)")


if __name__ == "__main__":
    with app.app_context():
        from sqlalchemy import func

        # Pre-fix snapshot
        pre_rows = (
            db.session.query(HansardSession.debate_type, func.count())
            .filter(
                HansardSession.house == "Lords",
                HansardSession.is_container == False,
            )
            .group_by(HansardSession.debate_type)
            .all()
        )
        pre = {dt: c for dt, c in pre_rows}
        pre_other = pre.get("other", 0)
        pre_si = pre.get("statutory_instrument", 0)
        pre_cs = pre.get("committee_stage", 0)
        pre_total = sum(pre.values())

        print("Pre-fix Lords distribution (non-container):")
        for dt, c in sorted(pre.items(), key=lambda x: -x[1]):
            print(f"  {dt:<30} {c:>5}")
        print(f"  other rate: {pre_other/pre_total*100:.1f}%\n")

        print("Collecting IDs for Fixes 1-3...")
        fix1_ids, fix2_ids, fix3_ids = collect_ids()
        print(f"  Fix 1 candidates: {len(fix1_ids)}")
        print(f"  Fix 2 candidates: {len(fix2_ids)}")
        print(f"  Fix 3 candidates: {len(fix3_ids)}")
        total_affected = len(fix1_ids) + len(fix2_ids) + len(fix3_ids)
        print(f"  Total: {total_affected} sessions\n")

        if total_affected == 0:
            print("Nothing to update — already applied?")
            sys.exit(0)

        print("Applying fixes...")
        apply_fixes(fix1_ids, fix2_ids, fix3_ids)
        print("Done.\n")

        verify(pre_other, pre_si, pre_cs)
