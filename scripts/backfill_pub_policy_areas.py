"""
scripts/backfill_pub_policy_areas.py

Phase 1.8 one-shot backfill: tag existing StatPublication rows with
policy_area themes using the Gemini Flash-Lite classifier.

Dry-run by default. Pass --execute to write.

Intended use: deploy as a Railway one-shot service, not run locally.
Local connections to production Postgres are prohibited (see CLAUDE.md).

Runtime estimate: ~1,124 publications × 200ms = ~4 min at 5 calls/sec.
Gemini Flash-Lite cost at paid tier: ~$0.10–0.30 total for this run.

Resume: if interrupted, restart with the same command. The batch query
excludes publications that already have policy_area tags, so any completed
work is skipped automatically.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

log = logging.getLogger("backfill_pub_policy_areas")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_TARGET_STATUSES = ("candidate", "authorised")
_BATCH_SIZE = 50
_INTER_CALL_DELAY = 0.20   # seconds between Gemini calls (~5 req/s)
_MAX_COMMIT_RETRIES = 3


def _run(execute: bool) -> None:
    import os
    from sqlalchemy.exc import OperationalError

    from flask_app import app, db
    from hansard_archive.models import StatPublication, StatPublicationTheme, THEME_TYPE_POLICY_AREA
    from hansard_archive.discovery.classifier import classify_candidate

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set — aborting")
        sys.exit(1)

    mode = "EXECUTE" if execute else "DRY-RUN"
    log.info("[%s] starting backfill", mode)

    with app.app_context():
        total_pubs = (
            db.session.query(StatPublication)
            .filter(StatPublication.authorisation_status.in_(_TARGET_STATUSES))
            .count()
        )
        log.info("[%s] total target publications: %d", mode, total_pubs)

        written = 0
        failed = 0
        no_tags = 0
        consecutive_commit_failures = 0
        last_id = 0

        while True:
            batch_start_id = last_id

            # Rebuilt each iteration (not hoisted) — defends against concurrent
            # run_discovery() writes adding new tagged publications mid-backfill.
            already_tagged_inner = (
                db.session.query(StatPublicationTheme.publication_id)
                .filter(StatPublicationTheme.theme_type == THEME_TYPE_POLICY_AREA)
                .distinct()
                .subquery()
            )

            try:
                batch = (
                    db.session.query(StatPublication)
                    .filter(
                        StatPublication.authorisation_status.in_(_TARGET_STATUSES),
                        StatPublication.id > last_id,
                        ~StatPublication.id.in_(already_tagged_inner),
                    )
                    .order_by(StatPublication.id)
                    .limit(_BATCH_SIZE)
                    .all()
                )
            except OperationalError as exc:
                log.warning("DB fetch error (last_id=%d): %s — reconnecting", last_id, exc)
                db.session.rollback()
                db.engine.dispose()
                continue

            if not batch:
                break

            for pub in batch:
                last_id = pub.id

                candidate = {
                    "title": pub.name,
                    "url": pub.url,
                    "date_hints": [],
                    "description_hint": pub.description or "",
                }
                result = classify_candidate(candidate, pub.producer, gemini_key)

                if result is None:
                    log.warning(
                        "classify_candidate returned None for pub id=%d (%r) — skipping",
                        pub.id, pub.name,
                    )
                    failed += 1
                    time.sleep(_INTER_CALL_DELAY)
                    continue

                areas = result.get("policy_areas", [])
                if not areas:
                    log.info("pub id=%d (%r): no policy_areas returned", pub.id, pub.name)
                    no_tags += 1
                    time.sleep(_INTER_CALL_DELAY)
                    continue

                log.info("pub id=%d (%r): %d area(s): %s",
                         pub.id, pub.name, len(areas), areas)

                if execute:
                    for area in areas:
                        db.session.add(StatPublicationTheme(
                            publication_id=pub.id,
                            theme=area,
                            theme_type=THEME_TYPE_POLICY_AREA,
                            model_used=result.get("model_used"),
                        ))
                    written += len(areas)

                time.sleep(_INTER_CALL_DELAY)

            if execute:
                try:
                    db.session.commit()
                    consecutive_commit_failures = 0
                    log.info("Batch committed — theme rows written so far: %d, failed: %d",
                             written, failed)
                except OperationalError as exc:
                    consecutive_commit_failures += 1
                    if consecutive_commit_failures >= _MAX_COMMIT_RETRIES:
                        log.error(
                            "Commit failed %d times in a row — aborting. "
                            "Restart script to resume from id > %d.",
                            consecutive_commit_failures, batch_start_id,
                        )
                        db.session.rollback()
                        sys.exit(1)
                    log.warning(
                        "Commit error (%d/%d): %s — rolling back, retrying batch",
                        consecutive_commit_failures, _MAX_COMMIT_RETRIES, exc,
                    )
                    db.session.rollback()
                    db.engine.dispose()
                    last_id = batch_start_id  # re-fetch this batch on next iteration

        log.info(
            "[%s] complete — theme rows %s: %d, pubs with no tags: %d, classify failures: %d",
            mode,
            "written" if execute else "would be written",
            written,
            no_tags,
            failed,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill policy_area tags onto existing StatPublication rows."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write to the database. Omit for dry-run (default).",
    )
    args = parser.parse_args()
    _run(execute=args.execute)


if __name__ == "__main__":
    main()
