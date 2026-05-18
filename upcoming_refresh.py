"""
Westminster Brief — Upcoming Releases Refresh
Fetches scheduled official statistics from the GOV.UK release calendar and
caches them in the upcoming_release table, one row per (content_id, theme_slug).

Data source:
  Step 1 — HTML scrape of /search/statistics-announcements per org slug to get
            a list of /government/statistics/announcements/{slug} paths.
  Step 2 — Content API call per path to get structured data including
            details.state (confirmed/provisional/cancelled).

Cancelled items are skipped. Existing rows are refreshed in-place on each run.
Stale rows (not seen in the latest fetch) are deleted after a successful run
for that organisation.

Flask CLI: flask upcoming refresh-all
           flask upcoming refresh-organisation <org-slug>
Direct:    python upcoming_refresh.py [--org <slug>]

Runs: daily Railway cron, 05:00 UTC.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime, date, timezone

import requests
from bs4 import BeautifulSoup

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

log = logging.getLogger("upcoming_refresh")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_TIMEOUT       = 20
_CONTENT_API   = "https://www.gov.uk/api/content"
_SEARCH_URL    = "https://www.gov.uk/search/statistics-announcements"
_GOVUK_BASE    = "https://www.gov.uk"

# ── Theme-to-organisation mapping ────────────────────────────────────────────
# Source of truth: docs/theme-to-organisation-mapping.md
# Each org slug maps to one or more theme slugs.
# One release can appear on multiple theme pages if both themes share an org.

ORG_TO_THEMES: dict[str, list[str]] = {
    "hm-treasury":                                    ["economy", "finance-and-taxation", "government-and-public-administration"],
    "hm-revenue-customs":                             ["finance-and-taxation"],
    "department-for-work-pensions":                   ["employment-and-labour-market", "welfare-and-social-security", "work-and-pensions"],
    "cabinet-office":                                 ["government-and-public-administration", "constitutional-affairs"],
    "department-for-business-and-trade":              ["business-and-industry"],
    "department-for-education":                       ["education"],
    "department-of-health-and-social-care":           ["health-and-social-care"],
    "ministry-of-housing-communities-local-government": ["housing-and-planning"],
    "department-for-transport":                       ["transport"],
    "home-office":                                    ["crime-justice-and-law", "immigration-and-asylum"],
    "ministry-of-justice":                            ["crime-justice-and-law"],
    "department-for-environment-food-rural-affairs":  ["environment-and-climate-change", "agriculture-environment-and-rural-affairs"],
    "ministry-of-defence":                            ["defence-and-national-security"],
    "foreign-commonwealth-development-office":        ["international-affairs", "foreign-affairs"],
    "department-for-science-innovation-and-technology": ["science-technology-and-innovation"],
    "department-for-energy-security-and-net-zero":    ["energy-and-utilities"],
    "department-for-culture-media-and-sport":         ["culture-media-and-sport"],
}


# ── Step 1: scrape the HTML search page for one org ──────────────────────────

def _fetch_announcement_paths(org_slug: str, today: str) -> list[str]:
    """
    Paginate /search/statistics-announcements for one org, returning every
    upcoming release path.  Stops when a page yields no new links.
    Returns a list of paths like ['/government/statistics/announcements/foo', ...].
    """
    paths: list[str] = []
    seen: set[str] = set()
    page = 1

    while True:
        try:
            r = requests.get(
                _SEARCH_URL,
                params={
                    "organisations[]":    org_slug,
                    "release_date_after": today,
                    "page":               page,
                },
                timeout=_TIMEOUT,
                headers={"User-Agent": "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"},
            )
            r.raise_for_status()
        except Exception as exc:
            log.warning("HTML fetch failed for org %s page %d: %s", org_slug, page, exc)
            break

        soup = BeautifulSoup(r.text, "html.parser")
        new_on_page = 0
        for a in soup.find_all("a", href=re.compile(r"^/government/statistics/announcements/")):
            path = a["href"].split("?")[0]
            if path not in seen:
                seen.add(path)
                paths.append(path)
                new_on_page += 1

        log.info("  %s page %d: %d new paths", org_slug, page, new_on_page)
        if new_on_page == 0:
            break   # last page reached

        page += 1
        time.sleep(0.25)   # polite delay between page fetches

    log.info("  %s: %d total announcement paths", org_slug, len(paths))
    return paths


# ── Step 2: fetch Content API for one announcement path ──────────────────────

def _fetch_content(path: str) -> dict | None:
    """
    Call the GOV.UK Content API for one announcement path.
    Returns a normalised dict or None if the fetch fails or the item is cancelled.
    """
    try:
        r = requests.get(
            f"{_CONTENT_API}{path}",
            timeout=_TIMEOUT,
            headers={"User-Agent": "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Content API failed for %s: %s", path, exc)
        return None

    details = data.get("details") or {}
    state   = details.get("state", "")

    if state == "cancelled":
        return None

    release_ts = details.get("release_timestamp", "")
    try:
        release_date = datetime.fromisoformat(release_ts.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        log.warning("Cannot parse release_timestamp %r for %s — skipping", release_ts, path)
        return None

    # Organisation: take the first linked org
    orgs = (data.get("links") or {}).get("organisations") or []
    org_name = orgs[0]["title"] if orgs else ""
    org_base = (orgs[0].get("base_path") or "") if orgs else ""
    org_slug = org_base.replace("/government/organisations/", "") if org_base else ""

    return {
        "govuk_content_id":       data.get("content_id", ""),
        "title":                  data.get("title", "").strip(),
        "summary":                data.get("description", "").strip() or None,
        "release_date":           release_date,
        "release_date_confirmed": (state == "confirmed"),
        "publication_url":        f"{_GOVUK_BASE}{path}",
        "document_type":          details.get("document_type_label", "").strip() or None,
        "organisation_slug":      org_slug,
        "organisation_name":      org_name,
    }


# ── Core refresh logic ────────────────────────────────────────────────────────

def _refresh_org(org_slug: str, theme_slugs: list[str], today: str,
                 now: datetime, dry_run: bool) -> tuple[int, int, int]:
    """
    Refresh all upcoming releases for one organisation.
    Returns (inserted, updated, deleted) counts.
    """
    from flask_app import db
    from hansard_archive.models import UpcomingRelease

    paths = _fetch_announcement_paths(org_slug, today)
    if not paths:
        log.info("  %s: no paths returned — leaving existing rows intact (SWR)", org_slug)
        return 0, 0, 0

    seen_content_ids: set[str] = set()
    inserted = updated = 0

    for path in paths:
        content = _fetch_content(path)
        if not content:
            continue  # cancelled or fetch failure

        cid = content["govuk_content_id"]
        if not cid:
            log.warning("  Missing content_id for %s — skipping", path)
            continue

        seen_content_ids.add(cid)

        for theme_slug in theme_slugs:
            row = UpcomingRelease.query.filter_by(
                govuk_content_id=cid,
                theme_slug=theme_slug,
            ).first()

            if row is None:
                row = UpcomingRelease(
                    govuk_content_id       = cid,
                    theme_slug             = theme_slug,
                    organisation_slug      = content["organisation_slug"] or org_slug,
                    organisation_name      = content["organisation_name"],
                    title                  = content["title"],
                    summary                = content["summary"],
                    release_date           = content["release_date"],
                    release_date_confirmed = content["release_date_confirmed"],
                    publication_url        = content["publication_url"],
                    document_type          = content["document_type"],
                    last_refreshed         = now,
                )
                if not dry_run:
                    db.session.add(row)
                inserted += 1
            else:
                row.title                  = content["title"]
                row.summary                = content["summary"]
                row.release_date           = content["release_date"]
                row.release_date_confirmed = content["release_date_confirmed"]
                row.publication_url        = content["publication_url"]
                row.document_type          = content["document_type"]
                row.last_refreshed         = now
                updated += 1

        if not dry_run:
            db.session.commit()
        time.sleep(0.25)   # polite delay between Content API calls

    # Delete stale rows (releases that have been removed or cancelled on GOV.UK)
    deleted = 0
    if seen_content_ids and not dry_run:
        stale = UpcomingRelease.query.filter(
            UpcomingRelease.theme_slug.in_(theme_slugs),
            UpcomingRelease.organisation_slug == org_slug,
            ~UpcomingRelease.govuk_content_id.in_(seen_content_ids),
        ).all()
        for s in stale:
            db.session.delete(s)
            deleted += 1
        if stale:
            db.session.commit()

    log.info("  %s: inserted=%d updated=%d deleted=%d", org_slug, inserted, updated, deleted)
    return inserted, updated, deleted


def run_refresh(org_slug: str | None = None, dry_run: bool = False) -> None:
    """Main entry point — refresh one org or all orgs."""
    from flask_app import app

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now   = datetime.now(timezone.utc).replace(tzinfo=None)

    mapping = (
        {org_slug: ORG_TO_THEMES[org_slug]}
        if org_slug and org_slug in ORG_TO_THEMES
        else ORG_TO_THEMES
    )

    if org_slug and org_slug not in ORG_TO_THEMES:
        log.error("Unknown org slug %r — check docs/theme-to-organisation-mapping.md", org_slug)
        sys.exit(1)

    with app.app_context():
        total_ins = total_upd = total_del = 0
        for slug, themes in mapping.items():
            log.info("Refreshing org: %s → themes: %s", slug, themes)
            ins, upd, dl = _refresh_org(slug, themes, today, now, dry_run)
            total_ins += ins
            total_upd += upd
            total_del += dl
            time.sleep(1.0)   # polite delay between orgs

        log.info("Refresh complete: inserted=%d updated=%d deleted=%d%s",
                 total_ins, total_upd, total_del, " (DRY RUN)" if dry_run else "")


# ── Flask CLI registration ────────────────────────────────────────────────────

def register_upcoming_cli(app) -> None:
    import click

    @app.cli.group("upcoming")
    def upcoming_group():
        """Upcoming official statistics release calendar commands."""

    @upcoming_group.command("refresh-all")
    @click.option("--dry-run", is_flag=True)
    def cli_refresh_all(dry_run):
        """Refresh all organisations from the GOV.UK release calendar."""
        run_refresh(org_slug=None, dry_run=dry_run)

    @upcoming_group.command("refresh-organisation")
    @click.argument("slug")
    @click.option("--dry-run", is_flag=True)
    def cli_refresh_org(slug, dry_run):
        """Refresh one organisation by its GOV.UK slug."""
        run_refresh(org_slug=slug, dry_run=dry_run)


# ── Direct invocation ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refresh upcoming_release table")
    parser.add_argument("--org",     help="Restrict to one org slug")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_refresh(org_slug=args.org, dry_run=args.dry_run)
