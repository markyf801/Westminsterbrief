"""
hansard_archive/topic_match.py

Text matching for /brief/<slug> topic pages (pure functions, no DB).

A topic page used to list only items whose stored free-text tag slugified to
exactly the page slug. Tags are free text, so one topic is stored in many
spellings ("support for ukraine", "ukraine defence support"), and some debates
are never tagged at all (debate_type 'other', which covers many Lords debates).
The page therefore also matches:

  - stored topic tags that CONTAIN the topic phrase as whole words
    ("animal welfare" -> "animal welfare strategy", "farm animal welfare");
  - debate titles and written-question headings that contain the phrase
    ("Ukraine and Russia", "Local Authority SEND Services", "Cats: Microchips").

Whole words, with a plural "s"/"es" allowed on each word: "cats" matches "cat
welfare" and "Cats and Ferrets", but not "Catapults". Short acronym aliases from
topic_canon ("SEND", "SEN", "EHCP") must be upper case in a title or heading, so
the verb "send" does not match.
"""

from __future__ import annotations

import re
import unicodedata

from hansard_archive import topic_canon as _tc

_APOSTROPHES = re.compile(r"['‘’‚‛′ʼ]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Phrases shorter than this are only matched exactly (as tags), never inside
# other text: "sen" is inside too many words for a LIKE pre-filter.
_MIN_CONTAINS_CHARS = 4

# Acronym aliases: one word of at most this many letters (SEND, EHCP, BIOT).
_ACRONYM_MAX_CHARS = 5

# One-word phrases too generic to look for inside titles, headings or other
# tags: words naming a kind of parliamentary business, or so broad that every
# title would match. The page still lists items tagged with the exact topic.
_NO_EXPANSION_WORDS = frozenset({
    "act", "acts", "amendment", "amendments", "bill", "bills", "business",
    "committee", "committees", "debate", "debates", "government", "house",
    "lords", "commons", "minister", "ministers", "motion", "order", "orders",
    "parliament", "petition", "petitions", "policy", "question", "questions",
    "regulations", "report", "reports", "statement", "statements", "update",
    "uk", "england", "britain", "national", "local", "public", "support",
    "services", "funding", "reform", "legislation", "engagements",
})


def normalise(text: str) -> str:
    """Lowercase, accents and apostrophes removed, anything else non-alphanumeric to one space."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", errors="ignore").decode("ascii")
    t = _APOSTROPHES.sub("", t.lower())
    return " ".join(_NON_ALNUM.sub(" ", t).split())


def _base(token: str) -> str:
    """Token with one plural 's' removed ("cats" -> "cat"); 'ss', 'us', 'is' endings kept."""
    if len(token) >= 4 and token.endswith("s") and token[-2] not in "sui":
        return token[:-1]
    return token


class Phrase:
    """One topic phrase, with its whole-word pattern and SQL LIKE pre-filter."""

    def __init__(self, text: str, acronym: bool = False):
        self.text = normalise(text)
        self.tokens = self.text.split()
        self.acronym = acronym and len(self.tokens) == 1
        bases = [_base(t) for t in self.tokens]
        self._like = "%".join(bases) + "%" if bases else ""
        self._pg_body = ".*".join(bases)   # bases are [a-z0-9] only: nothing to escape
        body = r" ".join(re.escape(b) + r"(?:s|es)?" for b in bases)
        self._re = re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])") if bases else None
        self._acronym_re = (
            re.compile(r"(?<![A-Za-z0-9])" + re.escape(self.text.upper()) + r"[sS]?(?![A-Za-z0-9])")
            if self.acronym else None
        )

    def __repr__(self):
        return f"Phrase({self.text!r}{', acronym' if self.acronym else ''})"

    def tag_like_patterns(self) -> list[str]:
        """LIKE pre-filters for stored lowercase tags: the phrase starting at a word start."""
        if not self._like:
            return []
        return [self._like, "% " + self._like, "%-" + self._like]

    def title_like_pattern(self) -> str:
        """LIKE pre-filter for lower(title) / lower(heading): the phrase anywhere."""
        return "%" + self._like if self._like else ""

    @property
    def expandable(self) -> bool:
        """Worth looking for inside other text (not too short, not a generic word)."""
        return (len(self.text) >= _MIN_CONTAINS_CHARS
                and not (len(self.tokens) == 1 and self.tokens[0] in _NO_EXPANSION_WORDS))

    def in_tag(self, tag: str) -> bool:
        """Whole-word match inside a stored (lowercase) topic tag."""
        return bool(self._re and self._re.search(normalise(tag)))

    def in_title(self, title: str) -> bool:
        """Whole-word match inside a debate title or question heading (acronyms upper case)."""
        if self._acronym_re is not None:
            return bool(self._acronym_re.search(title or ""))
        return bool(self._re and self._re.search(normalise(title)))


def phrases_for_slug(slug: str) -> list[Phrase]:
    """
    Phrases a /brief/<slug> page looks for: the slug's own words, plus, when the
    slug is a canonical topic in topic_canon, every spelling in its family (the
    parent's page gets the children too). Short one-word aliases are acronyms.
    """
    out: dict[str, Phrase] = {}

    def add(text: str, acronym: bool = False) -> None:
        p = Phrase(text, acronym)
        if p.text and p.text not in out:
            out[p.text] = p

    add(slug.replace("-", " "))
    canon = _tc.canonical_for_slug(slug)
    if canon:
        canon_names = set(_tc.known_canonical_topics())
        for name in sorted(_tc.topic_family_names(canon)):
            one_word = len(normalise(name).split()) == 1
            add(name, acronym=one_word and name not in canon_names
                and len(name) <= _ACRONYM_MAX_CHARS)
    return list(out.values())


def expandable(phrases: list[Phrase]) -> list[Phrase]:
    """The phrases worth looking for inside other text, with phrases already
    implied by a shorter one removed ("send provision" is covered by "send")."""
    cands = [p for p in phrases if p.expandable]
    keep: list[Phrase] = []
    for p in sorted(cands, key=lambda p: (len(p.tokens), len(p.text))):
        if not any(k.in_tag(p.text) for k in keep):
            keep.append(p)
    return keep


def tag_pg_regex(phrases: list[Phrase]) -> str:
    """
    One Postgres regex equivalent to every phrase's tag_like_patterns() ORed:
    a phrase starting at the start of the tag or after a space or hyphen. One
    regex per row is several times faster than dozens of LIKEs (SEND has 12
    phrases). Empty when there is nothing to match.
    """
    bodies = [p._pg_body for p in phrases if p._pg_body]
    return f"(^|[ -])({'|'.join(bodies)})" if bodies else ""


def tag_matches(tag: str, phrases: list[Phrase]) -> bool:
    return any(p.in_tag(tag) for p in phrases)


def title_matches(title: str, phrases: list[Phrase]) -> bool:
    return any(p.in_title(title) for p in phrases)
