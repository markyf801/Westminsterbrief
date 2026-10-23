"""
Bring stored Hansard debates up to date with Hansard — one-off backfill.

Why: until the archive cron learned to refresh sessions (hansard_archive/
refresh.py), a debate stored while it was still going on, or before Hansard
finished publishing it, stayed cut short for ever. A sample of 41 debates from
Sept-Oct 2026 found 19 of the 33 with speeches cut short (we held about a fifth
of their contributions). The cron now fixes debates in its 3-day window; this
script fixes the older ones. The hourly cron went live on 1 May 2026; debates
before that were bulk-loaded complete, so --since 2026-04-27 covers it.

Per session (one Hansard API call each, paced) it compares what we store with
what Hansard has now and classifies the session:
  unchanged      same named contributions (speaker, attribution, text)
  refresh        Hansard's copy changed -> brought up to date in place (same
                 session id, slug, URL and tags; rows matched on speaker and
                 text, so #contrib-<id> links stay with their speech; Hansard's
                 extra speeches added as new rows; rows Hansard dropped removed;
                 a session that had no speeches gets its debate type re-worked
                 out)
  held_empty     Hansard returned no named contributions -> left alone
  held_shrank    Hansard has under half our named contributions -> left alone
  legacy_rows    our rows still include the "Asked by" / "Moved by" items
                 current ingestion skips (Lords, ingested before May 2026) ->
                 left alone for scripts/repair_shifted_contributions.py
  not_found      Hansard answered 404 -> skipped and counted
  dead_id        Hansard answered 400 (or 200 without a debate) for this id:
                 a provisional id Hansard has since replaced with its final
                 one -> skipped and counted (usually a duplicate copy:
                 scripts/find_duplicate_debates.py)
  fetch_failed   the API call failed -> skipped and counted

Without --until it stops 4 days before today, so it never touches the debates
the cron is refreshing (3-day window). Writers also lock each session row
while refreshing it, so an overlap with the cron can't add speeches twice.

DRY RUN BY DEFAULT: nothing is written. --execute writes, one transaction per
session, after creating an undo table ha_contribution_refresh_bak_YYYYMMDD_
HHMMSS holding every changed, added or removed row (and every re-worked debate
type), numbered in order (seq). The undo SQL (UNDO_SQL below, Mark's call,
production SQL log) is printed at the end of every --execute run; it restores
each row as it was before its first change.

Tags: this script does not call Gemini. A refreshed session that had no tags
is picked up by the cron's tagger (its "given up" attempt count is cleared).
A refreshed session that already had tags and whose text changed materially is
listed at the end with a ready scripts/retag.py --ids command (that script has
its own dry run and --backup).

Resumable: with --execute, progress (last session id done, counts, undo table,
sessions to re-tag) is checkpointed in ha_cron_run (service_name
"refresh-partial-<hash of the arguments>") after every refreshed session and
every 25 sessions; running the same command again carries on and reuses the
same undo table. A dry run can be continued by hand with --after-id (printed
when it stops).

Cron windows: it doesn't run in or just before a production cron window
(hansard_archive/cron_windows.py: weekday daytime is blocked whole), dry run
or not: it refuses to start, or stops after the current session, with exit
code 4 (checkpoint saved). Override with --ignore-cron-windows.

API rules: one request at a time, at least 0.5s apart; a 429, 401 or 403 stops
the run at once (exit 3; finished sessions stay committed); a timeout,
connection error or 5xx is retried once after 5 seconds, then the session is
skipped; three failed sessions in a row stop the run (exit 3). A 404 is
counted but doesn't count towards that stop, nor does a dead id (a 400, or a
200 with no debate in it).

--accept-shrink (only with --ext-id, never with a date range): applies a
held_shrank or held_empty result for the sessions named, through the same
path (undo table, re-tag list). For headings that swallowed the debates
published after them while Hansard's copy was still being built, once a
person has checked them (scripts/find_duplicate_debates.py lists candidates).
A held_empty session ends up with no named contributions.

Usage (Railway one-off against production; dry run first):
    python scripts/refresh_partial_debates.py --since 2026-04-27
    python scripts/refresh_partial_debates.py --ext-id 54FB2EDB-83AF-40FE-8A98-A914AD221CD7
    python scripts/refresh_partial_debates.py --since 2026-04-27 --execute
    python scripts/refresh_partial_debates.py --accept-shrink --ext-id <id> --ext-id <id>

Exit codes: 0 done, 2 bad arguments, 3 stopped on API errors, 4 stopped for a
cron window (run the same command later to resume).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

_API = "https://hansard-api.parliament.uk/debates/debate/{}.json"
_DELAY = 0.5
_TIMEOUT = 30
_MAX_CONSECUTIVE_FETCH_ERRORS = 3   # CLAUDE.md three-strike rule
_CHECKPOINT_EVERY = 25
_RETAG_IDS_PER_LINE = 100
_DEFAULT_UNTIL_DAYS_AGO = 4         # the cron refreshes the last 3 days itself

STATES = ("unchanged", "refresh", "held_empty", "held_shrank", "legacy_rows",
          "not_found", "dead_id", "fetch_failed")

NOT_FOUND = "not_found"   # fetch_session_json() result for a 404
DEAD_ID = "dead_id"       # ... for a 400, or a 200 without "Overview" (id replaced by Hansard)

# Restores every row as it was before its first change (lowest seq per row).
UNDO_SQL = """\
-- 1. Remove the rows the refresh added.
DELETE FROM ha_contribution
 WHERE id IN (SELECT contribution_id FROM {bak} WHERE action = 'insert');
-- 2. Put changed rows back.
UPDATE ha_contribution
   SET member_id = b.member_id, member_name = b.member_name, party = b.party,
       speech_order = b.speech_order, speech_text = b.old_text
  FROM {bak} b
 WHERE b.action IN ('update', 'delete') AND ha_contribution.id = b.contribution_id
   AND b.seq = (SELECT MIN(e.seq) FROM {bak} e
                 WHERE e.contribution_id = b.contribution_id AND e.action IN ('update', 'delete'));
-- 3. Re-create removed rows.
INSERT INTO ha_contribution (id, session_id, member_id, member_name, party, speech_text,
                             speech_order, ingested_at)
SELECT b.contribution_id, b.session_id, b.member_id, b.member_name, b.party, b.old_text,
       b.speech_order, b.ingested_at
  FROM {bak} b
 WHERE b.action IN ('update', 'delete')
   AND b.seq = (SELECT MIN(e.seq) FROM {bak} e
                 WHERE e.contribution_id = b.contribution_id AND e.action IN ('update', 'delete'))
   AND b.contribution_id IN (SELECT contribution_id FROM {bak} WHERE action = 'delete')
   AND b.contribution_id NOT IN (SELECT contribution_id FROM {bak} WHERE action = 'insert');
-- 4. Put back debate types re-worked out for sessions that had no speeches.
UPDATE ha_session SET debate_type = b.old_text
  FROM {bak} b
 WHERE b.action = 'type' AND ha_session.id = b.session_id
   AND b.seq = (SELECT MIN(e.seq) FROM {bak} e
                 WHERE e.session_id = b.session_id AND e.action = 'type');
"""


class ApiStop(Exception):
    """429 / 401 / 403, or too many failures in a row: stop, don't keep calling."""


class WindowStop(Exception):
    """A production cron window is due: stop cleanly before doing more."""


def fetch_session_json(ext_id: str, sleep=time.sleep) -> dict | str | None:
    """Session JSON; NOT_FOUND on a 404; DEAD_ID on a 400 or a 200 without a
    debate in it; None on another failed fetch. Raises ApiStop on 429/401/403."""
    for attempt in range(2):
        try:
            r = requests.get(_API.format(ext_id), timeout=_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 0:
                print(f"[refresh] transient error, retrying in 5s: {exc}", flush=True)
                sleep(5)
                continue
            return None
        if r.status_code == 429:
            raise ApiStop(f"429 from the Hansard API at {ext_id} — wait before re-running")
        if r.status_code in (401, 403):
            raise ApiStop(f"HTTP {r.status_code} from the Hansard API at {ext_id} (permanent error)")
        if r.status_code == 404:
            return NOT_FOUND
        if r.status_code == 400:
            return DEAD_ID
        if r.status_code >= 500 and attempt == 0:
            print(f"[refresh] HTTP {r.status_code}, retrying in 5s", flush=True)
            sleep(5)
            continue
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return data if data.get("Overview") else DEAD_ID
    return None


def _walk(node: dict):
    """Items in the order _flatten_items visits them."""
    for item in node.get("Items", []):
        yield item
    for child in node.get("ChildDebates", []):
        yield from _walk(child)


def legacy_named_keys(data: dict) -> list[tuple]:
    """Speakers of every named item with text, as ingestion stored them before May 2026
    (it kept the "Asked by" / "Moved by" hs_TabledBy items current ingestion skips)."""
    from hansard_archive.ingestion import _clean_html
    from hansard_archive.refresh import speaker_key

    keys = []
    for item in _walk(data):
        name = (item.get("MemberName") or item.get("memberName")
                or item.get("AttributedTo") or item.get("attributedTo"))
        if name and _clean_html(item.get("Value") or item.get("value") or ""):
            mid = item.get("MemberId") or item.get("memberId")
            try:
                mid = int(mid) if mid else None
            except (TypeError, ValueError):
                mid = None
            keys.append(speaker_key(mid, name))
    return keys


def is_legacy(rows: list, fresh: list[dict], data: dict) -> bool:
    from hansard_archive.refresh import speaker_key
    stored = [speaker_key(r.member_id, r.member_name) for r in rows if r.member_name]
    fresh_keys = [speaker_key(c["member_id"], c["member_name"]) for c in fresh if c["member_name"]]
    legacy = legacy_named_keys(data)
    return len(legacy) > len(fresh_keys) and stored == legacy and stored != fresh_keys


# ---------------------------------------------------------------------------
# Checkpoint (ha_cron_run) and undo table
# ---------------------------------------------------------------------------

def checkpoint_service(args) -> str:
    # The arguments as given (a defaulted --until moves daily; resuming by id copes with that).
    fields = {"since": args.since, "until": args.until, "house": args.house,
              "ext_id": sorted(args.ext_id or []), "limit": args.limit}
    if getattr(args, "accept_shrink", False):
        fields["accept_shrink"] = True    # only when set: older checkpoints keep their name
    key = json.dumps(fields, sort_keys=True)
    return f"refresh-partial-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def load_checkpoint(service: str) -> dict:
    from hansard_archive.models import HaCronRun
    row = (HaCronRun.query.filter_by(service_name=service)
           .order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first())
    if row is None or not row.notes:
        return {}
    try:
        return json.loads(row.notes)
    except ValueError:
        return {}


def start_checkpoint(service: str, state: dict) -> int:
    from extensions import db
    from hansard_archive.models import HaCronRun
    row = HaCronRun(service_name=service, started_at=datetime.utcnow(), days_window=0,
                    status="running", notes=json.dumps(state))
    db.session.add(row)
    db.session.commit()
    return row.id


def save_checkpoint(run_id: int, state: dict, status: str = "running") -> None:
    from extensions import db
    from hansard_archive.models import HaCronRun
    row = db.session.get(HaCronRun, run_id)
    if row is None:
        return
    row.notes = json.dumps(state)
    row.status = status
    row.sessions_ingested = state.get("counts", {}).get("refresh", 0)
    row.errors = state.get("counts", {}).get("fetch_failed", 0)
    if status != "running":
        row.finished_at = datetime.utcnow()
    db.session.commit()


def create_undo_table(db) -> str:
    from sqlalchemy import inspect, text
    base = f"ha_contribution_refresh_bak_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    existing = set(inspect(db.engine).get_table_names())
    name, n = base, 1
    while name in existing:
        n += 1
        name = f"{base}_{n}"
    db.session.execute(text(
        f"CREATE TABLE {name} (seq INTEGER NOT NULL, contribution_id INTEGER NOT NULL, "
        f"session_id INTEGER NOT NULL, action VARCHAR(10) NOT NULL, member_id INTEGER, "
        f"member_name VARCHAR(300), party VARCHAR(100), speech_order INTEGER, old_text TEXT, "
        f"ingested_at TIMESTAMP)"))
    db.session.commit()
    print(f"[refresh] undo table: {name}", flush=True)
    return name


def write_undo(db, table: str, entries: list[dict]) -> None:
    """Append entries to the undo table, numbered after the ones already there."""
    from sqlalchemy import text
    if not entries:
        return
    start = db.session.execute(text(f"SELECT COALESCE(MAX(seq), 0) FROM {table}")).scalar() or 0
    db.session.execute(
        text(f"INSERT INTO {table} (seq, contribution_id, session_id, action, member_id, "
             f"member_name, party, speech_order, old_text, ingested_at) VALUES (:seq, "
             f":contribution_id, :session_id, :action, :member_id, :member_name, :party, "
             f":speech_order, :old_text, :ingested_at)"),
        [dict(e, seq=start + k) for k, e in enumerate(entries, 1)])


def _with_db_retry(db, fn):
    for _ in range(3):
        try:
            return fn()
        except OperationalError as exc:
            print(f"[refresh] DB error, retrying: {exc}", flush=True)
            db.session.rollback()
            db.engine.dispose()
            time.sleep(5)
    raise SystemExit("[refresh] DB unavailable — stopping")


def check_cron_window(args, clock=datetime.utcnow) -> None:
    from hansard_archive.cron_windows import blocked_window
    if args.ignore_cron_windows:
        return
    label = blocked_window(clock())
    if label:
        raise WindowStop(label)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def effective_until(args, today: date) -> str | None:
    """--until, or 4 days before today for a --since selection (the cron's own window)."""
    if args.until or not args.since:
        return args.until
    return (today - timedelta(days=_DEFAULT_UNTIL_DAYS_AGO)).isoformat()


def select_sessions(args, until: str | None) -> list[tuple[int, str]]:
    from extensions import db
    from hansard_archive.models import HansardSession
    q = db.session.query(HansardSession.id, HansardSession.ext_id).filter(
        HansardSession.is_container.is_(False))
    if args.house:
        q = q.filter(HansardSession.house == args.house)
    if args.since:
        q = q.filter(HansardSession.date >= date.fromisoformat(args.since))
    if until:
        q = q.filter(HansardSession.date <= date.fromisoformat(until))
    if args.ext_id:
        q = q.filter(HansardSession.ext_id.in_(args.ext_id))
    rows = [(r[0], r[1]) for r in q.order_by(HansardSession.id).all()]
    return rows[: args.limit] if args.limit else rows


def run(args, sleep=time.sleep, clock=datetime.utcnow) -> dict:
    from extensions import db
    from hansard_archive.ingestion import _flatten_items, _official_debate_type_label
    from hansard_archive.models import HansardSession, HansardSessionTheme
    from hansard_archive.refresh import (STATE_CHANGED, STATE_EMPTY, STATE_SHRANK,
                                         refresh_session, stored_rows)

    until = effective_until(args, clock().date())
    if until and not args.until:
        print(f"[refresh] --until not given: stopping at {until} (the cron refreshes the "
              f"last 3 days itself)", flush=True)
    ids = _with_db_retry(db, lambda: select_sessions(args, until))
    db.session.rollback()

    state: dict = {"counts": {s: 0 for s in STATES}, "retag_ids": [], "last_id": 0}
    run_id = None
    service = checkpoint_service(args)
    if args.execute:
        saved = load_checkpoint(service)
        if saved.get("last_id"):
            state.update(saved)
            state["counts"] = {s: saved.get("counts", {}).get(s, 0) for s in STATES}
            print(f"[refresh] resuming after session id {saved['last_id']} "
                  f"(checkpoint ha_cron_run service_name={service})", flush=True)
    after = max(state.get("last_id", 0), args.after_id or 0)
    todo = [(sid, ext) for sid, ext in ids if sid > after]
    print(f"[refresh] {len(ids)} sessions selected, {len(todo)} to check "
          f"({'EXECUTE' if args.execute else 'dry run — nothing is written'})", flush=True)

    counts = collections.Counter(state["counts"])
    by_house: collections.Counter = collections.Counter()
    listed: dict = collections.defaultdict(list)
    rows_changed = collections.Counter()
    stopped = None
    exit_code = 0
    consecutive_errors = 0
    last_done = after

    def snapshot() -> dict:
        return {**state, "counts": {s: counts[s] for s in STATES}, "last_id": last_done}

    try:
        if todo:
            check_cron_window(args, clock)
        if args.execute and todo:
            if not state.get("undo_table"):
                state["undo_table"] = create_undo_table(db)
            else:
                print(f"[refresh] undo table: {state['undo_table']} (from the checkpoint)", flush=True)
            run_id = start_checkpoint(service, snapshot())

        for n, (sid, ext_id) in enumerate(todo, 1):
            check_cron_window(args, clock)
            # Fetch first, so no DB transaction is open during the HTTP call.
            try:
                data = fetch_session_json(ext_id, sleep=sleep)
            finally:
                sleep(_DELAY)
            session = _with_db_retry(db, lambda sid=sid: db.session.get(HansardSession, sid))
            if data is None or data in (NOT_FOUND, DEAD_ID):
                st = {NOT_FOUND: "not_found", DEAD_ID: "dead_id"}.get(data, "fetch_failed")
                counts[st] += 1
                by_house[(session.house, st)] += 1
                if st == "fetch_failed":
                    consecutive_errors += 1
                    if consecutive_errors >= _MAX_CONSECUTIVE_FETCH_ERRORS:
                        raise ApiStop(f"{consecutive_errors} failed fetches in a row at session "
                                      f"{sid} — check the Hansard API before re-running")
                if len(listed[st]) < args.list_max:
                    listed[st].append(f"{session.date} {session.house} id={sid} "
                                      f"{(session.title or '')[:60]!r} ext_id={ext_id}")
                last_done = sid
                db.session.rollback()
                continue
            consecutive_errors = 0

            fresh = _flatten_items(data, [0])
            rows = _with_db_retry(db, lambda sid=sid: stored_rows(sid))
            if is_legacy(rows, fresh, data):
                st, res = "legacy_rows", None
            else:
                # Locks the session row and re-reads its rows when writing.
                res = refresh_session(session, fresh, apply=args.execute,
                                      debate_type_label=_official_debate_type_label(data),
                                      accept_shrink=args.accept_shrink)
                st = {STATE_CHANGED: "refresh", STATE_EMPTY: "held_empty",
                      STATE_SHRANK: "held_shrank"}.get(res.state, "unchanged")
            counts[st] += 1
            by_house[(session.house, st)] += 1
            if st != "unchanged" and len(listed[st]) < args.list_max:
                detail = (f" named {res.named_before} -> {res.named_after}" if res else
                          f" rows={sum(1 for r in rows if r.member_name)}")
                if res and res.type_change:
                    detail += f"; type {res.type_change[0]} -> {res.type_change[1]}"
                listed[st].append(f"{session.date} {session.house} id={sid} "
                                  f"{(session.title or '')[:60]!r}{detail}")
            if st == "refresh":
                rows_changed.update(updated=res.updated, inserted=res.inserted,
                                    deleted=res.deleted, types=1 if res.type_change else 0)
                had_tags = _with_db_retry(db, lambda sid=sid: HansardSessionTheme.query
                                          .filter_by(session_id=sid).first() is not None)
                if res.needs_retag and had_tags:
                    state["retag_ids"].append(sid)
                if args.execute:
                    write_undo(db, state["undo_table"], res.undo)
                    db.session.commit()
                    print(f"[refresh] REFRESHED id={sid} {session.date} {session.house} "
                          f"{(session.title or '')[:60]!r} — {res.summary()}", flush=True)
                if args.accept_shrink and res.named_after < res.named_before:
                    note = "" if args.execute else " (dry run: not written)"
                    print(f"[refresh] SHRINK ACCEPTED id={sid} ext_id={ext_id} named "
                          f"{res.named_before} -> {res.named_after}{note}", flush=True)
            db.session.rollback()   # end this session's transaction (no-op after a commit)
            last_done = sid

            if run_id and (st == "refresh" or n % _CHECKPOINT_EVERY == 0):
                save_checkpoint(run_id, snapshot())
            if n % 50 == 0:
                print(f"[refresh] checked {n}/{len(todo)} — "
                      + ", ".join(f"{s}={counts[s]}" for s in STATES), flush=True)
    except ApiStop as exc:
        db.session.rollback()
        stopped, exit_code = str(exc), 3
    except WindowStop as exc:
        db.session.rollback()
        stopped, exit_code = f"cron window due: {exc} — run the same command later to resume", 4
    except Exception:
        db.session.rollback()
        if run_id:
            save_checkpoint(run_id, {**snapshot(), "stopped": "crashed"}, status="failed")
        raise

    if run_id:
        status = "ok" if not stopped else ("stopped" if exit_code == 4 else "failed")
        save_checkpoint(run_id, {**snapshot(), **({"stopped": stopped} if stopped else {})}, status)

    print("\n[refresh] Sessions by state:")
    for s in STATES:
        print(f"  {counts[s]:6d}  {s}")
    print("[refresh] By house:")
    for (house, s), c in sorted(by_house.items()):
        print(f"  {c:6d}  {house:8s} {s}")
    for s in STATES[1:]:
        if listed[s]:
            print(f"\n[refresh] {s} (first {len(listed[s])}):")
            for line in listed[s]:
                print(f"  {line}")
    verb = "written" if args.execute else "would be written"
    print(f"\n[refresh] rows: {rows_changed['updated']} updated, {rows_changed['inserted']} added, "
          f"{rows_changed['deleted']} removed; {rows_changed['types']} debate type(s) re-worked "
          f"out ({verb}, this run)")
    if state.get("undo_table"):
        print(f"[refresh] undo table: {state['undo_table']}")
        print("[refresh] undo SQL (production SQL is Mark's call):\n"
              + UNDO_SQL.format(bak=state["undo_table"]))
    if state["retag_ids"]:
        print(f"\n[refresh] {len(state['retag_ids'])} refreshed session(s) already had tags and "
              f"changed materially — re-tag with (dry run first, then add --apply --backup):")
        ids_ = state["retag_ids"]
        for i in range(0, len(ids_), _RETAG_IDS_PER_LINE):
            chunk = ",".join(str(x) for x in ids_[i:i + _RETAG_IDS_PER_LINE])
            print(f"  python scripts/retag.py --what sessions --ids {chunk}")
    if counts["dead_id"]:
        print(f"\n[refresh] {counts['dead_id']} session(s) have an id Hansard no longer "
              f"recognises (replaced by a final id): usually a duplicate copy - see "
              f"scripts/find_duplicate_debates.py")
    if stopped:
        print(f"\n[refresh] STOPPED: {stopped}")
        if not args.execute:
            print(f"[refresh] continue this dry run with --after-id {last_done}")
    return {"counts": {s: counts[s] for s in STATES}, "rows": dict(rows_changed),
            "undo_table": state.get("undo_table"), "retag_ids": state["retag_ids"],
            "stopped": stopped, "exit_code": exit_code, "last_id": last_done}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--execute", action="store_true", help="write the refreshes (default: dry run)")
    p.add_argument("--since", help="session date YYYY-MM-DD, inclusive (required unless --ext-id)")
    p.add_argument("--until", help="session date YYYY-MM-DD, inclusive (default with --since: "
                                   "4 days before today)")
    p.add_argument("--house", choices=("Commons", "Lords"))
    p.add_argument("--ext-id", action="append", help="only this Hansard ExtId (repeatable)")
    p.add_argument("--limit", type=int, help="check at most N sessions (lowest ids first)")
    p.add_argument("--after-id", type=int, help="skip sessions with id <= N (continue a dry run)")
    p.add_argument("--list-max", type=int, default=40, help="list at most N sessions per state")
    p.add_argument("--ignore-cron-windows", action="store_true")
    p.add_argument("--accept-shrink", action="store_true",
                   help="apply held_shrank / held_empty results (only with --ext-id, for "
                        "reviewed sessions; held_empty leaves no named contributions)")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not args.since and not args.ext_id:
        p.print_usage()
        print("refresh_partial_debates.py: give --since (or --ext-id)", file=sys.stderr)
        return 2
    if args.accept_shrink and (not args.ext_id or args.since or args.until or args.house
                               or args.limit or args.after_id):
        p.print_usage()
        print("refresh_partial_debates.py: --accept-shrink only works with --ext-id "
              "(the reviewed sessions), not with a date range or other selection",
              file=sys.stderr)
        return 2
    try:
        for d in (args.since, args.until):
            if d:
                date.fromisoformat(d)
    except ValueError as exc:
        print(f"refresh_partial_debates.py: bad date: {exc}", file=sys.stderr)
        return 2

    from flask_app import app
    with app.app_context():
        result = run(args)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
