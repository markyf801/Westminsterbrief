"""
Re-discovery cron — keeps the stats catalogue current.

For each AUTHORISED, already-COMPLETED producer due for re-discovery (daily flat
cadence, tracked by last_rediscovered_at), run a re-discovery pass: find NEW
publications cheaply (pre-classify URL/dataset-ID dedup, so only genuinely-new
ones hit Gemini), insert them, and refresh ONS latest-release dates on known
datasets (the a+b unification). Never disturbs discovery_status or an existing
GOV.UK first_published_at.

Respects Decision H: discovery only — NO data-file extraction (that is the
separate maintenance cron).

Modes:
  (default)   --dry-run is implied: logs the distribution, writes NOTHING.
  --execute   write to the database.

The headline number is SKIPPED_KNOWN. A healthy run is mostly skips; a low skip
count means the pre-classify dedup is not working and a re-classify of the whole
corpus is imminent — STOP and investigate.

Deploy first as a Railway one-shot (--dry-run, then a controlled --execute), and
only THEN as the scheduled daily service. SKIP_MIGRATIONS=1, Restart: Never for
the one-shot. CAPTURE LOGS BEFORE TEARDOWN.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("rediscovery_cron")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REDISCOVERY_INTERVAL = timedelta(days=1)   # daily flat cadence, all due producers


def _configure_nullpool(app, db) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    connect_args = (
        {"connect_timeout": 10, "options": "-c statement_timeout=120000"}
        if uri.startswith("postgresql") else {}
    )
    with app.app_context():
        db.engine.dispose()
    new_engine = create_engine(uri, poolclass=NullPool, connect_args=connect_args)
    db._app_engines[app][None] = new_engine
    log.info("NullPool engine configured (driver: %s)", new_engine.url.drivername)


def _due_producers(db_session, cutoff):
    """Authorised + completed producers never re-discovered or due (cadence)."""
    from hansard_archive.models import StatProducer
    return (
        db_session.query(StatProducer)
        .filter(
            StatProducer.authorisation_status == "authorised",
            StatProducer.discovery_status == "completed",
            (StatProducer.last_rediscovered_at.is_(None))
            | (StatProducer.last_rediscovered_at < cutoff),
        )
        .order_by(StatProducer.id)
        .all()
    )


def run(execute: bool) -> None:
    from flask_app import app, db
    from hansard_archive.discovery import run_rediscovery

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set — aborting")
        return

    _configure_nullpool(app, db)
    cutoff = datetime.utcnow() - REDISCOVERY_INTERVAL

    totals = {"producers": 0, "fetched": 0, "skipped_known": 0, "new": 0,
              "ons_date_updated": 0, "classify_failed": 0, "slug_collision": 0,
              "errors": 0}

    with app.app_context():
        producers = _due_producers(db.session, cutoff)
        log.info("Re-discovery — execute=%s — %d producer(s) due", execute, len(producers))

        for producer in producers:
            totals["producers"] += 1
            try:
                s = run_rediscovery(producer, db.session, gemini_key, dry_run=not execute)
                for k in ("fetched", "skipped_known", "new", "ons_date_updated",
                          "classify_failed", "slug_collision"):
                    totals[k] += s.get(k, 0)
            except Exception as exc:
                log.exception("Re-discovery failed for %s: %s", producer.slug, exc)
                totals["errors"] += 1
                try:
                    db.session.rollback()
                except Exception:
                    pass

    # Headline: SKIPPED_KNOWN should dominate. Low skip => dedup not working.
    log.info("=== RE-DISCOVERY COMPLETE (execute=%s) ===", execute)
    log.info("producers=%d  fetched=%d  SKIPPED_KNOWN=%d  new=%d  "
             "ons_date_updated=%d  classify_failed=%d  slug_collision=%d  errors=%d",
             totals["producers"], totals["fetched"], totals["skipped_known"],
             totals["new"], totals["ons_date_updated"], totals["classify_failed"],
             totals["slug_collision"], totals["errors"])
    if totals["fetched"] and totals["skipped_known"] / totals["fetched"] < 0.5:
        log.warning("SKIP RATE LOW (%.0f%%) — pre-classify dedup may not be working; "
                    "investigate before scheduling.",
                    100 * totals["skipped_known"] / totals["fetched"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-discovery cron for the stats catalogue")
    parser.add_argument("--execute", action="store_true",
                        help="Write to the database (default: dry-run, log only)")
    args = parser.parse_args()
    run(execute=args.execute)


if __name__ == "__main__":
    main()
