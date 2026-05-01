"""
Hansard Archive incremental ingestion — cron entry point.

Ingest the last N days for both Commons and Lords, then theme-tag any newly
ingested sessions. Designed to run hourly during sitting-day windows.
Safe to re-run: sessions already in the DB are skipped; already-tagged
sessions are skipped by the tagger.

Three Railway Cron Job services use this script with different schedules:

  morning-catchup  0 8 * * 1-5     08:00 UTC Mon-Fri
  daytime-mth      0 11-23 * * 1-4  hourly Mon-Thu 11:00-23:00 UTC
  daytime-fri      0 9-19 * * 5    hourly Fri 09:00-19:00 UTC

Usage:
  python scripts/archive_cron.py                                     # defaults
  python scripts/archive_cron.py --days 1 --service-name morning-catchup
  python scripts/archive_cron.py --days 3 --service-name daytime-mth

Environment:
  DATABASE_URL          — Postgres (Railway Variable Reference); falls back to SQLite
  GEMINI_API_KEY        — required for theme tagging
  POSTMARK_SERVER_TOKEN — required for email alerts; absent = log-only
  ADMIN_EMAIL           — alert recipient; overridden by CRON_ALERT_EMAIL if set
  CRON_ALERT_EMAIL      — explicit alert recipient (optional)
  EMAIL_TEST_MODE=true  — disable real emails in local dev
"""

import os
import sys
import argparse
import traceback
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")

from flask_app import app
from hansard_archive.ingestion import ingest_date_range
from hansard_archive.tagger import tag_all_untagged
from hansard_archive.models import HaCronRun
from extensions import db

_LOG_RETENTION_DAYS = 90


def _alert_email() -> str:
    return os.environ.get("CRON_ALERT_EMAIL") or os.environ.get("ADMIN_EMAIL", "")


def _send_failure_alert(service_name: str, subject: str, body: str) -> None:
    """Send a failure alert email. Silently no-ops if Postmark isn't configured."""
    recipient = _alert_email()
    if not recipient:
        print(f"[cron] ALERT: no recipient configured (set ADMIN_EMAIL or CRON_ALERT_EMAIL)", flush=True)
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
        print(f"[cron] Alert email sent to {recipient}", flush=True)
    except Exception as exc:
        print(f"[cron] Failed to send alert email: {exc}", flush=True)


def _prune_old_runs() -> None:
    """Delete cron run records older than _LOG_RETENTION_DAYS."""
    cutoff = datetime.utcnow() - timedelta(days=_LOG_RETENTION_DAYS)
    try:
        deleted = HaCronRun.query.filter(HaCronRun.started_at < cutoff).delete()
        db.session.commit()
        if deleted:
            print(f"[cron] Pruned {deleted} old run records (>{_LOG_RETENTION_DAYS}d)", flush=True)
    except Exception as exc:
        print(f"[cron] Warning: could not prune old runs: {exc}", flush=True)
        db.session.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hansard Archive incremental cron")
    parser.add_argument("--days", type=int, default=3,
                        help="Lookback window in days (default: 3)")
    parser.add_argument("--service-name", default="manual",
                        help="Railway service name for monitoring (morning-catchup / daytime-mth / daytime-fri)")
    args = parser.parse_args()

    is_morning_catchup = args.service_name == "morning-catchup"
    run_start = datetime.utcnow()
    today = date.today()
    start = today - timedelta(days=args.days - 1)

    print(f"[cron] === START === service={args.service_name} window={start}->{today} ({args.days}d)", flush=True)
    print(f"[cron] started_at={run_start.isoformat()}Z", flush=True)

    total_sessions = 0
    total_errors = 0
    total_tagged = 0
    run_record_id: int | None = None
    fatal_error: str | None = None

    with app.app_context():
        # Create run record
        run_record = HaCronRun(
            service_name=args.service_name,
            started_at=run_start,
            days_window=args.days,
            status="running",
        )
        db.session.add(run_record)
        try:
            db.session.commit()
            run_record_id = run_record.id
        except Exception:
            db.session.rollback()

        _prune_old_runs()

        try:
            # --- Ingestion ---
            for house in ("Commons", "Lords"):
                print(f"[cron] === {house} ===", flush=True)
                result = ingest_date_range(start, today, house=house, verbose=True)
                house_sessions = result["total_sessions"]
                house_errors = result["errors"]
                total_sessions += house_sessions
                total_errors += house_errors
                print(
                    f"[cron] {house} done — "
                    f"{house_sessions} new sessions, "
                    f"{result['sitting_days']} sitting day(s), "
                    f"{house_errors} error(s)",
                    flush=True,
                )

            # --- Theme tagging ---
            if total_sessions > 0:
                print(f"[cron] Tagging {total_sessions} new session(s)…", flush=True)
            else:
                print(f"[cron] No new sessions — running tagger for any missed sessions…", flush=True)

            tag_result = tag_all_untagged(verbose=True)
            total_tagged = tag_result.get("tagged", 0)
            tag_errors = tag_result.get("errors", 0)
            total_errors += tag_errors
            print(
                f"[cron] Tagging done — {total_tagged} session(s) tagged, {tag_errors} error(s)",
                flush=True,
            )

        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            total_errors += 1
            print(f"[cron] FATAL ERROR: {fatal_error}", flush=True)

        # --- Finalise run record ---
        run_end = datetime.utcnow()
        elapsed = (run_end - run_start).total_seconds()
        final_status = "failed" if (total_errors > 0 or fatal_error) else "ok"

        if run_record_id:
            try:
                record = HaCronRun.query.get(run_record_id)
                if record:
                    record.finished_at = run_end
                    record.sessions_ingested = total_sessions
                    record.sessions_tagged = total_tagged
                    record.errors = total_errors
                    record.status = final_status
                    if fatal_error:
                        record.notes = fatal_error[:2000]
                    db.session.commit()
            except Exception as db_exc:
                db.session.rollback()
                print(f"[cron] Warning: could not update run record: {db_exc}", flush=True)

        # --- Summary ---
        print(
            f"[cron] === END === status={final_status} "
            f"ingested={total_sessions} tagged={total_tagged} errors={total_errors} "
            f"elapsed={elapsed:.1f}s",
            flush=True,
        )
        print(f"[cron] finished_at={run_end.isoformat()}Z", flush=True)

    # --- Failure alerting (outside app_context so send_email works cleanly) ---
    should_alert = (
        fatal_error is not None                      # always alert on crash
        or (is_morning_catchup and total_errors > 0) # morning catch-up: alert on any error
        or total_errors >= 3                         # other services: alert if 3+ errors
    )
    if should_alert:
        alert_subject = f"Cron failure: {args.service_name} — {total_errors} error(s)"
        alert_body = (
            f"Service:   {args.service_name}\n"
            f"Schedule:  {run_start.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Window:    {start} → {today} ({args.days} days)\n"
            f"Ingested:  {total_sessions} sessions\n"
            f"Tagged:    {total_tagged} sessions\n"
            f"Errors:    {total_errors}\n"
            f"Status:    {final_status}\n"
            f"Elapsed:   {elapsed:.1f}s\n"
        )
        if fatal_error:
            alert_body += f"\nFatal error:\n{fatal_error}"
        _send_failure_alert(args.service_name, alert_subject, alert_body)

    # Exit non-zero on failure so Railway marks the run as failed in dashboard
    if final_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
