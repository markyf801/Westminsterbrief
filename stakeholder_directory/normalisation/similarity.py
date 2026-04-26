"""
String similarity scoring for organisation name deduplication (Tier 2 / Tier 3).
"""
from rapidfuzz import fuzz
from stakeholder_directory.normalisation.string_utils import normalise_for_match


def compute_similarity(
    name_a: str,
    name_b: str,
    aliases: dict[str, list[str]] | None = None,
) -> float:
    """Return 0.0–1.0 similarity using rapidfuzz token_set_ratio.

    Both names are normalised via normalise_for_match before comparison.
    token_set_ratio handles word reordering ('Royal College of GPs' vs
    'GPs, Royal College of') better than basic ratio.
    """
    norm_a = normalise_for_match(name_a, aliases)
    norm_b = normalise_for_match(name_b, aliases)
    return fuzz.token_set_ratio(norm_a, norm_b) / 100.0
