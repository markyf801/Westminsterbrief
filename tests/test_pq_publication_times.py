"""
PQ publication-time evidence: the counts format, event detection, the weekday
summary / recommended window, and the read-only report script.

Pure functions plus one SQLite in-memory run of scripts/pq_publication_times.py.
"""

import importlib.util
import json
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from hansard_archive.pq_publication import (
    Observation, find_events, format_counts, parse_counts, recommend_cron,
    stored_time_estimate, summarise, to_uk, uk_offset_hours,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _obs(at, kind, d_from, d_to, api, db):
    return Observation(at, kind, d_from, d_to, api, db)


def _day_series(day, publish_q_at_utc, answers_by_utc):
    """Quick-checks at :15/:45 from 06:15 to 18:45 UTC on `day`.
    Questions jump by 600 at publish_q_at_utc; answers grow per answers_by_utc."""
    obs = []
    q_api = 1000
    synced = 1000
    a_api = 0
    t = datetime.combine(day, datetime.min.time()) + timedelta(hours=6, minutes=15)
    end = datetime.combine(day, datetime.min.time()) + timedelta(hours=18, minutes=46)
    while t < end:
        if t >= publish_q_at_utc and q_api == 1000:
            q_api = 1600
        a_api = sum(n for at, n in answers_by_utc if at <= t)
        obs.append(_obs(t, "tabled", day - timedelta(days=3), day, q_api, synced))
        obs.append(_obs(t, "answered", day, day, a_api, 0))
        synced = q_api
        t += timedelta(minutes=30)
    return obs


# ── format / parse ───────────────────────────────────────────────────────────

def test_format_and_parse_round_trip():
    checks = [
        {"kind": "tabled", "from": date(2026, 10, 17), "to": date(2026, 10, 20), "api": 862, "db": 860},
        {"kind": "answered", "from": date(2026, 10, 20), "to": date(2026, 10, 20), "api": 165, "db": 165},
    ]
    text = format_counts(checks)
    assert text == ("tabled 2026-10-17..2026-10-20 API=862 DB=860; "
                    "answered 2026-10-20 API=165 DB=165")
    at = datetime(2026, 10, 20, 9, 15)
    obs = parse_counts(f"quick-check: nothing new ({text})", at)
    assert [(o.kind, o.date_from, o.date_to, o.api, o.db) for o in obs] == [
        ("tabled", date(2026, 10, 17), date(2026, 10, 20), 862, 860),
        ("answered", date(2026, 10, 20), date(2026, 10, 20), 165, 165),
    ]
    assert parse_counts("quick-check: skipped (pq-morning running since 08:30 UTC)", at) == []
    assert parse_counts(None, at) == []


def test_uk_time_follows_bst_switch():
    assert uk_offset_hours(datetime(2026, 10, 20, 12, 0)) == 1      # BST
    assert uk_offset_hours(datetime(2026, 10, 25, 0, 59)) == 1      # just before switch
    assert uk_offset_hours(datetime(2026, 10, 25, 1, 0)) == 0       # GMT from 01:00 UTC
    assert uk_offset_hours(datetime(2026, 3, 29, 1, 0)) == 1        # BST starts
    assert to_uk(datetime(2026, 11, 3, 8, 15)).hour == 8


# ── events / summary ─────────────────────────────────────────────────────────

def test_events_from_count_increases_and_censored_first_check():
    day = date(2026, 10, 13)                                   # Tuesday, BST
    obs = _day_series(day, datetime(2026, 10, 13, 7, 0),       # 08:00 UK
                      [(datetime(2026, 10, 13, 9, 0), 50), (datetime(2026, 10, 13, 15, 0), 950)])
    events, first_check = find_events(obs)
    q = [e for e in events if e.category == "questions"]
    assert len(q) == 1 and q[0].size == 600 and not q[0].censored
    assert q[0].at_uk.strftime("%H:%M") == "08:15"             # first check after 08:00 UK
    a = [e for e in events if e.category == "answers"]
    assert [e.size for e in a] == [50, 950]
    assert first_check["questions"][day].strftime("%H:%M") == "07:15"

    # Already published before the first check: API > DB at 07:15 UK
    early = [_obs(datetime(2026, 10, 14, 6, 15), "tabled", date(2026, 10, 11), date(2026, 10, 14), 1600, 1000)]
    ev, _ = find_events(early)
    assert ev[0].censored and ev[0].size == 600


def test_late_answers_kept_separate():
    obs = [_obs(datetime(2026, 10, 14, 6, 15), "answered", date(2026, 10, 13), date(2026, 10, 13), 1000, 1000),
           _obs(datetime(2026, 10, 14, 6, 45), "answered", date(2026, 10, 13), date(2026, 10, 13), 1010, 1000)]
    events, _ = find_events(obs)
    assert [(e.category, e.size) for e in events] == [("late_answers", 10)]


def test_summary_by_weekday_and_recommendation():
    obs = []
    tuesdays = [date(2026, 9, 1) + timedelta(weeks=i) for i in range(4)]   # BST Tuesdays
    for i, day in enumerate(tuesdays):
        base = datetime.combine(day, datetime.min.time())
        obs += _day_series(day, base + timedelta(hours=7, minutes=10 + 10 * i),   # 08:10-08:40 UK
                           [(base + timedelta(hours=9), 100), (base + timedelta(hours=14), 900)])
    events, first_check = find_events(obs)
    rep = summarise(events, first_check)

    tue = rep["categories"]["questions"]["weekdays"]["Tue"]
    assert tue["days_with_publication"] == 4 and tue["days_checked"] == 4
    assert tue["first_seen_earliest"] == "08:15" and tue["first_seen_latest"] == "08:45"
    assert sum(tue["slot_pct"].values()) == 100
    ans = rep["categories"]["answers"]
    assert ans["cover"] is not None
    assert rep["enough_data"] is False                  # only Tuesdays observed
    rec = rep["recommendation"]
    # Questions 08:00-08:30 slots, answers 10:00 and 15:00 UK -> 07:30-15:30 with margin
    assert rec["uk_window"] == "07:30-16:00"
    assert rec["cron_utc_bst"] == "15,45 6-14 * * 1-5"
    assert rec["cron_utc_gmt"] == "15,45 7-15 * * 1-5"


def test_recommend_cron_both_offsets():
    rec = recommend_cron(14, 37)                        # 07:00 to 19:00 UK
    assert rec["uk_window"] == "07:00-19:00"
    assert rec["cron_utc_both"] == "15,45 6-18 * * 1-5"


def test_stored_time_estimate_upper_bounds():
    rows = [
        (date(2026, 10, 12), datetime(2026, 10, 13, 8, 31)),   # next day, 09:31 UK
        (date(2026, 10, 16), datetime(2026, 10, 19, 8, 31)),   # Fri -> Mon
        (date(2026, 10, 12), datetime(2026, 11, 1, 8, 0)),     # backfill weeks later: ignored
        (date(2026, 10, 12), None),
    ]
    assert stored_time_estimate(rows) == {"09:30": 2}
    assert stored_time_estimate([(date(2026, 10, 13), datetime(2026, 10, 13, 13, 0))],
                                same_day=True) == {"14:00": 1}


# ── report script (read-only, SQLite) ────────────────────────────────────────

@pytest.fixture
def app_db(monkeypatch):
    from flask import Flask
    from extensions import db as _db
    import cache_models  # noqa: F401
    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    test_app = Flask(__name__)
    test_app.config.update({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                            "SQLALCHEMY_TRACK_MODIFICATIONS": False, "TESTING": True})
    _db.init_app(test_app)
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=test_app))
    with test_app.app_context():
        _db.create_all()
        yield test_app, _db
        _db.session.remove()
        _db.drop_all()


def test_report_script_reads_quick_check_notes(app_db, capsys):
    from hansard_archive.models import HaCronRun
    _app, db = app_db
    day = (datetime.utcnow() - timedelta(days=2)).date()
    obs = _day_series(day, datetime.combine(day, datetime.min.time()) + timedelta(hours=7),
                      [(datetime.combine(day, datetime.min.time()) + timedelta(hours=10), 300)])
    by_time: dict = {}
    for o in obs:
        by_time.setdefault(o.at, []).append(
            {"kind": o.kind, "from": o.date_from, "to": o.date_to, "api": o.api, "db": o.db})
    for at, checks in by_time.items():
        db.session.add(HaCronRun(service_name="pq-quickcheck", started_at=at, finished_at=at,
                                 days_window=4, status="ok",
                                 notes=f"quick-check: nothing new ({format_counts(checks)})"))
    db.session.add(HaCronRun(service_name="pq-morning", started_at=datetime.utcnow(),
                             days_window=7, status="ok", notes=None))
    db.session.commit()
    before = HaCronRun.query.count()

    spec = importlib.util.spec_from_file_location(
        "pq_publication_times", PROJECT_ROOT / "scripts" / "pq_publication_times.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.main(["--weeks", "2", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["observations"] == len(obs)
    assert rep["categories"]["questions"]["days_with_publication"] == 1
    assert rep["categories"]["answers"]["days_with_publication"] == 1
    assert rep["recommendation"] is not None and rep["enough_data"] is False
    assert HaCronRun.query.count() == before            # read-only

    assert mod.main(["--weeks", "2"]) == 0
    text = capsys.readouterr().out
    assert "NOT ENOUGH DATA YET" in text and "Railway cron (UTC)" in text
