"""
Hansard Archive — bring an already-stored debate up to date with Hansard.

Why: the archive cron stores a debate the first time it sees it. A debate seen
while it was still going on (or before Hansard finished publishing it) used to
stay cut short for ever, because ingest_date() skipped any ext_id already in
ha_session. Example: the PM's "International Update" statement, Commons,
12 Oct 2026, stored with 1 contribution; Hansard has about 200.

Used by ingest_date() (scripts/archive_cron.py, for sessions in its window)
and by scripts/refresh_partial_debates.py (one-off backfill for older debates).

What a refresh keeps and changes
  - The ha_session row is never replaced: its id, slug, URL, title, type,
    department and tags stay, and so does every link to it (ha_division,
    ha_session_theme, briefings, sitemap).
  - Contribution rows are matched to Hansard's current list by speaker, in
    order (difflib on the speaker sequence), not blindly by position:
      * a stored row whose speaker lines up with Hansard's is updated in place
        (text, name, party, order), so its id - and any #contrib-<id> link to
        it - stays with the same speech;
      * Hansard's extra speeches (the usual case: the debate carried on) are
        inserted as new rows after them;
      * stored rows Hansard no longer has are deleted;
      * where Hansard changed the speaker at a spot, the rows there are reused
        in place, so a #contrib-<id> link at that exact spot now shows the
        corrected speaker's words.
    Copying text by position alone is what moved Lords speeches onto the
    wrong speaker in May 2026 (scripts/repair_shifted_contributions.py); the
    speaker alignment is there to avoid that.
  - Whether a session "changed" is judged on named contributions (the ones
    the debate page shows): speaker, attribution text and text with
    whitespace collapsed. Unnamed structural rows alone never trigger a
    refresh, but are brought in line when a refresh happens.

Safety
  - Hansard returning no named contributions never empties a stored session
    ("empty": left alone). A fetch error never reaches here (the caller has
    no data and skips the session).
  - If Hansard now has fewer than half the named contributions we hold, the
    session is left alone and reported ("shrank") - more likely a partial
    API response than a real edit. Smaller drops (Hansard removing a
    duplicate, merging two speeches) are applied.
"""

from __future__ import annotations

import difflib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from extensions import db
from hansard_archive.models import HansardContribution, HansardSession, HansardSessionTheme

# A session is re-tagged after a refresh when its named contribution count
# changed or its text grew/shrank by more than this share.
RETAG_TEXT_CHANGE = 0.10

# Hansard having fewer than this share of our named contributions = hold.
SHRINK_HOLD_SHARE = 0.5

STATE_UNCHANGED = "unchanged"
STATE_CHANGED = "changed"
STATE_EMPTY = "empty"      # Hansard has no named contributions: never wipe
STATE_SHRANK = "shrank"    # Hansard has under half of ours: hold for a person to look

_FIELDS = ("member_id", "member_name", "party", "speech_text", "speech_order")


def speaker_key(member_id, member_name) -> tuple:
    """Who a row belongs to: member id when known, else the raw attribution."""
    return (int(member_id), None) if member_id else (None, (member_name or "").strip())


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _row_dict(row) -> dict:
    return {f: getattr(row, f) for f in _FIELDS}


def _named_signature(items: list[dict]) -> list[tuple]:
    return [(speaker_key(c["member_id"], c["member_name"]), (c["member_name"] or "").strip(),
             _norm(c["speech_text"])) for c in items if c.get("member_name")]


@dataclass
class RefreshResult:
    state: str
    named_before: int = 0
    named_after: int = 0
    chars_before: int = 0
    chars_after: int = 0
    updated: int = 0
    inserted: int = 0
    deleted: int = 0
    kept: int = 0
    undo: list = field(default_factory=list)   # dicts, filled when applied

    @property
    def needs_retag(self) -> bool:
        if self.state != STATE_CHANGED:
            return False
        if self.named_before != self.named_after:
            return True
        return abs(self.chars_after - self.chars_before) > RETAG_TEXT_CHANGE * max(1, self.chars_before)

    def summary(self) -> str:
        return (f"named {self.named_before} -> {self.named_after}; rows {self.updated} updated, "
                f"{self.inserted} added, {self.deleted} removed, {self.kept} unchanged")


def assess(stored: list[dict], fresh: list[dict]) -> RefreshResult:
    """Compare stored rows (dicts of _FIELDS, in display order) with Hansard's list."""
    old_sig = _named_signature(stored)
    new_sig = _named_signature(fresh)
    res = RefreshResult(
        state=STATE_UNCHANGED,
        named_before=len(old_sig), named_after=len(new_sig),
        chars_before=sum(len(s[2]) for s in old_sig), chars_after=sum(len(s[2]) for s in new_sig),
    )
    if not new_sig:
        res.state = STATE_UNCHANGED if not old_sig else STATE_EMPTY
        return res
    if old_sig == new_sig:
        return res
    if len(new_sig) < SHRINK_HOLD_SHARE * len(old_sig):
        res.state = STATE_SHRANK
        return res
    res.state = STATE_CHANGED
    return res


def plan_ops(stored: list[dict], fresh: list[dict]) -> list[tuple[str, int | None, dict | None]]:
    """
    Row operations turning `stored` into `fresh`: (action, stored index, fresh entry)
    with action in keep / update / insert / delete. Rows are aligned on the
    speaker sequence; see the module docstring.
    """
    old_keys = [speaker_key(r["member_id"], r["member_name"]) for r in stored]
    new_keys = [speaker_key(c["member_id"], c["member_name"]) for c in fresh]
    ops: list[tuple[str, int | None, dict | None]] = []

    def pair(i: int, entry: dict) -> None:
        same = all(stored[i][f] == entry[f] for f in _FIELDS)
        ops.append(("keep" if same else "update", i, entry))

    sm = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pair(i1 + k, fresh[j1 + k])
            continue
        n_pair = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        for k in range(n_pair):
            pair(i1 + k, fresh[j1 + k])
        for i in range(i1 + n_pair, i2):
            ops.append(("delete", i, None))
        for j in range(j1 + n_pair, j2):
            ops.append(("insert", None, fresh[j]))
    return ops


def stored_rows(session_id: int) -> list:
    return (HansardContribution.query
            .filter(HansardContribution.session_id == session_id)
            .order_by(HansardContribution.speech_order, HansardContribution.id)
            .all())


def refresh_session(session: HansardSession, fresh: list[dict], apply: bool = True,
                    rows: Optional[list] = None) -> RefreshResult:
    """
    Assess one stored session against Hansard's current contributions
    (_flatten_items output) and, when it changed and apply=True, rewrite its
    contribution rows as described in the module docstring. Does not commit.
    When applied, result.undo lists every change with the old values.
    """
    rows = stored_rows(session.id) if rows is None else rows
    stored = [_row_dict(r) for r in rows]
    res = assess(stored, fresh)
    if res.state != STATE_CHANGED:
        return res

    ops = plan_ops(stored, fresh)
    counts = Counter(action for action, _, _ in ops)
    res.kept, res.updated = counts["keep"], counts["update"]
    res.inserted, res.deleted = counts["insert"], counts["delete"]
    if not apply:
        return res

    delete_ids = [rows[i].id for action, i, _ in ops if action == "delete"]
    if delete_ids:
        (HansardContribution.query
         .filter(HansardContribution.responds_to_id.in_(delete_ids))
         .update({HansardContribution.responds_to_id: None}, synchronize_session=False))

    def undo_entry(action: str, row, old_text, new_text) -> dict:
        return {"action": action, "contribution_id": row.id, "session_id": session.id,
                "member_id": row.member_id, "member_name": row.member_name, "party": row.party,
                "speech_order": row.speech_order, "old_text": old_text, "new_text": new_text}

    new_rows = []
    for action, i, entry in ops:
        if action == "update":
            row = rows[i]
            res.undo.append(undo_entry("update", row, row.speech_text, entry["speech_text"]))
            for f in _FIELDS:
                setattr(row, f, entry[f])
        elif action == "delete":
            row = rows[i]
            res.undo.append(undo_entry("delete", row, row.speech_text, None))
            db.session.delete(row)
        elif action == "insert":
            row = HansardContribution(session_id=session.id, responds_to_id=None,
                                      **{f: entry[f] for f in _FIELDS})
            db.session.add(row)
            new_rows.append(row)
    db.session.flush()
    # Undo entries hold each changed row as it was (for inserts: the new row's id).
    for row in new_rows:
        res.undo.append(undo_entry("insert", row, None, row.speech_text))
    session.contributions_ingested = True
    if res.needs_retag:
        clear_tag_attempts(session.id)
    return res


def clear_tag_attempts(session_id: int) -> None:
    """Let the tagger try a session again after its text changed (ha_tag_attempt is optional)."""
    import sqlalchemy as sa
    # Inspect through this transaction's own connection: tagger._attempts_table_ready()
    # checks out a pool connection, which on a single-connection pool resets
    # (rolls back) the writes this refresh has just flushed.
    if not sa.inspect(db.session.connection()).has_table("ha_tag_attempt"):
        return
    from hansard_archive.models import HaTagAttempt
    (HaTagAttempt.query
     .filter(HaTagAttempt.item_kind == "session", HaTagAttempt.item_id == session_id)
     .delete(synchronize_session=False))


def retag_refreshed(session_ids: list[int], api_key: Optional[str] = None, limit: int = 30,
                    verbose: bool = True, sleep: Callable[[float], None] = time.sleep) -> dict:
    """
    Re-tag refreshed sessions that already have tags (untagged ones are left to
    tag_all_untagged). New tags replace the old ones only when they include a
    verified policy area; otherwise the old tags stay (as scripts/retag.py).
    Stops after the tagger's MAX_CONSECUTIVE_API_FAILURES, or at once on a
    final API error. Commits per session.
    """
    from hansard_archive import tagger as tg

    stats = {"retagged": 0, "kept_old": 0, "untagged": 0, "api_errors": 0,
             "over_cap": 0, "aborted": ""}
    api_key = tg._api_key(api_key)
    ids = list(dict.fromkeys(session_ids))
    if not api_key:
        stats["aborted"] = "GEMINI_API_KEY not set"
        return stats
    tagged_ids = {r[0] for r in db.session.query(HansardSessionTheme.session_id)
                  .filter(HansardSessionTheme.session_id.in_(ids)).distinct()} if ids else set()
    todo = [i for i in ids if i in tagged_ids]
    stats["untagged"] = len(ids) - len(todo)
    stats["over_cap"] = max(0, len(todo) - limit)
    consecutive = 0
    for sid in todo[:limit]:
        session = db.session.get(HansardSession, sid)
        if session is None:
            continue
        si = tg.session_input_from_db(session)
        if not any(t.strip() for _, t in si.contributions):
            continue
        try:
            outcome = tg.tag_session_input(si, api_key)
            consecutive = 0
        except tg.GeminiCallFailed as e:
            stats["api_errors"] += 1
            consecutive += 1
            print(f"[retag] API ERROR session {sid}: {e}", flush=True)
            if e.final or consecutive >= tg.MAX_CONSECUTIVE_API_FAILURES:
                stats["aborted"] = f"stopped after {consecutive} API failure(s): {e}"
                break
            continue
        finally:
            sleep(tg._INTER_REQUEST_DELAY)
        if outcome.ok:
            tg.write_session_tags(sid, outcome, replace=True)
            stats["retagged"] += 1
            if verbose:
                print(f"[retag] {session.title[:60]!r} — {outcome.policy_areas}", flush=True)
        else:
            stats["kept_old"] += 1
            if verbose:
                print(f"[retag] {session.title[:60]!r} — nothing verified, old tags kept", flush=True)
    return stats
