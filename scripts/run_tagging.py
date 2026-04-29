"""
Hansard Archive — Phase 2A Week 2 batch theme tagging.

Tags all untagged non-container sessions with two-level themes via Gemini Flash-Lite.
Safe to re-run: already-tagged sessions are skipped.

Usage:
  python scripts/run_tagging.py                  # tag all ~1,101 eligible sessions
  python scripts/run_tagging.py --limit 50       # tag first 50 (sample run / validation)
  python scripts/run_tagging.py --limit 1 --id 123  # tag a specific session by ID

Environment:
  GEMINI_API_KEY — required
  DATABASE_URL   — optional; defaults to local SQLite (intelligence.db)
"""

import argparse
import sys

sys.path.insert(0, ".")

from flask_app import app
from hansard_archive.tagger import POLICY_AREAS, tag_all_untagged, tag_session
from hansard_archive.models import HansardSessionTheme


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch theme-tag Hansard sessions")
    parser.add_argument("--limit", type=int, default=None, help="Tag at most N sessions")
    parser.add_argument("--id", type=int, default=None, dest="session_id",
                        help="Tag a single specific session by ID")
    parser.add_argument("--include-other", action="store_true",
                        help="Include debate_type=other sessions (off by default)")
    args = parser.parse_args()

    skip_types = () if args.include_other else ("other",)

    with app.app_context():
        if args.session_id:
            print(f"[tagging] Tagging session {args.session_id}...")
            count = tag_session(args.session_id)
            print(f"[tagging] Done — {count} theme rows written")
            return

        before = HansardSessionTheme.query.count()
        summary = tag_all_untagged(
            limit=args.limit,
            verbose=True,
            skip_types=skip_types,
        )
        after = HansardSessionTheme.query.count()

    print()
    print("=" * 60)
    print("[tagging] Done.")
    print(f"[tagging]   Eligible sessions: {summary['total_eligible']}")
    print(f"[tagging]   Tagged:            {summary['tagged']}")
    print(f"[tagging]   Failed (no response): {summary['failed']}")
    print(f"[tagging]   Errors:            {summary['errors']}")
    print(f"[tagging]   Theme rows added:  {after - before}")
    print("=" * 60)
    print()
    print(f"[tagging] Policy areas in use (controlled vocab — {len(POLICY_AREAS)} terms):")
    print(f"[tagging] Run 'SELECT theme, COUNT(*) FROM ha_session_theme WHERE theme_type=\"policy_area\"")
    print(f"[tagging]   GROUP BY theme ORDER BY COUNT(*) DESC' to see distribution.")


if __name__ == "__main__":
    main()
