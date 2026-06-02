"""
Backfill / correct / compare first_published_at on ha_stat_publication.

Authoritative date source (via hansard_archive.discovery.pub_dates):
  - GOV.UK pubs: GOV.UK Content API first_published_at
  - ONS pubs:    ONS datasets API release_date

Modes:
  (default, dry-run)  log what WOULD be written for NULL rows; write nothing
  --execute           populate NULL rows only (original backfill behaviour)
  --compare           READ-ONLY over ALL rows: fetch authoritative date, log
                      MATCH / DIFFER vs stored value (diagnostic — no writes)
  --refresh --execute write authoritative date to ALL rows where it DIFFERS
                      from the stored value (corrective pass)

Deploy as a Railway one-shot service (restart: Never, SKIP_MIGRATIONS=1).
CAPTURE LOGS BEFORE TEARDOWN — log file is the only persistent output.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("backfill_pub_dates")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

import requests

from hansard_archive.discovery.pub_dates import resolve_publication_date

FETCH_DELAY = 1.5
BATCH_SIZE  = 50


def _configure_nullpool(app, db) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    connect_args = (
        {"connect_timeout": 10, "options": "-c statement_timeout=30000"}
        if uri.startswith("postgresql") else {}
    )
    with app.app_context():
        db.engine.dispose()
    new_engine = create_engine(uri, poolclass=NullPool, connect_args=connect_args)
    db._app_engines[app][None] = new_engine
    log.info("NullPool engine configured (driver: %s)", new_engine.url.drivername)


def run_backfill(db_session, execute: bool, compare: bool, refresh: bool) -> dict:
    """
    compare=True  → READ-ONLY over ALL rows; log MATCH/DIFFER; never writes.
    refresh=True  → process ALL rows; write only where authoritative DIFFERS.
    neither       → process NULL rows only (original backfill).
    execute gates all writes (ignored when compare=True).
    """
    from datetime import datetime
    from hansard_archive.models import StatProducer, StatPublication
    from sqlalchemy.orm import joinedload

    stats = {"processed": 0, "match": 0, "differ": 0, "null_result": 0,
             "written": 0, "skipped_no_source": 0, "errors": 0}
    http = requests.Session()
    last_id = 0
    scan_all = compare or refresh

    log.info("Starting pub-date pass — compare=%s refresh=%s execute=%s",
             compare, refresh, execute)

    while True:
        q = (
            db_session.query(StatPublication)
            .options(joinedload(StatPublication.producer))
            .join(StatProducer)
            .filter(StatPublication.id > last_id)
        )
        if not scan_all:
            q = q.filter(StatPublication.first_published_at.is_(None))
        batch = q.order_by(StatPublication.id).limit(BATCH_SIZE).all()
        if not batch:
            break

        for pub in batch:
            last_id = pub.id
            stats["processed"] += 1
            producer_slug = pub.producer.slug if pub.producer else "unknown"
            stored = pub.first_published_at

            try:
                fetched = resolve_publication_date(pub.url, http)

                if fetched is None:
                    log.info("pub=%d producer=%s NULL-result stored=%s url=%.70s",
                             pub.id, producer_slug, stored, pub.url)
                    stats["null_result"] += 1
                    continue

                if stored == fetched:
                    stats["match"] += 1
                    if compare:
                        log.info("pub=%d producer=%s MATCH date=%s",
                                 pub.id, producer_slug, fetched)
                else:
                    stats["differ"] += 1
                    log.info("pub=%d producer=%s DIFFER added=%s stored=%s fetched=%s url=%.70s",
                             pub.id, producer_slug,
                             pub.discovered_at.date() if pub.discovered_at else None,
                             stored, fetched, pub.url)
                    # Write only outside compare mode, when executing, and when
                    # there is a real change (covers both NULL-fill and refresh).
                    if execute and not compare:
                        pub.first_published_at = fetched
                        pub.updated_at = datetime.utcnow()
                        db_session.commit()
                        stats["written"] += 1

            except Exception as exc:
                log.exception("Unhandled error pub=%d: %s", pub.id, exc)
                stats["errors"] += 1
                try:
                    db_session.rollback()
                except Exception:
                    pass

            time.sleep(FETCH_DELAY)

    log.info("Pass complete: %s", stats)
    return stats


def run(execute: bool = False, compare: bool = False, refresh: bool = False) -> None:
    from flask_app import app, db

    _configure_nullpool(app, db)

    with app.app_context():
        run_backfill(db_session=db.session, execute=execute,
                     compare=compare, refresh=refresh)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Backfill / compare / refresh first_published_at on ha_stat_publication"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Write dates to database (default: dry-run, log only)",
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="READ-ONLY: scan all rows, log MATCH/DIFFER vs authoritative date",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Scan all rows (not just NULL); with --execute, correct DIFFER rows",
    )
    args = parser.parse_args()
    run(execute=args.execute, compare=args.compare, refresh=args.refresh)


if __name__ == "__main__":
    main()
