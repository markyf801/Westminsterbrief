"""
Backfill first_published_at on ha_stat_publication.

For each publication where first_published_at IS NULL:
  - GOV.UK pubs: call the GOV.UK Content API (first_published_at field)
  - ONS pubs:    call the ONS datasets API (release_date field)
  - Others:      skip — no structured date source available

Deploy as a Railway one-shot service (restart: Never, SKIP_MIGRATIONS=1).
CAPTURE LOGS BEFORE TEARDOWN — log file is the only persistent output.

Dry-run by default. Pass --execute to write to the database.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date as date_type
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("backfill_pub_dates")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

import requests

GOVUK_CONTENT_API = "https://www.gov.uk/api/content"
ONS_DATASETS_API  = "https://api.beta.ons.gov.uk/v1/datasets"
FETCH_DELAY       = 1.5
BATCH_SIZE        = 50
UA                = "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"


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


def _fetch_govuk_date(pub_url: str, http: requests.Session) -> date_type | None:
    """
    Fetch first_published_at from the GOV.UK Content API.
    Returns None if the field is absent or the request fails.
    """
    path = urlparse(pub_url).path
    api_url = f"{GOVUK_CONTENT_API}{path}"
    for attempt in range(3):
        try:
            resp = http.get(api_url, timeout=20, headers={"User-Agent": UA})
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1) * 5
                log.warning("429 rate-limited (GOV.UK Content API) — waiting %ds", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                log.debug("GOV.UK Content API 404 for %s", path)
                return None
            if not resp.ok:
                log.warning("GOV.UK Content API %d for %s", resp.status_code, path)
                return None
            raw = resp.json().get("first_published_at", "")
            if not raw:
                return None
            return date_type.fromisoformat(raw[:10])
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("GOV.UK Content API error (attempt %d) for %s: %s",
                        attempt + 1, path, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def _fetch_ons_date(pub_url: str, http: requests.Session) -> date_type | None:
    """
    Fetch release_date from the ONS datasets API.
    URL form: https://www.ons.gov.uk/datasets/{dataset_id}
    Returns None if the field is absent or the request fails.
    """
    parts = [p for p in urlparse(pub_url).path.split("/") if p]
    # Expect path like /datasets/{dataset_id}
    if len(parts) < 2 or parts[0] != "datasets":
        log.warning("ONS URL doesn't match /datasets/{id} pattern: %s", pub_url)
        return None
    dataset_id = parts[1]
    api_url = f"{ONS_DATASETS_API}/{dataset_id}"
    for attempt in range(3):
        try:
            resp = http.get(api_url, timeout=20, headers={"User-Agent": UA})
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1) * 5
                log.warning("429 rate-limited (ONS API) — waiting %ds", wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                log.warning("ONS API %d for dataset %s", resp.status_code, dataset_id)
                return None
            raw = resp.json().get("release_date", "")
            if not raw:
                return None
            return date_type.fromisoformat(raw[:10])
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("ONS API error (attempt %d) for %s: %s",
                        attempt + 1, dataset_id, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def run_backfill(db_session, execute: bool) -> dict:
    from datetime import datetime
    from hansard_archive.models import StatProducer, StatPublication
    from sqlalchemy.orm import joinedload

    stats = {"processed": 0, "populated": 0, "null_result": 0,
             "skipped_no_source": 0, "errors": 0}
    http = requests.Session()
    last_id = 0

    log.info("Starting publication date backfill — execute=%s", execute)

    while True:
        batch = (
            db_session.query(StatPublication)
            .options(joinedload(StatPublication.producer))
            .join(StatProducer)
            .filter(
                StatPublication.first_published_at.is_(None),
                StatPublication.id > last_id,
            )
            .order_by(StatPublication.id)
            .limit(BATCH_SIZE)
            .all()
        )
        if not batch:
            break

        for pub in batch:
            last_id = pub.id
            stats["processed"] += 1
            producer_slug = pub.producer.slug if pub.producer else "unknown"

            try:
                if pub.url.startswith("https://www.gov.uk/"):
                    date_val = _fetch_govuk_date(pub.url, http)
                    source   = "govuk_content_api:first_published_at"
                elif pub.url.startswith("https://www.ons.gov.uk/datasets/"):
                    date_val = _fetch_ons_date(pub.url, http)
                    source   = "ons_api:release_date"
                else:
                    log.debug("pub=%d no date source for URL %s", pub.id, pub.url)
                    stats["skipped_no_source"] += 1
                    continue

                if date_val is None:
                    log.info("pub=%d producer=%s NULL date  source=%s url=%.70s",
                             pub.id, producer_slug, source, pub.url)
                    stats["null_result"] += 1
                else:
                    log.info("pub=%d producer=%s date=%s source=%s",
                             pub.id, producer_slug, date_val, source)
                    stats["populated"] += 1
                    if execute:
                        pub.first_published_at = date_val
                        pub.updated_at = datetime.utcnow()
                        db_session.commit()

            except Exception as exc:
                log.exception("Unhandled error pub=%d: %s", pub.id, exc)
                stats["errors"] += 1
                try:
                    db_session.rollback()
                except Exception:
                    pass

            time.sleep(FETCH_DELAY)

    log.info("Backfill complete: %s", stats)
    return stats


def run(execute: bool = False) -> None:
    from flask_app import app, db

    _configure_nullpool(app, db)

    with app.app_context():
        run_backfill(db_session=db.session, execute=execute)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Backfill first_published_at on ha_stat_publication"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Write dates to database (default: dry-run, log only)",
    )
    args = parser.parse_args()
    run(execute=args.execute)


if __name__ == "__main__":
    main()
