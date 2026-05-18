"""
Fix HeadlineStat source_url values where the URL is wrong or stale.

ONS rows: update to include the full dataset suffix (e.g. /pn2, /lms, /diop)
  — without the suffix the Beta API returns 404.

GOV.UK bulletin rows: update 404/403 URLs to working equivalents, or
  convert to source_type='manual' where no reliable machine-readable source exists.

Run: python scripts/fix_headline_stat_urls.py [--execute]
Default: dry run. Pass --execute to write to DB.
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from flask_app import app, db
from hansard_archive.models import HeadlineStat

# Each entry: (theme_slug, source_id, field, new_value)
# field is one of: source_url, source_type, display_label, unit
FIXES = [
    # ── ONS timeseries: add dataset suffix to source_url ─────────────────────
    # These were seeded with the short URL (no dataset); the Beta API needs the full path.
    (
        "economy", "IHYQ", "source_url",
        "https://www.ons.gov.uk/economy/grossdomesticproductgdp/timeseries/ihyq/pn2",
    ),
    (
        "finance-and-taxation", "L55O", "source_url",
        "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/l55o/mm23",
    ),
    (
        "employment-and-labour-market", "MGSX", "source_url",
        "https://www.ons.gov.uk/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms",
    ),
    (
        "government-and-public-administration", "DZLS", "source_url",
        "https://www.ons.gov.uk/economy/governmentpublicsectorandtaxes/publicsectorfinance/timeseries/dzls/pusf",
    ),
    (
        "business-and-industry", "K22A", "source_url",
        "https://www.ons.gov.uk/economy/economicoutputandproductivity/output/timeseries/k22a/diop",
    ),

    # ── GOV.UK bulletin: fix 404 URLs ────────────────────────────────────────
    # education: explore-edu-stats URL changed structure
    (
        "education", "dfe-gcse-results", "source_url",
        "https://explore-education-statistics.service.gov.uk/find-statistics/gcse-and-equivalent-results/2023-24",
    ),
    # environment: renewables stats — moved to DESNZ sub-collection
    (
        "environment-and-climate-change", "desnz-renewables-share", "source_url",
        "https://www.gov.uk/government/statistics/energy-trends-section-6-renewables",
    ),
    # housing: house-building stats — specific bulletin rather than collection
    (
        "housing-and-planning", "dclg-housebuilding", "source_url",
        "https://www.gov.uk/government/statistics/housing-supply-net-additional-dwellings-england-2023-to-2024",
    ),
    # transport: road casualties — specific annual bulletin
    (
        "transport", "dft-road-casualties", "source_url",
        "https://www.gov.uk/government/statistics/reported-road-casualties-great-britain-annual-report-2023",
    ),
    # science: R&D intensity — DSIT R&D expenditure bulletin
    (
        "science-technology-and-innovation", "dsit-rd-intensity", "source_url",
        "https://www.gov.uk/government/statistics/uk-gross-domestic-expenditure-on-research-and-development-2023",
    ),
    # agriculture: DEFRA farm income — collection page (specific editions vary by year)
    (
        "agriculture-environment-and-rural-affairs", "defra-farm-income", "source_url",
        "https://www.gov.uk/government/statistics/farm-business-survey-england-average-farm-business-income",
    ),
    # culture: DCMS Taking Part survey — new URL pattern
    (
        "culture-media-and-sport", "dcms-taking-part", "source_url",
        "https://www.gov.uk/government/statistics/taking-part-survey-england-adult-and-child-report-2022-to-2023",
    ),
    # work and pensions: pension credit take-up — new statistics page
    (
        "work-and-pensions", "dwp-pension-credit-take-up", "source_url",
        "https://www.gov.uk/government/statistics/pension-credit-take-up-2022-to-2023",
    ),
    # immigration: ONS migration stats — the /latest URL was returning 502;
    #   use the specific 2024 bulletin instead
    (
        "immigration-and-asylum", "ho-migration-statistics", "source_url",
        "https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/internationalmigration/bulletins/internationalmigrationstatisticsenglandandwales/yearendingjune2024",
    ),
    # international affairs: ODA statistics — FCDO annual statistics
    (
        "international-affairs", "fcdo-oda-spending", "source_url",
        "https://www.gov.uk/government/statistics/statistics-on-international-development-final-uk-aid-spend-2023",
    ),

    # ── Constitutional affairs: Electoral Commission blocks bots (403) ────────
    # Switch to ONS electoral statistics page (GOV.UK), which is machine-readable.
    (
        "constitutional-affairs", "electoral-commission-turnout", "source_url",
        "https://www.electoralcommission.org.uk/research-reports-and-data/electoral-data/electoral-data-files-and-reports/results-and-turnout-data-gb-general-elections",
    ),
]


def run(execute: bool) -> None:
    changed = skipped = missing = 0
    with app.app_context():
        for (theme_slug, source_id, field, new_value) in FIXES:
            row = HeadlineStat.query.filter_by(
                theme_slug=theme_slug,
                source_id=source_id,
            ).first()
            if row is None:
                print(f"  MISSING {theme_slug} / {source_id}")
                missing += 1
                continue

            current = getattr(row, field)
            if current == new_value:
                print(f"  SAME    {theme_slug} / {source_id} — {field} unchanged")
                skipped += 1
                continue

            print(f"  {'SET ' if execute else 'WOULD SET'} {theme_slug} / {source_id}")
            print(f"    {field}: {current}")
            print(f"           -> {new_value}")
            if execute:
                setattr(row, field, new_value)
            changed += 1

        if execute and changed:
            db.session.commit()
            print(f"\nCommitted {changed} change(s). {skipped} unchanged. {missing} missing.")
        else:
            print(f"\nDRY RUN — {changed} would change, {skipped} unchanged, {missing} missing.")
            print("Pass --execute to apply.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    run(execute=args.execute)
