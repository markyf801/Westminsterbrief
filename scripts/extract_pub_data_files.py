"""
Extract data file URLs from statistical publication landing pages.

Deployment modes
----------------
One-shot backfill (piece 2):
    python scripts/extract_pub_data_files.py [--execute]
    Run as a Railway one-shot service; tear down when complete.
    Processes all GOV.UK-producer publications with data_files_status='pending'.
    ONS and other non-GOV.UK producers are skipped (stay pending for piece 3).

Daily cron (steady-state):
    Same script, same command.
    Run as a Railway scheduled service after the backfill is done.
    Processes any new pending publications since the previous run.

Default: dry-run (logs decisions, writes nothing).
Pass --execute to write to the database.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("extract_pub_data_files")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

import requests

BATCH_SIZE  = 50
FETCH_DELAY = 1.5    # seconds between HTTP requests
GOVUK_ROOT  = "https://www.gov.uk/"
EES_PREFIX  = "https://explore-education-statistics.service.gov.uk"


# ---------------------------------------------------------------------------
# NullPool — same pattern as discovery_worker.py
# ---------------------------------------------------------------------------

def _configure_nullpool(app, db) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    with app.app_context():
        db.engine.dispose()
    new_engine = create_engine(
        app.config["SQLALCHEMY_DATABASE_URI"],
        poolclass=NullPool,
        connect_args={"connect_timeout": 10, "options": "-c statement_timeout=30000"},
    )
    db._app_engines[app][None] = new_engine
    log.info("NullPool engine configured (driver: %s)", new_engine.url.drivername)


# ---------------------------------------------------------------------------
# Core extraction loop
# ---------------------------------------------------------------------------

def run_extraction(db_session, execute: bool, ids: list | None = None) -> dict:
    from hansard_archive.models import StatProducer, StatPublication, StatPublicationDataFile
    from hansard_archive.discovery.extractors import select_extractor

    stats = defaultdict(int)
    http  = requests.Session()
    last_id = 0

    log.info("Querying first batch (last_id=%d ids=%s)", last_id, ids)
    while True:
        filters = [
            StatPublication.data_files_status.in_(["pending", "fetch_failed"]),
            StatPublication.id > last_id,
            StatProducer.web_root_url.like(GOVUK_ROOT + "%"),
        ]
        if ids is not None:
            filters.append(StatPublication.id.in_(ids))

        batch = (
            db_session.query(StatPublication)
            .join(StatProducer)
            .filter(*filters)
            .order_by(StatPublication.id)
            .limit(BATCH_SIZE)
            .all()
        )
        if not batch:
            break

        for pub in batch:
            last_id = pub.id
            stats["processed"] += 1

            try:
                result = _extract_one(pub, http, select_extractor)

                if result.sub_page_urls:
                    log.info(
                        "[SUB_PAGE] pub=%d producer=%s sub_page_count=%d urls=%s",
                        pub.id,
                        pub.producer.slug if pub.producer else "unknown",
                        len(result.sub_page_urls),
                        result.sub_page_urls,
                    )

                if not execute:
                    log.info("[DRY RUN] pub=%d status=%s files=%d ees=%s url=%.80s",
                             pub.id, result.status, len(result.files),
                             bool(result.ees_url), pub.url)
                    for f in result.files:
                        log.info("  → %s | type=%-5s | size=%-10s | cls=%s | %.70s",
                                 f.display_order, f.file_type or "?",
                                 f.file_size_bytes or "?", f.classification or "null",
                                 f.url)
                    stats["dry_run"] += 1
                else:
                    _write_result(db_session, pub, result, StatPublicationDataFile)
                    stats[result.status] += 1
                    log.info("pub=%d → %s  files=%d  ees=%s",
                             pub.id, result.status, len(result.files), bool(result.ees_url))

            except Exception as exc:
                log.exception("Unhandled error pub=%d (%s): %s", pub.id, pub.url, exc)
                stats["unhandled_error"] += 1
                if execute:
                    _safe_mark_failed(db_session, pub)

            time.sleep(FETCH_DELAY)

    return dict(stats)


def _extract_one(pub, http, select_extractor_fn):
    from hansard_archive.discovery.extractors.base import ExtractionResult

    # Case (a): publication URL IS an EES page — no fetch needed
    if pub.url.startswith(EES_PREFIX):
        return ExtractionResult(status="not_extractable", ees_url=pub.url)

    extractor = select_extractor_fn(pub)
    return extractor.extract(pub.url, http)


def _write_result(db_session, pub, result, StatPublicationDataFile) -> None:
    # Delete existing rows only when fetch succeeded (preserve on fetch_failed)
    if result.status != "fetch_failed":
        db_session.query(StatPublicationDataFile).filter_by(
            publication_id=pub.id
        ).delete(synchronize_session=False)

    for f in result.files:
        db_session.add(StatPublicationDataFile(
            publication_id=pub.id,
            url=f.url,
            file_type=f.file_type,
            title=f.title,
            file_size_bytes=f.file_size_bytes,
            classification=f.classification,
            display_order=f.display_order,
        ))

    pub.data_files_status = result.status
    if result.status != "fetch_failed":
        pub.data_files_extracted_at = datetime.utcnow()
    if result.ees_url:
        pub.ees_url = result.ees_url

    db_session.commit()


def _safe_mark_failed(db_session, pub) -> None:
    try:
        db_session.rollback()
        pub.data_files_status = "fetch_failed"
        db_session.commit()
    except Exception as exc:
        log.error("Could not mark pub=%d failed: %s", pub.id, exc)
        db_session.rollback()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(execute: bool = False, ids: list | None = None) -> None:
    from flask_app import app, db

    _configure_nullpool(app, db)
    log.info("Starting extraction — execute=%s ids=%s", execute, ids)

    with app.app_context():
        stats = run_extraction(db_session=db.session, execute=execute, ids=ids)

    log.info("Extraction complete: %s", stats)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Extract data file URLs from GOV.UK publication pages"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Write results to database (default: dry-run, log only)"
    )
    parser.add_argument(
        "--ids",
        help="Comma-separated publication IDs to process (default: all pending/failed)"
    )
    args = parser.parse_args()
    ids = [int(i.strip()) for i in args.ids.split(",")] if args.ids else None
    run(execute=args.execute, ids=ids)


if __name__ == "__main__":
    main()
