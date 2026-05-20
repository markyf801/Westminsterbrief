"""
Cleanup orphan placeholder StatObservation rows.

The Phase 1 startup backfill created one StatObservation per HeadlineStat
with period_start=NULL and period_end=NULL, copying the legacy single-value
fields. Phase 1.5 ingestion now writes real time-series observations with
proper period bounds. Once a stat has at least one real observation, its
NULL-period placeholder is superseded and should be removed.

A placeholder row is safe to delete when the SAME headline_stat_id has at
least one other observation with non-NULL period_start and period_end.
Placeholders for stats that have no real observations yet are left in place
so those stats continue to render.

Usage:
  python scripts/cleanup_placeholder_observations.py          # dry run (default)
  python scripts/cleanup_placeholder_observations.py --execute # actually delete

Always run on beta first; only run on production after confirming Phase 1.5
has been refreshing for several weeks and legacy placeholders are superseded.

Required env vars (same as flask_app):
  DATABASE_URL (or local SQLite used automatically if unset)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("cleanup_placeholder_observations")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def find_deletable_placeholders(db_session, StatObservation) -> list:
    """
    Return all StatObservation rows that are placeholder rows (NULL period)
    whose stat also has at least one real observation (non-NULL period).

    A row is a placeholder if period_start IS NULL AND period_end IS NULL.
    It is deletable if the same headline_stat_id has at least one row where
    both period_start and period_end are non-NULL.
    """
    # IDs of stats that have at least one real (non-NULL period) observation.
    stats_with_real_obs = (
        db_session.query(StatObservation.headline_stat_id)
        .filter(
            StatObservation.period_start.isnot(None),
            StatObservation.period_end.isnot(None),
        )
        .distinct()
    )

    return (
        db_session.query(StatObservation)
        .filter(
            StatObservation.period_start.is_(None),
            StatObservation.period_end.is_(None),
            StatObservation.headline_stat_id.in_(stats_with_real_obs),
        )
        .all()
    )


def find_retained_placeholders(db_session, StatObservation) -> int:
    """
    Count placeholder rows that will be retained because their stat has no
    real observations yet. Used for the summary line only.
    """
    stats_with_real_obs = (
        db_session.query(StatObservation.headline_stat_id)
        .filter(
            StatObservation.period_start.isnot(None),
            StatObservation.period_end.isnot(None),
        )
        .distinct()
    )

    return (
        db_session.query(StatObservation)
        .filter(
            StatObservation.period_start.is_(None),
            StatObservation.period_end.is_(None),
            ~StatObservation.headline_stat_id.in_(stats_with_real_obs),
        )
        .count()
    )


def run(execute: bool = False) -> None:
    from flask_app import app, db
    from hansard_archive.models import StatObservation

    with app.app_context():
        deletable = find_deletable_placeholders(db.session, StatObservation)
        retained_count = find_retained_placeholders(db.session, StatObservation)

        if not deletable:
            log.info(
                "No deletable placeholders found. "
                "%d placeholder(s) retained (stats with no real observations yet).",
                retained_count,
            )
            return

        # Group by headline_stat_id for logging.
        by_stat: dict[int, list] = defaultdict(list)
        for obs in deletable:
            by_stat[obs.headline_stat_id].append(obs)

        mode = "EXECUTE" if execute else "DRY RUN"
        log.info("[%s] Found %d placeholder(s) across %d stat(s) to delete:",
                 mode, len(deletable), len(by_stat))

        for stat_id, rows in sorted(by_stat.items()):
            log.info("  headline_stat_id=%d: %d placeholder(s)", stat_id, len(rows))

        if not execute:
            log.info(
                "[DRY RUN] No rows deleted. Pass --execute to perform deletion. "
                "%d placeholder(s) retained (no real observations yet).",
                retained_count,
            )
            return

        # Deletion.
        n_deleted = 0
        for stat_id, rows in sorted(by_stat.items()):
            for obs in rows:
                db.session.delete(obs)
            db.session.commit()
            log.info("  DELETED %d placeholder(s) for headline_stat_id=%d",
                     len(rows), stat_id)
            n_deleted += len(rows)

        log.info(
            "Done. %d placeholder(s) deleted across %d stat(s). "
            "%d placeholder(s) retained (no real observations yet).",
            n_deleted, len(by_stat), retained_count,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Remove orphan placeholder StatObservation rows (period IS NULL) "
            "for stats that now have real time-series observations."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually delete rows. Without this flag the script is a dry run.",
    )
    args = parser.parse_args()

    if args.execute:
        log.warning(
            "Running in EXECUTE mode — placeholder rows WILL be deleted. "
            "Ensure you have run this on beta first."
        )

    run(execute=args.execute)


if __name__ == "__main__":
    main()
