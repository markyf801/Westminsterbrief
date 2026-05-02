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
import argparse
import traceback
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")

from flask_app import app
from hansard_archive.pq_ingestor import ingest_pq_date_range
from hansard_archive.models import HaCronRun
from extensions import db

_LOG_RETENTION_DAYS = 90


def _alert_email() -> str:
    return os.environ.get("CRON_ALERT_EMAIL") or os.environ.get("ADMIN_EMAIL", "")


def _send_failure_alert(service_name: str, subject: str, body: str) -> None:
    recipient = _alert_email()
    if not recipient:
        print(f"[pq-cron] ALERT: no recipient configured (set ADMIN_EMAIL or CRON_ALERT_EMAIL)", flush=True)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="PQ Archive incremental cron")
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (default: 7)")
    parser.add_argument("--service-name", default="manual",
                        help="Service name for monitoring (pq-morning / pq-afternoon / pq-monday / backfill)")
    parser.add_argument("--no-tag", action="store_true",
                        help="Skip theme tagging (use for large backfill runs)")
    args = parser.parse_args()

    run_start = datetime.utcnow()
    today = date.today()
    date_from = today - timedelta(days=args.days - 1)

    print(
        f"[pq-cron] === START === service={args.service_name} "
        f"window={date_from}->{today} ({args.days}d) "
        f"tagging={'disabled (--no-tag)' if args.no_tag else 'enabled'}",
        flush=True,
    )
    print(f"[pq-cron] started_at={run_start.isoformat()}Z", flush=True)

    total_inserted = 0
    total_updated = 0
    total_errors = 0
    total_tagged = 0
    run_record_id: int | None = None
    fatal_error: str | None = None

    with app.app_context():
        from sqlalchemy import text as sqla_text
        safe_url = db.engine.url.render_as_string(hide_password=True)
        print(f"[pq-cron] DB engine URL (password hidden): {safe_url}", flush=True)
        try:
            row = db.session.execute(
                sqla_text("SELECT inet_server_addr()::text, current_database()")
            ).fetchone()
            print(f"[pq-cron] DB host: {row[0]}, database: {row[1]}", flush=True)
        except Exception as _e:
            print(f"[pq-cron] Could not query DB host: {_e}", flush=True)

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
            print(f"[pq-cron] Ingesting WQs {date_from} → {today}…", flush=True)
            result = ingest_pq_date_range(date_from, today, verbose=True)
            total_inserted = result["inserted"]
            total_updated = result["updated"]
            total_errors += result["errors"]
            print(
                f"[pq-cron] Ingestion done — "
                f"inserted={total_inserted} updated={total_updated} errors={result['errors']}",
                flush=True,
            )

            # --- Theme tagging ---
            if not args.no_tag:
                from hansard_archive.tagger import tag_pq_all_untagged
                new_rows = total_inserted + total_updated
                if new_rows > 0:
                    print(f"[pq-cron] Tagging up to {new_rows} PQ(s)…", flush=True)
                else:
                    print("[pq-cron] No new/updated rows — running tagger for any missed PQs…", flush=True)
                tag_result = tag_pq_all_untagged(verbose=True)
                total_tagged = tag_result.get("tagged", 0)
                tag_errors = tag_result.get("errors", 0)
                total_errors += tag_errors
                print(
                    f"[pq-cron] Tagging done — tagged={total_tagged} errors={tag_errors}",
                    flush=True,
                )
            else:
                print("[pq-cron] Tagging skipped (--no-tag)", flush=True)

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
                    if fatal_error:
                        record.notes = fatal_error[:2000]
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
    should_alert = fatal_error is not None or total_errors >= 3
    if should_alert:
        alert_subject = f"PQ cron failure: {args.service_name} — {total_errors} error(s)"
        alert_body = (
            f"Service:   {args.service_name}\n"
            f"Schedule:  {run_start.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Window:    {date_from} → {today} ({args.days} days)\n"
            f"Inserted:  {total_inserted}\n"
            f"Updated:   {total_updated}\n"
            f"Tagged:    {total_tagged}\n"
            f"Errors:    {total_errors}\n"
            f"Status:    {final_status}\n"
            f"Elapsed:   {elapsed:.1f}s\n"
        )
        if fatal_error:
            alert_body += f"\nFatal error:\n{fatal_error}"
        _send_failure_alert(args.service_name, alert_subject, alert_body)

    if final_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
