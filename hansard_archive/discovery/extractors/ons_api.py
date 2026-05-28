"""
ONS Beta API extractor — piece 3 of Phase 1.8 data_url work.

Handles www.ons.gov.uk/datasets/{dataset-id} pages by deriving the dataset ID
and calling the ONS Beta API for structured download links. Two fetches per
publication:
  1. /v1/datasets/{id}          → title + links.latest_version.href
  2. {latest_version.href}      → downloads object (csv, xls, csvw)

csvw (JSON metadata) is excluded. Classification is always NULL — the ONS API
has no section heading structure.
"""
from __future__ import annotations

import logging
import time
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import urlparse

import requests

from .base import BaseExtractor, ExtractedFile, ExtractionResult

log = logging.getLogger(__name__)

_ONS_API_ROOT       = "https://api.beta.ons.gov.uk/v1/datasets/"
_ONS_LANDING_PREFIX = "https://www.ons.gov.uk/datasets/"

HEADERS = {
    "User-Agent": "WestminsterBriefBot/1.0 (+https://westminsterbrief.co.uk/)",
    "Accept":     "application/json",
}

# csvw is a JSON metadata file, not a data file — excluded
_DATA_FORMATS: frozenset[str] = frozenset({"csv", "xls", "xlsx"})
_EXT_NORM: dict[str, str]     = {"csv": "csv", "xls": "xlsx", "xlsx": "xlsx"}


class OnsApiExtractor(BaseExtractor):

    def extract(self, url: str, http_session: requests.Session) -> ExtractionResult:
        dataset_id = _dataset_id_from_url(url)
        if not dataset_id:
            return ExtractionResult(status="not_extractable",
                                    error=f"cannot derive ONS dataset ID from URL: {url}")

        # Step 1: dataset metadata — title + latest_version href
        meta = _fetch_json(_ONS_API_ROOT + dataset_id, http_session)
        if meta is None:
            return ExtractionResult(status="fetch_failed",
                                    error="dataset metadata request failed")

        title       = meta.get("title") or None
        latest_href = (meta.get("links") or {}).get("latest_version", {}).get("href")
        if not latest_href:
            return ExtractionResult(status="no_files_found")

        # Step 2: version metadata — downloads object
        version = _fetch_json(latest_href, http_session)
        if version is None:
            return ExtractionResult(status="fetch_failed",
                                    error="version metadata request failed")

        files: list[ExtractedFile] = []
        order = 0
        for fmt, info in (version.get("downloads") or {}).items():
            if fmt not in _DATA_FORMATS:
                continue
            href = (info.get("href") or "").strip()
            if not href:
                continue
            ext       = PurePosixPath(urlparse(href).path).suffix.lower().lstrip(".")
            file_type = _EXT_NORM.get(ext) or _EXT_NORM.get(fmt)
            if not file_type:
                continue
            size_str  = str(info.get("size", ""))
            size_bytes = int(size_str) if size_str.isdigit() else None
            files.append(ExtractedFile(
                url=href,
                file_type=file_type,
                title=title,
                file_size_bytes=size_bytes,
                classification=None,
                display_order=order,
            ))
            order += 1

        if files:
            return ExtractionResult(status="extracted", files=files)
        return ExtractionResult(status="no_files_found")


def _dataset_id_from_url(url: str) -> Optional[str]:
    """Extract dataset ID from www.ons.gov.uk/datasets/{id} URL."""
    if not url.startswith(_ONS_LANDING_PREFIX):
        return None
    tail       = url[len(_ONS_LANDING_PREFIX):].strip("/")
    dataset_id = tail.split("/")[0]
    return dataset_id if dataset_id else None


def _fetch_json(url: str, http: requests.Session) -> Optional[dict]:
    """Fetch a JSON endpoint with 429 backoff. Returns None on any failure."""
    for attempt in range(3):
        try:
            log.info("Fetching %s (attempt %d/3)", url, attempt + 1)
            resp = http.get(url, headers=HEADERS, timeout=(10, 20), allow_redirects=True)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                log.warning("429 rate-limit on %s — waiting %ds", url, wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            return resp.json()
        except requests.RequestException as exc:
            log.warning("Request error for %s (attempt %d/3): %s", url, attempt + 1, exc)
    log.error("All fetch attempts exhausted for %s", url)
    return None
