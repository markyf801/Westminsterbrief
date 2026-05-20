"""
hansard_archive/policy_areas.py

Single source of truth for the 23 Westminster Brief policy areas —
URL slugs, UI display labels, and the canonical Hansard DB policy-area
names stored in ha_session_theme.theme (theme_type='policy_area').

These "brief slugs" differ from slugify_theme(canonical_name) in several
cases (e.g. "education" vs "education-training-and-skills") because the
URL slugs were chosen for brevity and readability independently of the
GOV.UK taxonomy strings.  slugify_theme() in slugs.py remains the primitive
for generating slugs from arbitrary strings; this module is the authority
for the 23 known policy areas only.

Usage::

    from hansard_archive import policy_areas

    for pa in policy_areas.all_areas():
        print(pa.slug, pa.label)

    pa = policy_areas.by_slug("economy")   # PolicyArea | None
    names = policy_areas.hansard_names_for_slug("education")
    # ["Education, training and skills", "Children and families"]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PolicyArea:
    """Represents one of the 23 Westminster Brief policy areas."""

    slug: str
    """URL slug used in /brief/<slug> and /stats/<slug>."""

    label: str
    """UI display name (e.g. "Economy", "Crime, justice and law")."""

    hansard_names: tuple[str, ...]
    """
    Canonical policy-area strings stored in ha_session_theme.theme.

    Usually a single-element tuple, but some brief slugs aggregate two
    Hansard policy areas (e.g. "education" covers both "Education, training
    and skills" and "Children and families").
    """


# ---------------------------------------------------------------------------
# The 23 policy areas — ordered for the /stats index display.
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
    PolicyArea("education",
               "Education",
               ("Education, training and skills", "Children and families")),
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
# enum in the tagger.  This list differs slightly from the 23 brief/stats
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

# Fast-lookup indexes built once at import time.
_BY_SLUG: dict[str, PolicyArea] = {pa.slug: pa for pa in _POLICY_AREAS}
_BY_LABEL: dict[str, PolicyArea] = {pa.label: pa for pa in _POLICY_AREAS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def all_areas() -> list[PolicyArea]:
    """All 23 policy areas in canonical display order."""
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
    Hansard names — e.g. 'education' covers both 'Education, training
    and skills' and 'Children and families'.

    Returns [] for unknown slugs.
    """
    pa = _BY_SLUG.get(slug)
    return list(pa.hansard_names) if pa else []
