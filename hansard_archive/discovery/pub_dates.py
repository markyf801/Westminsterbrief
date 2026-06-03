"""
Shared publication-date resolution for the stats catalogue (Phase 1.9).

SINGLE SOURCE OF TRUTH for "when did the source first publish this release":
  - GOV.UK: Content API `first_published_at`
  - ONS:    datasets API `release_date`

Why this module exists: the GOV.UK SEARCH API (used by the discovery worker to
list candidates) does NOT expose first_published_at — it only returns
public_timestamp (last-updated). Relying on Search API date hints silently
captured the wrong (updated) date. Both the discovery worker (A2) and the
backfill/compare script (A3) import these helpers so they cannot diverge again.

All functions return a datetime.date or None (never raise). None means the
source did not provide a usable date, or the fetch failed after retries.
"""
from __future__ import annotations

import logging
import time
from datetime import date as date_type
from urllib.parse import urlparse

import requests

log = logging.getLogger("discovery.pub_dates")

GOVUK_CONTENT_API = "https://www.gov.uk/api/content"
ONS_DATASETS_API  = "https://api.beta.ons.gov.uk/v1/datasets"
UA       = "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"
_TIMEOUT = 20


def _parse_iso_date(raw) -> date_type | None:
    """Parse the leading YYYY-MM-DD of an ISO datetime string. None on failure."""
    try:
        return date_type.fromisoformat(raw[:10])
    except (ValueError, TypeError, AttributeError):
        return None


def _get_json(url: str, http: requests.Session | None) -> dict | None:
    """GET with up to 3 attempts and 429 backoff. Returns parsed JSON or None."""
    sess = http or requests
    for attempt in range(3):
        try:
            resp = sess.get(url, timeout=_TIMEOUT, headers={"User-Agent": UA})
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1) * 5
                log.warning("429 rate-limited — waiting %ds: %s", wait, url)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                log.info("404 for %s", url)
                return None
            if not resp.ok:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Fetch error (attempt %d) for %s: %s", attempt + 1, url, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def fetch_govuk_first_published(pub_url: str, http: requests.Session | None = None) -> date_type | None:
    """GOV.UK Content API first_published_at for a www.gov.uk publication URL."""
    path = urlparse(pub_url).path
    data = _get_json(f"{GOVUK_CONTENT_API}{path}", http)
    if not data:
        return None
    return _parse_iso_date(data.get("first_published_at", ""))


def fetch_ons_release_date(pub_url: str, http: requests.Session | None = None) -> date_type | None:
    """
    ONS release_date. The date lives on the VERSION, not the dataset root:
      dataset root → links.latest_version.href → version.release_date

    Handles both stored forms of the URL:
      - https://www.ons.gov.uk/datasets/{dataset_id}
      - https://api.beta.ons.gov.uk/v1/datasets/{dataset_id}/editions/.../versions/N
    """
    # Already a version endpoint — read release_date directly.
    if "api.beta.ons.gov.uk" in pub_url and "/versions/" in pub_url:
        version = _get_json(pub_url, http)
        return _parse_iso_date(version.get("release_date", "")) if version else None

    parts = [p for p in urlparse(pub_url).path.split("/") if p]
    # www.ons.gov.uk/datasets/{id}  →  parts = ["datasets", "{id}", ...]
    dataset_id = None
    if "datasets" in parts:
        idx = parts.index("datasets")
        if idx + 1 < len(parts):
            dataset_id = parts[idx + 1]
    if not dataset_id:
        log.warning("ONS URL has no extractable dataset id: %s", pub_url)
        return None

    root = _get_json(f"{ONS_DATASETS_API}/{dataset_id}", http)
    if not root:
        return None
    # Some datasets expose release_date on the root; prefer it when present.
    direct = _parse_iso_date(root.get("release_date", ""))
    if direct:
        return direct
    # Otherwise follow latest_version → version.release_date (the usual case).
    version_href = ((root.get("links") or {}).get("latest_version") or {}).get("href", "")
    if not version_href:
        return None
    version = _get_json(version_href, http)
    if not version:
        return None
    return _parse_iso_date(version.get("release_date", ""))


def resolve_publication_date(pub_url: str, http: requests.Session | None = None) -> date_type | None:
    """
    Dispatch by host: GOV.UK → Content API; ONS → datasets API.
    Returns the source publication date, or None for unrecognised hosts or
    when the source provides no date.
    """
    if pub_url.startswith("https://www.gov.uk/"):
        return fetch_govuk_first_published(pub_url, http)
    if "ons.gov.uk" in pub_url:
        return fetch_ons_release_date(pub_url, http)
    return None
