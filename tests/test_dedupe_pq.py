"""
scripts/dedupe_pq.py — merging Written Questions stored more than once.

SQLite in-memory; no network.
"""

import importlib.util
import sys
import types
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
_CUT = "<p>" + "x" * 252 + "..."      # 258 chars: the bulk endpoint's cut-off copy
_FULL_ANSWER = "<p>" + "x" * 252 + " and the rest of the answer.</p>"
_STAMP = datetime(2026, 6, 5, 9, 0)   # same updated_at unless a test says otherwise


@pytest.fixture
def env(monkeypatch):
    from flask import Flask
    from sqlalchemy import text
    from extensions import db
    import cache_models  # noqa: F401  (cached_member, referenced by archive models)
    import hansard_archive.models  # noqa: F401

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True)
    db.init_app(app)
    monkeypatch.setitem(sys.modules, "flask_app", types.SimpleNamespace(app=app))
    spec = importlib.util.spec_from_file_location(
        "dedupe_pq", PROJECT_ROOT / "scripts" / "dedupe_pq.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with app.app_context():
        db.create_all()
        # Some other table pointing at ha_pq.id, to prove references are re-pointed.
        db.session.execute(text(
            "CREATE TABLE pq_note (id INTEGER PRIMARY KEY, "
            "pq_id INTEGER REFERENCES ha_pq(id), note TEXT)"))
        db.session.commit()
        yield mod, db
        db.session.remove()
        db.drop_all()
        db.session.execute(text("DROP TABLE IF EXISTS pq_note"))


def _run(mod, db, argv):
    """main() runs in its own app context (its own session): expire this
    test's objects afterwards so they are re-read from the database."""
    rc = mod.main(argv)
    db.session.expire_all()
    return rc


def _pq(db, **kw):
    from hansard_archive.models import HaPQ
    base = dict(uin="6978", question_text="To ask the Secretary of State, what steps.",
                tabled_date=date(2026, 6, 4), chamber="Commons", is_answered=False,
                updated_at=_STAMP)
    base.update(kw)
    row = HaPQ(**base)
    db.session.add(row)
    db.session.flush()
    return row


def _themes(db, pq_id):
    from hansard_archive.models import HaPQTheme
    return {t.theme for t in HaPQTheme.query.filter_by(pq_id=pq_id)}


def _add_themes(db, pq_id, *themes):
    from hansard_archive.models import HaPQTheme
    for t in themes:
        db.session.add(HaPQTheme(pq_id=pq_id, theme=t, theme_type="specific"))


def _notes(db):
    from sqlalchemy import text
    return dict(db.session.execute(text("SELECT id, pq_id FROM pq_note")).all())


def test_api_id_pair_keeps_most_complete_merges_themes_and_references(env):
    from sqlalchemy import text
    from hansard_archive.models import HaPQ
    mod, db = env
    sparse = _pq(db, api_id=1912500, heading=None, answer_text=_CUT, is_answered=True)
    full = _pq(db, api_id=1912500, heading="Vaccination", asking_member="A Member",
               answer_text=_FULL_ANSWER, is_answered=True,
               answer_date=date(2026, 6, 9))
    _add_themes(db, sparse.id, "vaccines", "heart failure")
    _add_themes(db, full.id, "vaccines")
    db.session.execute(text("INSERT INTO pq_note (id, pq_id, note) VALUES (1, :i, 'x')"),
                       {"i": sparse.id})
    db.session.commit()
    sparse_id, full_id = sparse.id, full.id

    assert _run(mod, db, ["--apply"]) == 0

    rows = HaPQ.query.all()
    assert [r.id for r in rows] == [full_id]          # has the answer date, though later
    assert _themes(db, full_id) == {"vaccines", "heart failure"}   # no duplicate theme
    assert _themes(db, sparse_id) == set()
    assert _notes(db) == {1: full_id}
    assert rows[0].answer_text == _FULL_ANSWER


def test_dry_run_is_the_default_and_writes_nothing(env):
    from hansard_archive.models import HaPQ, HaPQTheme
    mod, db = env
    a = _pq(db, api_id=5)
    b = _pq(db, api_id=5)
    _add_themes(db, b.id, "fish")
    db.session.commit()

    assert _run(mod, db, []) == 0
    assert HaPQ.query.count() == 2 and HaPQTheme.query.count() == 1
    assert {r.id for r in HaPQ.query} == {a.id, b.id}


def test_tie_keeps_earliest_id_and_fills_empty_fields(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    first = _pq(db, api_id=None, uin="HL123", heading=None, asking_member="Lord A",
                answering_body="Dept A")
    second = _pq(db, api_id=777, uin="hl123", heading="Rail", asking_member=None)
    db.session.commit()
    first_id = first.id
    assert second.id > first_id

    assert _run(mod, db, ["--apply"]) == 0

    row = HaPQ.query.one()
    assert row.id == first_id                            # same completeness: earliest id
    assert row.api_id == 777 and row.heading == "Rail"   # filled from the duplicate
    assert row.asking_member == "Lord A"                 # never overwritten
    assert row.uin == "HL123"


def test_never_overwrites_a_stored_value(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    keep = _pq(db, api_id=9, heading="Kept heading", answering_body="Dept A",
               answer_text="<p>Complete.</p>", is_answered=True)
    _pq(db, api_id=9, heading="Other heading", answering_body="Dept B")
    db.session.commit()
    _run(mod, db, ["--apply"])
    row = HaPQ.query.one()
    assert row.id == keep.id and row.heading == "Kept heading" and row.answering_body == "Dept A"


def test_different_api_ids_on_same_date_uin_left_for_review(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    _pq(db, api_id=1904612, uin="444")
    _pq(db, api_id=1721729, uin="444")
    db.session.commit()
    assert _run(mod, db, ["--apply"]) == 3
    assert HaPQ.query.count() == 2


def test_same_api_id_different_date_left_for_review(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    _pq(db, api_id=42, tabled_date=date(2025, 6, 2))
    _pq(db, api_id=42, tabled_date=date(2026, 6, 4))
    db.session.commit()
    assert _run(mod, db, ["--apply"]) == 3
    assert HaPQ.query.count() == 2


def test_same_uin_different_sessions_is_not_a_duplicate(env):
    mod, db = env
    from hansard_archive.models import HaPQ
    _pq(db, api_id=1, tabled_date=date(2025, 6, 2))
    _pq(db, api_id=2, tabled_date=date(2026, 6, 4))
    db.session.commit()
    assert mod.find_duplicate_groups(db.session, HaPQ) == []


def test_triple_merged_and_oversized_group_reviewed(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    for _ in range(3):
        _pq(db, api_id=11, uin="11")
    for _ in range(4):
        _pq(db, api_id=12, uin="12")
    db.session.commit()
    assert _run(mod, db, ["--apply"]) == 3
    assert HaPQ.query.filter_by(api_id=11).count() == 1
    assert HaPQ.query.filter_by(api_id=12).count() == 4


def test_many_groups_batched_and_rerun_is_clean(env, monkeypatch):
    from hansard_archive.models import HaPQ
    mod, db = env
    monkeypatch.setattr(mod, "KEY_BATCH", 7)
    for i in range(40):
        _pq(db, api_id=100 + i, uin=str(100 + i))
        _pq(db, api_id=100 + i, uin=str(100 + i))
    for i in range(15):                                   # date+uin duplicates, no api_id
        _pq(db, api_id=None, uin=str(900 + i))
        _pq(db, api_id=None, uin=str(900 + i))
    _pq(db, api_id=5000, uin="5000")                       # not a duplicate
    db.session.commit()

    assert len(mod.find_duplicate_groups(db.session, HaPQ)) == 55
    assert _run(mod, db, ["--apply"]) == 0
    assert HaPQ.query.count() == 56
    assert mod.find_duplicate_groups(db.session, HaPQ) == []
    assert _run(mod, db, ["--apply"]) == 0                      # idempotent


def test_limit(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    for i in range(3):
        _pq(db, api_id=200 + i, uin=str(200 + i))
        _pq(db, api_id=200 + i, uin=str(200 + i))
    db.session.commit()
    _run(mod, db, ["--apply", "--limit", "1"])
    assert HaPQ.query.count() == 5


def test_other_fk_refs_found_from_schema(env):
    mod, db = env
    assert ("pq_note", "pq_id") in mod.other_fk_refs(db.engine)
    assert ("ha_pq_theme", "pq_id") not in mod.other_fk_refs(db.engine)


# ── re-review: newest row wins; differing versions go to review ───────────────

def test_holding_vs_substantive_answer_left_for_review(env, caplog):
    from hansard_archive.models import HaPQ
    mod, db = env
    _pq(db, api_id=31, is_answered=True, is_holding=True, answer_date=date(2026, 9, 10),
        answer_text="<p>I will reply to the hon. Member as soon as possible.</p>")
    _pq(db, api_id=31, is_answered=True, is_holding=False, answer_date=date(2026, 9, 20),
        answer_text="<p>The Department has allocated funding.</p>")
    db.session.commit()
    assert _run(mod, db, ["--apply"]) == 3
    assert HaPQ.query.count() == 2
    assert "answer text differs" in caplog.text


def test_truncated_vs_full_answer_merged_full_kept(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    newer = _pq(db, api_id=32, is_answered=True, answer_text=_CUT, answer_date=date(2026, 9, 1),
                updated_at=datetime(2026, 9, 2, 8, 0))
    older = _pq(db, api_id=32, is_answered=True, answer_text=_FULL_ANSWER,
                answer_date=date(2026, 9, 1), updated_at=datetime(2026, 9, 1, 8, 0))
    db.session.commit()
    newer_id = newer.id
    assert older.id != newer_id
    assert _run(mod, db, ["--apply"]) == 0
    row = HaPQ.query.one()
    assert row.id == newer_id                       # latest updated_at wins …
    assert row.answer_text == _FULL_ANSWER          # … but takes the full answer


def test_answered_row_preferred_over_newer_unanswered_row(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    answered = _pq(db, api_id=33, is_answered=True, answer_text=_FULL_ANSWER,
                   answer_date=date(2026, 9, 1), updated_at=datetime(2026, 9, 1))
    _pq(db, api_id=33, heading="Schools", updated_at=datetime(2026, 10, 1))
    db.session.commit()
    answered_id = answered.id
    assert _run(mod, db, ["--apply"]) == 0
    row = HaPQ.query.one()
    assert row.id == answered_id and row.heading == "Schools"


def test_newer_updated_at_wins_when_answer_dates_agree(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    _pq(db, api_id=34, heading="Old heading", answering_body="Dept A")
    newer = _pq(db, api_id=34, heading="New heading", updated_at=datetime(2026, 10, 1))
    db.session.commit()
    newer_id = newer.id
    _run(mod, db, ["--apply"])
    row = HaPQ.query.one()
    assert row.id == newer_id and row.heading == "New heading"
    assert row.answering_body == "Dept A"          # empty field filled


def test_longer_question_text_kept_when_prefix(env):
    from hansard_archive.models import HaPQ
    mod, db = env
    _pq(db, api_id=35, question_text="To ask the Secretary of State for Education, what...",
        updated_at=datetime(2026, 10, 1))
    _pq(db, api_id=35, question_text="To ask the Secretary of State for Education, what "
                                     "steps she is taking on school funding.")
    db.session.commit()
    assert _run(mod, db, ["--apply"]) == 0
    assert HaPQ.query.one().question_text.endswith("school funding.")


@pytest.mark.parametrize("a, b", [
    (dict(question_text="To ask about fish quotas."), dict(question_text="To ask about rail.")),
    (dict(is_withdrawn=True), dict(is_withdrawn=False)),
    (dict(is_answered=True, answer_text="<p>Yes.</p>", answer_date=date(2026, 9, 1)),
     dict(is_answered=True, answer_text="<p>Yes.</p>", answer_date=date(2026, 9, 8))),
    (dict(is_answered=True, answer_text="<p>Yes.</p>", is_holding=True),
     dict(is_answered=True, answer_text="<p>Yes.</p>", is_holding=False)),
])
def test_rows_with_different_versions_left_for_review(env, a, b):
    from hansard_archive.models import HaPQ
    mod, db = env
    _pq(db, api_id=36, **a)
    _pq(db, api_id=36, **b)
    db.session.commit()
    assert _run(mod, db, ["--apply"]) == 3
    assert HaPQ.query.count() == 2
