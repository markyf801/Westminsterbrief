"""
Scoring formula demonstration: before/after for proposed recency + engagement boost.

Shows how the current date-DESC sort compares to the proposed hybrid scoring
for a set of test queries. Run this before implementing to verify the formula
feels right, then adjust weights if needed.

Usage:
    cd c:/Users/marky/hansard_app
    python scripts/score_demo.py
    python scripts/score_demo.py "student loan" housing "AI"
"""

import sys
import math
from datetime import date

sys.path.insert(0, "c:/Users/marky/hansard_app")
from flask_app import app
from extensions import db
from hansard_archive.models import (
    HansardSession,
    HansardContribution,
    HansardSessionTheme,
    THEME_TYPE_POLICY_AREA,
)
from sqlalchemy import func

TODAY = date.today()

MONTH = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# ---------------------------------------------------------------------------
# Scoring components
# ---------------------------------------------------------------------------

HALF_LIFE_DAYS = 45  # decay constant — tune this


def recency_score(d) -> float:
    """Hyperbolic decay. At HALF_LIFE_DAYS: 0.5. At 0 days: 1.0."""
    age = (TODAY - d).days
    return 1.0 / (1.0 + age / HALF_LIFE_DAYS)


def engagement_score(contrib_count: int) -> float:
    """Log-normalised. 100 contributions → 1.0; 10 → 0.54; 1 → 0.15."""
    return min(1.0, math.log1p(contrib_count) / math.log1p(100))


def crosscut_score(policy_area_count: int) -> float:
    """3+ policy areas → 1.0; 0 → 0.0."""
    return min(1.0, policy_area_count / 3.0)


# Weights — what Mark is approving
WEIGHT_RELEVANCE  = 0.40
WEIGHT_RECENCY    = 0.35
WEIGHT_ENGAGEMENT = 0.15
WEIGHT_CROSSCUT   = 0.10

RELEVANCE_TITLE = 1.00
RELEVANCE_THEME = 0.65
RELEVANCE_TEXT  = 0.35


def compute_score(session_date, contrib_count, policy_area_count, match_type) -> float:
    relevance = {
        "title": RELEVANCE_TITLE,
        "theme": RELEVANCE_THEME,
        "text":  RELEVANCE_TEXT,
    }.get(match_type, RELEVANCE_TEXT)
    return (
        WEIGHT_RELEVANCE  * relevance
        + WEIGHT_RECENCY    * recency_score(session_date)
        + WEIGHT_ENGAGEMENT * engagement_score(contrib_count)
        + WEIGHT_CROSSCUT   * crosscut_score(policy_area_count)
    )


def human_date(d) -> str:
    return f"{d.day} {MONTH[d.month]} {d.year}"


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------

def run_query(q: str, limit: int = 15) -> None:
    with app.app_context():
        base = HansardSession.query.filter_by(is_container=False)

        # Collect matching session IDs per match type
        title_ids: set[int] = {
            s.id for s in base.filter(HansardSession.title.ilike(f"%{q}%")).all()
        }
        theme_ids: set[int] = {
            r[0] for r in db.session.query(HansardSessionTheme.session_id)
            .filter(HansardSessionTheme.theme.ilike(f"%{q}%")).all()
        }
        text_ids: set[int] = {
            r[0] for r in db.session.query(HansardContribution.session_id)
            .filter(HansardContribution.speech_text.ilike(f"%{q}%")).all()
        }

        all_ids = title_ids | theme_ids | text_ids
        if not all_ids:
            print(f"\n['{q}'] — no matches in archive")
            return

        sessions = HansardSession.query.filter(
            HansardSession.id.in_(all_ids),
            HansardSession.is_container == False,
        ).all()

        # Batch load contrib counts
        counts = dict(
            db.session.query(
                HansardContribution.session_id,
                func.count(HansardContribution.id),
            )
            .filter(
                HansardContribution.session_id.in_(all_ids),
                HansardContribution.member_name.isnot(None),
            )
            .group_by(HansardContribution.session_id)
            .all()
        )

        # Batch load policy area counts
        pa_counts = dict(
            db.session.query(
                HansardSessionTheme.session_id,
                func.count(HansardSessionTheme.id),
            )
            .filter(
                HansardSessionTheme.session_id.in_(all_ids),
                HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
            )
            .group_by(HansardSessionTheme.session_id)
            .all()
        )

        # Build scored rows
        rows = []
        for s in sessions:
            if s.id in title_ids:
                match_type = "title"
            elif s.id in theme_ids:
                match_type = "theme"
            else:
                match_type = "text"
            cc = counts.get(s.id, 0)
            pc = pa_counts.get(s.id, 0)
            score = compute_score(s.date, cc, pc, match_type)
            rows.append({
                "date":    s.date,
                "title":   s.title[:55],
                "house":   s.house[:1],        # C / L
                "dtype":   s.debate_type[:4],  # oral / deba / west / mins / othe
                "contrib": cc,
                "pa":      pc,
                "match":   match_type[:5],
                "score":   score,
            })

        before = sorted(rows, key=lambda r: r["date"], reverse=True)
        after  = sorted(rows, key=lambda r: r["score"], reverse=True)

        print(f"\n{'='*80}")
        print(f"  Query: \"{q}\"  ({len(rows)} matching sessions, showing top {limit})")
        print(f"  Half-life {HALF_LIFE_DAYS}d | weights: relevance {WEIGHT_RELEVANCE}"
              f" / recency {WEIGHT_RECENCY} / engagement {WEIGHT_ENGAGEMENT}"
              f" / crosscut {WEIGHT_CROSSCUT}")
        print(f"{'='*80}")

        W = 55  # title column width
        header = f"{'#':<3} {'Date':<16} {'C':<2} {'T':<5} {'Ctr':>4} {'PA':>3} {'Match':<6} {'Title'}"
        divider = "-" * (len(header) + W - 6)

        print(f"\n  BEFORE (current: date DESC)  — top {limit}:")
        print("  " + header)
        print("  " + divider)
        for i, r in enumerate(before[:limit], 1):
            print(f"  {i:<3} {human_date(r['date']):<16} {r['house']:<2} {r['dtype']:<5} "
                  f"{r['contrib']:>4} {r['pa']:>3} {r['match']:<6} {r['title']}")

        print(f"\n  AFTER  (proposed scoring)    — top {limit}:")
        print(f"  {'#':<3} {'Score':<7} {'Date':<16} {'C':<2} {'T':<5} {'Ctr':>4} {'PA':>3} {'Match':<6} {'Title'}")
        print("  " + divider)
        for i, r in enumerate(after[:limit], 1):
            print(f"  {i:<3} {r['score']:.3f}  {human_date(r['date']):<16} {r['house']:<2} {r['dtype']:<5} "
                  f"{r['contrib']:>4} {r['pa']:>3} {r['match']:<6} {r['title']}")

        # Show any sessions that appear in AFTER but not BEFORE top-N (true promotions)
        before_top_ids = {(r["date"], r["title"]) for r in before[:limit]}
        promoted = [r for r in after[:limit] if (r["date"], r["title"]) not in before_top_ids]
        if promoted:
            print(f"\n  >>> Promoted into top {limit} by scoring (not in date-DESC top {limit}):")
            for r in promoted:
                print(f"      [{human_date(r['date'])}] {r['title']} — score {r['score']:.3f}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_queries = sys.argv[1:] or [
        "student loan",
        "housing",
        "net zero",
        "AI",
    ]
    print(f"\nScoring demo — TODAY = {human_date(TODAY)}")
    print(f"Formula: score = {WEIGHT_RELEVANCE}×relevance + {WEIGHT_RECENCY}×recency"
          f" + {WEIGHT_ENGAGEMENT}×engagement + {WEIGHT_CROSSCUT}×crosscut")
    for q in test_queries:
        run_query(q)
    print()
