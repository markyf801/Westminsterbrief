"""
Repair Hansard sessions whose speeches are shown under the wrong speaker.

What went wrong: the April 2026 backfill stored every named item with text,
including the hs_TabledBy item ("Asked by Lord X: To ask His Majesty's
Government..." / "Moved by Lord X: That this House..."). On 1 May 2026
hs_tabledby joined the ingestion skip list, and scripts/refresh_speech_text.py
copied the new, shorter list's texts onto the old rows *by position*. Every text
after the dropped item moved up one row; the names stayed put. So each speech
is shown under the previous speaker's name, the tabled question's own text is
gone, and the last speech appears twice. Mostly Lords sessions dated up to
about 1 May 2026 (oral questions, Questions for Short Debate, motions, SIs,
Grand Committee); see the report for the measured scope.

What this script does, per session (one Hansard API call each, paced):
  - rebuilds what the old ingestion stored (every named item with text) and
    what current ingestion keeps (_flatten_items),
  - classifies the stored rows:
      ok                 stored rows match current ingestion
      shifted            the bug: speakers match the old list and texts match
                         the new list one row early -> REPAIRED
      legacy_unshifted   old rows (incl. the "Asked by" row) with their own,
                         correct text -> left alone, reported
      text_differs       right speakers, some text differs (e.g. a later
                         Hansard correction) -> left alone, reported
      mismatch           anything else -> left alone, reported
  - for "shifted" sessions: gives each speaker's row back its own text (row
    ids stay with the right speaker, so links to them become correct) and
    deletes the rows of items current ingestion skips (the "Asked by" /
    "Moved by" rows, which now hold someone else's words).

Dry run by default: nothing is written. Prints a before/after table for the
first --samples shifted sessions and a summary by state.

--execute writes, one transaction per session, and first creates an undo
table ha_contribution_repair_bak_YYYYMMDD_HHMMSS holding every changed or
deleted row as it was. Undo (Mark's call, production SQL log):
    UPDATE ha_contribution c SET speech_text = b.old_text
      FROM <bak> b WHERE b.action = 'update' AND c.id = b.contribution_id;
    INSERT INTO ha_contribution (id, session_id, member_id, member_name, party,
                                 speech_text, speech_order)
      SELECT contribution_id, session_id, member_id, member_name, party,
             old_text, speech_order FROM <bak> WHERE action = 'delete';

API rules: about 3 requests a second at most (0.3s gap plus response time);
a 429 stops the run at once (finished sessions stay committed); a timeout,
connection error or 5xx is retried once after 5 seconds, then the session is
skipped and counted.

Usage (Railway one-off against production; dry run first):
    python scripts/repair_shifted_contributions.py --house Lords --until 2026-05-31
    python scripts/repair_shifted_contributions.py --ext-id 115E0859-3219-42A8-AC48-BD09EC696208
    python scripts/repair_shifted_contributions.py --house Lords --until 2026-05-31 --execute
"""

import argparse
import collections
import os
import re
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

_API = "https://hansard-api.parliament.uk/debates/debate/{}.json"
_DELAY = 0.3
_TIMEOUT = 30
_MATCH_SHARE = 0.9  # share of rows whose text must line up to call a session shifted

STATES = ("ok", "shifted", "legacy_unshifted", "text_differs", "mismatch")


class RateLimited(Exception):
    pass


def fetch_session_json(ext_id: str) -> dict | None:
    """Session JSON, or None on a non-transient error. Raises RateLimited on 429."""
    for attempt in range(2):
        try:
            r = requests.get(_API.format(ext_id), timeout=_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 0:
                print(f"[repair] transient error, retrying in 5s: {exc}", flush=True)
                time.sleep(5)
                continue
            return None
        if r.status_code == 429:
            raise RateLimited(ext_id)
        if r.status_code >= 500 and attempt == 0:
            print(f"[repair] HTTP {r.status_code}, retrying in 5s", flush=True)
            time.sleep(5)
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def _walk(node: dict):
    """Items in the same order _flatten_items visits them."""
    for item in node.get("Items", []):
        yield item
    for child in node.get("ChildDebates", []):
        yield from _walk(child)


def build_lists(data: dict) -> list[dict]:
    """
    Every named item with text, in order, as the pre-May 2026 ingestion stored
    it (null-name rows were later deleted), each marked with whether current
    ingestion keeps it. Kept entries carry current ingestion's own fields.
    """
    from hansard_archive.ingestion import _clean_html, _flatten_items

    out = []
    for item in _walk(data):
        name = (item.get("MemberName") or item.get("memberName")
                or item.get("AttributedTo") or item.get("attributedTo"))
        text = _clean_html(item.get("Value") or item.get("value") or "")
        if not (name and text):
            continue
        kept = _flatten_items({"Items": [item]}, [0])
        if kept:
            out.append(dict(kept[0], kept=True))
        else:
            mid = item.get("MemberId") or item.get("memberId")
            try:
                mid = int(mid) if mid else None
            except (TypeError, ValueError):
                mid = None
            out.append({"member_id": mid, "member_name": name, "speech_text": text, "kept": False})
    return out


def speaker_key(member_id, member_name) -> tuple:
    return (int(member_id), None) if member_id else (None, (member_name or "").strip())


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def classify(rows: list, legacy: list[dict]) -> str:
    """rows: stored named contributions in display order (objects with
    member_id / member_name / speech_text)."""
    fresh = [e for e in legacy if e["kept"]]
    keys = [speaker_key(r.member_id, r.member_name) for r in rows]
    texts = [_norm(r.speech_text) for r in rows]
    fresh_keys = [speaker_key(e["member_id"], e["member_name"]) for e in fresh]

    if keys == fresh_keys:
        same = all(t == _norm(e["speech_text"]) for t, e in zip(texts, fresh))
        return "ok" if same else "text_differs"

    legacy_keys = [speaker_key(e["member_id"], e["member_name"]) for e in legacy]
    if keys == legacy_keys and len(legacy) > len(fresh):
        shifted_hits = sum(texts[i] == _norm(fresh[i]["speech_text"]) for i in range(len(fresh)))
        own_hits = sum(t == _norm(e["speech_text"]) for t, e in zip(texts, legacy))
        if fresh and shifted_hits >= _MATCH_SHARE * len(fresh) and shifted_hits > own_hits:
            return "shifted"
        if own_hits >= _MATCH_SHARE * len(legacy):
            return "legacy_unshifted"
    return "mismatch"


def plan_repair(rows: list, legacy: list[dict]) -> list[tuple]:
    """For a shifted session: [(row, 'update', new_text) | (row, 'delete', None)].
    Row j belongs to legacy entry j (same speaker), so it gets that entry's
    own text back, or is deleted if current ingestion skips the entry."""
    plan = []
    for row, entry in zip(rows, legacy):
        if not entry["kept"]:
            plan.append((row, "delete", None))
        elif row.speech_text != entry["speech_text"]:
            plan.append((row, "update", entry["speech_text"]))
    return plan


def _short(text: str, n: int = 60) -> str:
    t = _norm(text)
    return t if len(t) <= n else t[: n - 1] + "…"


def print_sample(session, rows: list, legacy: list[dict]) -> None:
    print(f"\n[sample] {session.date} {session.house} {session.title!r} (session id {session.id})")
    for row, entry in zip(rows, legacy):
        before = f"{(row.member_name or '')[:38]:38s} | {_short(row.speech_text)}"
        after = (f"DELETE (current ingestion skips this {_short(entry['speech_text'], 30)!r})"
                 if not entry["kept"] else _short(entry["speech_text"]))
        print(f"   row {row.id}: {before}\n   {'':>{len(str(row.id)) + 5}}-> {after}")


def _create_undo_table(db) -> str:
    from sqlalchemy import inspect, text
    base = f"ha_contribution_repair_bak_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    existing = set(inspect(db.engine).get_table_names())
    name, n = base, 1
    while name in existing:
        n += 1
        name = f"{base}_{n}"
    db.session.execute(text(
        f"CREATE TABLE {name} (contribution_id INTEGER NOT NULL, session_id INTEGER NOT NULL, "
        f"action VARCHAR(10) NOT NULL, member_id INTEGER, member_name VARCHAR(300), "
        f"party VARCHAR(100), speech_order INTEGER, old_text TEXT, new_text TEXT)"))
    db.session.commit()
    print(f"[repair] undo table: {name}", flush=True)
    return name


def apply_plan(db, HansardContribution, undo_table: str, session_id: int, plan: list) -> None:
    from sqlalchemy import text
    db.session.execute(
        text(f"INSERT INTO {undo_table} (contribution_id, session_id, action, member_id, "
             f"member_name, party, speech_order, old_text, new_text) VALUES "
             f"(:cid, :sid, :action, :mid, :mname, :party, :sorder, :old, :new)"),
        [{"cid": r.id, "sid": session_id, "action": action, "mid": r.member_id,
          "mname": r.member_name, "party": r.party, "sorder": r.speech_order,
          "old": r.speech_text, "new": new} for r, action, new in plan])
    delete_ids = [r.id for r, action, _ in plan if action == "delete"]
    if delete_ids:
        (db.session.query(HansardContribution)
         .filter(HansardContribution.responds_to_id.in_(delete_ids))
         .update({HansardContribution.responds_to_id: None}, synchronize_session=False))
    for r, action, new in plan:
        if action == "update":
            r.speech_text = new
        else:
            db.session.delete(r)
    db.session.commit()


def _with_db_retry(db, fn):
    for _ in range(3):
        try:
            return fn()
        except OperationalError as exc:
            print(f"[repair] DB error, retrying: {exc}", flush=True)
            db.session.rollback()
            db.engine.dispose()
            time.sleep(5)
    raise SystemExit("[repair] DB unavailable — stopping")


def run(args) -> dict:
    from flask_app import app
    from extensions import db
    from hansard_archive.models import HansardContribution, HansardSession

    counts: collections.Counter = collections.Counter()
    by_house: collections.Counter = collections.Counter()
    listed: dict = collections.defaultdict(list)
    shifted_dates: list = []
    rows_updated = rows_deleted = fetch_errors = samples_shown = 0
    stopped = None

    with app.app_context():
        undo_table = _create_undo_table(db) if args.execute else None
        q = db.session.query(HansardSession.id).filter(
            HansardSession.is_container.is_(False),
            HansardSession.contributions_ingested.is_(True))
        if args.house:
            q = q.filter(HansardSession.house == args.house)
        if args.since:
            q = q.filter(HansardSession.date >= date.fromisoformat(args.since))
        if args.until:
            q = q.filter(HansardSession.date <= date.fromisoformat(args.until))
        if args.ext_id:
            q = q.filter(HansardSession.ext_id.in_(args.ext_id))
        ids = [r[0] for r in _with_db_retry(db, lambda: q.order_by(HansardSession.id).all())]
        if args.limit:
            ids = ids[: args.limit]
        print(f"[repair] {len(ids)} sessions to check ({'EXECUTE' if args.execute else 'dry run'})",
              flush=True)

        for n, sid in enumerate(ids, 1):
            session = _with_db_retry(db, lambda sid=sid: db.session.get(HansardSession, sid))
            rows = _with_db_retry(db, lambda sid=sid: (
                HansardContribution.query
                .filter(HansardContribution.session_id == sid,
                        HansardContribution.member_name.isnot(None))
                .order_by(HansardContribution.speech_order, HansardContribution.id)
                .all()))
            try:
                data = fetch_session_json(session.ext_id)
            except RateLimited:
                stopped = f"429 from Hansard API at session {sid} — stopped; wait before re-running"
                break
            finally:
                time.sleep(_DELAY)
            if data is None:
                fetch_errors += 1
                continue

            legacy = build_lists(data)
            state = classify(rows, legacy)
            counts[state] += 1
            by_house[(session.house, state)] += 1
            if state != "ok" and len(listed[state]) < args.list_max:
                listed[state].append(f"{session.date} {session.house} id={session.id} "
                                     f"{(session.title or '')[:60]!r} rows={len(rows)}")

            if state == "shifted":
                shifted_dates.append(session.date)
                plan = plan_repair(rows, legacy)
                if samples_shown < args.samples:
                    print_sample(session, rows, legacy)
                    samples_shown += 1
                upd = sum(1 for _, a, _ in plan if a == "update")
                dele = len(plan) - upd
                if args.execute and plan:
                    _with_db_retry(db, lambda: apply_plan(db, HansardContribution, undo_table,
                                                          session.id, plan))
                    for r, action, _ in plan:
                        print(f"[undo] {action},{r.id},{session.id}", flush=True)
                else:
                    db.session.rollback()
                rows_updated += upd
                rows_deleted += dele

            if n % 50 == 0:
                print(f"[repair] checked {n}/{len(ids)} — "
                      + ", ".join(f"{s}={counts[s]}" for s in STATES), flush=True)

    print("\n[repair] Sessions by state:")
    for s in STATES:
        print(f"  {counts[s]:6d}  {s}")
    print("[repair] By house:")
    for (house, s), c in sorted(by_house.items()):
        print(f"  {c:6d}  {house:8s} {s}")
    for s in STATES[1:]:
        if listed[s]:
            print(f"\n[repair] {s} (first {len(listed[s])}):")
            for line in listed[s]:
                print(f"  {line}")
    if shifted_dates:
        print(f"\n[repair] shifted sessions dated {min(shifted_dates)} to {max(shifted_dates)}")
    verb = "written" if args.execute else "would be written"
    print(f"\n[repair] rows: {rows_updated} text updates, {rows_deleted} deletions ({verb}); "
          f"fetch_errors={fetch_errors}")
    if undo_table:
        print(f"[repair] undo table: {undo_table}")
    if stopped:
        print(f"[repair] {stopped}")
    return {"counts": dict(counts), "updated": rows_updated, "deleted": rows_deleted,
            "fetch_errors": fetch_errors, "undo_table": undo_table, "stopped": stopped}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--execute", action="store_true", help="write repairs (default: dry run)")
    p.add_argument("--house", choices=("Commons", "Lords"))
    p.add_argument("--since", help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", help="YYYY-MM-DD, inclusive")
    p.add_argument("--ext-id", action="append", help="only this Hansard ExtId (repeatable)")
    p.add_argument("--limit", type=int, help="check at most N sessions (by id)")
    p.add_argument("--samples", type=int, default=3,
                   help="print a before/after table for the first N shifted sessions")
    p.add_argument("--list-max", type=int, default=40,
                   help="list at most N sessions per non-ok state")
    run(p.parse_args())


if __name__ == "__main__":
    main()
