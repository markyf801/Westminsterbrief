"""Specific-topic hygiene in the tagger: acronym expansion, generic one-word themes dropped."""

import pytest

from hansard_archive.tagger import _clean_themes, normalise_specific_theme


@pytest.mark.parametrize("raw, expected", [
    ("SEND", "special educational needs"),          # acronym, not the verb
    ("  Send ", "special educational needs"),
    ("SEN", "special educational needs"),
    ("send funding", "special educational needs funding"),   # canonical child topic
    ("Chagos Archipelago", "chagos islands"),       # topic_canon alias
    ("school funding", "school funding"),           # unmapped multi-word themes keep their wording
    ("NHS   waiting  times", "nhs waiting times"),
    ("Housing", None),                              # restates a policy area
    ("immigration", None),
    ("Health and social care", None),               # a policy-area name verbatim
    ("social housing", "social housing"),
    ("", None),
])
def test_normalise_specific_theme(raw, expected):
    assert normalise_specific_theme(raw) == expected


def test_clean_themes_dedupes_and_caps():
    raw = ["SEND", "special educational needs", "housing", "ehcp delays", "a", "b", "c", "d", None, 3]
    assert _clean_themes(raw) == ["special educational needs", "education health and care plans", "a", "b", "c"]
