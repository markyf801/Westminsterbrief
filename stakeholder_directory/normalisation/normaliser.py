"""
Name normalisation pass — dedup tier engine.

Processes staging records with processing_status='pending' and produces:
  - New organisation records (Tier 4)
  - New engagement records (all tiers)
  - New alias records where staging name differs from canonical
  - Flag records for Tier 3 ambiguous cases
  - Updates staging records to 'committed'

Design spec: docs/stakeholder-directory-design.md, Section 6.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from stakeholder_directory.normalisation.string_utils import normalise_for_match
from stakeholder_directory.normalisation.similarity import compute_similarity
from stakeholder_directory.normalisation.commit import (
    commit_staging_record,
    commit_committee_evidence_record,
    commit_lobbying_record,
)
from stakeholder_directory.vocab import load_aliases, load_distinct_pairs

logger = logging.getLogger(__name__)

TIER2_THRESHOLD = 0.90
# Raised from 0.70 → 0.85 after first real-data run (DfE ministerial meetings,
# 2025): education-sector names sharing tokens ("Association", "National",
# "Schools") produced a 52% false-positive Tier 3 flag rate at 0.70.
# Raised further to 0.90 after second real-data run: at 0.85, 208 flags from
# 708 rows with notable false positives (NAHT 0.93 vs NASUWT; Sixth Form
# Colleges Association 0.93 vs Association of Colleges). 0.90 matches
# TIER2_THRESHOLD, so the effective tiers are now:
#   sim >= 0.90 + corroborator → Tier 2 (auto-merge)
#   sim >= 0.90, no corroborator → Tier 3 (flag for review)
#   sim < 0.90 → Tier 4 (new org)
TIER3_THRESHOLD = 0.90


@dataclass
class NormalisationResult:
    staging_records_processed: int = 0
    tier1_auto_merged: int = 0
    tier2_auto_merged: int = 0
    tier3_flagged: int = 0
    tier4_new_org: int = 0
    safety_blocks: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"NormalisationResult("
            f"processed={self.staging_records_processed}, "
            f"tier1={self.tier1_auto_merged}, "
            f"tier2={self.tier2_auto_merged}, "
            f"tier3_flagged={self.tier3_flagged}, "
            f"tier4_new={self.tier4_new_org}, "
            f"safety_blocks={self.safety_blocks}, "
            f"errors={len(self.errors)})"
        )


def _url_host(url: str | None) -> str | None:
    """Return the netloc (host) of a URL, or None if blank/unparseable."""
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower() or None
    except Exception:
        return None


def _independent_engagement_count(org) -> int:
    """Count engagements with distinct source_urls for this org."""
    urls = {e.source_url for e in org.engagements}
    return len(urls)


def _corroborator_match_between_orgs(org_a, org_b) -> bool:
    """True if two Organisation objects share a corroborating identifier."""
    host_a = _url_host(org_a.canonical_url)
    host_b = _url_host(org_b.canonical_url)
    if host_a and host_b and host_a == host_b:
        return True
    if (org_a.registration_number and org_b.registration_number
            and org_a.registration_number == org_b.registration_number):
        return True
    return False


def _corroborator_match_staging_to_org(staging_row, org) -> bool:
    """Corroborator check: staging row vs existing org.

    Staging rows carry no canonical_url or registration_number, so there is
    never a matching identifier to compare against the org. Tier 2 cannot fire
    on staging-to-org matches. All similarity-based staging matches fall to
    Tier 3 (flag for review). Tier 2 fires only in a future org-vs-org pass
    where both candidates carry identifiers.
    """
    return False


def _load_orgs_index(db, aliases_cfg: dict) -> tuple:
    """Load all existing organisations into in-memory lookup indices.

    Returns (orgs_list, norm_to_org, alias_norm_to_org).
    """
    from stakeholder_directory.models import Organisation

    orgs = db.session.query(Organisation).all()
    norm_to_org: dict[str, object] = {}
    alias_norm_to_org: dict[str, object] = {}

    for org in orgs:
        key = normalise_for_match(org.canonical_name, aliases_cfg)
        norm_to_org[key] = org
        for alias_obj in org.aliases:
            akey = normalise_for_match(alias_obj.alias_name, aliases_cfg)
            alias_norm_to_org[akey] = org

    return orgs, norm_to_org, alias_norm_to_org


def _create_new_org(canonical_name: str, db, dry_run: bool = False):
    """Create a new Organisation with type='unknown', scope='national', status='active'."""
    from stakeholder_directory.models import Organisation

    org = Organisation(
        canonical_name=canonical_name,
        type='unknown',
        scope='national',
        status='active',
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    if not dry_run:
        db.session.add(org)
        db.session.flush()
    return org


def _create_flag(org, detail: str, db, dry_run: bool = False):
    """Create a possible_duplicate Flag attached to org."""
    from stakeholder_directory.models import Flag

    flag = Flag(
        organisation_id=org.id,
        flag_type='possible_duplicate',
        detail=detail,
        raised_at=datetime.utcnow(),
        raised_by='ministerial_meeting_normaliser',
    )
    if not dry_run:
        db.session.add(flag)
    return flag


def _best_similarity_candidate(name: str, orgs: list, aliases_cfg: dict) -> tuple:
    """Return (org, similarity) for the highest-similarity existing org, or (None, 0.0)."""
    best_org = None
    best_sim = 0.0
    for org in orgs:
        sim = compute_similarity(name, org.canonical_name, aliases_cfg)
        if sim > best_sim:
            best_sim = sim
            best_org = org
    return best_org, best_sim


def _build_distinct_pairs_set(distinct_pairs: list, aliases_cfg: dict) -> set:
    """Convert raw distinct-pair entries to a set of frozensets of normalised names.

    Each entry in distinct_pairs is a two-element list. Alias expansion is applied
    so entries like "NASUWT" resolve to the same normalised form as the full name.
    """
    result = set()
    for pair in distinct_pairs:
        if len(pair) == 2:
            norm_a = normalise_for_match(pair[0], aliases_cfg)
            norm_b = normalise_for_match(pair[1], aliases_cfg)
            result.add(frozenset({norm_a, norm_b}))
    return result


def _is_in_distinct_pair(name_a: str, name_b: str, distinct_set: set, aliases_cfg: dict) -> bool:
    """True if name_a and name_b are listed as a known-distinct pair."""
    norm_a = normalise_for_match(name_a, aliases_cfg)
    norm_b = normalise_for_match(name_b, aliases_cfg)
    return frozenset({norm_a, norm_b}) in distinct_set


def _refresh_alias_index(org, alias_norm_to_org: dict, aliases_cfg: dict) -> None:
    """Re-index all aliases for an org in the in-memory lookup dict."""
    for alias_obj in org.aliases:
        akey = normalise_for_match(alias_obj.alias_name, aliases_cfg)
        alias_norm_to_org[akey] = org


def normalise_pending_staging(
    staging_table_name: str,
    batch_size: int = 100,
    dry_run: bool = False,
) -> NormalisationResult:
    """Process pending staging records through the dedup tier engine.

    Args:
        staging_table_name: 'staging_ministerial_meeting' (only table supported now).
        batch_size:         Max rows to process per run.
        dry_run:            Count without writing to the database.

    Returns:
        NormalisationResult with counts per tier.

    Raises:
        ValueError: if staging_table_name is not recognised.
    """
    from extensions import db

    if staging_table_name == 'staging_ministerial_meeting':
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting as StagingModel
        commit_fn = commit_staging_record
    elif staging_table_name == 'staging_committee_evidence':
        from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence as StagingModel
        commit_fn = commit_committee_evidence_record
    elif staging_table_name == 'staging_lobbying_entry':
        from stakeholder_directory.ingesters.staging import StagingLobbyingEntry as StagingModel
        commit_fn = commit_lobbying_record
    else:
        raise ValueError(
            f"Unknown staging table: {staging_table_name!r}. "
            "Supported: 'staging_ministerial_meeting', 'staging_committee_evidence', "
            "'staging_lobbying_entry'."
        )

    result = NormalisationResult()
    aliases_cfg = load_aliases()
    distinct_set = _build_distinct_pairs_set(load_distinct_pairs(), aliases_cfg)

    pending_rows = (
        db.session.query(StagingModel)
        .filter(StagingModel.processing_status == 'pending')
        .limit(batch_size)
        .all()
    )

    if not pending_rows:
        return result

    orgs, norm_to_org, alias_norm_to_org = _load_orgs_index(db, aliases_cfg)

    for staging_row in pending_rows:
        try:
            if not dry_run:
                # begin_nested() creates a SAVEPOINT. The context manager
                # releases it on success and rolls it back on exception,
                # so a flush()'d org is undone if engagement creation fails.
                with db.session.begin_nested():
                    _process_row(
                        staging_row, orgs, norm_to_org, alias_norm_to_org,
                        aliases_cfg, distinct_set, result, db, dry_run, commit_fn,
                    )
            else:
                _process_row(
                    staging_row, orgs, norm_to_org, alias_norm_to_org,
                    aliases_cfg, distinct_set, result, db, dry_run, commit_fn,
                )
        except Exception as exc:
            result.errors.append(
                f"Row id={staging_row.id} ({staging_row.raw_organisation_name!r}): {exc}"
            )
            logger.exception("Error normalising staging row id=%s", staging_row.id)

    if not dry_run:
        db.session.commit()

    return result


def _process_row(
    staging_row, orgs, norm_to_org, alias_norm_to_org,
    aliases_cfg, distinct_set, result, db, dry_run, commit_fn,
) -> None:
    """Run one staging row through Tier 1 → 2 → 3 → 4 and update result."""
    result.staging_records_processed += 1
    raw_name = staging_row.raw_organisation_name
    norm_name = normalise_for_match(raw_name, aliases_cfg)

    # -------------------------------------------------------------------
    # Tier 1 — exact match after normalisation (canonical or alias)
    # -------------------------------------------------------------------
    matched_org = norm_to_org.get(norm_name) or alias_norm_to_org.get(norm_name)
    if matched_org:
        commit_fn(staging_row, matched_org, dry_run=dry_run)
        if not dry_run:
            _refresh_alias_index(matched_org, alias_norm_to_org, aliases_cfg)
        result.tier1_auto_merged += 1
        return

    best_org, best_sim = _best_similarity_candidate(raw_name, orgs, aliases_cfg)

    # -------------------------------------------------------------------
    # Distinct-pair override: skip similarity tiers entirely for pairs
    # confirmed by human review as genuinely distinct organisations.
    # -------------------------------------------------------------------
    if (best_org is not None
            and best_sim >= TIER3_THRESHOLD
            and _is_in_distinct_pair(raw_name, best_org.canonical_name, distinct_set, aliases_cfg)):
        new_org = _create_new_org(raw_name, db, dry_run=dry_run)
        commit_fn(staging_row, new_org, dry_run=dry_run)
        if not dry_run:
            orgs.append(new_org)
            norm_to_org[norm_name] = new_org
        result.tier4_new_org += 1
        return

    # -------------------------------------------------------------------
    # Tier 2 — similarity ≥ 0.90 + corroborating identifier
    # -------------------------------------------------------------------
    if best_sim >= TIER2_THRESHOLD and best_org is not None:
        has_corroborator = _corroborator_match_staging_to_org(staging_row, best_org)
        if has_corroborator:
            # Safety rule: if existing org already has ≥ 2 independent engagements, flag instead
            if _independent_engagement_count(best_org) >= 2:
                result.safety_blocks += 1
                _do_tier3(
                    staging_row, best_org, best_sim,
                    "safety rule (existing org has ≥2 independent engagements)",
                    orgs, norm_to_org, aliases_cfg, result, db, dry_run, commit_fn,
                )
                return
            commit_fn(staging_row, best_org, dry_run=dry_run)
            if not dry_run:
                _refresh_alias_index(best_org, alias_norm_to_org, aliases_cfg)
            result.tier2_auto_merged += 1
            return
        # similarity ≥ 0.90 but no corroborator → Tier 3
        _do_tier3(
            staging_row, best_org, best_sim, "none",
            orgs, norm_to_org, aliases_cfg, result, db, dry_run, commit_fn,
        )
        return

    # -------------------------------------------------------------------
    # Tier 3 — similarity 0.70–0.90, no corroborator
    # -------------------------------------------------------------------
    if best_sim >= TIER3_THRESHOLD and best_org is not None:
        _do_tier3(
            staging_row, best_org, best_sim, "none",
            orgs, norm_to_org, aliases_cfg, result, db, dry_run, commit_fn,
        )
        return

    # -------------------------------------------------------------------
    # Tier 4 — genuinely new organisation
    # -------------------------------------------------------------------
    new_org = _create_new_org(raw_name, db, dry_run=dry_run)
    commit_fn(staging_row, new_org, dry_run=dry_run)
    if not dry_run:
        orgs.append(new_org)
        norm_to_org[norm_name] = new_org
    result.tier4_new_org += 1


def _do_tier3(
    staging_row, best_org, best_sim, corroborator_desc,
    orgs, norm_to_org, aliases_cfg, result, db, dry_run, commit_fn,
) -> None:
    """Create a provisional org + possible_duplicate flag for Tier 3."""
    raw_name = staging_row.raw_organisation_name
    detail = (
        f"Possible duplicate: '{raw_name}' (similarity {best_sim:.2f}) "
        f"↔ '{best_org.canonical_name}' (org_id={best_org.id}). "
        f"Corroborator: {corroborator_desc}."
    )
    new_org = _create_new_org(raw_name, db, dry_run=dry_run)
    _create_flag(new_org, detail, db, dry_run=dry_run)
    commit_fn(staging_row, new_org, dry_run=dry_run)
    if not dry_run:
        orgs.append(new_org)
        # Index by normalised name so subsequent occurrences of the same raw
        # name hit Tier 1 (exact match) rather than Tier 3 again — which would
        # create duplicate canonical_name records for the same organisation.
        norm_to_org[normalise_for_match(raw_name, aliases_cfg)] = new_org
    result.tier3_flagged += 1
