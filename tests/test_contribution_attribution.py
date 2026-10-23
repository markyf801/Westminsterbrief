"""
Speaker attribution in stored Hansard contributions.

The bug (found 22 Oct 2026): on Lords pages ingested before 1 May 2026 every
speech after the first shows the name of the speaker *before* it, e.g. the
17 March 2026 oral question "Plan 2 Student Loans: Repayment Terms" shows the
Government Whip asking a hostile question and Baroness Deech giving the
Minister's answer.

How it happened:
  1. The original backfill (April 2026) had no HRSTag skip list, so it stored
     the named hs_TabledBy item ("Lord Balfe: To ask His Majesty's
     Government...") as an ordinary contribution row.
  2. On 1 May 2026 hs_tabledby joined _SKIP_HRS_TAGS, and
     scripts/refresh_speech_text.py re-fetched every session and copied the
     new list's texts onto the old rows *by position*. The new list is one
     item shorter, so every text moved up one row while the names stayed put.

Ingestion itself pairs each item's text with that item's own member (tested
below). The fixture is a trimmed copy of the real API response.
"""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from hansard_archive.ingestion import _clean_html, _flatten_items

FIXTURE = Path(__file__).parent / "fixtures" / "hansard_attribution" / "lords_oral_question_plan2.json"
_SCRIPTS = Path(__file__).parents[1] / "scripts"


def _data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _legacy_rows(data: dict) -> list[dict]:
    """What the pre-May 2026 ingestion stored once null-name rows were deleted:
    every named item with text, whatever its HRSTag."""
    rows = []
    for item in data["Items"]:
        text = _clean_html(item.get("Value") or "")
        if text and item.get("AttributedTo"):
            rows.append({"member_id": item.get("MemberId"), "member_name": item["AttributedTo"],
                         "speech_text": text})
    return rows


def _hansard_pairs(data: dict) -> dict[str, int]:
    """speech text -> member id, straight from the API items."""
    return {_clean_html(i["Value"]): i["MemberId"] for i in data["Items"]
            if i.get("MemberId") and _clean_html(i.get("Value") or "")}


# ---------------------------------------------------------------------------
# Ingestion pairs each speech with its own speaker
# ---------------------------------------------------------------------------

def test_flatten_items_pairs_each_speech_with_its_own_member():
    data = _data()
    pairs = _hansard_pairs(data)
    contribs = _flatten_items(data, [0])
    assert [c["member_id"] for c in contribs] == [1879, 4912, 1879, 4912, 3756, 4912]
    for c in contribs:
        assert pairs[c["speech_text"]] == c["member_id"]
    assert contribs[0]["speech_text"].startswith("My Lords, on behalf of my noble friend Lord Balfe")
    # The tabled question itself (hs_TabledBy) is not stored by current ingestion.
    assert not any(c["speech_text"].startswith("To ask") for c in contribs)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app_db():
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import HansardContribution, HansardSession

    with _fa.app.app_context():
        db.create_all()
        HansardContribution.query.delete()
        HansardSession.query.delete()
        db.session.commit()
        yield _fa.app, db, HansardSession, HansardContribution
        db.session.rollback()
        HansardContribution.query.delete()
        HansardSession.query.delete()
        db.session.commit()


def _add_session(db, HansardSession, HansardContribution, ext_id, rows, orders=None):
    s = HansardSession(ext_id=ext_id, title="Plan 2 Student Loans: Repayment Terms",
                       date=date(2026, 3, 17), house="Lords", debate_type="oral_questions",
                       location="Lords Chamber", hrs_tag="NewDebate", is_container=False,
                       contributions_ingested=True, slug=f"plan-2-{ext_id.lower()}")
    db.session.add(s)
    db.session.flush()
    for i, r in enumerate(rows):
        db.session.add(HansardContribution(
            session_id=s.id, member_id=r["member_id"], member_name=r["member_name"],
            speech_text=r["speech_text"],
            # Legacy rows have gaps where null-name rows were deleted.
            speech_order=orders[i] if orders else i))
    db.session.commit()
    return s


def _stored(HansardContribution, session_id):
    return (HansardContribution.query.filter_by(session_id=session_id)
            .order_by(HansardContribution.speech_order, HansardContribution.id).all())


# ---------------------------------------------------------------------------
# refresh_speech_text must never move text between speakers
# ---------------------------------------------------------------------------

def test_refresh_speech_text_keeps_each_text_with_its_speaker(app_db, monkeypatch):
    app, db, HansardSession, HansardContribution = app_db
    data = _data()
    script = _load_script("refresh_speech_text")
    monkeypatch.setattr(script, "_fetch_raw", lambda ext_id: data)

    legacy = _legacy_rows(data)
    s = _add_session(db, HansardSession, HansardContribution, "PLAN2", legacy,
                     orders=[3, 4, 5, 7, 8, 9, 10])
    script._refresh_session(s, dry_run=False)

    pairs = _hansard_pairs(data)
    for row in _stored(HansardContribution, s.id):
        assert pairs[row.speech_text] == row.member_id, (
            f"{row.member_name} shown with someone else's words: {row.speech_text[:50]!r}")


def test_refresh_speech_text_still_updates_aligned_sessions(app_db, monkeypatch):
    app, db, HansardSession, HansardContribution = app_db
    data = _data()
    script = _load_script("refresh_speech_text")
    monkeypatch.setattr(script, "_fetch_raw", lambda ext_id: data)

    fresh = [c for c in _flatten_items(data, [0]) if c["member_name"]]
    old = [dict(c, speech_text=c["speech_text"].replace(" ", "  ")) for c in fresh]
    s = _add_session(db, HansardSession, HansardContribution, "ALIGNED", old)
    updated, _ = script._refresh_session(s, dry_run=False)

    assert updated == len(fresh)
    assert [r.speech_text for r in _stored(HansardContribution, s.id)] == [c["speech_text"] for c in fresh]


# ---------------------------------------------------------------------------
# scripts/repair_shifted_contributions.py
# ---------------------------------------------------------------------------

def _shifted_rows(data: dict) -> list[dict]:
    """What production holds today: legacy speakers, texts moved up one row by
    the old positional refresh (the last row keeps its own text)."""
    legacy = _legacy_rows(data)
    fresh = [c for c in _flatten_items(data, [0]) if c["member_name"]]
    return [dict(r, speech_text=fresh[i]["speech_text"] if i < len(fresh) else r["speech_text"])
            for i, r in enumerate(legacy)]


@pytest.fixture
def repair(app_db, monkeypatch):
    app, db, HansardSession, HansardContribution = app_db
    data = _data()
    script = _load_script("repair_shifted_contributions")
    monkeypatch.setattr(script, "fetch_session_json", lambda ext_id: data)
    monkeypatch.setattr(script, "_DELAY", 0)
    fresh = [c for c in _flatten_items(data, [0]) if c["member_name"]]
    sessions = {
        "SHIFT": _add_session(db, HansardSession, HansardContribution, "SHIFT", _shifted_rows(data),
                              orders=[3, 4, 5, 7, 8, 9, 10]),
        "OK": _add_session(db, HansardSession, HansardContribution, "OK", fresh),
        "LEGACY": _add_session(db, HansardSession, HansardContribution, "LEGACY", _legacy_rows(data)),
    }
    return script, db, HansardContribution, sessions, data


def _args(**kw):
    import argparse
    base = dict(execute=False, house=None, since=None, until=None, ext_id=None, limit=None,
                samples=1, list_max=10)
    base.update(kw)
    return argparse.Namespace(**base)


def _snapshot(HansardContribution, session_id):
    return [(r.id, r.member_id, r.speech_text) for r in _stored(HansardContribution, session_id)]


def test_shifted_rows_reproduce_the_live_symptom():
    rows = _shifted_rows(_data())
    assert rows[0]["member_name"] == "Lord Balfe"
    assert rows[0]["speech_text"].startswith("My Lords, on behalf of my noble friend Lord Balfe")
    deech = next(r for r in rows if r["member_name"] == "Baroness Deech (CB)")
    assert deech["speech_text"].startswith("I always listen to the noble Baroness")


def test_repair_dry_run_classifies_and_writes_nothing(repair):
    script, db, HC, sessions, data = repair
    before = {k: _snapshot(HC, s.id) for k, s in sessions.items()}
    result = script.run(_args())
    assert result["counts"] == {"shifted": 1, "ok": 1, "legacy_unshifted": 1}
    assert result["updated"] == 5 and result["deleted"] == 1  # last row already had its own text
    assert result["undo_table"] is None
    assert {k: _snapshot(HC, s.id) for k, s in sessions.items()} == before


def test_repair_execute_fixes_shifted_session_only_and_can_be_undone(repair):
    from sqlalchemy import text
    script, db, HC, sessions, data = repair
    before = {k: _snapshot(HC, s.id) for k, s in sessions.items()}
    result = script.run(_args(execute=True))
    assert result["updated"] == 5 and result["deleted"] == 1  # last row already had its own text

    pairs = _hansard_pairs(data)
    fixed = _stored(HC, sessions["SHIFT"].id)
    assert [r.member_id for r in fixed] == [1879, 4912, 1879, 4912, 3756, 4912]
    for r in fixed:
        assert pairs[r.speech_text] == r.member_id
    # Each speaker keeps their own row id (links to a contribution stay with that peer).
    kept_ids = [rid for rid, mid, _ in before["SHIFT"][1:]]
    assert [r.id for r in fixed] == kept_ids
    assert _snapshot(HC, sessions["OK"].id) == before["OK"]
    assert _snapshot(HC, sessions["LEGACY"].id) == before["LEGACY"]

    # Second run finds nothing left to do.
    again = script.run(_args())
    assert again["counts"].get("shifted", 0) == 0 and again["counts"]["ok"] == 2

    # The documented undo restores the session exactly.
    bak = result["undo_table"]
    db.session.execute(text(
        f"UPDATE ha_contribution SET speech_text = b.old_text FROM {bak} b "
        f"WHERE b.action = 'update' AND ha_contribution.id = b.contribution_id"))
    db.session.execute(text(
        f"INSERT INTO ha_contribution (id, session_id, member_id, member_name, party, speech_text, "
        f"speech_order) SELECT contribution_id, session_id, member_id, member_name, party, old_text, "
        f"speech_order FROM {bak} WHERE action = 'delete'"))
    db.session.commit()
    assert _snapshot(HC, sessions["SHIFT"].id) == before["SHIFT"]


def test_repair_stops_on_429(repair, monkeypatch):
    script, db, HC, sessions, data = repair

    def _limited(ext_id):
        raise script.RateLimited(ext_id)

    monkeypatch.setattr(script, "fetch_session_json", _limited)
    before = _snapshot(HC, sessions["SHIFT"].id)
    result = script.run(_args(execute=True))
    assert result["stopped"] and "429" in result["stopped"]
    assert result["counts"] == {}
    assert _snapshot(HC, sessions["SHIFT"].id) == before


def test_repair_leaves_unrecognised_sessions_alone(repair):
    script, db, HC, sessions, data = repair
    other = [dict(r, member_id=9999) for r in _shifted_rows(data)]
    from hansard_archive.models import HansardSession
    s = _add_session(db, HansardSession, HC, "ODD", other)
    before = _snapshot(HC, s.id)
    result = script.run(_args(execute=True, ext_id=["ODD"]))
    assert result["counts"] == {"mismatch": 1}
    assert _snapshot(HC, s.id) == before


# ---------------------------------------------------------------------------
# Display names (hansard_archive.views._parse_attribution)
# Live on 22 Oct 2026: /archive/mp/4958 was titled "Con" and /archive/mp/4912
# "Baroness in Waiting/Government Whip".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, name, role, party", [
    # Peers styled "The ..." — bracket is the party, not a proxy name
    ("The Earl of Effingham (Con)", "The Earl of Effingham", "", None),
    ("The Lord Bishop of Leeds", "The Lord Bishop of Leeds", "", None),
    # Lords office holders: "Role (Peer) (Party)"
    ("Baroness in Waiting/Government Whip (Baroness Blake of Leeds) (Lab)",
     "Baroness Blake of Leeds", "Baroness in Waiting/Government Whip", "Lab"),
    ("The Parliamentary Under-Secretary of State, Department for Education "
     "(Baroness Smith of Malvern) (Lab)",
     "Baroness Smith of Malvern",
     "The Parliamentary Under-Secretary of State, Department for Education", "Lab"),
    ("The Deputy Speaker (Lord Bassam of Brighton) (Lab)",
     "Lord Bassam of Brighton", "The Deputy Speaker", "Lab"),
    # Unchanged formats
    ("The Lord Speaker (Lord Forsyth of Drumlean)", "Lord Forsyth of Drumlean", "The Lord Speaker", None),
    ("The Secretary of State for Education (Bridget Phillipson)",
     "Bridget Phillipson", "The Secretary of State for Education", None),
    ("Alice Macdonald (Norwich North) (Lab/Co-op)", "Alice Macdonald", "", "Lab/Co-op"),
    ("Mr Gagan Mohindra (South West Hertfordshire) (Con)", "Gagan Mohindra", "", "Con"),
    # A constituency starting with a peerage word is not a peer's name
    ("Sam Rushworth (Bishop Auckland) (Lab)", "Sam Rushworth", "", "Lab"),
    ("Baroness Deech (CB)", "Baroness Deech", "", None),
    ("Lord Balfe", "Lord Balfe", "", None),
])
def test_parse_attribution(raw, name, role, party):
    from hansard_archive.views import _parse_attribution
    got = _parse_attribution(raw)
    assert (got["name"], got["role"], got["party"]) == (name, role, party)


# ---------------------------------------------------------------------------
# Repair edge cases (review, 22 Oct 2026): several skipped items, items in
# ChildDebates, the same speaker twice in a row, a named item with no member
# id, an unnamed paragraph, and a named "in the Chair" line.
# ---------------------------------------------------------------------------

def _item(text, mid=None, name=None, tag="hs_para"):
    return {"ItemType": "Contribution", "MemberId": mid, "AttributedTo": name,
            "Value": text, "HRSTag": tag}


def _gc_data() -> dict:
    return {
        "Overview": {"ExtId": "GC1", "Title": "X Regulations 2026"},
        "Items": [
            _item("Motion to Consider", tag="hs_DebateType"),
            _item("Moved by", tag="hs_TabledBy"),
            _item("That the Grand Committee do consider the X Regulations 2026.", 101,
                  "Lord Alpha", "hs_TabledBy"),
            _item("<p>My Lords, I beg to move.</p><p>These regulations matter.</p>", 101,
                  "The Minister of State (Lord Alpha) (Lab)"),
            _item("I add a second point straight away.", 101, "Lord Alpha (Lab)"),
            _item("A quoted passage from the report.", 101, "Lord Alpha (Lab)", "hs_Quote"),
            _item("An unnamed paragraph of debate text."),
            _item("I have concerns about cost.", 202, "Baroness Beta (Con)"),
            _item("Hear, hear!", None, "Noble Lords"),
            _item("Lord Gamma in the Chair", 303, "Lord Gamma", "hs_76fChair"),
            _item("I want to ask about timing.", 303, "Lord Gamma (LD)"),
        ],
        "ChildDebates": [{
            "Items": [
                _item("That the draft Y Order 2026 be approved.", 404, "Baroness Delta",
                      "hs_TabledBy"),
                _item("My Lords, this order is linked.", 404, "Baroness Delta (Lab)"),
                _item("I thank the noble Baroness.", 101, "Lord Alpha (Lab)"),
            ],
            "ChildDebates": [],
        }],
    }


def _gc_rows(script, data, shifted: bool) -> list[dict]:
    legacy = script.build_lists(data)
    fresh = [e for e in legacy if e["kept"]]
    return [{"member_id": e["member_id"], "member_name": e["member_name"],
             "speech_text": (fresh[i]["speech_text"] if shifted and i < len(fresh)
                             else e["speech_text"])}
            for i, e in enumerate(legacy)]


@pytest.fixture
def gc(app_db, monkeypatch):
    app, db, HansardSession, HansardContribution = app_db
    data = _gc_data()
    script = _load_script("repair_shifted_contributions")
    monkeypatch.setattr(script, "fetch_session_json", lambda ext_id: data)
    monkeypatch.setattr(script, "_DELAY", 0)
    return script, db, HansardSession, HansardContribution, data


def _current_ingestion(data) -> list[tuple]:
    return [(c["member_id"], c["member_name"], c["speech_text"])
            for c in _flatten_items(data, [0]) if c["member_name"]]


def test_repair_gc_shape_ends_identical_to_current_ingestion(gc):
    script, db, HS, HC, data = gc
    rows = _gc_rows(script, data, shifted=True)
    assert len(rows) - len(_current_ingestion(data)) == 3  # three skipped named items
    s = _add_session(db, HS, HC, "GC1", rows, orders=[2 * i + 5 for i in range(len(rows))])
    result = script.run(_args(execute=True))
    assert result["counts"] == {"shifted": 1}
    assert result["deleted"] == 3
    stored = [(r.member_id, r.member_name, r.speech_text) for r in _stored(HC, s.id)]
    assert stored == _current_ingestion(data)
    # Running again (dry run and execute) changes nothing.
    assert script.run(_args())["counts"] == {"ok": 1}
    again = script.run(_args(execute=True))
    assert again["updated"] == 0 and again["deleted"] == 0
    assert [(r.member_id, r.member_name, r.speech_text) for r in _stored(HC, s.id)] == stored


def test_repair_gc_shape_correct_legacy_rows_left_alone(gc):
    script, db, HS, HC, data = gc
    s = _add_session(db, HS, HC, "GC1", _gc_rows(script, data, shifted=False))
    before = _snapshot(HC, s.id)
    result = script.run(_args(execute=True))
    assert result["counts"] == {"legacy_unshifted": 1}
    assert _snapshot(HC, s.id) == before


@pytest.mark.parametrize("change", ["extra_row", "missing_row", "speaker_swapped", "texts_edited"])
def test_repair_gc_shape_anything_unexpected_is_left_alone(gc, change):
    script, db, HS, HC, data = gc
    rows = _gc_rows(script, data, shifted=True)
    if change == "extra_row":
        rows.append({"member_id": 202, "member_name": "Baroness Beta (Con)", "speech_text": "Extra."})
    elif change == "missing_row":
        rows.pop(4)
    elif change == "speaker_swapped":
        rows[3], rows[4] = rows[4], rows[3]
    else:  # Hansard has since corrected several speeches
        for r in rows[:3]:
            r["speech_text"] += " (corrected)"
    s = _add_session(db, HS, HC, "GC1", rows)
    before = _snapshot(HC, s.id)
    result = script.run(_args(execute=True))
    assert result["counts"].get("shifted", 0) == 0
    assert _snapshot(HC, s.id) == before


def test_repair_stops_on_403(repair, monkeypatch):
    script, db, HC, sessions, data = repair

    def _refused(ext_id):
        raise script.Refused("HTTP 403")

    monkeypatch.setattr(script, "fetch_session_json", _refused)
    before = _snapshot(HC, sessions["SHIFT"].id)
    result = script.run(_args(execute=True))
    assert result["stopped"] and "403" in result["stopped"]
    assert result["counts"] == {}
    assert _snapshot(HC, sessions["SHIFT"].id) == before


def test_repair_stops_after_three_failed_fetches_in_a_row(repair, monkeypatch):
    script, db, HC, sessions, data = repair
    from hansard_archive.models import HansardSession
    for i in range(3):
        _add_session(db, HansardSession, HC, f"EXTRA{i}", _shifted_rows(data))
    calls = []

    def _flaky(ext_id):
        calls.append(ext_id)
        return data if len(calls) == 1 else None  # first works, then the API is down

    monkeypatch.setattr(script, "fetch_session_json", _flaky)
    result = script.run(_args())
    assert len(calls) == 4 and result["fetch_errors"] == 3
    assert result["stopped"] and "in a row" in result["stopped"]
