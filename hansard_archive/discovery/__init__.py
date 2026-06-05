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
from datetime import datetime

log = logging.getLogger("discovery")

_BATCH_SIZE = 50

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
    import requests
    from hansard_archive.discovery.strategies import select_strategy
    from hansard_archive.discovery.classifier import classify_candidate
    from hansard_archive.discovery.pub_dates import resolve_publication_date
    from hansard_archive.models import StatPublication, StatPublicationTheme, THEME_TYPE_POLICY_AREA
    from hansard_archive.slugs import slugify_theme

    http_session = requests.Session()
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

    # Pre-classify dedup index (Phase 1.9): one pass over existing pubs → O(1)
    # lookups. Skips known URLs before paying for a Gemini classify. Shared with
    # re-discovery so the two paths cannot diverge.
    by_url, by_ds, slugs = _build_existing_index(producer.id, db_session)

    for batch_start in range(0, len(candidates_to_process), _BATCH_SIZE):
        batch = candidates_to_process[batch_start:batch_start + _BATCH_SIZE]
        batch_num += 1

        for cand in batch:
            # Pre-classify dedup: skip a known URL without a Gemini call. For a
            # known ONS dataset, refresh its latest-release date via the shared
            # helper (the a+b unification) — the only update-existing path.
            existing, is_ons = _lookup_existing(cand.url, by_url, by_ds)
            if existing is not None:
                skipped += 1
                if is_ons:
                    new_date = resolve_publication_date(cand.url, http_session)
                    if new_date and existing.first_published_at != new_date:
                        existing.first_published_at = new_date
                        existing.updated_at = datetime.utcnow()
                continue

            result = classify_candidate({
                "title":            cand.title,
                "url":              cand.url,
                "date_hints":       cand.date_hints,
                "description_hint": cand.description_hint,
            }, producer, gemini_key)
            if result is None:
                failed_classify += 1
                continue

            slug = slugify_theme(result["name"])
            if not slug:
                log.warning("run_discovery: empty slug for %r — skipping", result["name"])
                failed_classify += 1
                continue

            # Slug guard: a different URL classified to a slug we already hold
            # (e.g. a re-issued edition at a new URL) — skip, never duplicate.
            if slug in slugs:
                skipped += 1
                log.info("run_discovery: skip existing slug %s/%s (%s)",
                         producer.slug, slug, result["name"])
                continue

            written_slug = _insert_pub_from_result(producer, cand, result, db_session, http_session)
            if written_slug:
                slugs.add(written_slug)
                by_url[_normalise_pub_url(cand.url)] = True
                written += 1
                log.info("run_discovery: wrote candidate %s / %s", producer.slug, written_slug)

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


# ===========================================================================
# Pre-classify dedup + re-discovery (Phase 1.9 — keeps the catalogue current)
# ===========================================================================

def _normalise_pub_url(url: str) -> str:
    """Normalise a GOV.UK publication URL for dedup: lowercase scheme+host, drop
    query/fragment, strip trailing slash. So a trivial variant (trailing slash,
    tracking param, scheme/host case) dedups to the same publication rather than
    being re-classified and re-inserted as a duplicate."""
    from urllib.parse import urlparse
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return (url or "").strip().rstrip("/").lower()
    if not p.netloc:
        return (url or "").strip().rstrip("/").lower()
    return f"{(p.scheme or 'https').lower()}://{p.netloc.lower()}{p.path.rstrip('/')}"


def _ons_dataset_id(url: str) -> str | None:
    """Extract the ONS dataset-ID from either stored URL form
    (www.ons.gov.uk/datasets/{id} or api.beta.ons.gov.uk/v1/datasets/{id}/...).
    None for non-ONS URLs. This is the ONS dedup key AND the face-(b) trigger."""
    from urllib.parse import urlparse
    if "ons.gov.uk" not in (url or ""):
        return None
    parts = [x for x in urlparse(url).path.split("/") if x]
    if "datasets" in parts:
        i = parts.index("datasets")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _build_existing_index(producer_id: int, db_session):
    """One pass over the producer's publications → (by_url, by_dataset, slugs).
    O(1) dedup lookups per candidate instead of a query each."""
    from hansard_archive.models import StatPublication
    by_url: dict[str, object] = {}
    by_ds: dict[str, object] = {}
    slugs: set[str] = set()
    for pub in db_session.query(StatPublication).filter_by(producer_id=producer_id).all():
        slugs.add(pub.slug)
        ds = _ons_dataset_id(pub.url)
        if ds:
            by_ds[ds] = pub
        else:
            by_url[_normalise_pub_url(pub.url)] = pub
    return by_url, by_ds, slugs


def _lookup_existing(cand_url: str, by_url: dict, by_ds: dict):
    """Return (existing_pub_or_None, is_ons). ONS matches by dataset-ID,
    GOV.UK by normalised URL."""
    ds = _ons_dataset_id(cand_url)
    if ds is not None:
        return by_ds.get(ds), True
    return by_url.get(_normalise_pub_url(cand_url)), False


def _insert_pub_from_result(producer, cand, result, db_session, http_session):
    """Shared insert path for a classified candidate — used by BOTH initial
    discovery and re-discovery so they cannot diverge. Adds the StatPublication
    and its policy-area themes. first_published_at via the shared pub_dates
    helper (the single source of truth — same semantic as everywhere else).
    Returns the slug, or None if the slug is empty."""
    from hansard_archive.models import (
        StatPublication, StatPublicationTheme, THEME_TYPE_POLICY_AREA,
    )
    from hansard_archive.discovery.pub_dates import resolve_publication_date
    from hansard_archive.slugs import slugify_theme

    slug = slugify_theme(result["name"])
    if not slug:
        return None

    raw_cadence = result.get("update_cadence")
    if raw_cadence and raw_cadence not in _VALID_CADENCES:
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
        first_published_at=resolve_publication_date(cand.url, http_session),
    )
    db_session.add(pub)
    for area in result.get("policy_areas", []):
        db_session.add(StatPublicationTheme(
            publication=pub, theme=area,
            theme_type=THEME_TYPE_POLICY_AREA,
            model_used=result.get("model_used"),
        ))
    return slug


def run_rediscovery(producer, db_session, gemini_key, dry_run=False):
    """
    Re-discovery pass for an ALREADY-COMPLETED producer: find new publications
    cheaply, without disturbing initial-discovery state or the INC-007 date
    correctness.

    - Pre-classify dedup by normalised URL (GOV.UK) / dataset-ID (ONS): a known
      publication is skipped WITHOUT a Gemini call.
    - ONS a+b unification: a known dataset-ID also refreshes its latest-release
      date (the ONE update-existing path), routed through the SAME pub_dates
      helper as inserts — never a re-implementation.
    - Genuinely-new URLs are classified and inserted via the shared insert path.
    - INSERT-only for new; NEVER updates an existing GOV.UK first_published_at.
    - Leaves discovery_status untouched; stamps last_rediscovered_at.

    Returns a summary with the SKIP count prominent — a healthy run is mostly
    skips; a low skip count means the dedup isn't working.
    """
    import requests
    from hansard_archive.models import StatPublication
    from hansard_archive.discovery.strategies import select_strategy
    from hansard_archive.discovery.classifier import classify_candidate
    from hansard_archive.discovery.pub_dates import resolve_publication_date

    http = requests.Session()
    strategy = select_strategy(producer)
    candidates = strategy.fetch_candidates(producer)
    by_url, by_ds, slugs = _build_existing_index(producer.id, db_session)

    s = {"producer": producer.slug, "fetched": len(candidates),
         "skipped_known": 0, "new": 0, "ons_date_updated": 0,
         "classify_failed": 0, "slug_collision": 0}

    for cand in candidates:
        existing, is_ons = _lookup_existing(cand.url, by_url, by_ds)
        if existing is not None:
            s["skipped_known"] += 1
            if is_ons:
                # Face (b): refresh the ONS latest-release date via the shared
                # helper (same semantic as insert). The ONLY update-existing path.
                new_date = resolve_publication_date(cand.url, http)
                if new_date and existing.first_published_at != new_date:
                    s["ons_date_updated"] += 1
                    if not dry_run:
                        existing.first_published_at = new_date
                        existing.updated_at = datetime.utcnow()
            continue

        # Genuinely-new URL → classify.
        result = classify_candidate({
            "title": cand.title, "url": cand.url,
            "date_hints": cand.date_hints, "description_hint": cand.description_hint,
        }, producer, gemini_key)
        if result is None:
            s["classify_failed"] += 1
            continue
        from hansard_archive.slugs import slugify_theme
        slug = slugify_theme(result["name"])
        if not slug:
            s["classify_failed"] += 1
            continue
        if slug in slugs:
            # Different URL classified to a slug we already hold (e.g. a re-issued
            # edition at a new URL). The unique-constraint guard — skip, do not
            # touch the existing row.
            s["slug_collision"] += 1
            continue

        s["new"] += 1
        if not dry_run:
            written_slug = _insert_pub_from_result(producer, cand, result, db_session, http)
            if written_slug:
                slugs.add(written_slug)
                by_url[_normalise_pub_url(cand.url)] = True
        else:
            log.info("run_rediscovery[DRY]: would insert %s / %s (%.70s)",
                     producer.slug, slug, cand.url)

    if not dry_run:
        producer.last_rediscovered_at = datetime.utcnow()
        db_session.commit()

    log.info("run_rediscovery: %s", s)
    return s
