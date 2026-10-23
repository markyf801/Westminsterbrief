"""
hansard_archive/topic_canon.py

Canonical hierarchy for free-text "specific" topic tags.

The tagger emits topics in many spellings ("send", "send provision", "special
educational needs and disabilities"), and each spelling used to become its own
thin /brief/<slug> page. This module is the one place that says which spellings
are the same topic, and which topics sit under a broader one.

    TOPIC_FAMILIES = {
        "<parent>": {
            "aliases":  [...],                 # same topic, other spelling -> parent
            "children": {"<child>": [aliases of the child], ...},
            "exclude":  [...],                 # optional: text naming something else
        },
    }

Write time (tagger): canonical_topic("send provision") -> "special educational needs".
Read time (/brief pages, related panels): topic_family_names("special educational
needs") -> the parent, its children and every alias, so pages also find rows
stored before canonicalisation existed (e.g. "send", "send funding").

Topic pages also look for a family's spellings inside other tags, debate titles
and question headings (hansard_archive/topic_match.py). A one-word topic is only
looked for that way when it has a family here, because one word often has
another sense ("arms" in "arm's-length bodies", "turkey" the bird): adding a
family is the decision that the word is safe to search for. "exclude" lists
text that contains the word but means something else ("CAT scans" on the cats
page); "upper_case_exclude" words in upper case in a title or heading are an
acronym for something else ("CAT"). ACRONYMS are the reverse: they must be upper
case in a title or heading ("SEND", not "send").

Data-driven and extendable: add a family or an alias here, no other code changes.
Keys and aliases are lowercase, whitespace-collapsed (as stored). Run
scripts/propose_topic_merges.py against the database for merge suggestions.
"""

from __future__ import annotations

TOPIC_FAMILIES: dict[str, dict] = {
    "special educational needs": {
        "aliases": [
            "send", "sen", "sen/d", "send provision", "send support", "send services",
            "send system", "send reform", "send reforms", "sen provision", "sen support",
            "special educational needs and disabilities",
            "special educational needs and disability",
            "special educational needs provision", "special educational needs support",
            "special educational needs reform",
        ],
        "children": {
            "special educational needs funding": [
                "send funding", "sen funding", "high needs funding",
                "special educational needs and disabilities funding",
            ],
            "education health and care plans": [
                "ehcp", "ehcps", "ehc plans", "ehcp delays", "ehcp assessments",
                "education, health and care plans", "education health and care plan",
            ],
            "special educational needs transport": [
                "send transport", "send home to school transport",
                "home to school transport for send pupils",
            ],
            "special educational needs coordinators": ["senco", "sencos", "send coordinators"],
            "special schools": ["special school places", "send school places"],
        },
    },
    "chagos islands": {
        "aliases": [
            "chagos", "chagos archipelago", "british indian ocean territory", "biot",
        ],
        "children": {
            "chagos islands sovereignty": [
                "chagos sovereignty", "chagos islands sovereignty transfer",
                "chagos sovereignty transfer", "chagos deal", "chagos agreement",
                "chagos islands agreement", "chagos islands deal", "chagos treaty",
                "chagos islands treaty", "uk-mauritius chagos treaty",
                "uk-mauritius treaty", "chagos islands handover",
                "sovereignty of the chagos islands",
            ],
            "diego garcia": [
                "diego garcia military base", "diego garcia base", "diego garcia lease",
            ],
            "chagossian community": ["chagossians", "chagossian people", "chagossian rights"],
        },
    },
    "two-child limit": {
        "aliases": [
            "two child limit", "two-child benefit cap", "two child benefit cap",
            "two-child benefit limit", "two child benefit limit", "two-child cap",
            "two child cap", "two-child policy",
        ],
        "children": {},
    },
    "winter fuel payment": {
        "aliases": ["winter fuel payments", "winter fuel allowance"],
        "children": {},
    },
    "assisted dying": {
        "aliases": ["assisted suicide", "assisted dying legislation"],
        "children": {
            "terminally ill adults (end of life) bill": [
                "terminally ill adults bill", "assisted dying bill",
            ],
        },
    },
    "pensions triple lock": {
        "aliases": ["triple lock", "state pension triple lock", "the triple lock"],
        "children": {},
    },
    # One-word topics safe to look for inside titles, headings and other tags.
    "ukraine": {
        "aliases": [],
        "children": {},
    },
    "cats": {
        "aliases": ["cat"],
        "children": {},
        "exclude": [
            "cat scan", "cat scans", "cat scanner", "cat scanners",
            "centres for advanced training", "music and dance scheme",
            "cat and mouse", "fat cat", "cat s eyes", "cats eyes", "cat smith",
            "out of the bag", "competition appeal tribunal",
        ],
        # Upper case in a title or heading is an acronym, not the animal:
        # CAT scans, the Competition Appeal Tribunal (CAT), CATs dance scheme.
        "upper_case_exclude": ["cat"],
    },
}

# Aliases that are acronyms: on topic pages they must be upper case in a debate
# title or question heading (tags are stored lowercase, so tags match as is).
ACRONYMS: frozenset[str] = frozenset({"send", "sen", "ehcp", "ehcps", "senco", "sencos", "biot"})


def _norm(t: str) -> str:
    return " ".join((t or "").lower().split())


def _build_indexes() -> tuple[dict[str, str], dict[str, str], dict[str, frozenset[str]]]:
    alias_to_canon: dict[str, str] = {}    # any spelling -> canonical (parent or child)
    parent_of: dict[str, str] = {}         # child canonical -> parent canonical
    family: dict[str, frozenset[str]] = {}  # canonical -> stored names to query
    for parent, spec in TOPIC_FAMILIES.items():
        parent = _norm(parent)
        parent_names = {parent} | {_norm(a) for a in spec.get("aliases", [])}
        child_all: set[str] = set()
        for child, child_aliases in (spec.get("children") or {}).items():
            child = _norm(child)
            names = {child} | {_norm(a) for a in child_aliases}
            for n in names:
                alias_to_canon[n] = child
            parent_of[child] = parent
            family[child] = frozenset(names)
            child_all |= names
        for n in parent_names:
            alias_to_canon[n] = parent
        family[parent] = frozenset(parent_names | child_all)
    return alias_to_canon, parent_of, family


_ALIAS_TO_CANON, _PARENT_OF, _FAMILY = _build_indexes()


def canonical_topic(theme: str) -> str:
    """Canonical spelling for a stored/emitted topic (lowercase); unknown topics pass through."""
    t = _norm(theme)
    return _ALIAS_TO_CANON.get(t, t)


def parent_topic(theme: str) -> str | None:
    """The broader canonical topic this one sits under, or None."""
    return _PARENT_OF.get(canonical_topic(theme))


def topic_family_names(theme: str) -> set[str]:
    """
    Every stored spelling that belongs on this topic's page: for a parent, the
    parent, its aliases, its children and their aliases; for a child, the child
    and its aliases; for an unknown topic, just itself.
    """
    canon = canonical_topic(theme)
    return set(_FAMILY.get(canon, {canon}))


def expand_topics(themes) -> list[str]:
    """Stored-name expansion for matching a list of topics (canonical + aliases), sorted."""
    out: set[str] = set()
    for t in themes or []:
        if t:
            out |= topic_family_names(t)
    return sorted(out)


def _family_spec(theme: str) -> dict:
    canon = canonical_topic(theme)
    root = _PARENT_OF.get(canon, canon)
    for parent, spec in TOPIC_FAMILIES.items():
        if _norm(parent) == root:
            return spec
    return {}


def topic_exclusions(theme: str) -> list[str]:
    """The "exclude" texts of the family this topic belongs to (empty if none)."""
    return [_norm(x) for x in _family_spec(theme).get("exclude", [])]


def topic_upper_case_exclusions(theme: str) -> list[str]:
    """Words of this topic's family that, written in upper case in a title or
    heading, are an acronym for something else ("CAT")."""
    return [_norm(x) for x in _family_spec(theme).get("upper_case_exclude", [])]


def known_canonical_topics() -> list[str]:
    """All parents and children defined here (canonical spellings)."""
    return sorted(_FAMILY)


# ---------------------------------------------------------------------------
# Slug helpers for /brief/<slug>
# ---------------------------------------------------------------------------

def _build_slug_indexes() -> tuple[dict[str, str], dict[str, str]]:
    from hansard_archive.slugs import slugify_theme

    canon_by_slug = {slugify_theme(c): c for c in _FAMILY}
    redirect: dict[str, str] = {}
    ambiguous: set[str] = set()
    for alias, canon in _ALIAS_TO_CANON.items():
        s = slugify_theme(alias)
        target = slugify_theme(canon)
        if not s or s == target or s in canon_by_slug:
            continue
        if redirect.get(s, target) != target:
            ambiguous.add(s)
        redirect[s] = target
    for s in ambiguous:
        redirect.pop(s, None)
    return canon_by_slug, redirect


_CANON_BY_SLUG, _ALIAS_SLUG_REDIRECT = _build_slug_indexes()


def canonical_for_slug(slug: str) -> str | None:
    """The canonical topic whose /brief slug is `slug`, or None."""
    return _CANON_BY_SLUG.get(slug)


def alias_slug_target(slug: str) -> str | None:
    """
    /brief slug to 301 to when `slug` is the slug of an alias spelling
    ("send" -> "special-educational-needs"), else None.
    """
    return _ALIAS_SLUG_REDIRECT.get(slug)


def children_of(theme: str) -> list[str]:
    """Canonical child topics of a parent topic (empty for a child or unknown topic)."""
    canon = canonical_topic(theme)
    return sorted(c for c, p in _PARENT_OF.items() if p == canon)


def related_topic_names(themes) -> list[str]:
    """
    Stored names to match for "related" panels: each topic's whole family from
    its root (a child topic is related to its parent and siblings), plus the
    topics exactly as given. Sorted.
    """
    out: set[str] = set()
    for t in themes or []:
        if not t:
            continue
        out.add(t)
        root = parent_topic(t) or canonical_topic(t)
        out |= topic_family_names(root)
    return sorted(out)
