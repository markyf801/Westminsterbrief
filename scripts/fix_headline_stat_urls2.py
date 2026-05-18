"""
Round 2 URL fixes for HeadlineStat rows that still fail after fix1.

- Rows where correct URL found: update source_url
- Rows where URL is permanently blocked or no reliable machine-readable source:
  switch to source_type='manual' so they don't fail the cron run.

Run: python scripts/fix_headline_stat_urls2.py [--execute]
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from flask_app import app, db
from hansard_archive.models import HeadlineStat

FIXES = [
    # defra-farm-income: collection page works (specific edition keeps changing URL)
    (
        "agriculture-environment-and-rural-affairs", "defra-farm-income",
        "source_url",
        "https://www.gov.uk/government/collections/farm-business-survey",
    ),
    # dfe-gcse-results: use explore-edu key-stage-4 or GOV.UK collection
    (
        "education", "dfe-gcse-results",
        "source_url",
        "https://explore-education-statistics.service.gov.uk/find-statistics/key-stage-4-performance",
    ),
    # fcdo-oda: specific 2022 edition exists
    (
        "international-affairs", "fcdo-oda-spending",
        "source_url",
        "https://www.gov.uk/government/statistics/statistics-on-international-development-final-uk-aid-spend-2022",
    ),
    # dcms-taking-part: use collection page (survey results listed there)
    (
        "culture-media-and-sport", "dcms-taking-part",
        "source_url",
        "https://www.gov.uk/government/collections/taking-part-survey",
    ),

    # constitutional-affairs: Electoral Commission blocks bots (403) — switch to manual
    (
        "constitutional-affairs", "electoral-commission-turnout",
        "source_type",
        "manual",
    ),
    # immigration: all specific edition URLs return 404 — switch to manual
    (
        "immigration-and-asylum", "ho-migration-statistics",
        "source_type",
        "manual",
    ),
    # dsit-rd: no stable URL found for R&D intensity — switch to manual
    (
        "science-technology-and-innovation", "dsit-rd-intensity",
        "source_type",
        "manual",
    ),
    # dwp-pension-credit: specific edition URLs all 404 — switch to manual
    (
        "work-and-pensions", "dwp-pension-credit-take-up",
        "source_type",
        "manual",
    ),
]


def run(execute: bool) -> None:
    changed = skipped = missing = 0
    with app.app_context():
        for (theme_slug, source_id, field, new_value) in FIXES:
            row = HeadlineStat.query.filter_by(
                theme_slug=theme_slug, source_id=source_id,
            ).first()
            if row is None:
                print(f"  MISSING {theme_slug} / {source_id}")
                missing += 1
                continue

            current = getattr(row, field)
            if current == new_value:
                print(f"  SAME    {theme_slug} / {source_id} - {field} already correct")
                skipped += 1
                continue

            print(f"  {'SET ' if execute else 'DRY '} {theme_slug} / {source_id}")
            print(f"    {field}: {current!r}")
            print(f"           -> {new_value!r}")
            if execute:
                setattr(row, field, new_value)
            changed += 1

        if execute and changed:
            db.session.commit()
            print(f"\nCommitted {changed} change(s). {skipped} same. {missing} missing.")
        else:
            print(f"\nDRY RUN - {changed} would change, {skipped} same, {missing} missing.")
            print("Pass --execute to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    run(execute=args.execute)
