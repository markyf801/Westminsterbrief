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
