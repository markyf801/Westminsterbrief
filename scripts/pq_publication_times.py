"""
When does Parliament publish written questions and answers? Read-only report.

Reads the counts that `ingest_pq_cron.py --quick-check` stores in
ha_cron_run.notes (one observation per run) and summarises, by weekday and in
UK time:
  - earliest / median / latest time new questions and new answers were first seen
  - % of publication days with something published in each 30-minute slot
  - a recommended quick-check window (slots holding 95% of what was published,
    plus one slot either side) as Railway cron lines (UTC)

Parliament's API has dates only — no times — so there is nothing to backfill
from the API. A coarse "stored at" section uses ha_pq.ingested_at /
updated_at; the old crons ran a few times a day, so those times are upper
bounds, useful only until quick-check data builds up.

The recommendation is flagged "not enough data yet" until every weekday
Mon-Thu has 4+ publication days (about 4 sitting weeks). ha_cron_run keeps
90 days, so --weeks above 12 adds nothing.

Usage:
  python scripts/pq_publication_times.py                 # last 8 weeks
  python scripts/pq_publication_times.py --weeks 4 --json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")
os.environ.setdefault("SKIP_MIGRATIONS", "1")   # read-only: no startup schema work


def _load(weeks: int, service_name: str | None):
    from extensions import db
    from hansard_archive.models import HaCronRun, HaPQ
    from hansard_archive.pq_publication import parse_counts

    since = datetime.utcnow() - timedelta(weeks=weeks)
    query = HaCronRun.query.filter(HaCronRun.started_at >= since,
                                   HaCronRun.notes.like("quick-check:%"))
    if service_name:
        query = query.filter(HaCronRun.service_name == service_name)
    observations = []
    for run in query.order_by(HaCronRun.started_at):
        observations.extend(parse_counts(run.notes, run.started_at))

    since_date = since.date()
    questions = (db.session.query(HaPQ.tabled_date, HaPQ.ingested_at)
                 .filter(HaPQ.tabled_date >= since_date).all())
    answers = (db.session.query(HaPQ.answer_date, HaPQ.updated_at)
               .filter(HaPQ.answer_date >= since_date).all())
    db.session.rollback()
    return observations, questions, answers


def build_report(weeks: int, service_name: str | None = None) -> dict:
    from hansard_archive.pq_publication import find_events, stored_time_estimate, summarise

    observations, questions, answers = _load(weeks, service_name)
    events, first_check = find_events(observations)
    report = summarise(events, first_check)
    report["weeks"] = weeks
    report["observations"] = len(observations)
    report["stored_at_estimate"] = {
        "questions": stored_time_estimate(questions),
        "answers": stored_time_estimate(answers, same_day=True),
    }
    return report


def _print(report: dict) -> None:
    from hansard_archive.pq_publication import MIN_DAYS_PER_WEEKDAY

    print(f"PQ publication times — last {report['weeks']} weeks, "
          f"{report['observations']} quick-check observations (times are UK local)\n")
    names = {"questions": "New questions (tabled list)",
             "answers": "New answers (answered today)",
             "late_answers": "Late answers (dated yesterday, seen today)"}
    for cat, label in names.items():
        data = report["categories"][cat]
        print(f"== {label}: {data['days_with_publication']} publication day(s)")
        if data["censored_days"]:
            print(f"   {data['censored_days']} day(s) already published at the first check — "
                  "start checks earlier to see the real time")
        for wd, w in data["weekdays"].items():
            slots = ", ".join(f"{s} {p}%" for s, p in w["slot_pct"].items()) or "-"
            print(f"   {wd}: days={w['days_with_publication']}/{w['days_checked']} "
                  f"first seen earliest={w['first_seen_earliest']} "
                  f"median={w['first_seen_median']} latest={w['first_seen_latest']}")
            print(f"        slots: {slots}")
        if data["cover"]:
            print(f"   95% of items published {data['cover'][0]}–{data['cover'][1]} slot(s)")
        print()

    est = report["stored_at_estimate"]
    print("== Coarse estimate from existing rows (first stored at, UK; upper bounds)")
    for cat in ("questions", "answers"):
        slots = ", ".join(f"{s} {n}" for s, n in est[cat].items()) or "-"
        print(f"   {cat}: {slots}")
    print()

    rec = report["recommendation"]
    if not rec:
        print("No recommendation: no quick-check publication events recorded yet.")
        return
    status = ("" if report["enough_data"] else
              f"  (NOT ENOUGH DATA YET — need {MIN_DAYS_PER_WEEKDAY}+ publication days per weekday Mon-Thu)")
    print(f"Recommended quick-check window (UK): {rec['uk_window']}{status}")
    print(f"   Railway cron (UTC) during GMT: {rec['cron_utc_gmt']}")
    print(f"   Railway cron (UTC) during BST: {rec['cron_utc_bst']}")
    print(f"   One line for both:             {rec['cron_utc_both']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PQ publication-time report (read-only)")
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--service-name", default=None,
                        help="Only this quick-check service (default: all quick-check runs)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from flask_app import app
    with app.app_context():
        report = build_report(args.weeks, args.service_name)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
