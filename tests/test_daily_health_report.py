"""
Tests for the daily health report (hansard_archive/health_checks.py and
scripts/daily_health_report.py).

SQLite in-memory DB built from the real model tables; R2, HTTP and the email
sender are always mocked — nothing leaves the machine.

Run: python -m pytest tests/test_daily_health_report.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import cache_models  # noqa: E402
import daily_health_report as dhr  # noqa: E402
from extensions import db  # noqa: E402
from hansard_archive import health_checks as hc  # noqa: E402
from hansard_archive import models as m  # noqa: E402

TUESDAY = datetime(2026, 10, 20, 7, 30)   # report time, UTC
MONDAY = datetime(2026, 10, 19, 7, 30)

ALL_TABLES = [
    m.HansardSession, m.HaCronRun, m.HaPQ, m.HaBill, m.HaEdm, cache_models.CachedMember,
    m.HeadlineStat, m.UpcomingRelease, m.StatProducer,
    cache_models.MemberProfile, cache_models.MemberBiography, cache_models.MemberNameAudit,
    m.HansardSessionTheme, m.HaPQTheme,
]

# Created by hand on production (docs/plans/pq-identity-phase3-sql.md step 4).
PQ_UNIQUE_INDEXES = [
    "CREATE UNIQUE INDEX uq_ha_pq_api_id ON ha_pq (api_id)",
    "CREATE UNIQUE INDEX uq_ha_pq_date_uin ON ha_pq (tabled_date, lower(uin))",
]


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("EMAIL_TEST_MODE", "true")
    monkeypatch.delenv("CRON_ALERT_EMAIL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    for var in hc.R2_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _engine(models=ALL_TABLES, unique_pq_indexes: bool = True) -> sa.Engine:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                              connect_args={"check_same_thread": False})
    db.metadata.create_all(engine, tables=[cls.__table__ for cls in models])
    if m.HaPQ in models and unique_pq_indexes:
        with engine.begin() as conn:
            for ddl in PQ_UNIQUE_INDEXES:
                conn.execute(sa.text(ddl))
    return engine


def _seed_fresh(engine: sa.Engine, now: datetime, skip: tuple = ()) -> None:
    """Data as it should look on a healthy sitting-week morning."""
    yesterday = now.date() - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)
    recent = now - timedelta(hours=2)
    tables = {cls.__tablename__: cls.__table__ for cls in ALL_TABLES}
    with engine.begin() as conn:
        def add(name, rows):
            if name in tables and name not in skip and sa.inspect(conn).has_table(name):
                conn.execute(tables[name].insert(), rows)

        add("ha_session", [
            {"ext_id": "c1", "title": "Commons debate", "date": yesterday, "house": "Commons"},
            {"ext_id": "l1", "title": "Lords debate", "date": yesterday, "house": "Lords"},
        ])
        add("ha_cron_run", [
            {"service_name": svc, "started_at": recent, "finished_at": recent, "status": "ok"}
            for svc in ["morning-catchup", "daytime-mth", "daytime-fri", "pq-cron-early", "pq-cron-morning",
                        "pq-cron-afternoon", "pq-cron-monday", "pq-quickcheck", "pq-cron-weekly",
                        "committee-evidence-cron", "backup-r2", "edm-cron"]
        ])
        add("ha_pq", [
            {"uin": f"HL{i}" if i % 2 else f"C{i}", "question_text": "Q", "heading": "Schools",
             "tabled_date": yesterday, "chamber": "Lords" if i % 2 else "Commons",
             "is_answered": True, "answer_date": yesterday, "updated_at": recent, "ingested_at": recent}
            for i in range(20)
        ])
        add("ha_pq_theme", [{"pq_id": i + 1, "theme": "Education, training and skills",
                             "theme_type": "policy_area"} for i in range(20)])
        add("ha_session_theme", [{"session_id": i + 1, "theme": "Transport", "theme_type": "policy_area"}
                                 for i in range(2)])
        add("ha_bill", [{"parliament_bill_id": 1, "title": "Bill", "house_of_origin": "Commons",
                         "session": "2026-27", "parliament_url": "https://example.test/b",
                         "last_refreshed": now - timedelta(hours=1)}])
        add("ha_edm", [{"id": 1, "uin": "1", "title": "EDM", "last_refreshed": now - timedelta(hours=1)}])
        add("cached_member", [{"member_id": i, "cached_at": now - timedelta(days=2)} for i in range(450)])
        add("headline_stat", [{"theme_slug": "t", "source_type": "manual", "source_id": "x",
                               "display_label": "X", "source_url": "https://example.test",
                               "last_refreshed": now - timedelta(days=1),
                               "last_success": now - timedelta(days=1)}])
        add("upcoming_release", [{"govuk_content_id": "g", "theme_slug": "t", "organisation_slug": "o",
                                  "organisation_name": "O", "title": "R", "release_date": now.date(),
                                  "release_date_confirmed": True, "publication_url": "https://example.test",
                                  "last_refreshed": now - timedelta(hours=3)}])
        add("ha_stat_producer", [{"slug": "ons", "name": "ONS", "producer_type": "central_department",
                                  "web_root_url": "https://example.test", "licence": "OGL_v3",
                                  "last_rediscovered_at": recent}])
        add("member_profile", [{"member_id": i, "slug": f"m-{i}", "name": f"M {i}", "is_current": i < 9,
                                "summary_refreshed_at": now - timedelta(days=2),
                                "details_refreshed_at": now - timedelta(days=2) if i < 5 else None}
                               for i in range(10)])
        add("member_biography", [{"member_id": i, "bio_text": "Bio" if i < 4 else None,
                                  "generated_at": now - timedelta(days=40),
                                  "last_attempt_at": now - timedelta(days=40),
                                  "last_error": "boom" if i == 4 else None} for i in range(5)])
        add("member_name_audit", [{"run_at": now - timedelta(days=18), "counts": {}, "gaps": []}])


def _mon_thu(start: date, end: date, skip: tuple[date, date] | None = None) -> set[date]:
    """Fake sitting calendar: every Mon-Thu in [start, end], minus an optional recess."""
    days, d = set(), start
    while d <= end:
        if d.weekday() < 4 and not (skip and skip[0] <= d <= skip[1]):
            days.add(d)
        d += timedelta(days=1)
    return days


CALENDAR = _mon_thu(date(2026, 8, 1), date(2026, 10, 31))
calendar_calls: list[str] = []


def _calendar_response(days: set[date]):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"SittingDates": [{"Date": f"{d.isoformat()}T00:00:00"} for d in sorted(days)]}
    return resp


def _ok_http(url, **kwargs):
    """Mocked requests.get: serves the sitting calendar and 200s for the site."""
    if "sittingdates" in url:
        calendar_calls.append(url)
        start = date.fromisoformat(kwargs["params"]["startDate"])
        end = date.fromisoformat(kwargs["params"]["endDate"])
        return _calendar_response({d for d in CALENDAR if start <= d <= end})
    return MagicMock(status_code=200)


def _s3_factory(last_modified: datetime, keys=("daily/2026-10-19.sql.gz.gpg", "daily/2026-10-20.sql.gz.gpg")):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    s3.head_object.return_value = {"LastModified": last_modified}
    return lambda: s3


def _run_db(engine, now, sitting_days=None):
    """sitting_days=None -> weekday fallback rule (calendar unavailable)."""
    with engine.connect() as conn:
        ctx = hc.HealthContext(conn, now, recess_periods=[], sitting_days=sitting_days)
        return {r.name: r for r in hc.run_db_checks(ctx)}


R2_ENV = {"R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s", "R2_ENDPOINT_URL": "https://r2.example.test"}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def test_fresh_data_all_ok():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    results = dhr.collect(engine, TUESDAY, env=R2_ENV, http_get=_ok_http,
                          s3_factory=_s3_factory(TUESDAY - timedelta(hours=4)))
    bad = [(r.name, r.status, r.detail) for r in results if r.status not in (hc.OK, hc.SKIPPED)]
    assert bad == []
    skipped = {r.name for r in results if r.status == hc.SKIPPED}
    assert skipped == set()
    assert hc.count_problems(results) == 0


def test_stale_bills_warn_then_fail():
    engine = _engine()
    _seed_fresh(engine, TUESDAY, skip=("ha_bill",))
    bill = m.HaBill.__table__
    row = {"parliament_bill_id": 1, "title": "B", "house_of_origin": "Commons", "session": "s",
           "parliament_url": "u", "last_refreshed": TUESDAY - timedelta(days=3)}
    with engine.begin() as conn:
        conn.execute(bill.insert(), row)
    assert _run_db(engine, TUESDAY)["Bills: last refresh"].status == hc.WARN
    with engine.begin() as conn:
        conn.execute(bill.update().values(last_refreshed=TUESDAY - timedelta(days=150)))
    res = _run_db(engine, TUESDAY)["Bills: last refresh"]
    assert res.status == hc.FAIL
    assert res.last_success == TUESDAY - timedelta(days=150)


def test_missing_table_is_skipped_not_fail():
    engine = _engine([cls for cls in ALL_TABLES if cls is not m.HaEdm])
    _seed_fresh(engine, TUESDAY)
    res = _run_db(engine, TUESDAY)["EDMs: last refresh"]
    assert res.status == hc.SKIPPED
    assert "ha_edm" in res.detail


def test_empty_database_never_fails():
    results = _run_db(_engine([]), TUESDAY)
    assert {r.status for r in results.values()} == {hc.SKIPPED}


def test_archive_gap_warns_and_recess_suppresses():
    engine = _engine()
    _seed_fresh(engine, TUESDAY, skip=("ha_session",))
    with engine.begin() as conn:
        conn.execute(m.HansardSession.__table__.insert(), [
            {"ext_id": "c", "title": "t", "date": date(2026, 10, 8), "house": "Commons"},   # Thursday
            {"ext_id": "l", "title": "t", "date": date(2026, 10, 19), "house": "Lords"},
        ])
    res = _run_db(engine, TUESDAY)["Hansard archive: newest session"]
    assert res.status == hc.WARN
    assert "Commons: 2026-10-08 (7 weekdays" in res.detail
    with engine.connect() as conn:
        ctx = hc.HealthContext(conn, TUESDAY, recess_periods=[("2026-10-09", "2026-10-16")])
        assert hc.check_archive_sessions(ctx).status == hc.OK


def test_cron_failures_and_one_off_services():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    cron = m.HaCronRun.__table__
    with engine.begin() as conn:
        conn.execute(cron.insert(), [
            # latest pq-cron-afternoon run failed
            {"service_name": "pq-cron-afternoon", "started_at": TUESDAY - timedelta(hours=1), "status": "failed"},
            # an unknown pq-* service that last succeeded a week ago
            {"service_name": "pq-early", "started_at": TUESDAY - timedelta(days=7),
             "finished_at": TUESDAY - timedelta(days=7), "status": "ok"},
            {"service_name": "backfill", "started_at": TUESDAY - timedelta(days=7), "status": "failed"},
        ] + [
            # a hand-started catch-up failing on deep pages
            {"service_name": "pq-catchup", "started_at": TUESDAY - timedelta(hours=h),
             "finished_at": TUESDAY - timedelta(hours=h - 1), "status": "failed"} for h in range(2, 7)
        ])
    res = _run_db(engine, TUESDAY)
    assert res["PQ cron: pq-cron-afternoon"].status == hc.WARN
    assert "latest run failed" in res["PQ cron: pq-cron-afternoon"].detail
    unknown = res["PQ cron: pq-early"]                 # a name the config doesn't know
    assert unknown.status == hc.WARN and "not in the health config" in unknown.detail
    catchup = res["PQ one-off: pq-catchup"]
    assert catchup.status == hc.OK
    assert "latest run failed" in catchup.detail and "5 failed run(s) in last 24h" in catchup.detail
    assert "one-off run, report only" in catchup.detail
    assert "PQ cron: backfill" not in res and "PQ one-off: backfill" not in res
    assert hc.count_problems(list(res.values())) == 2


def test_misnamed_weekly_service_warns():
    engine = _fresh_engine(FRIDAY)
    cron = m.HaCronRun.__table__
    with engine.begin() as conn:
        conn.execute(cron.delete().where(cron.c.service_name == "pq-cron-weekly"))
        conn.execute(cron.insert(), [{"service_name": "pq-weekly-refresh", "started_at": datetime(2026, 10, 17, 6),
                                      "finished_at": datetime(2026, 10, 17, 6, 5), "status": "failed"}])
    res = _run_db(engine, FRIDAY)
    assert res["PQ cron: pq-cron-weekly"].status == hc.SKIPPED
    wrong = res["PQ cron: pq-weekly-refresh"]
    assert wrong.status == hc.WARN and "latest run failed" in wrong.detail
    assert hc.count_problems(list(res.values())) == 1


def test_cron_names_match_the_ops_doc():
    names = set(hc.CONFIG["pq_crons"]["known"])
    assert names == {"pq-cron-early", "pq-cron-morning", "pq-cron-afternoon", "pq-cron-monday",
                     "pq-quickcheck", "pq-cron-weekly"}
    docs = (PROJECT_ROOT / "docs" / "ops" / "railway-services.md").read_text(encoding="utf-8")
    expected = (names | set(hc.CONFIG["archive_crons"]) | set(hc.CONFIG["pq_crons"]["one_off"])
                | {hc.CONFIG[k]["service"] for k in ("edm_cron", "committee_evidence", "backup_record")})
    assert {n for n in expected if f"`{n}`" not in docs} == set()
    assert set(m.PQ_SCHEDULED_RUNS) == names       # the "updated" times on /questions and /tracker


def test_optional_cron_without_runs_is_skipped():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    cron = m.HaCronRun.__table__
    with engine.begin() as conn:
        conn.execute(cron.delete().where(cron.c.service_name.in_(["pq-cron-weekly", "pq-quickcheck"])))
    res = _run_db(engine, TUESDAY)
    assert res["PQ cron: pq-cron-weekly"].status == hc.SKIPPED
    assert res["PQ cron: pq-quickcheck"].status == hc.WARN          # not optional


FRIDAY = datetime(2026, 10, 23, 7, 30)
THU = datetime(2026, 10, 22)
TUE = datetime(2026, 10, 20)


def _pq_runs(engine, rows):
    cron = m.HaCronRun.__table__
    with engine.begin() as conn:
        conn.execute(cron.delete().where(cron.c.service_name.like("pq-cron-%")))
        conn.execute(cron.insert(), [
            {"service_name": svc, "started_at": start, "finished_at": end, "status": status}
            for svc, start, end, status in rows])


def _ok(svc, start):
    return (svc, start, start + timedelta(minutes=40), "ok")


def _stopped(svc, start):
    return (svc, start, None, "running")


def test_run_stopped_by_deploy_is_covered_by_a_later_sweep():
    # 22 Oct: a deploy stopped the early and morning runs; the afternoon run finished.
    engine = _fresh_engine(FRIDAY)
    _pq_runs(engine, [
        _ok("pq-cron-early", TUE.replace(hour=8, minute=30)),
        _stopped("pq-cron-early", THU.replace(hour=8, minute=30)),
        _ok("pq-cron-morning", TUE.replace(hour=9, minute=30)),
        _stopped("pq-cron-morning", THU.replace(hour=9, minute=30)),
        _ok("pq-cron-afternoon", THU.replace(hour=14)),
        _ok("pq-cron-monday", datetime(2026, 10, 19, 9)),
    ])
    res = _run_db(engine, FRIDAY)
    early = res["PQ cron: pq-cron-early"]
    assert early.status == hc.OK                      # own last ok is 71 Mon-Fri hours old
    assert "latest run (started 2026-10-22 08:30 UTC) has no finish time after 23.0h" in early.detail
    assert "stopped by a deploy or restart" in early.detail
    assert "covered by pq-cron-afternoon finishing ok at 2026-10-22 14:40 UTC" in early.detail
    assert res["PQ cron: pq-cron-morning"].status == hc.OK
    assert "finish time" not in res["PQ cron: pq-cron-afternoon"].detail


def test_stopped_runs_without_a_later_sweep_are_graded_by_age():
    # No sweep has finished since Tuesday: 3 working days, a real FAIL.
    engine = _fresh_engine(FRIDAY)
    _pq_runs(engine, [
        _ok("pq-cron-early", TUE.replace(hour=8, minute=30)),
        _stopped("pq-cron-early", THU.replace(hour=8, minute=30)),
        _ok("pq-cron-afternoon", TUE.replace(hour=14)),
        _stopped("pq-cron-afternoon", THU.replace(hour=14)),
    ])
    res = _run_db(engine, FRIDAY)
    for svc in ("pq-cron-early", "pq-cron-afternoon"):
        assert res[f"PQ cron: {svc}"].status == hc.FAIL
        assert "has no finish time" in res[f"PQ cron: {svc}"].detail
    # A recent ok run but the newest run unfinished and uncovered: WARN, not OK.
    _pq_runs(engine, [
        _ok("pq-cron-early", THU.replace(hour=5)),
        _stopped("pq-cron-early", THU.replace(hour=8, minute=30)),
    ])
    assert _run_db(engine, FRIDAY)["PQ cron: pq-cron-early"].status == hc.WARN
    # once a later run of the same service finishes, the stopped run no longer shows
    _pq_runs(engine, [
        _ok("pq-cron-early", TUE.replace(hour=8, minute=30)),
        _stopped("pq-cron-early", THU.replace(hour=8, minute=30)),
        _ok("pq-cron-early", FRIDAY + timedelta(hours=1)),
    ])
    early = _run_db(engine, FRIDAY + timedelta(hours=2))["PQ cron: pq-cron-early"]
    assert early.status == hc.OK and "finish time" not in early.detail


def test_hung_run_is_not_covered_for_ever():
    # Railway skips the next runs while one is still running: one unfinished row since 16 Oct.
    engine = _fresh_engine(FRIDAY)
    _pq_runs(engine, [
        _ok("pq-cron-early", datetime(2026, 10, 15, 8, 30)),
        _stopped("pq-cron-early", datetime(2026, 10, 16, 8, 30)),
        _ok("pq-cron-morning", THU.replace(hour=9, minute=30)),
        _ok("pq-cron-afternoon", THU.replace(hour=14)),
    ])
    early = _run_db(engine, FRIDAY)["PQ cron: pq-cron-early"]
    assert early.status == hc.FAIL
    assert "covered by" not in early.detail and "has no finish time after 6d 23h" in early.detail


def test_repeated_stopped_runs_warn_even_when_covered():
    engine = _fresh_engine(FRIDAY)
    _pq_runs(engine, [
        _ok("pq-cron-early", datetime(2026, 10, 16, 8, 30)),
        *[_stopped("pq-cron-early", datetime(2026, 10, d, 8, 30)) for d in (20, 21, 22)],
        _ok("pq-cron-afternoon", THU.replace(hour=14)),
    ])
    early = _run_db(engine, FRIDAY)["PQ cron: pq-cron-early"]
    assert early.status == hc.FAIL                   # last ok a week ago
    assert "3 runs in a row never finished" in early.detail
    _pq_runs(engine, [
        _ok("pq-cron-early", datetime(2026, 10, 9, 8, 30)),
        *[_stopped("pq-cron-early", datetime(2026, 10, d, 8, 30)) for d in (19, 20, 21, 22)],
        _ok("pq-cron-afternoon", THU.replace(hour=14)),
    ])
    early = _run_db(engine, FRIDAY)["PQ cron: pq-cron-early"]
    assert early.status == hc.FAIL and "4 runs in a row never finished" in early.detail


def test_failed_run_after_last_ok_is_still_graded():
    engine = _fresh_engine(FRIDAY)
    _pq_runs(engine, [
        _ok("pq-cron-early", datetime(2026, 10, 16, 8, 30)),
        ("pq-cron-early", datetime(2026, 10, 21, 8, 30), datetime(2026, 10, 21, 9), "failed"),
        _stopped("pq-cron-early", THU.replace(hour=8, minute=30)),
        _ok("pq-cron-afternoon", THU.replace(hour=14)),
    ])
    assert _run_db(engine, FRIDAY)["PQ cron: pq-cron-early"].status == hc.FAIL


def test_ungrouped_cron_stopped_run_warns_until_next_run():
    engine = _fresh_engine(TUESDAY)
    cron = m.HaCronRun.__table__
    with engine.begin() as conn:
        conn.execute(cron.delete().where(cron.c.service_name == "edm-cron"))
        conn.execute(cron.insert(), [
            {"service_name": "edm-cron", "started_at": TUESDAY - timedelta(hours=25),
             "finished_at": TUESDAY - timedelta(hours=25), "status": "ok"},
            {"service_name": "edm-cron", "started_at": TUESDAY - timedelta(hours=5),
             "finished_at": None, "status": "running"}])
    res = _run_db(engine, TUESDAY)["EDM cron"]
    assert res.status == hc.WARN and "has no finish time after 5.0h" in res.detail
    with engine.begin() as conn:
        conn.execute(cron.insert(), [{"service_name": "edm-cron", "started_at": TUESDAY - timedelta(hours=1),
                                      "finished_at": TUESDAY - timedelta(minutes=50), "status": "ok"}])
    assert _run_db(engine, TUESDAY)["EDM cron"].status == hc.OK


def test_skipped_quick_checks_are_counted():
    engine = _fresh_engine(TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaCronRun.__table__.insert(), [{
            "service_name": "pq-quickcheck", "started_at": TUESDAY - timedelta(minutes=30 * i),
            "finished_at": TUESDAY - timedelta(minutes=30 * i - 1), "status": "skipped"} for i in range(1, 4)])
    res = _run_db(engine, TUESDAY)["PQ cron: pq-quickcheck"]
    assert res.status == hc.OK
    assert "3 run(s) skipped in last 24h (another PQ run in progress)" in res.detail


def test_run_in_progress_is_not_flagged():
    engine = _fresh_engine(TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaCronRun.__table__.insert(), [{
            "service_name": "pq-cron-early", "started_at": TUESDAY - timedelta(hours=1),
            "finished_at": None, "status": "running"}])
    res = _run_db(engine, TUESDAY)["PQ cron: pq-cron-early"]
    assert res.status == hc.OK and "finish time" not in res.detail


def test_weekend_does_not_count_for_weekday_crons():
    # Friday 14:00 -> Monday 07:30 is 17.5 Mon-Fri hours
    assert hc.active_hours_since(datetime(2026, 10, 16, 14), MONDAY, hc._DAYS["mon-fri"]) == pytest.approx(17.5)
    engine = _engine()
    _seed_fresh(engine, MONDAY, skip=("ha_cron_run",))
    with engine.begin() as conn:
        conn.execute(m.HaCronRun.__table__.insert(), [
            {"service_name": "pq-cron-morning", "started_at": datetime(2026, 10, 16, 9, 30),
             "finished_at": datetime(2026, 10, 16, 9, 35), "status": "ok"},
        ])
    assert _run_db(engine, MONDAY)["PQ cron: pq-cron-morning"].status == hc.OK


def test_missing_headings_warn():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.update().values(heading=None, tabled_date=date(2026, 10, 12)))
    res = _run_db(engine, TUESDAY)["Written questions: missing headings"]
    assert res.status == hc.WARN
    assert res.detail.startswith("20 of 20 tabled 2026-10-06 to 2026-10-14")
    assert "backfill_pq_headings" in res.detail


def test_newest_questions_without_headings_are_not_counted():
    # Friday 23 Oct: Parliament hasn't headed questions tabled 20-22 Oct yet.
    engine = _engine()
    _seed_fresh(engine, FRIDAY, skip=("ha_pq",))
    rows = [_pq_row(f"N{i}", date(2026, 10, 20 + i % 3), heading=None) for i in range(30)]
    rows += [_pq_row(f"O{i}", date(2026, 10, 15), heading="Schools") for i in range(40)]
    rows += [_pq_row("O-missing", date(2026, 10, 15), heading=None)]
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.insert(), rows)
    res = _run_db(engine, FRIDAY)["Written questions: missing headings"]
    assert res.status == hc.OK
    assert res.detail.startswith("1 of 41 tabled 2026-10-09 to 2026-10-19 (2.4%)")
    assert "30 tabled since 2026-10-20 not counted" in res.detail
    # Thursday: the counted window ends on the Friday before, not on a Sunday
    thursday = _run_db(engine, datetime(2026, 10, 22, 7, 30))["Written questions: missing headings"]
    assert " to 2026-10-16 (" in thursday.detail


def test_no_recent_answers_warns():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.update().values(answer_date=date(2026, 9, 1)))
    assert _run_db(engine, TUESDAY)["Written questions: recent answers"].status == hc.WARN


def test_r2_backup_mocked():
    fresh = hc.check_r2_backup(R2_ENV, TUESDAY, s3_factory=_s3_factory(TUESDAY - timedelta(hours=4)))
    assert fresh.status == hc.OK
    assert "daily/2026-10-20.sql.gz.gpg" in fresh.detail
    stale = hc.check_r2_backup(R2_ENV, TUESDAY, s3_factory=_s3_factory(TUESDAY - timedelta(hours=60)))
    assert stale.status == hc.FAIL
    assert hc.check_r2_backup({}, TUESDAY).status == hc.SKIPPED
    empty = hc.check_r2_backup(R2_ENV, TUESDAY, s3_factory=_s3_factory(TUESDAY, keys=()))
    assert empty.status == hc.FAIL


def test_web_checks():
    def degraded(url, timeout, allow_redirects):
        if url.endswith("/ping"):
            raise TimeoutError("timed out")
        resp = MagicMock(status_code=503)
        resp.json.return_value = {"database": "ok", "backup": "STALE: last backup 30h ago",
                                  "status": "degraded", "version": "abc1234"}
        return resp

    res = {r.name: r for r in hc.check_web(degraded)}
    assert res["Web: /ping"].status == hc.FAIL
    assert res["Web: /health"].status == hc.WARN
    assert "backup=STALE" in res["Web: /health"].detail


# ---------------------------------------------------------------------------
# Email policy via main()
# ---------------------------------------------------------------------------

def _main(engine, now, *flags, sender=None):
    sender = sender or MagicMock(return_value=True)
    code = dhr.main(["--skip-web", *flags], engine=engine, now=now, sender=sender, env={},
                    http_get=_ok_http)
    return code, sender


def _fresh_engine(now):
    engine = _engine()
    _seed_fresh(engine, now)
    return engine


def test_no_email_on_all_ok_weekday(capsys):
    code, sender = _main(_fresh_engine(TUESDAY), TUESDAY)
    assert code == 0
    sender.assert_not_called()
    out = capsys.readouterr().out
    assert "0 problem(s)" in out and "No email" in out


def test_email_on_problem(monkeypatch):
    monkeypatch.setenv("CRON_ALERT_EMAIL", "alerts@example.test")
    engine = _fresh_engine(TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaBill.__table__.update().values(last_refreshed=TUESDAY - timedelta(days=150)))
    code, sender = _main(engine, TUESDAY)
    assert code == 0                      # problems don't fail the cron
    sender.assert_called_once()
    kwargs = sender.call_args.kwargs
    assert kwargs["to"] == "alerts@example.test"
    assert kwargs["subject"] == "[Westminster Brief] Daily health: 1 problem"
    assert "Bills: last refresh" in kwargs["text_body"]
    assert "<table" in kwargs["html_body"]


def test_monday_all_ok_summary():
    code, sender = _main(_fresh_engine(MONDAY), MONDAY)
    assert code == 0
    sender.assert_called_once()
    assert sender.call_args.kwargs["subject"] == "[Westminster Brief] Daily health: all OK"
    assert sender.call_args.kwargs["to"] == "admin@example.test"


def test_always_send_and_dry_run():
    _, sender = _main(_fresh_engine(TUESDAY), TUESDAY, "--always-send")
    sender.assert_called_once()
    engine = _fresh_engine(TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaBill.__table__.update().values(last_refreshed=TUESDAY - timedelta(days=150)))
    code, sender = _main(engine, TUESDAY, "--dry-run")
    assert code == 0
    sender.assert_not_called()


def test_email_failure_is_nonzero():
    code, _ = _main(_fresh_engine(MONDAY), MONDAY, sender=MagicMock(return_value=False))
    assert code == 1


def test_checker_crash_is_nonzero(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("renderer broke")
    monkeypatch.setattr(hc, "render_text", boom)
    code, sender = _main(_fresh_engine(TUESDAY), TUESDAY)
    assert code == 2
    assert "crashed" in sender.call_args.kwargs["subject"]


def test_unreachable_database_is_reported_not_crash():
    engine = MagicMock()
    engine.connect.side_effect = sa.exc.OperationalError("connect", {}, Exception("refused"))
    code, sender = _main(engine, TUESDAY)
    assert code == 0
    assert "Daily health: 1 problem" in sender.call_args.kwargs["subject"]


# ---------------------------------------------------------------------------
# Sitting calendar (What's On API, mocked)
# ---------------------------------------------------------------------------

def _sessions_engine(commons: date, lords: date, now: datetime):
    engine = _engine()
    _seed_fresh(engine, now, skip=("ha_session",))
    with engine.begin() as conn:
        conn.execute(m.HansardSession.__table__.insert(), [
            {"ext_id": "c", "title": "t", "date": commons, "house": "Commons"},
            {"ext_id": "l", "title": "t", "date": lords, "house": "Lords"},
        ])
    return engine


def test_fetch_sitting_days_parses_and_caches_per_run():
    calendar_calls.clear()
    days, note = hc.fetch_sitting_days(_ok_http, TUESDAY.date())
    assert note == ""
    assert date(2026, 10, 19) in days["Commons"] and date(2026, 10, 16) not in days["Lords"]
    assert len(calendar_calls) == 2                      # one request per House
    calendar_calls.clear()
    dhr.collect(_fresh_engine(TUESDAY), TUESDAY, env={}, skip_web=True, http_get=_ok_http)
    assert len(calendar_calls) == 2                      # fetched once per run, shared by all checks


def _raise_connection_error(url, **kwargs):
    raise ConnectionError("down")


@pytest.mark.parametrize("getter, expected", [
    (_raise_connection_error, "calendar unavailable (ConnectionError)"),
    (lambda url, **kw: MagicMock(status_code=500), "calendar HTTP 500 for Commons"),
    (lambda url, **kw: _calendar_response(set()), "calendar returned no sitting days"),
])
def test_fetch_sitting_days_failures(getter, expected):
    days, note = hc.fetch_sitting_days(getter, TUESDAY.date())
    assert days is None
    assert note.startswith(expected)


def test_calendar_allows_publication_lag():
    # Commons last data Thursday 15th; it didn't sit Friday; Monday 19th's data
    # isn't due until the end of Tuesday -> nothing overdue on Tuesday 07:30.
    engine = _sessions_engine(date(2026, 10, 15), date(2026, 10, 19), TUESDAY)
    res = _run_db(engine, TUESDAY, sitting_days={"Commons": CALENDAR, "Lords": CALENDAR})
    assert res["Hansard archive: newest session"].status == hc.OK
    assert "[sitting calendar]" in res["Hansard archive: newest session"].detail


def test_calendar_flags_missing_sitting_day():
    engine = _sessions_engine(date(2026, 10, 14), date(2026, 10, 19), TUESDAY)
    res = _run_db(engine, TUESDAY, sitting_days={"Commons": CALENDAR, "Lords": CALENDAR})
    archive = res["Hansard archive: newest session"]
    assert archive.status == hc.WARN
    assert "Commons: 2026-10-14 (1 sitting day(s) missing: 2026-10-15)" in archive.detail


def test_calendar_recess_does_not_warn_but_weekday_rule_would():
    recess = (date(2026, 9, 17), date(2026, 10, 11))
    cal = _mon_thu(date(2026, 8, 1), date(2026, 10, 31), skip=recess)
    first_day_back = datetime(2026, 10, 12, 7, 30)        # Monday after conference recess
    engine = _sessions_engine(date(2026, 9, 16), date(2026, 9, 16), first_day_back)
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.update().values(tabled_date=date(2026, 9, 16),
                                                      answer_date=date(2026, 9, 16)))
    with_cal = _run_db(engine, first_day_back, sitting_days={"Commons": cal, "Lords": cal})
    assert with_cal["Hansard archive: newest session"].status == hc.OK
    assert with_cal["Written questions: newest tabled"].status == hc.OK
    assert with_cal["Written questions: recent answers"].status == hc.OK
    fallback = _run_db(engine, first_day_back)
    assert fallback["Hansard archive: newest session"].status == hc.WARN
    assert "weekday rule" in fallback["Hansard archive: newest session"].detail


def test_pq_tabled_uses_each_houses_calendar():
    # Lords sat Friday 16th (Commons didn't); Lords questions for Friday were due by end of Monday.
    lords = CALENDAR | {date(2026, 10, 16)}
    engine = _engine()
    _seed_fresh(engine, TUESDAY, skip=("ha_pq",))
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.insert(), [
            {"uin": "C1", "question_text": "Q", "heading": "H", "tabled_date": date(2026, 10, 15),
             "chamber": "Commons", "is_answered": True, "answer_date": date(2026, 10, 19)},
            {"uin": "HL1", "question_text": "Q", "heading": "H", "tabled_date": date(2026, 10, 15),
             "chamber": "Lords", "is_answered": False, "answer_date": None},
        ])
    res = _run_db(engine, TUESDAY, sitting_days={"Commons": CALENDAR, "Lords": lords})
    pq = res["Written questions: newest tabled"]
    assert pq.status == hc.WARN
    assert "Commons: 2026-10-15;" in pq.detail
    assert "Lords: 2026-10-15 (1 sitting day(s) missing: 2026-10-16)" in pq.detail


def test_manual_recess_override_applies_to_calendar():
    engine = _sessions_engine(date(2026, 10, 14), date(2026, 10, 19), TUESDAY)
    with engine.connect() as conn:
        ctx = hc.HealthContext(conn, TUESDAY, recess_periods=[("2026-10-15", "2026-10-15")],
                               sitting_days={"Commons": CALENDAR, "Lords": CALENDAR})
        assert hc.check_archive_sessions(ctx).status == hc.OK


def test_calendar_failure_noted_in_report(capsys):
    def no_calendar(url, **kw):
        if "sittingdates" in url:
            raise TimeoutError("slow")
        return MagicMock(status_code=200)

    engine = _fresh_engine(TUESDAY)
    code = dhr.main(["--skip-web", "--dry-run"], engine=engine, now=TUESDAY, env={}, http_get=no_calendar)
    assert code == 0
    out = capsys.readouterr().out
    assert "calendar unavailable (TimeoutError): weekday rule" in out
    assert "0 problem(s)" in out


# ---------------------------------------------------------------------------
# Checks added for jobs deployed after the first version
# ---------------------------------------------------------------------------

def _pq_row(uin, tabled, api_id=None, heading="H"):
    return {"uin": uin, "question_text": "Q", "heading": heading, "tabled_date": tabled,
            "chamber": "Commons", "is_answered": False, "api_id": api_id}


def test_pq_integrity_ok_with_indexes_and_no_duplicates():
    res = _run_db(_fresh_engine(TUESDAY), TUESDAY)["Written questions: duplicates + unique indexes"]
    assert res.status == hc.OK
    assert res.detail == ("duplicate groups: api_id 0, (tabled_date, uin) 0; "
                          "indexes: uq_ha_pq_api_id ok, uq_ha_pq_date_uin ok")


def test_pq_integrity_duplicates_fail_and_missing_index_warns():
    engine = _engine(unique_pq_indexes=False)
    _seed_fresh(engine, TUESDAY, skip=("ha_pq",))
    d = date(2026, 10, 19)
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.insert(), [_pq_row("100", d, 5), _pq_row("101", d, 6)])
    res = _run_db(engine, TUESDAY)["Written questions: duplicates + unique indexes"]
    assert res.status == hc.WARN
    assert "uq_ha_pq_api_id missing, uq_ha_pq_date_uin missing" in res.detail
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.insert(), [
            _pq_row("200", d, 5),                       # same api_id as "100"
            _pq_row("hl1", d), _pq_row("HL1", d),      # same date + uin, case differs
            _pq_row("100", date(2025, 1, 6), None),    # UIN reused in an older session: fine
        ])
    res = _run_db(engine, TUESDAY)["Written questions: duplicates + unique indexes"]
    assert res.status == hc.FAIL
    assert res.detail.startswith("duplicate groups: api_id 1, (tabled_date, uin) 1;")


def test_pq_index_state_flags_non_unique_index():
    engine = _engine(unique_pq_indexes=False)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE INDEX uq_ha_pq_api_id ON ha_pq (api_id)"))
    with engine.connect() as conn:
        state = hc._pq_index_state(hc.HealthContext(conn, TUESDAY), ["uq_ha_pq_api_id", "uq_ha_pq_date_uin"])
    assert state == {"uq_ha_pq_api_id": "not unique", "uq_ha_pq_date_uin": "missing"}


def test_missing_headings_total_is_report_only():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.insert(), [
            _pq_row(str(i), date(2026, 6, 1), heading=None) for i in range(30)])
    res = _run_db(engine, TUESDAY)["Written questions: missing headings (all)"]
    assert res.status == hc.OK
    assert res.detail.startswith("30 of 50 rows have no heading, 30 of them tabled before 2026-10-06")


def test_edm_freshness_and_cron_record():
    engine = _engine()
    _seed_fresh(engine, TUESDAY, skip=("ha_cron_run",))
    with engine.begin() as conn:
        conn.execute(m.HaEdm.__table__.update().values(last_refreshed=TUESDAY - timedelta(hours=50)))
        conn.execute(m.HaCronRun.__table__.insert(), [
            {"service_name": "edm-cron", "started_at": TUESDAY - timedelta(hours=27),
             "finished_at": TUESDAY - timedelta(hours=27), "status": "ok"},
            {"service_name": "edm-cron", "started_at": TUESDAY - timedelta(hours=3), "finished_at": None,
             "status": "failed"},
        ])
    res = _run_db(engine, TUESDAY)
    assert res["EDMs: last refresh"].status == hc.WARN          # > 2 days
    assert res["EDM cron"].status == hc.WARN                   # missed one daily run
    assert "latest run failed" in res["EDM cron"].detail


def test_upcoming_releases_daily_threshold():
    engine = _fresh_engine(TUESDAY)
    up = m.UpcomingRelease.__table__
    with engine.begin() as conn:
        conn.execute(up.update().values(last_refreshed=TUESDAY - timedelta(hours=25, minutes=45)))
    assert _run_db(engine, TUESDAY)["Stats: upcoming releases"].status == hc.WARN   # missed 05:45 run
    with engine.begin() as conn:
        conn.execute(up.update().values(last_refreshed=TUESDAY - timedelta(days=4)))
    assert _run_db(engine, TUESDAY)["Stats: upcoming releases"].status == hc.FAIL


def test_headline_stats_unscheduled_is_skipped_not_warn():
    engine = _fresh_engine(TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HeadlineStat.__table__.update().values(last_success=TUESDAY - timedelta(days=30)))
    res = _run_db(engine, TUESDAY)["Stats: headline refresh"]
    assert res.status == hc.SKIPPED
    assert "30d ago" in res.detail and "not scheduled yet" in res.detail
    with engine.connect() as conn:
        config = {**hc.CONFIG, "headline_stats": {**hc.CONFIG["headline_stats"], "scheduled": True}}
        ctx = hc.HealthContext(conn, TUESDAY, config=config, recess_periods=[])
        assert hc.check_headline_stats(ctx).status == hc.WARN


def test_member_profiles_and_biographies():
    engine = _fresh_engine(TUESDAY)
    res = _run_db(engine, TUESDAY)
    profiles = res["Members: profiles"]
    assert profiles.status == hc.OK
    assert "10 profiles (9 current, 5 with details)" in profiles.detail
    bios = res["Members: biographies"]
    # bios themselves are 40 days old, but the monthly job ran 18 days ago (name audit)
    assert bios.status == hc.OK
    assert "4 bios in 5 rows, 1 with last_error" in bios.detail
    with engine.begin() as conn:
        conn.execute(cache_models.MemberProfile.__table__.update().values(
            summary_refreshed_at=TUESDAY - timedelta(days=9)))
        conn.execute(cache_models.MemberNameAudit.__table__.delete())
    res = _run_db(engine, TUESDAY)
    assert res["Members: profiles"].status == hc.WARN
    assert res["Members: biographies"].status == hc.WARN


def test_member_tables_empty_warn():
    engine = _engine()
    _seed_fresh(engine, TUESDAY, skip=("member_profile", "member_biography", "member_name_audit"))
    res = _run_db(engine, TUESDAY)
    assert res["Members: profiles"].status == hc.WARN and "empty" in res["Members: profiles"].detail
    assert res["Members: biographies"].status == hc.WARN


def test_calendar_lookback_spans_summer_recess():
    seen = {}

    def getter(url, **kwargs):
        seen.update(kwargs["params"])
        return _calendar_response({date(2026, 7, 20)})
    days, note = hc.fetch_sitting_days(getter, date(2026, 9, 1))
    assert note == "" and date(2026, 7, 20) in days["Commons"]
    assert seen == {"startDate": "2026-06-03", "endDate": "2026-09-01"}


def test_tagging_coverage_warns_when_recent_items_are_untagged():
    engine = _engine()
    _seed_fresh(engine, TUESDAY, skip=("ha_pq_theme",))
    results = _run_db(engine, TUESDAY)
    pq = results["Tagging: recent written questions tagged"]
    assert pq.status == hc.WARN and "0 of 20" in pq.detail
    sessions = results["Tagging: recent debates tagged"]
    assert sessions.status == hc.OK and "not judged" in sessions.detail   # only 2 sessions


def test_tagging_coverage_ok_and_counts_attempts():
    engine = _engine(ALL_TABLES + [m.HaTagAttempt])
    _seed_fresh(engine, TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaTagAttempt.__table__.insert(), [{"item_kind": "pq", "item_id": 99, "attempts": 3}])
    pq = _run_db(engine, TUESDAY)["Tagging: recent written questions tagged"]
    assert pq.status == hc.OK and "20 of 20" in pq.detail and "1 item(s) with failed" in pq.detail
