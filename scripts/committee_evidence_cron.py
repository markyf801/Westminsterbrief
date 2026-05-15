"""
Stakeholder Directory — committee evidence incremental cron.

Fetches new oral and written committee evidence from the Parliament committees
API, stages it, and runs normalisation. Runs incrementally using per-committee
high-water marks so each run only fetches new evidence.

Railway cron service: committee-evidence-cron
  Cron: 0 6 * * 1   (06:00 UTC Monday — weekly)

Usage:
  python scripts/committee_evidence_cron.py
  python scripts/committee_evidence_cron.py --service-name committee-evidence-cron

Environment:
  DATABASE_URL          — Postgres (Railway Variable Reference)
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
from hansard_archive.models import HaCronRun
from extensions import db

_FALLBACK_START = date(2024, 1, 1)
_LOG_RETENTION_DAYS = 90


def _alert_email() -> str:
    return os.environ.get("CRON_ALERT_EMAIL") or os.environ.get("ADMIN_EMAIL", "")


def _send_alert(service_name: str, subject: str, body: str) -> None:
    recipient = _alert_email()
    if not recipient:
        print(f"[ce-cron] ALERT: no recipient configured (set ADMIN_EMAIL or CRON_ALERT_EMAIL)", flush=True)
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
        print(f"[ce-cron] Email sent to {recipient}", flush=True)
    except Exception as exc:
        print(f"[ce-cron] Failed to send email: {exc}", flush=True)


def _fetch_all_committee_ids() -> list[int]:
    import requests
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://committees-api.parliament.uk/api/Committees",
                params={"status": "Current", "take": 300},
                timeout=30,
            )
            resp.raise_for_status()
            return [item["id"] for item in resp.json().get("items", [])]
        except Exception as exc:
            if attempt == 2:
                raise
            import time
            time.sleep(3)
    return []


def _prune_old_runs() -> None:
    cutoff = datetime.utcnow() - timedelta(days=_LOG_RETENTION_DAYS)
    try:
        deleted = HaCronRun.query.filter(
            HaCronRun.started_at < cutoff,
            HaCronRun.service_name.like("committee-%"),
        ).delete()
        db.session.commit()
        if deleted:
            print(f"[ce-cron] Pruned {deleted} old run records (>{_LOG_RETENTION_DAYS}d)", flush=True)
    except Exception as exc:
        print(f"[ce-cron] Warning: could not prune old runs: {exc}", flush=True)
        db.session.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description="Committee evidence incremental cron")
    parser.add_argument("--service-name", default="committee-evidence-cron",
                        help="Service name for monitoring")
    args = parser.parse_args()

    run_start = datetime.utcnow()
    today = date.today()

    print(f"[ce-cron] === START === service={args.service_name} date={today}", flush=True)
    print(f"[ce-cron] started_at={run_start.isoformat()}Z", flush=True)

    total_publications = 0
    total_staged = 0
    total_errors = 0
    run_record_id: int | None = None
    fatal_error: str | None = None
    elapsed = 0.0

    with app.app_context():
        run_record = HaCronRun(
            service_name=args.service_name,
            started_at=run_start,
            days_window=7,
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
            # Fetch current committee IDs from API
            print("[ce-cron] Fetching current committee list…", flush=True)
            committee_ids = _fetch_all_committee_ids()
            print(f"[ce-cron] {len(committee_ids)} active committees", flush=True)

            # Compute per-committee incremental start dates
            from stakeholder_directory.ingesters.committee_evidence import (
                get_incremental_start_dates,
                ingest_committee_evidence,
            )
            per_start = get_incremental_start_dates(committee_ids, fallback_start=_FALLBACK_START)
            new_count = sum(1 for cid in committee_ids if per_start[cid] == _FALLBACK_START)
            incremental_count = len(committee_ids) - new_count
            print(
                f"[ce-cron] {incremental_count} committees updating since last run, "
                f"{new_count} fetching from scratch",
                flush=True,
            )

            import time as _t
            t0 = _t.monotonic()
            result = ingest_committee_evidence(
                committee_ids,
                start_date=_FALLBACK_START,
                end_date=today,
                per_committee_start_dates=per_start,
            )
            elapsed = _t.monotonic() - t0

            total_publications = result.publications_fetched
            total_staged = result.rows_staged
            total_errors += len(result.errors)
            for err in result.errors[:10]:
                print(f"[ce-cron] ERROR: {err}", flush=True)

            print(
                f"[ce-cron] Ingestion done — "
                f"publications={total_publications} staged={total_staged} "
                f"skipped_govt={result.rows_skipped_internal_govt} "
                f"skipped_dup={result.rows_skipped_duplicate} "
                f"errors={len(result.errors)} elapsed={elapsed:.0f}s",
                flush=True,
            )

            # Normalise newly staged records
            from stakeholder_directory.normalisation.normaliser import normalise_pending_staging
            norm = normalise_pending_staging("staging_committee_evidence", batch_size=5000)
            print(f"[ce-cron] Normalisation done — {norm}", flush=True)

        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            total_errors += 1
            print(f"[ce-cron] FATAL ERROR: {fatal_error}", flush=True)

        # Finalise run record
        run_end = datetime.utcnow()
        elapsed_total = (run_end - run_start).total_seconds()
        final_status = "failed" if (total_errors > 0 or fatal_error) else "ok"

        if run_record_id:
            try:
                from sqlalchemy import select
                record = db.session.execute(
                    select(HaCronRun).where(HaCronRun.id == run_record_id)
                ).scalar_one_or_none()
                if record:
                    record.finished_at = run_end
                    record.sessions_ingested = total_publications
                    record.sessions_tagged = total_staged
                    record.errors = total_errors
                    record.status = final_status
                    if fatal_error:
                        record.notes = fatal_error[:2000]
                    db.session.commit()
            except Exception as db_exc:
                db.session.rollback()
                print(f"[ce-cron] Warning: could not update run record: {db_exc}", flush=True)

        print(
            f"[ce-cron] === END === status={final_status} "
            f"publications={total_publications} staged={total_staged} "
            f"errors={total_errors} elapsed={elapsed_total:.0f}s",
            flush=True,
        )
        print(f"[ce-cron] finished_at={run_end.isoformat()}Z", flush=True)

    # Email on failure always; on success only if new evidence was staged
    # (suppresses weekly noise during parliamentary recess when 0 publications are found)
    should_email = (final_status == "failed") or (final_status == "ok" and total_staged > 0)
    recipient = _alert_email()
    if recipient and should_email:
        if final_status == "ok":
            subject = f"Committee evidence ingested: {total_publications} publications, {total_staged} staged"
            status_line = "completed successfully"
        else:
            subject = f"Committee evidence cron FAILED — {total_errors} error(s)"
            status_line = f"FAILED ({total_errors} error(s))"
        body = (
            f"Service:      {args.service_name}\n"
            f"Status:       {status_line}\n"
            f"Run date:     {run_start.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Publications: {total_publications}\n"
            f"Staged:       {total_staged}\n"
            f"Errors:       {total_errors}\n"
            f"Elapsed:      {elapsed_total:.0f}s\n"
        )
        if fatal_error:
            body += f"\nFatal error:\n{fatal_error}"
        _send_alert(args.service_name, subject, body)
    elif final_status == "ok" and total_staged == 0:
        print("[ce-cron] No new evidence staged — email suppressed (likely recess or no new publications)", flush=True)

    if final_status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
