"""
Hansard Archive — bring an already-stored debate up to date with Hansard.

Why: the archive cron stores a debate the first time it sees it. A debate seen
while it was still going on (or before Hansard finished publishing it) used to
stay cut short for ever, because ingest_date() skipped any ext_id already in
ha_session. Example: the PM's "International Update" statement, Commons,
12 Oct 2026, stored with 1 contribution; Hansard has 186.

Used by ingest_date() (scripts/archive_cron.py, for sessions in its window)
and by scripts/refresh_partial_debates.py (one-off backfill for older debates).

What a refresh keeps and changes
  - The ha_session row is never replaced: its id, slug, URL, title,
    department and tags stay, and so does every link to it (ha_division,
    ha_session_theme, briefings, sitemap). Its debate type is re-worked out
    only when we held no named speeches for it before (the type can depend on
    the first speech); a debate that already had speeches keeps its type
    (scripts/reclassify_debate_types.py covers the rest).
  - Contribution rows are matched to Hansard's current list so that a row id
    - and any #contrib-<id> link to it - stays with the same speech:
      1. rows whose speaker AND text (whitespace collapsed) match a Hansard
         speech are paired first, in order (difflib on (speaker, text));
      2. inside the stretches left over, rows are paired with a Hansard
         speech by the same speaker, in order (difflib on the speaker): this
         is how a speech cut off mid-sentence gets its full text;
      3. a leftover row is reused for a Hansard speech by a different speaker
         only when the two texts are identical (an attribution correction);
         otherwise the row is deleted and Hansard's speech added as a new row.
    So Hansard's extra speeches are new rows, rows Hansard no longer has are
    deleted, and a link never moves onto a different person's words.
    Copying text by position alone is what moved Lords speeches onto the
    wrong speaker in May 2026 (scripts/repair_shifted_contributions.py).
  - Whether a session "changed" is judged on named contributions (the ones
    the debate page shows): speaker, attribution text and text with
    whitespace collapsed. Unnamed structural rows alone never trigger a
    refresh, but are brought in line when a refresh happens.

Safety
  - Hansard returning no named contributions never empties a stored session
    ("empty": left alone). A fetch error never reaches here (the caller has
    no data and skips the session).
  - Fewer named contributions on Hansard than we hold:
      cron (strict=True): any net loss of more than 3 rows and more than 10%
        is held ("shrank") and logged - an hourly job should not act on what
        is more likely a partial API response;
      backfill (strict=False): held only below half of ours.
    Smaller drops (Hansard removing a duplicate, merging two speeches) are
    applied.
  - accept_shrink=True (scripts/refresh_partial_debates.py --accept-shrink,
    only together with --ext-id, for sessions a person has reviewed) applies
    a held "shrank" or "empty" result: the session ends up with Hansard's
    contributions, which for "empty" means no named contributions at all.
    Used for a heading that swallowed the debates published after it while
    Hansard's copy was still being built (scripts/find_duplicate_debates.py
    lists them). Nothing else ever empties a session.
  - Writers lock the ha_session row (SELECT ... FOR NO KEY UPDATE: it does not
    block inserts into tables pointing at the session) and read its rows
    after the lock, so the cron and the backfill refreshing the same session
    at once can't both add the same speeches. The lock is held until the
    caller commits or rolls back.
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

# Backfill: Hansard having fewer than this share of our named contributions = hold.
SHRINK_HOLD_SHARE = 0.5
# Cron (strict): hold a net loss of more than this many rows AND this share.
STRICT_MAX_LOSS_ROWS = 3
STRICT_MAX_LOSS_SHARE = 0.10

STATE_UNCHANGED = "unchanged"
STATE_CHANGED = "changed"
STATE_EMPTY = "empty"      # Hansard has no named contributions: never wipe
STATE_SHRANK = "shrank"    # Hansard has far fewer than us: hold for a person to look

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
    type_change: Optional[tuple] = None       # (old, new) debate_type, when re-worked out
    undo: list = field(default_factory=list)   # dicts, filled when applied

    @property
    def needs_retag(self) -> bool:
        if self.state != STATE_CHANGED:
            return False
        if self.named_before != self.named_after:
            return True
        return abs(self.chars_after - self.chars_before) > RETAG_TEXT_CHANGE * max(1, self.chars_before)

    def summary(self) -> str:
        out = (f"named {self.named_before} -> {self.named_after}; rows {self.updated} updated, "
               f"{self.inserted} added, {self.deleted} removed, {self.kept} unchanged")
        if self.type_change:
            out += f"; type {self.type_change[0]} -> {self.type_change[1]}"
        return out


def assess(stored: list[dict], fresh: list[dict], strict: bool = False,
           accept_shrink: bool = False) -> RefreshResult:
    """Compare stored rows (dicts of _FIELDS, in display order) with Hansard's list.
    accept_shrink turns a held result (empty / shrank) into a change."""
    old_sig = _named_signature(stored)
    new_sig = _named_signature(fresh)
    res = RefreshResult(
        state=STATE_UNCHANGED,
        named_before=len(old_sig), named_after=len(new_sig),
        chars_before=sum(len(s[2]) for s in old_sig), chars_after=sum(len(s[2]) for s in new_sig),
    )
    if not new_sig:
        if not old_sig:
            return res
        res.state = STATE_CHANGED if accept_shrink else STATE_EMPTY
        return res
    if old_sig == new_sig:
        return res
    loss = len(old_sig) - len(new_sig)
    if strict:
        held = loss > STRICT_MAX_LOSS_ROWS and loss > STRICT_MAX_LOSS_SHARE * len(old_sig)
    else:
        held = len(new_sig) < SHRINK_HOLD_SHARE * len(old_sig)
    res.state = STATE_SHRANK if held and not accept_shrink else STATE_CHANGED
    return res


def plan_ops(stored: list[dict], fresh: list[dict]) -> list[tuple[str, int | None, dict | None]]:
    """
    Row operations turning `stored` into `fresh`: (action, stored index, fresh entry)
    with action in keep / update / insert / delete. See the module docstring
    for the three matching passes.
    """
    ops: list[tuple[str, int | None, dict | None]] = []
    old_keys = [speaker_key(r["member_id"], r["member_name"]) for r in stored]
    new_keys = [speaker_key(c["member_id"], c["member_name"]) for c in fresh]
    old_text = [_norm(r["speech_text"]) for r in stored]
    new_text = [_norm(c["speech_text"]) for c in fresh]

    def pair(i: int, j: int) -> None:
        same = all(stored[i][f] == fresh[j][f] for f in _FIELDS)
        ops.append(("keep" if same else "update", i, fresh[j]))

    def unmatched(i1: int, i2: int, j1: int, j2: int) -> None:
        sm = difflib.SequenceMatcher(None, old_keys[i1:i2], new_keys[j1:j2], autojunk=False)
        for tag, a1, a2, b1, b2 in sm.get_opcodes():
            a1, a2, b1, b2 = a1 + i1, a2 + i1, b1 + j1, b2 + j1
            if tag == "equal":
                for k in range(a2 - a1):
                    pair(a1 + k, b1 + k)
                continue
            n = min(a2 - a1, b2 - b1) if tag == "replace" else 0
            leftover_old = list(range(a1 + n, a2))
            leftover_new = list(range(b1 + n, b2))
            for k in range(n):
                if old_text[a1 + k] == new_text[b1 + k]:
                    pair(a1 + k, b1 + k)        # same words, corrected attribution
                else:
                    leftover_old.append(a1 + k)
                    leftover_new.append(b1 + k)
            ops.extend(("delete", i, None) for i in sorted(leftover_old))
            ops.extend(("insert", None, fresh[j]) for j in sorted(leftover_new))

    sm = difflib.SequenceMatcher(None, list(zip(old_keys, old_text)), list(zip(new_keys, new_text)),
                                 autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pair(i1 + k, j1 + k)
        else:
            unmatched(i1, i2, j1, j2)
    return ops


def stored_rows(session_id: int) -> list:
    return (HansardContribution.query
            .filter(HansardContribution.session_id == session_id)
            .order_by(HansardContribution.speech_order, HansardContribution.id)
            .all())


def lock_session(session_id: int) -> None:
    """SELECT ... FOR NO KEY UPDATE on the ha_session row until commit/rollback (no-op on SQLite)."""
    lock_query(session_id).one()


def lock_query(session_id: int):
    return (db.session.query(HansardSession.id)
            .filter(HansardSession.id == session_id)
            .with_for_update(key_share=True))


def refresh_session(session: HansardSession, fresh: list[dict], apply: bool = True,
                    strict: bool = False, debate_type_label: str = "",
                    accept_shrink: bool = False) -> RefreshResult:
    """
    Assess one stored session against Hansard's current contributions
    (_flatten_items output) and, when it changed and apply=True, rewrite its
    contribution rows as described in the module docstring. With apply=True
    the session row is locked first and its rows are read after the lock; the
    caller must commit (or roll back, also when nothing changed, to release
    the lock). When applied, result.undo lists every change with the old values.
    accept_shrink: apply a held shrink/empty result (reviewed sessions only;
    see the module docstring).
    """
    if apply:
        lock_session(session.id)
    rows = stored_rows(session.id)
    stored = [_row_dict(r) for r in rows]
    res = assess(stored, fresh, strict=strict, accept_shrink=accept_shrink)
    if res.state != STATE_CHANGED:
        return res

    ops = plan_ops(stored, fresh)
    counts = Counter(action for action, _, _ in ops)
    res.kept, res.updated = counts["keep"], counts["update"]
    res.inserted, res.deleted = counts["insert"], counts["delete"]

    if res.named_before == 0:
        from hansard_archive.ingestion import classify_session
        new_type = classify_session(session.title or "", session.location or "",
                                    session.hrs_tag or "", session.house,
                                    debate_type_label=debate_type_label or "",
                                    first_speech=fresh[0]["speech_text"] if fresh else "")
        if new_type != session.debate_type:
            res.type_change = (session.debate_type, new_type)
    if not apply:
        return res

    delete_ids = [rows[i].id for action, i, _ in ops if action == "delete"]
    if delete_ids:
        (HansardContribution.query
         .filter(HansardContribution.responds_to_id.in_(delete_ids))
         .update({HansardContribution.responds_to_id: None}, synchronize_session=False))

    def undo_entry(action: str, row, old_text) -> dict:
        return {"action": action, "contribution_id": row.id, "session_id": session.id,
                "member_id": row.member_id, "member_name": row.member_name, "party": row.party,
                "speech_order": row.speech_order, "old_text": old_text,
                "ingested_at": row.ingested_at}

    new_rows = []
    for action, i, entry in ops:
        if action == "update":
            row = rows[i]
            res.undo.append(undo_entry("update", row, row.speech_text))
            for f in _FIELDS:
                setattr(row, f, entry[f])
        elif action == "delete":
            row = rows[i]
            res.undo.append(undo_entry("delete", row, row.speech_text))
            db.session.delete(row)
        elif action == "insert":
            row = HansardContribution(session_id=session.id, responds_to_id=None,
                                      **{f: entry[f] for f in _FIELDS})
            db.session.add(row)
            new_rows.append(row)
    db.session.flush()
    # Undo entries hold each changed row as it was (for inserts: the new row's id).
    for row in new_rows:
        res.undo.append(undo_entry("insert", row, None))
    if res.type_change:
        res.undo.append({"action": "type", "contribution_id": 0, "session_id": session.id,
                         "member_id": None, "member_name": None, "party": None,
                         "speech_order": None, "old_text": res.type_change[0], "ingested_at": None})
        session.debate_type = res.type_change[1]
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
    final API error. Any other error on one session is logged, counted in
    "errors" and the run moves on. Commits per session.

    "pending" lists the ids still to re-tag (over the cap, transient API
    failures, or not reached after a stop) so the caller can carry them to the
    next run. A session whose own call failed with a final error is not listed.
    """
    from hansard_archive import tagger as tg

    stats = {"retagged": 0, "kept_old": 0, "untagged": 0, "api_errors": 0, "errors": 0,
             "over_cap": 0, "aborted": "", "pending": []}
    api_key = tg._api_key(api_key)
    ids = list(dict.fromkeys(session_ids))
    if not api_key:
        stats["aborted"] = "GEMINI_API_KEY not set"
        stats["pending"] = ids
        return stats
    tagged_ids = {r[0] for r in db.session.query(HansardSessionTheme.session_id)
                  .filter(HansardSessionTheme.session_id.in_(ids)).distinct()} if ids else set()
    todo = [i for i in ids if i in tagged_ids]
    stats["untagged"] = len(ids) - len(todo)
    stats["over_cap"] = max(0, len(todo) - limit)
    pending = list(todo[limit:])
    consecutive = 0
    for n, sid in enumerate(todo[:limit]):
        try:
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
                if not e.final:     # a final error (bad request/key/model) would fail again
                    pending.append(sid)
                print(f"[retag] API ERROR session {sid}: {e}", flush=True)
                if e.final or consecutive >= tg.MAX_CONSECUTIVE_API_FAILURES:
                    stats["aborted"] = f"stopped after {consecutive} API failure(s): {e}"
                    pending.extend(todo[n + 1:limit])
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
                    print(f"[retag] {session.title[:60]!r} — nothing verified, old tags kept",
                          flush=True)
        except Exception as e:  # noqa: BLE001 - one bad session must not stop the cron run
            stats["errors"] += 1
            db.session.rollback()
            print(f"[retag] ERROR session {sid}: {type(e).__name__}: {e}", flush=True)
    stats["pending"] = list(dict.fromkeys(pending))
    return stats
