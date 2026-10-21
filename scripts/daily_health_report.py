"""
Daily site-health report — Railway cron entry point.

Runs read-only checks (data freshness, cron runs, R2 backups, live site) and
prints the report to stdout (Railway log). Emails the report via Postmark:

  - any WARN/FAIL      -> email every day
  - all OK             -> no email, except a weekly all-OK summary on Mondays
  - --always-send      -> email regardless

Exit code is 0 even when problems are found (so the cron run itself isn't
marked failed); non-zero only if the checker itself crashes or the email can't
be sent.

Usage:
  python scripts/daily_health_report.py               # check + email per policy
  python scripts/daily_health_report.py --dry-run     # print only, never email
  python scripts/daily_health_report.py --always-send
  python scripts/daily_health_report.py --skip-web    # don't GET the live site
  python scripts/daily_health_report.py --no-calendar # skip Parliament's sitting calendar

Sitting days come from Parliament's What's On API (read-only, no key); see
hansard_archive/health_checks.py for the rule and the fallback.

Railway (see docs/ops/railway-services.md):
  service health-report, Dockerfile.cron, SKIP_MIGRATIONS=1,
  schedule 30 7 * * * (UTC), start command as above.

Environment:
  DATABASE_URL          — Postgres (Variable Reference); opened read-only
  POSTMARK_SERVER_TOKEN — email; absent = log-only (email_service test mode)
  ADMIN_EMAIL           — recipient; overridden by CRON_ALERT_EMAIL if set
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL, R2_BUCKET_NAME
                        — optional; without them the R2 check is SKIPPED
  HEALTH_BASE_URL       — optional; defaults to the production site
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))  # for prune_old_backups helpers

from hansard_archive import health_checks as hc  # noqa: E402


def _log(msg: str) -> None:
    print(f"[health] {msg}", flush=True)


def _recipient() -> str:
    return os.environ.get("CRON_ALERT_EMAIL") or os.environ.get("ADMIN_EMAIL", "")


def make_engine(url: str | None = None) -> sa.Engine:
    url = url or os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    connect_args = {}
    if url.startswith("postgresql"):
        # Read-only session + 60s statement cap: this job must never write.
        connect_args = {"options": "-c default_transaction_read_only=on -c statement_timeout=60000",
                        "connect_timeout": 15}
    return sa.create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def collect(engine: sa.Engine, now: datetime, *, env: dict | None = None, skip_web: bool = False,
            no_calendar: bool = False, http_get=None, s3_factory=None) -> list[hc.CheckResult]:
    env = os.environ if env is None else env
    if http_get is None:
        import requests
        http_get = requests.get
    # Parliament sitting calendar: fetched once per run; on failure the
    # "no new data" checks fall back to the weekday rule and say so.
    if no_calendar:
        sitting_days, note = None, "--no-calendar"
    else:
        sitting_days, note = hc.fetch_sitting_days(http_get, now.date())
    if note:
        _log(f"Sitting calendar: {note}; using weekday fallback rule")
    results: list[hc.CheckResult] = []
    try:
        with engine.connect() as conn:
            ctx = hc.HealthContext(conn, now, sitting_days=sitting_days, calendar_note=note)
            results.extend(hc.run_db_checks(ctx))
    except Exception as exc:  # noqa: BLE001 — DB unreachable is a finding, not a crash
        results.append(hc.CheckResult("Database connection", hc.FAIL,
                                      f"{type(exc).__name__}: {str(exc)[:200]}"))
    results.append(hc.check_r2_backup(env, now, s3_factory=s3_factory))
    if skip_web:
        results.append(hc.CheckResult("Web", hc.SKIPPED, "--skip-web"))
    else:
        config = dict(hc.CONFIG)
        config["web"] = {**hc.CONFIG["web"], "base_url": env.get("HEALTH_BASE_URL") or hc.CONFIG["web"]["base_url"]}
        results.extend(hc.check_web(http_get, config))
    return results


def main(argv: list[str] | None = None, *, engine: sa.Engine | None = None, now: datetime | None = None,
         sender=None, http_get=None, s3_factory=None, env: dict | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily Westminster Brief health report")
    parser.add_argument("--dry-run", action="store_true", help="Print the report; never email")
    parser.add_argument("--always-send", action="store_true", help="Email even when all checks are OK")
    parser.add_argument("--skip-web", action="store_true", help="Don't GET the live site")
    parser.add_argument("--no-calendar", action="store_true",
                        help="Don't fetch Parliament's sitting calendar (use the weekday rule)")
    args = parser.parse_args(argv)

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if engine is None and not os.environ.get("DATABASE_URL"):
        _log("ERROR: DATABASE_URL is not set")
        return 1
    try:
        engine = engine or make_engine()
        results = collect(engine, now, env=env, skip_web=args.skip_web, no_calendar=args.no_calendar,
                          http_get=http_get, s3_factory=s3_factory)
        subject = hc.subject_for(results)
        text_body = hc.render_text(results, now)
        print(text_body, flush=True)

        send, reason = hc.should_send(results, now, always_send=args.always_send)
        if args.dry_run:
            _log(f"--dry-run: not emailing (would {'send' if send else 'skip'}: {reason})")
            return 0
        if not send:
            _log(f"No email: {reason}")
            return 0
        to = _recipient()
        if not to:
            _log("ERROR: no recipient (set ADMIN_EMAIL or CRON_ALERT_EMAIL)")
            return 1
        if sender is None:
            from email_service import send_email as sender
        if not sender(to=to, subject=subject, html_body=hc.render_html(results, now), text_body=text_body):
            _log("ERROR: email send failed")
            return 1
        _log(f"Email sent ({reason}): {subject}")
        return 0
    except Exception:  # noqa: BLE001 — the checker itself broke
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        if not args.dry_run and _recipient():
            try:
                if sender is None:
                    from email_service import send_email as sender
                sender(to=_recipient(), subject="[Westminster Brief] Daily health check crashed",
                       html_body=f"<pre>{tb[-3000:]}</pre>", text_body=tb[-3000:])
            except Exception:  # noqa: BLE001
                pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
