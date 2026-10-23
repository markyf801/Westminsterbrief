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
    ("Animal Welfare in Farming", "Local Authority SEND Services").

Sense guards (one word often means something else):
  - A one-word topic is only looked for inside other text when topic_canon has
    a family for it ("ukraine", "cats"); otherwise its page lists exact tags
    only. "arms" would match "Arm's-Length Bodies", "turkey" the bird, "trade"
    "trade unions".
  - Words match as written, plus a plural "s"/"es" on the last word ("cat" ->
    "cats"). Nothing is stripped, and a possessive "'s" is its own token, so it
    never makes a plural ("arm's" is "arm s", not "arms").
  - A family's "exclude" texts veto a match ("CAT scans" on the cats page),
    and its "upper_case_exclude" words veto a title written in upper case
    ("CAT" is an acronym there, not the animal).
  - Acronyms (topic_canon.ACRONYMS: SEND, EHCP) must be upper case in a title
    or heading, so the verb "send" does not match.
"""

from __future__ import annotations

import re
import unicodedata

from hansard_archive import topic_canon as _tc

_POSSESSIVE = re.compile(r"['‘’‚‛′ʼ]s\b")
_APOSTROPHES = re.compile(r"['‘’‚‛′ʼ]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Phrases shorter than this are only matched exactly (as tags), never inside
# other text: "sen" is inside too many words for a LIKE pre-filter. A curated
# three-letter word ("cat") is the exception; three-letter acronyms are not.
_MIN_CONTAINS_CHARS = 4

# One-word phrases never looked for inside other text, even with a family:
# words naming a kind of parliamentary business, or so broad that every title
# would match.
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
    """Lowercase, accents removed, possessive "'s" to its own "s" token, other
    apostrophes removed, anything else non-alphanumeric to one space."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", errors="ignore").decode("ascii")
    t = _POSSESSIVE.sub(" s", t.lower())
    t = _APOSTROPHES.sub("", t)
    return " ".join(_NON_ALNUM.sub(" ", t).split())


class Phrase:
    """One topic phrase, with its whole-word pattern and SQL pre-filters."""

    def __init__(self, text: str, acronym: bool = False, curated: bool = False):
        self.text = normalise(text)
        self.tokens = self.text.split()
        self.acronym = acronym and len(self.tokens) == 1
        self.curated = curated
        toks = self.tokens
        self._like = "%".join(toks) + "%" if toks else ""
        # Words separated by non-alphanumerics ("women's pensions", "russia-ukraine"):
        # far cheaper per row than ".*", and still a superset of the Python check.
        self._pg_body = "[^a-z0-9]+".join(toks)    # tokens are [a-z0-9] only: nothing to escape
        body = " ".join(re.escape(t) + (r"(?:s|es)?" if i == len(toks) - 1 else "")
                        for i, t in enumerate(toks))
        self._re = re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])") if toks else None
        self._acronym_re = (
            re.compile(r"(?<![A-Za-z0-9])" + re.escape(self.text.upper()) + r"[sS]?(?![A-Za-z0-9])")
            if self.acronym else None
        )

    def __repr__(self):
        flags = [f for f, on in (("acronym", self.acronym), ("curated", self.curated)) if on]
        return f"Phrase({self.text!r}{''.join(', ' + f for f in flags)})"

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
        """Safe to look for inside other text: long enough, and either several
        words or a one-word topic curated in topic_canon (and not generic)."""
        if len(self.text) < _MIN_CONTAINS_CHARS and not (self.curated and not self.acronym
                                                         and len(self.text) >= 3):
            return False
        if len(self.words) == 1:
            return self.curated and self.words[0] not in _NO_EXPANSION_WORDS
        return True

    @property
    def words(self) -> list[str]:
        """Tokens without possessive "s" tokens: "arm's" is one word, so it
        cannot pass the one-word gate as if it were two."""
        return [t for i, t in enumerate(self.tokens) if not (i and t == "s")]

    def in_tag(self, tag: str) -> bool:
        """Whole-word match inside a stored (lowercase) topic tag."""
        return bool(self._re and self._re.search(normalise(tag)))

    def in_title(self, title: str) -> bool:
        """Whole-word match inside a debate title or question heading (acronyms upper case)."""
        if self._acronym_re is not None:
            return bool(self._acronym_re.search(title or ""))
        return bool(self._re and self._re.search(normalise(title)))


def phrases_for_slug(slug: str, stored_names=()) -> list[Phrase]:
    """
    Phrases a /brief/<slug> page looks for: the slug's own words, the stored
    tag spellings that gave the page ("women's pensions" keeps its apostrophe,
    which the slug lost), and, when the slug is a canonical topic in topic_canon,
    every spelling in its family (curated; the parent's page gets the children).
    """
    out: dict[str, Phrase] = {}

    def add(text: str, acronym: bool = False, curated: bool = False) -> None:
        p = Phrase(text, acronym, curated)
        if not p.text:
            return
        if p.text in out:
            curated = curated or out[p.text].curated
            acronym = acronym or out[p.text].acronym
            p = Phrase(text, acronym, curated)
        out[p.text] = p

    add(slug.replace("-", " "))
    for name in stored_names or ():
        add(name)
    canon = _tc.canonical_for_slug(slug)
    if canon:
        for name in sorted(_tc.topic_family_names(canon)):
            add(name, acronym=name in _tc.ACRONYMS, curated=True)
    return list(out.values())


def expandable(phrases: list[Phrase]) -> list[Phrase]:
    """The phrases worth looking for inside other text, with phrases already
    implied by a shorter one removed ("send provision" is covered by "send")."""
    cands = [p for p in phrases if p.expandable]
    keep: list[Phrase] = []
    for p in sorted(cands, key=lambda p: (len(p.tokens), len(p.text))):
        if not any(k.in_tag(p.text) and k.acronym == p.acronym for k in keep):
            keep.append(p)
    return keep


class TopicMatcher:
    """Everything a topic page needs to match text: phrases plus exclusions."""

    def __init__(self, slug: str, stored_names=()):
        self.phrases = expandable(phrases_for_slug(slug, stored_names))
        canon = _tc.canonical_for_slug(slug)
        self.excludes = [Phrase(x) for x in _tc.topic_exclusions(canon)] if canon else []
        self._upper_excludes = [
            re.compile(r"(?<![A-Za-z0-9])" + re.escape(w.upper()) + r"[sS]?(?![A-Za-z0-9])")
            for w in (_tc.topic_upper_case_exclusions(canon) if canon else [])
        ]

    def __bool__(self) -> bool:
        return bool(self.phrases)

    def tag_matches(self, tag: str) -> bool:
        return (any(p.in_tag(tag) for p in self.phrases)
                and not any(x.in_tag(tag) for x in self.excludes))

    def title_matches(self, title: str) -> bool:
        return (any(p.in_title(title) for p in self.phrases)
                and not any(x.in_tag(title) for x in self.excludes)
                and not any(u.search(title or "") for u in self._upper_excludes))

    def tag_like_patterns(self) -> list[str]:
        return sorted({x for p in self.phrases for x in p.tag_like_patterns()})

    def tag_pg_regex(self) -> str:
        """
        One Postgres regex pre-filter for all phrases (used instead of the
        LIKEs): a phrase starting at a word start, its words separated by
        non-alphanumerics. Never narrower than tag_matches() for ASCII tags;
        Python makes the exact check. Empty when there is nothing to match.
        """
        bodies = [p._pg_body for p in self.phrases if p._pg_body]
        return f"(^|[^a-z0-9])({'|'.join(bodies)})" if bodies else ""
