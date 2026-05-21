"""
Backfill licence_evidence_wayback_url for ha_stat_producer rows where it is NULL.

Also corrects known-broken licence_evidence_raw_url values (404 pages) to their
working replacements. The raw URL correction and wayback capture are applied in
the same UPDATE per producer so the before_update audit listener records both
changes in one audit log entry.

Root cause of the original NULL values: https://www.gov.uk/help/copyright returns
404. It was used as the evidence URL for 21 of 29 producers. The correct canonical
OGL v3 URL is https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/
Four other producers had similarly broken evidence URLs.

Selects: ha_stat_producer WHERE licence_evidence_raw_url IS NOT NULL
                              AND licence_evidence_wayback_url IS NULL

Uses the Wayback Machine availability API (lookup for existing snapshot — no new
capture is triggered). Deduplicates Wayback calls by URL to keep request count low.
Waits _WAYBACK_DELAY seconds between unique URL lookups.

Dry-run is the DEFAULT. Pass --execute to write.

Usage:
  python scripts/backfill_wayback_snapshots.py           # dry run
  python scripts/backfill_wayback_snapshots.py --execute  # write
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("backfill_wayback")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Seconds to wait between Wayback API calls for distinct URLs.
# The availability endpoint is a lightweight read; 5s is conservative but safe.
_WAYBACK_DELAY = 5

# Broken raw evidence URLs (404 pages) mapped to their live replacements.
# All OGL_v3 central-department producers pointed to gov.uk/help/copyright (404);
# the canonical OGL v3 reference lives on nationalarchives.gov.uk.
_URL_CORRECTIONS: dict[str, str] = {
    "https://www.gov.uk/help/copyright": (
        "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
    ),
    "https://www.england.nhs.uk/contact-us/privacy-notice/copyright/": (
        "https://www.england.nhs.uk/contact-us/privacy-notice/"
    ),
    "https://www.nisra.gov.uk/contact/crown-copyright": (
        "https://www.nisra.gov.uk/crown-copyright"
    ),
    "https://www.llyw.cymru/copyright-statement": (
        "https://www.gov.wales/copyright-statement"
    ),
    "https://www.ucas.com/about-us/terms-and-conditions": (
        "https://www.ucas.com/terms-and-conditions-for-use-of-the-ucas-network"
    ),
}


def _lookup_wayback(url: str) -> str | None:
    """
    Query the Wayback Machine availability API for the closest existing snapshot.
    Returns the snapshot URL on success, None if no snapshot exists or on any error.
    """
    try:
        import requests
        resp = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=15,
        )
        snap = resp.json().get("archived_snapshots", {}).get("closest", {})
        if snap.get("available"):
            return snap["url"]
    except Exception as exc:
        log.warning("Wayback lookup error for %s: %s", url, exc)
    return None


def backfill(db_session, StatProducer, execute: bool = False) -> None:
    """
    Core backfill logic. Accepts db_session and StatProducer model class so it
    can be called from tests without a full Flask app startup.
    """
    producers = (
        db_session.query(StatProducer)
        .filter(
            StatProducer.licence_evidence_raw_url.isnot(None),
            StatProducer.licence_evidence_wayback_url.is_(None),
        )
        .order_by(StatProducer.id)
        .all()
    )

    if not producers:
        log.info("No producers need Wayback backfill — nothing to do.")
        return

    log.info("%d producer(s) need Wayback capture:", len(producers))

    if not execute:
        for p in producers:
            corrected = _URL_CORRECTIONS.get(
                p.licence_evidence_raw_url, p.licence_evidence_raw_url
            )
            note = " [raw URL will be corrected]" if corrected != p.licence_evidence_raw_url else ""
            log.info("  %s  →  %s%s", p.slug, corrected, note)
        log.info("[DRY RUN] No Wayback calls made. No DB writes. Pass --execute to run.")
        return

    # Deduplicate Wayback calls: many producers share the same evidence URL.
    wayback_cache: dict[str, str | None] = {}
    n_captured = 0
    n_failed = 0
    n_url_corrected = 0
    failures: list[str] = []

    for p in producers:
        raw_corrected = _URL_CORRECTIONS.get(
            p.licence_evidence_raw_url, p.licence_evidence_raw_url
        )
        url_was_corrected = raw_corrected != p.licence_evidence_raw_url

        if raw_corrected not in wayback_cache:
            log.info("  Querying Wayback: %s", raw_corrected)
            wayback_cache[raw_corrected] = _lookup_wayback(raw_corrected)
            time.sleep(_WAYBACK_DELAY)
        else:
            log.info(
                "  Using cached Wayback result for: %s", raw_corrected[:60]
            )

        wayback_url = wayback_cache[raw_corrected]

        if wayback_url:
            if url_was_corrected:
                p.licence_evidence_raw_url = raw_corrected
                n_url_corrected += 1
            p.licence_evidence_wayback_url = wayback_url
            db_session.flush()
            log.info("  OK   %s  wayback=%s…", p.slug, wayback_url[:70])
            n_captured += 1
        else:
            reason = f"no Wayback snapshot found for {raw_corrected}"
            log.warning("  FAIL %s  — %s", p.slug, reason)
            failures.append(f"{p.slug}: {reason}")
            n_failed += 1

    db_session.commit()

    log.info(
        "\nSummary: %d captured, %d failed, %d raw URLs corrected",
        n_captured, n_failed, n_url_corrected,
    )
    if failures:
        log.warning("Failures:")
        for f in failures:
            log.warning("  %s", f)


def run(execute: bool = False) -> None:
    from flask_app import app, db
    from hansard_archive.models import StatProducer

    with app.app_context():
        backfill(db.session, StatProducer, execute=execute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill licence_evidence_wayback_url for StatProducer rows where "
            "it is NULL. Also corrects known-broken licence_evidence_raw_url values. "
            "Dry run by default — pass --execute to write."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Perform Wayback lookups and write to the database.",
    )
    args = parser.parse_args()
    run(execute=args.execute)


if __name__ == "__main__":
    main()
