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
]


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("EMAIL_TEST_MODE", "true")
    monkeypatch.delenv("CRON_ALERT_EMAIL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    for var in hc.R2_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _engine(models=ALL_TABLES) -> sa.Engine:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                              connect_args={"check_same_thread": False})
    db.metadata.create_all(engine, tables=[cls.__table__ for cls in models])
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
            for svc in ["morning-catchup", "daytime-mth", "daytime-fri", "pq-morning", "pq-afternoon",
                        "pq-monday", "committee-evidence-cron", "backup-r2"]
        ])
        add("ha_pq", [
            {"uin": f"HL{i}" if i % 2 else f"C{i}", "question_text": "Q", "heading": "Schools",
             "tabled_date": yesterday, "chamber": "Lords" if i % 2 else "Commons",
             "is_answered": True, "answer_date": yesterday, "updated_at": recent, "ingested_at": recent}
            for i in range(20)
        ])
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


def _ok_http(url, timeout, allow_redirects):
    resp = MagicMock(status_code=200)
    return resp


def _s3_factory(last_modified: datetime, keys=("daily/2026-10-19.sql.gz.gpg", "daily/2026-10-20.sql.gz.gpg")):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    s3.head_object.return_value = {"LastModified": last_modified}
    return lambda: s3


def _run_db(engine, now):
    with engine.connect() as conn:
        return {r.name: r for r in hc.run_db_checks(hc.HealthContext(conn, now, recess_periods=[]))}


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
    assert skipped == {"Members: profiles", "Members: biographies"}   # tables don't exist yet
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


def test_cron_failures_and_discovered_services():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    cron = m.HaCronRun.__table__
    with engine.begin() as conn:
        conn.execute(cron.insert(), [
            # latest pq-afternoon run failed
            {"service_name": "pq-afternoon", "started_at": TUESDAY - timedelta(hours=1), "status": "failed"},
            # an unknown pq-* service that last succeeded a week ago
            {"service_name": "pq-early", "started_at": TUESDAY - timedelta(days=7),
             "finished_at": TUESDAY - timedelta(days=7), "status": "ok"},
            {"service_name": "backfill", "started_at": TUESDAY - timedelta(days=7), "status": "failed"},
        ])
    res = _run_db(engine, TUESDAY)
    assert res["PQ cron: pq-afternoon"].status == hc.WARN
    assert "latest run failed" in res["PQ cron: pq-afternoon"].detail
    assert res["PQ cron: pq-early"].status == hc.FAIL
    assert "PQ cron: backfill" not in res


def test_weekend_does_not_count_for_weekday_crons():
    # Friday 14:00 -> Monday 07:30 is 17.5 Mon-Fri hours
    assert hc.active_hours_since(datetime(2026, 10, 16, 14), MONDAY, hc._DAYS["mon-fri"]) == pytest.approx(17.5)
    engine = _engine()
    _seed_fresh(engine, MONDAY, skip=("ha_cron_run",))
    with engine.begin() as conn:
        conn.execute(m.HaCronRun.__table__.insert(), [
            {"service_name": "pq-morning", "started_at": datetime(2026, 10, 16, 9, 30),
             "finished_at": datetime(2026, 10, 16, 9, 35), "status": "ok"},
        ])
    assert _run_db(engine, MONDAY)["PQ cron: pq-morning"].status == hc.OK


def test_missing_headings_warn():
    engine = _engine()
    _seed_fresh(engine, TUESDAY)
    with engine.begin() as conn:
        conn.execute(m.HaPQ.__table__.update().values(heading=None))
    res = _run_db(engine, TUESDAY)["Written questions: missing headings"]
    assert res.status == hc.WARN
    assert res.detail.startswith("20 of 20")


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
    code = dhr.main(["--skip-web", *flags], engine=engine, now=now, sender=sender, env={})
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
