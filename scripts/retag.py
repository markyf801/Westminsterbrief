"""
Re-tag existing debate sessions and/or Written Questions with the current tagger
(evidence-checked tags, max 3 policy areas, canonical topics).

DRY RUN BY DEFAULT. A dry run counts the selected rows, prints a cost estimate,
then tags a small sample (--sample, default 20) and prints each item's tags
before and after. It writes nothing to the database.

--apply re-tags every selected row, replacing its tags. Safety:
  - --apply needs --backup or --no-backup. --backup first copies the whole
    theme table(s) being changed into snapshot tables (once per checkpoint:
    a resumed run reuses the snapshot recorded in its checkpoint)
    (ha_session_theme_bak_YYYYMMDD_HHMMSS / ha_pq_theme_bak_...) with
    CREATE TABLE ... AS SELECT; restoring is a manual SQL step (Mark's call):
    the run prints the undo SQL at the end.
  - An item is only rewritten when the new tags include at least one verified
    policy area; otherwise it keeps its old tags (reported as kept_old).
  - Progress is checkpointed in ha_cron_run (service_name "retag-<kind>-<hash
    of the arguments>", notes = JSON with the last id done) after every chunk,
    so a stopped Railway one-off resumes when started again with the same
    arguments. Nothing is kept on the container's disk.
  - It never writes in or just before a production cron window
    (hansard_archive/cron_windows.py, which blocks weekday daytime whole):
    it refuses to start, or stops after the current chunk, with exit code 4
    and the checkpoint saved, so the same command resumes later. Override
    with --ignore-cron-windows.
  - It stops with exit code 3 after --max-failures consecutive API failures
    (rate limit, outage), or at once on a final API error (bad request, key,
    model), so the one-off fails visibly instead of skipping rows.
  - A Claude model must be in tagger.LIVE_CLAUDE_MODELS to --apply.

Model: --model, else TAGGER_MODEL, else auto-detected Gemini Flash-Lite — the
same resolution as the crons (tagger.resolve_backend). Claude needs
ANTHROPIC_API_KEY, Gemini GEMINI_API_KEY.

--batch (Claude only): the Message Batches API, at half the price. Same
selection flags, same requests as live tagging (one session, or 5 PQs, per
request), and every answer goes through the same verification, PQ UIN check
and write path (write_*_tags with replace) as live tagging. PQs whose UIN
didn't line up, that verified nothing, or whose group request failed are sent
again on their own in a second round, as live tagging retries them.
  - Without --apply: prints counts and the estimated cost and makes NO API
    call — unless --sample N is given, which submits a batch of N items only,
    waits for it and prints before/after (writes no tags).
  - Resumable: every submitted batch id is saved in the ha_cron_run
    checkpoint as soon as it is created (service "retag-<kind>-batch-<hash>",
    or "-bsample-" for a sample), so a restarted run polls the batches it
    already paid for instead of submitting again. The same command resumes.
  - Results are written as each batch ends, outside cron windows only (exit 4
    otherwise; batches keep processing on Anthropic's side and are applied on
    the next run). --backup snapshots the table just before the first write.
  - Errored / expired requests, items deleted meanwhile, and items whose text
    changed after their request was built keep their old tags and are listed
    (ids also in the checkpoint) with a --ids command to re-run them.
  - Exit code 5: batches still processing after --max-wait-hours (default
    25; every batch ends within 24 h). Run the same command again later.
  Message Batches API limits (Anthropic docs, Oct 2026): at most 100,000
  requests or 256 MB per batch (this script submits at most
  _BATCH_MAX_REQUESTS requests / _BATCH_MAX_BYTES per batch); most batches end
  within an hour, every batch within 24 hours (unfinished requests then
  expire); results stay downloadable for 29 days and come back in any order
  (matched here by custom_id). The organisation's batch queue has a size limit
  per usage tier (Console > Settings > Limits): a submit refused --max-failures
  times in a row (e.g. 429, queue full) stops the run (exit 3) with what was
  submitted saved; the same command resumes. Submitting and polling may happen
  in a cron window (no tag writes); applying results waits for a free window.

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
  Claude re-tag (docs/ops/railway-services.md "Re-tag with Claude Haiku"):
    python scripts/retag.py --batch --model claude-haiku-5-5
    python scripts/retag.py --batch --model claude-haiku-5-5 --sample 50
    python scripts/retag.py --batch --model claude-haiku-5-5 --apply --backup

Exit codes: 0 ok, 1 unexpected error (traceback, e.g. a database error; the
checkpoint holds what was done, so the same command resumes), 2 no API key /
bad arguments, 3 stopped after API failures, 4 stopped for a cron window
(resume later with the same command), 5 --batch: batches still processing
(resume later with the same command).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CHUNK = 50

# Rough token model for the up-front estimate (chars/4 plus the fixed prompt).
_EST_OUTPUT_TOKENS = {"session": 450, "pq": 120}
# Thinking tokens per call. claude-haiku-5-5 at effort medium: calibrated on the
# 24 Oct 2026 eval ($1.30 per 1,000 debates, $0.28 per 1,000 questions, before the batch discount).
_EST_THINKING_TOKENS = {"gemini-2.5-flash-lite": 0, "gemini-2.5-flash": 700, "gemini-2.5-pro": 1500,
                        "claude-haiku-5-5": 1300}

# Message Batches: well under the API's 100,000 requests / 256 MB per batch, so
# batches end sooner and a stop loses less.
_BATCH_MAX_REQUESTS = 5_000
_BATCH_MAX_BYTES = 100 * 1024 * 1024
_SUBMIT_TIMEOUT = 600.0           # seconds to upload one batch
_BATCH_BUILD_SLICE = 500          # items read from the DB at a time while building requests
_APPLY_WINDOW_EVERY = 200         # results applied between cron-window checks
_ERRORED_KEPT = 2_000             # errored ids kept in the checkpoint (all are counted)


class TooManyFailures(Exception):
    pass


class WindowStop(Exception):
    """A production cron window is due: stop cleanly (checkpoint saved) before writing more."""


class StillProcessing(Exception):
    """--batch: batches are still processing at --max-wait-hours (checkpoint saved)."""


def check_cron_window(args, clock=datetime.utcnow) -> None:
    """Raise WindowStop when now, or the next LOOKAHEAD_MINUTES, is in a cron window."""
    from hansard_archive.cron_windows import blocked_window
    if args.ignore_cron_windows:
        return
    label = blocked_window(clock())
    if label:
        raise WindowStop(label)


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


def _sample_of(ids: list[int], n: int) -> list[int]:
    return ids[:: max(1, len(ids) // max(1, n))][:n]


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


def print_estimate(kind: str, ids: list[int], model: str, batch: bool) -> None:
    from hansard_archive import tagger as tg
    avg = _avg_input_chars(kind, ids)
    cost, tin, tout = estimate_cost("pq" if kind == "pqs" else "session", len(ids), avg, model)
    if cost is not None and batch:
        cost *= tg.BATCH_PRICE_FACTOR
    price = ("unknown (no price for this model)" if cost is None
             else f"${cost:.2f}" + (" at Batch API prices (50% off; PQ single retries add a few %)"
                                    if batch else ""))
    print(f"\n[retag] {kind}: {len(ids):,} selected | model {model} | "
          f"~{tin:,} input + ~{tout:,} output tokens | estimated cost {price}", flush=True)


# ---------------------------------------------------------------------------
# Checkpoint in ha_cron_run (survives one-off containers)
# ---------------------------------------------------------------------------

def checkpoint_service(kind: str, args, model: str | None = None, mode: str = "") -> str:
    """
    Checkpoint name for this exact selection. A Claude model resolved from
    TAGGER_MODEL is part of the key, so a Claude run never resumes a Gemini
    run's checkpoint (and skips rows Gemini re-tagged). mode: "" (live calls),
    "batch" or "bsample".
    """
    from hansard_archive import tagger as tg
    fields = {k: getattr(args, k) for k in ("since", "until", "type", "house", "ids",
                                            "include_other", "limit", "model")}
    if not args.model and model and tg.is_claude_model(model):
        fields["model"] = model
    if mode == "bsample":
        fields["sample"] = args.sample
    if mode:
        fields["pq_batch"] = args.pq_batch
    key = json.dumps(fields, sort_keys=True)
    return f"retag-{kind}-{mode + '-' if mode else ''}{hashlib.sha1(key.encode()).hexdigest()[:10]}"


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


def undo_sql(kind: str, backup: str, model: str, since: str) -> str:
    """
    SQL (for Mark, by hand) that puts back the backed-up tags of every item this
    run re-tagged: items in the backup whose tags are now from `model`, written
    at or after `since` (UTC). Items untagged before the run keep their new tags.
    It also reverts any of those the crons re-tagged with the same model since.
    """
    table, col = ("ha_session_theme", "session_id") if kind == "sessions" else ("ha_pq_theme", "pq_id")
    order = " ORDER BY b.id" if kind == "sessions" else ""     # session tags: centrality = id order
    cols = f"{col}, theme, theme_type, tagged_at, model_used" + (", confidence" if kind == "sessions" else "")
    b_cols = ", ".join(f"b.{c.strip()}" for c in cols.split(","))
    tmp = f"retag_undo_{kind}"          # one per kind: both undos can run in one DBeaver session
    model_q = model.replace("'", "''")
    return (
        f"-- Undo re-tag ({kind}): restore tags from {backup}. Run in one transaction; check the counts.\n"
        "BEGIN;\n"
        f"CREATE TEMP TABLE {tmp} AS SELECT DISTINCT t.{col} AS item_id FROM {table} t\n"
        f"  WHERE t.model_used = '{model_q}' AND t.tagged_at >= '{since}'\n"
        f"    AND t.{col} IN (SELECT {col} FROM {backup});\n"
        f"SELECT COUNT(*) AS items_to_restore FROM {tmp};\n"
        f"DELETE FROM {table} WHERE {col} IN (SELECT item_id FROM {tmp});\n"
        f"INSERT INTO {table} ({cols})\n"
        f"  SELECT {b_cols} FROM {backup} b\n"
        f"  WHERE b.{col} IN (SELECT item_id FROM {tmp}){order};\n"
        "COMMIT;"
    )


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
            out.append((sid, _label("sessions", s), outcome))
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
        out.append((p.id, _label("pqs", p), outcome))
    return out


def _label(kind: str, row) -> str:
    if kind == "sessions":
        return f"{row.date} {row.house} {row.debate_type} {(row.title or '')[:60]!r}"
    return f"{row.tabled_date} {row.uin} {(row.heading or row.question_text or '')[:60]!r}"


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


def run(kind: str, args, api_key: str, model: str | None = None) -> dict:
    from extensions import db
    from hansard_archive import tagger as tg

    model = model or tg.resolve_model(api_key, args.model)
    ids = select_ids(kind, args)
    print_estimate(kind, ids, model, batch=False)
    db.session.rollback()

    failures = {"run": 0, "total": 0}
    stats = {"selected": len(ids), "retagged": 0, "kept_old": 0, "failed": 0, "skipped_done": 0}

    if not args.apply:
        sample = _sample_of(ids, args.sample)
        print(f"[retag] DRY RUN — tagging a sample of {len(sample)} and writing nothing", flush=True)
        for item_id, label, outcome in tag_chunk(kind, sample, api_key, model, args.pq_batch, failures,
                                                 args.max_failures):
            _print_before_after(kind, item_id, label, outcome)
        db.session.rollback()
        return stats

    service = checkpoint_service(kind, args, model)
    state = load_checkpoint(service)
    done_upto = state.get("last_id", 0)
    todo = [i for i in ids if i > done_upto]
    stats["skipped_done"] = len(ids) - len(todo)
    if done_upto:
        print(f"[retag] resuming after id {done_upto} ({stats['skipped_done']:,} already done; "
              f"checkpoint ha_cron_run service_name={service})", flush=True)
    if not todo:
        return stats
    check_cron_window(args)
    if args.backup:
        if state.get("backup_table"):
            stats["backup_table"] = state["backup_table"]
            print(f"[retag] backup: reusing {state['backup_table']} from the checkpoint", flush=True)
        else:
            stats["backup_table"] = backup_table(kind)
    elif state.get("backup_table"):
        stats["backup_table"] = state["backup_table"]
    stats["started_at"] = state.get("started_at") or datetime.utcnow().isoformat(sep=" ")
    run_id = start_checkpoint(service, {"last_id": done_upto, **stats})

    write = tg.write_session_tags if kind == "sessions" else tg.write_pq_tags
    shown = 0
    try:
        for start in range(0, len(todo), _CHUNK):
            chunk = todo[start:start + _CHUNK]
            check_cron_window(args)
            results = tag_chunk(kind, chunk, api_key, model, args.pq_batch, failures, args.max_failures)
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
    except WindowStop as e:
        db.session.rollback()
        save_checkpoint(run_id, {**load_checkpoint(service), "stopped": f"cron window: {e}"},
                        status="stopped")
        raise
    save_checkpoint(run_id, {"last_id": todo[-1], **stats}, status="ok")
    if stats.get("backup_table"):
        print("\n" + undo_sql(kind, stats["backup_table"], model, stats["started_at"]), flush=True)
    return stats


# ---------------------------------------------------------------------------
# --batch: Claude Message Batches API
# ---------------------------------------------------------------------------
# custom_id (API: 1-64 chars of [A-Za-z0-9_-]) names the items and the prompt:
#   s<session id>-<fp>            one session
#   p<pq id>_<pq id>_...-<fp>     up to 5 PQs in one request (main round)
#   q<pq id>-<fp>                 one PQ on its own (retry round)
# <fp> is TagRequest.fingerprint: when applying, the request is rebuilt from
# the database and must hash the same, so tags are always checked against the
# text that was sent (and an item edited meanwhile keeps its old tags).

_CUSTOM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _custom_id(prefix: str, ids: list[int], fp: str) -> str:
    cid = f"{prefix}{'_'.join(str(i) for i in ids)}-{fp}"
    if not _CUSTOM_ID_RE.fullmatch(cid):
        # 5 ids of up to 9 digits fit; refuse before anything is submitted rather than
        # have the whole batch rejected.
        raise ValueError(f"custom_id {cid!r} breaks the Batch API limit (1-64 chars of A-Za-z0-9_-)")
    return cid


def _parse_custom_id(cid: str) -> tuple[str, list[int], str]:
    body, fp = cid[1:].rsplit("-", 1)
    return cid[0], [int(x) for x in body.split("_")], fp


class _Batcher:
    """One kind (sessions or pqs) of a --batch run: submit, poll, apply. State is
    the checkpoint (saved after every change), so any stop resumes."""

    def __init__(self, kind, args, model, client, service, state, dry, sleep=time.sleep):
        self.kind, self.args, self.model, self.client = kind, args, model, client
        self.service, self.state, self.dry, self.sleep = service, state, dry, sleep
        self.run_id = None
        self.shown = 0
        self.failures = 0
        self.finished = False
        st = self.state
        if dry and st.get("batches"):
            # A sample run again: show its results again from the stored batches
            # (results stay downloadable for 29 days); nothing is resubmitted.
            for b in st["batches"]:
                b["applied"] = False
            st.update(retry_ids=[], errored=[], errored_count=0, counts={})
        st.setdefault("mode", "bsample" if dry else "batch")
        st.setdefault("model", model)
        st.setdefault("started_at", datetime.utcnow().isoformat(sep=" "))
        st.setdefault("batches", [])
        st.setdefault("submitted", {"main": 0, "retry": 0})
        st.setdefault("retry_ids", [])
        st.setdefault("errored", [])
        st.setdefault("errored_count", 0)
        st.setdefault("counts", {})
        st["retagged"] = st["counts"].get("retagged", 0)
        st["failed"] = st.get("errored_count", 0)

    # -- checkpoint --------------------------------------------------------
    def save(self, status: str = "running") -> None:
        self.state["retagged"] = self.state["counts"].get("retagged", 0)
        self.state["failed"] = self.state.get("errored_count", 0)
        if self.run_id is None:
            self.run_id = start_checkpoint(self.service, self.state)
            if status == "running":
                return
        save_checkpoint(self.run_id, self.state, status=status)

    # -- API ---------------------------------------------------------------
    def _api(self, fn, what: str):
        """Call the SDK; count failures in a row; TooManyFailures at the limit or on a final error."""
        import anthropic
        from hansard_archive import tagger as tg
        try:
            out = fn()
        except anthropic.APIError as e:
            err = tg.claude_error(e, self.model)
            self.failures += 1
            print(f"[retag] {self.kind}: {what} failed: {err}", flush=True)
            if err.final or self.failures >= self.args.max_failures:
                raise TooManyFailures(f"{what}: {err}") from e
            return None
        self.failures = 0
        return out

    # -- building requests -------------------------------------------------
    def _requests(self, round_name: str, ids: list[int]):
        """[(custom_id, params, last item id)] for these items, in id order."""
        from extensions import db
        from hansard_archive import tagger as tg
        from hansard_archive.models import HansardSession, HaPQ
        out = []
        if self.kind == "sessions":
            for sid in ids:
                s = db.session.get(HansardSession, sid)
                if s is None:
                    continue
                si = tg.session_input_from_db(s)
                if not any(t.strip() for _, t in si.contributions):
                    self.state["counts"]["no_text"] = self.state["counts"].get("no_text", 0) + 1
                    continue
                req = tg.session_request(si)
                params, _ = tg.claude_request_params(self.model, req.prompt, req.schema)
                out.append((_custom_id("s", [sid], req.fingerprint), params, sid))
            return out
        pqs = [p for p in (db.session.get(HaPQ, i) for i in ids) if p is not None]
        if round_name == "retry":
            for p in pqs:
                req = tg.pq_request(tg.pq_input_from_db(p))
                params, _ = tg.claude_request_params(self.model, req.prompt, req.schema)
                out.append((_custom_id("q", [p.id], req.fingerprint), params, p.id))
            return out
        size = self.args.pq_batch
        for start in range(0, len(pqs), size):
            group = pqs[start:start + size]
            req = tg.pq_batch_request([tg.pq_input_from_db(p) for p in group])
            params, _ = tg.claude_request_params(self.model, req.prompt, req.schema)
            out.append((_custom_id("p", [p.id for p in group], req.fingerprint), params, group[-1].id))
        return out

    def submit(self, round_name: str, ids: list[int]) -> None:
        """Submit requests for the items not yet submitted in this round (ids above the saved mark)."""
        from extensions import db
        done = self.state["submitted"][round_name]
        todo = [i for i in ids if i > done]
        if not todo:
            return
        print(f"[retag] {self.kind}: submitting {len(todo):,} item(s) ({round_name} round)", flush=True)
        pending, size = [], 0
        for start in range(0, len(todo), _BATCH_BUILD_SLICE):
            for cid, params, last_id in self._requests(round_name, todo[start:start + _BATCH_BUILD_SLICE]):
                n = len(json.dumps(params)) + len(cid) + 64
                if pending and (len(pending) >= _BATCH_MAX_REQUESTS or size + n > _BATCH_MAX_BYTES):
                    self._create(round_name, pending)
                    pending, size = [], 0
                pending.append((cid, params, last_id))
                size += n
            db.session.rollback()          # end the read transaction between slices
        if pending:
            self._create(round_name, pending)
        self.state["submitted"][round_name] = todo[-1]     # also past trailing items with no request
        self.save()

    def _submit(self, requests: list):
        """
        Create one batch. No automatic retry, and a long timeout: a POST that timed
        out may still have created the batch, and sending it again would pay twice.
        So a submit with no answer stops the run; one the API refused (429, 5xx
        with a response) created nothing and is retried by _create.
        """
        import anthropic
        client = (self.client.with_options(timeout=_SUBMIT_TIMEOUT, max_retries=0)
                  if hasattr(self.client, "with_options") else self.client)
        try:
            return client.messages.batches.create(requests=requests)
        except anthropic.APIConnectionError as e:     # includes timeouts
            raise TooManyFailures(
                f"no answer while submitting {len(requests)} requests ({e}); a batch may have been "
                "created anyway. Before re-running, check Console > Batches for a batch created just "
                "now that is not in this run's log, and cancel it") from e

    def _create(self, round_name: str, pending: list) -> None:
        requests = [{"custom_id": cid, "params": params} for cid, params, _ in pending]
        while True:
            batch = self._api(lambda: self._submit(requests), f"submitting {len(requests)} requests")
            if batch is not None:
                break
            self.sleep(min(self.args.poll_seconds, 5 * 2 ** self.failures))
        self.state["batches"].append({"id": batch.id, "round": round_name, "requests": len(requests),
                                      "status": getattr(batch, "processing_status", "in_progress"),
                                      "applied": False})
        self.state["submitted"][round_name] = pending[-1][2]
        self.save()
        print(f"[retag] {self.kind}: submitted batch {batch.id} ({len(requests):,} requests, "
              f"{round_name} round)", flush=True)

    # -- polling and applying ----------------------------------------------
    def finish(self) -> None:
        """Poll every unapplied batch, apply each as it ends, then the PQ retry round."""
        deadline = time.monotonic() + self.args.max_wait_hours * 3600
        while True:
            unapplied = [b for b in self.state["batches"] if not b["applied"]]
            if not unapplied:
                retry = self.state["retry_ids"]
                if self.kind == "pqs" and retry and self.state["submitted"]["retry"] < max(retry):
                    self.submit("retry", sorted(retry))
                    continue
                return
            progressed = False
            for b in unapplied:
                if b["status"] != "ended":
                    info = self._api(lambda: self.client.messages.batches.retrieve(b["id"]),
                                     f"checking batch {b['id']}")
                    if info is None:
                        continue
                    b["status"] = info.processing_status
                    rc = getattr(info, "request_counts", None)
                    if rc is not None:
                        b["request_counts"] = {k: getattr(rc, k, 0) for k in
                                               ("processing", "succeeded", "errored", "canceled", "expired")}
                    self.save()
                if b["status"] == "ended":
                    self.apply(b)
                    progressed = True
            if progressed:
                continue
            if time.monotonic() > deadline:
                raise StillProcessing(", ".join(f"{b['id']} {b['status']} {b.get('request_counts', '')}"
                                                for b in unapplied))
            print(f"[retag] {self.kind}: waiting for {len(unapplied)} batch(es): "
                  + "; ".join(f"{b['id']} {b.get('request_counts', b['status'])}" for b in unapplied),
                  flush=True)
            self.sleep(self.args.poll_seconds)

    def _ensure_backup(self) -> None:
        if self.dry or not self.args.backup or self.state.get("backup_table"):
            return
        check_cron_window(self.args)
        self.state["backup_table"] = backup_table(self.kind)
        self.save()

    def apply(self, b: dict) -> None:
        """Apply one ended batch: every result through the live verification and write path.
        Re-applying a batch after a stop is harmless (same tags written again)."""
        from extensions import db
        if not self.dry:
            check_cron_window(self.args)
            self._ensure_backup()
        counts: Counter = Counter()
        retry: list[int] = []
        errored: list[list] = []
        results = self._api(lambda: self.client.messages.batches.results(b["id"]),
                            f"fetching results of {b['id']}")
        if results is None:
            return
        for n, res in enumerate(results):
            if not self.dry and n and n % _APPLY_WINDOW_EVERY == 0:
                check_cron_window(self.args)
            self._apply_one(res, counts, retry, errored)
        db.session.commit()
        st = self.state
        for k, v in counts.items():
            st["counts"][k] = st["counts"].get(k, 0) + v
        st["retry_ids"] = sorted(set(st["retry_ids"]) | set(retry))
        st["errored_count"] += len(errored)
        st["errored"] = (st["errored"] + errored)[:_ERRORED_KEPT]
        b["applied"] = True
        self.save()
        print(f"[retag] {self.kind}: applied batch {b['id']} — " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())) + (f", errored={len(errored)}" if errored else ""),
            flush=True)

    def _apply_one(self, res, counts: Counter, retry: list, errored: list) -> None:
        from extensions import db
        from hansard_archive import tagger as tg
        from hansard_archive.models import HansardSession, HaPQ

        prefix, ids, fp = _parse_custom_id(res.custom_id)
        rtype = res.result.type
        if rtype != "succeeded":
            why = rtype
            err = getattr(getattr(res.result, "error", None), "error", None)
            if err is not None and getattr(err, "type", None):
                why = f"{rtype}: {err.type}"
            if prefix == "p":                       # as live: a failed group call is retried singly
                retry.extend(ids)
                counts["to_retry"] += len(ids)
            else:
                errored.append([ids[0], why])
            return
        msg = res.result.message

        if prefix == "s":
            s = db.session.get(HansardSession, ids[0])
            if s is None:
                errored.append([ids[0], "gone (deleted since submission)"])
                return
            req = tg.session_request(tg.session_input_from_db(s))
            if req.fingerprint != fp:
                errored.append([ids[0], "text changed since submission"])
                return
            outcome = self._parse(msg, req, lambda parsed, usage: tg.session_outcome(req, parsed, usage,
                                                                                     self.model))
            if outcome is None:
                errored.append([ids[0], "unparseable answer"])
                return
            self._record(ids[0], _label("sessions", s), outcome, counts)
            return

        rows = [db.session.get(HaPQ, i) for i in ids]
        if prefix == "q":
            p = rows[0]
            if p is None:
                errored.append([ids[0], "gone (deleted since submission)"])
                return
            req = tg.pq_request(tg.pq_input_from_db(p))
            if req.fingerprint != fp:
                errored.append([ids[0], "text changed since submission"])
                return
            outcome = self._parse(msg, req, lambda parsed, usage: tg.pq_outcome(req, parsed, usage, self.model))
            if outcome is None:
                errored.append([ids[0], "unparseable answer"])
                return
            outcome.retried = True
            self._record(p.id, _label("pqs", p), outcome, counts)
            return

        # "p": a group of PQs, mapped back by position + echoed UIN, exactly as live
        present = [p for p in rows if p is not None]
        if len(present) < len(rows):
            counts["gone"] += len(rows) - len(present)
        inputs = [tg.pq_input_from_db(p) for p in present]
        req = tg.pq_batch_request(inputs) if inputs else None
        if req is None or req.fingerprint != fp or len(present) < len(rows):
            retry.extend(p.id for p in present)     # text changed: ask again with the current text
            counts["to_retry"] += len(present)
            return
        _, wrapped = tg.claude_request_params(self.model, req.prompt, req.schema)
        try:
            parsed, usage = tg.parse_claude_message(msg, self.model, wrapped)
        except (tg.GeminiEmptyResponse, json.JSONDecodeError):
            parsed, usage = None, {}
        first, again = tg.pq_batch_outcomes(inputs, req, parsed, usage, self.model)
        for j, p in enumerate(present):
            if j in again:
                retry.append(p.id)
                counts["to_retry"] += 1
            else:
                self._record(p.id, _label("pqs", p), first[j], counts)

    def _parse(self, msg, req, make):
        """TagOutcome from a succeeded result (empty outcome for a refusal etc.), None if unparseable."""
        from hansard_archive import tagger as tg
        _, wrapped = tg.claude_request_params(self.model, req.prompt, req.schema)
        try:
            parsed, usage = tg.parse_claude_message(msg, self.model, wrapped)
        except tg.GeminiEmptyResponse as e:
            return tg._empty_outcome(e, self.model)
        except json.JSONDecodeError:
            return None
        return make(parsed, usage)

    def _record(self, item_id: int, label: str, outcome, counts: Counter) -> None:
        from hansard_archive import tagger as tg
        if self.shown < self.args.sample:
            _print_before_after(self.kind, item_id, label, outcome)
            self.shown += 1
        if not outcome.ok:
            counts["kept_old"] += 1
            return
        if self.dry:
            counts["would_retag"] += 1
            return
        write = tg.write_session_tags if self.kind == "sessions" else tg.write_pq_tags
        write(item_id, outcome, replace=True)
        counts["retagged"] += 1

    def report(self) -> None:
        st = self.state
        print(f"\n[retag] {self.kind} batch run: {dict(sorted(st['counts'].items()))}, "
              f"errored/expired/changed (old tags kept)={st['errored_count']}", flush=True)
        if st["errored"]:
            by_reason = Counter(why for _, why in st["errored"])
            print(f"[retag]   reasons: {dict(by_reason)}", flush=True)
            ids = sorted({i for i, _ in st["errored"]})
            if len(ids) <= 300:
                print(f"[retag]   re-run them: python scripts/retag.py --what {self.kind} "
                      f"--ids {','.join(map(str, ids))} --batch --model {self.model}"
                      + (" --apply --backup" if not self.dry else ""), flush=True)
            else:
                print(f"[retag]   {len(ids)} ids in ha_cron_run notes (service_name={self.service})",
                      flush=True)
        if st.get("backup_table"):
            print("\n" + undo_sql(self.kind, st["backup_table"], self.model, st["started_at"]), flush=True)


def run_batch(kinds: tuple, args, api_key: str, model: str, sample_given: bool,
              client=None, sleep=time.sleep) -> dict:
    """--batch for each kind: estimate, then (apply or --sample) submit all kinds, then poll and apply."""
    from extensions import db
    from hansard_archive import tagger as tg

    selections = {}
    for kind in kinds:
        ids = select_ids(kind, args)
        print_estimate(kind, ids, model, batch=True)
        selections[kind] = ids
    db.session.rollback()

    if not args.apply and not sample_given:
        for kind in kinds:
            st = load_checkpoint(checkpoint_service(kind, args, model, "batch"))
            if st.get("batches"):
                print(f"[retag] {kind}: a --batch --apply run of this selection has "
                      f"{len(st['batches'])} batch(es) on record; rerunning with --apply resumes it",
                      flush=True)
        print("[retag] DRY RUN (--batch) — no API call made. Add --sample N to submit a batch of N "
              "items and see their tags, or --apply --backup to re-tag everything selected.", flush=True)
        return {}

    dry = not args.apply
    tg.check_anthropic_key(api_key, model)
    client = client or tg._claude_client(api_key)
    batchers = []
    for kind in kinds:
        mode = "bsample" if dry else "batch"
        service = checkpoint_service(kind, args, model, mode)
        state = load_checkpoint(service)
        if state.get("batches"):
            print(f"[retag] {kind}: resuming {len(state['batches'])} batch(es) from the checkpoint "
                  f"(ha_cron_run service_name={service}) — nothing is submitted twice", flush=True)
        ids = _sample_of(selections[kind], args.sample) if dry else selections[kind]
        if dry:
            print(f"[retag] {kind}: SAMPLE — submitting {len(ids)} item(s) via the Batch API; "
                  "no tags are written", flush=True)
        batchers.append((_Batcher(kind, args, model, client, service, state, dry, sleep), ids))

    stats = {}
    current = None
    try:
        for b, ids in batchers:
            current = b
            b.submit("main", ids)
        for b, _ in batchers:
            current = b
            b.finish()
            b.save(status="ok")
            b.finished = True
            b.report()
            stats[b.kind] = dict(b.state["counts"])
    except (TooManyFailures, WindowStop, StillProcessing) as e:
        db.session.rollback()
        status = "stopped" if isinstance(e, (WindowStop, StillProcessing)) else "failed"
        for b, _ in batchers:
            if not b.finished and (b.run_id is not None or b is current):
                b.state["stopped"] = f"{type(e).__name__}: {e}"
                b.save(status=status if b is current else "stopped")
        raise
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
    ap.add_argument("--model", help="model id (default: TAGGER_MODEL, else auto-detected Gemini Flash-Lite)")
    ap.add_argument("--pq-batch", type=int, default=5, help="Written Questions per API call / batch request")
    ap.add_argument("--sample", type=int, default=None,
                    help="items shown before/after (default 20); with --batch and no --apply, "
                         "submit a sample batch of this many")
    ap.add_argument("--apply", action="store_true", help="write the new tags (default: dry run)")
    ap.add_argument("--backup", action="store_true", help="snapshot the theme table(s) before --apply")
    ap.add_argument("--no-backup", action="store_true", help="--apply without a snapshot (you made one)")
    ap.add_argument("--batch", action="store_true",
                    help="Claude Message Batches API (half price; see the module docstring)")
    ap.add_argument("--poll-seconds", type=int, default=60, help="--batch: seconds between status checks")
    ap.add_argument("--max-wait-hours", type=float, default=25.0,
                    help="--batch: stop (exit 5, resumable) if batches are still processing after this")
    ap.add_argument("--ignore-cron-windows", action="store_true")
    ap.add_argument("--max-failures", type=int, default=5, help="stop after this many API failures in a row")
    args = ap.parse_args(argv)
    sample_given = args.sample is not None
    if args.sample is None:
        args.sample = 20

    if args.apply and args.backup == args.no_backup:
        print("[retag] --apply needs exactly one of --backup or --no-backup")
        return 2

    from hansard_archive import tagger as tg
    try:
        configured = tg.configured_model(args.model)
    except tg.GeminiCallFailed as e:
        print(f"[retag] {e}")
        return 2
    if args.batch:
        if not tg.is_claude_model(configured):
            print("[retag] --batch uses the Claude Message Batches API: pass --model claude-haiku-5-5 "
                  "or set TAGGER_MODEL=claude-haiku-5-5")
            return 2
        if not 1 <= args.pq_batch <= 5:
            print("[retag] --batch needs --pq-batch between 1 and 5")
            return 2
        if sample_given and args.sample < 1:
            print("[retag] --sample must be at least 1")
            return 2
    if configured and tg.is_claude_model(configured) and args.apply \
            and configured not in tg.LIVE_CLAUDE_MODELS:
        print(f"[retag] {configured} is not approved for live tags "
              f"(tagger.LIVE_CLAUDE_MODELS: {', '.join(sorted(tg.LIVE_CLAUDE_MODELS))})")
        return 2
    model = configured or tg.DEFAULT_MODEL
    api_key = tg.key_for_model(model, os.environ.get("GEMINI_API_KEY"))
    if not api_key:
        print(f"[retag] {tg.key_env_name(model)} is not set (model {model})")
        return 2

    from flask_app import app
    kinds = ("sessions", "pqs") if args.what == "both" else (args.what,)
    with app.app_context():
        try:
            if args.batch:
                stats = run_batch(kinds, args, api_key, configured, sample_given)
                print(f"[retag] done: {stats}", flush=True)
                return 0
            for kind in kinds:
                stats = run(kind, args, api_key, configured)
                print(f"[retag] {kind} done: {stats}", flush=True)
        except TooManyFailures as e:
            print(f"[retag] STOPPED after API failures ({e}). "
                  "Progress is saved; run the same command again to resume.", flush=True)
            return 3
        except WindowStop as e:
            print(f"[retag] STOPPED for cron window: {e}. "
                  "Progress is saved; run the same command again outside the window.", flush=True)
            return 4
        except StillProcessing as e:
            print(f"[retag] STOPPED: batches still processing ({e}). "
                  "Nothing is lost; run the same command again later to collect them.", flush=True)
            return 5
        except tg.GeminiCallFailed as e:          # e.g. no usable Anthropic key
            print(f"[retag] STOPPED: {e}", flush=True)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
