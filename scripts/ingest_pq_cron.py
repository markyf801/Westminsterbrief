"""
PQ Archive incremental ingestion — cron entry point.

Fetches the last N days of Written Questions from Parliament's WQ API,
upserts into ha_pq, then theme-tags any untagged PQs.

Three Railway Cron Job services use this script:

  pq-cron-morning    0 11 * * 1-5   11:00 UTC Mon-Fri  (first WQ publication window)
  pq-cron-afternoon  0 14 * * 1-5   14:00 UTC Mon-Fri  (answer publication window)
  pq-cron-monday     0 9 * * 1      09:00 UTC Monday   (catch recess backlog)

Usage:
  python scripts/ingest_pq_cron.py                            # defaults (7d, inline tag)
  python scripts/ingest_pq_cron.py --days 7 --service-name pq-morning
  python scripts/ingest_pq_cron.py --days 365 --service-name backfill --no-tag
  python scripts/ingest_pq_cron.py --days 1 --answered-days 120 --no-tag   # answer catch-up
  python scripts/ingest_pq_cron.py --quick-check --service-name pq-quickcheck  # frequent check

Two passes: questions TABLED in the last --days, then questions ANSWERED in the
last --answered-days (default: same as --days). Answers usually arrive 1-4 weeks
after tabling, so the tabled window alone leaves answered PQs showing
"Awaiting answer". The answered pass only re-fetches the full record for rows
whose answer is new or changed.

--quick-check (opt-in; the scheduled full sweeps don't use it) asks the API for
counts only (take=1, totalResults): questions tabled in the last
--quick-tabled-days, and questions answered on each of the last
--quick-answered-days. The counts are kept in ha_cron_run.notes, which
scripts/pq_publication_times.py reads to find when Parliament publishes
(the API itself has dates only, no times). A window is ingested when the API
count is above the matching ha_pq count (answers count only when their full
text is stored — answer_needs_fetch_sql), or above the API count the last
successful check saw for the same window (so extra rows on our side can't hide
new ones). Otherwise it logs one line and exits in a few seconds. It skips when
another PQ run is in progress. Changes that leave every count the same (a
corrected answer, a withdrawal) are left to the full sweeps.

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
    WQ_API_BASE, PQIngestAborted, answer_needs_fetch_sql, ingest_pq_date_range,
)
from hansard_archive.models import HaCronRun, HaPQ
from hansard_archive.pq_publication import format_counts, parse_counts
from extensions import db

_LOG_RETENTION_DAYS = 90
_PASS_KEYS = ("inserted", "updated", "unchanged", "identity_mismatch",
              "failed", "page_errors", "rolled_back", "errors")

_QUICK_NOTE = "quick-check"     # HaCronRun.notes prefix for quick-check runs
_QUICK_TIMEOUT = 30
_QUICK_RETRIES = 2              # one retry on a transient error (CLAUDE.md)
_ACTIVE_RUN_MINUTES = 60        # a "running" record older than this is treated as dead
_FULL_WAIT_SECONDS = 15 * 60    # a full sweep waits this long for a running quick-check
_FULL_WAIT_POLL = 30


class QuickCheckError(RuntimeError):
    """A count request to Parliament's API failed."""


def _run_pass(label: str, date_from: date, date_to: date, **kwargs) -> tuple[dict, str | None]:
    """Run one ingest pass. Returns (counts, abort_message).

    An aborted pass (PQIngestAborted) still returns the counts it reached, so
    the run summary and the alert show what happened before it stopped."""
    try:
        counts = ingest_pq_date_range(date_from, date_to, verbose=True, **kwargs)
        abort = None
    except PQIngestAborted as exc:
        counts, abort = exc.counts, f"{type(exc).__name__}: {exc}"
        print(f"[pq-cron] {label} ABORTED: {abort}", flush=True)
    counts = {k: counts.get(k, 0) for k in _PASS_KEYS}
    print(
        f"[pq-cron] {label} done — "
        f"inserted={counts['inserted']} updated={counts['updated']} "
        f"unchanged={counts['unchanged']} "
        f"identity_mismatch={counts['identity_mismatch']} "
        f"failed={counts['failed']} page_errors={counts['page_errors']} "
        f"rolled_back={counts['rolled_back']} errors={counts['errors']}",
        flush=True,
    )
    return counts, abort


def _alert_reasons(passes: dict, tag_errors: int, fatal_error: str | None) -> list[str]:
    """Why this run needs an alert email (empty list = no alert)."""
    reasons = []
    if fatal_error:
        reasons.append("fatal error")
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


def _running_query(exclude_id: int | None, quick_only: bool):
    cutoff = datetime.utcnow() - timedelta(minutes=_ACTIVE_RUN_MINUTES)
    query = HaCronRun.query.filter(HaCronRun.status == "running",
                                   HaCronRun.started_at >= cutoff)
    if quick_only:
        query = query.filter(HaCronRun.notes.like(f"{_QUICK_NOTE}%"))
    else:
        query = query.filter(or_(HaCronRun.service_name.like("pq-%"),
                                 HaCronRun.notes.like(f"{_QUICK_NOTE}%")))
    if exclude_id:
        query = query.filter(HaCronRun.id != exclude_id)
    return query


def _wait_for_quick_check(exclude_id: int | None) -> None:
    """A full sweep waits (up to _FULL_WAIT_SECONDS) for a running quick-check
    so the two don't insert the same new rows at the same time."""
    waited = 0
    while True:
        try:
            db.session.commit()   # fresh snapshot each poll
            if _running_query(exclude_id, quick_only=True).count() == 0:
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
    """Statuses of this service's previous runs, newest first. None if unreadable."""
    try:
        query = HaCronRun.query.filter(HaCronRun.service_name == service_name)
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
    "tagged", "tag_errors", "errors", "abort", "notes"}. Raises
    QuickCheckError when a count request fails — deliberately no fallback to a
    full run: if the API is failing, a 7-day sweep only hammers it, and the next
    check is 30 minutes away.
    """
    out = {"inserted": 0, "updated": 0, "tagged": 0, "tag_errors": 0, "errors": 0,
           "abort": None}

    active = _running_query(run_record_id, quick_only=False).order_by(
        HaCronRun.started_at.desc()).first()
    if active is not None:
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
        res, abort = _run_pass(f"{kind.capitalize()} pass", d_from, d_to,
                               skip_answer_fetch=args.skip_answer_fetch,
                               date_field=kind,
                               fetch_only_new_answers=True)
        passes[kind] = res
        out["inserted"] += res["inserted"]
        out["updated"] += res["updated"]
        out["errors"] += res["errors"]
        ran.append(f"{kind} {d_from}..{d_to}")
        if abort:
            out["abort"] = abort   # API down or identities disagree: stop here
            break

    new_rows = out["inserted"] + out["updated"]
    if out["abort"]:
        print("[pq-cron] Tagging skipped (ingest aborted)", flush=True)
    elif new_rows:
        out["tagged"], out["tag_errors"] = _run_tagger(args.no_tag, new_rows)
        out["errors"] += out["tag_errors"]

    out["notes"] = (f"{_QUICK_NOTE}: ran {', '.join(ran)} "
                    f"ins={out['inserted']} upd={out['updated']} ({summary})")
    return out


# ── shared ───────────────────────────────────────────────────────────────────

def _run_tagger(no_tag: bool, new_rows: int) -> tuple[int, int]:
    """Theme-tag untagged PQs. Returns (tagged, errors)."""
    if no_tag:
        print("[pq-cron] Tagging skipped (--no-tag)", flush=True)
        return 0, 0
    from hansard_archive.tagger import tag_pq_all_untagged
    if new_rows > 0:
        print(f"[pq-cron] Tagging up to {new_rows} PQ(s)…", flush=True)
    else:
        print("[pq-cron] No new/updated rows — running tagger for any missed PQs…", flush=True)
    tag_result = tag_pq_all_untagged(verbose=True)
    tagged = tag_result.get("tagged", 0)
    tag_errors = tag_result.get("errors", 0)
    print(f"[pq-cron] Tagging done — tagged={tagged} errors={tag_errors}", flush=True)
    return tagged, tag_errors


def _run_full(args, date_from: date, today: date, answered_days: int, passes: dict) -> dict:
    """The scheduled sweep: tabled window, then answered window, then tagging.
    Returns {"inserted", "updated", "tagged", "tag_errors", "errors", "abort"}."""
    # --- Ingestion ---
    print(f"[pq-cron] Ingesting WQs {date_from} → {today}…", flush=True)
    result, abort = _run_pass("Tabled pass", date_from, today,
                              skip_answer_fetch=args.skip_answer_fetch)
    passes["tabled"] = result
    out = {"inserted": result["inserted"], "updated": result["updated"],
           "errors": result["errors"], "tagged": 0, "tag_errors": 0}

    # --- Answers to older questions (tabled before the window) ---
    # Skipped after an abort: the API is down or identities disagree.
    if answered_days > 0 and not abort:
        answered_from = today - timedelta(days=answered_days - 1)
        print(f"[pq-cron] Refreshing WQs answered {answered_from} → {today}…", flush=True)
        ans, abort = _run_pass("Answered pass", answered_from, today,
                               skip_answer_fetch=args.skip_answer_fetch,
                               date_field="answered",
                               fetch_only_new_answers=True)
        passes["answered"] = ans
        out["inserted"] += ans["inserted"]
        out["updated"] += ans["updated"]
        out["errors"] += ans["errors"]
    out["abort"] = abort

    # --- Theme tagging ---
    if abort and not args.no_tag:
        print("[pq-cron] Tagging skipped (ingest aborted)", flush=True)
    else:
        out["tagged"], out["tag_errors"] = _run_tagger(args.no_tag, out["inserted"] + out["updated"])
        out["errors"] += out["tag_errors"]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="PQ Archive incremental cron")
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (default: 7)")
    parser.add_argument("--service-name", default="manual",
                        help="Service name for monitoring (pq-morning / pq-afternoon / pq-monday / backfill)")
    parser.add_argument("--no-tag", action="store_true",
                        help="Skip theme tagging (use for large backfill runs)")
    parser.add_argument("--answered-days", type=int, default=None,
                        help="Also refresh questions ANSWERED in the last N days "
                             "(default: same as --days; 0 = off)")
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
    args = parser.parse_args()
    if args.quick_tabled_days < 1 or args.quick_answered_days < 0:
        parser.error("--quick-tabled-days must be >= 1 and --quick-answered-days >= 0")

    run_start = datetime.utcnow()
    today = date.today()
    days_window = args.quick_tabled_days if args.quick_check else args.days
    date_from = today - timedelta(days=days_window - 1)
    answered_days = args.days if args.answered_days is None else args.answered_days

    print(
        f"[pq-cron] === START === service={args.service_name} "
        f"mode={'quick-check' if args.quick_check else 'full'} "
        f"window={date_from}->{today} ({days_window}d) "
        f"tagging={'disabled (--no-tag)' if args.no_tag else 'enabled'}",
        flush=True,
    )
    print(f"[pq-cron] started_at={run_start.isoformat()}Z", flush=True)

    total_inserted = 0
    total_updated = 0
    total_errors = 0
    total_tagged = 0
    tag_errors = 0
    passes: dict[str, dict] = {}
    run_record_id: int | None = None
    fatal_error: str | None = None
    notes: str | None = None
    prev_statuses: list[str] | None = None

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
            else:
                _wait_for_quick_check(run_record_id)
                res = _run_full(args, date_from, today, answered_days, passes)
            total_inserted = res["inserted"]
            total_updated = res["updated"]
            total_tagged = res["tagged"]
            tag_errors = res["tag_errors"]
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

        # --- Finalise run record ---
        run_end = datetime.utcnow()
        elapsed = (run_end - run_start).total_seconds()
        final_status = "failed" if (total_errors > 0 or fatal_error) else "ok"

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
                    record.status = final_status
                    # Counts first, so scripts/pq_publication_times.py can
                    # still read them when a narrow pass fails.
                    record.notes = " | ".join(x for x in (notes, fatal_error) if x)[:2000] or None
                    db.session.commit()
            except Exception as db_exc:
                db.session.rollback()
                print(f"[pq-cron] Warning: could not update run record: {db_exc}", flush=True)

        print(
            f"[pq-cron] === END === status={final_status} "
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
    reasons = _alert_reasons(passes, tag_errors, fatal_error)
    if reasons and args.quick_check and not _quick_check_should_alert(prev_statuses):
        print("[pq-cron] Quick-check failure — alert held (emails on the 2nd failure in a row): "
              + "; ".join(reasons), flush=True)
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
            f"Window:    {date_from} → {today} ({days_window} days)\n"
            f"Inserted:  {total_inserted}\n"
            f"Updated:   {total_updated}\n"
            f"Tagged:    {total_tagged}\n"
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
