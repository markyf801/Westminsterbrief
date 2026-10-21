"""
hansard_archive/policy_areas.py

Single source of truth for the Westminster Brief policy areas —
URL slugs, UI display labels, and the canonical Hansard DB policy-area
names stored in ha_session_theme.theme (theme_type='policy_area').

These "brief slugs" differ from slugify_theme(canonical_name) in several
cases (e.g. "education" vs "education-training-and-skills") because the
URL slugs were chosen for brevity and readability independently of the
GOV.UK taxonomy strings.  slugify_theme() in slugs.py remains the primitive
for generating slugs from arbitrary strings; this module is the authority
for the known policy areas only.

Usage::

    from hansard_archive import policy_areas

    for pa in policy_areas.all_areas():
        print(pa.slug, pa.label)

    pa = policy_areas.by_slug("economy")   # PolicyArea | None
    names = policy_areas.hansard_names_for_slug("international-affairs")
    # ["International development", "Foreign affairs and diplomacy"]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PolicyArea:
    """Represents one of the Westminster Brief policy areas."""

    slug: str
    """URL slug used in /brief/<slug> and /stats/<slug>."""

    label: str
    """UI display name (e.g. "Economy", "Crime, justice and law")."""

    hansard_names: tuple[str, ...]
    """
    Canonical policy-area strings stored in ha_session_theme.theme.

    Usually a single-element tuple, but some brief slugs aggregate two
    Hansard policy areas (e.g. "international-affairs" covers both
    "International development" and "Foreign affairs and diplomacy").
    """


# ---------------------------------------------------------------------------
# The policy areas (23 original + Children and families, 2026-10-20) —
# ordered for the /stats index display.
# Slugs are locked: changing them would break indexed URLs.
# ---------------------------------------------------------------------------

_POLICY_AREAS: list[PolicyArea] = [
    PolicyArea("economy",
               "Economy",
               ("Economy",)),
    PolicyArea("employment-and-labour-market",
               "Employment and labour market",
               ("Employment and labour market",)),
    PolicyArea("finance-and-taxation",
               "Finance and taxation",
               ("Finance and taxation",)),
    PolicyArea("government-and-public-administration",
               "Government and public administration",
               ("Government and public administration",)),
    PolicyArea("business-and-industry",
               "Business and industry",
               ("Business and industry",)),
    # Education covers "Education, training and skills" only (owner decision,
    # 2026-10-20): childcare and family items have their own page below.
    PolicyArea("education",
               "Education",
               ("Education, training and skills",)),
    PolicyArea("children-and-families",
               "Children and families",
               ("Children and families",)),
    PolicyArea("health-and-social-care",
               "Health and social care",
               ("Health and social care",)),
    PolicyArea("housing-and-planning",
               "Housing and planning",
               ("Housing and planning",)),
    PolicyArea("transport",
               "Transport",
               ("Transport",)),
    PolicyArea("crime-justice-and-law",
               "Crime, justice and law",
               ("Crime, justice and law",)),
    PolicyArea("welfare-and-social-security",
               "Welfare and social security",
               ("Welfare and benefits",)),
    PolicyArea("immigration-and-asylum",
               "Immigration and asylum",
               ("Immigration and borders",)),
    PolicyArea("environment-and-climate-change",
               "Environment and climate change",
               ("Environment",)),
    PolicyArea("defence-and-national-security",
               "Defence and national security",
               ("Defence and armed forces",)),
    PolicyArea("international-affairs",
               "International affairs",
               ("International development", "Foreign affairs and diplomacy")),
    PolicyArea("science-technology-and-innovation",
               "Science, technology and innovation",
               ("Science and technology",)),
    PolicyArea("energy-and-utilities",
               "Energy and utilities",
               ("Energy",)),
    PolicyArea("work-and-pensions",
               "Work and pensions",
               ("Welfare and benefits", "Employment and labour market")),
    PolicyArea("agriculture-environment-and-rural-affairs",
               "Agriculture, environment and rural affairs",
               ("Environment",)),
    PolicyArea("culture-media-and-sport",
               "Culture, media and sport",
               ("Society and culture",)),
    PolicyArea("constitutional-affairs",
               "Constitutional affairs",
               ("Parliament and constitution",)),
    PolicyArea("foreign-affairs",
               "Foreign affairs",
               ("Foreign affairs and diplomacy",)),
    PolicyArea("parliamentary-affairs",
               "Parliamentary affairs",
               ("Parliament and constitution",)),
]

# ---------------------------------------------------------------------------
# Canonical Hansard policy area names — the controlled vocabulary used in
# ha_session_theme.theme (theme_type='policy_area') and enforced as a Gemini
# enum in the tagger.  This list differs slightly from the brief/stats
# page slugs above: "Local government" and "Trade" are valid tagger outputs
# but have no dedicated brief or stats page (they fall under broader pages).
# ---------------------------------------------------------------------------

HANSARD_POLICY_NAMES: list[str] = [
    "Business and industry",
    "Children and families",
    "Crime, justice and law",
    "Defence and armed forces",
    "Economy",
    "Education, training and skills",
    "Employment and labour market",
    "Energy",
    "Environment",
    "Finance and taxation",
    "Foreign affairs and diplomacy",
    "Government and public administration",
    "Health and social care",
    "Housing and planning",
    "Immigration and borders",
    "International development",
    "Local government",
    "Parliament and constitution",
    "Science and technology",
    "Society and culture",
    "Trade",
    "Transport",
    "Welfare and benefits",
]

# ---------------------------------------------------------------------------
# One-line scope note per Hansard policy area, with exclusions. Shown to the
# tagger model next to each enum value so it picks by subject, not by word
# overlap ("Hares" is not welfare; an e-petition debate is not about Parliament).
# Every HANSARD_POLICY_NAMES entry must have a note (tests enforce it).
# ---------------------------------------------------------------------------

POLICY_AREA_SCOPE: dict[str, str] = {
    "Business and industry":
        "companies, industrial sectors, manufacturing, retail, hospitality, consumer "
        "protection, insolvency, post offices and banking access. Not tax policy "
        "(Finance and taxation) or workers' rights (Employment and labour market).",
    "Children and families":
        "childcare, early years, child poverty programmes, children's social care, "
        "fostering, adoption, family law, parenthood and young carers. Not schools "
        "or SEND (Education, training and skills).",
    "Crime, justice and law":
        "policing, crime, courts, prisons, probation, sentencing, victims, legal aid "
        "and law reform. Not immigration enforcement (Immigration and borders) or "
        "national security threats from states (Defence / Foreign affairs).",
    "Defence and armed forces":
        "armed forces, military equipment, defence spending, veterans, bases and "
        "deployments. Not diplomacy or sanctions (Foreign affairs and diplomacy).",
    "Economy":
        "macroeconomy, growth, inflation, interest rates, cost of living, productivity "
        "and public finances overall. Not specific taxes (Finance and taxation).",
    "Education, training and skills":
        "schools, SEND, further and higher education, apprenticeships, skills, exams "
        "and teachers. Not childcare or early years (Children and families).",
    "Employment and labour market":
        "jobs, unemployment, workers' rights, pay, trade unions, flexible working and "
        "workplace safety. Not benefits (Welfare and benefits).",
    "Energy":
        "energy supply and prices, electricity grid, oil and gas, nuclear, renewables, "
        "energy efficiency and fuel poverty. Not climate or pollution in general (Environment).",
    "Environment":
        "climate change, pollution, water and sewage, nature and wildlife, animal "
        "welfare, farming, fisheries, food and rural affairs. Not energy supply (Energy).",
    "Finance and taxation":
        "taxes, tax reliefs, national insurance, budgets, financial services and "
        "regulation, pensions tax. Not benefit levels (Welfare and benefits).",
    "Foreign affairs and diplomacy":
        "relations with other countries and the EU, treaties, sanctions, conflicts "
        "abroad, overseas territories and consular cases. Not aid spending "
        "(International development) or the armed forces (Defence and armed forces).",
    "Government and public administration":
        "how government is run: departments, civil service, public appointments, "
        "procurement, public bodies, data and digital government. Not Parliament "
        "itself or elections (Parliament and constitution).",
    "Health and social care":
        "NHS, hospitals, medicines, public health, mental health, adult social care "
        "and care workers. Not disability benefits (Welfare and benefits).",
    "Housing and planning":
        "housebuilding, social and private rented housing, homelessness, rough "
        "sleeping, building safety, planning system and leasehold.",
    "Immigration and borders":
        "visas, asylum, refugees, small boats, deportation, border control and "
        "citizenship. Not foreign relations generally (Foreign affairs and diplomacy).",
    "International development":
        "overseas aid, humanitarian relief, global health and poverty abroad. Not "
        "diplomacy or trade (Foreign affairs and diplomacy, Trade).",
    "Local government":
        "councils, local government finance and reorganisation, devolution deals, "
        "mayors, local services such as bins, libraries and potholes.",
    "Parliament and constitution":
        "Parliament's own procedures and standards, elections, voting, party "
        "funding, devolution settlements and the constitution. Only when that is the "
        "subject: a petition, debate or question ABOUT another subject is not this area.",
    "Science and technology":
        "research funding, science, AI, digital and telecoms, broadband, online "
        "safety and space. Not general business support (Business and industry).",
    "Society and culture":
        "arts, culture, heritage, media and broadcasting, sport, charities, faith, "
        "equalities and community cohesion.",
    "Trade":
        "international trade deals, tariffs, exports and imports, trade remedies. Not "
        "domestic retail (Business and industry).",
    "Transport":
        "roads, rail, buses, aviation, maritime, cycling, road safety and transport "
        "infrastructure.",
    "Welfare and benefits":
        "social security benefits: universal credit, PIP, state pension, pension "
        "credit, carer's allowance, child benefit. Not animal welfare (Environment) "
        "and not 'welfare' meaning wellbeing.",
}

# ---------------------------------------------------------------------------
# /brief/<slug> aliases — slugs that would otherwise hit an exact free-text
# "specific" tag and show a thin, misleading page. 301 to the canonical page.
#   - One-word policy synonyms the tagger emits as specific tags ("housing",
#     "health"), which the topic page then treats as a tiny separate topic.
#   - Alias spellings of specific topics (SEND -> "send" ->
#     special-educational-needs) come from topic_canon.TOPIC_FAMILIES, whose
#     canonical page also lists rows stored under the alias, so the target is
#     never empty when the alias page had content.
# Only add a slug here when the target page is the obviously right home for it.
# ---------------------------------------------------------------------------

BRIEF_SLUG_ALIASES: dict[str, str] = {
    "housing":     "housing-and-planning",
    "health":      "health-and-social-care",
    "defence":     "defence-and-national-security",
    "immigration": "immigration-and-asylum",
    "welfare":     "welfare-and-social-security",
    "crime":       "crime-justice-and-law",
}


def brief_alias(slug: str) -> Optional[str]:
    """Canonical /brief slug that `slug` should redirect to, or None."""
    if slug in BRIEF_SLUG_ALIASES:
        return BRIEF_SLUG_ALIASES[slug]
    from hansard_archive.topic_canon import alias_slug_target
    target = alias_slug_target(slug)
    if target and target not in _BY_SLUG and slug not in _BY_SLUG:
        return target
    return None


# Fast-lookup indexes built once at import time.
_BY_SLUG: dict[str, PolicyArea] = {pa.slug: pa for pa in _POLICY_AREAS}
_BY_LABEL: dict[str, PolicyArea] = {pa.label: pa for pa in _POLICY_AREAS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def all_areas() -> list[PolicyArea]:
    """All policy areas in canonical display order."""
    return _POLICY_AREAS


def display_order() -> list[PolicyArea]:
    """Alias for all_areas() — explicit name when order is the intent."""
    return _POLICY_AREAS


def by_slug(slug: str) -> Optional[PolicyArea]:
    """Return the PolicyArea for the given URL slug, or None."""
    return _BY_SLUG.get(slug)


def by_display_name(name: str) -> Optional[PolicyArea]:
    """Return the PolicyArea matching the given UI label, or None."""
    return _BY_LABEL.get(name)


def slug_from_display_name(name: str) -> str:
    """Return the URL slug for the given UI label, or '' if not found."""
    pa = _BY_LABEL.get(name)
    return pa.slug if pa else ""


def display_name_from_slug(slug: str) -> str:
    """Return the UI label for the given URL slug, or '' if not found."""
    pa = _BY_SLUG.get(slug)
    return pa.label if pa else ""


def valid_slugs() -> set[str]:
    """Set of all valid URL slugs — use for fast O(1) membership tests."""
    return set(_BY_SLUG)


def hansard_names_for_slug(slug: str) -> list[str]:
    """
    Canonical Hansard DB policy-area name(s) for a brief slug.

    These are the strings stored in ha_session_theme.theme
    (theme_type='policy_area').  One brief slug may cover multiple
    Hansard names — e.g. 'international-affairs' covers both
    'International development' and 'Foreign affairs and diplomacy'.

    Returns [] for unknown slugs.
    """
    pa = _BY_SLUG.get(slug)
    return list(pa.hansard_names) if pa else []
