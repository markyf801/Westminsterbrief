"""
Hansard Archive — finding debates we store twice, or that swallowed others.

Why (Oct 2026): the hourly archive cron stores debates while Hansard's
provisional ("rolling") copy is still being built, and two things go wrong:
  1. Swallowed sessions. The last open heading temporarily holds everything
     published after it; Hansard later splits those speeches into their own
     sessions (new ids), which we store too, but the heading keeps the extra
     speeches. 10 Jun 2026 "Speaker's Statement" held all of PMQs (63 rows,
     Hansard 1). The refresh holds these as "shrank"/"empty" because the drop
     is over its safety threshold, so they are never fixed by themselves.
  2. Dead-id duplicates. Hansard replaces provisional ids (numeric ones like
     26101550000005, or lowercase GUIDs) with final uppercase GUIDs; the old
     id then answers HTTP 400 {"Message":"An error has occurred."} (sometimes
     a 200 with that body). We keep both copies, so a debate shows 2-5 times
     (15 Oct 2026 Westminster Hall "Human Rights and the Regulation of AI": 5).

Used by scripts/find_duplicate_debates.py (read-only report and
recommendations), scripts/delete_duplicate_debates.py (re-checks each pair
just before deleting), scripts/refresh_partial_debates.py --accept-shrink
(re-checks a swallower just before shrinking it) and ingestion.ingest_date
(the cron logs and counts a session it has just stored or refreshed that is a
copy of, or sits inside, another stored session; it never deletes anything).

The rules that keep this safe:
  - Only a copy whose Hansard id is confirmed DEAD is ever deleted (a 400 or
    404; a 200 without a debate only if a second look-up agrees), and only
    when a LIVE twin with the same title and location holds its speeches and
    every one of its speeches is held by the twin or another stored session
    that day. Live or unchecked copies are left for review.
  - A twin is never a session holding other debates (a parent heading or a
    swallower), nor one Hansard now has fewer speeches for.
  - A swallower is shrunk only when every speech the refresh would remove is
    held by another stored session that day and what is left matches
    Hansard's current copy.

How two copies are compared (named contributions only: the ones the debate
page shows; containers are ignored, they hold their children by design):
  exact   the same speaker (member id, else the attribution as stored) and
          the same text with whitespace collapsed, contribution by
          contribution (a hash of the text).
  fuzzy   provisional and final copies differ by small edits, so the two
          sessions are aligned in order (difflib on the first 60 characters
          of each text) and an aligned pair matches when it is the same
          speaker (member ids equal; when one is missing, the names) and the
          texts are identical or one is a cut-off start of the other; between
          two sessions with the same title, also when at least 80% of the
          words line up.
A session's "coverage" in another is the share of its named contributions
matched there.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Optional

import requests

from extensions import db
from hansard_archive.models import HansardContribution, HansardSession
from hansard_archive.refresh import _norm, plan_ops, speaker_key

HEAD_CHARS = 60
INDEX_MIN_CHARS = 40       # shorter texts ("Hear, hear!") are too common to link two sessions
PREFIX_MIN_CHARS = 40      # a cut-off speech must keep at least this much to count as the same
FUZZY_MIN_RATIO = 0.8
COVERAGE_MIN = 0.9         # share of a copy's speeches its kept twin must hold
SMALL_MAX_CHARS = 300      # one named contribution shorter than this: never acted on automatically
API_PACE = 1.0             # seconds between Hansard look-ups

LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"
REASON_EMPTY_200 = "200 without a debate"

_FINAL_ID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
# Structural headings: a copy under one of these is the less useful title to keep.
_HEADING_HRS_TAGS = {"hs_6bbigboldhdg", "hs_3mainhdg", "hs_6bdepartment", "hs_3oralanswers",
                     "hs_venue"}


def is_final_id(ext_id: Optional[str]) -> bool:
    """Hansard's final ids are uppercase GUIDs; provisional ones are numeric or lowercase."""
    return bool(_FINAL_ID.fullmatch(ext_id or ""))


def title_key(title: Optional[str]) -> str:
    t = (title or "").lower().replace("’", "'").replace("‘", "'")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _name_key(name: Optional[str]) -> str:
    """'The Minister for X (Baroness Y) (Lab)' -> 'baroness y'; 'A B (Seat) (Lab)' -> 'a b'."""
    n = (name or "").lower().strip()
    m = re.match(r"^the\b[^()]*\(([^()]+)\)", n)
    if m:
        n = m.group(1)
    n = re.sub(r"\([^()]*\)", " ", n)
    return " ".join(n.split())


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class Contrib:
    id: int
    member_id: Optional[int]
    member_name: str
    text: str                       # whitespace collapsed

    @property
    def sig(self) -> tuple:
        return (speaker_key(self.member_id, self.member_name), _hash(self.text))

    @property
    def head(self) -> str:
        return self.text[:HEAD_CHARS].lower()


@dataclass
class Sess:
    id: int
    ext_id: str
    title: str
    date: date
    house: str
    hrs_tag: Optional[str] = None
    slug: Optional[str] = None
    debate_type: Optional[str] = None
    location: Optional[str] = None
    contribs: list = field(default_factory=list)   # named Contribs, in page order

    @property
    def named(self) -> int:
        return len(self.contribs)

    @property
    def chars(self) -> int:
        return sum(len(c.text) for c in self.contribs)

    @property
    def signature(self) -> tuple:
        return tuple(c.sig for c in self.contribs)

    @property
    def small(self) -> bool:
        return self.named < 2 and self.chars < SMALL_MAX_CHARS

    @property
    def is_heading(self) -> bool:
        return (self.hrs_tag or "").lower() in _HEADING_HRS_TAGS

    @property
    def place_key(self) -> tuple:
        """Same debate: same title and location."""
        return (title_key(self.title), (self.location or "").strip().lower())

    @property
    def group_key(self) -> tuple:
        """Exact duplicates are only grouped when title, location and debate type agree."""
        return self.place_key + ((self.debate_type or ""),)


def contribs_from_fresh(fresh: list[dict]) -> list[Contrib]:
    """Named contributions from Hansard's copy (ingestion._flatten_items output)."""
    return [Contrib(0, c["member_id"], c["member_name"], _norm(c["speech_text"]))
            for c in fresh if c.get("member_name")]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _sessions_with_contribs(sessions: list) -> dict[int, Sess]:
    out = {s.id: Sess(s.id, s.ext_id, s.title or "", s.date, s.house, s.hrs_tag, s.slug,
                      s.debate_type, s.location) for s in sessions}
    if not out:
        return out
    rows = (db.session.query(HansardContribution.id, HansardContribution.session_id,
                             HansardContribution.member_id, HansardContribution.member_name,
                             HansardContribution.speech_text)
            .filter(HansardContribution.session_id.in_(list(out)),
                    HansardContribution.member_name.isnot(None))
            .order_by(HansardContribution.session_id, HansardContribution.speech_order,
                      HansardContribution.id)
            .all())
    for cid, sid, mid, name, text in rows:
        if name:
            out[sid].contribs.append(Contrib(cid, int(mid) if mid else None, name, _norm(text)))
    return out


def load_day(day: date, house: str) -> list[Sess]:
    """Every non-container session stored for one sitting day and House, lowest id first."""
    sessions = (HansardSession.query
                .filter(HansardSession.date == day, HansardSession.house == house,
                        HansardSession.is_container.is_(False))
                .order_by(HansardSession.id).all())
    return list(_sessions_with_contribs(sessions).values())


def load_sessions(ids: Iterable[int]) -> dict[int, Sess]:
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    return _sessions_with_contribs(HansardSession.query.filter(HansardSession.id.in_(ids)).all())


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def same_speaker(a: Contrib, b: Contrib) -> bool:
    if a.member_id and b.member_id:
        return a.member_id == b.member_id
    ka, kb = _name_key(a.member_name), _name_key(b.member_name)
    return bool(ka) and ka == kb


def texts_match(a: Contrib, b: Contrib, fuzzy: bool = False) -> bool:
    """Same speaker and the same words: identical, or a cut-off start of the other;
    with fuzzy=True (two sessions with the same title) also 80% of the words."""
    if not same_speaker(a, b):
        return False
    if a.text == b.text:
        return True
    short, long_ = sorted((a.text, b.text), key=len)
    if len(short) >= PREFIX_MIN_CHARS and long_.startswith(short):
        return True
    if not fuzzy:
        return False
    sm = difflib.SequenceMatcher(None, a.text.split(), b.text.split(), autojunk=False)
    return (sm.real_quick_ratio() >= FUZZY_MIN_RATIO and sm.quick_ratio() >= FUZZY_MIN_RATIO
            and sm.ratio() >= FUZZY_MIN_RATIO)


def held_by(c: Contrib, other: Contrib, fuzzy: bool) -> bool:
    """c's words are in `other`: a match, or (long enough) inside its text."""
    if texts_match(c, other, fuzzy):
        return True
    return len(c.text) >= INDEX_MIN_CHARS and c.text in other.text and same_speaker(c, other)


def align(a: list[Contrib], b: list[Contrib], fuzzy: bool = False) -> dict[int, int]:
    """{index in a: index in b} for the contributions of a matched, in order, in b."""
    sm = difflib.SequenceMatcher(None, [c.head for c in a], [c.head for c in b], autojunk=False)
    out: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("equal", "replace"):
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            if texts_match(a[i1 + k], b[j1 + k], fuzzy):
                out[i1 + k] = j1 + k
    return out


def same_title(a: Sess, b: Sess) -> bool:
    return title_key(a.title) == title_key(b.title)


def align_sessions(a: Sess, b: Sess) -> dict[int, int]:
    return align(a.contribs, b.contribs, fuzzy=same_title(a, b))


def coverage(a: Sess, b: Sess) -> float:
    """Share of a's named contributions matched, in order, in b."""
    if not a.named:
        return 0.0
    return len(align_sessions(a, b)) / a.named


def twin_shape_ok(a: Sess, b: Sess, m: Optional[dict] = None) -> tuple[bool, str]:
    """
    Can b stand in for a (a deleted, b kept)? b must not be much bigger than a,
    unless every extra speech of b comes after the last one they share (the
    debate went on after a's copy was taken). A twin with extra speeches
    before or between shared ones holds other content (a swallower).
    """
    m = align_sessions(a, b) if m is None else m
    if not m:
        return False, "the twin shares no speeches with it"
    rev = len(m) / b.named if b.named else 0.0
    if rev >= COVERAGE_MIN:
        return True, ""
    last = max(m.values())
    extras_before = [j for j in range(last) if j not in set(m.values())]
    if extras_before:
        return False, (f"the twin has {b.named - len(m)} speeches it doesn't share, "
                       f"{len(extras_before)} of them before or among the shared ones "
                       f"(a heading holding other debates?)")
    return True, ""


def unheld_speeches(a: Sess, twin: Sess, others: Iterable[Sess],
                    m: Optional[dict] = None) -> list[Contrib]:
    """a's speeches not in the twin (by alignment) nor anywhere in `others`."""
    m = align_sessions(a, twin) if m is None else m
    out = []
    pool = [(o, same_title(a, o)) for o in others if o.id != a.id]
    for k, c in enumerate(a.contribs):
        if k in m:
            continue
        if any(held_by(c, x, fz) for o, fz in pool for x in o.contribs):
            continue
        out.append(c)
    return out


def describe(contribs: list[Contrib], limit: int = 3) -> str:
    return "; ".join(f"{c.member_name[:40]!r}: {c.text[:60]!r}" for c in contribs[:limit]) + (
        f" (+{len(contribs) - limit} more)" if len(contribs) > limit else "")


def _is_run(inner: tuple, outer: tuple) -> bool:
    n = len(inner)
    return any(outer[i:i + n] == inner for i in range(len(outer) - n + 1))


@dataclass
class DayAnalysis:
    sessions: dict                                  # id -> Sess
    exact_groups: list                              # [[id, ...]] identical named contributions
    matches: dict                                   # a -> b -> {a index: b index}

    def coverage(self, a: int, b: int) -> float:
        n = self.sessions[a].named
        return len(self.matches.get(a, {}).get(b, {})) / n if n else 0.0

    def contained(self, a: int, b: int) -> bool:
        """Every named contribution of a is also in b, and b has more."""
        sa, sb = self.sessions[a], self.sessions[b]
        return bool(sa.named) and sb.named > sa.named and self.coverage(a, b) == 1.0

    def holds_others(self, b: int) -> bool:
        """b holds every speech of a session with another title (a heading holding
        child debates, or a swallower). A same-title copy inside b is just an
        earlier, shorter version of the same debate."""
        sb = self.sessions[b]
        return any(self.contained(a, b) and self.sessions[a].place_key != sb.place_key
                   for a in self.matches.get(b, {}))

    def involved(self) -> set:
        ids = {i for g in self.exact_groups for i in g}
        for a, bs in self.matches.items():
            ids.add(a)
            ids.update(bs)
        return ids


def analyse(sessions: list[Sess]) -> DayAnalysis:
    """Exact duplicate groups, and every pair of sessions sharing a long contribution."""
    by_id = {s.id: s for s in sessions}
    by_sig: dict[tuple, list[int]] = defaultdict(list)
    for s in sessions:
        if s.named:
            by_sig[s.signature].append(s.id)
    groups = [ids for ids in by_sig.values() if len(ids) > 1]

    index: dict[str, set] = defaultdict(set)
    for s in sessions:
        for c in s.contribs:
            if len(c.text) >= INDEX_MIN_CHARS:
                index[c.head].add(s.id)
    matches: dict[int, dict[int, dict[int, int]]] = defaultdict(dict)
    for s in sessions:
        partners = set()
        for c in s.contribs:
            if len(c.text) >= INDEX_MIN_CHARS:
                partners |= index[c.head]
        for b in sorted(p for p in partners if p > s.id):
            m = align_sessions(s, by_id[b])
            if not any(len(s.contribs[i].text) >= INDEX_MIN_CHARS for i in m):
                continue
            matches[s.id][b] = m
            matches[b][s.id] = {j: i for i, j in m.items()}
    return DayAnalysis(by_id, groups, dict(matches))


# ---------------------------------------------------------------------------
# The cron's check (ingestion.ingest_date): log, count, never delete
# ---------------------------------------------------------------------------

def copies_and_swallows(day: date, house: str, check_ids: Iterable[int],
                        related: Iterable[tuple] = ()) -> list[tuple]:
    """
    (kind, inner Sess, outer Sess) for each session in check_ids (just stored
    or refreshed) that is an exact copy of another stored session on the same
    day ("duplicate"), or whose named contributions are an exact run inside a
    bigger one ("swallowed"), and for each other session that is a run inside
    one in check_ids. Small sessions (one short contribution) are ignored.
    `related`: (ext_id, ext_id) parent/child pairs from Hansard's ChildDebates;
    a session holding its own child debate is reported as kind "parent".
    """
    check = set(check_ids)
    if not check:
        return []
    pairs = {frozenset(p) for p in related}
    sessions = [s for s in load_day(day, house) if s.named]
    sigs = {s.id: s.signature for s in sessions}
    out = []
    seen_dupes = set()
    for inner in sessions:
        if inner.small:
            continue
        for outer in sessions:
            if inner.id == outer.id or (inner.id not in check and outer.id not in check):
                continue
            family = frozenset((inner.ext_id, outer.ext_id)) in pairs
            if sigs[inner.id] == sigs[outer.id]:
                pair = tuple(sorted((inner.id, outer.id)))
                if pair not in seen_dupes:
                    seen_dupes.add(pair)
                    out.append(("parent" if family else "duplicate", inner, outer))
            elif outer.named > inner.named and _is_run(sigs[inner.id], sigs[outer.id]):
                out.append(("parent" if family else "swallowed", inner, outer))
    return out


# ---------------------------------------------------------------------------
# Hansard API check: is the id still live?
# ---------------------------------------------------------------------------

class ApiStop(Exception):
    """429 / 401 / 403 from Hansard: stop calling."""


def check_ext_id(ext_id: str) -> tuple[str, Optional[dict], str]:
    """
    (LIVE / DEAD / UNKNOWN, the debate JSON when live, reason). Dead: HTTP 400
    or 404, or a 200 whose JSON has no "Overview" (Hansard's "An error has
    occurred." body; reason REASON_EMPTY_200 — see confirmed_status). A
    timeout, connection error, 5xx or bad JSON is UNKNOWN. Raises ApiStop on
    429 / 401 / 403. Uses ingestion._api_get (one retry on a timeout or
    connection error).
    """
    from hansard_archive.ingestion import HANSARD_API_BASE, _api_get
    url = f"{HANSARD_API_BASE}/debates/debate/{ext_id}.json"
    try:
        resp = _api_get(url)
    except requests.HTTPError as exc:
        code = getattr(exc.response, "status_code", None)
        if code in (401, 403, 429):
            raise ApiStop(f"HTTP {code} from the Hansard API at {ext_id} — stop and wait") from exc
        if code in (400, 404):
            return DEAD, None, f"HTTP {code}"
        return UNKNOWN, None, f"HTTP {code}"
    except requests.RequestException as exc:
        return UNKNOWN, None, type(exc).__name__
    try:
        data = resp.json()
    except ValueError:
        return UNKNOWN, None, "not JSON"
    if isinstance(data, dict) and data.get("Overview"):
        return LIVE, data, "ok"
    if isinstance(data, dict):
        return DEAD, None, REASON_EMPTY_200
    return UNKNOWN, None, "unexpected JSON"


class Checker:
    """
    Paced Hansard look-ups (at least API_PACE seconds apart), cached by ext_id.
    A 200 without a debate only counts as dead when a second look-up agrees
    (otherwise the second result stands). Stops (ApiStop) on 429/401/403 or
    after three unknowns in a row (CLAUDE.md three-strike rule).
    """

    def __init__(self, pace: float = API_PACE, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, max_unknown: int = 3):
        self.pace, self.sleep, self.clock = max(API_PACE, pace), sleep, clock
        self.max_unknown = max_unknown
        self.cache: dict[str, tuple] = {}
        self.calls = 0
        self.unknown_run = 0
        self._last: Optional[float] = None

    def _one(self, ext_id: str) -> tuple:
        if self._last is not None:
            wait = self.pace - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        try:
            return check_ext_id(ext_id)
        finally:
            self._last = self.clock()
            self.calls += 1

    def __call__(self, ext_id: str) -> tuple[str, Optional[dict], str]:
        if ext_id in self.cache:
            return self.cache[ext_id]
        result = self._one(ext_id)
        if result[0] == DEAD and result[2] == REASON_EMPTY_200:
            second = self._one(ext_id)
            result = (DEAD, None, f"{REASON_EMPTY_200} (twice)") if second[0] == DEAD else second
        self.cache[ext_id] = result
        if result[0] == UNKNOWN:
            self.unknown_run += 1
            if self.unknown_run >= self.max_unknown:
                raise ApiStop(f"{self.unknown_run} failed look-ups in a row (last {ext_id}: "
                              f"{result[2]}) - check the Hansard API before re-running")
        else:
            self.unknown_run = 0
        return result


# ---------------------------------------------------------------------------
# Swallowers: what would shrinking to Hansard's copy lose?
# ---------------------------------------------------------------------------

def shrink_check(s: Sess, fresh: list[dict], stored: list[dict],
                 held: Callable[[int], bool]) -> tuple[bool, list[str], int]:
    """
    Is shrinking s to Hansard's current copy (fresh) safe? stored: s's rows as
    dicts with "id" plus refresh._FIELDS, in page order; held(k): whether
    s.contribs[k] is also held by another stored session. Safe when every
    named row the refresh would remove or overwrite is held elsewhere AND the
    rows not held elsewhere match Hansard's copy. (ok, notes, rows removed).
    """
    h = contribs_from_fresh(fresh)
    idx_by_cid = {c.id: k for k, c in enumerate(s.contribs)}
    lost = set()
    for action, i, entry in plan_ops(stored, fresh):
        if action == "delete" or (action == "update" and
                                  _norm(stored[i]["speech_text"]) != _norm(entry["speech_text"])):
            k = idx_by_cid.get(stored[i]["id"])
            if k is not None:
                lost.add(k)
    unmatched = [s.contribs[k] for k in sorted(lost) if not held(k)]
    left = [c for k, c in enumerate(s.contribs) if not held(k)]
    left_ok = len(left) == len(h) and all(texts_match(x, y, fuzzy=True) for x, y in zip(left, h))
    notes = [f"ours {s.named} named, Hansard {len(h)}; {len(lost)} would be removed"]
    if unmatched:
        notes.append(f"{len(unmatched)} of them not held by any other stored session: "
                     f"{describe(unmatched)}")
    if not left_ok:
        notes.append(f"what is left after removing the speeches held elsewhere ({len(left)}) "
                     f"does not match Hansard's copy ({len(h)})")
    if not h:
        notes.append("Hansard's copy has no named contributions: shrinking empties the "
                     "session (needs --allow-empty)")
    return (not unmatched and left_ok), notes, len(lost)


def shrink_safety_now(session_id: int, fresh: list[dict], stored: list[dict]) -> tuple[bool, str]:
    """
    The apply-time re-check for refresh_partial_debates.py --accept-shrink:
    shrink_check against every other stored session that day, as stored now.
    """
    session = db.session.get(HansardSession, session_id)
    day = {x.id: x for x in load_day(session.date, session.house)}
    s = day.get(session_id)
    if s is None:
        return False, "session not found (or a container)"
    others = [(o, same_title(s, o)) for o in day.values() if o.id != s.id]

    def held(k: int) -> bool:
        c = s.contribs[k]
        return any(held_by(c, x, fz) for o, fz in others for x in o.contribs)

    ok, notes, _ = shrink_check(s, fresh, stored, held)
    return ok, "; ".join(notes)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

ACTION_KEEP = "keep"
ACTION_DELETE = "delete"
ACTION_ACCEPT_SHRINK = "accept_shrink"
ACTION_REVIEW = "review"


@dataclass
class Finding:
    session: Sess
    finding: str
    action: str
    status: str = UNKNOWN
    hansard_named: Optional[int] = None
    kept: Optional[Sess] = None
    coverage: Optional[float] = None
    group: str = ""
    notes: list = field(default_factory=list)


_STATUS_RANK = {LIVE: 0, UNKNOWN: 1, DEAD: 2}


def _hansard_signature(fresh: Optional[list]) -> Optional[tuple]:
    return None if fresh is None else tuple(c.sig for c in contribs_from_fresh(fresh))


def keep_rank(s: Sess, status: dict, hansard: dict) -> tuple:
    """Lower is better: live, then same as Hansard's current copy, then a final id,
    then a real title, then oldest."""
    same = _hansard_signature(hansard.get(s.id)) == s.signature
    return (_STATUS_RANK[status.get(s.id, UNKNOWN)], 0 if same else 1,
            0 if is_final_id(s.ext_id) else 1, 1 if s.is_heading else 0, s.id)


def _keep_reason(keep: Sess, runner_up: Sess, status: dict, hansard: dict) -> str:
    a, b = keep_rank(keep, status, hansard), keep_rank(runner_up, status, hansard)
    if a[0] != b[0]:
        st_keep, st_other = status.get(keep.id, UNKNOWN), status.get(runner_up.id, UNKNOWN)
        if st_keep == LIVE:
            return f"live in Hansard (id {runner_up.id} is {st_other})"
        return f"not known to be dead (could not be checked; id {runner_up.id} is {st_other})"
    reasons = (None, "same as Hansard's current copy", "final Hansard id",
               "debate title rather than a heading", "lowest id")
    for reason, x, y in zip(reasons, a, b):
        if reason and x != y:
            return reason
    return "lowest id"


def recommend(an: DayAnalysis, status: dict, hansard: dict,
              stored_full: Callable[[int], list]) -> list[Finding]:
    """
    Findings for one day and House, one per session involved (see
    scripts/find_duplicate_debates.py for what each action means).
    status: id -> LIVE / DEAD / UNKNOWN; hansard: id -> Hansard's contributions
    (_flatten_items output) for live ids; stored_full(id): the session's stored
    rows as dicts with "id" plus refresh._FIELDS, in page order.
    """
    S = an.sessions
    out: dict[int, Finding] = {}
    deleted: set[int] = set()
    day_key = ""
    if S:
        first = next(iter(S.values()))
        day_key = f"{first.date.isoformat()}-{first.house[0]}"

    def st(i: int) -> str:
        return status.get(i, UNKNOWN)

    def h_named(i: int) -> Optional[int]:
        fresh = hansard.get(i)
        return None if fresh is None else len(contribs_from_fresh(fresh))

    def matches_hansard(i: int) -> bool:
        return _hansard_signature(hansard.get(i)) == S[i].signature

    def shrinking(i: int) -> bool:
        n = h_named(i)
        return n is not None and n < S[i].named

    def can_be_twin(i: int) -> bool:
        return st(i) == LIVE and i not in deleted and not shrinking(i) and not an.holds_others(i)

    def finding(s: Sess, name: str, action: str, **kw) -> Finding:
        f = Finding(s, name, action, status=st(s.id), hansard_named=h_named(s.id), **kw)
        out[s.id] = f
        return f

    def others_in_day(exclude: set) -> list[Sess]:
        return [x for x in S.values() if x.id not in exclude and x.id not in deleted]

    def delete_if_safe(s: Sess, twin: Sess, f: Finding) -> None:
        """Turn f into a delete when s is dead, twin can stand in and nothing is lost."""
        m = an.matches.get(s.id, {}).get(twin.id) if s.id != twin.id else None
        if m is None:
            m = align_sessions(s, twin)
        ok_shape, why = twin_shape_ok(s, twin, m)
        unheld = unheld_speeches(s, twin, others_in_day({s.id}), m)
        if st(s.id) != DEAD:
            f.notes.append(f"Hansard id is {st(s.id)}, not dead: not deleted")
        elif st(twin.id) != LIVE:
            f.notes.append(f"the copy to keep is {st(twin.id)} in Hansard: not deleted")
        elif s.small:
            f.notes.append("small session (one short contribution): check by hand")
        elif not ok_shape:
            f.notes.append(why)
        elif unheld:
            f.notes.append(f"{len(unheld)} speech(es) held nowhere else: {describe(unheld)}")
        else:
            f.action = ACTION_DELETE
            deleted.add(s.id)

    # 1. Exact duplicates, grouped only when title, location and debate type agree.
    for n, ids in enumerate(an.exact_groups, 1):
        subgroups: dict[tuple, list[Sess]] = defaultdict(list)
        for i in ids:
            subgroups[S[i].group_key].append(S[i])
        for m_i, members in enumerate(subgroups.values(), 1):
            group = f"dup-{day_key}-{n}" + (f"{chr(96 + m_i)}" if len(subgroups) > 1 else "")
            elsewhere = sorted(i for i in ids if i not in {s.id for s in members})
            cross = (f"the same speeches are also stored under another title, location or "
                     f"type: id(s) {', '.join(map(str, elsewhere))}" if elsewhere else "")
            members.sort(key=lambda s: keep_rank(s, status, hansard))
            keep = members[0]
            if len(members) == 1:
                finding(keep, "exact_duplicate", ACTION_REVIEW, group=group,
                        notes=[cross + ": check by hand"])
                continue
            reason = _keep_reason(keep, members[1], status, hansard)
            confirmed = st(keep.id) == LIVE and matches_hansard(keep.id) and can_be_twin(keep.id)
            note = (f"{len(members)} copies with the same {keep.named} named contribution(s); "
                    f"keep id {keep.id} ({reason})" + (f"; {cross}" if cross else ""))
            if not confirmed:
                why = ("no copy is live in Hansard" if st(keep.id) != LIVE else
                       f"Hansard's copy of id {keep.id} now has {h_named(keep.id)} named "
                       f"contribution(s), not ours" if not matches_hansard(keep.id) else
                       f"id {keep.id} holds other debates")
                for s in members:
                    finding(s, "exact_duplicate", ACTION_REVIEW, group=group,
                            kept=None if s is keep else keep,
                            notes=[note, f"{why}: decide by hand which copy to keep"])
                continue
            finding(keep, "exact_duplicate", ACTION_KEEP, group=group, notes=[note])
            for s in members[1:]:
                f = finding(s, "exact_duplicate", ACTION_REVIEW, group=group, kept=keep,
                            coverage=1.0, notes=[note])
                delete_if_safe(s, keep, f)

    # 2. Dead ids whose speeches a live session with the same title holds.
    for s in S.values():
        if s.id in out or st(s.id) != DEAD or not s.named:
            continue
        best = None
        for b in an.matches.get(s.id, {}):
            sb = S[b]
            if not can_be_twin(b) or sb.place_key != s.place_key:
                continue
            cov = an.coverage(s.id, b)
            if cov < COVERAGE_MIN:
                continue
            key = (cov, an.coverage(b, s.id), -abs(sb.named - s.named), -b)
            if best is None or key > best[0]:
                best = (key, b)
        if best is not None:
            twin = S[best[1]]
            f = finding(s, "dead_superseded", ACTION_REVIEW, group=f"sup-{twin.id}", kept=twin,
                        coverage=an.coverage(s.id, twin.id),
                        notes=[f"Hansard no longer has this id; live id {twin.id} "
                               f"{twin.title[:60]!r} holds {an.coverage(s.id, twin.id):.0%} of "
                               f"its speeches"])
            delete_if_safe(s, twin, f)
            continue
        partners = [(an.coverage(s.id, b), b) for b in an.matches.get(s.id, {}) if b not in deleted]
        if partners:
            cov, b = max(partners)
            finding(s, "dead_partial", ACTION_REVIEW, kept=S[b], coverage=cov,
                    notes=[f"Hansard no longer has this id; no live session with the same title "
                           f"can stand in; best: {cov:.0%} of its speeches in id {b} ({st(b)}) "
                           f"{S[b].title[:60]!r}"])
        else:
            finding(s, "dead_only_copy", ACTION_REVIEW,
                    notes=["Hansard no longer has this id and no other stored session holds its "
                           "speeches: Hansard's final copy of this debate is probably missing "
                           "from the archive (re-ingest the day)"])

    # 3. Swallowers: a live session holding speeches that Hansard now puts elsewhere.
    def check(s: Sess, exclude: set) -> Optional[tuple]:
        fresh = hansard.get(s.id)
        if fresh is None or not shrinking(s.id):
            return None
        others = {b: m for b, m in an.matches.get(s.id, {}).items()
                  if b not in deleted and b not in exclude and st(b) == LIVE}
        if not others:
            return None
        elsewhere: set[int] = set()
        for m in others.values():
            elsewhere |= set(m)
        ok, notes, _lost = shrink_check(s, fresh, stored_full(s.id), lambda k: k in elsewhere)
        inside = [b for b in others if an.contained(b, s.id)]
        if inside:
            notes.insert(1, "holds all of " + ", ".join(
                f"id {b} {S[b].title[:40]!r} ({S[b].named})" for b in inside))
        return ok, notes, inside

    candidates = {}
    for s in S.values():
        if s.id in deleted or st(s.id) != LIVE:
            continue
        res = check(s, set())
        if res is not None:
            candidates[s.id] = res
    for sid, (ok, notes, inside) in candidates.items():
        if ok and len(candidates) > 1:
            # Don't count speeches held only by another session that is about to shrink.
            again = check(S[sid], set(candidates) - {sid})
            if again is None:
                ok, notes = False, notes + ["its speeches are only held by other swallowers"]
            else:
                ok, notes = again[0], again[1]
        action = ACTION_ACCEPT_SHRINK if ok else ACTION_REVIEW
        note = "; ".join(notes)
        prev = out.get(sid)
        if prev is not None:            # e.g. a copy in a duplicate group
            prev.finding += "+swallower"
            prev.notes.append(note)
            if prev.action == ACTION_KEEP:
                prev.action = action
        else:
            finding(S[sid], "swallower", action, group=f"swl-{sid}", notes=[note])
        for b in inside:
            if b not in out:
                finding(S[b], "swallowed", ACTION_KEEP, group=f"swl-{sid}",
                        kept=S[sid], coverage=1.0,
                        notes=[f"all its speeches are also in id {sid} {S[sid].title[:60]!r}"])

    # 4. Kept twins of deletions, listed for review; never delete a kept twin.
    for f in list(out.values()):
        k = f.kept
        if f.action != ACTION_DELETE or k is None:
            continue
        if k.id in deleted or (k.id in out and out[k.id].action not in (ACTION_KEEP,)):
            f.action = ACTION_REVIEW
            deleted.discard(f.session.id)
            f.notes.append(f"the copy to keep (id {k.id}) is not being kept as it is: not deleted")
        elif k.id not in out:
            finding(k, "kept_twin", ACTION_KEEP, group=f.group,
                    notes=[f"kept copy for id {f.session.id}"])
    return sorted(out.values(), key=lambda f: (f.group or "~", f.session.id))
