"""
Tests for hansard_archive.slugs.slugify_theme().

Edge cases documented in CLAUDE.md (SEO conventions) and the function
docstring. Run: python -m pytest tests/test_slugify_theme.py -v
"""

import pytest
from hansard_archive.slugs import slugify_theme


class TestSlugifyTheme:

    # --- normal cases ---

    def test_plain_string(self):
        assert slugify_theme("Health and social care") == "health-and-social-care"

    def test_already_lowercase(self):
        assert slugify_theme("home affairs") == "home-affairs"

    def test_mixed_case(self):
        assert slugify_theme("Foreign Affairs") == "foreign-affairs"

    def test_numeric(self):
        # Hyphens and digits are in the allowed set — preserved as-is
        assert slugify_theme("COVID-19") == "covid-19"

    def test_existing_hyphen_preserved(self):
        assert slugify_theme("Pre-16 education") == "pre-16-education"

    # --- whitespace ---

    def test_multiple_spaces_collapse(self):
        assert slugify_theme("Public  spending") == "public-spending"

    def test_leading_trailing_whitespace_stripped(self):
        assert slugify_theme("  Education  ") == "education"

    # --- apostrophes ---

    def test_straight_apostrophe_stripped(self):
        # "Children's" -> "childrens", no replacement
        assert slugify_theme("Children's services") == "childrens-services"

    def test_curly_apostrophe_stripped(self):
        # Smart/curly apostrophe — same behaviour as straight
        assert slugify_theme("Children’s services") == "childrens-services"

    # --- ampersands ---

    def test_ampersand_dropped_not_substituted(self):
        # "&" is stripped; surrounding spaces collapse to one hyphen.
        # Result is "health-social-care", NOT "health-and-social-care".
        assert slugify_theme("Health & social care") == "health-social-care"

    def test_multiple_special_chars(self):
        # "&" and "!" both stripped; double space left by "&" collapses
        assert slugify_theme("Drugs & alcohol policy!") == "drugs-alcohol-policy"

    # --- non-ASCII ---

    def test_non_ascii_dropped_not_transliterated(self):
        # "é" is not in [a-z0-9\s-] so it is dropped entirely.
        # "Café" -> "caf", not "cafe".
        assert slugify_theme("Café culture") == "caf-culture"

    # --- empty / degenerate inputs ---

    def test_empty_string(self):
        assert slugify_theme("") == ""

    def test_only_special_chars(self):
        # Everything stripped -> empty string
        assert slugify_theme("***") == ""

    def test_only_whitespace(self):
        # strip() removes it, result is empty
        assert slugify_theme("   ") == ""
