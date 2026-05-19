"""
Workstream C -- Explanatory Notes ingestion for ha_bill_publication.

Fetches publication metadata from the Parliament Bills API for every bill in
ha_bill, classifies each publication into a controlled vocabulary, and for
Explanatory Notes with an HTML link, extracts the opening overview paragraphs
verbatim (no AI).

Usage:
    python scripts/ingest_bill_publications.py             # full run
    python scripts/ingest_bill_publications.py --dry-run   # fetch, print, no DB writes
    python scripts/ingest_bill_publications.py --bill-id N # single bill by parliament_bill_id

Resumable: the UNIQUE(bill_id, publication_url) constraint means re-running
silently skips already-ingested publications.  Only new publications are added.

Rate-limited: 0.2s between publication API calls, 0.5s between HTML fetches.
"""

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from extensions import db
from hansard_archive.models import HaBill, HaBillPublication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://bills-api.parliament.uk/api/v1"
REQUEST_TIMEOUT = 30
FETCH_TIMEOUT = 25
INTER_API_DELAY = 0.2
INTER_HTML_DELAY = 0.5
MAX_RETRIES = 3
SUMMARY_WORD_LIMIT = 500


# ---------------------------------------------------------------------------
# Publication type classification
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, str] = {
    "explanatory notes":            "explanatory_notes",
    "impact assessment":            "impact_assessment",
    "delegated powers memorandum":  "delegated_powers_memorandum",
    "human rights memorandum":      "human_rights_memorandum",
    "bill":                         "bill_text",
    "act of parliament":            "bill_text",
}


def _classify(pub_type_name: str) -> str:
    key = pub_type_name.lower().strip()
    return _TYPE_MAP.get(key, "other")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict | None = None) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 503):
                delay = 2 ** (attempt + 1)
                logger.warning("Rate-limited (%d) — waiting %ds", resp.status_code, delay)
                time.sleep(delay)
                continue
            if resp.status_code == 404:
                return None
            logger.warning("GET %s returned HTTP %d", url, resp.status_code)
            return None
        except requests.Timeout:
            logger.warning("Timeout fetching %s (attempt %d)", url, attempt + 1)
        except requests.RequestException as exc:
            logger.warning("Error fetching %s: %s", url, exc)
    return None


def _fetch_html(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"Accept": "text/html"})
        if resp.status_code == 200:
            return resp.text
        logger.warning("HTML fetch %s returned HTTP %d", url, resp.status_code)
        return None
    except requests.RequestException as exc:
        logger.warning("HTML fetch error %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Explanatory Notes HTML extraction
# ---------------------------------------------------------------------------

def _extract_overview_url(base_url: str, soup: BeautifulSoup) -> str | None:
    """
    Find the 'Overview of the Bill' (or 'Introduction') link from the table
    of contents nav.  Returns the absolute URL or None if not found.
    """
    nav = soup.find("nav")
    if not nav:
        return None
    for a in nav.find_all("a"):
        text = a.get_text(strip=True).lower()
        if "overview" in text or "introduction" in text:
            href = a.get("href", "")
            if href:
                return urljoin(base_url, href)
    return None


def _extract_paragraphs_from_article(soup: BeautifulSoup) -> str:
    """
    Extract paragraph text from the article element up to SUMMARY_WORD_LIMIT words.

    Collects all <p> descendants within the article, in document order.
    Stops once the cumulative word count reaches the limit.
    On overview pages the relevant content begins after one or two heading
    elements, so we collect all paragraphs rather than stopping at headings.
    """
    article = soup.find("article")
    if not article:
        return ""

    paragraphs: list[str] = []
    word_count = 0

    for p in article.find_all("p"):
        text = p.get_text(separator=" ", strip=True)
        if not text:
            continue
        paragraphs.append(text)
        word_count += len(text.split())
        if word_count >= SUMMARY_WORD_LIMIT:
            break

    return "\n\n".join(paragraphs)


def extract_en_summary(html_url: str) -> str | None:
    """
    Fetch the Explanatory Notes HTML and extract the opening overview.

    1. Fetch the base page.
    2. Look for a 'Overview of the Bill' link in the ToC nav.
    3. If found, fetch that page and extract paragraphs.
    4. If no ToC, extract paragraphs from the base page directly.

    Returns extracted text, empty string if no content found, or None on error.
    """
    html = _fetch_html(html_url)
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    time.sleep(INTER_HTML_DELAY)

    overview_url = _extract_overview_url(html_url, soup)
    if overview_url and overview_url != html_url:
        overview_html = _fetch_html(overview_url)
        time.sleep(INTER_HTML_DELAY)
        if overview_html:
            overview_soup = BeautifulSoup(overview_html, "html.parser")
            text = _extract_paragraphs_from_article(overview_soup)
            if text:
                return text

    # Fallback: extract from base page article
    return _extract_paragraphs_from_article(soup) or ""


# ---------------------------------------------------------------------------
# Publication URL selection
# ---------------------------------------------------------------------------

def _best_url(links: list[dict]) -> tuple[str | None, str | None]:
    """
    Pick the best URL from a publication's links array.

    Preference: HTML over PDF.  Returns (url, content_type).
    """
    html_link = None
    pdf_link = None
    for lnk in links:
        url = lnk.get("url", "")
        ct = lnk.get("contentType", "")
        if not url:
            continue
        if "html" in ct.lower():
            html_link = (url, ct)
        elif "pdf" in ct.lower() and pdf_link is None:
            pdf_link = (url, ct)
    return html_link or pdf_link or (None, None)


# ---------------------------------------------------------------------------
# Main ingestion logic
# ---------------------------------------------------------------------------

def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def ingest_bill(bill: HaBill, dry_run: bool = False) -> int:
    """
    Fetch and upsert publications for a single bill.
    Returns the count of new publications ingested.
    """
    url = f"{BASE_URL}/Bills/{bill.parliament_bill_id}/Publications"
    data = _get_json(url)
    time.sleep(INTER_API_DELAY)

    if data is None:
        logger.warning("Bill %d: no publications data", bill.parliament_bill_id)
        return 0

    publications = data.get("publications", [])
    if not publications:
        logger.info("Bill %d: 0 publications", bill.parliament_bill_id)
        return 0

    new_count = 0

    for pub in publications:
        pub_type_name = pub.get("publicationType", {}).get("name", "")
        pub_type = _classify(pub_type_name)
        links = pub.get("links", [])
        best_url, content_type = _best_url(links)

        if not best_url:
            continue

        title = pub.get("title") or pub_type_name or "(untitled)"
        pub_date = _parse_date(pub.get("displayDate"))

        # Check if already ingested
        existing = (
            HaBillPublication.query
            .filter_by(bill_id=bill.id, publication_url=best_url)
            .first()
        )
        if existing:
            continue

        summary_text: str | None = None
        if pub_type == "explanatory_notes" and content_type and "html" in content_type.lower():
            logger.info(
                "  Extracting EN summary from %s",
                best_url[:80] + ("..." if len(best_url) > 80 else ""),
            )
            extracted = extract_en_summary(best_url)
            if extracted:
                summary_text = extracted
            else:
                logger.warning("  EN extraction failed or empty for %s", best_url[:80])

        row = HaBillPublication(
            bill_id=bill.id,
            publication_type=pub_type,
            title=title,
            publication_date=pub_date,
            publication_url=best_url,
            summary_text=summary_text,
            raw_data=pub,
        )

        if not dry_run:
            db.session.add(row)
            try:
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                logger.warning("  DB error for %s: %s", best_url[:60], exc)
                continue

        new_count += 1
        if dry_run:
            logger.info(
                "  [dry-run] %s | %s | EN-summary=%s",
                pub_type, title[:60], bool(summary_text),
            )

    return new_count


def run(bill_id: int | None = None, dry_run: bool = False) -> None:
    from flask_app import app

    with app.app_context():
        if bill_id is not None:
            bills = HaBill.query.filter_by(parliament_bill_id=bill_id).all()
            if not bills:
                logger.error("Bill %d not found in DB", bill_id)
                sys.exit(1)
        else:
            bills = HaBill.query.order_by(HaBill.id).all()

        logger.info("Starting publications ingest for %d bill(s)%s", len(bills), " [dry-run]" if dry_run else "")
        total_new = 0

        for i, bill in enumerate(bills, 1):
            logger.info("[%d/%d] Bill %d: %s", i, len(bills), bill.parliament_bill_id, bill.title[:60])
            new = ingest_bill(bill, dry_run=dry_run)
            total_new += new
            if new:
                logger.info("  -> %d new publication(s)", new)

        logger.info("Done. %d new publications ingested.", total_new)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest bill publications from Parliament API")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write to DB")
    parser.add_argument("--bill-id", type=int, default=None, help="Ingest a single bill by parliament_bill_id")
    args = parser.parse_args()
    run(bill_id=args.bill_id, dry_run=args.dry_run)
