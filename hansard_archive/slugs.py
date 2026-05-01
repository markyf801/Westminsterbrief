"""
Hansard Archive — URL slug generation.

Produces slugs of the form:  {title-slug}-{short-id}
Example: womens-state-pension-age-communication-phso-report-4069

Rules (approved 30 April 2026):
- title-slug: lowercase, hyphenated, stop words removed, max 60 chars at word boundary
- short-id: last 4 chars of ext_id, lowercased
- Collision fallback: last 6 chars (handled by caller via unique constraint signal)
- Containers (is_container=True) receive a slug; public pages are gated by is_container
"""

import re
import unicodedata

# Smart/curly apostrophes and quote variants — all removed (apostrophe removal rule)
_SMART_QUOTE_RE = re.compile(r"[‘’‚‛′ʼ]")

# After substitutions + lowercase + unicode strip: keep only a-z, 0-9, space
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")

# Expanded 14-word stop list (approved 30 April 2026)
_STOP_WORDS = frozenset({
    "the", "and", "of", "in",
    "a", "an", "to", "for",
    "on", "at", "by", "with",
    "from", "or",
})

_MAX_TITLE_CHARS = 60


def title_to_slug(title: str) -> str:
    """Convert a session title to a hyphenated lowercase slug, max 60 chars."""
    t = title or ""

    # 1. Collapse whitespace variants — newlines appear in multi-SI bundle titles
    t = t.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\xa0", " ")

    # 2. Remove smart/curly apostrophes (Women's → womens)
    t = _SMART_QUOTE_RE.sub("", t)

    # 3. Character substitutions before stripping
    t = t.replace("&", " and ")
    t = t.replace(":", " ")
    t = t.replace("/", " ")
    t = t.replace("-", " ")

    # 4. Lowercase
    t = t.lower()

    # 5. Unicode decomposition — drops accents, handles any remaining non-ASCII
    t = unicodedata.normalize("NFKD", t).encode("ascii", errors="ignore").decode("ascii")

    # 6. Strip all non-alphanumeric, non-space characters
    t = _NON_ALNUM_RE.sub("", t)

    # 7. Tokenise and remove stop words
    tokens = [w for w in t.split() if w and w not in _STOP_WORDS]

    if not tokens:
        return "session"

    # 8. Join words with hyphens up to the 60-char limit
    parts: list[str] = []
    length = 0
    for token in tokens:
        if not parts:
            if len(token) > _MAX_TITLE_CHARS:
                parts.append(token[:_MAX_TITLE_CHARS])
                break
            parts.append(token)
            length = len(token)
        else:
            if length + 1 + len(token) > _MAX_TITLE_CHARS:
                break
            parts.append(token)
            length += 1 + len(token)

    return "-".join(parts)


def make_slug(title: str, ext_id: str, suffix_len: int = 4) -> str:
    """
    Build the full URL slug for a Hansard session.

    suffix_len=4 is the default; pass suffix_len=6 when the 4-char slug
    has already collided (caller detects this via UniqueConstraint violation
    or explicit collision check in the backfill pass).
    """
    title_part = title_to_slug(title)
    short_id = (ext_id or "xxxx")[-suffix_len:].lower()
    return f"{title_part}-{short_id}"
