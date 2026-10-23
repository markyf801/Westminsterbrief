"""
Topic tags stored twice in ha_session_theme: the one-off clean-up
(scripts/dedupe_theme_rows.py), the optional unique-index migration, and the
tagger writing each tag once (hansard_archive/tagger.py write_session_tags).

SQLite in-memory; no network.
"""

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

import hansard_archive.tagger as tg
from extensions import db
from hansard_archive import models as m
from hansard_archive.tagger import TagOutcome

PROJECT_ROOT = Path(__file__).parents[1]
T1 = datetime(2026, 6, 1, 9, 0)
T2 = datetime(2026, 10, 20, 9, 0)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


dd = _load("scripts/dedupe_theme_rows.py", "dedupe_theme_rows")
mig = _load("migrations/theme_unique_indexes.py", "theme_unique_indexes")


@pytest.fixture
def engine():
    eng = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                           connect_args={"check_same_thread": False})
    db.metadata.create_all(eng, tables=[m.HansardSessionTheme.__table__, m.HaPQTheme.__table__])
    return eng


def _row(id_, session_id, theme, theme_type="policy_area", tagged_at=T1, model="flash-lite",
         confidence=None):
    return {"id": id_, "session_id": session_id, "theme": theme, "theme_type": theme_type,
            "tagged_at": tagged_at, "model_used": model, "confidence": confidence}


def _seed(engine, rows):
    with engine.begin() as conn:
        conn.execute(m.HansardSessionTheme.__table__.insert(), rows)


def _all(engine):
    with engine.connect() as conn:
        return sorted(tuple(r) for r in conn.execute(sa.text(
            "SELECT id, session_id, theme, theme_type, confidence, tagged_at, model_used "
            "FROM ha_session_theme")))


SEED = [
    _row(1, 10, "Health and social care"),
    _row(2, 10, "Health and social care"),            # same write as 1 (old tagger)
    _row(3, 10, "hospice funding", "specific"),
    _row(4, 11, "Economy", tagged_at=None, model=None),
    _row(5, 11, "Economy", tagged_at=T2, model="flash"),   # more informative than 4
    _row(6, 11, "Economy", tagged_at=T2, model="flash"),
    _row(7, 12, "Economy"),                            # another session: not a duplicate
    _row(8, 12, "Economy", "specific"),                # other theme_type: not a duplicate
]


# ── planning ──────────────────────────────────────────────────────────────────

def test_plan_finds_groups_and_keeps_the_most_informative_row(engine):
    _seed(engine, SEED)
    with engine.connect() as conn:
        plan = dd.plan_dedupe(conn)
    assert plan.total_rows == 8
    groups = {g.key: g for g in plan.groups}
    assert set(groups) == {(10, "Health and social care", "policy_area"), (11, "Economy", "policy_area")}
    health = groups[(10, "Health and social care", "policy_area")]
    assert (health.keep_id, health.delete_ids, health.same_write) == (1, [2], True)
    economy = groups[(11, "Economy", "policy_area")]
    assert (economy.keep_id, economy.delete_ids, economy.same_write) == (5, [4, 6], False)
    assert plan.delete_ids == [2, 4, 6]
    assert plan.by_type() == {"policy_area": 2}


def test_higher_confidence_wins_when_equally_filled(engine):
    _seed(engine, [_row(1, 10, "Economy", confidence=0.4), _row(2, 10, "Economy", confidence=0.9)])
    with engine.connect() as conn:
        (g,) = dd.plan_dedupe(conn).groups
    assert (g.keep_id, g.delete_ids) == (2, [1])


def test_missing_table_is_none():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        assert dd.plan_dedupe(conn) is None
        assert dd.count_duplicates(conn, "ha_pq_theme", ("pq_id", "theme", "theme_type")) is None


# ── execute + undo ────────────────────────────────────────────────────────────

def test_execute_backs_up_deletes_and_undo_restores(engine):
    _seed(engine, SEED)
    before = _all(engine)
    bak, deleted, cols = dd.execute_dedupe(engine, stamp="20261023_120000")
    assert (bak, deleted) == ("ha_theme_dedupe_bak_20261023_120000", 3)
    assert [r[0] for r in _all(engine)] == [1, 3, 5, 7, 8]
    with engine.connect() as conn:
        assert sorted(r[0] for r in conn.execute(sa.text(f"SELECT id FROM {bak}"))) == [2, 4, 6]
        assert dd.plan_dedupe(conn).groups == []

    # nothing left to do on a second run; a fresh backup name is never reused
    assert dd.execute_dedupe(engine, stamp="20261023_120000") == (None, 0, [])

    undo = dd.undo_sql(bak, cols)
    with engine.begin() as conn:
        conn.execute(sa.text(undo.split(";")[0]))
    assert _all(engine) == before


def test_undo_skips_rows_whose_kept_partner_is_gone(engine):
    """After a re-tag replaced session 11's tags, undo must not bring its old duplicates back."""
    _seed(engine, SEED)
    bak, _, cols = dd.execute_dedupe(engine, stamp="u")
    undo = dd.undo_sql(bak, cols)
    assert undo.startswith("-- Drop uq_ha_session_theme_session_theme_type first")
    assert "Only valid before any re-tag of these sessions" in undo
    assert "WHERE EXISTS (SELECT 1 FROM ha_session_theme k WHERE k.session_id = b.session_id" in undo
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM ha_session_theme WHERE session_id = 11"))   # re-tagged
        conn.execute(sa.text(undo.split(";")[0]))
    assert [r[0] for r in _all(engine)] == [1, 2, 3, 7, 8]      # 2 back; 4 and 6 not


def test_dry_run_lists_separate_write_sessions_with_a_retag_line(engine, capsys):
    _seed(engine, SEED + [_row(9, 13, "Transport", tagged_at=T1), _row(10, 13, "Transport", tagged_at=T2)])
    dd.main([], engine=engine)
    out = capsys.readouterr().out
    assert "separate-write sessions (two runs' tag sets mixed): 11,13" in out
    assert "python scripts/retag.py --what sessions --ids 11,13" in out


def test_backup_name_does_not_clobber_an_existing_table(engine):
    _seed(engine, SEED)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE ha_theme_dedupe_bak_x (id INTEGER)"))
    bak, _, _ = dd.execute_dedupe(engine, stamp="x")
    assert bak == "ha_theme_dedupe_bak_x_2"


def test_end_state_mismatch_rolls_back(engine, monkeypatch):
    _seed(engine, SEED)
    before = _all(engine)
    real = dd.plan_dedupe
    calls = []

    def flaky(conn):
        calls.append(1)
        plan = real(conn)
        if len(calls) == 2:                 # the re-check after deleting
            plan.groups.append(dd.Group((0, "x", "y"), [], 0, [], True))
        return plan
    monkeypatch.setattr(dd, "plan_dedupe", flaky)
    with pytest.raises(RuntimeError, match="end state check failed"):
        dd.execute_dedupe(engine, stamp="s")
    assert _all(engine) == before
    # Postgres rolls the CREATE TABLE back too; Python's sqlite3 runs DDL outside
    # the transaction, so here the backup table may remain, but empty.
    with engine.connect() as conn:
        if sa.inspect(conn).has_table("ha_theme_dedupe_bak_s"):
            assert conn.execute(sa.text("SELECT COUNT(*) FROM ha_theme_dedupe_bak_s")).scalar() == 0


# ── main ─────────────────────────────────────────────────────────────────────

def test_main_dry_run_writes_nothing(engine, capsys):
    _seed(engine, SEED)
    before = _all(engine)
    assert dd.main([], engine=engine) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "2 duplicate group(s), 3 row(s) to delete" in out
    assert "theme_type=policy_area: 2 group(s)" in out
    assert "1 group(s) from one write (same tagged_at + model_used), 1 from separate writes" in out
    assert "ha_pq_theme (pq_id, theme, theme_type): 0 duplicate group(s)" in out
    assert "ha_bill_theme: not present, skipped" in out
    assert _all(engine) == before


def test_main_execute_prints_undo(engine, capsys, monkeypatch):
    import hansard_archive.cron_windows as cw
    monkeypatch.setattr(cw, "blocked_window", lambda now, *a, **k: None)
    _seed(engine, SEED)
    assert dd.main(["--execute"], engine=engine) == 0
    out = capsys.readouterr().out
    assert "Deleted 3 row(s); copies in ha_theme_dedupe_bak_" in out
    assert "INSERT INTO ha_session_theme (id, session_id, theme, theme_type" in out
    assert len(_all(engine)) == 5


def test_main_execute_refuses_in_a_cron_window(engine, capsys, monkeypatch):
    import hansard_archive.cron_windows as cw
    monkeypatch.setattr(cw, "blocked_window", lambda now, *a, **k: "archive hourly")
    _seed(engine, SEED)
    assert dd.main(["--execute"], engine=engine) == 1
    assert "cron window (archive hourly)" in capsys.readouterr().out
    assert len(_all(engine)) == 8
    assert dd.main(["--execute", "--ignore-cron-windows"], engine=engine) == 0
    assert len(_all(engine)) == 5


def test_report_only_table_with_duplicates_exits_3_and_is_untouched(engine, capsys):
    with engine.begin() as conn:      # as if production lacked the model's unique constraint
        conn.execute(sa.text("CREATE TABLE ha_bill_theme (id INTEGER PRIMARY KEY, bill_id INTEGER, theme TEXT)"))
        conn.execute(sa.text("INSERT INTO ha_bill_theme VALUES (1, 5, 'Economy'), (2, 5, 'Economy')"))
    assert dd.main(["--execute", "--ignore-cron-windows"], engine=engine) == 3
    assert "ha_bill_theme (bill_id, theme): 1 duplicate group(s), 1 surplus row(s)" in capsys.readouterr().out
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM ha_bill_theme")).scalar() == 2


# ── unique-index migration ────────────────────────────────────────────────────

def test_migration_is_a_no_op_off_postgres(engine, capsys):
    assert mig.main(["--execute"], engine=engine) == 0
    assert "Not Postgres: nothing to do" in capsys.readouterr().out


class _FakeConn:
    def __init__(self, valid, create_error=None, dups=0):
        self.valid, self.create_error, self.dups = valid, create_error, dups

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execution_options(self, **kw):
        return self

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if sql.startswith("CREATE UNIQUE INDEX"):
            if self.create_error:
                raise self.create_error
            return None
        if "pg_index" in sql:
            row = None if self.valid is None else (self.valid,)
            return type("R", (), {"first": lambda _self: row})()
        return type("R", (), {"scalar": lambda _self: self.dups})()


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def connect(self):
        return self.conn


@pytest.mark.parametrize("valid, error, expect", [
    (True, None, None),
    (False, None, "INVALID after the build. Drop it with DROP INDEX CONCURRENTLY"),
    (None, None, "missing after the build. Run the dedupe again"),
    (False, RuntimeError("could not create unique index"),
     "the build failed (RuntimeError: could not create unique index); "
     "uq_ha_session_theme_session_theme_type is INVALID after the build. Drop it with DROP INDEX CONCURRENTLY"),
])
def test_migration_apply_checks_the_index_even_when_the_build_fails(valid, error, expect):
    problem = mig.apply(_FakeEngine(_FakeConn(valid, error)))
    if expect is None:
        assert problem is None
    else:
        assert expect in problem


def test_migration_refuses_while_duplicates_remain():
    problem = mig.apply(_FakeEngine(_FakeConn(True, dups=3)))
    assert problem.startswith("3 duplicate (session_id, theme, theme_type) group(s)")


def test_migration_sql_is_concurrent_and_idempotent():
    assert mig.CREATE_SQL.startswith("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ")
    assert "ON ha_session_theme (session_id, theme, theme_type)" in mig.CREATE_SQL


def test_migration_duplicate_count_query(engine):
    _seed(engine, SEED)
    with engine.connect() as conn:
        assert mig.duplicate_groups(conn) == 2
    dd.execute_dedupe(engine, stamp="s")
    with engine.connect() as conn:
        assert mig.duplicate_groups(conn) == 0


# ── the tagger writes each tag once ───────────────────────────────────────────

@pytest.fixture
def app_db():
    import flask_app as _fa
    with _fa.app.app_context():
        db.create_all()
        m.HansardSessionTheme.query.delete()
        m.HansardSession.query.delete()
        db.session.commit()
        s = m.HansardSession(ext_id="D1", title="Hospices", date=date(2026, 10, 14), house="Commons",
                             debate_type="debate", is_container=False, slug="hospices-d1")
        db.session.add(s)
        db.session.commit()
        yield s.id
        m.HansardSessionTheme.query.delete()
        m.HansardSession.query.delete()
        db.session.commit()


def _tags(session_id):
    return [(r.theme, r.theme_type) for r in
            m.HansardSessionTheme.query.filter_by(session_id=session_id).order_by(m.HansardSessionTheme.id)]


def test_write_session_tags_writes_a_repeated_tag_once(app_db):
    out = TagOutcome(policy_areas=["Economy", "Economy", "Health and social care"],
                     topics=["hospice funding", "hospice funding"], model="m")
    assert tg.write_session_tags(app_db, out) == 3
    assert _tags(app_db) == [("Economy", "policy_area"), ("Health and social care", "policy_area"),
                             ("hospice funding", "specific")]
    with db.engine.connect() as conn:
        assert dd.plan_dedupe(conn).groups == []


def test_second_writer_without_replace_writes_nothing(app_db):
    """Two runs tagging the same untagged session (hourly cron + backfill): both
    passed tag_session's "already tagged?" check before either wrote."""
    assert tg.write_session_tags(app_db, TagOutcome(policy_areas=["Economy"], model="a")) == 1
    assert (tg.write_session_tags(app_db, TagOutcome(policy_areas=["Economy", "Transport"], model="b"))
            == tg.ALREADY_TAGGED)
    assert _tags(app_db) == [("Economy", "policy_area")]


def test_replace_swaps_tags_without_leaving_duplicates(app_db):
    tg.write_session_tags(app_db, TagOutcome(policy_areas=["Economy"], model="a"))
    assert tg.write_session_tags(app_db, TagOutcome(policy_areas=["Economy", "Transport"], model="b"),
                                 replace=True) == 2
    assert _tags(app_db) == [("Economy", "policy_area"), ("Transport", "policy_area")]


def test_postgres_takes_a_per_session_advisory_lock(monkeypatch):
    calls = []

    class Bind:
        dialect = type("D", (), {"name": "postgresql"})()
    monkeypatch.setattr(tg.db.session, "get_bind", lambda *a, **k: Bind())
    monkeypatch.setattr(tg.db.session, "execute", lambda stmt, params=None: calls.append((str(stmt), params)))
    tg._lock_session_tags(42)
    assert calls == [("SELECT pg_advisory_xact_lock(CAST(:k AS integer), CAST(:i AS integer))",
                      {"k": tg._SESSION_TAGS_LOCK, "i": 42})]


def test_sqlite_takes_no_lock(app_db, monkeypatch):
    calls = []
    monkeypatch.setattr(tg.db.session, "execute", lambda *a, **k: calls.append(a))
    tg._lock_session_tags(app_db)
    assert calls == []


def test_tag_all_untagged_counts_a_lost_race_as_already_tagged_not_failed(app_db, monkeypatch, capsys):
    monkeypatch.setattr(tg, "tag_session", lambda sid, api_key=None, model=None: tg.ALREADY_TAGGED)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    res = tg.tag_all_untagged(api_key="key")
    assert (res["tagged"], res["failed"], res["already_tagged"]) == (0, 0, 1)
    out = capsys.readouterr().out
    assert "already tagged by another run" in out and "FAIL" not in out
