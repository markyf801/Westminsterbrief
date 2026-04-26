"""
Pure-text normalisation helpers for organisation name deduplication.

Used only for matching; canonical names are always stored in their original form.
"""
import re

_LEGAL_SUFFIXES = re.compile(
    r'\b(ltd|limited|plc|llp|llc|inc\.?|incorporated|co\.?ltd|cic|'
    r'community interest company|charitable incorporated organisation|cio)\b',
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r'\s+')
_PUNCTUATION = re.compile(r"[^\w\s]")


def strip_legal_suffixes(name: str) -> str:
    """Remove ' Ltd', ' Limited', ' plc', ' LLP', ' Inc.', etc."""
    return _LEGAL_SUFFIXES.sub('', name).strip()


def normalise_for_match(name: str, aliases: dict[str, list[str]] | None = None) -> str:
    """Aggressive normalisation for exact-match dedup (Tier 1).

    Lowercases, strips punctuation, collapses whitespace, removes legal
    suffixes, expands aliases from config. Used only for matching, never stored.
    """
    n = name.strip()
    if aliases:
        n = expand_aliases(n, aliases)
    n = strip_legal_suffixes(n)
    n = n.lower()
    n = _PUNCTUATION.sub(' ', n)
    n = _WHITESPACE.sub(' ', n).strip()
    return n


def expand_aliases(name: str, aliases: dict[str, list[str]]) -> str:
    """Look up name in alias map and return canonical form if known.

    Matching is case-insensitive. If name matches an alias value, returns
    the canonical key. Otherwise returns name unchanged.

    Examples:
        'RCGP' -> 'Royal College of General Practitioners'
        'UUK'  -> 'Universities UK'
    """
    name_lower = name.strip().lower()
    for canonical, alias_list in aliases.items():
        if name_lower == canonical.lower():
            return canonical
        for alias in alias_list:
            if name_lower == alias.strip().lower():
                return canonical
    return name
