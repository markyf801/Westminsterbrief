"""
One-off script: update HESA/Jisc licence classification in ha_stat_producer.

hesa-jisc was seeded as HESA_Open (a guess). The correct terms page has been
located manually at https://www.hesa.ac.uk/about/website/terms — the bot-block
403 that prevented automated fetching does not apply to direct browsing.

The page explicitly distinguishes:
  - HESA statistical content: CC BY 4.0 — permits commercial use with attribution
  - Other Jisc-owned content on hesa.ac.uk: standard copyright

Westminster Brief uses HESA statistical content, so the applicable licence is
CC BY 4.0, which maps to Custom_Open in the registry vocabulary.

The update touches licence, licence_evidence_raw_url, and licence_evidence_wayback_url
in a single session.flush(). The before_update event listener fires ONCE, creating
one StatLicenceAuditLog entry capturing all three before/after value pairs.

Idempotent: re-running with the same target values is a no-op.
Dry-run is the default. Pass --execute to write.

Usage:
  python scripts/update_hesa_licence.py           # dry run
  python scripts/update_hesa_licence.py --execute  # write
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("update_hesa_licence")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_UPDATE = {
    "slug": "hesa-jisc",
    "licence": "Custom_Open",
    "licence_evidence_raw_url": "https://www.hesa.ac.uk/about/website/terms",
    "licence_evidence_wayback_url": (
        "http://web.archive.org/web/20240722052750/"
        "https://www.hesa.ac.uk/about/website/terms"
    ),
    "authorisation_reason": (
        "HESA statistical content published under CC BY 4.0 (per producer terms page). "
        "Permits commercial republication with attribution. Note: other Jisc-owned "
        "content on hesa.ac.uk is standard copyright — distinction matters if ingesting "
        "non-statistical content."
    ),
}

_TRACKED = ("licence", "licence_evidence_raw_url", "licence_evidence_wayback_url")


def _is_already_applied(producer, update: dict) -> bool:
    return all(getattr(producer, field) == update[field] for field in _TRACKED)


def update_producer(db_session, StatProducer, execute: bool = False) -> None:
    upd = _UPDATE
    producer = db_session.query(StatProducer).filter_by(slug=upd["slug"]).first()
    if producer is None:
        log.error("Producer not found: %s", upd["slug"])
        return

    if _is_already_applied(producer, upd):
        log.info("SKIP (already applied): %s", upd["slug"])
        return

    log.info(
        "%s  %s  licence: %s → %s",
        "[EXECUTE]" if execute else "[DRY RUN]",
        upd["slug"],
        producer.licence,
        upd["licence"],
    )
    log.info("  old_raw_url: %s", producer.licence_evidence_raw_url or "(null)")
    log.info("  new_raw_url: %s", upd["licence_evidence_raw_url"])
    log.info("  wayback_url: %s", upd["licence_evidence_wayback_url"][:70] + "…")

    if not execute:
        log.info("[DRY RUN] No writes. Pass --execute to apply.")
        return

    producer.licence = upd["licence"]
    producer.licence_evidence_raw_url = upd["licence_evidence_raw_url"]
    producer.licence_evidence_wayback_url = upd["licence_evidence_wayback_url"]
    producer.authorisation_reason = upd["authorisation_reason"]
    db_session.flush()
    db_session.commit()
    log.info("Done. hesa-jisc updated.")


def run(execute: bool = False) -> None:
    from flask_app import app, db
    from hansard_archive.models import StatProducer

    with app.app_context():
        update_producer(db.session, StatProducer, execute=execute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Update HESA/Jisc licence classification from HESA_Open to Custom_Open. "
            "Dry run by default — pass --execute to write."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Write update to the database.",
    )
    args = parser.parse_args()
    run(execute=args.execute)


if __name__ == "__main__":
    main()
