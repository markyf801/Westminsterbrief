"""
PQ Archive incremental ingestion — cron entry point.

Fetches the last N days of Written Questions from Parliament's WQ API,
upserts into ha_pq, then theme-tags any untagged PQs.

Railway Cron Job services using this script (each records its own name as
--service-name; schedules and settings: docs/ops/railway-services.md):

  pq-cron-early      30 8 * * 1-5   08:30 UTC Mon-Fri  (first capture after publication)
  pq-cron-morning    30 9 * * 1-5   09:30 UTC Mon-Fri  (late-published questions)
  pq-cron-afternoon  0 14 * * 1-5   14:00 UTC Mon-Fri  (answer publication window)
  pq-cron-monday     0 9 * * 1      09:00 UTC Monday   (post-weekend backlog)
  pq-quickcheck      --quick-check  (proposed; see the doc)

Usage:
  python scripts/ingest_pq_cron.py                            # defaults (7d, inline tag)
  python scripts/ingest_pq_cron.py --days 7 --service-name pq-cron-morning
  python scripts/ingest_pq_cron.py --days 365 --service-name backfill --no-tag
  python scripts/ingest_pq_cron.py --days 1 --answered-days 120 --no-tag   # answer catch-up
  python scripts/ingest_pq_cron.py --quick-check --service-name pq-quickcheck  # frequent check
  python scripts/ingest_pq_cron.py --quick-check --dry-run   # counts + predicted changes, no writes
  python scripts/ingest_pq_cron.py --dry-run --no-tag      # full sweep, predicted changes, no writes
  python scripts/ingest_pq_cron.py --answered-from 2025-05-01 --answered-to 2026-10-23 \
      --chunk-days 7 --service-name pq-catchup --no-tag      # long catch-up in weekly windows

Three passes: questions TABLED in the last --days, then questions ANSWERED in
the last --answered-days (default: same as --days), then answers CORRECTED in
the last --corrected-days (default: same as --answered-days). Answers usually
arrive 1-4 weeks after tabling, so the tabled window alone leaves answered PQs
showing "Awaiting answer". A correction keeps the original answer date, so only
the corrected window finds a corrected answer to an older question. Every
pass fetches the full (individual) record only for rows that need it: new
rows, rows whose stored question text is not the full text (the bulk endpoint
cuts it at 255 characters), and rows whose answer is new or changed —
including a holding answer replaced by the real one on the same day (same
answer date, holding flag changed) and a newly corrected answer (new
correction date). Rows already complete cost one DB lookup, not a request.

--quick-check (opt-in; the scheduled full sweeps don't use it) asks the API for
counts only (take=1, totalResults): questions tabled in the last
--quick-tabled-days, and questions answered on each of the last
--quick-answered-days. The counts are kept in ha_cron_run.notes, which
scripts/pq_publication_times.py reads to find when Parliament publishes
(the API itself has dates only, no times). A window is ingested when the API
count is above the matching ha_pq count (answers count only when their full
text is stored — answer_needs_fetch_sql), or above the API count the last
successful check saw for the same window (so extra rows on our side can't hide
new ones). Otherwise it logs one line and exits in a few seconds. It skips
(status "skipped") when another PQ run is in progress. Changes that leave every
count the same (a corrected answer, a withdrawal) are left to the full sweeps.

Chunked catch-up (--answered-from/--answered-to or --tabled-from/--tabled-to,
with --chunk-days, default 7) replaces the three passes: the period is walked
in short date windows, newest first, so the API is never paged deep (deep pages
return HTTP 500). Progress is saved in the run's ha_cron_run.notes after every
page; running the same command again carries on from where an unfinished run
stopped and retries the chunks that failed. A failed chunk doesn't stop the
run; 3 failed chunks in a row, a 429 or too many identity mismatches do. See
hansard_archive/pq_chunks.py and docs/ops/railway-services.md.

If the API fails during a pass (pages failing after all retries), or a sweep
reaches its time budget (--max-minutes, default 50), the run still tags what
it stored before stopping, and is reported as failed. --full-refresh (used
weekly by pq-cron-monday) fetches the full record for every question in the
tabled window, as a safety net for changes the "only when needed" rule can't
see (see docs/ops/railway-services.md, "API trouble").

The --no-tag flag skips theme tagging. Use it for backfill runs — tagging
~70k rows inline would take 2-4 hours. Run tagging as a separate pass instead.

Environment:
  DATABASE_URL          — Postgres (Railway Variable Reference); falls back to SQLite
  GEMINI_API_KEY        — required for theme tagging (not needed with --no-tag)
  POSTMARK_SERVER_TOKEN — required for email alerts; absent = log-only
  ADMIN_EMAIL           — alert recipient; overridden by CRON_ALERT_EMAIL if set
  CRON_ALERT_EMAIL      — explicit alert recipient (optional)
"""

import os
import sys
import time
import argparse
import traceback
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")

import requests
from sqlalchemy import func, not_, or_

from flask_app import app
from hansard_archive.pq_ingestor import (
    WQ_API_BASE, PQIdentityMismatchError, PQIngestAborted, PQRateLimitedError,
    _env_int, answer_needs_fetch_sql, ingest_pq_date_range,
)
from hansard_archive.models import HaCronRun, HaPQ
from hansard_archive.pq_chunks import NOTE_PREFIX as _CHUNK_NOTE
from hansard_archive.pq_chunks import ChunkProgress, last_beat, parse_progress
from hansard_archive.pq_publication import format_counts, parse_counts
from extensions import db

_LOG_RETENTION_DAYS = 90
_PASS_KEYS = ("inserted", "updated", "unchanged", "identity_mismatch",
              "failed", "page_errors", "rolled_back", "full_fetched",
              "full_fetch_failed", "full_missing", "errors")

_QUICK_NOTE = "quick-check"     # HaCronRun.notes prefix for quick-check runs
_QUICK_TIMEOUT = 60             # count calls have taken 40+ s while the API is slow
_QUICK_RETRIES = 2              # one retry on a transient error (CLAUDE.md)
_ACTIVE_RUN_MINUTES = 120       # a "running" record with no progress for this long is dead
_FULL_WAIT_SECONDS = 15 * 60    # a full sweep waits this long for a running quick-check
_FULL_WAIT_POLL = 30
_CHUNK_FAILS_TO_STOP = 3        # chunked run: this many failed chunks in a row stop it
_RUN_MAX_IDENTITY_MISMATCHES = 20   # chunked run: more than this in the whole run stop it
_MAX_CHUNKS = 200               # bigger plans: use longer chunks (keeps the notes small)
_HEARTBEAT_SECONDS = 60         # chunked run: progress saved at most this often mid-chunk
# Scheduled sweeps stop starting new work after this many minutes (0 = no
# limit), so a hanging API can't make one run into the next.
_RUN_BUDGET_MINUTES = _env_int("PQ_RUN_BUDGET_MINUTES", 50)
# Most PQs tagged per run (env PQ_TAG_LIMIT), fewer after an ingest that
# stopped early, so tagging can't run for hours past the time budget. The
# rest are tagged by the next run.
_TAG_LIMIT = _env_int("PQ_TAG_LIMIT", 500)
_TAG_LIMIT_AFTER_ABORT = 200


class QuickCheckError(RuntimeError):
    """A count request to Parliament's API failed."""


def _run_pass(label: str, date_from: date, date_to: date,
              **kwargs) -> tuple[dict, str | None, PQIngestAborted | None]:
    """Run one ingest pass. Returns (counts, abort_message, abort_exception).

    An aborted pass (PQIngestAborted) still returns the counts it reached, so
    the run summary and the alert show what happened before it stopped."""
    try:
        counts = ingest_pq_date_range(date_from, date_to, verbose=True, **kwargs)
        abort, exc_out = None, None
    except PQIngestAborted as exc:
        counts, abort, exc_out = exc.counts, f"{type(exc).__name__}: {exc}", exc
        print(f"[pq-cron] {label} ABORTED: {abort}", flush=True)
    counts = {k: counts.get(k, 0) for k in _PASS_KEYS}
    print(
        f"[pq-cron] {label} done — "
        f"inserted={counts['inserted']} updated={counts['updated']} "
        f"unchanged={counts['unchanged']} "
        f"identity_mismatch={counts['identity_mismatch']} "
        f"failed={counts['failed']} page_errors={counts['page_errors']} "
        f"rolled_back={counts['rolled_back']} full_fetched={counts['full_fetched']} "
        f"full_fetch_failed={counts['full_fetch_failed']} "
        f"full_missing={counts['full_missing']} errors={counts['errors']}",
        flush=True,
    )
    return counts, abort, exc_out


def _pass_notes(passes: dict) -> str:
    """One line per run for ha_cron_run.notes: what each pass wrote and how
    many full records it fetched."""
    return "passes: " + "; ".join(
        f"{label} ins={c['inserted']} upd={c['updated']} full_fetched={c['full_fetched']} "
        f"fetch_failed={c['full_fetch_failed']} missing={c['full_missing']}"
        for label, c in passes.items())


def _tag_after_abort(exc: PQIngestAborted | None) -> bool:
    """Tag what was stored after an ingest that stopped early because the API
    was failing (the rows written are good). Not after an identity-mismatch
    abort: then which question is which is in doubt."""
    return not isinstance(exc, PQIdentityMismatchError)


def _alert_reasons(passes: dict, tag_errors: int, fatal_error: str | None,
                   tag_aborted: str = "") -> list[str]:
    """Why this run needs an alert email (empty list = no alert)."""
    reasons = []
    if fatal_error:
        reasons.append("fatal error")
    if tag_aborted:
        reasons.append(f"tagging {tag_aborted}")
    for label, counts in passes.items():
        for key in ("identity_mismatch", "failed", "page_errors"):
            if counts.get(key):
                reasons.append(f"{label}: {key}={counts[key]}")
    if tag_errors >= 3:
        reasons.append(f"tagging errors={tag_errors}")
    return reasons


def _alert_email() -> str:
    return os.environ.get("CRON_ALERT_EMAIL") or os.environ.get("ADMIN_EMAIL", "")


def _send_failure_alert(service_name: str, subject: str, body: str) -> None:
    recipient = _alert_email()
    if not recipient:
        print("[pq-cron] ALERT: no recipient configured (set ADMIN_EMAIL or CRON_ALERT_EMAIL)", flush=True)
        return
    try:
        from email_service import send_email
        with app.app_context():
            send_email(
                to=recipient,
                subject=f"[Westminster Brief] {subject}",
                html_body=f"<pre>{body}</pre>",
                text_body=body,
            )
        print(f"[pq-cron] Alert email sent to {recipient}", flush=True)
    except Exception as exc:
        print(f"[pq-cron] Failed to send alert email: {exc}", flush=True)


def _prune_old_runs() -> None:
    cutoff = datetime.utcnow() - timedelta(days=_LOG_RETENTION_DAYS)
    try:
        deleted = HaCronRun.query.filter(
            HaCronRun.started_at < cutoff,
            HaCronRun.service_name.like("pq-%"),
        ).delete()
        db.session.commit()
        if deleted:
            print(f"[pq-cron] Pruned {deleted} old PQ run records (>{_LOG_RETENTION_DAYS}d)", flush=True)
    except Exception as exc:
        print(f"[pq-cron] Warning: could not prune old runs: {exc}", flush=True)
        db.session.rollback()


# ── quick-check ──────────────────────────────────────────────────────────────

def _api_count(params: dict) -> int:
    """totalResults for a WQ API query, fetching a single row.

    A 4xx (bad request, auth, 429 rate limit) is not retried. A transient error
    (5xx, timeout, connection reset, bad body) is retried once after a pause."""
    last_exc: Exception | None = None
    for attempt in range(_QUICK_RETRIES):
        try:
            resp = requests.get(WQ_API_BASE, params={**params, "take": 1},
                                timeout=_QUICK_TIMEOUT)
        except Exception as exc:
            last_exc = exc
        else:
            if 400 <= resp.status_code < 500:
                raise QuickCheckError(
                    f"WQ API count failed for {params}: HTTP {resp.status_code} (not retried)")
            try:
                resp.raise_for_status()
                total = resp.json().get("totalResults")
                if not isinstance(total, int):
                    raise ValueError("response has no totalResults")
                return total
            except Exception as exc:
                last_exc = exc
        if attempt < _QUICK_RETRIES - 1:
            time.sleep(2 ** (attempt + 1))
    raise QuickCheckError(f"WQ API count failed for {params}: {last_exc}")


def _db_count(column, date_from: date, date_to: date, *conditions) -> int:
    return (db.session.query(func.count(HaPQ.id))
            .filter(column >= date_from, column <= date_to, *conditions)
            .scalar()) or 0


def _quick_counts(today: date, tabled_days: int, answered_days: int) -> list[dict]:
    """Tabled over the last tabled_days (one API call), and answered for each
    of the last answered_days (one call per day), each with the DB count.

    The answered DB count leaves out rows whose answer text is missing or looks
    like the truncated bulk copy (the ingestor's answer_needs_fetch_sql rule),
    so such a row keeps the API count ahead until its full answer is stored."""
    tabled_from = today - timedelta(days=tabled_days - 1)
    checks = [{
        "kind": "tabled", "from": tabled_from, "to": today,
        "api": _api_count({"tabledWhenFrom": tabled_from.isoformat(),
                           "tabledWhenTo": today.isoformat()}),
        "db": _db_count(HaPQ.tabled_date, tabled_from, today),
    }]
    answer_complete = not_(answer_needs_fetch_sql(HaPQ.answer_text))
    for i in range(answered_days):
        day = today - timedelta(days=i)
        checks.append({
            "kind": "answered", "from": day, "to": day,
            "api": _api_count({"answeredWhenFrom": day.isoformat(),
                               "answeredWhenTo": day.isoformat()}),
            "db": _db_count(HaPQ.answer_date, day, day, answer_complete),
        })
    return checks


def _last_ok_api_counts(service_name: str, exclude_id: int | None) -> dict:
    """{(kind, from, to): API count} from this service's newest successful
    quick-check that recorded counts. A failed run never becomes the baseline,
    so a narrow pass that failed is retried."""
    try:
        query = HaCronRun.query.filter(HaCronRun.service_name == service_name,
                                       HaCronRun.status == "ok",
                                       HaCronRun.notes.like(f"{_QUICK_NOTE}: %API=%"))
        if exclude_id:
            query = query.filter(HaCronRun.id != exclude_id)
        run = query.order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first()
    except Exception as exc:
        db.session.rollback()
        print(f"[pq-cron] Warning: could not read the previous quick-check: {exc}", flush=True)
        return {}
    if run is None:
        return {}
    return {(o.kind, o.date_from, o.date_to): o.api
            for o in parse_counts(run.notes, run.started_at)}


def _grown(checks: list[dict], previous: dict) -> list[dict]:
    """Windows to ingest: API above the DB, or above what the last successful
    check saw (catches new items even when the DB count is ahead of the API)."""
    out = []
    for c in checks:
        prev_api = previous.get((c["kind"], c["from"], c["to"]))
        if c["api"] > c["db"] or (prev_api is not None and c["api"] > prev_api):
            out.append(c)
    return out


def _last_progress(run: HaCronRun) -> datetime:
    """When a run last showed it was alive: its start, or the newer heartbeat
    a chunked run writes into its notes after every page."""
    beat = last_beat(run.notes) if (run.notes or "").startswith(_CHUNK_NOTE) else None
    return max(run.started_at, beat) if beat else run.started_at


def _active_runs(exclude_id: int | None, quick_only: bool) -> list[HaCronRun]:
    """Runs still "running" that showed progress in the last
    _ACTIVE_RUN_MINUTES, newest first. A chunked catch-up can last many hours,
    so it counts by its heartbeat, not its start; a crashed run (left
    "running") stops counting once its heartbeat is that old."""
    cutoff = datetime.utcnow() - timedelta(minutes=_ACTIVE_RUN_MINUTES)
    query = HaCronRun.query.filter(
        HaCronRun.status == "running",
        or_(HaCronRun.started_at >= cutoff, HaCronRun.notes.like(f"{_CHUNK_NOTE}:%")))
    if quick_only:
        query = query.filter(HaCronRun.notes.like(f"{_QUICK_NOTE}%"))
    else:
        query = query.filter(or_(HaCronRun.service_name.like("pq-%"),
                                 HaCronRun.notes.like(f"{_QUICK_NOTE}%")))
    if exclude_id:
        query = query.filter(HaCronRun.id != exclude_id)
    runs = query.order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).all()
    return [r for r in runs if _last_progress(r) >= cutoff]


def _wait_for_quick_check(exclude_id: int | None) -> None:
    """A full sweep waits (up to _FULL_WAIT_SECONDS) for a running quick-check
    so the two don't insert the same new rows at the same time."""
    waited = 0
    while True:
        try:
            db.session.commit()   # fresh snapshot each poll
            if not _active_runs(exclude_id, quick_only=True):
                return
        except Exception as exc:
            db.session.rollback()
            print(f"[pq-cron] Warning: could not check for a running quick-check: {exc}", flush=True)
            return
        if waited >= _FULL_WAIT_SECONDS:
            print("[pq-cron] Quick-check still running after the wait — continuing anyway", flush=True)
            return
        if waited == 0:
            print("[pq-cron] A quick-check is running — waiting for it to finish…", flush=True)
        time.sleep(_FULL_WAIT_POLL)
        waited += _FULL_WAIT_POLL


def _previous_statuses(service_name: str, exclude_id: int | None, n: int = 2) -> list[str] | None:
    """Statuses of this service's previous finished runs (skipped ones left
    out), newest first. None if unreadable."""
    try:
        query = HaCronRun.query.filter(HaCronRun.service_name == service_name,
                                       HaCronRun.status.in_(("ok", "failed")))
        if exclude_id:
            query = query.filter(HaCronRun.id != exclude_id)
        order = (HaCronRun.started_at.desc(), HaCronRun.id.desc())
        return [r.status for r in query.order_by(*order).limit(n)]
    except Exception:
        db.session.rollback()
        return None


def _quick_check_should_alert(prev: list[str] | None) -> bool:
    """Quick-checks run every 30 minutes: email on the 2nd failure in a row
    only, so one blip doesn't alert and an outage sends one email, not twenty."""
    if prev is None:
        return True
    return bool(prev) and prev[0] == "failed" and (len(prev) < 2 or prev[1] != "failed")


def _run_quick_check(args, today: date, run_record_id: int | None, passes: dict) -> dict:
    """Counts first; narrow passes only for windows that grew.

    Fills ``passes`` like the full sweep does. Returns {"inserted", "updated",
    "tagged", "tag_errors", "tag_aborted", "tag_notes", "errors", "abort",
    "skipped", "notes"}. Raises
    QuickCheckError when a count request fails — deliberately no fallback to a
    full run: if the API is failing, a 7-day sweep only hammers it, and the next
    check is 30 minutes away.
    """
    out = {"inserted": 0, "updated": 0, "tagged": 0, "tag_errors": 0, "errors": 0,
           "abort": None, "skipped": False, "tag_aborted": "", "tag_notes": ""}

    running = _active_runs(run_record_id, quick_only=False)
    active = running[0] if running else None
    if active is not None:
        out["skipped"] = True
        out["notes"] = (f"{_QUICK_NOTE}: skipped ({active.service_name} running since "
                        f"{active.started_at:%H:%M} UTC)")
        print(f"[pq-cron] {out['notes']}", flush=True)
        return out

    previous = _last_ok_api_counts(args.service_name, run_record_id)
    checks = _quick_counts(today, args.quick_tabled_days, args.quick_answered_days)
    summary = format_counts(checks)
    for c in checks:
        if c["db"] > c["api"]:
            print(f"[pq-cron] Note: ha_pq has more rows than the API for {c['kind']} "
                  f"{c['from']}..{c['to']} (DB={c['db']} API={c['api']})", flush=True)
    grown = _grown(checks, previous)
    if not grown:
        out["notes"] = f"{_QUICK_NOTE}: nothing new ({summary})"
        print(f"[pq-cron] {out['notes']}", flush=True)
        return out

    print(f"[pq-cron] {_QUICK_NOTE}: something new ({summary})", flush=True)
    ran = []
    to_run = []
    abort_exc = None
    tabled = [c for c in grown if c["kind"] == "tabled"]
    if tabled:
        to_run.append(("tabled", tabled[0]["from"], tabled[0]["to"]))
    answered = [c for c in grown if c["kind"] == "answered"]
    if answered:
        to_run.append(("answered", min(c["from"] for c in answered),
                       max(c["to"] for c in answered)))

    for kind, d_from, d_to in to_run:
        print(f"[pq-cron] Ingesting WQs {kind} {d_from} → {d_to}…", flush=True)
        # fetch_only_new_answers: full-record fetch only for new rows and new
        # (or missing/truncated) answers, so stored rows cost one DB lookup.
        res, abort, abort_exc = _run_pass(f"{kind.capitalize()} pass", d_from, d_to,
                                          skip_answer_fetch=args.skip_answer_fetch,
                                          date_field=kind,
                                          fetch_only_new_answers=True,
                                          dry_run=args.dry_run,
                                          deadline=args.deadline)
        passes[kind] = res
        out["inserted"] += res["inserted"]
        out["updated"] += res["updated"]
        out["errors"] += res["errors"]
        ran.append(f"{kind} {d_from}..{d_to}")
        if abort:
            out["abort"] = abort   # API down or identities disagree: stop here
            break

    new_rows = out["inserted"] + out["updated"]
    if out["abort"] and not _tag_after_abort(abort_exc):
        print("[pq-cron] Tagging skipped (ingest aborted: identity mismatches)", flush=True)
    elif args.dry_run:
        print("[pq-cron] Tagging skipped (--dry-run)", flush=True)
    elif new_rows:
        out.update(_run_tagger(args.no_tag, new_rows,
                               _TAG_LIMIT_AFTER_ABORT if out["abort"] else _TAG_LIMIT))
        out["errors"] += out["tag_errors"]

    fetched = sum(p["full_fetched"] for p in passes.values())
    out["notes"] = (f"{_QUICK_NOTE}: ran {', '.join(ran)} "
                    f"ins={out['inserted']} upd={out['updated']} full_fetched={fetched} "
                    f"({summary})")
    return out


# ── shared ───────────────────────────────────────────────────────────────────

def _run_tagger(no_tag: bool, new_rows: int, limit: int | None = None) -> dict:
    """Theme-tag untagged PQs, at most `limit` (None/0 = all). Returns
    {"tagged", "tag_errors", "tag_aborted", "tag_notes"}."""
    if no_tag:
        print("[pq-cron] Tagging skipped (--no-tag)", flush=True)
        return {"tagged": 0, "tag_errors": 0, "tag_aborted": "", "tag_notes": ""}
    from hansard_archive.tagger import tag_pq_all_untagged
    if new_rows > 0:
        print(f"[pq-cron] Tagging up to {new_rows} PQ(s)…", flush=True)
    else:
        print("[pq-cron] No new/updated rows — running tagger for any missed PQs…", flush=True)
    tag_result = tag_pq_all_untagged(limit=limit or None, verbose=True)
    tagged = tag_result.get("tagged", 0)
    tag_aborted = tag_result.get("aborted") or ""
    tag_notes = (
        f"tagging: eligible={tag_result.get('total_eligible', 0)} tagged={tagged} "
        f"nothing_verified={tag_result.get('failed', 0)} "
        f"api_errors={tag_result.get('api_errors', 0)} given_up={tag_result.get('given_up')}"
        + (f" ABORTED: {tag_aborted}" if tag_aborted else "")
    )
    print(f"[pq-cron] Tagging done — {tag_notes}", flush=True)
    return {"tagged": tagged, "tag_errors": tag_result.get("errors", 0),
            "tag_aborted": tag_aborted, "tag_notes": tag_notes}


def _run_full(args, date_from: date, today: date, answered_days: int, passes: dict,
              corrected_days: int = 0) -> dict:
    """The scheduled sweep: tabled window, then answered window, then corrected
    window, then tagging.
    Returns {"inserted", "updated", "tagged", "tag_errors", "tag_aborted",
    "tag_notes", "errors", "abort"}."""
    # --- Ingestion ---
    print(f"[pq-cron] Ingesting WQs {date_from} → {today}…", flush=True)
    # fetch_only_new_answers: the full record only for new rows, cut question
    # text and new/changed answers (it used to be fetched for every row in the
    # window, about 3,500 requests a run). --full-refresh (pq-cron-monday)
    # fetches every row once a week, as before, as a safety net for changes
    # that rule can't see.
    result, abort, abort_exc = _run_pass("Tabled pass", date_from, today,
                                         skip_answer_fetch=args.skip_answer_fetch,
                                         fetch_only_new_answers=not args.full_refresh,
                                         dry_run=args.dry_run,
                                         deadline=args.deadline)
    passes["tabled"] = result
    out = {"inserted": result["inserted"], "updated": result["updated"],
           "errors": result["errors"], "tagged": 0, "tag_errors": 0,
           "tag_aborted": "", "tag_notes": ""}

    # --- Answers to older questions (tabled before the window), then answers
    # corrected recently (a correction keeps the original answer date) ---
    # Each skipped after an abort: the API is down or identities disagree.
    for kind, days, verb in (("answered", answered_days, "answered"),
                             ("corrected", corrected_days, "with answers corrected")):
        if days <= 0 or abort:
            continue
        since = today - timedelta(days=days - 1)
        print(f"[pq-cron] Refreshing WQs {verb} {since} → {today}…", flush=True)
        res, abort, abort_exc = _run_pass(f"{kind.capitalize()} pass", since, today,
                                          skip_answer_fetch=args.skip_answer_fetch,
                                          date_field=kind,
                                          fetch_only_new_answers=True,
                                          dry_run=args.dry_run,
                                          deadline=args.deadline)
        passes[kind] = res
        out["inserted"] += res["inserted"]
        out["updated"] += res["updated"]
        out["errors"] += res["errors"]
    out["abort"] = abort
    out["notes"] = _pass_notes(passes)

    # --- Theme tagging ---
    # After an API failure the rows already stored are still tagged; the run
    # is still reported as failed (with an alert).
    _tag_at_end(args, out, abort_exc)
    return out


def _tag_at_end(args, out: dict, abort_exc: PQIngestAborted | None) -> None:
    """Tagging step of a full sweep or chunked run (fills out's tag keys)."""
    if out["abort"] and not _tag_after_abort(abort_exc):
        if not args.no_tag:
            print("[pq-cron] Tagging skipped (ingest aborted: identity mismatches)", flush=True)
    elif args.dry_run:
        print("[pq-cron] Tagging skipped (--dry-run)", flush=True)
    else:
        if out["abort"] and not args.no_tag:
            print("[pq-cron] Ingest stopped early (API failing or time budget) — tagging "
                  "what was stored",
                  flush=True)
        out.update(_run_tagger(args.no_tag, out["inserted"] + out["updated"],
                               _TAG_LIMIT_AFTER_ABORT if out["abort"] else _TAG_LIMIT))
        out["errors"] += out["tag_errors"]


# ── chunked catch-up ─────────────────────────────────────────────────────────

def _chunk_plan(args) -> ChunkProgress:
    if args.answered_from:
        return ChunkProgress.fresh("answered", args.answered_from, args.answered_to,
                                   args.chunk_days)
    return ChunkProgress.fresh("tabled", args.tabled_from, args.tabled_to, args.chunk_days)


def _previous_chunked_run(service_name: str, key: str, exclude_id: int | None):
    """This service's newest run of the same plan (any status), or None."""
    query = HaCronRun.query.filter(HaCronRun.service_name == service_name,
                                   HaCronRun.notes.like(f"{_CHUNK_NOTE}: {key} |%"))
    if exclude_id:
        query = query.filter(HaCronRun.id != exclude_id)
    return query.order_by(HaCronRun.started_at.desc(), HaCronRun.id.desc()).first()


def _save_progress(run_record_id: int | None, notes: str) -> None:
    """Write progress (and the heartbeat) into this run's record. Never fatal:
    the next save writes it again."""
    if not run_record_id:
        return
    try:
        HaCronRun.query.filter_by(id=run_record_id).update(
            {"notes": notes}, synchronize_session=False)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"[pq-cron] Warning: could not save progress: {exc}", flush=True)


def _walk_chunk(args, kind: str, c_from: date, c_to: date, on_progress,
                page_size: int | None = None, seen: set | None = None):
    """One ingest of one chunk. Returns (counts, abort_exc, seen_ids). An
    unexpected error (a dropped DB connection, say) fails the chunk, not the
    run: the session is reset so the next chunk starts clean."""
    seen = set() if seen is None else seen
    try:
        counts = ingest_pq_date_range(
            c_from, c_to, verbose=True, skip_answer_fetch=args.skip_answer_fetch,
            date_field=kind, fetch_only_new_answers=True, dry_run=args.dry_run,
            page_size=page_size, seen_ids=seen, on_progress=on_progress)
        return counts, None, seen
    except PQIngestAborted as exc:
        return exc.counts, exc, seen
    except Exception as exc:
        print(f"[pq-cron] Chunk {c_from}..{c_to} hit an unexpected error: "
              f"{type(exc).__name__}: {exc}", flush=True)
        db.session.rollback()
        db.engine.dispose()
        zero = {k: 0 for k in _PASS_KEYS}
        zero["page_errors"] = 1
        return zero, exc, seen


def _run_chunked(args, run_record_id: int | None, passes: dict, progress_out: dict) -> dict:
    """Walk a long answered/tabled period in short date windows (newest first),
    resuming a previous run of the same plan that did not finish.

    A chunk fails when a page could not be fetched, it was cut short by API
    failures, or something unexpected went wrong; it is retried once after the
    walk, and a later run of the same command retries what still fails.
    _CHUNK_FAILS_TO_STOP failed chunks in a row stop the run (the API is down).
    A 429 or an identity-mismatch abort stops the run at once, and so do more
    than _RUN_MAX_IDENTITY_MISMATCHES identity mismatches in the whole run. A
    chunk where some full-record requests failed also counts as failed (its
    stale answers would otherwise never be revisited).

    After a chunk, the API's totalResults is compared with the unique
    questions seen; if fewer were seen (rows shifting between pages), the
    chunk is walked once more and the union counted. A chunk still short is
    listed in the notes (short=) but not failed.

    progress_out["notes"] always holds the latest progress line, for the run
    record. Returns the same keys as _run_full.
    """
    progress = _chunk_plan(args)
    kind = progress.kind
    out = {"inserted": 0, "updated": 0, "errors": 0, "tagged": 0, "tag_errors": 0,
           "tag_aborted": "", "tag_notes": "", "abort": None}
    totals = {k: 0 for k in _PASS_KEYS}

    def save():
        progress_out["notes"] = progress.format(datetime.utcnow())
        _save_progress(run_record_id, progress_out["notes"])

    prev = _previous_chunked_run(args.service_name, progress.key, run_record_id)
    if prev is not None and prev.status == "running" and not args.take_over and \
            _last_progress(prev) >= datetime.utcnow() - timedelta(minutes=_ACTIVE_RUN_MINUTES):
        # Not a failure (no email): either it really is still running, or its
        # container was killed (a deploy) less than 2 hours ago.
        out["skipped"] = True
        out["notes"] = (f"chunked catch-up not started: run id {prev.id} of the same plan "
                        f"is still marked running (last progress "
                        f"{_last_progress(prev):%Y-%m-%d %H:%M} UTC). If its container has "
                        f"stopped (a deploy kills it), run again with --take-over, or wait "
                        f"{_ACTIVE_RUN_MINUTES} minutes from its last progress")
        print(f"[pq-cron] {out['notes']}", flush=True)
        return out
    saved = parse_progress(prev.notes) if prev is not None else None
    if saved is not None and not saved.is_complete():
        progress = saved
        print(f"[pq-cron] Resuming run id {prev.id} ({prev.status}): "
              f"{progress.done_count()}/{len(progress.chunks())} chunks done, next "
              f"{progress.next_from or 'none'}, to retry: "
              f"{', '.join(map(str, progress.failed)) or 'none'}", flush=True)
        if prev.status == "running" and not args.dry_run:
            # It stopped without finishing (container killed): no heartbeat
            # for _ACTIVE_RUN_MINUTES, or --take-over. Close it so it no
            # longer looks active.
            HaCronRun.query.filter_by(id=prev.id).update(
                {"status": "failed", "notes": (prev.notes or "")
                 + f" | abandoned, resumed by run {run_record_id}"},
                synchronize_session=False)
            db.session.commit()
    else:
        print(f"[pq-cron] Starting {progress.key}: {len(progress.chunks())} chunks", flush=True)
    save()

    beat = {"at": 0.0}

    def heartbeat(_skip, _rows):
        if time.monotonic() - beat["at"] >= _HEARTBEAT_SECONDS:
            save()
            beat["at"] = time.monotonic()

    walked = 0
    fails_in_a_row = 0
    stop = None
    stop_exc = None

    def walk(chunk) -> bool:
        """Walk one chunk; True if it should be counted as done."""
        nonlocal stop, stop_exc
        c_from, c_to = chunk
        print(f"[pq-cron] Chunk {kind} {c_from} → {c_to}…", flush=True)
        counts, exc, seen = _walk_chunk(args, kind, c_from, c_to, heartbeat)
        ok = exc is None and not counts.get("page_errors") and not counts.get("full_fetch_failed")
        api_total = counts.get("api_total")
        if ok and api_total and len(seen) < api_total:
            print(f"[pq-cron] Chunk {c_from}..{c_to}: API said {api_total}, saw {len(seen)} "
                  f"unique — walking it once more", flush=True)
            _add(totals, counts)
            counts, exc, seen = _walk_chunk(args, kind, c_from, c_to, heartbeat, seen=seen)
            ok = (exc is None and not counts.get("page_errors")
                  and not counts.get("full_fetch_failed"))
            if ok and len(seen) < (counts.get("api_total") or 0):
                print(f"[pq-cron] Chunk {c_from}..{c_to} still short after the second walk: "
                      f"API said {counts['api_total']}, saw {len(seen)} unique", flush=True)
                progress.add_short(chunk)
        _add(totals, counts)
        if isinstance(exc, (PQRateLimitedError, PQIdentityMismatchError)):
            stop, stop_exc = f"{type(exc).__name__}: {exc}", exc
        elif totals["identity_mismatch"] > _RUN_MAX_IDENTITY_MISMATCHES:
            stop = (f"{totals['identity_mismatch']} identity mismatches in this run "
                    f"(limit {_RUN_MAX_IDENTITY_MISMATCHES})")
            stop_exc = PQIdentityMismatchError(stop, counts)
        print(f"[pq-cron] Chunk {c_from}..{c_to} {'done' if ok else 'FAILED'} — "
              f"inserted={counts.get('inserted', 0)} updated={counts.get('updated', 0)} "
              f"unchanged={counts.get('unchanged', 0)} page_errors={counts.get('page_errors', 0)} "
              f"full_fetched={counts.get('full_fetched', 0)} "
              f"full_fetch_failed={counts.get('full_fetch_failed', 0)}"
              + (f" ({type(exc).__name__}: {exc})" if exc else ""), flush=True)
        return ok

    def record(chunk, ok: bool) -> None:
        nonlocal fails_in_a_row, walked
        walked += 1
        progress.mark(chunk, ok)
        fails_in_a_row = 0 if ok else fails_in_a_row + 1
        for key, src in (("ins", "inserted"), ("upd", "updated"), ("same", "unchanged"),
                         ("full", "full_fetched"), ("fetch_failed", "full_fetch_failed")):
            progress.totals[key] = progress_totals_base[key] + totals[src]
        save()

    progress_totals_base = dict(progress.totals)
    # The walk, then one retry of every chunk still failing (this run's and
    # those carried over from a previous run).
    for phase in ("walk", "retry"):
        todo = progress.remaining() if phase == "walk" else progress.failed_chunks()
        if phase == "retry" and todo:
            print(f"[pq-cron] Retrying {len(todo)} failed chunk(s)…", flush=True)
        for chunk in todo:
            if args.max_chunks and walked >= args.max_chunks:
                stop = stop or "limit"
                break
            ok = walk(chunk)
            if phase == "walk":
                progress.advance_past(chunk)
            record(chunk, ok)
            if stop:
                break
            if fails_in_a_row >= _CHUNK_FAILS_TO_STOP:
                stop = (f"{fails_in_a_row} chunks in a row failed (API down?) - stopped; "
                        f"run the same command again to carry on")
                break
        if stop:
            break

    passes["catch-up"] = {k: totals[k] for k in _PASS_KEYS}
    # page_errors here = chunks still failing (a chunk that failed and then
    # worked on its retry is not an error).
    passes["catch-up"]["page_errors"] = len(progress.failed)
    passes["catch-up"]["errors"] = (totals["failed"] + totals["identity_mismatch"]
                                    + len(progress.failed))
    out["inserted"], out["updated"] = totals["inserted"], totals["updated"]
    out["errors"] = passes["catch-up"]["errors"]

    left = len(progress.remaining()) + len(progress.failed)
    if stop == "limit":
        print(f"[pq-cron] Stopped after --max-chunks {args.max_chunks}; {left} chunk(s) left "
              f"— run the same command again to carry on", flush=True)
    elif stop:
        out["abort"] = f"chunked {progress.key}: {stop}"
    elif progress.failed:
        out["abort"] = (f"chunked {progress.key}: {len(progress.failed)} chunk(s) still "
                        f"failing ({', '.join(map(str, progress.failed))}) - run the same "
                        f"command again to retry them")
    print(f"[pq-cron] Catch-up: {progress.done_count()}/{len(progress.chunks())} chunks done, "
          f"{left} left, short={', '.join(map(str, progress.short)) or 'none'}", flush=True)
    save()

    _tag_at_end(args, out, stop_exc)
    return out


def _add(totals: dict, counts: dict) -> None:
    for k in totals:
        totals[k] += counts.get(k) or 0


def main() -> None:
    parser = argparse.ArgumentParser(description="PQ Archive incremental cron")
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (default: 7)")
    parser.add_argument("--service-name", default="manual",
                        help="Service name recorded in ha_cron_run (pq-cron-early / pq-cron-morning / "
                             "pq-cron-afternoon / pq-cron-monday / pq-quickcheck / pq-catchup / backfill)")
    parser.add_argument("--no-tag", action="store_true",
                        help="Skip theme tagging (use for large backfill runs)")
    parser.add_argument("--answered-days", type=int, default=None,
                        help="Also refresh questions ANSWERED in the last N days "
                             "(default: same as --days; 0 = off)")
    parser.add_argument("--corrected-days", type=int, default=None,
                        help="Also refresh answers CORRECTED in the last N days "
                             "(default: same as --answered-days; 0 = off; full sweep only)")
    parser.add_argument("--skip-answer-fetch", action="store_true",
                        help="Skip per-row individual answer fetches (use for backfill — answer text already in DB)")
    parser.add_argument("--quick-check", action="store_true",
                        help="Compare API counts with the DB first; exit if nothing new, "
                             "else ingest only the windows that grew")
    parser.add_argument("--quick-tabled-days", type=int, default=4,
                        help="Quick-check: tabled window in days ending today "
                             "(default: 4, so Monday still sees Friday)")
    parser.add_argument("--quick-answered-days", type=int, default=2,
                        help="Quick-check: answered days checked one by one, ending today (default: 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Predicted inserts/updates only (with --quick-check: counts too); "
                             "writes nothing (no ha_cron_run row, no tagging, no email)")
    day = date.fromisoformat
    parser.add_argument("--answered-from", type=day, metavar="YYYY-MM-DD",
                        help="Chunked catch-up: questions ANSWERED from this date "
                             "(with --answered-to; replaces the normal passes)")
    parser.add_argument("--answered-to", type=day, metavar="YYYY-MM-DD",
                        help="Chunked catch-up: ... to this date (inclusive)")
    parser.add_argument("--tabled-from", type=day, metavar="YYYY-MM-DD",
                        help="Chunked catch-up: questions TABLED from this date (with --tabled-to)")
    parser.add_argument("--tabled-to", type=day, metavar="YYYY-MM-DD",
                        help="Chunked catch-up: ... to this date (inclusive)")
    parser.add_argument("--chunk-days", type=int, default=None,
                        help="Chunked catch-up: days per window, walked newest first (default: 7)")
    parser.add_argument("--take-over", action="store_true",
                        help="Chunked catch-up: resume even though the previous run of the "
                             "same plan is still marked running (use only when its container "
                             "has stopped, e.g. after a deploy)")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Full sweep: fetch the full record for every question in the "
                             "tabled window, not only those that need it (weekly safety net)")
    parser.add_argument("--max-minutes", type=int, default=_RUN_BUDGET_MINUTES,
                        help="Full sweep / quick-check: start no new page or full-record "
                             "request after this many minutes (default 50, env "
                             "PQ_RUN_BUDGET_MINUTES; 0 = no limit; not used by chunked runs)")
    parser.add_argument("--max-chunks", type=int, default=0,
                        help="Chunked catch-up: stop after this many chunks this run "
                             "(0 = no limit); the same command carries on later")
    args = parser.parse_args()
    if args.quick_tabled_days < 1 or args.quick_answered_days < 0:
        parser.error("--quick-tabled-days must be >= 1 and --quick-answered-days >= 0")
    ranges = [r for r in ((args.answered_from, args.answered_to),
                          (args.tabled_from, args.tabled_to)) if r[0] or r[1]]
    chunked = bool(ranges)
    if chunked:
        if len(ranges) != 1 or not all(ranges[0]):
            parser.error("give --answered-from and --answered-to, or --tabled-from and "
                         "--tabled-to (one kind per run)")
        if ranges[0][0] > ranges[0][1]:
            parser.error("the from date is after the to date")
        if args.quick_check or args.answered_days is not None or args.corrected_days is not None:
            parser.error("a chunked catch-up can't be combined with --quick-check, "
                         "--answered-days or --corrected-days")
        args.chunk_days = 7 if args.chunk_days is None else args.chunk_days
        if not 1 <= args.chunk_days <= 31:
            parser.error("--chunk-days must be 1-31")
        if args.max_chunks < 0:
            parser.error("--max-chunks must be >= 0")
        n_chunks = len(_chunk_plan(args).chunks())
        if n_chunks > _MAX_CHUNKS:
            parser.error(f"{n_chunks} chunks is too many (limit {_MAX_CHUNKS}): use a longer "
                         f"--chunk-days or split the period")
        if not args.dry_run and not args.service_name.startswith("pq-"):
            parser.error("a chunked catch-up needs a --service-name starting with pq- "
                         "(e.g. pq-catchup): that is what pauses quick-checks and lets "
                         "the run resume")
    elif args.chunk_days is not None or args.max_chunks or args.take_over:
        parser.error("--chunk-days, --max-chunks and --take-over need "
                     "--answered-from/--answered-to or --tabled-from/--tabled-to")
    if args.max_minutes < 0:
        parser.error("--max-minutes must be >= 0")
    if args.full_refresh and (chunked or args.quick_check):
        parser.error("--full-refresh is for full sweeps only (not --quick-check or a "
                     "chunked catch-up)")
    args.deadline = (time.monotonic() + args.max_minutes * 60
                     if args.max_minutes and not chunked else None)

    run_start = datetime.utcnow()
    today = date.today()
    window_to = today
    if chunked:
        date_from, window_to = ranges[0]
        days_window = (window_to - date_from).days + 1
        mode = "chunked"
    else:
        days_window = args.quick_tabled_days if args.quick_check else args.days
        date_from = today - timedelta(days=days_window - 1)
        mode = "quick-check" if args.quick_check else "full"
    answered_days = args.days if args.answered_days is None else args.answered_days
    corrected_days = answered_days if args.corrected_days is None else args.corrected_days

    print(
        f"[pq-cron] === START === service={args.service_name} "
        f"mode={mode}{' (dry run)' if args.dry_run else ''} "
        f"window={date_from}->{window_to} ({days_window}d) "
        f"tagging={'disabled (--no-tag)' if args.no_tag else 'enabled'}",
        flush=True,
    )
    print(f"[pq-cron] started_at={run_start.isoformat()}Z", flush=True)

    total_inserted = 0
    total_updated = 0
    total_errors = 0
    total_tagged = 0
    tag_errors = 0
    tag_aborted = ""
    tag_notes = ""
    passes: dict[str, dict] = {}
    run_record_id: int | None = None
    fatal_error: str | None = None
    notes: str | None = None
    skipped = False
    prev_statuses: list[str] | None = None
    chunk_progress: dict = {}

    with app.app_context():
        from sqlalchemy import text as sqla_text
        safe_url = db.engine.url.render_as_string(hide_password=True)
        print(f"[pq-cron] DB engine URL (password hidden): {safe_url}", flush=True)
        if not args.quick_check:
            try:
                row = db.session.execute(
                    sqla_text("SELECT inet_server_addr()::text, current_database()")
                ).fetchone()
                print(f"[pq-cron] DB host: {row[0]}, database: {row[1]}", flush=True)
            except Exception as _e:
                db.session.rollback()
                print(f"[pq-cron] Could not query DB host: {_e}", flush=True)

        if args.dry_run:
            print("[pq-cron] Dry run: no run record, nothing written", flush=True)
        else:
            run_record = HaCronRun(
                service_name=args.service_name,
                started_at=run_start,
                days_window=days_window,
                status="running",
                notes=_QUICK_NOTE if args.quick_check else None,
            )
            db.session.add(run_record)
            try:
                db.session.commit()
                run_record_id = run_record.id
            except Exception:
                db.session.rollback()

            _prune_old_runs()

        try:
            if args.quick_check:
                prev_statuses = _previous_statuses(args.service_name, run_record_id)
                res = _run_quick_check(args, today, run_record_id, passes)
                notes = res["notes"]
                skipped = res["skipped"]
            elif chunked:
                _wait_for_quick_check(run_record_id)
                res = _run_chunked(args, run_record_id, passes, chunk_progress)
                notes = res.get("notes")
                skipped = res.get("skipped", False)
            else:
                _wait_for_quick_check(run_record_id)
                res = _run_full(args, date_from, today, answered_days, passes,
                                corrected_days=corrected_days)
                notes = res.get("notes")
            total_inserted = res["inserted"]
            total_updated = res["updated"]
            total_tagged = res["tagged"]
            tag_errors = res["tag_errors"]
            tag_aborted = res["tag_aborted"]
            tag_notes = res["tag_notes"]
            total_errors += res["errors"]
            if res["abort"]:
                fatal_error = res["abort"]
        except QuickCheckError as exc:
            fatal_error = f"{_QUICK_NOTE}: {exc}"
            total_errors += 1
            print(f"[pq-cron] FATAL ERROR: {fatal_error}", flush=True)
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            total_errors += 1
            print(f"[pq-cron] FATAL ERROR: {fatal_error}", flush=True)
        if chunk_progress.get("notes"):
            notes = chunk_progress["notes"]   # first in the record, so a rerun can resume

        # --- Finalise run record ---
        run_end = datetime.utcnow()
        elapsed = (run_end - run_start).total_seconds()
        final_status = "failed" if (total_errors > 0 or fatal_error) else "ok"
        # A skipped quick-check checked nothing: not "ok", so it never counts
        # as an archive update or a run that succeeded.
        recorded_status = "skipped" if (skipped and final_status == "ok") else final_status

        if run_record_id:
            try:
                from sqlalchemy import select
                record = db.session.execute(
                    select(HaCronRun).where(HaCronRun.id == run_record_id)
                ).scalar_one_or_none()
                if record:
                    record.finished_at = run_end
                    record.sessions_ingested = total_inserted + total_updated
                    record.sessions_tagged = total_tagged
                    record.errors = total_errors
                    record.status = recorded_status
                    # Counts first, so scripts/pq_publication_times.py can
                    # still read them when a narrow pass fails.
                    tail = " | ".join(x for x in (tag_notes, fatal_error) if x)
                    if chunked and notes:
                        # A chunked run's progress is never cut: a rerun resumes from it.
                        record.notes = notes + (" | " + tail[:1500] if tail else "")
                    else:
                        record.notes = " | ".join(
                            x for x in (notes, tag_notes, fatal_error) if x)[:2000] or None
                    db.session.commit()
            except Exception as db_exc:
                db.session.rollback()
                print(f"[pq-cron] Warning: could not update run record: {db_exc}", flush=True)

        print(
            f"[pq-cron] === END === status={recorded_status} "
            f"inserted={total_inserted} updated={total_updated} "
            f"tagged={total_tagged} errors={total_errors} "
            f"elapsed={elapsed:.1f}s",
            flush=True,
        )
        print(f"[pq-cron] finished_at={run_end.isoformat()}Z", flush=True)

    # --- Failure alerting ---
    # One email per run. Any identity mismatch, failed row or failed API page
    # alerts (missing or mis-matched questions); tagging errors alone at 3+.
    # A quick-check emails on the 2nd failure in a row only (a lasting problem
    # repeats every 30 minutes, so it still emails once per streak).
    reasons = _alert_reasons(passes, tag_errors, fatal_error, tag_aborted)
    if reasons and args.quick_check and not _quick_check_should_alert(prev_statuses):
        print("[pq-cron] Quick-check failure — alert held (emails on the 2nd failure in a row): "
              + "; ".join(reasons), flush=True)
        reasons = []
    if reasons and args.dry_run:
        print("[pq-cron] Dry run — would alert: " + "; ".join(reasons), flush=True)
        reasons = []
    if reasons:
        alert_subject = f"PQ cron failure: {args.service_name} — {total_errors} error(s)"
        pass_lines = "".join(
            f"{label.capitalize():<10} " + " ".join(f"{k}={v}" for k, v in counts.items()) + "\n"
            for label, counts in passes.items()
        )
        alert_body = (
            f"Reasons:   {'; '.join(reasons)}\n"
            f"Service:   {args.service_name}\n"
            f"Schedule:  {run_start.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Window:    {date_from} → {window_to} ({days_window} days)\n"
            f"Inserted:  {total_inserted}\n"
            f"Updated:   {total_updated}\n"
            f"Tagged:    {total_tagged}\n"
            f"Tagging:   {tag_notes or 'not run'}\n"
            f"Errors:    {total_errors}\n"
            f"Status:    {final_status}\n"
            f"Elapsed:   {elapsed:.1f}s\n"
            f"\n{pass_lines}"
        )
        if notes:
            alert_body += f"Notes:     {notes}\n"
        if fatal_error:
            alert_body += f"\nFatal error:\n{fatal_error}"
        _send_failure_alert(args.service_name, alert_subject, alert_body)

    if final_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
