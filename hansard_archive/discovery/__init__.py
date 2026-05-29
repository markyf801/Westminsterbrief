"""
hansard_archive.discovery — publication discovery skill.

Entry point: run_discovery(producer, db_session, gemini_key)

Pipeline:
  1. select_strategy(producer) — picks the right fetcher
  2. strategy.fetch_candidates(producer) — returns list[CandidateItem]
  3. classify_candidate(cand, producer, gemini_key) — LLM decision per item
  4. Write passing candidates to ha_stat_publication as authorisation_status='candidate'

Candidates are processed in batches of _BATCH_SIZE. Each batch is committed
independently, and producer.candidates_processed_count is updated after each
commit. On worker restart for an in_progress producer, the first
candidates_processed_count candidates are skipped and processing resumes from
the next one.

Returns a summary dict so the caller (discovery_worker.py) can log results.
"""
from __future__ import annotations

import logging
from datetime import date as date_type, datetime

log = logging.getLogger("discovery")

_BATCH_SIZE = 50


def _parse_date_hint(hints: list[str]) -> date_type | None:
    """Return the first parseable YYYY-MM-DD date from date_hints, or None."""
    for hint in hints:
        try:
            return date_type.fromisoformat(hint[:10])
        except (ValueError, TypeError, AttributeError):
            continue
    return None

_VALID_CADENCES = frozenset({
    "daily", "weekly", "monthly", "quarterly",
    "annual", "biennial", "ad_hoc", "one_off",
})


def run_discovery(producer, db_session, gemini_key: str) -> dict:
    """
    Run the full discovery pipeline for a single StatProducer.

    Returns:
        {
          "fetched":         int,   # raw candidates from strategy
          "llm_passed":      int,   # passed LLM classification (this run)
          "written":         int,   # new rows inserted (this run)
          "skipped":         int,   # already-existing slug collisions (this run)
          "failed_classify": int,   # LLM failures / not-a-publication (this run)
          "resume_offset":   int,   # candidates skipped at start (0 for fresh run)
        }

    Raises on ManualStrategy or unrecoverable fetch failure — caller must
    catch and record on the producer row.
    """
    from hansard_archive.discovery.strategies import select_strategy
    from hansard_archive.discovery.classifier import classify_candidate
    from hansard_archive.models import StatPublication, StatPublicationTheme, THEME_TYPE_POLICY_AREA
    from hansard_archive.slugs import slugify_theme

    strategy = select_strategy(producer)
    log.info("run_discovery: producer=%s strategy=%s",
             producer.slug, type(strategy).__name__)

    # Raises for ManualStrategy — propagated to caller
    candidates_raw = strategy.fetch_candidates(producer)
    total_fetched = len(candidates_raw)
    log.info("run_discovery: fetched %d candidates for %s",
             total_fetched, producer.slug)

    # Resume: skip candidates already processed in a previous interrupted run
    resume_offset = producer.candidates_processed_count or 0
    if resume_offset > 0:
        log.info("run_discovery: resuming — skipping first %d already-processed "
                 "candidates of %d total", resume_offset, total_fetched)
    candidates_to_process = candidates_raw[resume_offset:]

    written = skipped = failed_classify = 0
    batch_num = 0

    for batch_start in range(0, len(candidates_to_process), _BATCH_SIZE):
        batch = candidates_to_process[batch_start:batch_start + _BATCH_SIZE]
        batch_num += 1

        for cand in batch:
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
                log.info("run_discovery: skip existing %s/%s (%s)",
                         producer.slug, slug, result["name"])
                continue

            raw_cadence = result.get("update_cadence")
            if raw_cadence and raw_cadence not in _VALID_CADENCES:
                log.warning(
                    "run_discovery: invalid update_cadence %r for %s/%s — setting NULL",
                    raw_cadence, producer.slug, slug,
                )
                raw_cadence = None

            pub = StatPublication(
                producer_id=producer.id,
                slug=slug,
                name=result["name"],
                url=cand.url,
                description=result.get("description"),
                update_cadence=raw_cadence,
                subject_area=result.get("subject_area"),
                authorisation_status="candidate",
                discovered_at=datetime.utcnow(),
                # GOV.UK: public_timestamp (last updated) from Search API.
                # ONS: release_date from datasets API. NULL if neither provides it.
                first_published_at=_parse_date_hint(cand.date_hints),
            )
            db_session.add(pub)
            for area in result.get("policy_areas", []):
                db_session.add(StatPublicationTheme(
                    publication=pub,
                    theme=area,
                    theme_type=THEME_TYPE_POLICY_AREA,
                    model_used=result.get("model_used"),
                ))
            written += 1
            log.info("run_discovery: wrote candidate %s / %s", producer.slug, slug)

        # Commit this batch and record progress atomically
        new_count = resume_offset + batch_start + len(batch)
        producer.candidates_processed_count = new_count
        db_session.commit()
        log.info("run_discovery: batch %d complete, %d candidates processed of %d total",
                 batch_num, new_count, total_fetched)

    summary = {
        "fetched":         total_fetched,
        "llm_passed":      len(candidates_to_process) - failed_classify,
        "written":         written,
        "skipped":         skipped,
        "failed_classify": failed_classify,
        "resume_offset":   resume_offset,
    }
    log.info("run_discovery: complete — %s", summary)
    return summary
