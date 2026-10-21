"""
scripts/retag.py: dry run writes nothing; --apply replaces tags; resumes from a
checkpoint in ha_cron_run; --backup snapshots; a result without a verified
policy area keeps the old tags; cron windows pause writes; stops on API failures.
"""

import importlib.util
from datetime import date, datetime
from pathlib import Path

import pytest

import hansard_archive.tagger as tg

_SCRIPT = Path(__file__).parents[1] / "scripts" / "retag.py"
APPLY = ["--apply", "--no-backup", "--ignore-cron-windows"]


@pytest.fixture(scope="module")
def retag():
    spec = importlib.util.spec_from_file_location("retag", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded(monkeypatch):
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import (HaCronRun, HansardContribution, HansardSession,
                                        HansardSessionTheme, HaPQ, HaPQTheme)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(tg, "_detect_model", lambda key: "gemini-2.5-flash-lite")
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    def clear():
        for m in (HansardSessionTheme, HansardContribution, HansardSession, HaPQTheme, HaPQ):
            m.query.delete()
        HaCronRun.query.filter(HaCronRun.service_name.like("retag-%")).delete(synchronize_session=False)
        db.session.commit()

    with _fa.app.app_context():
        db.create_all()
        clear()
        sessions = [HansardSession(ext_id=f"R{i}", title=f"Hares {i}", date=date(2026, 10, 13),
                                   house="Commons", debate_type="debate", is_container=False,
                                   slug=f"hares-r{i}") for i in range(3)]
        db.session.add_all(sessions)
        db.session.flush()
        for s in sessions:
            db.session.add(HansardContribution(
                session_id=s.id, member_name="Ruth Jones (Lab)", speech_order=1,
                speech_text="I beg to move a Bill to establish a close season for brown hares."))
            db.session.add(HansardSessionTheme(session_id=s.id, theme="Welfare and benefits",
                                               theme_type="policy_area"))
        pq = HaPQ(uin="901", question_text="what assessment has been made of farmland bird numbers.",
                  tabled_date=date(2026, 10, 13))
        db.session.add(pq)
        db.session.flush()
        db.session.add(HaPQTheme(pq_id=pq.id, theme="Welfare and benefits", theme_type="policy_area"))
        db.session.commit()
        yield _fa.app
        clear()


def _fake_ok(key, model, prompt, schema):
    if schema is tg._BATCH_RESPONSE_SCHEMA:
        return [{"uin": "901",
                 "policy_areas": [{"area": "Environment", "evidence": "farmland bird numbers"}],
                 "topics": [{"topic": "farmland birds", "evidence": "farmland bird numbers"}]}], {}
    return {"policy_areas": [{"area": "Environment", "evidence": "a close season for brown hares"}],
            "topics": [{"topic": "brown hares", "evidence": "close season for brown hares"}]}, {}


def _tags(app):
    from hansard_archive.models import HansardSessionTheme, HaPQTheme
    with app.app_context():
        return (sorted((t.session_id, t.theme) for t in HansardSessionTheme.query.all()),
                sorted(t.theme for t in HaPQTheme.query.all()))


def _checkpoints(app):
    from hansard_archive.models import HaCronRun
    with app.app_context():
        rows = HaCronRun.query.filter(HaCronRun.service_name.like("retag-%")).order_by(HaCronRun.id)
        return [(r.service_name.split("-")[1], r.status) for r in rows]


def test_dry_run_writes_nothing_and_estimates_cost(retag, seeded, monkeypatch, capsys):
    monkeypatch.setattr(tg, "_gemini_json", _fake_ok)
    before = _tags(seeded)
    assert retag.main([]) == 0
    assert _tags(seeded) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "estimated cost $" in out
    assert "before: ['Welfare and benefits']" in out and "after : ['Environment'] | ['brown hares']" in out
    assert _checkpoints(seeded) == []             # no checkpoint from a dry run


def test_apply_needs_a_backup_choice(retag, seeded):
    assert retag.main(["--apply"]) == 2
    assert retag.main(["--apply", "--backup", "--no-backup"]) == 2


def test_apply_replaces_tags_and_resumes(retag, seeded, monkeypatch, capsys):
    calls = []

    def counting(*a):
        calls.append(1)
        return _fake_ok(*a)
    monkeypatch.setattr(tg, "_gemini_json", counting)
    assert retag.main(APPLY) == 0
    sessions, pqs = _tags(seeded)
    assert {t for _, t in sessions} == {"Environment", "brown hares"} and len(sessions) == 6
    assert pqs == ["Environment", "farmland birds"]
    assert _checkpoints(seeded) == [("sessions", "ok"), ("pqs", "ok")]   # in ha_cron_run, not on disk

    calls.clear()
    assert retag.main(APPLY) == 0                 # same command: nothing left
    assert calls == []
    assert "already done" in capsys.readouterr().out


def test_backup_snapshots_the_theme_table(retag, seeded, monkeypatch):
    from sqlalchemy import inspect, text
    from extensions import db
    monkeypatch.setattr(tg, "_gemini_json", _fake_ok)
    assert retag.main(["--what", "sessions", "--apply", "--backup", "--ignore-cron-windows"]) == 0
    with seeded.app_context():
        bak = sorted(n for n in inspect(db.engine).get_table_names() if n.startswith("ha_session_theme_bak_"))
        assert bak
        rows = db.session.execute(text(f"SELECT theme FROM {bak[-1]}")).fetchall()
        assert {r[0] for r in rows} == {"Welfare and benefits"}       # the old tags
    assert {t for _, t in _tags(seeded)[0]} == {"Environment", "brown hares"}


def test_item_with_nothing_verified_keeps_old_tags(retag, seeded, monkeypatch):
    monkeypatch.setattr(tg, "_gemini_json", lambda *a: ({"policy_areas": [
        {"area": "Economy", "evidence": "words that are not in the text"}], "topics": []}, {}))
    assert retag.main(["--what", "sessions", *APPLY]) == 0
    sessions, _ = _tags(seeded)
    assert {t for _, t in sessions} == {"Welfare and benefits"}


def test_topics_only_result_keeps_old_policy_areas(retag, seeded, monkeypatch):
    monkeypatch.setattr(tg, "_gemini_json", lambda *a: ({"policy_areas": [], "topics": [
        {"topic": "brown hares", "evidence": "close season for brown hares"}]}, {}))
    assert retag.main(["--what", "sessions", *APPLY]) == 0
    sessions, _ = _tags(seeded)
    assert {t for _, t in sessions} == {"Welfare and benefits"}


def test_repeated_api_failure_exits_non_zero(retag, seeded, monkeypatch, capsys):
    def boom(*a):
        raise tg.GeminiCallFailed("HTTP 503")
    monkeypatch.setattr(tg, "_gemini_json", boom)
    assert retag.main(["--what", "sessions", *APPLY, "--max-failures", "2"]) == 3
    assert "STOPPED after API failures" in capsys.readouterr().out
    sessions, _ = _tags(seeded)
    assert {t for _, t in sessions} == {"Welfare and benefits"}
    assert _checkpoints(seeded) == [("sessions", "failed")]


def test_final_api_error_stops_at_once(retag, seeded, monkeypatch):
    calls = []

    def bad(*a):
        calls.append(1)
        raise tg.GeminiCallFailed("HTTP 400", final=True)
    monkeypatch.setattr(tg, "_gemini_json", bad)
    assert retag.main(["--what", "sessions", *APPLY]) == 3
    assert len(calls) == 1


@pytest.mark.parametrize("when, blocked", [
    (datetime(2026, 10, 20, 3, 5), "backup"),             # daily 03:00
    (datetime(2026, 10, 24, 2, 50), "backup"),            # Saturday, 5 min before + lookahead
    (datetime(2026, 10, 20, 7, 30), "health report"),
    (datetime(2026, 10, 20, 8, 0), "archive 08:00"),      # Tuesday morning catch-up
    (datetime(2026, 10, 19, 9, 0), "Mon also 09:00"),     # Monday PQ run
    (datetime(2026, 10, 21, 15, 30), "hourly"),           # Wednesday daytime, between runs
    (datetime(2026, 10, 23, 12, 0), "Fri"),               # Friday daytime
    (datetime(2026, 10, 23, 21, 0), None),                # Friday evening
    (datetime(2026, 10, 24, 12, 0), None),                # Saturday midday
    (datetime(2026, 10, 21, 23, 45), None),               # after the last weekday run
    (datetime(2026, 10, 24, 7, 50), None),                # Saturday: no archive run
    (datetime(2026, 10, 22, 7, 50), "archive 08:00"),     # Thursday, about to start
])
def test_shared_cron_windows(when, blocked):
    from hansard_archive.cron_windows import blocked_window
    label = blocked_window(when)
    if blocked is None:
        assert label is None
    else:
        assert label and blocked in label


def test_apply_refuses_to_start_in_a_cron_window(retag, seeded, monkeypatch, capsys):
    import hansard_archive.cron_windows as cw
    monkeypatch.setattr(tg, "_gemini_json", _fake_ok)
    monkeypatch.setattr(cw, "blocked_window", lambda now: "test window")
    assert retag.main(["--what", "sessions", "--apply", "--no-backup"]) == 4
    assert "STOPPED for cron window: test window" in capsys.readouterr().out
    assert {t for _, t in _tags(seeded)[0]} == {"Welfare and benefits"}


def test_window_mid_run_stops_after_chunk_and_resumes(retag, seeded, monkeypatch):
    import hansard_archive.cron_windows as cw
    monkeypatch.setattr(tg, "_gemini_json", _fake_ok)
    monkeypatch.setattr(retag, "_CHUNK", 1)
    checks = []

    def window(now):
        checks.append(1)
        return "test window" if len(checks) == 3 else None   # start ok, chunk 1 ok, chunk 2 blocked
    monkeypatch.setattr(cw, "blocked_window", window)
    assert retag.main(["--what", "sessions", "--apply", "--no-backup"]) == 4
    sessions, _ = _tags(seeded)
    assert sum(1 for _, t in sessions if t == "Environment") == 1          # one chunk written
    assert _checkpoints(seeded) == [("sessions", "stopped")]
    monkeypatch.setattr(cw, "blocked_window", lambda now: None)
    assert retag.main(["--what", "sessions", "--apply", "--no-backup"]) == 0
    assert {t for _, t in _tags(seeded)[0]} == {"Environment", "brown hares"}


def test_resumed_run_reuses_its_backup(retag, seeded, monkeypatch, capsys):
    from sqlalchemy import inspect
    from extensions import db
    import hansard_archive.cron_windows as cw
    monkeypatch.setattr(tg, "_gemini_json", _fake_ok)
    monkeypatch.setattr(retag, "_CHUNK", 1)
    calls = []

    def window(now):
        calls.append(1)
        return "test window" if len(calls) == 3 else None
    monkeypatch.setattr(cw, "blocked_window", window)
    with seeded.app_context():
        before = {n for n in inspect(db.engine).get_table_names() if "_bak_" in n}
    assert retag.main(["--what", "sessions", "--apply", "--backup"]) == 4
    monkeypatch.setattr(cw, "blocked_window", lambda now: None)
    assert retag.main(["--what", "sessions", "--apply", "--backup"]) == 0
    with seeded.app_context():
        made = {n for n in inspect(db.engine).get_table_names() if "_bak_" in n} - before
    assert len(made) == 1                                     # one snapshot for the whole job
    assert "backup: reusing" in capsys.readouterr().out


def test_estimate_cost(retag):
    cost, tin, tout = retag.estimate_cost("pq", 131000, 700, "gemini-2.5-flash-lite")
    assert 0 < cost < 100 and tin > 0 and tout > 0
    pro, _, _ = retag.estimate_cost("session", 7000, 12000, "gemini-2.5-pro")
    lite, _, _ = retag.estimate_cost("session", 7000, 12000, "gemini-2.5-flash-lite")
    assert pro > 10 * lite
    assert retag.estimate_cost("pq", 10, 700, "some-other-model")[0] is None


def test_missing_key(retag, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert retag.main([]) == 2
