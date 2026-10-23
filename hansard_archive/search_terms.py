"""
Query helpers for the archive full-text search (/archive/search, /hansard).

Two jobs:

1. parse_query() — a query wrapped in double quotes is an exact phrase;
   anything else needs every word (stemmed: "cats" finds "cat").

2. name_guard() — stop a search word matching only because it is part of a
   member's name or seat. Hansard text is full of attributions such as
   "the hon. Member for Lancaster and Wyre (Cat Smith)" and committee lists
   ("Ordered, That ... Cat Eccles, Cat Smith ..."), so a search for "cats"
   used to return them.

   A name is removed only when the search word is a part of it that is not
   how people search for that person:
   - an MP's first or middle name ("cat" in "Cat Smith");
   - a Commons seat, in the form "Member for <seat>" ("hull" in "Member for
     Kingston upon Hull East");
   - a peer's territorial "of Place" ("brentwood" in "Lord Black of
     Brentwood").
   It is never removed when the search word is the surname or the peer's
   name (Badenoch, Starmer, Hunt, Green, Frost, Price, Stroud): searching a
   surname is the natural way to find mentions of a person, so "green" still
   matches "Sarah Green", as before. Naming the member in full ("cat smith")
   or the seat ("forest of dean") also keeps it.
   A speech that mentions a removable form is kept only if it still matches
   the search (same Postgres stemming) once those forms are removed.
   Speeches that never mention them are not rechecked.

   Former members are kept in the list on purpose: their names still appear
   in debates, and a removed form never carries the topic being searched.

Postgres only: callers use it inside the existing Postgres branches.
"""

import re
import time
from dataclasses import dataclass

from sqlalchemy import text as sqla_text

from extensions import db


def parse_query(q_raw: str) -> tuple[str, str]:
    """(tsquery function, query text). "Quoted" means an exact phrase."""
    if q_raw.startswith('"') and q_raw.endswith('"') and len(q_raw) > 2:
        return "phraseto_tsquery", q_raw[1:-1]
    return "plainto_tsquery", q_raw


# Peerage titles, in front of a peer's name in the removable form.
_PEER_TITLES = ("Viscountess", "Viscount", "Countess", "Baroness", "Marquess",
                "Duchess", "Baron", "Lord", "Lady", "Earl", "Duke")
_PEER_TITLE_RE = "(?:" + "|".join(_PEER_TITLES) + r")\s+"

# Titles are not names: "lord" must not pull in every peer.
_NOT_NAME_WORDS = frozenset({
    "lord", "lady", "baroness", "baron", "viscount", "viscountess", "earl",
    "countess", "duke", "duchess", "marquess", "bishop", "sir", "dame",
    "dr", "mr", "mrs", "ms", "miss", "rt", "hon", "the", "of", "and",
    "lords", "members", "member", "mp", "mps",
})
_WORD_RE = re.compile(r"[^\W\d_][\w'’-]*")
_LEXEME_RE = re.compile(r"'((?:[^']|'')+)'")
_MAX_PHRASES = 80
_CACHE_TTL_SECONDS = 6 * 3600
_FAILED_TTL_SECONDS = 300
_CACHE_MAX = 500
_cache: dict = {}
_failed: dict = {"until": 0.0}

# Replaces a removed name, so the words either side do not become a phrase.
NAME_GAP = " xname "

# Leading titles, so "Dr Danny Chambers" is matched as "Danny Chambers".
_TITLES_SQL = (r"'^((the|rt|hon|lord|lady|baroness|baron|viscount|viscountess|earl|"
               r"countess|duke|duchess|marquess|bishop|sir|dame|dr|mr|mrs|ms|miss)\.?\s+)+'")
# A peer's display name starts with a title: "Lord Price", "The Earl of Kinnoull".
_PEER_NAME_SQL = (r"'^(the\s+)?(lord|lady|baroness|baron|viscount|viscountess|earl|"
                  r"countess|duke|duchess|marquess)\s'")


@dataclass(frozen=True)
class NameGuard:
    tsquery: str   # tsquery text: matches a speech that mentions a removable form
    pattern: str   # Postgres regex for those forms

    def sql(self, tsv_col: str, text_expr: str, query_expr: str) -> str:
        """AND-clause: keep a row unless it matches only through a removable form."""
        return (
            f"AND (NOT ({tsv_col} @@ CAST(:ng_q AS tsquery)) "
            f"OR to_tsvector('english', regexp_replace({text_expr}, :ng_re, "
            f"'{NAME_GAP}', 'g')) @@ {query_expr})"
        )

    def params(self) -> dict:
        return {"ng_q": self.tsquery, "ng_re": self.pattern}


@dataclass(frozen=True)
class _Entry:
    kind: str                 # "mp", "peer" or "seat"
    phrase: str               # MP: full name; peer: name without title; seat
    trigger: frozenset        # stems that make this form removable (first names, place, seat)
    keep: frozenset           # stems that keep it: surname / peer's name
    tsquery: str              # prefilter: speech mentions this form
    regex: str                # Postgres regex for the removable form


def _regex_word(word: str) -> str:
    word = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", word)
    return re.sub(r"['’]", "['’]", word)


def _phrase_regex(phrase: str) -> str:
    """Postgres ARE for a name, tolerant of extra spaces and either apostrophe."""
    return r"\s+".join(_regex_word(w) for w in phrase.split())


def _names_regex(phrases) -> str:
    """MP names as one alternation grouped by first word, e.g. Cat then (Eccles|Smith):
    noticeably faster than a flat list when many members share a first name."""
    groups: dict = {}
    for phrase in phrases:
        first, _, rest = phrase.partition(" ")
        groups.setdefault(_regex_word(first), set()).add(_phrase_regex(rest))
    alts = []
    for first in sorted(groups):
        rests = sorted(groups[first])
        tail = rests[0] if len(rests) == 1 else "(?:" + "|".join(rests) + ")"
        alts.append(first + r"\s+" + tail)
    return "|".join(alts)


def _query_words(q_clean: str) -> list[str]:
    return sorted({w.lower() for w in _WORD_RE.findall(q_clean)} - _NOT_NAME_WORDS)


def _plain_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _is_named_in(query_words: set, phrase: str) -> bool:
    words = [w for w in _plain_words(phrase) if w not in _NOT_NAME_WORDS]
    return bool(words) and set(words) <= query_words


def _lexemes(tsquery_text: str) -> set[str]:
    """Stems in a tsquery or tsvector printed as text ("'cat' <-> 'smith'", "'cat':1")."""
    return {m.replace("''", "'") for m in _LEXEME_RE.findall(tsquery_text or "")}


def name_guard(q_clean: str) -> NameGuard | None:
    """Guard for the member names / seats that contain a word of the query, or None."""
    words = _query_words(q_clean)
    if not words:
        return None
    now = time.monotonic()
    key = (tuple(words), q_clean.lower())
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    if now < _failed["until"]:
        return None     # the member list failed to load recently: don't retry every search
    try:
        # A savepoint: a failure here undoes only this lookup, so the search's
        # transaction (and its SET LOCAL statement_timeout) carries on.
        with db.session.begin_nested():
            guard = _lookup(words, q_clean)
    except Exception as exc:
        _failed["until"] = now + _FAILED_TTL_SECONDS
        print(f"[search_terms] name guard skipped for 5 minutes: {exc}", flush=True)
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (now, guard)
    return guard


_index: dict = {"at": None, "entries": ()}


def _member_index() -> tuple:
    """Every removable name form in member_profile, as _Entry. Reloaded every 6 hours."""
    now = time.monotonic()
    if _index["at"] is not None and now - _index["at"] < _CACHE_TTL_SECONDS:
        return _index["entries"]
    title_tsv, has_names = db.session.execute(sqla_text(
        "SELECT CAST(to_tsvector('english', :t) AS text),"
        " to_regclass('member_name') IS NOT NULL"), {"t": " ".join(_PEER_TITLES)}).one()
    # member_name.family_name tells "Saville Roberts" apart from first names
    family = "mn.family_name" if has_names else "CAST(NULL AS text)"
    join = "LEFT JOIN member_name mn ON mn.member_id = p.member_id" if has_names else ""
    rows = db.session.execute(sqla_text(f"""
        WITH m AS (
            SELECT p.house, p.membership_from,
                   regexp_replace(p.name, {_TITLES_SQL}, '', 'i') AS bare,
                   (p.name ~* {_PEER_NAME_SQL}) AS is_peer,
                   {family} AS family
            FROM member_profile p
            {join}
        ),
        n AS (
            SELECT *,
                   CASE WHEN is_peer THEN split_part(bare, ' of ', 1)
                        WHEN family <> '' AND lower(bare) LIKE '% ' || lower(family) THEN family
                        ELSE regexp_replace(bare, '^.*\\s', '') END AS surname
            FROM m
        )
        SELECT CASE WHEN is_peer THEN 'peer' ELSE 'mp' END, bare,
               CAST(phraseto_tsquery('english', bare) AS text),
               CAST(to_tsvector('english', CASE
                   WHEN is_peer THEN split_part(bare, ' of ', 2)
                   ELSE left(bare, length(bare) - length(surname)) END) AS text),
               CAST(to_tsvector('english', surname) AS text),
               surname
        FROM n
        UNION
        SELECT 'seat', membership_from,
               CAST(phraseto_tsquery('english', membership_from) AS text),
               CAST(to_tsvector('english', membership_from) AS text), '', ''
        FROM m
        WHERE house = 'Commons' AND membership_from IS NOT NULL
    """)).fetchall()
    title_q = "( " + " | ".join(f"'{t}'" for t in sorted(_lexemes(title_tsv))) + " )"

    entries = set()
    for kind, phrase, tsq, trigger_tsv, keep_tsv, surname in rows:
        phrase = " ".join((phrase or "").split())
        trigger = frozenset(_lexemes(trigger_tsv))
        if not phrase or not tsq or not trigger:
            continue    # nothing but a surname / peer's name: never removed
        keep = frozenset(_lexemes(keep_tsv))
        if kind == "seat":
            entries.add(_Entry("seat", phrase, trigger, keep, tsq, _phrase_regex(phrase)))
        elif kind == "mp":
            entries.add(_Entry("mp", phrase, trigger, keep, tsq, _phrase_regex(phrase)))
        else:
            short, _, place = phrase.partition(" of ")
            if not short or not place:
                continue    # "The Earl of Kinnoull", "Bishop of London": titles
            # "Lord Black of Brentwood" (only the full form carries the place)
            regex = _PEER_TITLE_RE + _phrase_regex(short) + r"\s+of\s+" + _phrase_regex(place)
            entries.add(_Entry("peer", phrase, trigger, keep,
                               f"{title_q} <-> ( {tsq} )", regex))
    entries = tuple(sorted(entries, key=lambda e: (e.kind, e.phrase)))
    _index.update(at=now, entries=entries)
    return entries


def _word_lexemes(words: list[str]) -> list[set]:
    """Postgres's English stems for each word ("cats" -> {"cat"}; stopwords -> empty)."""
    sql = sqla_text("SELECT " + ", ".join(
        f"CAST(plainto_tsquery('english', :w{i}) AS text)" for i in range(len(words))))
    row = db.session.execute(sql, {f"w{i}": w for i, w in enumerate(words)}).fetchone()
    return [_lexemes(t) for t in row]


def _removable(entry: _Entry, query_lex: set, query_words: set) -> bool:
    """A search word is a first name / place / seat of this form, and the search
    neither uses the surname or peer's name nor names the seat in full."""
    if not entry.trigger & query_lex or entry.keep & query_lex:
        return False
    return not (entry.kind == "seat" and _is_named_in(query_words, entry.phrase))


def _lookup(words: list[str], q_clean: str) -> NameGuard | None:
    query_lex = set().union(*_word_lexemes(words))
    if not query_lex:
        return None
    query_words = set(_plain_words(q_clean))
    chosen = [e for e in _member_index()
              if _removable(e, query_lex, query_words)][:_MAX_PHRASES]
    if not chosen:
        return None
    mps = {e.phrase for e in chosen if e.kind == "mp"}
    names = ([_names_regex(mps)] if mps else []) + sorted(
        {e.regex for e in chosen if e.kind == "peer"})
    seats = sorted({e.regex for e in chosen if e.kind == "seat"})
    alternatives = []
    if names:
        alternatives.append(r"\m(?:" + "|".join(names) + r")\M")
    if seats:
        alternatives.append(r"\m(?:Members?|MP)\s+for\s+(?:" + "|".join(seats) + r")\M")
    return NameGuard(tsquery=" | ".join(sorted({f"( {e.tsquery} )" for e in chosen})),
                     pattern="|".join(alternatives))
