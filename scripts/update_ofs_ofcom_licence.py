"""
One-off script: update OfS and Ofcom licence classifications in ha_stat_producer.

Both producers were seeded as Unverified. Their licence pages have now been
located and confirmed:

  office-for-students: OGL_v3 — https://www.officeforstudents.org.uk/copyright/
  ofcom:               Custom_Open — https://www.ofcom.org.uk/about-ofcom/our-website/copyright
                       (Ofcom's own open licence, not OGL v3; individual publications
                        may carry OGL v3 separately — to be captured at StatPublication
                        level when ingested)

Each update touches licence, licence_evidence_raw_url, and licence_evidence_wayback_url
in a single session.flush(). The before_update event listener fires ONCE per producer,
creating one StatLicenceAuditLog entry that captures all three before/after pairs.

Idempotent: if a producer already has the target values, the script skips it.
Dry-run is the default. Pass --execute to write.

Usage:
  python scripts/update_ofs_ofcom_licence.py           # dry run
  python scripts/update_ofs_ofcom_licence.py --execute  # write
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("update_ofs_ofcom")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_UPDATES = [
    {
        "slug": "office-for-students",
        "licence": "OGL_v3",
        "licence_evidence_raw_url": "https://www.officeforstudents.org.uk/copyright/",
        "licence_evidence_wayback_url": (
            "http://web.archive.org/web/20260211160519/"
            "https://www.officeforstudents.org.uk/copyright/"
        ),
        "authorisation_reason": (
            "Licence confirmed from producer's dedicated copyright page (OGL v3.0)"
        ),
    },
    {
        "slug": "ofcom",
        "licence": "Custom_Open",
        "licence_evidence_raw_url": (
            "https://www.ofcom.org.uk/about-ofcom/our-website/copyright"
        ),
        "licence_evidence_wayback_url": (
            "http://web.archive.org/web/20260130060741/"
            "https://www.ofcom.org.uk/about-ofcom/our-website/copyright"
        ),
        "authorisation_reason": (
            "Ofcom website operates under custom permissive terms (free reproduction "
            "with attribution). Individual Ofcom publications may carry OGL v3 "
            "separately — to be captured at StatPublication level when ingested."
        ),
    },
]

_TRACKED = ("licence", "licence_evidence_raw_url", "licence_evidence_wayback_url")


def _is_already_applied(producer, update: dict) -> bool:
    return all(
        getattr(producer, field) == update[field]
        for field in _TRACKED
    )


def update_producers(db_session, StatProducer, execute: bool = False) -> None:
    updated = 0
    skipped = 0

    for upd in _UPDATES:
        producer = db_session.query(StatProducer).filter_by(slug=upd["slug"]).first()
        if producer is None:
            log.error("Producer not found: %s — skipping", upd["slug"])
            continue

        if _is_already_applied(producer, upd):
            log.info("SKIP (already applied): %s", upd["slug"])
            skipped += 1
            continue

        log.info(
            "%s  %s  licence: %s → %s",
            "[EXECUTE]" if execute else "[DRY RUN]",
            upd["slug"],
            producer.licence,
            upd["licence"],
        )
        log.info("  raw_url:     %s", upd["licence_evidence_raw_url"])
        log.info("  wayback_url: %s", upd["licence_evidence_wayback_url"][:70] + "…")

        if not execute:
            continue

        producer.licence = upd["licence"]
        producer.licence_evidence_raw_url = upd["licence_evidence_raw_url"]
        producer.licence_evidence_wayback_url = upd["licence_evidence_wayback_url"]
        producer.authorisation_reason = upd["authorisation_reason"]
        db_session.flush()
        updated += 1

    if execute:
        db_session.commit()
        log.info("Done. %d producer(s) updated, %d skipped (already applied).", updated, skipped)
    else:
        log.info(
            "[DRY RUN] %d producer(s) would be updated, %d already applied. "
            "Pass --execute to write.",
            len(_UPDATES) - skipped, skipped,
        )


def run(execute: bool = False) -> None:
    from flask_app import app, db
    from hansard_archive.models import StatProducer

    with app.app_context():
        update_producers(db.session, StatProducer, execute=execute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Update OfS and Ofcom licence classifications. "
            "Dry run by default — pass --execute to write."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Write updates to the database.",
    )
    args = parser.parse_args()
    run(execute=args.execute)


if __name__ == "__main__":
    main()
