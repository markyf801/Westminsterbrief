"""
Committee evidence ingester for the stakeholder directory.

Pulls oral and written evidence from the Parliament committees API,
extracts witnesses (organisations and affiliated individuals), and stages
them for normalisation through the existing dedup-tier pipeline.

Design spec: docs/stakeholder-directory-design.md, Section 5.

API base: https://committees.parliament.uk/api
Endpoints used:
  GET /OralEvidence   — panel session witnesses
  GET /WrittenEvidence — written submissions
  GET /Committees     — committee metadata (id, name, house)
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import requests

from stakeholder_directory.vocab import load_internal_government

logger = logging.getLogger(__name__)

_API_BASE = 'https://committees-api.parliament.uk/api'
_ORAL_ENDPOINT = f'{_API_BASE}/OralEvidence'
_WRITTEN_ENDPOINT = f'{_API_BASE}/WrittenEvidence'
_COMMITTEES_ENDPOINT = f'{_API_BASE}/Committees'

_PAGE_SIZE = 50
_TIMEOUT = 30

_HOUSE_MAP = {1: 'Commons', 2: 'Lords', 4: 'Joint'}


@dataclass
class IngestionResult:
    publications_fetched: int = 0
    witnesses_processed: int = 0
    rows_staged: int = 0
    rows_skipped_internal_govt: int = 0
    rows_skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"IngestionResult("
            f"publications={self.publications_fetched}, "
            f"witnesses={self.witnesses_processed}, "
            f"staged={self.rows_staged}, "
            f"skipped_govt={self.rows_skipped_internal_govt}, "
            f"skipped_dup={self.rows_skipped_duplicate}, "
            f"errors={len(self.errors)})"
        )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (compatible; WestminsterBriefBot/1.0)',
}


def _fetch_with_retry(url: str, params: dict) -> dict:
    """GET url with params. Retries once on 503. Returns parsed JSON or {}."""
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            if resp.status_code == 503 and attempt == 0:
                logger.warning("503 from %s — retrying in 2s", url)
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            if attempt == 0:
                logger.warning("Request error %s — retrying: %s", url, exc)
                time.sleep(2)
            else:
                logger.warning("Request error %s (final): %s", url, exc)
    return {}


def _fetch_committees(committee_ids: list[int]) -> dict[int, dict]:
    """Return {committee_id: {name, house}} for the requested IDs."""
    data = _fetch_with_retry(_COMMITTEES_ENDPOINT, {'take': 500})
    items = data.get('items') or []
    result: dict[int, dict] = {}
    for item in items:
        cid = item.get('id')
        if cid in committee_ids:
            house_val = item.get('house', '')
            house = _HOUSE_MAP.get(house_val, str(house_val)) if isinstance(house_val, int) else str(house_val)
            result[cid] = {
                'name': item.get('name') or item.get('displayName', ''),
                'house': house,
            }
    for cid in committee_ids:
        if cid not in result:
            result[cid] = {'name': f'Committee {cid}', 'house': ''}
    return result


def _fetch_all_publications(
    endpoint: str,
    committee_id: int,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Paginate through all publications for a committee in the date range."""
    publications: list[dict] = []
    skip = 0
    while True:
        data = _fetch_with_retry(endpoint, {
            'committeeId': committee_id,
            'StartDate': start_date.isoformat(),
            'EndDate': end_date.isoformat(),
            'skip': skip,
            'take': _PAGE_SIZE,
        })
        items = data.get('items') or []
        publications.extend(items)
        total = data.get('totalResults', 0)
        skip += len(items)
        if skip >= total or not items:
            break
    return publications


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _is_internal_government(name: str, variants: list[str]) -> bool:
    """True if any internal-government variant matches name as a whole word (case-insensitive)."""
    name_lower = name.lower()
    return any(re.search(r'\b' + re.escape(v.lower()) + r'\b', name_lower) for v in variants)


def _split_org_names(raw: str) -> list[str]:
    """Split on '; ' to handle multi-org entries. Returns non-empty stripped names."""
    return [p.strip() for p in raw.split(';') if p.strip()]


def _extract_orgs_from_witness(witness: dict) -> list[tuple[str, str | None]]:
    """Return [(org_name, attendee_role)] from one witness record.

    For Organisation-type witnesses: org names come from organisations[].name;
    attendee_role is the role string (if any).
    For Individual-type witnesses: org names from organisations[].name;
    attendee_role is 'person_name, role' for provenance context.
    Individuals with no org affiliation are skipped (we track orgs, not persons).
    """
    orgs = witness.get('organisations') or []
    if not orgs:
        return []

    submitter_type = witness.get('submitterType', '')
    person_name = witness.get('name', '')
    results = []

    for org in orgs:
        org_name = (org.get('name') or '').strip()
        if not org_name:
            continue
        org_role = (org.get('role') or '').strip()

        if submitter_type == 'Individual':
            role_parts = [person_name]
            if org_role:
                role_parts.append(org_role)
            attendee_role = ', '.join(role_parts) or None
        else:
            attendee_role = org_role or None

        results.append((org_name, attendee_role))

    return results


def _get_inquiry_info(publication: dict, pub_type: str) -> tuple[str | None, str | None]:
    """Return (inquiry_id, inquiry_title) from publication metadata.

    The Parliament committees API returns 'id' and 'title' on committee business
    objects — not 'businessId'/'name' as earlier assumed from fixture data.
    Correct base URL: https://committees-api.parliament.uk/api
    (https://committees.parliament.uk/api returns 403 from automated clients.)
    """
    if pub_type == 'oral_evidence':
        businesses = publication.get('committeeBusinesses') or []
        if businesses:
            b = businesses[0]
            return str(b['id']) if b.get('id') else None, b.get('title')
    else:
        b = publication.get('committeeBusiness') or {}
        if b:
            return str(b['id']) if b.get('id') else None, b.get('title')
    return None, None


def _source_url(publication_id: int, pub_type: str) -> str:
    if pub_type == 'oral_evidence':
        return f'https://committees.parliament.uk/oralevidence/{publication_id}/html/'
    return f'https://committees.parliament.uk/writtenevidence/{publication_id}/html/'


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _stage_one(
    staging_model,
    committee_id: int,
    committee_meta: dict,
    publication: dict,
    pub_type: str,
    org_name: str,
    attendee_role: str | None,
    pub_date: date | None,
    inquiry_id: str | None,
    inquiry_title: str | None,
    source_url: str,
    source_type: str,
    internal_govt_variants: list[str],
    result: 'IngestionResult',
    dry_run: bool,
) -> None:
    """Stage a single (publication, org_name) pair after applying filters."""
    if _is_internal_government(org_name, internal_govt_variants):
        result.rows_skipped_internal_govt += 1
        return

    if dry_run:
        result.rows_staged += 1
        return

    from extensions import db

    pub_id = publication.get('id')
    existing = db.session.query(staging_model).filter_by(
        publication_id=pub_id,
        raw_organisation_name=org_name,
    ).first()
    if existing:
        result.rows_skipped_duplicate += 1
        return

    row = staging_model(
        committee_id=committee_id,
        committee_name=committee_meta.get('name', ''),
        committee_house=committee_meta.get('house', ''),
        publication_id=pub_id,
        publication_type=source_type,
        publication_date=pub_date,
        inquiry_id=inquiry_id,
        inquiry_title=inquiry_title,
        raw_organisation_name=org_name,
        attendee_role=attendee_role,
        source_url=source_url,
        processing_status='pending',
        source_json=json.dumps(publication),
    )
    db.session.add(row)
    result.rows_staged += 1


def _process_publication(
    publication: dict,
    pub_type: str,
    committee_id: int,
    committee_meta: dict,
    internal_govt_variants: list[str],
    staging_model,
    result: 'IngestionResult',
    dry_run: bool,
) -> None:
    """Extract witnesses from one publication and stage each org."""
    result.publications_fetched += 1

    pub_id = publication.get('id')
    if pub_id is None:
        logger.warning("Publication missing 'id' field — skipping")
        return

    pub_date_raw = publication.get('publicationDate', '')
    try:
        pub_date = datetime.fromisoformat(pub_date_raw[:10]).date() if pub_date_raw else None
    except (ValueError, TypeError):
        pub_date = None

    inquiry_id, inquiry_title = _get_inquiry_info(publication, pub_type)
    source_url = _source_url(pub_id, pub_type)
    source_type = (
        'oral_evidence_committee' if pub_type == 'oral_evidence'
        else 'written_evidence_committee'
    )

    witnesses = publication.get('witnesses') or []
    for witness in witnesses:
        org_entries = _extract_orgs_from_witness(witness)
        if not org_entries:
            continue

        result.witnesses_processed += 1
        for raw_name, attendee_role in org_entries:
            for org_name in _split_org_names(raw_name):
                _stage_one(
                    staging_model,
                    committee_id,
                    committee_meta,
                    publication,
                    pub_type,
                    org_name,
                    attendee_role,
                    pub_date,
                    inquiry_id,
                    inquiry_title,
                    source_url,
                    source_type,
                    internal_govt_variants,
                    result,
                    dry_run,
                )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest_committee_evidence(
    committee_ids: list[int],
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> IngestionResult:
    """Ingest oral and written committee evidence for the given committee IDs.

    Args:
        committee_ids: Parliament committee IDs (e.g. [203, 127]).
        start_date:    Inclusive start of the publication date range.
        end_date:      Inclusive end of the publication date range.
        dry_run:       Count without writing to the database.

    Returns:
        IngestionResult with counts per action.
    """
    from stakeholder_directory.ingesters.staging import StagingCommitteeEvidence

    result = IngestionResult()
    internal_govt_variants = load_internal_government()
    committee_meta_map = _fetch_committees(committee_ids)

    for pub_type, endpoint in [
        ('oral_evidence', _ORAL_ENDPOINT),
        ('written_evidence', _WRITTEN_ENDPOINT),
    ]:
        for committee_id in committee_ids:
            committee_meta = committee_meta_map.get(committee_id, {})
            try:
                publications = _fetch_all_publications(
                    endpoint, committee_id, start_date, end_date,
                )
            except Exception as exc:
                msg = f"Failed to fetch {pub_type} for committee {committee_id}: {exc}"
                result.errors.append(msg)
                logger.exception("Failed to fetch %s for committee %d", pub_type, committee_id)
                continue

            for publication in publications:
                try:
                    _process_publication(
                        publication,
                        pub_type,
                        committee_id,
                        committee_meta,
                        internal_govt_variants,
                        StagingCommitteeEvidence,
                        result,
                        dry_run,
                    )
                except Exception as exc:
                    pub_id = publication.get('id', '?')
                    msg = f"Error processing {pub_type} publication {pub_id}: {exc}"
                    result.errors.append(msg)
                    logger.exception("Error processing %s publication %s", pub_type, pub_id)

    if not dry_run:
        from extensions import db
        db.session.commit()

    return result
