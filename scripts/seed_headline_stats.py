"""
Seed script: headline_stat table — Phase 2 Cross-Government Statistics.

Curation date: 2026-05-17
Run: python scripts/seed_headline_stats.py [--execute]
Default is DRY RUN. Pass --execute to actually write rows.

Confirmed ONS rows (5): verified from previous ONS curation session.
GOV.UK bulletin rows (~17): DRAFT — Mark must review URLs and display_labels
    before seeding. Comment out any row where the bulletin URL is wrong or
    the theme should be skipped. Themes with no plausible source are marked
    # NO SOURCE: <reason>.

Idempotent: ON CONFLICT (theme_slug, source_id) DO NOTHING — safe to re-run.

Phase 3 backlog (not seeded here):
    HPI dataset endpoint (house price index)
    GHG emissions (BEIS/DESNZ bulletin)
    Life expectancy (ONS health tables)
    R&D expenditure (DSIT bulletin)
    UC claimants (DWP stat release)
    Monthly trade balance (ONS trade bulletin)
"""

import argparse
import os
import sys
from datetime import datetime, date

# ── Resolve paths ─────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from flask_app import app, db
from hansard_archive.models import HeadlineStat

# ── Rows ───────────────────────────────────────────────────────────────────────

# Each row is a dict matching HeadlineStat columns.
# required: theme_slug, source_type, source_id, display_label, source_url
# optional but important for display: geography, display_hint, unit

_NOW = datetime(2026, 5, 17, 0, 0, 0)   # seeded_at placeholder; refreshed on first cron run

ROWS = [

    # ── CONFIRMED ONS TIMESERIES ROWS ────────────────────────────────────────
    # These are verified CDIDs from the previous curation session.
    # Refresh handler calls /datasets/timeseries/{cdid}/data.

    {
        "theme_slug":    "economy",
        "source_type":   "ons_timeseries",
        "source_id":     "IHYQ",           # GDP growth (quarter on quarter, chained volume)
        "display_label": "GDP growth",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "%",
        "source_url":    "https://www.ons.gov.uk/economy/grossdomesticproductgdp/timeseries/IHYQ",
    },
    {
        "theme_slug":    "employment-and-labour-market",
        "source_type":   "ons_timeseries",
        "source_id":     "MGSX",           # Unemployment rate (aged 16+, seasonally adjusted)
        "display_label": "Unemployment rate",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "%",
        "source_url":    "https://www.ons.gov.uk/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/MGSX",
    },
    {
        "theme_slug":    "finance-and-taxation",
        "source_type":   "ons_timeseries",
        "source_id":     "L55O",           # CPIH (12-month rate)
        "display_label": "Inflation (CPIH)",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "%",
        "source_url":    "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/L55O",
    },
    {
        "theme_slug":    "government-and-public-administration",
        "source_type":   "ons_timeseries",
        "source_id":     "DZLS",           # Public sector net borrowing excl. public sector banks
        "display_label": "Public sector net borrowing",
        "display_hint":  "borrowing",
        "geography":     "UK",
        "unit":          "£m",
        "source_url":    "https://www.ons.gov.uk/economy/governmentpublicsectorandtaxes/publicsectorfinance/timeseries/DZLS",
    },
    {
        "theme_slug":    "business-and-industry",
        "source_type":   "ons_timeseries",
        "source_id":     "K22A",           # Manufacturing output index (2022=100)
        "display_label": "Manufacturing output index",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "index (2022=100)",
        "source_url":    "https://www.ons.gov.uk/economy/economicoutputandproductivity/output/timeseries/K22A",
    },

    # ── GOV.UK BULLETIN ROWS — DRAFT FOR REVIEW ──────────────────────────────
    # Mark: review each URL. If a URL is wrong or the theme should be skipped,
    # comment out the row or replace the URL before seeding with --execute.
    # Bulletin URLs point to the stable landing page (not a dated version URL).

    {
        "theme_slug":    "education",
        "source_type":   "govuk_bulletin",
        "source_id":     "dfe-gcse-results",
        "display_label": "GCSE grade 4+ pass rate (English and maths)",
        "display_hint":  "raw",
        "geography":     "England",
        "unit":          "%",
        "source_url":    "https://explore-education-statistics.service.gov.uk/find-statistics/gcse-and-equivalent-results",
    },
    {
        "theme_slug":    "health-and-social-care",
        "source_type":   "govuk_bulletin",
        "source_id":     "nhs-ae-performance",
        "display_label": "A&E four-hour standard — patients seen within 4 hours",
        "display_hint":  "raw",
        "geography":     "England",
        "unit":          "%",
        "source_url":    "https://www.england.nhs.uk/statistics/statistical-work-areas/ae-waiting-times-and-activity/",
    },
    {
        "theme_slug":    "housing-and-planning",
        "source_type":   "govuk_bulletin",
        "source_id":     "dclg-housebuilding",
        "display_label": "New dwellings completed",
        "display_hint":  "raw",
        "geography":     "England",
        "unit":          "dwellings",
        "source_url":    "https://www.gov.uk/government/collections/house-building-new-build-dwellings",
    },
    {
        "theme_slug":    "transport",
        "source_type":   "govuk_bulletin",
        "source_id":     "dft-road-casualties",
        "display_label": "Road casualties — killed or seriously injured",
        "display_hint":  "raw",
        "geography":     "Great Britain",
        "unit":          "people",
        "source_url":    "https://www.gov.uk/government/collections/reported-road-casualties-great-britain",
    },
    {
        "theme_slug":    "crime-justice-and-law",
        "source_type":   "govuk_bulletin",
        "source_id":     "ons-crime-survey",
        "display_label": "Crime Survey for England and Wales — total offences",
        "display_hint":  "raw",
        "geography":     "England and Wales",
        "unit":          "offences (millions)",
        "source_url":    "https://www.ons.gov.uk/peoplepopulationandcommunity/crimeandjustice/bulletins/crimeinenglandandwales/latest",
    },
    {
        "theme_slug":    "welfare-and-social-security",
        "source_type":   "govuk_bulletin",
        "source_id":     "dwp-benefit-claimants",
        "display_label": "People on key DWP benefits",
        "display_hint":  "raw",
        "geography":     "Great Britain",
        "unit":          "claimants",
        "source_url":    "https://www.gov.uk/government/collections/benefit-expenditure-and-caseload-tables",
    },
    {
        "theme_slug":    "immigration-and-asylum",
        "source_type":   "govuk_bulletin",
        "source_id":     "ho-migration-statistics",
        "display_label": "Long-term net migration",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "people",
        "source_url":    "https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/internationalmigration/bulletins/internationalmigrationstatisticsenglandandwales/latest",
    },
    {
        "theme_slug":    "environment-and-climate-change",
        "source_type":   "govuk_bulletin",
        "source_id":     "desnz-renewables-share",
        "display_label": "Renewables share of electricity generation",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "%",
        "source_url":    "https://www.gov.uk/government/statistics/renewable-sources-of-energy",
    },
    {
        "theme_slug":    "defence-and-national-security",
        "source_type":   "govuk_bulletin",
        "source_id":     "mod-defence-spending-gdp",
        "display_label": "Defence spending as share of GDP",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "% of GDP",
        "source_url":    "https://www.gov.uk/government/statistics/uk-defence-in-numbers",
    },
    {
        "theme_slug":    "international-affairs",
        "source_type":   "govuk_bulletin",
        "source_id":     "fcdo-oda-spending",
        "display_label": "UK official development assistance (ODA)",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "£m",
        "source_url":    "https://www.gov.uk/government/statistics/statistics-on-international-development",
    },
    {
        "theme_slug":    "science-technology-and-innovation",
        "source_type":   "govuk_bulletin",
        "source_id":     "dsit-rd-intensity",
        "display_label": "R&D expenditure as share of GDP",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "% of GDP",
        "source_url":    "https://www.gov.uk/government/statistics/ukis-2024-summary-report",
    },
    {
        "theme_slug":    "energy-and-utilities",
        "source_type":   "govuk_bulletin",
        "source_id":     "desnz-energy-prices",
        "display_label": "Average household energy bill (Ofgem cap)",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "£/year",
        "source_url":    "https://www.ofgem.gov.uk/check-if-energy-price-cap-affects-you",
    },
    {
        "theme_slug":    "work-and-pensions",
        "source_type":   "govuk_bulletin",
        "source_id":     "dwp-pension-credit-take-up",
        "display_label": "Pension Credit take-up rate",
        "display_hint":  "raw",
        "geography":     "Great Britain",
        "unit":          "%",
        "source_url":    "https://www.gov.uk/government/statistics/pension-credit-take-up",
    },
    {
        "theme_slug":    "agriculture-environment-and-rural-affairs",
        "source_type":   "govuk_bulletin",
        "source_id":     "defra-farm-income",
        "display_label": "Average Farm Business Income",
        "display_hint":  "raw",
        "geography":     "England",
        "unit":          "£/farm",
        "source_url":    "https://www.gov.uk/government/statistics/farm-business-survey-england-farm-business-income",
    },
    {
        "theme_slug":    "culture-media-and-sport",
        "source_type":   "govuk_bulletin",
        "source_id":     "dcms-taking-part",
        "display_label": "Adults engaging in arts activities in last 12 months",
        "display_hint":  "raw",
        "geography":     "England",
        "unit":          "%",
        "source_url":    "https://www.gov.uk/government/statistics/taking-part-survey",
    },
    {
        "theme_slug":    "constitutional-affairs",
        "source_type":   "govuk_bulletin",
        "source_id":     "electoral-commission-turnout",
        "display_label": "General election turnout",
        "display_hint":  "raw",
        "geography":     "UK",
        "unit":          "%",
        "source_url":    "https://www.electoralcommission.org.uk/who-we-are-and-what-we-do/elections-and-referendums/past-elections-and-referendums/uk-general-elections",
    },

    # NO SOURCE: foreign-affairs — ONS/gov.uk don't publish a single headline
    #   statistic for "foreign affairs" as a policy area. International trade
    #   figures exist (ONS Pink Book) but overlap with economy. Deferred to
    #   Phase 3 review.

    # NO SOURCE: parliamentary-affairs — no single national statistic. Possible
    #   candidates (turnout, number of sitting days) are episodic or too narrow.
    #   Deferred to Phase 3 review.

]


def _seed(execute: bool) -> None:
    seeded = skipped = 0
    with app.app_context():
        for row in ROWS:
            exists = HeadlineStat.query.filter_by(
                theme_slug=row["theme_slug"],
                source_id=row["source_id"],
            ).first()
            if exists:
                print(f"  SKIP (exists) {row['theme_slug']} / {row['source_id']}")
                skipped += 1
                continue

            stat = HeadlineStat(
                theme_slug     = row["theme_slug"],
                source_type    = row["source_type"],
                source_id      = row["source_id"],
                display_label  = row["display_label"],
                display_hint   = row.get("display_hint", "raw"),
                geography      = row.get("geography", "UK"),
                unit           = row.get("unit"),
                source_url     = row["source_url"],
                last_refreshed = _NOW,
                last_success   = _NOW,
            )
            print(f"  {'SEED' if execute else 'DRY '} {row['theme_slug']} / {row['source_id']}")
            if execute:
                db.session.add(stat)
            seeded += 1

        if execute:
            db.session.commit()
            print(f"\nSeeded {seeded} rows, skipped {skipped} existing.")
        else:
            print(f"\nDRY RUN — would seed {seeded} rows, skip {skipped} existing. Pass --execute to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually write rows (default: dry run)")
    args = parser.parse_args()
    _seed(execute=args.execute)
