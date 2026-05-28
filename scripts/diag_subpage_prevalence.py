"""
Diagnostic: sub-page prevalence among no_files_found GOV.UK publications.

Read-only — no DB writes, no sub-page following.

For each StatPublication with data_files_status='no_files_found' and a
GOV.UK producer, fetch the parent page, parse gem-c-attachment containers,
and count how many sub-page URLs are present vs how many are data file URLs.

Output: structured log lines (stderr) + a CSV block printed to stdout at the
end. The Railway log capture is the only persistent output — capture logs
before tearing down the service.

Deploy as a Railway one-shot service (restart: Never, SKIP_MIGRATIONS=1).
CAPTURE LOGS BEFORE TEARDOWN — diagnostic output is lost otherwise.

CSV columns:
    pub_id, producer_slug, pub_url, http_status,
    data_file_count, sub_page_count, sub_page_urls (pipe-separated)
"""
from __future__ import annotations

import csv
import io
import logging
import os
import sys
import time
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("diag_subpage_prevalence")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

import requests

# Single source of truth for data-file extensions — same set the extractor uses
# (govuk_generic.py line 61). Stored without leading dots there; we add them below.
from hansard_archive.discovery.extractors.govuk_generic import DATA_FILE_TYPES as _EXTRACTOR_FILE_TYPES

BATCH_SIZE  = 50
FETCH_DELAY = 1.5
GOVUK_ROOT  = "https://www.gov.uk/"


def _is_sub_page_url(href: str) -> bool:
    """
    True if the href looks like a GOV.UK stats sub-page rather than a data file.

    Matches both /government/statistics/<pub>/<sub> and
    /government/publications/<pub>/<sub> patterns (and any other 4-part
    /government/... path) — the depth check is path-agnostic.

    Rule: netloc is www.gov.uk (or empty for relative), path has 4+ segments,
    last segment has no file extension (no dot).

    Normalisation: urlparse already strips query string and fragment;
    splitting on "/" and filtering empty strings removes trailing slashes.
    """
    try:
        p = urlparse(href)
    except Exception:
        return False
    if p.netloc not in ("www.gov.uk", ""):
        return False
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 4:
        return False
    if "." in parts[-1]:
        return False
    return True


def _is_data_file_url(href: str) -> bool:
    """True if href has an extension matching the extractor's DATA_FILE_TYPES."""
    try:
        path = urlparse(href).path.lower()
    except Exception:
        return False
    return any(path.endswith("." + ext.lstrip(".")) for ext in _EXTRACTOR_FILE_TYPES)


def _fetch_page(url: str, http: requests.Session) -> tuple[int, str | None]:
    """
    Fetch a GOV.UK page with up to 3 attempts and 429 backoff.

    Returns (http_status, html_text). On failure returns (status, None).
    """
    for attempt in range(3):
        try:
            resp = http.get(url, timeout=20, headers={
                "User-Agent": "WestminsterBrief-Diagnostic/1.0"
            })
            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                log.warning("429 rate-limited — backing off %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            return resp.status_code, resp.text if resp.status_code == 200 else None
        except requests.exceptions.RequestException as exc:
            log.warning("Fetch error (attempt %d): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return -1, None


def _parse_attachments(html: str) -> tuple[list[str], list[str]]:
    """
    Parse all gem-c-attachment containers from a GOV.UK page.

    Returns (data_file_urls, sub_page_urls). Links that are neither
    (e.g. external domains, bare fragment links) are silently dropped.

    This is an independent parser — it does NOT reuse the piece 2
    extractor's _parse_govuk_page, which filters to data files only.
    Here we classify ALL attachment hrefs before filtering.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    data_files: list[str] = []
    sub_pages: list[str] = []

    containers = soup.find_all(class_="gem-c-attachment")
    if not containers:
        containers = soup.find_all("section", class_="attachment")

    for container in containers:
        link = container.find("a", href=True)
        if not link:
            continue
        href = link["href"].strip()
        if not href or href in seen:
            continue
        if href.startswith("/"):
            href = "https://www.gov.uk" + href
        seen.add(href)
        if _is_sub_page_url(href):
            sub_pages.append(href)
        elif _is_data_file_url(href):
            data_files.append(href)

    for span in soup.find_all("span", class_="gem-c-attachment-link"):
        link = span.find("a", href=True)
        if not link:
            continue
        href = link["href"].strip()
        if not href or href in seen:
            continue
        if href.startswith("/"):
            href = "https://www.gov.uk" + href
        seen.add(href)
        if _is_sub_page_url(href):
            sub_pages.append(href)
        elif _is_data_file_url(href):
            data_files.append(href)

    return data_files, sub_pages


def run_diagnostic(db_session) -> list[dict]:
    from hansard_archive.models import StatProducer, StatPublication
    from sqlalchemy.orm import joinedload

    rows: list[dict] = []
    last_id = 0
    http = requests.Session()

    log.info("Starting sub-page prevalence diagnostic (read-only, no DB writes)")

    while True:
        batch = (
            db_session.query(StatPublication)
            .options(joinedload(StatPublication.producer))
            .join(StatProducer)
            .filter(
                StatPublication.data_files_status == "no_files_found",
                StatProducer.web_root_url.like(GOVUK_ROOT + "%"),
                StatPublication.id > last_id,
            )
            .order_by(StatPublication.id)
            .limit(BATCH_SIZE)
            .all()
        )
        if not batch:
            break

        for pub in batch:
            last_id = pub.id
            producer_slug = pub.producer.slug if pub.producer else "unknown"
            result = {
                "pub_id": pub.id,
                "producer_slug": producer_slug,
                "pub_url": pub.url,
                "http_status": None,
                "data_file_count": 0,
                "sub_page_count": 0,
                "sub_page_urls": "",
            }

            http_status, html = _fetch_page(pub.url, http)
            result["http_status"] = http_status

            if html is not None:
                data_files, sub_pages = _parse_attachments(html)
                result["data_file_count"] = len(data_files)
                result["sub_page_count"] = len(sub_pages)
                result["sub_page_urls"] = "|".join(sub_pages)
                log.info(
                    "pub=%d producer=%s http=%d data_files=%d sub_pages=%d url=%.70s",
                    pub.id, producer_slug, http_status,
                    len(data_files), len(sub_pages), pub.url,
                )
                for sp in sub_pages:
                    log.info("  [SUB_PAGE] pub=%d %.100s", pub.id, sp)
            else:
                log.warning("pub=%d http=%d url=%.70s", pub.id, http_status, pub.url)

            rows.append(result)
            time.sleep(FETCH_DELAY)

    return rows


def summarise(rows: list[dict]) -> None:
    from collections import Counter

    total           = len(rows)
    with_sub_pages  = [r for r in rows if r["sub_page_count"] > 0]
    confirmed_empty = [r for r in rows if r["sub_page_count"] == 0 and r["http_status"] == 200]
    errors          = [r for r in rows if r["http_status"] not in (200, None) or r["http_status"] == -1]

    log.info("=== SUMMARY ===")
    log.info("Total no_files_found GOV.UK pubs scanned: %d", total)
    log.info("With sub-pages:             %d  (%.0f%%)",
             len(with_sub_pages),
             100 * len(with_sub_pages) / total if total else 0)
    log.info("Confirmed empty (no links): %d", len(confirmed_empty))
    log.info("Fetch errors / non-200:     %d", len(errors))

    if with_sub_pages:
        log.info("--- Sub-page hits by producer ---")
        for slug, count in Counter(r["producer_slug"] for r in with_sub_pages).most_common():
            log.info("  %-40s %d", slug, count)

        log.info("--- Sub-page count distribution ---")
        for count, freq in sorted(Counter(r["sub_page_count"] for r in with_sub_pages).items()):
            log.info("  %2d sub-page(s): %d pubs", count, freq)

        log.info("--- Sample entries (first 10 pubs with sub-pages) ---")
        for r in with_sub_pages[:10]:
            for sp in r["sub_page_urls"].split("|"):
                if sp:
                    log.info("  pub=%d %s", r["pub_id"], sp)


def write_csv_stdout(rows: list[dict]) -> None:
    fields = [
        "pub_id", "producer_slug", "pub_url", "http_status",
        "data_file_count", "sub_page_count", "sub_page_urls",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    log.info(
        "CSV block follows on stdout (between === CSV START === markers) "
        "— verify log capture includes stdout before tearing down service"
    )
    print("=== CSV START ===")
    print(buf.getvalue())
    print("=== CSV END ===")


def _configure_nullpool(app, db) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    # connect_args are psycopg2/Postgres-only; guard for local SQLite runs
    connect_args = (
        {"connect_timeout": 10, "options": "-c statement_timeout=30000"}
        if uri.startswith("postgresql")
        else {}
    )

    with app.app_context():
        db.engine.dispose()

    new_engine = create_engine(uri, poolclass=NullPool, connect_args=connect_args)
    # Flask-SQLAlchemy 3.x caches engines in db._app_engines[app][None].
    # We replace it directly because init_app() raises RuntimeError if called twice.
    # See discovery_worker.py _configure_nullpool() for the full explanation.
    db._app_engines[app][None] = new_engine
    log.info("NullPool engine configured (driver: %s)", new_engine.url.drivername)


def run() -> None:
    from flask_app import app, db

    _configure_nullpool(app, db)

    with app.app_context():
        rows = run_diagnostic(db_session=db.session)

    summarise(rows)
    write_csv_stdout(rows)


if __name__ == "__main__":
    run()
