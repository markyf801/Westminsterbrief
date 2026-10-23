"""
Query helpers for the archive full-text search (/archive/search, /hansard).

Two jobs:

1. parse_query() — a query wrapped in double quotes is an exact phrase;
   anything else needs every word (stemmed: "cats" finds "cat").

2. name_guard() — stop a search word matching only because it is part of a
   member's name or seat. Hansard text is full of attributions such as
   "the hon. Member for Lancaster and Wyre (Cat Smith)" and committee lists
   ("Ordered, That ... Cat Eccles, Cat Smith ..."), so a search for "cats"
   used to return them. The guard finds members whose name (or Commons seat)
   contains a search word. A speech that mentions one of those names is kept
   only if the search word still appears once the names are removed.
   Speeches that never mention those names are not rechecked, so the extra
   work stays small (a regular expression on a few hundred speeches).
   If the search names the member ("cat smith") or the seat ("forest of
   dean"), that name or seat is not removed.

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
_CACHE_MAX = 500
_cache: dict = {}

# Replaces a removed name, so the words either side do not become a phrase.
NAME_GAP = " xname "

# Leading titles, so "Dr Danny Chambers" is matched as "Danny Chambers".
_TITLES_SQL = (r"'^((the|rt|hon|lord|lady|baroness|baron|viscount|viscountess|earl|"
               r"countess|duke|duchess|marquess|bishop|sir|dame|dr|mr|mrs|ms|miss)\.?\s+)+'")


@dataclass(frozen=True)
class NameGuard:
    tsquery: str                  # matches a speech that mentions a guarded name or seat
    pattern: str                  # Postgres regex for those names / "Member for <seat>"
    word_patterns: tuple = ()     # regex per search word that is part of a guarded name

    def sql(self, tsv_col: str, text_expr: str) -> str:
        """AND-clause: keep a row unless the search word appears only in guarded names."""
        stripped = f"regexp_replace({text_expr}, :ng_re, '{NAME_GAP}', 'g')"
        checks = " AND ".join(f"{stripped} ~* :ng_w{i}" for i in range(len(self.word_patterns)))
        return f"AND (NOT ({tsv_col} @@ CAST(:ng_q AS tsquery)) OR ({checks}))"

    def params(self) -> dict:
        p = {"ng_q": self.tsquery, "ng_re": self.pattern}
        p.update({f"ng_w{i}": w for i, w in enumerate(self.word_patterns)})
        return p


def _regex_word(word: str) -> str:
    word = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", word)
    return re.sub(r"['’]", "['’]", word)


def _phrase_regex(phrase: str) -> str:
    """Postgres ARE for a name, tolerant of extra spaces and either apostrophe."""
    return r"\s+".join(_regex_word(w) for w in phrase.split())


def _names_regex(phrases) -> str:
    """One alternation grouped by first word, e.g. Cat followed by (Eccles|Smith):
    noticeably faster than a flat list when many members share a first name."""
    groups: dict = {}
    for phrase in phrases:
        first, _, rest = phrase.partition(" ")
        groups.setdefault(_regex_word(first), set()).add(_phrase_regex(rest) if rest else "")
    alts = []
    for first in sorted(groups):
        rests = sorted(groups[first] - {""})
        if rests:
            tail = rests[0] if len(rests) == 1 else "(?:" + "|".join(rests) + ")"
            alts.append(first + r"\s+" + tail)
        if "" in groups[first]:
            alts.append(first)
    return "|".join(alts)


def _word_regex(typed: str, lexeme: str) -> str:
    """Postgres ARE (used case-insensitively) for a search word and its plurals:
    "cats" -> cat, cats, cat's. Close enough to stemming for the few speeches
    it is used on (those that mention a guarded name)."""
    forms = sorted({typed.lower(), lexeme.lower()} - {""}, key=len, reverse=True)
    return r"\m(?:" + "|".join(_regex_word(f) for f in forms) + r")(?:s|es|['’]s|['’])?\M"


def _query_words(q_clean: str) -> list[str]:
    return sorted({w.lower() for w in _WORD_RE.findall(q_clean)} - _NOT_NAME_WORDS)


def _plain_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _is_named_in(query_words: set, phrase: str, keep_titles: bool = False) -> bool:
    skip = {"the", "of", "and"} if keep_titles else _NOT_NAME_WORDS
    words = [w for w in _plain_words(phrase) if w not in skip]
    return bool(words) and set(words) <= query_words


def _lexemes(tsquery_text: str) -> set[str]:
    return {m.replace("''", "'") for m in _LEXEME_RE.findall(tsquery_text or "")}


def name_guard(q_clean: str) -> NameGuard | None:
    """Guard for the member names / seats that contain a word of the query, or None."""
    words = _query_words(q_clean)
    if not words:
        return None
    key = (tuple(words), q_clean.lower())
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    try:
        guard = _lookup(words, q_clean)
    except Exception as exc:
        db.session.rollback()
        print(f"[search_terms] name guard skipped: {exc}", flush=True)
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (time.monotonic(), guard)
    return guard


_index: dict = {"at": None, "entries": ()}


def _member_index() -> tuple:
    """(kind, member name, phrase, phrase tsquery text, lexemes) for every member
    name (titles removed) and Commons seat in member_profile. Reloaded every 6 hours."""
    now = time.monotonic()
    if _index["at"] is not None and now - _index["at"] < _CACHE_TTL_SECONDS:
        return _index["entries"]
    rows = db.session.execute(sqla_text(f"""
        WITH m AS (
            SELECT name, house, membership_from,
                   regexp_replace(name, {_TITLES_SQL}, '', 'i') AS bare
            FROM member_profile
        )
        SELECT 'name', name, bare, CAST(phraseto_tsquery('english', bare) AS text) FROM m
        UNION
        SELECT 'seat', name, membership_from,
               CAST(phraseto_tsquery('english', membership_from) AS text)
        FROM m
        WHERE house = 'Commons' AND membership_from IS NOT NULL
    """)).fetchall()
    entries = tuple(sorted(
        (kind, member_name or "", " ".join(phrase.split()), tsq, frozenset(_lexemes(tsq)))
        for kind, member_name, phrase, tsq in rows if phrase and phrase.strip() and tsq))
    _index.update(at=now, entries=entries)
    return entries


def _word_lexemes(words: list[str]) -> list[set]:
    """Postgres's English stems for each word ("cats" -> {"cat"}; stopwords -> empty)."""
    sql = sqla_text("SELECT " + ", ".join(
        f"CAST(plainto_tsquery('english', :w{i}) AS text)" for i in range(len(words))))
    row = db.session.execute(sql, {f"w{i}": w for i, w in enumerate(words)}).fetchone()
    return [_lexemes(t) for t in row]


def _lookup(words: list[str], q_clean: str) -> NameGuard | None:
    word_lex = _word_lexemes(words)
    query_lex = set().union(*word_lex)
    if not query_lex:
        return None
    candidates = [e for e in _member_index() if e[4] & query_lex][:_MAX_PHRASES]

    query_words = set(_plain_words(q_clean))
    names, seats, tsqueries = set(), set(), set()
    for kind, member_name, phrase, tsq, _lex in candidates:
        if kind == "name":
            if phrase.lower().startswith("of "):
                continue    # "The Earl of Kinnoull": a title, not a name
            # The search names this member ("cat smith"; "lord black" for
            # Lord Black of Brentwood), so keep their name searchable.
            short = re.split(r"\s+of\s+", phrase)[0]
            if len(_plain_words(short)) >= 2:
                named = _is_named_in(query_words, short)
            else:   # a peer known by one name: needs the title too
                named = _is_named_in(query_words, re.split(r"\s+of\s+", member_name)[0],
                                     keep_titles=True)
            if named:
                continue
            names.add(phrase)
        else:
            if _is_named_in(query_words, phrase):
                continue    # the search names the seat ("forest of dean")
            seats.add(_phrase_regex(phrase))
        tsqueries.add(tsq)
    if not tsqueries:
        return None

    # Only the search words that are part of a guarded name need rechecking.
    in_names = set().union(*(_lexemes(t) for t in tsqueries))
    word_patterns = [_word_regex(word, lexeme)
                     for word, lexemes in zip(words, word_lex)
                     for lexeme in sorted(lexemes) if lexeme in in_names]
    if not word_patterns:
        return None

    alternatives = []
    if names:
        alternatives.append(r"\m(?:" + _names_regex(names) + r")\M")
    if seats:
        alternatives.append(r"\m(?:Members?|MP)\s+for\s+(?:" + "|".join(sorted(seats)) + r")\M")
    return NameGuard(tsquery=" | ".join(f"( {t} )" for t in sorted(tsqueries)),
                     pattern="|".join(alternatives),
                     word_patterns=tuple(word_patterns))
