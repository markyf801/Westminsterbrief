"""
Find debates the archive stores twice, or that swallowed other debates — READ-ONLY.

Why: the hourly archive cron stores debates while Hansard's provisional copy
is still being built (see hansard_archive/dedupe.py). Two problems follow:
  - a heading that was the last open one holds the speeches published after
    it, which Hansard later moves to their own sessions (we store those too);
  - Hansard replaces provisional ids with final ones and the old ids go dead
    (HTTP 400), but we keep both copies, so a debate shows two to five times.

Per sitting day and House since --since (default 2026-04-27, when the hourly
cron started) it compares every stored debate's named contributions (the
speeches the page shows) with every other's and reports:
  exact_duplicate  identical named contributions (speaker + text) to another
                   session: one copy is kept — live in Hansard first, then a
                   final (uppercase GUID) id, then a real debate title rather
                   than a heading, then the copy matching Hansard's current
                   one, then the oldest — and the reason is logged.
  dead_superseded  Hansard no longer has this id, and a live session holds at
                   least 90% of its speeches (small text edits allowed):
                   delete it, keep the live one.
  swallower        a live session holding speeches Hansard now has fewer of,
                   which are held by other stored sessions:
                   accept_shrink when everything the refresh would remove is
                   held elsewhere AND what is left matches Hansard's copy
                   (then scripts/refresh_partial_debates.py --accept-shrink
                   --ext-id ... brings it in line); otherwise review.
  swallowed        a session whose speeches all sit inside a swallower (kept).
  kept_twin        the copy kept for a deletion (listed so it can be checked).
  dead_partial     a dead id only partly held elsewhere: review.
  dead_only_copy   a dead id nobody else holds: Hansard's final copy of the
                   debate is probably missing from the archive: review.
Small sessions (one named contribution under 300 characters) are never
recommended for deletion, only for review.

Hansard API check (skip with --no-api): every session involved is looked up
once (/debates/debate/<id>.json, at least 1 second apart): HTTP 400 or 404,
or a 200 without a debate in it, = dead; a timeout, 5xx or bad JSON =
unknown. A 429, 401 or 403 stops the run (exit 3); so do three unknowns in a
row. Without the API check nothing is known to be live or dead, so only exact
duplicates get a keep/delete recommendation. --check-provisional also looks up
every session in range with a provisional (numeric / lowercase) id, to find
dead ids that have no twin stored at all (dead_only_copy).

Output: a CSV (--csv PATH, and always printed to the log between
"----- CSV START -----" and "----- CSV END -----" so a Railway one-shot's log
holds it), a summary, and ready commands:
  scripts/delete_duplicate_debates.py --session-id <delete>:<keep>,...   (dry run first)
  scripts/refresh_partial_debates.py --accept-shrink --ext-id ...         (dry run first)
Review the CSV before running either; the delete script takes the CSV too
(rows with action "delete"), so change an action there to override.

Nothing is written to the database.

Usage:
    python scripts/find_duplicate_debates.py                       # since 2026-04-27, with API check
    python scripts/find_duplicate_debates.py --since 2026-10-15 --until 2026-10-15 --csv dupes.csv
    python scripts/find_duplicate_debates.py --no-api

Exit codes: 0 done, 2 bad arguments, 3 stopped on API errors (the CSV covers
the days finished).
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_SINCE = "2026-04-27"
MIN_PACE = 1.0
_MAX_CONSECUTIVE_UNKNOWN = 3     # CLAUDE.md three-strike rule
_IDS_PER_COMMAND = 40

CSV_FIELDS = ("finding", "action", "group", "date", "house", "session_id", "ext_id", "id_format",
              "api_status", "title", "slug", "debate_type", "named", "hansard_named",
              "kept_session_id", "kept_ext_id", "coverage", "note")


class Checker:
    """Paced Hansard look-ups, cached by ext_id; stops on 429/401/403 or three unknowns in a row."""

    def __init__(self, pace: float, sleep=time.sleep, clock=time.monotonic):
        self.pace, self.sleep, self.clock = max(MIN_PACE, pace), sleep, clock
        self.cache: dict[str, tuple] = {}
        self.calls = 0
        self.unknown_run = 0
        self._last = None

    def __call__(self, ext_id: str) -> tuple:
        from hansard_archive.dedupe import UNKNOWN, ApiStop, check_ext_id
        if ext_id in self.cache:
            return self.cache[ext_id]
        if self._last is not None:
            wait = self.pace - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        try:
            result = check_ext_id(ext_id)
        finally:
            self._last = self.clock()
            self.calls += 1
        self.cache[ext_id] = result
        if result[0] == UNKNOWN:
            self.unknown_run += 1
            if self.unknown_run >= _MAX_CONSECUTIVE_UNKNOWN:
                raise ApiStop(f"{self.unknown_run} failed look-ups in a row (last {ext_id}: "
                              f"{result[2]}) — check the Hansard API before re-running")
        else:
            self.unknown_run = 0
        return result


def sitting_days(since: date, until: date | None, house: str | None) -> list[tuple[date, str]]:
    from extensions import db
    from hansard_archive.models import HansardSession
    q = (db.session.query(HansardSession.date, HansardSession.house)
         .filter(HansardSession.date >= since, HansardSession.is_container.is_(False)))
    if until:
        q = q.filter(HansardSession.date <= until)
    if house:
        q = q.filter(HansardSession.house == house)
    return sorted({(d, h) for d, h in q.distinct().all()})


def stored_full(session_id: int) -> list[dict]:
    from hansard_archive.refresh import _FIELDS, stored_rows
    return [dict({f: getattr(r, f) for f in _FIELDS}, id=r.id) for r in stored_rows(session_id)]


def analyse_day(day: date, house: str, checker, provisional: bool) -> list:
    from hansard_archive import dedupe as dd
    from hansard_archive.ingestion import _flatten_items

    sessions = dd.load_day(day, house)
    an = dd.analyse(sessions)
    to_check = an.involved()
    if provisional:
        to_check |= {s.id for s in sessions if not dd.is_final_id(s.ext_id)}
    status: dict[int, str] = {}
    hansard: dict[int, list] = {}
    for sid in sorted(to_check):
        if checker is None:
            status[sid] = dd.UNKNOWN
            continue
        st, data, _reason = checker(an.sessions[sid].ext_id)
        status[sid] = st
        if data is not None:
            hansard[sid] = _flatten_items(data, [0])
    findings = dd.recommend(an, status, hansard, stored_full)
    from extensions import db
    db.session.rollback()     # read-only: end the transaction for the day
    return findings


def csv_row(f) -> dict:
    from hansard_archive.dedupe import is_final_id
    s, k = f.session, f.kept
    return {
        "finding": f.finding, "action": f.action, "group": f.group,
        "date": s.date.isoformat(), "house": s.house, "session_id": s.id, "ext_id": s.ext_id,
        "id_format": "final" if is_final_id(s.ext_id) else "provisional",
        "api_status": f.status, "title": s.title, "slug": s.slug or "",
        "debate_type": s.debate_type or "", "named": s.named,
        "hansard_named": "" if f.hansard_named is None else f.hansard_named,
        "kept_session_id": k.id if k else "", "kept_ext_id": k.ext_id if k else "",
        "coverage": "" if f.coverage is None else f"{f.coverage:.2f}",
        "note": " | ".join(f.notes),
    }


def run(args, sleep=time.sleep, clock=time.monotonic) -> dict:
    from hansard_archive.dedupe import ACTION_ACCEPT_SHRINK, ACTION_DELETE, ApiStop

    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else None
    days = sitting_days(since, until, args.house)
    checker = None if args.no_api else Checker(args.pace, sleep=sleep, clock=clock)
    print(f"[dupes] {len(days)} sitting day/House pairs from {since}"
          f"{f' to {until}' if until else ''} "
          f"({'no API check' if args.no_api else f'API check, {checker.pace:.1f}s apart'}) — "
          f"read-only", flush=True)

    rows: list[dict] = []
    stopped = None
    for n, (day, house) in enumerate(days, 1):
        try:
            findings = analyse_day(day, house, checker, args.check_provisional)
        except ApiStop as exc:
            stopped = f"{exc} (stopped at {day} {house}; the CSV covers the days before it)"
            break
        rows.extend(csv_row(f) for f in findings)
        if findings:
            print(f"[dupes] {day} {house}: " + ", ".join(
                f"{a}={c}" for a, c in sorted(collections.Counter(f.action for f in findings).items())),
                flush=True)
        if n % 20 == 0:
            print(f"[dupes] {n}/{len(days)} days checked"
                  + (f", {checker.calls} API look-ups" if checker else ""), flush=True)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        print(f"[dupes] CSV written: {args.csv} ({len(rows)} rows)", flush=True)
    print("----- CSV START -----")
    print(buf.getvalue(), end="")
    print("----- CSV END -----", flush=True)

    by_action = collections.Counter(r["action"] for r in rows)
    by_finding = collections.Counter(r["finding"] for r in rows)
    print("\n[dupes] Sessions by finding:")
    for k, c in sorted(by_finding.items()):
        print(f"  {c:6d}  {k}")
    print("[dupes] Recommended actions:")
    for k, c in sorted(by_action.items()):
        print(f"  {c:6d}  {k}")
    if checker:
        print(f"[dupes] Hansard API look-ups: {checker.calls}")

    deletes = [r for r in rows if r["action"] == ACTION_DELETE]
    if deletes:
        print(f"\n[dupes] {len(deletes)} copies to delete (after review; dry run first, then "
              f"add --execute):")
        pairs = [f"{r['session_id']}:{r['kept_session_id']}" for r in deletes]
        for i in range(0, len(pairs), _IDS_PER_COMMAND):
            print(f"  python scripts/delete_duplicate_debates.py --session-id "
                  f"{','.join(pairs[i:i + _IDS_PER_COMMAND])}")
        if args.csv:
            print(f"  (or: python scripts/delete_duplicate_debates.py --csv {args.csv})")
    shrinks = [r for r in rows if r["action"] == ACTION_ACCEPT_SHRINK]
    if shrinks:
        print(f"\n[dupes] {len(shrinks)} swallowing session(s) to bring in line with Hansard "
              f"AFTER the deletes (dry run first, then add --execute):")
        for i in range(0, len(shrinks), _IDS_PER_COMMAND):
            ids = " ".join(f"--ext-id {r['ext_id']}" for r in shrinks[i:i + _IDS_PER_COMMAND])
            print(f"  python scripts/refresh_partial_debates.py --accept-shrink {ids}")
    reviews = [r for r in rows if r["action"] == "review"]
    if reviews:
        print(f"\n[dupes] {len(reviews)} session(s) to review by hand (action 'review' in the CSV).")
    if stopped:
        print(f"\n[dupes] STOPPED: {stopped}")
    return {"rows": rows, "stopped": stopped, "exit_code": 3 if stopped else 0,
            "api_calls": checker.calls if checker else 0}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--since", default=DEFAULT_SINCE, help=f"YYYY-MM-DD (default {DEFAULT_SINCE})")
    p.add_argument("--until", help="YYYY-MM-DD, inclusive (default: today)")
    p.add_argument("--house", choices=("Commons", "Lords"))
    p.add_argument("--csv", help="also write the CSV to this file")
    p.add_argument("--no-api", action="store_true", help="skip the Hansard live/dead check")
    p.add_argument("--check-provisional", action="store_true",
                   help="also look up every session with a provisional id (finds dead ids with "
                        "no twin stored)")
    p.add_argument("--pace", type=float, default=MIN_PACE,
                   help=f"seconds between API calls (at least {MIN_PACE})")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    try:
        for d in (args.since, args.until):
            if d:
                date.fromisoformat(d)
    except ValueError as exc:
        print(f"find_duplicate_debates.py: bad date: {exc}", file=sys.stderr)
        return 2
    if args.until and args.until < args.since:
        print("find_duplicate_debates.py: --until is before --since", file=sys.stderr)
        return 2

    from flask_app import app
    with app.app_context():
        result = run(args)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())

