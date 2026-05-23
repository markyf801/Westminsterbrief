"""
Seed the StatProducer registry with ~29 known UK statistics-producing bodies.

All rows are seeded with authorisation_status='candidate'. Mark reviews and
authorises each producer in the admin UI after the seed has been reviewed.

Dry-run is the DEFAULT — lists every proposed insert without writing.
Pass --execute to write to the database.

Usage:
  python scripts/seed_stat_producers.py           # dry run
  python scripts/seed_stat_producers.py --execute  # write to DB

The model-level SQLAlchemy event listener (hansard_archive/models.py) creates
a StatLicenceAuditLog entry for every insert automatically — no special handling
needed here. Verify after running: audit log count should equal producer count.

Idempotent by slug — running twice will not create duplicates.

Producers where licence could not be verified within ~2 minutes have been set
to Unverified with null evidence URLs. Mark should review these and update:
  - office-for-students  (OfS: likely OGL v3 but their copyright page is unclear)
  - ofcom                (Ofcom uses its own open licence, not standard OGL v3)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

log = logging.getLogger("seed_stat_producers")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# The canonical OGL v3 reference URL (live, Wayback-indexed).
# https://www.gov.uk/help/copyright returns 404 — do not use that URL.
_GOV_COPYRIGHT = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
_OGL_CANONICAL = _GOV_COPYRIGHT


def _get_wayback_url(raw_url: str) -> str | None:
    """
    Query the Wayback Machine availability API for the closest snapshot of raw_url.
    Returns the snapshot URL on success, or None if unavailable or on any error.
    Non-blocking: 10-second timeout, all exceptions swallowed.
    """
    try:
        import requests
        resp = requests.get(
            "https://archive.org/wayback/available",
            params={"url": raw_url},
            timeout=10,
        )
        data = resp.json()
        snapshot = data.get("archived_snapshots", {}).get("closest", {})
        if snapshot.get("available"):
            return snapshot["url"]
    except Exception:
        pass
    return None


def build_seed_data() -> list[dict]:
    """
    Return the list of producer dicts to seed. No DB access.

    Each dict contains all fields needed to construct a StatProducer row
    except licence_evidence_wayback_url, which is populated at seed time
    via Wayback API (or set to None if unavailable).
    """
    return [
        # ----------------------------------------------------------------
        # GSS producers / independent statistics bodies
        # ----------------------------------------------------------------
        {
            "slug": "office-for-national-statistics",
            "name": "Office for National Statistics",
            "short_name": "ONS",
            "producer_type": "gss_producer",
            "web_root_url": "https://www.ons.gov.uk/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "The UK's largest independent producer of official statistics, "
                "covering the economy, population, and society."
            ),
        },
        # ----------------------------------------------------------------
        # Central government departments
        # ----------------------------------------------------------------
        {
            "slug": "department-for-education",
            "name": "Department for Education",
            "short_name": "DfE",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-education",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes education statistics including school performance, "
                "higher education, skills and children's social care."
            ),
        },
        {
            "slug": "department-of-health-and-social-care",
            "name": "Department of Health and Social Care",
            "short_name": "DHSC",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-of-health-and-social-care",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes statistics on NHS performance, social care, "
                "public health and health spending."
            ),
        },
        {
            "slug": "hm-treasury",
            "name": "HM Treasury",
            "short_name": "HMT",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/hm-treasury",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes public finance statistics including borrowing, "
                "debt and fiscal forecasts."
            ),
        },
        {
            "slug": "cabinet-office",
            "name": "Cabinet Office",
            "short_name": None,
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/cabinet-office",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes civil service statistics and government workforce data."
            ),
        },
        {
            "slug": "home-office",
            "name": "Home Office",
            "short_name": None,
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/home-office",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes immigration, police, crime and fire and rescue statistics."
            ),
        },
        {
            "slug": "ministry-of-justice",
            "name": "Ministry of Justice",
            "short_name": "MoJ",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/ministry-of-justice",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes courts, prisons, probation and legal aid statistics."
            ),
        },
        {
            "slug": "ministry-of-housing-communities-and-local-government",
            "name": "Ministry of Housing, Communities and Local Government",
            "short_name": "MHCLG",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/ministry-of-housing-communities-and-local-government",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes housing, planning, local government finance "
                "and English Indices of Deprivation."
            ),
        },
        {
            "slug": "department-for-work-and-pensions",
            "name": "Department for Work and Pensions",
            "short_name": "DWP",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-work-pensions",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes benefits, pensions and labour market statistics."
            ),
        },
        {
            "slug": "department-for-business-and-trade",
            "name": "Department for Business and Trade",
            "short_name": "DBT",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-business-and-trade",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes trade, investment and business statistics."
            ),
        },
        {
            "slug": "department-for-energy-security-and-net-zero",
            "name": "Department for Energy Security and Net Zero",
            "short_name": "DESNZ",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-energy-security-and-net-zero",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes energy prices, generation, consumption and "
                "net zero progress statistics."
            ),
        },
        {
            "slug": "department-for-transport",
            "name": "Department for Transport",
            "short_name": "DfT",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-transport",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes road, rail, aviation, maritime and "
                "road casualty statistics."
            ),
        },
        {
            "slug": "department-for-environment-food-and-rural-affairs",
            "name": "Department for Environment, Food and Rural Affairs",
            "short_name": "Defra",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-environment-food-rural-affairs",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes agriculture, food, environment and "
                "rural affairs statistics."
            ),
        },
        {
            "slug": "department-for-science-innovation-and-technology",
            "name": "Department for Science, Innovation and Technology",
            "short_name": "DSIT",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-science-innovation-and-technology",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes R&D expenditure, broadband and "
                "digital economy statistics."
            ),
        },
        {
            "slug": "department-for-culture-media-and-sport",
            "name": "Department for Culture, Media and Sport",
            "short_name": "DCMS",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/department-for-culture-media-sport",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes sport participation, arts engagement, "
                "tourism and broadband statistics."
            ),
        },
        {
            "slug": "ministry-of-defence",
            "name": "Ministry of Defence",
            "short_name": "MoD",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/ministry-of-defence",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes defence spending, armed forces personnel "
                "and equipment statistics."
            ),
        },
        {
            "slug": "foreign-commonwealth-and-development-office",
            "name": "Foreign, Commonwealth and Development Office",
            "short_name": "FCDO",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/foreign-commonwealth-development-office",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes UK official development assistance (ODA) statistics."
            ),
        },
        {
            "slug": "hm-revenue-and-customs",
            "name": "HM Revenue and Customs",
            "short_name": "HMRC",
            "producer_type": "central_department",
            "web_root_url": "https://www.gov.uk/government/organisations/hm-revenue-customs",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes tax receipts, child benefit, "
                "trade and VAT statistics."
            ),
        },
        # ----------------------------------------------------------------
        # Independent statutory fiscal body
        # ----------------------------------------------------------------
        {
            "slug": "office-for-budget-responsibility",
            "name": "Office for Budget Responsibility",
            "short_name": "OBR",
            "producer_type": "ndpb",
            "web_root_url": "https://obr.uk/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Independent statutory body that scrutinises the public finances. "
                "Publishes Economic and Fiscal Outlooks, Fiscal Sustainability "
                "Reports, Welfare Trends Reports and the Expenditure Audit."
            ),
        },
        # ----------------------------------------------------------------
        # Other public bodies / arm's-length bodies
        # ----------------------------------------------------------------
        {
            "slug": "nhs-england",
            "name": "NHS England",
            "short_name": "NHSE",
            "producer_type": "other_public_body",
            "web_root_url": "https://www.england.nhs.uk/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": "https://www.england.nhs.uk/contact-us/privacy-notice/",
            "description": (
                "Publishes NHS performance statistics including A&E waiting times, "
                "referral to treatment and ambulance data."
            ),
        },
        {
            "slug": "uk-health-security-agency",
            "name": "UK Health Security Agency",
            "short_name": "UKHSA",
            "producer_type": "executive_agency",
            "web_root_url": "https://www.gov.uk/government/organisations/uk-health-security-agency",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes infectious disease surveillance, vaccination "
                "and health protection statistics."
            ),
        },
        # ----------------------------------------------------------------
        # Regulators
        # ----------------------------------------------------------------
        {
            "slug": "office-for-students",
            "name": "Office for Students",
            "short_name": "OfS",
            "producer_type": "regulator",
            "web_root_url": "https://www.officeforstudents.org.uk/",
            "licence": "Unverified",
            "licence_evidence_raw_url": None,
            "description": (
                "Publishes higher education access, participation "
                "and outcomes statistics."
            ),
        },
        {
            "slug": "ofsted",
            "name": "Ofsted",
            "short_name": "Ofsted",
            "producer_type": "regulator",
            "web_root_url": "https://www.gov.uk/government/organisations/ofsted",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes school inspection outcomes, early years "
                "and further education inspection statistics."
            ),
        },
        {
            "slug": "ofcom",
            "name": "Ofcom",
            "short_name": "Ofcom",
            "producer_type": "regulator",
            "web_root_url": "https://www.ofcom.org.uk/",
            "licence": "Unverified",
            "licence_evidence_raw_url": None,
            "description": (
                "Publishes telecommunications, broadcasting and "
                "postal sector statistics."
            ),
        },
        {
            "slug": "ofgem",
            "name": "Ofgem",
            "short_name": "Ofgem",
            "producer_type": "regulator",
            "web_root_url": "https://www.ofgem.gov.uk/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": _GOV_COPYRIGHT,
            "description": (
                "Publishes energy market statistics including the "
                "Ofgem energy price cap."
            ),
        },
        # ----------------------------------------------------------------
        # Higher education-specific bodies
        # ----------------------------------------------------------------
        {
            "slug": "hesa-jisc",
            "name": "HESA / Jisc",
            "short_name": "HESA",
            "producer_type": "other_public_body",
            "web_root_url": "https://www.hesa.ac.uk/",
            "licence": "HESA_Open",
            "licence_evidence_raw_url": "https://www.hesa.ac.uk/about/copyright",
            "description": (
                "Publishes higher education statistics on student numbers, "
                "staff, finance and widening participation."
            ),
        },
        {
            "slug": "ucas",
            "name": "UCAS",
            "short_name": "UCAS",
            "producer_type": "other_public_body",
            "web_root_url": "https://www.ucas.com/",
            "licence": "UCAS",
            "licence_evidence_raw_url": "https://www.ucas.com/terms-and-conditions-for-use-of-the-ucas-network",
            "description": (
                "Publishes university application, offer and acceptance statistics."
            ),
        },
        # ----------------------------------------------------------------
        # Devolved administrations
        # ----------------------------------------------------------------
        {
            "slug": "scottish-government",
            "name": "Scottish Government",
            "short_name": "ScotGov",
            "producer_type": "devolved_administration",
            "web_root_url": "https://www.gov.scot/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": "https://www.gov.scot/crown-copyright/",
            "description": (
                "Publishes Scotland-specific statistics across education, "
                "health, economy and justice."
            ),
        },
        {
            "slug": "welsh-government",
            "name": "Welsh Government / StatsWales",
            "short_name": "WelshGov",
            "producer_type": "devolved_administration",
            "web_root_url": "https://statswales.gov.wales/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": "https://www.gov.wales/copyright-statement",
            "description": (
                "Publishes Wales-specific statistics via StatsWales, covering "
                "health, education, economy and Welsh language."
            ),
        },
        {
            "slug": "nisra",
            "name": "Northern Ireland Statistics and Research Agency",
            "short_name": "NISRA",
            "producer_type": "devolved_body",
            "web_root_url": "https://www.nisra.gov.uk/",
            "licence": "OGL_v3",
            "licence_evidence_raw_url": "https://www.nisra.gov.uk/crown-copyright",
            "description": (
                "Publishes Northern Ireland official statistics on population, "
                "health, economy and the census."
            ),
        },
    ]


def validate_seed_data(producers: list[dict]) -> list[str]:
    """Return a list of problem descriptions. Empty = all clear."""
    problems = []
    slugs_seen: set[str] = set()
    for p in producers:
        if p["slug"] in slugs_seen:
            problems.append(f"Duplicate slug: {p['slug']!r}")
        slugs_seen.add(p["slug"])
        if p["licence"] != "Unverified" and not p.get("licence_evidence_raw_url"):
            problems.append(
                f"{p['slug']}: licence={p['licence']!r} but licence_evidence_raw_url is null"
            )
    return problems


def seed(db_session, StatProducer, producers: list[dict], execute: bool) -> None:
    """
    Dry-run (execute=False): print proposed inserts, no DB writes.
    Execute (execute=True): insert missing producers, skip existing slugs.
    """
    existing_slugs = {
        row[0] for row in db_session.query(StatProducer.slug).all()
    }
    to_insert = [p for p in producers if p["slug"] not in existing_slugs]
    to_skip   = [p for p in producers if p["slug"] in existing_slugs]

    for p in to_skip:
        log.info("SKIP (already exists): %s", p["slug"])

    if not to_insert:
        log.info("Nothing to insert — all %d producers already in DB.", len(producers))
        return

    mode = "EXECUTE" if execute else "DRY RUN"
    log.info("[%s] %d producer(s) to insert:", mode, len(to_insert))

    for p in to_insert:
        wayback = None
        if execute and p.get("licence_evidence_raw_url"):
            log.info("  Fetching Wayback snapshot for %s ...", p["slug"])
            wayback = _get_wayback_url(p["licence_evidence_raw_url"])
            if wayback is None:
                log.warning(
                    "  WARN: no Wayback snapshot for %s — wayback URL set to NULL; "
                    "investigate later.",
                    p["slug"],
                )

        log.info(
            "  %s  licence=%s  evidence=%s",
            p["slug"],
            p["licence"],
            p.get("licence_evidence_raw_url") or "NULL",
        )

        if not execute:
            continue

        producer = StatProducer(
            slug=p["slug"],
            name=p["name"],
            short_name=p.get("short_name"),
            producer_type=p["producer_type"],
            web_root_url=p["web_root_url"],
            licence=p["licence"],
            licence_evidence_raw_url=p.get("licence_evidence_raw_url"),
            licence_evidence_wayback_url=wayback,
            description=p.get("description"),
            authorisation_status="candidate",
        )
        db_session.add(producer)
        db_session.flush()  # materialise id so event listener can write audit row

    if execute:
        db_session.commit()
        log.info(
            "Done. Inserted %d StatProducer row(s). "
            "Audit log entries are created automatically by the model hook.",
            len(to_insert),
        )
    else:
        log.info(
            "[DRY RUN] No rows written. Pass --execute to perform inserts."
        )


def run(execute: bool = False) -> None:
    from flask_app import app, db
    from hansard_archive.models import StatProducer

    producers = build_seed_data()

    problems = validate_seed_data(producers)
    if problems:
        for p in problems:
            log.error("SEED DATA ERROR: %s", p)
        sys.exit(1)

    with app.app_context():
        seed(db.session, StatProducer, producers, execute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Seed the StatProducer registry with known UK statistics producers. "
            "Dry run by default — pass --execute to write."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Write inserts to the database. Without this flag the script is a dry run.",
    )
    args = parser.parse_args()
    run(execute=args.execute)


if __name__ == "__main__":
    main()
