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

   Only unambiguous name forms are removed, never a lone word:
   - an MP's full name ("Cat Smith"; titles such as "Dr" are optional);
   - a peer with a title in front ("Lord Price", "Lady Stroud", "Baroness
     Stroud", "Lord Black of Brentwood") — so "Stroud" the town, "price"
     and "bird flu" are untouched;
   - "Member for <seat>" (Commons seats).
   A speech that mentions one of those forms is kept only if it still
   matches the search (same Postgres stemming) once they are removed.
   Speeches that never mention them are not rechecked.
   If the search names the member ("cat smith", "lady stroud") or the seat
   ("forest of dean"), that name or seat is not removed.

   Former members are kept in the list on purpose: their names still appear
   in debates ("as Boris Johnson said"), and only full or titled names are
   removed, which never carry a topic.

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


# Peerage titles: a one-word peer name is only removed with one of these in front.
_PEER_TITLES = ("Viscountess", "Viscount", "Countess", "Baroness", "Marquess",
                "Duchess", "Baron", "Lord", "Lady", "Earl", "Duke")
_PEER_TITLE_WORDS = frozenset(t.lower() for t in _PEER_TITLES)
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
    tsquery: str   # tsquery text: matches a speech that mentions a guarded name or seat
    pattern: str   # Postgres regex for those name forms / "Member for <seat>"

    def sql(self, tsv_col: str, text_expr: str, query_expr: str) -> str:
        """AND-clause: keep a row unless it matches only through a guarded name."""
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
    lexemes: frozenset        # stems in phrase, to match search words
    tsquery: str              # prefilter: speech mentions this name form
    regex: str                # Postgres regex for the removable form
    short: str = ""           # peer: name before " of " ("Black")


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
        guard = _lookup(words, q_clean)
    except Exception as exc:
        db.session.rollback()
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
    title_tsv = db.session.execute(
        sqla_text("SELECT CAST(to_tsvector('english', :t) AS text)"),
        {"t": " ".join(_PEER_TITLES)}).scalar()
    rows = db.session.execute(sqla_text(f"""
        WITH m AS (
            SELECT name, house, membership_from,
                   regexp_replace(name, {_TITLES_SQL}, '', 'i') AS bare,
                   (name ~* {_PEER_NAME_SQL}) AS is_peer
            FROM member_profile
        )
        SELECT CASE WHEN is_peer THEN 'peer' ELSE 'mp' END, bare,
               CAST(phraseto_tsquery('english', bare) AS text),
               CAST(phraseto_tsquery('english', split_part(bare, ' of ', 1)) AS text)
        FROM m
        UNION
        SELECT 'seat', membership_from,
               CAST(phraseto_tsquery('english', membership_from) AS text), NULL
        FROM m
        WHERE house = 'Commons' AND membership_from IS NOT NULL
    """)).fetchall()
    title_q = "( " + " | ".join(f"'{t}'" for t in sorted(_lexemes(title_tsv))) + " )"

    entries = set()
    for kind, phrase, tsq, short_tsq in rows:
        phrase = " ".join((phrase or "").split())
        if not phrase or not tsq:
            continue
        lex = frozenset(_lexemes(tsq))
        if kind == "seat":
            entries.add(_Entry("seat", phrase, lex, tsq, _phrase_regex(phrase)))
        elif kind == "mp":
            if len(phrase.split()) < 2:
                continue    # one word is not a safe name form ("Hunt", "Green")
            entries.add(_Entry("mp", phrase, lex, tsq, _phrase_regex(phrase)))
        else:
            if phrase.lower().startswith("of ") or not short_tsq:
                continue    # "The Earl of Kinnoull", "Bishop of London": titles, not names
            short, _, place = phrase.partition(" of ")
            regex = _PEER_TITLE_RE + _phrase_regex(short)
            if place:
                regex += r"(?:\s+of\s+" + _phrase_regex(place) + ")?"
            # "Lord Price", "Lady Stroud", "Baroness Stroud", "Lord Black (of Brentwood)"
            entries.add(_Entry("peer", phrase, lex, f"{title_q} <-> ( {short_tsq} )",
                               regex, short=short))
    entries = tuple(sorted(entries, key=lambda e: (e.kind, e.phrase)))
    _index.update(at=now, entries=entries)
    return entries


def _word_lexemes(words: list[str]) -> list[set]:
    """Postgres's English stems for each word ("cats" -> {"cat"}; stopwords -> empty)."""
    sql = sqla_text("SELECT " + ", ".join(
        f"CAST(plainto_tsquery('english', :w{i}) AS text)" for i in range(len(words))))
    row = db.session.execute(sql, {f"w{i}": w for i, w in enumerate(words)}).fetchone()
    return [_lexemes(t) for t in row]


def _named(entry: _Entry, query_words: set) -> bool:
    """The search names this member or seat, so it stays searchable."""
    if entry.kind == "peer":
        # "lord black", "lady stroud", "baroness stroud": the name and any title
        return (_is_named_in(query_words, entry.short)
                and bool(query_words & _PEER_TITLE_WORDS))
    return _is_named_in(query_words, entry.phrase)


def _lookup(words: list[str], q_clean: str) -> NameGuard | None:
    query_lex = set().union(*_word_lexemes(words))
    if not query_lex:
        return None
    query_words = set(_plain_words(q_clean))
    chosen = [e for e in _member_index()
              if e.lexemes & query_lex and not _named(e, query_words)][:_MAX_PHRASES]
    if not chosen:
        return None
    alternatives = []
    mps = {e.phrase for e in chosen if e.kind == "mp"}
    peers = sorted({e.regex[len(_PEER_TITLE_RE):] for e in chosen if e.kind == "peer"})
    seats = [e.regex for e in chosen if e.kind == "seat"]
    names = []
    if mps:
        names.append(_names_regex(mps))
    if peers:
        names.append(_PEER_TITLE_RE + "(?:" + "|".join(peers) + ")")
    if names:
        alternatives.append(r"\m(?:" + "|".join(names) + r")\M")
    if seats:
        alternatives.append(r"\m(?:Members?|MP)\s+for\s+(?:" + "|".join(sorted(set(seats))) + r")\M")
    return NameGuard(tsquery=" | ".join(sorted({f"( {e.tsquery} )" for e in chosen})),
                     pattern="|".join(alternatives))
