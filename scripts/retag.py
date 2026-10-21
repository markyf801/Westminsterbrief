"""
Re-tag existing debate sessions and/or Written Questions with the current tagger
(evidence-checked tags, max 3 policy areas, canonical topics).

DRY RUN BY DEFAULT. A dry run counts the selected rows, prints a cost estimate,
then tags a small sample (--sample, default 20) and prints each item's tags
before and after. It writes nothing to the database.

--apply re-tags every selected row, replacing its tags. Safety:
  - --apply needs --backup or --no-backup. --backup first copies the whole
    theme table(s) being changed into snapshot tables
    (ha_session_theme_bak_YYYYMMDD_HHMMSS / ha_pq_theme_bak_...) with
    CREATE TABLE ... AS SELECT; restoring is a manual SQL step (Mark's call).
  - An item is only rewritten when the new tags include at least one verified
    policy area; otherwise it keeps its old tags (reported as kept_old).
  - Progress is checkpointed in ha_cron_run (service_name "retag-<kind>-<hash
    of the arguments>", notes = JSON with the last id done) after every chunk,
    so a stopped Railway one-off resumes when started again with the same
    arguments. Nothing is kept on the container's disk.
  - It pauses (does not write) during the scheduled cron windows listed in
    CRON_WINDOWS_UTC, unless --ignore-cron-windows.
  - It stops with exit code 3 after --max-failures consecutive API failures
    (rate limit, outage), or at once on a final API error (bad request, key,
    model), so the one-off fails visibly instead of skipping rows.

Selection (default: everything the crons would tag):
    --what sessions|pqs|both      (default both)
    --since / --until YYYY-MM-DD   session date / PQ tabled date
    --type oral_questions,pmqs     session debate types (sessions only)
    --house Commons|Lords
    --include-other                also 'other' sessions (procedural; crons skip them)
    --ids 1,2,3                    explicit row ids (with --what sessions or pqs)
    --limit N

Examples (Railway one-off; dry run first, then a small apply, then everything):
    python scripts/retag.py --what sessions --since 2026-10-01
    python scripts/retag.py --what pqs --limit 500 --apply --backup --model gemini-2.5-flash
    python scripts/retag.py --apply --backup --model gemini-2.5-flash

Exit codes: 0 ok, 2 no GEMINI_API_KEY / bad arguments, 3 stopped after API failures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CHUNK = 50

# Rough token model for the up-front estimate (chars/4 plus the fixed prompt).
_EST_OUTPUT_TOKENS = {"session": 450, "pq": 120}
_EST_THINKING_TOKENS = {"gemini-2.5-flash-lite": 0, "gemini-2.5-flash": 700, "gemini-2.5-pro": 1500}

# Scheduled cron windows (UTC) from CLAUDE.md "Branches and workflow":
# (weekdays or None for every day, start "HH:MM", end "HH:MM"). The hourly
# archive daytime runs (first ~5 minutes of 11:00-23:00 Mon-Thu) are listed
# hour by hour.
CRON_WINDOWS_UTC: list[tuple[set[int] | None, str, str]] = [
    (None, "03:00", "03:15"),                    # backup-cron-r2
    (None, "04:30", "04:45"),                    # edm-cron
    (None, "06:30", "07:15"),                    # bills-cron
    ({0, 1, 2, 3, 4}, "08:30", "09:00"),         # PQ crons
    ({0, 1, 2, 3, 4}, "09:30", "10:00"),
    ({0, 1, 2, 3, 4}, "14:00", "14:30"),
] + [({0, 1, 2, 3}, f"{h:02d}:00", f"{h:02d}:06") for h in range(11, 24)]


class TooManyFailures(Exception):
    pass


def cron_window(now: datetime) -> str | None:
    """'HH:MM-HH:MM' when `now` (UTC) falls in a scheduled cron window, else None."""
    hm = now.strftime("%H:%M")
    for days, start, end in CRON_WINDOWS_UTC:
        if (days is None or now.weekday() in days) and start <= hm < end:
            return f"{start}-{end}"
    return None


def wait_outside_cron_windows(args, sleep=time.sleep, clock=datetime.utcnow) -> None:
    if args.ignore_cron_windows:
        return
    window = cron_window(clock())
    while window:
        print(f"[retag] in cron window {window} UTC — pausing (no writes)", flush=True)
        sleep(60)
        window = cron_window(clock())


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_ids(kind: str, args) -> list[int]:
    from extensions import db
    from hansard_archive.models import HansardSession, HaPQ

    if kind == "sessions":
        q = db.session.query(HansardSession.id).filter(HansardSession.is_container.is_(False))
        if not args.include_other:
            q = q.filter(HansardSession.debate_type != "other")
        if args.type:
            q = q.filter(HansardSession.debate_type.in_(args.type.split(",")))
        if args.house:
            q = q.filter(HansardSession.house == args.house)
        if args.since:
            q = q.filter(HansardSession.date >= date.fromisoformat(args.since))
        if args.until:
            q = q.filter(HansardSession.date <= date.fromisoformat(args.until))
        col = HansardSession.id
    else:
        q = db.session.query(HaPQ.id).filter(HaPQ.question_text.isnot(None))
        if args.house:
            q = q.filter(HaPQ.chamber == args.house)
        if args.since:
            q = q.filter(HaPQ.tabled_date >= date.fromisoformat(args.since))
        if args.until:
            q = q.filter(HaPQ.tabled_date <= date.fromisoformat(args.until))
        col = HaPQ.id
    if args.ids:
        q = q.filter(col.in_([int(x) for x in args.ids.split(",") if x.strip()]))
    ids = [r[0] for r in q.order_by(col).all()]
    return ids[: args.limit] if args.limit else ids


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------

def estimate_cost(kind: str, n: int, avg_input_chars: float, model: str) -> tuple[float | None, int, int]:
    """(USD or None, input tokens, output tokens) for tagging n items of this kind."""
    from hansard_archive import tagger as tg
    fixed = len(tg._SCOPE_BLOCK) + 1800                 # rules + scope notes + framing
    if kind == "pq":
        per_item_in = (fixed / tg._PQ_BATCH_SIZE + avg_input_chars) / 4
    else:
        per_item_in = (fixed + avg_input_chars) / 4
    thinking = next((v for k, v in sorted(_EST_THINKING_TOKENS.items(), key=lambda kv: -len(kv[0]))
                     if model.startswith(k)), 0)
    per_item_out = _EST_OUTPUT_TOKENS["pq" if kind == "pq" else "session"] + (
        thinking / tg._PQ_BATCH_SIZE if kind == "pq" else thinking)
    tin, tout = int(per_item_in * n), int(per_item_out * n)
    prices = tg.model_prices(model)
    if prices is None:
        return None, tin, tout
    return (tin * prices[0] + tout * prices[1]) / 1_000_000, tin, tout


def _avg_input_chars(kind: str, ids: list[int]) -> float:
    """Average characters the tagger would send per item, from up to 200 rows."""
    from sqlalchemy import func
    from extensions import db
    from hansard_archive import tagger as tg
    from hansard_archive.models import HansardContribution, HaPQ

    sample = ids[:: max(1, len(ids) // 200)][:200]
    if not sample:
        return 0.0
    if kind == "pqs":
        rows = (db.session.query(func.length(HaPQ.question_text), func.length(HaPQ.heading))
                .filter(HaPQ.id.in_(sample)).all())
        return sum(min(q or 0, tg._PQ_BATCH_QUESTION_CHARS) + (h or 0) + 80 for q, h in rows) / len(rows)
    rows = (db.session.query(func.sum(func.length(HansardContribution.speech_text)))
            .filter(HansardContribution.session_id.in_(sample))
            .group_by(HansardContribution.session_id).all())
    return sum(min(r[0] or 0, tg._SESSION_TEXT_CHARS) for r in rows) / max(1, len(sample))


# ---------------------------------------------------------------------------
# Checkpoint in ha_cron_run (survives one-off containers)
# ---------------------------------------------------------------------------

def checkpoint_service(kind: str, args) -> str:
    key = json.dumps({k: getattr(args, k) for k in ("since", "until", "type", "house", "ids",
                                                    "include_other", "limit", "model")},
                     sort_keys=True)
    return f"retag-{kind}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def load_checkpoint(service: str) -> dict:
    """Last checkpoint for this exact selection, from the newest ha_cron_run row."""
    from hansard_archive.models import HaCronRun
    row = (HaCronRun.query.filter_by(service_name=service)
           .order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first())
    if row is None or not row.notes:
        return {}
    try:
        return json.loads(row.notes)
    except ValueError:
        return {}


def start_checkpoint(service: str, state: dict):
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
    row.sessions_tagged = state.get("retagged", 0)
    row.errors = state.get("failed", 0)
    if status != "running":
        row.finished_at = datetime.utcnow()
    db.session.commit()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

_THEME_TABLE = {"sessions": "ha_session_theme", "pqs": "ha_pq_theme"}


def backup_table(kind: str, stamp: str | None = None) -> str:
    """Copy the whole theme table to <table>_bak_<stamp> (CREATE TABLE AS SELECT). Returns its name."""
    from sqlalchemy import text
    from extensions import db
    src = _THEME_TABLE[kind]
    from sqlalchemy import inspect
    base = f"{src}_bak_{stamp or datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    existing = set(inspect(db.engine).get_table_names())
    name, n = base, 1
    while name in existing:
        n += 1
        name = f"{base}_{n}"
    db.session.execute(text(f"CREATE TABLE {name} AS SELECT * FROM {src}"))
    n = db.session.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
    db.session.commit()
    print(f"[retag] backup: copied {n:,} rows of {src} to {name}", flush=True)
    return name


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

def _current_tags(kind: str, item_id: int) -> tuple[list[str], list[str]]:
    from hansard_archive.models import (THEME_TYPE_POLICY_AREA, HansardSessionTheme, HaPQTheme)
    if kind == "sessions":
        rows = (HansardSessionTheme.query.filter_by(session_id=item_id)
                .order_by(HansardSessionTheme.id).all())
    else:
        rows = HaPQTheme.query.filter_by(pq_id=item_id).all()
    areas = [r.theme for r in rows if r.theme_type == THEME_TYPE_POLICY_AREA]
    topics = [r.theme for r in rows if r.theme_type != THEME_TYPE_POLICY_AREA]
    return areas, topics


def tag_chunk(kind: str, ids: list[int], api_key: str, model: str, batch: int, failures: dict,
              max_failures: int) -> list[tuple[int, str, object]]:
    """
    Tag a chunk without writing. Returns [(id, label, TagOutcome | None)].
    Counts consecutive API failures in `failures['run']`; raises TooManyFailures
    (also at once on a final API error).
    """
    from extensions import db
    from hansard_archive import tagger as tg
    from hansard_archive.models import HansardSession, HaPQ

    out: list[tuple[int, str, object]] = []
    if kind == "sessions":
        for sid in ids:
            s = db.session.get(HansardSession, sid)
            if s is None:
                continue
            si = tg.session_input_from_db(s)
            outcome = None
            if any(t.strip() for _, t in si.contributions):
                try:
                    outcome = tg.tag_session_input(si, api_key, model)
                    failures["run"] = 0
                except tg.GeminiCallFailed as e:
                    failures["run"] += 1
                    failures["total"] += 1
                    print(f"[retag] session {sid}: API failure ({e})", flush=True)
                    if e.final or failures["run"] >= max_failures:
                        raise TooManyFailures(str(e))
            out.append((sid, f"{s.date} {s.house} {s.debate_type} {(s.title or '')[:60]!r}", outcome))
            time.sleep(tg._INTER_REQUEST_DELAY)
        return out

    pqs = [p for p in (db.session.get(HaPQ, i) for i in ids) if p is not None]
    try:
        outcomes = tg.tag_pq_inputs([tg.pq_input_from_db(p) for p in pqs], api_key, model, batch_size=batch)
    except tg.GeminiCallFailed as e:
        raise TooManyFailures(str(e))
    for p, outcome in zip(pqs, outcomes):
        if outcome is None:
            failures["run"] += 1
            failures["total"] += 1
            if failures["run"] >= max_failures:
                raise TooManyFailures(f"PQ {p.id}: no result after retries")
        else:
            failures["run"] = 0
        out.append((p.id, f"{p.tabled_date} {p.uin} {(p.heading or p.question_text or '')[:60]!r}", outcome))
    return out


def _print_before_after(kind: str, item_id: int, label: str, outcome) -> None:
    areas, topics = _current_tags(kind, item_id)
    print(f"\n  {kind[:-1]} {item_id}  {label}")
    print(f"     before: {areas} | {topics}")
    if outcome is None:
        print("     after : (API failure — unchanged)")
        return
    note = "" if outcome.ok else "   (no verified policy area: old tags kept)"
    print(f"     after : {outcome.policy_areas} | {outcome.topics}{note}")
    dropped = [f"{k}:{v} ({why})" for k, v, why in outcome.dropped]
    if dropped:
        print(f"     dropped: {'; '.join(dropped[:6])}")


def run(kind: str, args, api_key: str) -> dict:
    from extensions import db
    from hansard_archive import tagger as tg

    model = tg.resolve_model(api_key, args.model)
    ids = select_ids(kind, args)
    avg = _avg_input_chars(kind, ids)
    cost, tin, tout = estimate_cost("pq" if kind == "pqs" else "session", len(ids), avg, model)
    db.session.rollback()
    print(f"\n[retag] {kind}: {len(ids):,} selected | model {model} | "
          f"~{tin:,} input + ~{tout:,} output tokens | estimated cost "
          f"{'$%.2f' % cost if cost is not None else 'unknown (no price for this model)'}", flush=True)

    failures = {"run": 0, "total": 0}
    stats = {"selected": len(ids), "retagged": 0, "kept_old": 0, "failed": 0, "skipped_done": 0}

    if not args.apply:
        sample = ids[:: max(1, len(ids) // max(1, args.sample))][: args.sample]
        print(f"[retag] DRY RUN — tagging a sample of {len(sample)} and writing nothing", flush=True)
        for item_id, label, outcome in tag_chunk(kind, sample, api_key, model, args.batch, failures,
                                                 args.max_failures):
            _print_before_after(kind, item_id, label, outcome)
        db.session.rollback()
        return stats

    service = checkpoint_service(kind, args)
    state = load_checkpoint(service)
    done_upto = state.get("last_id", 0)
    todo = [i for i in ids if i > done_upto]
    stats["skipped_done"] = len(ids) - len(todo)
    if done_upto:
        print(f"[retag] resuming after id {done_upto} ({stats['skipped_done']:,} already done; "
              f"checkpoint ha_cron_run service_name={service})", flush=True)
    if not todo:
        return stats
    if args.backup:
        stats["backup_table"] = backup_table(kind)
    run_id = start_checkpoint(service, {"last_id": done_upto, **stats})

    write = tg.write_session_tags if kind == "sessions" else tg.write_pq_tags
    shown = 0
    try:
        for start in range(0, len(todo), _CHUNK):
            chunk = todo[start:start + _CHUNK]
            results = tag_chunk(kind, chunk, api_key, model, args.batch, failures, args.max_failures)
            wait_outside_cron_windows(args)
            for item_id, label, outcome in results:
                if shown < args.sample:
                    _print_before_after(kind, item_id, label, outcome)
                    shown += 1
                if outcome is None:
                    stats["failed"] += 1
                elif not outcome.ok:
                    stats["kept_old"] += 1
                else:
                    write(item_id, outcome, replace=True)
                    stats["retagged"] += 1
            db.session.commit()
            save_checkpoint(run_id, {"last_id": chunk[-1], **stats})
            print(f"[retag] {kind}: {stats['skipped_done'] + start + len(chunk):,}/{len(ids):,} — "
                  f"retagged={stats['retagged']} kept_old={stats['kept_old']} failed={stats['failed']}",
                  flush=True)
    except TooManyFailures:
        db.session.rollback()
        save_checkpoint(run_id, {**load_checkpoint(service), "stopped": "API failures"}, status="failed")
        raise
    save_checkpoint(run_id, {"last_id": todo[-1], **stats}, status="ok")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Re-tag sessions / Written Questions (dry run by default).")
    ap.add_argument("--what", choices=("sessions", "pqs", "both"), default="both")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--type", help="comma-separated session debate types")
    ap.add_argument("--house", choices=("Commons", "Lords"))
    ap.add_argument("--ids", help="comma-separated row ids")
    ap.add_argument("--include-other", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model", help="Gemini model id (default: TAGGER_MODEL or auto-detected Flash-Lite)")
    ap.add_argument("--batch", type=int, default=5, help="Written Questions per API call")
    ap.add_argument("--sample", type=int, default=20, help="items shown before/after")
    ap.add_argument("--apply", action="store_true", help="write the new tags (default: dry run)")
    ap.add_argument("--backup", action="store_true", help="snapshot the theme table(s) before --apply")
    ap.add_argument("--no-backup", action="store_true", help="--apply without a snapshot (you made one)")
    ap.add_argument("--ignore-cron-windows", action="store_true")
    ap.add_argument("--max-failures", type=int, default=5, help="stop after this many API failures in a row")
    args = ap.parse_args(argv)

    if args.apply and args.backup == args.no_backup:
        print("[retag] --apply needs exactly one of --backup or --no-backup")
        return 2
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[retag] GEMINI_API_KEY is not set")
        return 2

    from flask_app import app
    kinds = ("sessions", "pqs") if args.what == "both" else (args.what,)
    with app.app_context():
        try:
            for kind in kinds:
                stats = run(kind, args, api_key)
                print(f"[retag] {kind} done: {stats}", flush=True)
        except TooManyFailures as e:
            print(f"[retag] STOPPED after API failures ({e}). "
                  "Progress is saved; run the same command again to resume.", flush=True)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
