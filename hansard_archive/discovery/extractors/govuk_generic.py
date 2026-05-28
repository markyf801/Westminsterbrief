"""
GOV.UK generic extractor — piece 2 of Phase 1.8 data_url work.

Handles all www.gov.uk publication pages (17 central departments + UKHSA,
Ofsted, Ofgem). Supports both the current gem-c-attachment component and
the older section.attachment pattern.

Classification is rule-based from explicit section headings only.
NULL where unlabelled — no inference, no LLM.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import BaseExtractor, ExtractedFile, ExtractionResult

log = logging.getLogger(__name__)

EES_PREFIX    = "https://explore-education-statistics.service.gov.uk"
ASSETS_DOMAIN = "assets.publishing.service.gov.uk"

HEADERS = {
    "User-Agent": "WestminsterBriefBot/1.0 (+https://westminsterbrief.co.uk/)",
    "Accept":     "text/html,application/xhtml+xml",
}

# GOV.UK markup type labels → normalised short forms (primary signal).
# Checked with substring match on lowercased label so partial strings hit too.
_GOVUK_TYPE_LABELS: dict[str, str] = {
    "ms excel spreadsheet":    "xlsx",
    "microsoft excel":         "xlsx",
    "ms excel":                "xlsx",
    "opendocument spreadsheet": "ods",
    "opendocument":            "ods",
    "portable document format": "pdf",
    "comma-separated values":  "csv",
    "comma separated values":  "csv",
    "ms word document":        "docx",
    "microsoft word":          "docx",
    "zip archive":             "zip",
    # Short-form abbreviation text (when abbr has no title attribute)
    "xlsx": "xlsx",
    "xls":  "xlsx",
    "ods":  "ods",
    "pdf":  "pdf",
    "csv":  "csv",
    "zip":  "zip",
    "json": "json",
    "xml":  "xml",
}

# Only these types are included as data files. html/docx/etc excluded (Q2 resolution).
DATA_FILE_TYPES: frozenset[str] = frozenset({"xlsx", "xls", "ods", "csv", "pdf", "zip", "json", "xml"})

# Sub-page path segments that are never data — skip fetch to avoid unnecessary HTTP calls.
EXCLUDED_SUB_PAGE_PREFIXES: tuple[str, ...] = ("pre-release-access-",)

_SIZE_RE      = re.compile(r"([\d.]+)\s*(bytes?|kb|mb|gb)", re.IGNORECASE)
_SIZE_FACTORS = {
    "byte": 1, "bytes": 1,
    "kb": 1_024, "mb": 1_048_576, "gb": 1_073_741_824,
}

_CLASSIFICATION_RULES: list[tuple[list[str], str]] = [
    (["main", "headline", "key stat", "primary"],                                "main_release"),
    (["supporting", "additional", "supplementary", "underlying data"],           "supporting_tables"),
    (["technical", "methodology", "quality", "note", "annex", "guide", "user"], "technical_docs"),
]


class GovUKGenericExtractor(BaseExtractor):

    def extract(self, url: str, http_session: requests.Session) -> ExtractionResult:
        resp = _fetch(url, http_session)
        if resp is None:
            return ExtractionResult(status="fetch_failed", error="request failed after retries")
        if not resp.ok:
            return ExtractionResult(status="fetch_failed", error=f"HTTP {resp.status_code}")

        files, ees_url, sub_page_urls = _parse_govuk_page(resp.text)

        if sub_page_urls:
            extra = _follow_sub_pages(
                sub_page_urls, http_session,
                parent_file_urls={f.url for f in files},
                start_order=len(files),
            )
            if extra:
                files = files + extra

        if ees_url and not files:
            return ExtractionResult(status="not_extractable", ees_url=ees_url,
                                    sub_page_urls=sub_page_urls)
        if files:
            return ExtractionResult(status="extracted", files=files, ees_url=ees_url,
                                    sub_page_urls=sub_page_urls)
        return ExtractionResult(status="no_files_found", ees_url=ees_url,
                                sub_page_urls=sub_page_urls)


# ---------------------------------------------------------------------------
# HTTP fetch with 429 backoff
# ---------------------------------------------------------------------------

def _fetch(url: str, http: requests.Session) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            log.info("Fetching %s (attempt %d/3)", url, attempt + 1)
            resp = http.get(url, headers=HEADERS, timeout=(10, 20), allow_redirects=True)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                log.warning("429 rate-limit on %s — waiting %ds (attempt %d/3)", url, wait, attempt + 1)
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as exc:
            log.warning("Request error for %s (attempt %d/3): %s", url, attempt + 1, exc)
    log.error("All fetch attempts exhausted for %s", url)
    return None


# ---------------------------------------------------------------------------
# Sub-page following (piece 2b)
# ---------------------------------------------------------------------------

def _should_fetch_sub_page(url: str) -> bool:
    """Cheap pre-fetch filter — False for path patterns that never contain data files."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return not any(p.startswith(EXCLUDED_SUB_PAGE_PREFIXES) for p in parts)


def _follow_sub_pages(
    sub_page_urls: list[str],
    http: requests.Session,
    parent_file_urls: set[str],
    start_order: int = 0,
) -> list[ExtractedFile]:
    """
    Depth-1 sub-page following.

    Fetches each sub-page URL, parses attachments, and returns any new data
    files found. Files already on the parent page (parent_file_urls) are
    skipped to avoid duplicates. Sub-pages that yield no new files are
    silently dropped (whitelist approach — harmless against unknown patterns).
    Sub-page URLs found within a sub-page are NOT followed (depth-1 only).
    """
    extra: list[ExtractedFile] = []
    seen: set[str] = set(parent_file_urls)
    order = start_order

    for url in sub_page_urls:
        if not _should_fetch_sub_page(url):
            log.info("Skipping excluded sub-page pattern: %s", url)
            continue

        resp = _fetch(url, http)
        if resp is None or not resp.ok:
            status = resp.status_code if resp is not None else "None"
            log.warning("Sub-page fetch failed (status=%s): %s", status, url)
            continue

        sub_files, _ees, _sub_sub = _parse_govuk_page(resp.text)
        new_files = [f for f in sub_files if f.url not in seen]
        if not new_files:
            log.debug("Sub-page yielded no new data files (silently dropped): %s", url)
            continue

        log.info("Sub-page %s yielded %d new file(s)", url, len(new_files))
        for f in new_files:
            extra.append(ExtractedFile(
                url=f.url,
                file_type=f.file_type,
                title=f.title,
                file_size_bytes=f.file_size_bytes,
                classification=f.classification,
                display_order=order,
            ))
            seen.add(f.url)
            order += 1

    return extra


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_govuk_page(html: str) -> tuple[list[ExtractedFile], Optional[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    # Attachments sit outside div.govspeak on current GOV.UK pages — search full page
    files:         list[ExtractedFile] = []
    ees_url:       Optional[str]       = None
    sub_page_urls: list[str]           = []
    seen_urls:     set[str]            = set()
    order = 0

    for attach in _find_attachments(soup):
        heading = _nearest_preceding_heading(attach)

        a = (
            attach.find("a", class_="gem-c-attachment__link")
            or attach.select_one(".title a")
            or attach.find("a", href=True)
        )
        if not a:
            continue

        href = a.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        if href.startswith("/"):
            href = "https://www.gov.uk" + href

        if href in seen_urls:
            continue

        if href.startswith(EES_PREFIX):
            ees_url = ees_url or href
            continue

        file_type = _derive_file_type(attach, href)
        if file_type not in DATA_FILE_TYPES:
            if _is_govuk_subpage(href) and href not in sub_page_urls:
                sub_page_urls.append(href)
            continue

        seen_urls.add(href)
        files.append(ExtractedFile(
            url=href,
            file_type=file_type,
            title=a.get_text(strip=True) or None,
            file_size_bytes=_extract_size(attach),
            classification=_classify_from_heading(heading),
            display_order=order,
        ))
        order += 1

    # Inline attachment links — span.gem-c-attachment-link inside govspeak
    for span in soup.find_all("span", class_="gem-c-attachment-link"):
        a = span.find("a", href=True)
        if not a:
            continue
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href in seen_urls:
            continue
        if href.startswith("/"):
            href = "https://www.gov.uk" + href
        if href.startswith(EES_PREFIX):
            ees_url = ees_url or href
            continue
        file_type = _derive_file_type(span, href)
        if file_type not in DATA_FILE_TYPES:
            continue
        heading = _nearest_preceding_heading(span.parent)
        files.append(ExtractedFile(
            url=href,
            file_type=file_type,
            title=a.get_text(strip=True) or None,
            file_size_bytes=None,  # not in inline pattern markup
            classification=_classify_from_heading(heading),
            display_order=order,
        ))
        seen_urls.add(href)
        order += 1

    return files, ees_url, sub_page_urls


def _is_govuk_subpage(href: str) -> bool:
    """True if href is a GOV.UK content sub-page rather than a downloadable asset."""
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc != "www.gov.uk":
        return False
    if PurePosixPath(parsed.path).suffix:
        return False  # has a file extension — it's a file, not a content page
    parts = [p for p in parsed.path.split("/") if p]
    return (
        len(parts) >= 4
        and parts[0] == "government"
        and parts[1] in ("statistics", "publications", "collections")
    )


def _find_attachments(content) -> list:
    """Return all attachment container elements — both GOV.UK component patterns."""
    results = []
    for el in content.find_all(True):
        classes = el.get("class") or []
        # Current GOV.UK pattern: section.gem-c-attachment (also div on some older pages)
        if "gem-c-attachment" in classes:
            if not any(c.startswith("gem-c-attachment__") for c in classes):
                results.append(el)
        # Legacy pattern: section.attachment
        elif el.name == "section" and "attachment" in classes:
            results.append(el)
    return results


def _nearest_preceding_heading(el) -> Optional[str]:
    """Nearest h2/h3 sibling preceding this element within its parent."""
    for sib in el.previous_siblings:
        name = getattr(sib, "name", None)
        if name in ("h2", "h3"):
            return sib.get_text(strip=True)
    return None


# ---------------------------------------------------------------------------
# File type derivation
# ---------------------------------------------------------------------------

def _derive_file_type(attach_el, href: str) -> Optional[str]:
    """
    Normalised file type. GOV.UK markup label is primary; URL extension is fallback.
    Returns None if genuinely underivable.
    """
    label = _get_type_label(attach_el)
    if label:
        norm = _normalise_type_label(label)
        if norm is not None:
            return norm

    ext = PurePosixPath(urlparse(href).path).suffix.lower().lstrip(".")
    if ext:
        return _GOVUK_TYPE_LABELS.get(ext, ext if len(ext) <= 5 else None)
    return None


def _get_type_label(attach_el) -> Optional[str]:
    """Extract raw type label string from attachment markup."""
    # New gem-c pattern: first gem-c-attachment__attribute span inside metadata
    meta = attach_el.find(class_="gem-c-attachment__metadata")
    if meta:
        attrs = meta.find_all(class_="gem-c-attachment__attribute")
        if attrs:
            abbr = attrs[0].find("abbr")
            if abbr:
                return abbr.get("title", "").strip() or abbr.get_text(strip=True)
            return attrs[0].get_text(strip=True) or None

    # Old pattern: span.type
    type_el = attach_el.find(class_="type")
    return type_el.get_text(strip=True) if type_el else None


def _normalise_type_label(label: str) -> Optional[str]:
    """Map a raw type label to a normalised short form."""
    l = label.strip().lower()
    if l in _GOVUK_TYPE_LABELS:
        return _GOVUK_TYPE_LABELS[l]
    for key, norm in _GOVUK_TYPE_LABELS.items():
        if key in l:
            return norm
    return None


# ---------------------------------------------------------------------------
# File size extraction
# ---------------------------------------------------------------------------

def _extract_size(attach_el) -> Optional[int]:
    meta = (
        attach_el.find(class_="gem-c-attachment__metadata")
        or attach_el.find(class_="metadata")
    )
    text = meta.get_text() if meta else attach_el.get_text()
    m = _SIZE_RE.search(text)
    if not m:
        return None
    value  = float(m.group(1))
    factor = _SIZE_FACTORS.get(m.group(2).lower(), 0)
    return int(value * factor) if factor else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify_from_heading(heading: Optional[str]) -> Optional[str]:
    """Map section heading to classification. NULL for unlabelled."""
    if not heading:
        return None
    h = heading.lower()
    for keywords, cls in _CLASSIFICATION_RULES:
        if any(kw in h for kw in keywords):
            return cls
    return None
