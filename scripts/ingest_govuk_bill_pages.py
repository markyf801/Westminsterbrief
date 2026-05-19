"""
Ingest GOV.UK publication pages for Government Bills.

For each Government Bill in the DB:
  1. Search GOV.UK Search API to find the matching publications page
  2. Validate the match against the bill title slug
  3. Store the URL in ha_bill.govuk_url
  4. Call GOV.UK Content API to get attached documents
  5. Upsert documents into ha_bill_publication with source='govuk'

Scope: Government Bills only (bill_type = 'Government Bill').
PMBs do not have GOV.UK policy pages.

Usage:
    python scripts/ingest_govuk_bill_pages.py [--dry-run] [--bill-id ID]

    --dry-run     Print matches and docs without writing to DB
    --bill-id ID  Process one specific parliament_bill_id only (for testing)
"""

import argparse
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Bootstrap Flask app context
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flask_app import app, db
from hansard_archive.models import HaBill, HaBillPublication

# ---------------------------------------------------------------------------
# GOV.UK type → our controlled vocab
# ---------------------------------------------------------------------------
_GOVUK_TYPE_MAP = {
    "factsheet":                    "factsheet",
    "policy paper":                 "policy_paper",
    "policy_paper":                 "policy_paper",
    "impact assessment":            "impact_assessment",
    "delegated powers memorandum":  "delegated_powers_memorandum",
    "delegated powers":             "delegated_powers_memorandum",
    "human rights memorandum":      "human_rights_memorandum",
    "human rights":                 "human_rights_memorandum",
    "consultation outcome":         "consultation_outcome",
    "consultation response":        "consultation_outcome",
    "bill text":                    "bill_text",
}

_GOVUK_BASE = "https://www.gov.uk"
_SEARCH_URL = "https://www.gov.uk/api/search.json"
_CONTENT_URL = "https://www.gov.uk/api/content"

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "WestminsterBrief/1.0 (+https://westminsterbrief.co.uk)"


def _slugify(title: str) -> str:
    """Convert bill title to a GOV.UK-style slug for matching."""
    t = title.lower()
    t = re.sub(r"\s+act\s+\d{4}$", "", t)
    t = re.sub(r"\s+bill(\s+\[?hl\]?)?$", "", t)
    t = re.sub(r"[\[\]()'\"&]", "", t)
    t = re.sub(r"[^a-z0-9\s-]", "", t)
    t = re.sub(r"\s+", "-", t.strip())
    t = re.sub(r"-+", "-", t)
    return t


def _classify_doc(title: str, govuk_type: str) -> str:
    """Map a GOV.UK document to our controlled publication_type vocab."""
    title_l = title.lower()
    for keyword, pub_type in _GOVUK_TYPE_MAP.items():
        if keyword in title_l:
            return pub_type
    # Fall back to govuk_type field
    type_l = (govuk_type or "").lower().replace("_", " ")
    for keyword, pub_type in _GOVUK_TYPE_MAP.items():
        if keyword in type_l:
            return pub_type
    return "policy_paper"


def _bill_slug(bill_title: str) -> str:
    """
    Derive the GOV.UK publications slug for a bill.
    For acts (title ends 'Act YYYY'), strip that suffix and keep the core name.
    The GOV.UK page is always published as a 'bill' page, even after Royal Assent.
    """
    t = re.sub(r"\s+Act\s+\d{4}$", "", bill_title, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+Bill(\s+\[?HL\]?|\s+\(HL\))?$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*[\[\(]HL[\]\)]", "", t, flags=re.IGNORECASE).strip()
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", "-", t.strip())
    return t + "-bill"


def _probe_url(url: str) -> bool:
    """Return True if the URL responds with 200."""
    try:
        r = _SESSION.head(url, timeout=10, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _find_govuk_url(bill_title: str) -> str | None:
    """
    Find the GOV.UK page for a bill by probing candidate URLs directly.
    Tries /publications/ and /collections/ paths, with and without '-bill' suffix.
    Returns full GOV.UK URL or None.
    """
    slug = _bill_slug(bill_title)           # e.g. "armed-forces-bill"
    slug_no_bill = slug[:-5]                # e.g. "armed-forces"
    # Also try with year suffix stripped from slug_no_bill (for "X Act 2025" → "x")
    # and the raw slug from title as-is (some bills don't end in "bill")
    candidates = [
        f"{_GOVUK_BASE}/government/publications/{slug}",
        f"{_GOVUK_BASE}/government/collections/{slug}",
        f"{_GOVUK_BASE}/government/publications/{slug_no_bill}",
        f"{_GOVUK_BASE}/government/collections/{slug_no_bill}",
    ]
    for url in candidates:
        if _probe_url(url):
            return url
    return None


def _fetch_govuk_documents(govuk_url: str) -> tuple[list[dict], str | None]:
    """
    Call GOV.UK Content API for the bill's page.
    Handles both schema_name=publication (details.attachments)
    and schema_name=collection (links.documents).
    Returns (list of doc dicts, page description or None).
    """
    path = govuk_url.replace(_GOVUK_BASE, "")
    try:
        resp = _SESSION.get(f"{_CONTENT_URL}{path}", timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [WARN] Content API failed for {govuk_url}: {e}")
        return [], None

    data = resp.json()
    description = data.get("description") or None
    schema = data.get("schema_name", "")

    page_date_str = data.get("first_published_at", "") or ""
    page_date = None
    if page_date_str:
        try:
            page_date = datetime.fromisoformat(page_date_str[:10]).date()
        except ValueError:
            pass

    docs = []

    if schema in ("collection", "document_collection"):
        # Collections list child pages in links.documents
        for doc in data.get("links", {}).get("documents", []):
            title = doc.get("title", "").strip()
            base_path = doc.get("base_path", "").strip()
            if not title or not base_path:
                continue
            doc_date_str = doc.get("public_updated_at", "") or ""
            doc_date = page_date
            if doc_date_str:
                try:
                    doc_date = datetime.fromisoformat(doc_date_str[:10]).date()
                except ValueError:
                    pass
            docs.append({
                "title":    title,
                "url":      _GOVUK_BASE + base_path,
                "doc_type": doc.get("document_type", ""),
                "pub_date": doc_date,
            })
    else:
        # Publications have attachments in details.attachments
        for att in data.get("details", {}).get("attachments", []):
            if att.get("attachment_type") == "external":
                continue  # skip links back to parliament.uk
            title = att.get("title", "").strip()
            url = att.get("url", "").strip()
            if not title or not url:
                continue
            if url.startswith("/"):
                url = _GOVUK_BASE + url
            docs.append({
                "title":    title,
                "url":      url,
                "doc_type": att.get("content_type", ""),
                "pub_date": page_date,
            })

    return docs, description


def process_bill(bill: HaBill, dry_run: bool, override_url: str | None = None) -> None:
    print(f"[{bill.parliament_bill_id}] {bill.title}")

    # 1. Find GOV.UK URL
    if override_url:
        govuk_url = override_url.strip()
        print(f"  -> using manual URL: {govuk_url}")
    elif bill.govuk_url:
        govuk_url = bill.govuk_url
        print(f"  -> already have URL: {govuk_url}")
    else:
        govuk_url = _find_govuk_url(bill.title)
        if not govuk_url:
            print("  -> no GOV.UK match found")
            return
        print(f"  -> matched: {govuk_url}")

    # 2. Fetch documents
    docs, description = _fetch_govuk_documents(govuk_url)
    print(f"  -> {len(docs)} document(s) from Content API")
    if description:
        print(f"  -> description: {description[:100]}")

    if dry_run:
        for d in docs:
            print(f"     {d['doc_type']:30s} {d['title'][:60]}")
        return

    # 3. Write to DB
    bill.govuk_url = govuk_url
    new_count = 0
    for d in docs:
        pub_type = _classify_doc(d["title"], d["doc_type"])
        existing = HaBillPublication.query.filter_by(
            bill_id=bill.id,
            publication_url=d["url"],
        ).first()
        if existing:
            continue
        pub = HaBillPublication(
            bill_id          = bill.id,
            publication_type = pub_type,
            source           = "govuk",
            title            = d["title"],
            publication_date = d["pub_date"],
            publication_url  = d["url"],
            publisher        = "GOV.UK",
        )
        db.session.add(pub)
        new_count += 1

    db.session.commit()
    print(f"  -> saved {new_count} new publication(s)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bill-id", type=int, default=None,
                        help="parliament_bill_id to process (single bill)")
    parser.add_argument("--set-url", default=None,
                        help="manually set GOV.UK URL for --bill-id (requires --bill-id)")
    parser.add_argument("--unmatched-only", action="store_true",
                        help="only process bills with no govuk_url yet (skip re-fetch of matched bills)")
    args = parser.parse_args()

    if args.set_url and not args.bill_id:
        parser.error("--set-url requires --bill-id")

    with app.app_context():
        if args.bill_id:
            bills = HaBill.query.filter_by(
                parliament_bill_id=args.bill_id,
                bill_type="Government Bill",
            ).all()
        elif args.unmatched_only:
            bills = HaBill.query.filter_by(
                bill_type="Government Bill",
            ).filter(HaBill.govuk_url.is_(None)).order_by(
                HaBill.introduced_date.desc().nulls_last()
            ).all()
        else:
            bills = HaBill.query.filter_by(
                bill_type="Government Bill",
            ).order_by(HaBill.introduced_date.desc().nulls_last()).all()

        print(f"Processing {len(bills)} Government Bill(s) "
              f"{'(DRY RUN)' if args.dry_run else ''}")

        for i, bill in enumerate(bills, 1):
            print(f"\n[{i}/{len(bills)}]", end=" ")
            try:
                process_bill(bill, args.dry_run, override_url=args.set_url)
            except Exception as e:
                print(f"  [ERROR] {e}")
            time.sleep(0.3)  # polite rate limiting


if __name__ == "__main__":
    main()
