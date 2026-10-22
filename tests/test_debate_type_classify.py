"""
Debate-type classification against real Hansard API payloads.

Fixtures in tests/fixtures/hansard_debate_types/ are trimmed copies of
https://hansard-api.parliament.uk/debates/debate/<ExtId>.json (Overview plus the
first few Items) from sitting days in March-October 2026.

The bug these cover: Lords sessions all have HRSTag "NewDebate", and the only
Lords signal the old code used ("To ask His Majesty's Government..." as the first
speech) sits in an hs_TabledBy item that ingestion drops. So Lords questions,
statements, urgent-question repeats and take-note debates were stored as 'other'
("Proceedings") and never topic-tagged. Commons urgent questions were 'other'
because the HRSTag check was misspelt (hs_2cUrgenQuestion).
"""

import json
from pathlib import Path

import pytest

from hansard_archive.ingestion import (
    _classify_from_overview,
    _fetch_session_full,
    _flatten_items,
    _official_debate_type_label,
    _type_from_debate_type_label,
    classify_session,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hansard_debate_types"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _classify(data: dict) -> str:
    ov = data["Overview"]
    contributions = _flatten_items(data, [0])
    return classify_session(
        (ov.get("Title") or "").strip(), ov.get("Location") or "", ov.get("HRSTag") or "",
        ov.get("House") or "",
        debate_type_label=_official_debate_type_label(data),
        first_speech=contributions[0]["speech_text"] if contributions else "",
    )


@pytest.mark.parametrize("name, expected", [
    # Lords — official hs_DebateType label decides
    ("lords_question",                  "oral_questions"),
    ("lords_commons_urgent_question",   "urgent_question"),
    ("lords_private_notice_question",   "urgent_question"),
    ("lords_statement",                 "ministerial_statement"),
    ("lords_take_note_grand_committee", "debate"),            # was committee_stage (venue)
    ("lords_question_for_short_debate", "debate"),
    ("lords_motion_to_approve_code",    "statutory_instrument"),
    ("lords_committee_grand_committee", "committee_stage"),
    ("lords_committee_chamber",         "committee_stage"),   # "Committee (2nd Day) (Continued)"
    ("lords_arrangement_of_business",   "other"),
    ("lords_second_reading",            "debate"),
    # Commons — Overview HRSTag decides
    ("commons_urgent_question",         "urgent_question"),
    ("commons_speakers_statement",      "other"),
    ("commons_oral_question",           "oral_questions"),
    ("commons_westminster_hall",        "westminster_hall"),
    ("commons_statement",               "ministerial_statement"),
    ("commons_statement_generic_tag",   "ministerial_statement"),  # final record re-tagged hs_2DebBill
])
def test_real_payloads(name, expected):
    assert _classify(_load(name)) == expected


@pytest.mark.parametrize("name", [
    "lords_question", "lords_statement", "lords_commons_urgent_question",
    "lords_question_for_short_debate",
])
def test_old_path_misclassified_these(name):
    """Without the label (the old behaviour) these Lords items came out as 'other'."""
    data = _load(name)
    ov = data["Overview"]
    assert _classify_from_overview(ov["Title"], ov["Location"], ov["HRSTag"]) == "other"
    # ...and the first stored speech is not the "To ask..." line, so the old
    # opening-phrase override never fired.
    first = _flatten_items(data, [0])[0]["speech_text"].lower()
    assert not first.startswith("to ask his majesty")


def test_label_extraction():
    assert _official_debate_type_label(_load("lords_question")) == "Question"
    assert _official_debate_type_label(_load("commons_oral_question")) == ""


@pytest.mark.parametrize("label, expected", [
    ("Question", "oral_questions"),
    ("Committee (2nd Day) (Continued)", "committee_stage"),
    ("Motions to Approve", "statutory_instrument"),
    ("Motion to Regret", "statutory_instrument"),
    ("Second Reading (and remaining stages)", "debate"),
    ("Commons Reasons", "debate"),
    ("Something New", None),
    ("", None),
])
def test_label_mapping(label, expected):
    assert _type_from_debate_type_label(label) == expected


def test_unknown_label_falls_back_to_venue():
    assert _classify_from_overview("Some Item", "Grand Committee", "NewDebate", "Something New") == "committee_stage"


def test_lords_opening_phrase_fallback_only_without_label():
    ask = "To ask His Majesty’s Government what steps they are taking..."
    # No label (older payload shape): opening phrase still marks an oral question
    assert classify_session("Rail Fares", "Lords Chamber", "NewDebate", "Lords", "", ask) == "oral_questions"
    # A Question for Short Debate opens the same way — the label wins
    assert classify_session("Family Businesses", "Lords Chamber", "NewDebate", "Lords",
                            "Question for Short Debate", ask) == "debate"
    # Procedural titles stay procedural
    assert classify_session("Arrangement of Business", "Lords Chamber", "NewDebate", "Lords", "", ask) == "other"
    # Commons never uses the Lords fallback
    assert classify_session("Rail Fares", "Commons Chamber", "", "Commons", "", ask) == "other"


@pytest.mark.parametrize("title", [
    "Children’s Wellbeing and Schools Bill: Implementation",   # "bill" keyword -> debate
    "Renters’ Rights Act: Amendment of Guidance",               # "amendment" keyword
    "Water (Special Measures) Regulations 2025",                # made-SI title regex
    "Motion of No Confidence: Polling",                         # "motion" keyword
])
def test_lords_question_without_label_beats_title_keywords(title):
    ask = "To ask His Majesty’s Government what assessment they have made..."
    assert classify_session(title, "Lords Chamber", "NewDebate", "Lords", "", ask) == "oral_questions"
    # Without the opening phrase the title fallback still decides
    assert classify_session(title, "Lords Chamber", "NewDebate", "Lords", "", "My Lords, I beg to move") != "oral_questions"


def test_lords_question_fallback_never_overrides_structured_signals():
    ask = "To ask His Majesty’s Government what assessment they have made..."
    # Venue: a Grand Committee item opening this way is a Question for Short Debate
    assert classify_session("Schools Bill", "Grand Committee", "NewDebate", "Lords", "", ask) == "committee_stage"
    # Official label always wins
    assert classify_session("Schools Bill", "Lords Chamber", "NewDebate", "Lords",
                            "Second Reading", ask) == "debate"


def test_fetch_session_full_keeps_label(monkeypatch):
    data = _load("lords_statement")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr("hansard_archive.ingestion.requests.get", lambda *a, **k: _Resp())
    overview, contributions, _children = _fetch_session_full(data["Overview"]["ExtId"])
    assert overview["_DebateTypeLabel"] == "Statement"
    assert overview["Title"] == data["Overview"]["Title"]
    assert contributions  # speech items still flattened
