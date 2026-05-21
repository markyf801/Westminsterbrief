"""
hansard_archive.discovery — publication discovery skill.

Entry point: run_discovery(producer, db_session, gemini_key)

Pipeline:
  1. select_strategy(producer) — picks the right fetcher
  2. strategy.fetch_candidates(producer) — returns list[CandidateItem]
  3. classify_candidate(cand, producer, gemini_key) — LLM decision per item
  4. Write passing candidates to ha_stat_publication as authorisation_status='candidate'

Returns a summary dict so the caller (discovery_worker.py) can log results.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("discovery")


def run_discovery(producer, db_session, gemini_key: str) -> dict:
    """
    Run the full discovery pipeline for a single StatProducer.

    Returns:
        {
          "fetched":         int,   # raw candidates from strategy
          "classified":      int,   # passed LLM classification
          "written":         int,   # new rows inserted
          "skipped":         int,   # already-existing (slug collision)
          "failed_classify": int,   # LLM failures / not-a-publication
        }

    Raises on ManualStrategy or unrecoverable fetch failure — caller must
    catch and record on the producer row.
    """
    from hansard_archive.discovery.strategies import select_strategy
    from hansard_archive.discovery.classifier import classify_candidate
    from hansard_archive.models import StatPublication
    from hansard_archive.slugs import slugify_theme

    strategy = select_strategy(producer)
    log.info("run_discovery: producer=%s strategy=%s",
             producer.slug, type(strategy).__name__)

    # Raises for ManualStrategy — propagated to caller
    candidates_raw = strategy.fetch_candidates(producer)
    log.info("run_discovery: fetched %d candidates for %s",
             len(candidates_raw), producer.slug)

    written = skipped = failed_classify = 0

    for cand in candidates_raw:
        cand_dict = {
            "title":            cand.title,
            "url":              cand.url,
            "date_hints":       cand.date_hints,
            "description_hint": cand.description_hint,
        }
        result = classify_candidate(cand_dict, producer, gemini_key)
        if result is None:
            failed_classify += 1
            continue

        slug = slugify_theme(result["name"])
        if not slug:
            log.warning("run_discovery: empty slug for %r — skipping", result["name"])
            failed_classify += 1
            continue

        existing = (
            db_session.query(StatPublication)
            .filter_by(producer_id=producer.id, slug=slug)
            .first()
        )
        if existing:
            skipped += 1
            log.debug("run_discovery: skip existing %s/%s", producer.slug, slug)
            continue

        pub = StatPublication(
            producer_id=producer.id,
            slug=slug,
            name=result["name"],
            url=cand.url,
            description=result.get("description"),
            update_cadence=result.get("update_cadence"),
            subject_area=result.get("subject_area"),
            authorisation_status="candidate",
            discovered_at=datetime.utcnow(),
        )
        db_session.add(pub)
        db_session.flush()
        written += 1
        log.info("run_discovery: wrote candidate %s / %s", producer.slug, slug)

    db_session.commit()

    summary = {
        "fetched":         len(candidates_raw),
        "classified":      len(candidates_raw) - failed_classify,
        "written":         written,
        "skipped":         skipped,
        "failed_classify": failed_classify,
    }
    log.info("run_discovery: complete — %s", summary)
    return summary
