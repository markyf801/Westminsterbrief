"""
Strategy library for discovering statistical publications.

Each strategy knows how to:
1. Determine whether it can handle a given producer
2. Fetch candidate publication items from that producer's pages/API

Strategies are tried in order by select_strategy(); the first match wins.
Order matters: specific strategies (ONS API) precede generic ones (GOV.UK Search,
direct page parser); ManualStrategy is the final fallback and always raises.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

log = logging.getLogger("discovery.strategies")

_TIMEOUT = 20
_UA = "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"

_GOV_UK_SEARCH_URL = "https://www.gov.uk/api/search.json"
_ONS_DATASETS_URL  = "https://api.beta.ons.gov.uk/v1/datasets"

_STAT_KEYWORDS = re.compile(
    r"(statistic|data|publication|report|survey|research|release|bulletin)",
    re.IGNORECASE,
)


@dataclass
class CandidateItem:
    title:            str
    url:              str
    date_hints:       list[str] = field(default_factory=list)
    description_hint: str       = ""
    raw_metadata:     dict      = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

class OnsApiStrategy:
    """
    Use the ONS Beta API to list datasets.
    Specific to office-for-national-statistics — tried first.
    """

    def can_handle(self, producer) -> bool:
        return producer.slug == "office-for-national-statistics"

    def fetch_candidates(self, producer) -> list[CandidateItem]:
        items: list[CandidateItem] = []
        try:
            resp = requests.get(
                _ONS_DATASETS_URL,
                headers={"User-Agent": _UA},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("items", []):
                title = item.get("title", "")
                url   = (item.get("links", {})
                             .get("latest_version", {})
                             .get("href", ""))
                if not url:
                    uri = item.get("uri", "")
                    url = f"https://www.ons.gov.uk{uri}" if uri else ""
                if not title or not url:
                    continue
                # ONS API field: release_date (ISO date string, e.g. "2024-03-21T00:00:00.000Z")
                date_hints = []
                if item.get("release_date"):
                    date_hints.append(item["release_date"][:10])
                items.append(CandidateItem(
                    title=title,
                    url=url,
                    date_hints=date_hints,
                    description_hint=item.get("description", ""),
                    raw_metadata=item,
                ))
        except Exception as exc:
            log.warning("OnsApiStrategy: fetch failed — %s", exc)
        log.info("OnsApiStrategy: %d candidates", len(items))
        return items


class GovUkSearchStrategy:
    """
    Use the GOV.UK Search API to list statistical publications by org slug.
    Covers central departments, agencies, and regulators with a gov.uk presence.
    """

    def can_handle(self, producer) -> bool:
        url = producer.web_root_url or ""
        return "gov.uk" in url or producer.producer_type in (
            "central_department", "executive_agency", "ndpb", "regulator",
        )

    def _org_slug(self, producer) -> str:
        m = re.search(
            r"/organisations/([a-z0-9-]+)/?$",
            producer.web_root_url or "",
        )
        return m.group(1) if m else producer.slug

    def fetch_candidates(self, producer) -> list[CandidateItem]:
        org_slug = self._org_slug(producer)
        items: list[CandidateItem] = []
        start = 0
        page_size = 100
        while True:
            try:
                resp = requests.get(
                    _GOV_UK_SEARCH_URL,
                    params={
                        "filter_organisations[]": org_slug,
                        "filter_content_store_document_type[]": [
                            "statistics_announcement",
                            "official_statistics",
                            "statistical_data_set",
                        ],
                        "start":    start,
                        "count":    page_size,
                        "fields[]": ["title", "description", "link",
                                     "public_timestamp"],
                        # Newest-first ensures that when a slug collision is
                        # detected and the candidate is skipped, the already-
                        # stored URL belongs to the most recent release rather
                        # than whichever release happened to come first in
                        # default (relevance) ordering.
                        "order":    "newest",
                    },
                    headers={"User-Agent": _UA},
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                data    = resp.json()
                results = data.get("results", [])
                for r in results:
                    url = r.get("link", "")
                    if url and not url.startswith("http"):
                        url = f"https://www.gov.uk{url}"
                    # NOTE: the Search API only exposes public_timestamp
                    # (last-updated), NOT first_published_at. date_hints is a
                    # classifier context hint only — the authoritative
                    # publication date is resolved separately via the Content
                    # API in run_discovery (see discovery.pub_dates).
                    date_hints = []
                    if r.get("public_timestamp"):
                        date_hints.append(r["public_timestamp"][:10])
                    items.append(CandidateItem(
                        title=r.get("title", ""),
                        url=url,
                        date_hints=date_hints,
                        description_hint=r.get("description", ""),
                        raw_metadata=r,
                    ))
                if len(results) < page_size:
                    break
                start += page_size
            except Exception as exc:
                log.warning("GovUkSearchStrategy: fetch failed for %s — %s", org_slug, exc)
                break
        log.info("GovUkSearchStrategy: %d candidates for %s", len(items), org_slug)
        return items


class DirectPageParserStrategy:
    """
    Generic HTML fetcher for producers without a gov.uk presence.
    Follows web_root_url and extracts publication-like links by keyword.
    """

    def can_handle(self, producer) -> bool:
        url = producer.web_root_url or ""
        return bool(url) and "gov.uk" not in url

    def fetch_candidates(self, producer) -> list[CandidateItem]:
        from html.parser import HTMLParser

        class _LinkParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links: list[tuple[str, str]] = []
                self._href: str | None = None
                self._buf:  list[str]  = []

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self._href = dict(attrs).get("href", "")
                    self._buf  = []

            def handle_endtag(self, tag):
                if tag == "a" and self._href is not None:
                    text = "".join(self._buf).strip()
                    if text:
                        self.links.append((self._href, text))
                    self._href = None

            def handle_data(self, data):
                if self._href is not None:
                    self._buf.append(data)

        items: list[CandidateItem] = []
        try:
            resp = requests.get(
                producer.web_root_url,
                headers={"User-Agent": _UA},
                timeout=_TIMEOUT,
                allow_redirects=True,
            )
            resp.raise_for_status()
            parser = _LinkParser()
            parser.feed(resp.text)
            parsed_base = urlparse(producer.web_root_url)
            base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
            for href, text in parser.links:
                if not _STAT_KEYWORDS.search(text) and not _STAT_KEYWORDS.search(href):
                    continue
                if href.startswith("http"):
                    full_url = href
                elif href.startswith("/"):
                    full_url = f"{base_origin}{href}"
                else:
                    full_url = f"{producer.web_root_url.rstrip('/')}/{href}"
                items.append(CandidateItem(
                    title=text,
                    url=full_url,
                    raw_metadata={"source": "direct_page_parser"},
                ))
        except Exception as exc:
            log.warning("DirectPageParserStrategy: failed for %s — %s",
                        producer.web_root_url, exc)
        log.info("DirectPageParserStrategy: %d candidates for %s",
                 len(items), producer.web_root_url)
        return items


class ManualStrategy:
    """
    Last-resort fallback: always raises to signal that manual intervention
    is required. The discovery worker catches the error and marks the producer
    as failed with a human-readable reason.
    """

    def can_handle(self, producer) -> bool:
        return True

    def fetch_candidates(self, producer) -> list[CandidateItem]:
        raise RuntimeError(
            f"No automated discovery strategy for '{producer.slug}' "
            f"(type: {producer.producer_type}, url: {producer.web_root_url}). "
            "Add publication rows manually or implement a dedicated strategy."
        )


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

_ORDERED_STRATEGIES = [
    OnsApiStrategy(),
    GovUkSearchStrategy(),
    DirectPageParserStrategy(),
    ManualStrategy(),          # always last
]


def select_strategy(producer) -> object:
    """Return the first strategy that claims to handle this producer."""
    for strategy in _ORDERED_STRATEGIES:
        if strategy.can_handle(producer):
            return strategy
    return ManualStrategy()    # unreachable — ManualStrategy always returns True
