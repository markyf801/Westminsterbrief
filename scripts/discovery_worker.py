"""
Discovery worker — Phase 1.7.

Polls ha_stat_producer for authorised producers awaiting discovery, processes
one at a time, and writes candidate StatPublication rows.

Lifecycle per producer:
  pending → in_progress (atomic, prevents duplicate processing)
  in_progress → completed  (on success)
  in_progress → failed     (on any exception)

Railway service: discovery-worker
  - Start command: python scripts/discovery_worker.py
  - Environment vars: same as main service (DATABASE_URL, GEMINI_API_KEY)
  - DISCOVERY_POLL_INTERVAL — poll delay in seconds (default: 30)
  - DISCOVERY_DRY_RUN — if set to "1" or "true", suppresses writes to
    ha_stat_producer.discovery_status (in_progress / completed / failed
    transitions are skipped). It does NOT suppress candidate creation in
    ha_stat_publication — run_discovery() still runs in full and writes rows.
    Use this only to test the polling/state-machine logic without dirtying
    producer status. It is NOT a "safe no-write preview mode". To avoid
    writing any candidates, stop the worker service entirely.

Error handling:
  - Failures within a single producer's discovery are caught, recorded, and
    the worker continues polling for the next producer.
  - Unhandled exceptions in the outer loop are logged; Railway restarts the
    container automatically.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("discovery_worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

_POLL_INTERVAL = int(os.environ.get("DISCOVERY_POLL_INTERVAL", "30"))
_DRY_RUN = os.environ.get("DISCOVERY_DRY_RUN", "").lower() in ("1", "true")


def _pick_next_producer(db_session, StatProducer):
    """
    Atomically claim the oldest pending authorised producer.

    Uses a flush-based optimistic lock: read + update in one session flush.
    Railway runs a single worker instance, so this is sufficient; if multiple
    workers are ever needed, replace with SELECT FOR UPDATE.

    Returns the claimed StatProducer or None if nothing is pending.
    """
    producer = (
        db_session.query(StatProducer)
        .filter_by(authorisation_status="authorised", discovery_status="pending")
        .order_by(StatProducer.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if producer is None:
        return None
    if not _DRY_RUN:
        producer.discovery_status = "in_progress"
        db_session.flush()
        db_session.commit()
    return producer


def _process_producer(db_session, producer, gemini_key: str) -> None:
    """
    Run discovery for one producer and update its status.
    All exceptions are caught; the producer is marked failed on any error.
    """
    log.info("Processing producer: %s", producer.slug)
    try:
        from hansard_archive.discovery import run_discovery
        summary = run_discovery(producer, db_session, gemini_key)
        log.info("Discovery complete for %s: %s", producer.slug, summary)
        if not _DRY_RUN:
            producer.discovery_status        = "completed"
            producer.discovery_completed_at  = datetime.utcnow()
            producer.discovery_failure_reason = None
            db_session.commit()
    except Exception as exc:
        log.exception("Discovery failed for %s: %s", producer.slug, exc)
        try:
            db_session.rollback()
        except Exception:
            pass
        if not _DRY_RUN:
            try:
                producer.discovery_status         = "failed"
                producer.discovery_failure_reason = str(exc)[:1000]
                db_session.commit()
            except Exception as commit_exc:
                log.error("Could not record failure for %s: %s", producer.slug, commit_exc)


def run_worker() -> None:
    """Main worker loop. Polls for pending producers and processes them."""
    from flask_app import app, db
    from hansard_archive.models import StatProducer

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set — worker cannot classify candidates")
        sys.exit(1)

    if _DRY_RUN:
        log.info("DRY RUN mode — status transitions will not be written")

    log.info("Discovery worker started (poll_interval=%ds)", _POLL_INTERVAL)

    with app.app_context():
        while True:
            try:
                producer = _pick_next_producer(db.session, StatProducer)
                if producer is None:
                    log.debug("No pending producers — sleeping %ds", _POLL_INTERVAL)
                    time.sleep(_POLL_INTERVAL)
                    continue
                _process_producer(db.session, producer, gemini_key)
            except KeyboardInterrupt:
                log.info("Worker interrupted — shutting down")
                break
            except Exception as exc:
                log.exception("Unhandled exception in worker loop: %s", exc)
                time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    run_worker()
