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
just before deleting) and ingestion.ingest_date (the cron logs and counts a
session it has just stored or refreshed that is a copy of, or sits inside,
another stored session; it never deletes anything).

How two copies are compared (named contributions only: the ones the debate
page shows; containers are ignored, they hold their children by design):
  exact   the same speaker (member id, else the attribution as stored) and
          the same text with whitespace collapsed, contribution by
          contribution (a hash of the text).
  fuzzy   provisional and final copies differ by small edits, so the two
          sessions are aligned in order (difflib on the first 60 characters
          of each text) and an aligned pair matches when the member ids don't
          differ and the texts are identical, one is a cut-off start of the
          other, or at least 80% of their words line up.
A session's "coverage" in another is the share of its named contributions
matched there.
"""

from __future__ import annotations

import difflib
import hashlib
import re
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

LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"

_FINAL_ID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
# Structural headings: a copy under one of these is the less useful title to keep.
_HEADING_HRS_TAGS = {"hs_6bbigboldhdg", "hs_3mainhdg", "hs_6bdepartment", "hs_3oralanswers",
                     "hs_venue"}


def is_final_id(ext_id: Optional[str]) -> bool:
    """Hansard's final ids are uppercase GUIDs; provisional ones are numeric or lowercase."""
    return bool(_FINAL_ID.fullmatch(ext_id or ""))


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


def contribs_from_fresh(fresh: list[dict]) -> list[Contrib]:
    """Named contributions from Hansard's copy (ingestion._flatten_items output)."""
    return [Contrib(0, c["member_id"], c["member_name"], _norm(c["speech_text"]))
            for c in fresh if c.get("member_name")]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _sessions_with_contribs(sessions: list) -> dict[int, Sess]:
    out = {s.id: Sess(s.id, s.ext_id, s.title or "", s.date, s.house, s.hrs_tag, s.slug,
                      s.debate_type) for s in sessions}
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

def texts_match(a: Contrib, b: Contrib) -> bool:
    if a.member_id and b.member_id and a.member_id != b.member_id:
        return False
    if a.text == b.text:
        return True
    short, long_ = sorted((a.text, b.text), key=len)
    if len(short) >= PREFIX_MIN_CHARS and long_.startswith(short):
        return True
    sm = difflib.SequenceMatcher(None, a.text.split(), b.text.split(), autojunk=False)
    return (sm.real_quick_ratio() >= FUZZY_MIN_RATIO and sm.quick_ratio() >= FUZZY_MIN_RATIO
            and sm.ratio() >= FUZZY_MIN_RATIO)


def align(a: list[Contrib], b: list[Contrib]) -> dict[int, int]:
    """{index in a: index in b} for the contributions of a matched, in order, in b."""
    sm = difflib.SequenceMatcher(None, [c.head for c in a], [c.head for c in b], autojunk=False)
    out: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("equal", "replace"):
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            if texts_match(a[i1 + k], b[j1 + k]):
                out[i1 + k] = j1 + k
    return out


def coverage(a: Sess, b: Sess) -> float:
    """Share of a's named contributions matched, in order, in b."""
    if not a.named:
        return 0.0
    return len(align(a.contribs, b.contribs)) / a.named


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
            m = align(s.contribs, by_id[b].contribs)
            if not any(len(s.contribs[i].text) >= INDEX_MIN_CHARS for i in m):
                continue
            matches[s.id][b] = m
            matches[b][s.id] = {j: i for i, j in m.items()}
    return DayAnalysis(by_id, groups, dict(matches))


# ---------------------------------------------------------------------------
# The cron's check (ingestion.ingest_date): log, count, never delete
# ---------------------------------------------------------------------------

def copies_and_swallows(day: date, house: str, check_ids: Iterable[int]) -> list[tuple]:
    """
    (kind, inner Sess, outer Sess) for each session in check_ids (just stored
    or refreshed) that is an exact copy of another stored session on the same
    day ("duplicate"), or whose named contributions are an exact run inside a
    bigger one ("swallowed"), and for each other session that is a run inside
    one in check_ids. Small sessions (one short contribution) are ignored.
    """
    check = set(check_ids)
    if not check:
        return []
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
            if sigs[inner.id] == sigs[outer.id]:
                pair = tuple(sorted((inner.id, outer.id)))
                if pair not in seen_dupes:
                    seen_dupes.add(pair)
                    out.append(("duplicate", inner, outer))
            elif outer.named > inner.named and _is_run(sigs[inner.id], sigs[outer.id]):
                out.append(("swallowed", inner, outer))
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
    occurred." body). A timeout, connection error, 5xx or bad JSON is UNKNOWN.
    Raises ApiStop on 429 / 401 / 403. Uses ingestion._api_get (one retry on
    a timeout or connection error).
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
        return DEAD, None, "200 without a debate"
    return UNKNOWN, None, "unexpected JSON"


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
_KEEP_REASONS = ("live in Hansard", "final Hansard id", "debate title rather than a heading",
                 "same as Hansard's current copy", "lowest id")


def _hansard_signature(fresh: Optional[list]) -> Optional[tuple]:
    return None if fresh is None else tuple(c.sig for c in contribs_from_fresh(fresh))


def keep_rank(s: Sess, status: dict, hansard: dict) -> tuple:
    """Lower is better: live, then a final id, then a real title, then same as Hansard, then oldest."""
    same = _hansard_signature(hansard.get(s.id)) == s.signature
    return (_STATUS_RANK[status.get(s.id, UNKNOWN)], 0 if is_final_id(s.ext_id) else 1,
            1 if s.is_heading else 0, 0 if same else 1, s.id)


def _keep_reason(keep: Sess, runner_up: Sess, status: dict, hansard: dict) -> str:
    a, b = keep_rank(keep, status, hansard), keep_rank(runner_up, status, hansard)
    for reason, x, y in zip(_KEEP_REASONS, a, b):
        if x != y:
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

    def finding(s: Sess, name: str, action: str, **kw) -> Finding:
        f = Finding(s, name, action, status=st(s.id), hansard_named=h_named(s.id), **kw)
        out[s.id] = f
        return f

    def best_live_cover(a: int, exclude: set) -> Optional[int]:
        best = None
        for b in an.matches.get(a, {}):
            if b in exclude or b in deleted or st(b) != LIVE:
                continue
            cov = an.coverage(a, b)
            if cov < COVERAGE_MIN:
                continue
            key = (cov, S[b].named, -b)
            if best is None or key > best[0]:
                best = (key, b)
        return best[1] if best else None

    # 1. Exact duplicates: keep one copy, delete the others.
    for n, ids in enumerate(an.exact_groups, 1):
        members = sorted((S[i] for i in ids), key=lambda s: keep_rank(s, status, hansard))
        keep, others = members[0], members[1:]
        group = f"dup-{day_key}-{n}"
        reason = _keep_reason(keep, others[0], status, hansard)
        cover = best_live_cover(keep.id, set(ids)) if st(keep.id) == DEAD else None
        note = (f"{len(members)} copies with the same {keep.named} named contribution(s); "
                f"keep id {keep.id} ({reason})")
        if cover is not None:
            note += f"; every copy has a dead id, so all go: live id {cover} holds the speeches"
        elif st(keep.id) == DEAD:
            note += "; every copy has a dead id and no live session holds the speeches"
        kept_sig = _hansard_signature(hansard.get(keep.id))
        if cover is None and kept_sig is not None and kept_sig != keep.signature:
            note += (f"; NB Hansard's copy of the kept id now has {h_named(keep.id)} named "
                     f"contribution(s): check which copy to keep")
        kept_obj = S[cover] if cover is not None else keep
        for s in members:
            if s is keep and cover is None:
                finding(s, "exact_duplicate", ACTION_KEEP, group=group, notes=[note])
                continue
            action = ACTION_REVIEW if s.small else ACTION_DELETE
            f = finding(s, "exact_duplicate", action, group=group, kept=kept_obj,
                        coverage=an.coverage(s.id, kept_obj.id) if kept_obj.id != s.id else 1.0,
                        notes=[note])
            if s.small:
                f.notes.append("small session (one short contribution): check by hand")
            if action == ACTION_DELETE:
                deleted.add(s.id)
        if cover is not None and cover not in out:
            finding(S[cover], "kept_twin", ACTION_KEEP, group=group,
                    notes=[f"holds the speeches of duplicate group {group}"])

    # 2. Dead ids whose speeches a live session holds: delete the dead copy.
    for s in S.values():
        if s.id in out or st(s.id) != DEAD or not s.named:
            continue
        cover = best_live_cover(s.id, set())
        if cover is not None:
            action = ACTION_REVIEW if s.small else ACTION_DELETE
            f = finding(s, "dead_superseded", action, group=f"sup-{cover}", kept=S[cover],
                        coverage=an.coverage(s.id, cover),
                        notes=[f"Hansard no longer has this id; live id {cover} "
                               f"{S[cover].title[:60]!r} holds "
                               f"{an.coverage(s.id, cover):.0%} of its speeches"])
            if s.small:
                f.notes.append("small session (one short contribution): check by hand")
            if action == ACTION_DELETE:
                deleted.add(s.id)
            continue
        partners = [(an.coverage(s.id, b), b) for b in an.matches.get(s.id, {}) if b not in deleted]
        if partners:
            cov, b = max(partners)
            finding(s, "dead_partial", ACTION_REVIEW, kept=S[b], coverage=cov,
                    notes=[f"Hansard no longer has this id; only {cov:.0%} of its speeches are "
                           f"in id {b} ({st(b)}) {S[b].title[:60]!r}"])
        else:
            finding(s, "dead_only_copy", ACTION_REVIEW,
                    notes=["Hansard no longer has this id and no other stored session holds its "
                           "speeches: Hansard's final copy of this debate is probably missing "
                           "from the archive (re-ingest the day)"])

    # 3. Swallowers: a live session holding speeches that Hansard now puts elsewhere.
    def shrink_check(s: Sess, exclude: set) -> Optional[tuple[bool, str, list]]:
        fresh = hansard.get(s.id)
        if fresh is None:
            return None
        h = contribs_from_fresh(fresh)
        if len(h) >= s.named:
            return None
        others = {b: m for b, m in an.matches.get(s.id, {}).items()
                  if b not in deleted and b not in exclude}
        if not others:
            return None
        elsewhere: set[int] = set()
        for b, m in others.items():
            elsewhere |= set(m)
        stored = stored_full(s.id)
        idx_by_cid = {c.id: k for k, c in enumerate(s.contribs)}
        lost = set()
        for action, i, entry in plan_ops(stored, fresh):
            if action == "delete" or (action == "update" and
                                      _norm(stored[i]["speech_text"]) != _norm(entry["speech_text"])):
                k = idx_by_cid.get(stored[i]["id"])
                if k is not None:
                    lost.add(k)
        unmatched = sorted(k for k in lost if k not in elsewhere)
        left = [c for k, c in enumerate(s.contribs) if k not in elsewhere]
        left_ok = len(left) == len(h) and all(texts_match(x, y) for x, y in zip(left, h))
        inside = [b for b in others if an.contained(b, s.id)]
        notes = [f"ours {s.named} named, Hansard {len(h)}; {len(lost)} would be removed"]
        if inside:
            notes.append("holds all of " + ", ".join(
                f"id {b} {S[b].title[:40]!r} ({S[b].named})" for b in inside))
        if unmatched:
            c = s.contribs[unmatched[0]]
            notes.append(f"{len(unmatched)} of them not found in any other stored session "
                         f"(first: {c.member_name[:40]!r}: {c.text[:60]!r})")
        if not left_ok:
            notes.append(f"what is left after removing the speeches held elsewhere ({len(left)}) "
                         f"does not match Hansard's copy ({len(h)})")
        return (not unmatched and left_ok), "; ".join(notes), inside

    candidates = {}
    for s in S.values():
        if s.id in deleted or st(s.id) != LIVE:
            continue
        res = shrink_check(s, set())
        if res is not None:
            candidates[s.id] = res
    for sid, (ok, note, inside) in candidates.items():
        if ok and len(candidates) > 1:
            # Don't count speeches held only by another session that is about to shrink.
            again = shrink_check(S[sid], set(candidates) - {sid})
            ok, note = (again[0], again[1]) if again else (False, note + "; only held by other swallowers")
        action = ACTION_ACCEPT_SHRINK if ok else ACTION_REVIEW
        prev = out.get(sid)
        if prev is not None:            # e.g. the kept copy of a duplicate group
            prev.finding += "+swallower"
            prev.action = action
            prev.notes.append(note)
        else:
            finding(S[sid], "swallower", action, group=f"swl-{sid}", notes=[note])
        for b in inside:
            if b not in out:
                finding(S[b], "swallowed", ACTION_KEEP, group=f"swl-{sid}",
                        kept=S[sid], coverage=1.0,
                        notes=[f"all its speeches are also in id {sid} {S[sid].title[:60]!r}"])

    # 4. Kept twins of deletions, listed for review.
    for f in list(out.values()):
        k = f.kept
        if f.action == ACTION_DELETE and k is not None and k.id not in out:
            finding(k, "kept_twin", ACTION_KEEP, group=f.group,
                    notes=[f"kept copy for id {f.session.id}"])
    return sorted(out.values(), key=lambda f: (f.group or "~", f.session.id))
