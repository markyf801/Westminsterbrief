"""
URL validation stub for the stakeholder directory.

Full behaviour spec: docs/stakeholder-directory-design.md, Section 7.

STUB — not yet implemented. The function signature is final; the body is a
no-op that logs intent. When the real implementation lands, all call sites
(ingesters) require no changes.

Full implementation will add:
- HEAD request with 5-second timeout
- Shared worker pool, 10 concurrent max
- Per-host rate limit: 5 requests/second
- On 404 / timeout / SSL error / connection refused: raise url_dead flag,
  still create the record
- On parked-domain signatures: raise url_parked flag
- Log results to url_validation_log table (reserved for revalidation passes)

IMPORTANT — source_url must also be validated. This function is currently
called on canonical_url and evidence_url. The full implementation must also
be called on engagement source_url values at ingestion time. Non-200
responses on source_url should raise url_dead flags on the engagement record.
The DfE pilot ingest demonstrated the consequence of skipping this: broken
committee evidence URLs (missing /html/ suffix) and non-resolving ministerial
meeting URLs were stored silently and required manual post-hoc discovery.
See docs/stakeholder-directory-design.md Section 7 for full spec.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    url: str
    reachable: bool
    status_code: int | None
    flag_raised: str | None   # flag_type value, or None if clean
    detail: str | None        # human-readable note for the flag, or None


def validate_url_or_flag(
    url: str,
    org_id: int,
    engagement_id: int | None = None,
) -> ValidationResult:
    """Validate a URL and raise a flag on the associated record if unreachable.

    Call this on every URL before persisting an engagement or organisation record.
    See docs/stakeholder-directory-design.md Section 7 for the full behaviour spec.

    Args:
        url:           The URL to validate (canonical_url or evidence_url).
        org_id:        The sd_organisation.id this URL belongs to.
        engagement_id: The sd_engagement.id, if the URL is on an engagement record.
                       None if validating an organisation's canonical_url.

    Returns:
        ValidationResult with reachable=True and flag_raised=None for all URLs
        until the real implementation is installed.
    """
    logger.info(
        'URL validator not yet implemented — would validate: %s '
        '(org_id=%s, engagement_id=%s)',
        url, org_id, engagement_id,
    )
    return ValidationResult(
        url=url,
        reachable=True,
        status_code=None,
        flag_raised=None,
        detail=None,
    )
