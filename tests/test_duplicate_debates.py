"""
Duplicate and swallowing debates (hansard_archive/dedupe.py,
scripts/find_duplicate_debates.py, scripts/delete_duplicate_debates.py,
refresh_partial_debates.py --accept-shrink, the ha_session_redirect redirect
and the archive cron's copy check). In-memory SQLite + mocked Hansard HTTP.

Scenarios are cut-down versions of what production held in Oct 2026:
  10 Jun 2026 "Speaker's Statement" holding all of PMQs (stored separately as
  "Engagements"); 15 Oct 2026 Westminster Hall "Human Rights and the
  Regulation of AI" stored five times under live, numeric and dead ids;
  18 May 2026 Lords "King's Speech" kept under a dead provisional id with
  small text edits beside the live final copy.

The S1-S9 tests come from an adversarial review (Oct 2026): live copies must
never be deleted, nor a speech held nowhere else, nor a real debate in
favour of the heading that swallowed it.
"""

from __future__ import annotations

import csv
import importlib.util
import itertools
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from hansard_archive import dedupe as dd
from hansard_archive import ingestion
from hansard_archive import refresh as rf

_ROOT = Path(__file__).parents[1]
JUNE = date(2026, 6, 10)
OCT = date(2026, 10, 15)
SATURDAY_NOON = datetime(2026, 10, 24, 12, 0)
WEDNESDAY_NOON = datetime(2026, 10, 21, 12, 0)

SPEAKER = (None, "Mr Speaker")
PM = (4514, "The Prime Minister (Keir Starmer)")
LOTO = (4011, "Kemi Badenoch (North West Essex) (Con)")
LD = (188, "Ed Davey (Kingston and Surbiton) (LD)")
SOBEL = (4893, "Alex Sobel (Leeds Central and Headingley) (Lab/Co-op)")
A = (100, "Member A")
B = (200, "Member B")

LIVE_GUID = "C5400E9A-3650-4AF8-A3EC-C404CB6792AB"
LIVE_NUMERIC = "26101550000005"
DEAD_UPPER = "9295C7F6-EF59-4903-9788-C17CAFFFFF29"
DEAD_LOWER_1 = "86f2ac86-7796-4eaa-b25c-31d2cb472ad6"
DEAD_LOWER_2 = "5d5a18d7-eca0-40bb-8c1a-18bd4c858cf5"
SPEAKER_EXT = "2F75C139-0B97-4A39-A7F8-6A83A0521C6B"
ENGAGEMENTS_EXT = "3219CC50-6B6A-4F88-8661-9E71FDEDF276"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pmqs(n: int = 8) -> list[tuple]:
    out = [(PM, "This morning I had meetings with ministerial colleagues and others. In addition "
                "to my duties in this House, I shall have further such meetings later today.")]
    for i in range(1, n):
        who = PM if i % 2 == 0 else (LOTO if i % 4 == 1 else LD)
        out.append((who, f"Prime Minister's Questions exchange number {i}, which is long enough "
                         f"to be told apart from the others."))
    return out


SPEAKER_TEXT = ("Before we come to Prime Minister's questions, I know that the thoughts of the "
                "House will be with the families affected.")


def jcr() -> list[tuple]:
    """A Select Committee statement in Westminster Hall: five named contributions."""
    return [
        (SOBEL, "Thank you very much. As the newly elected Chair, I am proud to be presenting the report."),
        (LD, "This is a fast-moving topic that has continued to be of deep concern to my constituents."),
        (SOBEL, "I thank my fellow Committee member for that question. In the report, we said a lot."),
        (LD, "My hon. Friend will not have failed to clock the comments from the industry this week."),
        (SOBEL, "First, AI may have the potential to replace many things, but never human judgement."),
    ]


def long(i, tag=""):
    return (f"Speech number {i}{tag} in this debate about something important, long enough to "
            f"count properly here.")


class FakeResp:
    def __init__(self, url, status=200, payload=None):
        self.url, self.status_code, self._payload = url, status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for {self.url}", response=self)


def items(rows):
    return [{"ItemType": "Contribution", "HRSTag": "hs_Para", "MemberId": m[0],
             "MemberName": m[1], "Value": f"<p>{t}</p>"} for m, t in rows]


def debate_json(ext, rows, title="Debate", day=JUNE, location="Commons Chamber"):
    return {"Overview": {"ExtId": ext, "Title": title, "Date": f"{day.isoformat()}T00:00:00",
                         "Location": location, "HRSTag": "hs_2DebBill",
                         "NextDebateExtId": None, "PreviousDebateExtId": None},
            "Items": items(rows), "ChildDebates": []}


ERROR_BODY = {"Message": "An error has occurred."}


class FakeHansard:
    """requests.get stand-in: payloads[ext_id] = JSON dict, an int status, or a list
    of those (one per successive call; the last one repeats)."""

    def __init__(self, payloads=None):
        self.payloads = dict(payloads or {})
        self.calls: list[str] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/search/debates.json"):
            if params.get("queryParameters.searchTerm"):
                return FakeResp(url, payload={"Results": []})
            return FakeResp(url, payload={"Results": [
                {"DebateSectionExtId": e, "Title": "x"} for e, p in self.payloads.items()
                if isinstance(p, dict) and p.get("Overview")]})
        ext = url.rsplit("/", 1)[-1].removesuffix(".json")
        got = self.payloads.get(ext, 404)
        if isinstance(got, list):
            got = got.pop(0) if len(got) > 1 else got[0]
        if isinstance(got, int):
            return FakeResp(url, got, payload=ERROR_BODY)
        return FakeResp(url, payload=got)


@pytest.fixture
def app(monkeypatch):
    import flask_app as _fa
    from extensions import db
    from sqlalchemy import inspect, text
    from divisions_models import HaDivision
    from hansard_archive import views
    from hansard_archive.models import (HaContributionRedirect, HaCronRun, HansardContribution,
                                        HansardSession, HansardSessionTheme, HaSessionRedirect,
                                        HaTagAttempt)
    monkeypatch.setattr(ingestion.time, "sleep", lambda s: None)
    monkeypatch.setattr(views, "_REDIRECT_TABLES", {"ready": False, "checked_at": None})

    def clear():
        db.session.rollback()
        for m in (HansardSessionTheme, HansardContribution, HansardSession, HaTagAttempt,
                  HaSessionRedirect, HaContributionRedirect, HaDivision):
            m.query.delete()
        _fa.Alert.query.delete()
        for prefix in ("test-dupes%", "refresh-partial-%"):     # incl. backfill checkpoints
            HaCronRun.query.filter(HaCronRun.service_name.like(prefix)).delete(
                synchronize_session=False)
        for name in inspect(db.session.connection()).get_table_names():
            if "_dedupe_" in name or "_refresh_bak_" in name:
                db.session.execute(text(f"DROP TABLE {name}"))   # test DB only
        db.session.commit()

    with _fa.app.app_context():
        db.create_all()
        clear()
        yield _fa.app
        clear()


@pytest.fixture
def hansard(monkeypatch):
    fake = FakeHansard()
    monkeypatch.setattr(ingestion.requests, "get", fake)
    return fake


_slugs = itertools.count(1)


def add_session(ext, title, rows, day=JUNE, house="Commons", hrs_tag="hs_2DebBill", slug=None,
                location=None):
    from extensions import db
    from hansard_archive.models import HansardContribution, HansardSession
    from hansard_archive.slugs import make_slug
    s = HansardSession(ext_id=ext, title=title, date=day, house=house, debate_type="debate",
                       hrs_tag=hrs_tag, is_container=False, contributions_ingested=True,
                       location=location, slug=slug or f"{make_slug(title, ext)}-{next(_slugs)}")
    db.session.add(s)
    db.session.flush()
    for order, (m, t) in enumerate(rows):
        db.session.add(HansardContribution(session_id=s.id, member_id=m[0] if m else None,
                                           member_name=m[1] if m else None, speech_text=t,
                                           party=None, speech_order=order))
    db.session.commit()
    return s.id


def named_count(sid):
    from hansard_archive.models import HansardContribution
    return (HansardContribution.query.filter(HansardContribution.session_id == sid,
                                             HansardContribution.member_name.isnot(None)).count())


def seed_june():
    """Speaker's Statement swallowed PMQs; PMQs stored again as Engagements."""
    speaker = add_session(SPEAKER_EXT, "Speaker’s Statement",
                          [(SPEAKER, SPEAKER_TEXT)] + pmqs(), hrs_tag="hs_2GenericHdg")
    engagements = add_session(ENGAGEMENTS_EXT, "Engagements", pmqs())
    other = add_session("B1E4C1D2-F4F5-4195-9D67-DB0A3CA17607", "Speaker’s Statement",
                        [(SPEAKER, "I should inform the House that the case is sub judice until "
                                   "sentencing.")])
    payloads = {
        SPEAKER_EXT: debate_json(SPEAKER_EXT, [(SPEAKER, SPEAKER_TEXT)]),
        ENGAGEMENTS_EXT: debate_json(ENGAGEMENTS_EXT, pmqs(), title="Engagements"),
    }
    return speaker, engagements, other, payloads


HR = "Human Rights and the Regulation of Artificial Intelligence"
JC = "Joint committee on Human Rights"


def seed_october():
    """Five copies of one Westminster Hall statement under two titles: two live ids, three dead."""
    ids = {}
    for ext, title, hrs in ((DEAD_UPPER, HR, "hs_2DebBill"),
                            (DEAD_LOWER_1, JC, "hs_6bBigBoldHdg"),
                            (LIVE_NUMERIC, JC, "hs_6bBigBoldHdg"),
                            (DEAD_LOWER_2, JC, "hs_6bBigBoldHdg"),
                            (LIVE_GUID, HR, "hs_2DebBill")):
        ids[ext] = add_session(ext, title, jcr(), day=OCT, hrs_tag=hrs)
    payloads = {LIVE_GUID: debate_json(LIVE_GUID, [], day=OCT),          # Hansard: empty heading
                LIVE_NUMERIC: debate_json(LIVE_NUMERIC, jcr(), day=OCT),
                DEAD_UPPER: 400, DEAD_LOWER_1: 400,
                DEAD_LOWER_2: ERROR_BODY}                                # 200 with the error body
    return ids, payloads


@pytest.fixture
def find():
    return _load("find_duplicate_debates", "scripts/find_duplicate_debates.py")


@pytest.fixture
def delete():
    return _load("delete_duplicate_debates", "scripts/delete_duplicate_debates.py")


@pytest.fixture
def backfill():
    return _load("refresh_partial_debates", "scripts/refresh_partial_debates.py")


def run_find(find, *argv, sleeps=None):
    args = find.build_parser().parse_args(["--since", "2026-01-01", *argv])
    return find.run(args, sleep=(sleeps.append if sleeps is not None else lambda s: None),
                    clock=lambda: 0.0)


def by_session(rows):
    """CSV rows by session id, values as the CSV file holds them (strings)."""
    return {int(r["session_id"]): {k: str(v) for k, v in r.items()} for r in rows}


def no_delete_keeps_a_deleted_twin(rows):
    deleted = {r["session_id"] for r in rows if r["action"] == "delete"}
    return all(r["kept_session_id"] not in deleted for r in rows if r["action"] == "delete")


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def test_final_id_format():
    assert dd.is_final_id(LIVE_GUID) and dd.is_final_id(DEAD_UPPER)
    assert not dd.is_final_id(LIVE_NUMERIC) and not dd.is_final_id(DEAD_LOWER_1)


def test_fuzzy_match_allows_edits_and_cut_off_speeches_but_not_another_member():
    a = dd.Contrib(1, 10, "A", "My Lords, it is a privilege to open this debate on the "
                               "Government's plans for home affairs and the justice system today.")
    edited = dd.Contrib(2, 10, "A (Lab)", a.text.replace("today", "this afternoon"))
    cut = dd.Contrib(3, 10, "A", a.text[:50])
    other = dd.Contrib(4, 11, "B", a.text)
    assert dd.texts_match(a, edited, fuzzy=True) and dd.texts_match(a, cut)
    assert not dd.texts_match(a, edited), "word overlap alone only counts for same-title sessions"
    assert not dd.texts_match(a, other)
    assert not dd.texts_match(a, dd.Contrib(5, 10, "A", "Something else entirely, said at length."),
                              fuzzy=True)


def test_S5_missing_member_id_compares_names():
    a = dd.Contrib(1, None, "Mr Speaker", "Order. I have to notify the House that the urgent "
                                          "question will be taken after the statement today.")
    b = dd.Contrib(2, 300, "Some MP", a.text.replace("today", "this afternoon"))
    assert not dd.texts_match(a, b, fuzzy=True)
    same = dd.Contrib(3, None, "The Parliamentary Under-Secretary of State, Ministry of Justice "
                               "(Baroness Levitt) (Lab)", a.text)
    assert dd.same_speaker(same, dd.Contrib(4, None, "Baroness Levitt", "x"))


def test_S6_similar_motions_on_different_instruments_are_not_the_same():
    a = dd.Contrib(1, 100, "Min", "I beg to move, That the draft Social Security Benefits Up-rating "
                                  "Order 2026, which was laid before this House on 12 January, "
                                  "be approved.")
    b = dd.Contrib(2, 100, "Min", "I beg to move, That the draft Guaranteed Minimum Pensions "
                                  "Increase Order 2026, which was laid before this House on "
                                  "12 January, be approved.")
    assert not dd.texts_match(a, b)


# ---------------------------------------------------------------------------
# B. The find script
# ---------------------------------------------------------------------------

def test_swallower_is_an_accept_shrink_candidate_when_hansard_matches_what_is_left(app, find, hansard):
    speaker, engagements, other, payloads = seed_june()
    hansard.payloads.update(payloads)
    hansard.payloads["B1E4C1D2-F4F5-4195-9D67-DB0A3CA17607"] = 400   # irrelevant: not involved
    res = run_find(find)
    rows = by_session(res["rows"])
    assert rows[speaker]["finding"] == "swallower" and rows[speaker]["action"] == "accept_shrink"
    assert rows[speaker]["hansard_named"] == "1" and rows[speaker]["named"] == "9"
    assert "Engagements" in rows[speaker]["note"]
    assert rows[engagements]["finding"] == "swallowed" and rows[engagements]["action"] == "keep"
    assert other not in rows, "a session sharing nothing with the others is not involved"
    assert res["exit_code"] == 0


def test_swallower_is_left_for_review_when_something_would_be_lost(app, find, hansard):
    speaker, engagements, _other, payloads = seed_june()
    from extensions import db
    from hansard_archive.models import HansardContribution
    # One PMQs exchange only the swallower holds.
    db.session.add(HansardContribution(session_id=speaker, member_id=LD[0], member_name=LD[1],
                                       speech_text="An extra question that nobody else stored, "
                                                   "long enough to count.", speech_order=99))
    db.session.commit()
    hansard.payloads.update(payloads)
    rows = by_session(run_find(find)["rows"])
    assert rows[speaker]["action"] == "review"
    assert "not held by any other stored session" in rows[speaker]["note"]
    assert "An extra question" in rows[speaker]["note"], "the speech that would be lost is listed"


def test_october_copies_only_dead_ones_go_and_the_empty_heading_is_reviewed(app, find, hansard,
                                                                            tmp_path):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    out = tmp_path / "dupes.csv"
    res = run_find(find, "--csv", str(out))
    rows = by_session(res["rows"])
    keep = ids[LIVE_NUMERIC]       # live AND the same as Hansard's copy (the GUID is now empty)
    assert rows[keep]["action"] == "keep" and rows[keep]["finding"] == "exact_duplicate"
    for ext in (DEAD_LOWER_1, DEAD_LOWER_2):
        r = rows[ids[ext]]
        assert (r["action"], r["kept_session_id"], r["api_status"]) == ("delete", str(keep), "dead")
    # The other title: its only live copy is an empty heading at Hansard -> by hand.
    assert rows[ids[LIVE_GUID]]["action"] == "review"
    assert rows[ids[DEAD_UPPER]]["action"] == "review"
    assert "now has 0 named" in rows[ids[DEAD_UPPER]]["note"]
    assert "another title" in rows[ids[DEAD_UPPER]]["note"]
    assert no_delete_keeps_a_deleted_twin(res["rows"])
    with open(out, encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 5


def test_200_error_body_is_dead_only_when_a_second_look_up_agrees(app, find, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    hansard.payloads[DEAD_LOWER_2] = [ERROR_BODY, debate_json(DEAD_LOWER_2, jcr(), day=OCT)]
    rows = by_session(run_find(find)["rows"])
    r = rows[ids[DEAD_LOWER_2]]
    assert r["api_status"] == "live" and r["action"] == "review"


def test_dead_copy_with_edited_text_is_superseded_by_the_live_one(app, find, hansard):
    live_rows = [(LD if i % 2 else PM, f"My Lords, speech {i} in the King's Speech debate on home "
                                       f"affairs, justice and the union, at some length.")
                 for i in range(12)]
    dead_rows = [(m, t.replace("at some length", "at length") if i % 3 == 0 else t)
                 for i, (m, t) in enumerate(live_rows[:10])]
    live = add_session("353770A7-17B3-4586-A7C6-8DA45E4E07A4", "King’s Speech", live_rows,
                       house="Lords")
    dead = add_session("2CE80D2C-A601-42B9-AB2E-6EBEA6067198", "King’s Speech", dead_rows,
                       house="Lords")
    hansard.payloads.update({
        "353770A7-17B3-4586-A7C6-8DA45E4E07A4": debate_json("353770A7-17B3-4586-A7C6-8DA45E4E07A4",
                                                            live_rows, location="Lords Chamber"),
        "2CE80D2C-A601-42B9-AB2E-6EBEA6067198": 400})
    rows = by_session(run_find(find)["rows"])
    assert rows[dead]["finding"] == "dead_superseded" and rows[dead]["action"] == "delete"
    assert rows[dead]["kept_session_id"] == str(live) and rows[dead]["coverage"] == "1.00"
    assert rows[live]["finding"] == "kept_twin" and rows[live]["action"] == "keep"


def test_no_api_recommends_no_deletes_or_shrinks(app, find, hansard):
    seed_october()
    seed_june()
    res = run_find(find, "--no-api")
    rows = by_session(res["rows"])
    assert hansard.calls == [] and res["api_calls"] == 0
    assert rows and {r["action"] for r in rows.values()} <= {"keep", "review"}
    assert all(r["api_status"] == "unknown" for r in rows.values())


def test_small_duplicates_are_only_for_review(app, find, hansard):
    a = add_session("AAAAAAAA-0000-0000-0000-000000000001", "Point of Order", [(SPEAKER, "Order.")])
    b = add_session("aaaaaaaa-0000-0000-0000-000000000002", "Point of Order", [(SPEAKER, "Order.")])
    hansard.payloads["AAAAAAAA-0000-0000-0000-000000000001"] = debate_json(
        "AAAAAAAA-0000-0000-0000-000000000001", [(SPEAKER, "Order.")])
    hansard.payloads["aaaaaaaa-0000-0000-0000-000000000002"] = 400
    rows = by_session(run_find(find)["rows"])
    assert rows[a]["action"] == "keep" and rows[b]["action"] == "review"


def test_S1_two_live_copies_with_different_titles_are_never_deleted(app, find, hansard):
    rows = [(A, long(1)), (B, long(2))]
    x = add_session("11111111-1111-1111-1111-111111111111", "Draft Foo Regulations 2026", rows)
    y = add_session("22222222-2222-2222-2222-222222222222", "Draft Bar Regulations 2026", rows)
    for ext in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"):
        hansard.payloads[ext] = debate_json(ext, rows)
    r = by_session(run_find(find)["rows"])
    assert r[x]["action"] == "review" and r[y]["action"] == "review"


def test_S1b_two_live_copies_with_the_same_title_keep_both(app, find, hansard):
    rows = [(A, long(1)), (B, long(2))]
    x = add_session("11111111-1111-1111-1111-111111111111", "Draft Foo Regulations 2026", rows)
    y = add_session("22222222-2222-2222-2222-222222222222", "Draft Foo Regulations 2026", rows)
    for ext in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"):
        hansard.payloads[ext] = debate_json(ext, rows)
    r = by_session(run_find(find)["rows"])
    assert r[x]["action"] == "keep" and r[y]["action"] == "review"
    assert "not dead" in r[y]["note"]


def test_S2_dead_copy_gets_the_real_debate_as_twin_not_the_swallower(app, find, hansard):
    pm = pmqs()
    w = add_session("AAAAAAAA-1111-1111-1111-111111111111", "Speaker's Statement",
                    [(SPEAKER, SPEAKER_TEXT)] + pm, hrs_tag="hs_2GenericHdg")
    f = add_session("BBBBBBBB-1111-1111-1111-111111111111", "Engagements", pm)
    d = add_session("cccccccc-1111-1111-1111-111111111111", "Engagements",
                    [(m, t + " x") if i == 0 else (m, t) for i, (m, t) in enumerate(pm)])
    hansard.payloads["AAAAAAAA-1111-1111-1111-111111111111"] = debate_json(
        "AAAAAAAA-1111-1111-1111-111111111111", [(SPEAKER, SPEAKER_TEXT)])
    hansard.payloads["BBBBBBBB-1111-1111-1111-111111111111"] = debate_json(
        "BBBBBBBB-1111-1111-1111-111111111111", pm)
    hansard.payloads["cccccccc-1111-1111-1111-111111111111"] = 400
    res = run_find(find)
    r = by_session(res["rows"])
    assert r[d]["kept_session_id"] == str(f) and r[d]["action"] == "delete"
    assert r[w]["action"] in ("accept_shrink", "review")
    assert no_delete_keeps_a_deleted_twin(res["rows"])


def test_S3_a_speech_held_nowhere_else_blocks_the_delete(app, find, hansard, delete):
    live_rows = [(A if i % 2 else B, long(i)) for i in range(10)]
    unique = "A completely unique long speech that exists nowhere else in our archive, about fisheries."
    dead_rows = live_rows[:9] + [(A, unique)]
    live = add_session("DDDDDDDD-1111-1111-1111-111111111111", "Fisheries", live_rows)
    dead = add_session("dddddddd-2222-1111-1111-111111111111", "Fisheries", dead_rows)
    hansard.payloads["DDDDDDDD-1111-1111-1111-111111111111"] = debate_json(
        "DDDDDDDD-1111-1111-1111-111111111111", live_rows)
    hansard.payloads["dddddddd-2222-1111-1111-111111111111"] = 400
    r = by_session(run_find(find)["rows"])
    assert r[dead]["action"] == "review" and "unique long speech" in r[dead]["note"]
    res = delete.run(delete.build_parser().parse_args(["--execute"]), [(dead, live)],
                     clock=lambda: SATURDAY_NOON, sleep=lambda s: None)
    assert res["deleted"] == [] and "held nowhere else" in res["refused"][0][2]
    assert named_count(dead) == 10


def test_S4_the_deleter_never_deletes_a_real_debate_to_keep_its_swallower(app, delete, hansard):
    speaker, engagements, _other, payloads = seed_june()
    hansard.payloads.update(payloads)
    res = delete.run(delete.build_parser().parse_args(["--execute"]), [(engagements, speaker)],
                     clock=lambda: SATURDAY_NOON, sleep=lambda s: None)
    assert res["deleted"] == [] and len(res["refused"]) == 1
    assert named_count(engagements) == 8


def test_S7_lords_and_commons_are_never_compared(app, find, hansard):
    rows = [(A, long(1)), (B, long(2))]
    c = add_session("EEEEEEEE-1111-1111-1111-111111111111", "Statement", rows, house="Commons")
    lords = add_session("eeeeeeee-1111-1111-1111-111111111111", "Statement", rows, house="Lords")
    hansard.payloads["EEEEEEEE-1111-1111-1111-111111111111"] = debate_json(
        "EEEEEEEE-1111-1111-1111-111111111111", rows)
    hansard.payloads["eeeeeeee-1111-1111-1111-111111111111"] = 400
    r = by_session(run_find(find)["rows"])
    assert c not in r and lords not in r


def test_S8_westminster_hall_and_chamber_copies_are_not_grouped(app, find, hansard):
    rows = [(A, long(1)), (B, long(2)), (A, long(3))]
    ch = add_session("FFFFFFFF-1111-1111-1111-111111111111", "Topic", rows)
    wh = add_session("ffffffff-1111-1111-1111-111111111111", "Topic", rows,
                     location="Westminster Hall")
    hansard.payloads["FFFFFFFF-1111-1111-1111-111111111111"] = debate_json(
        "FFFFFFFF-1111-1111-1111-111111111111", rows)
    hansard.payloads["ffffffff-1111-1111-1111-111111111111"] = 400
    r = by_session(run_find(find)["rows"])
    assert r[ch]["action"] == "review" and r[wh]["action"] == "review"


def test_S9_unchecked_copy_is_kept_over_a_dead_one_but_nothing_is_deleted(app, find, hansard):
    rows = [(A, long(1)), (B, long(2))]
    x = add_session("99999999-1111-1111-1111-111111111111", "T", rows)
    y = add_session("99999999-2222-1111-1111-111111111111", "T", rows)
    hansard.payloads["99999999-1111-1111-1111-111111111111"] = 503
    hansard.payloads["99999999-2222-1111-1111-111111111111"] = 400
    r = by_session(run_find(find)["rows"])
    assert r[x]["api_status"] == "unknown" and r[y]["api_status"] == "dead"
    assert r[y]["action"] == "review" and r[x]["action"] == "review"
    assert "not known to be dead" in r[y]["note"] and "(live in Hansard" not in r[y]["note"]


def test_api_checks_are_paced_and_stop_on_429(app, find, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    sleeps: list = []
    run_find(find, sleeps=sleeps)
    assert sleeps and min(sleeps) >= 1.0, "at least a second between look-ups"
    calls = [c for c in hansard.calls if "/debates/debate/" in c]
    assert len(calls) == 6, "each id once, plus a second look at the 200 error body"

    hansard.calls.clear()
    hansard.payloads[DEAD_UPPER] = 429
    res = run_find(find)
    assert res["exit_code"] == 3 and "429" in res["stopped"]


def test_find_prints_csv_and_one_ready_command_each(app, find, hansard, capsys, tmp_path):
    ids, payloads = seed_october()
    *_, june = seed_june()
    hansard.payloads.update(payloads)
    hansard.payloads.update(june)
    run_find(find)
    out = capsys.readouterr().out
    assert "----- CSV START -----" in out and "----- CSV END -----" in out
    pairs = {f"{ids[DEAD_LOWER_1]}:{ids[LIVE_NUMERIC]}", f"{ids[DEAD_LOWER_2]}:{ids[LIVE_NUMERIC]}"}
    line = next(x for x in out.splitlines() if "delete_duplicate_debates.py --session-id" in x)
    assert set(line.split("--session-id ")[1].split(",")) == pairs
    assert out.count("delete_duplicate_debates.py --session-id") == 1
    assert f"--accept-shrink --ext-id {SPEAKER_EXT}" in out
    assert "read-only" in out
    path = tmp_path / "d.csv"
    run_find(find, "--csv", str(path))
    assert f"delete_duplicate_debates.py --csv {path}" in capsys.readouterr().out


def test_find_writes_nothing(app, find, hansard):
    from hansard_archive.models import HansardContribution, HansardSession
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    before = (HansardSession.query.count(), HansardContribution.query.count())
    run_find(find)
    assert (HansardSession.query.count(), HansardContribution.query.count()) == before


# ---------------------------------------------------------------------------
# A. refresh_partial_debates.py --accept-shrink
# ---------------------------------------------------------------------------

def _patch_backfill_http(backfill, monkeypatch, payloads):
    def fake_get(url, timeout=None):
        ext = url.rsplit("/", 1)[-1].removesuffix(".json")
        got = payloads.get(ext, 404)
        if isinstance(got, int):
            return FakeResp(url, got, payload=ERROR_BODY)
        return FakeResp(url, payload=got)
    monkeypatch.setattr(backfill.requests, "get", fake_get)


def _bf(backfill, *argv):
    return backfill.run(backfill.build_parser().parse_args(list(argv)), sleep=lambda s: None,
                        clock=lambda: SATURDAY_NOON)


def test_accept_shrink_needs_ext_id_and_nothing_else(backfill):
    assert backfill.main(["--accept-shrink", "--since", "2026-04-27"]) == 2
    assert backfill.main(["--accept-shrink", "--since", "2026-04-27", "--ext-id", "X"]) == 2
    assert backfill.main(["--accept-shrink"]) == 2
    assert backfill.main(["--allow-empty", "--ext-id", "X"]) == 2


def test_held_shrink_is_applied_only_with_accept_shrink(app, backfill, monkeypatch):
    from extensions import db
    from sqlalchemy import text
    speaker, engagements, _o, payloads = seed_june()
    _patch_backfill_http(backfill, monkeypatch, payloads)

    res = _bf(backfill, "--ext-id", SPEAKER_EXT, "--execute")
    assert res["counts"]["held_shrank"] == 1 and named_count(speaker) == 9

    res = _bf(backfill, "--ext-id", SPEAKER_EXT, "--accept-shrink")
    assert res["counts"]["refresh"] == 1 and named_count(speaker) == 9, "dry run writes nothing"

    res = _bf(backfill, "--ext-id", SPEAKER_EXT, "--accept-shrink", "--execute")
    assert res["counts"]["refresh"] == 1
    assert named_count(speaker) == 1 and named_count(engagements) == 8
    undo = dict(db.session.execute(text(f"SELECT action, COUNT(*) FROM {res['undo_table']} "
                                        f"GROUP BY action")).fetchall())
    assert undo == {"delete": 8}


def test_accept_shrink_is_refused_at_apply_time_if_a_speech_would_be_lost(app, backfill,
                                                                          monkeypatch, capsys):
    speaker, engagements, _o, payloads = seed_june()
    from extensions import db
    from hansard_archive.models import HansardContribution
    # Since the review, Engagements was deleted / lost a speech: one PMQs row is only in the heading.
    row = (HansardContribution.query.filter_by(session_id=engagements)
           .order_by(HansardContribution.speech_order.desc()).first())
    db.session.delete(row)
    db.session.commit()
    _patch_backfill_http(backfill, monkeypatch, payloads)
    res = _bf(backfill, "--ext-id", SPEAKER_EXT, "--accept-shrink", "--execute")
    assert res["counts"]["shrink_refused"] == 1 and res["counts"]["refresh"] == 0
    assert named_count(speaker) == 9
    assert "SHRINK REFUSED" in capsys.readouterr().out


def test_emptying_needs_allow_empty_and_drops_tags_with_undo(app, backfill, monkeypatch):
    from extensions import db
    from sqlalchemy import text
    from hansard_archive.models import HansardSessionTheme
    ids, payloads = seed_october()
    sid = ids[LIVE_GUID]
    db.session.add(HansardSessionTheme(session_id=sid, theme="Science and technology",
                                       theme_type="policy_area"))
    db.session.commit()
    _patch_backfill_http(backfill, monkeypatch, payloads)

    res = _bf(backfill, "--ext-id", LIVE_GUID, "--execute")
    assert res["counts"]["held_empty"] == 1 and named_count(sid) == 5
    res = _bf(backfill, "--ext-id", LIVE_GUID, "--execute", "--accept-shrink")
    assert res["counts"]["shrink_refused"] == 1 and named_count(sid) == 5, "needs --allow-empty"

    res = _bf(backfill, "--ext-id", LIVE_GUID, "--execute", "--accept-shrink", "--allow-empty")
    assert res["counts"]["refresh"] == 1 and named_count(sid) == 0
    assert HansardSessionTheme.query.filter_by(session_id=sid).count() == 0, "tags removed"
    assert sid not in res["retag_ids"], "untagged now: the cron's tagger picks it up"
    tbak = [n for n in db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' "
                                               "AND name LIKE 'ha_session_theme_refresh_bak_%'"))]
    assert len(tbak) == 1
    assert db.session.execute(text(f"SELECT COUNT(*) FROM {tbak[0][0]}")).scalar() == 1


def test_assess_accept_shrink_unit():
    stored = [{"member_id": 1, "member_name": "A", "party": None, "speech_text": f"t{i}",
               "speech_order": i} for i in range(10)]
    assert rf.assess(stored, stored[:2]).state == rf.STATE_SHRANK
    assert rf.assess(stored, stored[:2], accept_shrink=True).state == rf.STATE_CHANGED
    assert rf.assess(stored, []).state == rf.STATE_EMPTY
    assert rf.assess(stored, [], accept_shrink=True).state == rf.STATE_CHANGED
    assert rf.assess([], [], accept_shrink=True).state == rf.STATE_UNCHANGED


def test_backfill_counts_dead_ids_and_does_not_stop_on_them(app, backfill, monkeypatch):
    ids, payloads = seed_october()
    _patch_backfill_http(backfill, monkeypatch, payloads)
    res = _bf(backfill, "--since", "2026-10-01", "--until", "2026-10-20")
    assert res["exit_code"] == 0
    assert res["counts"]["dead_id"] == 3 and res["counts"]["fetch_failed"] == 0


# ---------------------------------------------------------------------------
# C. The delete script, D. redirects
# ---------------------------------------------------------------------------

def _del(delete, pairs, *argv, clock=SATURDAY_NOON):
    return delete.run(delete.build_parser().parse_args(list(argv)), pairs,
                      clock=lambda: clock, sleep=lambda s: None)


def _snapshot():
    from extensions import db
    from sqlalchemy import text
    q = {"s": "SELECT id, ext_id, title, slug, date, house FROM ha_session ORDER BY id",
         "c": "SELECT id, session_id, member_name, speech_text, speech_order FROM ha_contribution "
              "ORDER BY id",
         "t": "SELECT id, session_id, theme, theme_type FROM ha_session_theme ORDER BY id",
         "a": "SELECT item_kind, item_id, attempts FROM ha_tag_attempt ORDER BY item_id",
         "d": "SELECT id, ha_session_id FROM ha_division ORDER BY id",
         "r": "SELECT old_slug, new_session_id FROM ha_session_redirect ORDER BY old_slug",
         "cr": "SELECT old_contribution_id, new_contribution_id FROM ha_contribution_redirect "
               "ORDER BY 1"}
    return {k: [tuple(r) for r in db.session.execute(text(v)).fetchall()] for k, v in q.items()}


def _add_extras(sid, keep):
    from extensions import db
    from divisions_models import HaDivision
    from hansard_archive.models import HansardSessionTheme, HaTagAttempt
    db.session.add_all([
        HansardSessionTheme(session_id=sid, theme="Science and technology", theme_type="policy_area"),
        HansardSessionTheme(session_id=sid, theme="ai regulation", theme_type="specific"),
        HansardSessionTheme(session_id=keep, theme="Science and technology", theme_type="policy_area"),
        HaTagAttempt(item_kind="session", item_id=sid, attempts=1),
        HaDivision(house="Commons", parliament_division_id=1, date=OCT, title="Division",
                   official_url="https://example.invalid/1", ha_session_id=sid),
    ])
    db.session.commit()


def _run_undo(sql):
    from extensions import db
    from sqlalchemy import text
    body = "".join(line for line in sql.splitlines(keepends=True)
                   if not line.lstrip().startswith("--"))
    for statement in (st.strip() for st in body.split(";") if st.strip()):
        if statement in ("BEGIN", "COMMIT"):     # the test runs inside its own transaction
            continue
        db.session.execute(text(statement))
    db.session.commit()
    db.session.expire_all()


def test_delete_dry_run_writes_nothing(app, delete, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    before = _snapshot()
    res = _del(delete, [(ids[DEAD_LOWER_1], ids[LIVE_NUMERIC])])
    assert res["exit_code"] == 0 and len(res["deleted"]) == 1 and res["undo"] is None
    assert _snapshot() == before


def test_delete_execute_backs_up_redirects_and_undo_restores(app, delete, hansard):
    from extensions import db
    from sqlalchemy import text
    from hansard_archive.models import HansardSession, HaSessionRedirect
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    sid, keep = ids[DEAD_LOWER_1], ids[LIVE_NUMERIC]
    _add_extras(sid, keep)
    # An older redirect that pointed at the copy being deleted moves to the twin.
    db.session.add(HaSessionRedirect(old_slug="older-copy-1234", new_session_id=sid))
    db.session.commit()
    old_slug = db.session.get(HansardSession, sid).slug
    before = _snapshot()

    res = _del(delete, [(sid, keep)], "--execute")
    assert res["refused"] and "division" in res["refused"][0][2], "divisions block without the flag"
    assert db.session.get(HansardSession, sid) is not None

    res = _del(delete, [(sid, keep)], "--execute", "--repoint-divisions",
               clock=datetime(2026, 10, 24, 12, 0, 1))
    assert len(res["deleted"]) == 1 and not res["refused"]
    db.session.expire_all()
    after = _snapshot()
    assert sid not in [s[0] for s in after["s"]]
    assert all(c[1] != sid for c in after["c"]) and all(t[1] != sid for t in after["t"])
    assert after["a"] == [] and after["d"] == [(after["d"][0][0], keep)]
    assert (old_slug, keep) in after["r"] and ("older-copy-1234", keep) in after["r"]
    assert len(after["cr"]) == 5, "every old #contrib- id maps to the twin's speech"
    assert [t for t in after["t"] if t[1] == keep] == [t for t in before["t"] if t[1] == keep], \
        "the twin's own tags are untouched"
    names = res["undo"]["names"]
    assert db.session.execute(text(f"SELECT COUNT(*) FROM {names['contrib']}")).scalar() == 5

    sql = delete.undo_sql(res["ts"], res["undo"])
    assert sql.count("BEGIN;") == 1 and "ON CONFLICT DO NOTHING" in sql
    _run_undo(sql)
    assert _snapshot() == before


def test_undo_can_be_run_for_one_session(app, delete, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    first, second = ids[DEAD_LOWER_1], ids[DEAD_LOWER_2]
    before = _snapshot()
    res = _del(delete, [(first, ids[LIVE_NUMERIC]), (second, ids[LIVE_NUMERIC])], "--execute")
    assert len(res["deleted"]) == 2
    from extensions import db
    one = delete.undo_sql(res["ts"], delete.load_undo(db, res["ts"]), [first])
    _run_undo(one)
    now = _snapshot()
    assert first in [s[0] for s in now["s"]] and second not in [s[0] for s in now["s"]]
    _run_undo(delete.undo_sql(res["ts"], res["undo"], [second]))
    assert _snapshot() == before


def test_delete_refusals(app, delete, hansard):
    from extensions import db
    from flask_app import Alert
    from hansard_archive.models import HansardContribution, HansardSession
    ids, payloads = seed_october()
    speaker, engagements, other, june = seed_june()
    hansard.payloads.update(payloads)
    hansard.payloads.update(june)
    recent = add_session("RECENT01-0000-0000-0000-000000000001", "Recent", jcr(), day=date(2026, 10, 22))
    recent_twin = add_session("RECENT01-0000-0000-0000-000000000002", "Recent", jcr(),
                              day=date(2026, 10, 22))
    first_contrib = (HansardContribution.query.filter_by(session_id=ids[DEAD_LOWER_1])
                     .order_by(HansardContribution.id).first())
    db.session.add(HansardContribution(session_id=ids[LIVE_NUMERIC], member_id=None,
                                       member_name=None, speech_text="x", speech_order=99,
                                       responds_to_id=first_contrib.id))
    alerted = db.session.get(HansardSession, ids[DEAD_LOWER_2]).slug
    db.session.add(Alert(title="t", snippet="s",
                         url=f"https://westminsterbrief.co.uk/archive/debate/15-october-2026/{alerted}"))
    db.session.commit()
    pairs = [(speaker, engagements),                    # different title
             (other, ids[LIVE_GUID]),                   # different day
             (ids[DEAD_UPPER], ids[DEAD_LOWER_1]),      # twin also listed for deletion
             (ids[DEAD_LOWER_1], ids[LIVE_NUMERIC]),    # responds_to from another session
             (ids[DEAD_LOWER_2], ids[LIVE_NUMERIC]),    # saved alert links to it
             (recent, recent_twin),                     # inside the cron's window
             (999999, ids[LIVE_GUID])]                  # missing
    before = _snapshot()
    res = _del(delete, pairs, "--execute")
    reasons = {sid: why for sid, _k, why in res["refused"]}
    assert "different title" in reasons[speaker]
    assert "is on 2026-10-15 Commons, not 2026-06-10" in reasons[other]
    assert "itself listed" in reasons[ids[DEAD_UPPER]]
    assert "respond" in reasons[ids[DEAD_LOWER_1]]
    assert "alert" in reasons[ids[DEAD_LOWER_2]]
    assert "cron" in reasons[recent]
    assert "not found" in reasons[999999]
    assert res["deleted"] == [] and _snapshot() == before


def test_delete_refuses_live_or_unchecked_copies_and_dead_twins(app, delete, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    hansard.payloads[DEAD_LOWER_2] = 503                     # can't check: unknown
    before = _snapshot()
    res = _del(delete, [(ids[LIVE_GUID], ids[DEAD_UPPER]),       # live copy, dead twin
                        (ids[DEAD_LOWER_2], ids[LIVE_NUMERIC])], "--execute")
    reasons = {sid: why for sid, _k, why in res["refused"]}
    assert "not confirmed dead" in reasons[ids[LIVE_GUID]]
    assert "unknown" in reasons[ids[DEAD_LOWER_2]]
    assert res["deleted"] == [] and _snapshot() == before


def test_delete_stops_before_writing_on_429(app, delete, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    hansard.payloads[DEAD_LOWER_1] = 429
    before = _snapshot()
    res = _del(delete, [(ids[DEAD_LOWER_1], ids[LIVE_NUMERIC])], "--execute")
    assert res["exit_code"] == 3 and res["deleted"] == [] and _snapshot() == before


def test_delete_reads_the_csv_and_needs_the_redirect_tables(app, delete, hansard, tmp_path):
    from extensions import db
    from sqlalchemy import text
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    path = tmp_path / "d.csv"
    path.write_text("finding,action,session_id,kept_session_id\n"
                    f"exact_duplicate,delete,{ids[DEAD_LOWER_1]},{ids[LIVE_NUMERIC]}\n"
                    f"exact_duplicate,keep,{ids[LIVE_NUMERIC]},\n"
                    f"exact_duplicate,review,{ids[DEAD_UPPER]},{ids[LIVE_GUID]}\n",
                    encoding="utf-8")
    assert delete.read_csv(str(path)) == [(ids[DEAD_LOWER_1], ids[LIVE_NUMERIC])]
    assert delete.parse_pairs(["1:2, 3:4", "5:6"]) == [(1, 2), (3, 4), (5, 6)]
    with pytest.raises(ValueError):
        delete.parse_pairs(["7"])

    db.session.execute(text("DROP TABLE ha_contribution_redirect"))   # test DB only
    db.session.commit()
    try:
        res = _del(delete, [(ids[DEAD_LOWER_1], ids[LIVE_NUMERIC])], "--execute")
        assert res["exit_code"] == 2 and res["deleted"] == []
    finally:
        migration = _load("session_redirect_table", "migrations/session_redirect_table.py")
        migration.upgrade(db.engine)
        migration.upgrade(db.engine)        # idempotent


def test_delete_execute_respects_cron_windows(app, delete, hansard):
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    res = _del(delete, [(ids[DEAD_LOWER_1], ids[LIVE_NUMERIC])], "--execute", clock=WEDNESDAY_NOON)
    assert res["exit_code"] == 4 and res["deleted"] == []


def test_deleted_slug_redirects_to_the_kept_copy(app, delete, hansard, monkeypatch):
    from extensions import db
    from hansard_archive.models import HansardContribution, HansardSession
    ids, payloads = seed_october()
    hansard.payloads.update(payloads)
    sid, keep = ids[DEAD_LOWER_1], ids[LIVE_NUMERIC]
    old_slug = db.session.get(HansardSession, sid).slug
    new_slug = db.session.get(HansardSession, keep).slug
    old_first = (HansardContribution.query.filter_by(session_id=sid)
                 .order_by(HansardContribution.speech_order).first().id)
    new_first = (HansardContribution.query.filter_by(session_id=keep)
                 .order_by(HansardContribution.speech_order).first().id)
    _del(delete, [(sid, keep)], "--execute")

    client = app.test_client()
    r = client.get(f"/archive/debate/15-october-2026/{old_slug}?mp=4893")
    assert r.status_code == 302, "302 while the clean-up is new"
    assert r.headers["Location"].endswith(f"/archive/debate/15-october-2026/{new_slug}?mp=4893")
    r = client.get(f"/archive/debate/15-october-2026/{old_slug}/word")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"{new_slug}/word")
    monkeypatch.setenv("ARCHIVE_REDIRECT_CODE", "301")
    assert client.get(f"/archive/debate/15-october-2026/{old_slug}").status_code == 301
    assert client.get("/archive/debate/15-october-2026/never-existed-0000").status_code == 404
    page = client.get(f"/archive/debate/15-october-2026/{new_slug}")
    assert page.status_code == 200
    assert f'"{old_first}": {new_first}' in page.get_data(as_text=True), "#contrib- map on the page"


def test_unknown_slug_404s_quietly_when_the_redirect_tables_are_missing(app, capsys):
    from extensions import db
    from sqlalchemy import text
    db.session.execute(text("DROP TABLE ha_session_redirect"))   # test DB only
    db.session.commit()
    try:
        client = app.test_client()
        assert client.get("/archive/debate/15-october-2026/gone-0000").status_code == 404
        assert client.get("/archive/debate/15-october-2026/gone-0001").status_code == 404
        assert "ha_session_redirect" not in capsys.readouterr().out, "no log line per 404"
    finally:
        _load("session_redirect_table", "migrations/session_redirect_table.py").upgrade(db.engine)


# ---------------------------------------------------------------------------
# E. The cron logs and counts copies and swallowed sessions (never deletes)
# ---------------------------------------------------------------------------

def test_cron_counts_a_new_session_that_sits_inside_a_stored_one(app, hansard, capsys):
    speaker = add_session(SPEAKER_EXT, "Speaker’s Statement", [(SPEAKER, SPEAKER_TEXT)] + pmqs())
    hansard.payloads[SPEAKER_EXT] = debate_json(SPEAKER_EXT, [(SPEAKER, SPEAKER_TEXT)] + pmqs())
    hansard.payloads[ENGAGEMENTS_EXT] = debate_json(ENGAGEMENTS_EXT, pmqs(), title="Engagements")
    stats = ingestion._new_refresh_stats()
    new = ingestion.ingest_date(JUNE, "Commons", verbose=False, refresh_existing=True,
                                refresh_stats=stats)
    assert new == 1 and stats["swallowed"] == 1 and stats["duplicates"] == 0
    assert "SWALLOWED?" in capsys.readouterr().out
    assert named_count(speaker) == 9, "nothing is deleted or shrunk"


def test_cron_does_not_count_a_parent_holding_its_child_debate(app, hansard, capsys):
    parent = debate_json(SPEAKER_EXT, [(SPEAKER, SPEAKER_TEXT)] + pmqs(), title="Parent")
    parent["ChildDebates"] = [{"Overview": {"ExtId": ENGAGEMENTS_EXT}}]
    hansard.payloads[SPEAKER_EXT] = parent
    hansard.payloads[ENGAGEMENTS_EXT] = debate_json(ENGAGEMENTS_EXT, pmqs(), title="Engagements")
    stats = ingestion._new_refresh_stats()
    ingestion.ingest_date(JUNE, "Commons", verbose=False, refresh_existing=True, refresh_stats=stats)
    assert stats["swallowed"] == 0 and stats["duplicates"] == 0
    assert "(parent/child)" in capsys.readouterr().out


def test_cron_counts_an_exact_copy_under_a_new_id(app, hansard, capsys):
    add_session(LIVE_NUMERIC, JC, jcr(), day=OCT)
    hansard.payloads[LIVE_GUID] = debate_json(LIVE_GUID, jcr(), title="Human Rights", day=OCT)
    stats = ingestion._new_refresh_stats()
    ingestion.ingest_date(OCT, "Commons", verbose=False, refresh_existing=True, refresh_stats=stats)
    assert stats["duplicates"] == 1 and stats["swallowed"] == 0
    assert "DUPLICATE?" in capsys.readouterr().out


def test_without_refresh_flag_there_is_no_copy_check(app, hansard):
    add_session(LIVE_NUMERIC, JC, jcr(), day=OCT)
    hansard.payloads[LIVE_GUID] = debate_json(LIVE_GUID, jcr(), title="Human Rights", day=OCT)
    stats = ingestion._new_refresh_stats()
    ingestion.ingest_date(OCT, "Commons", verbose=False, refresh_existing=False, refresh_stats=stats)
    assert stats["duplicates"] == 0


def test_copy_check_failure_never_stops_ingestion(app, hansard, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("check failed")
    monkeypatch.setattr(dd, "copies_and_swallows", boom)
    hansard.payloads[LIVE_GUID] = debate_json(LIVE_GUID, jcr(), title="Human Rights", day=OCT)
    stats = ingestion._new_refresh_stats()
    assert ingestion.ingest_date(OCT, "Commons", verbose=False, refresh_existing=True,
                                 refresh_stats=stats) == 1
    assert "duplicate check failed" in capsys.readouterr().out


def test_archive_cron_notes_count_swallowed_sessions(app, monkeypatch):
    from extensions import db
    from hansard_archive.models import HaCronRun
    cron = _load("archive_cron", "scripts/archive_cron.py")

    def fake_range(start, end, house="Commons", verbose=True, refresh_existing=False):
        return {"total_days": 3, "sitting_days": 1, "total_sessions": 1, "errors": 0,
                "unchanged": 0, "held": 1, "refreshed": 0, "refresh_errors": 0, "retag_ids": [],
                "swallowed": 1 if house == "Commons" else 0, "duplicates": 2}

    monkeypatch.setattr(cron, "ingest_date_range", fake_range)
    monkeypatch.setattr(cron, "tag_all_untagged", lambda limit, verbose: {"tagged": 0, "errors": 0})
    monkeypatch.setattr(cron.sys, "argv", ["archive_cron.py", "--service-name", "test-dupes"])
    cron.main()
    run = HaCronRun.query.filter_by(service_name="test-dupes").one()
    assert "refresh: refreshed=0 held=2 swallowed=1 duplicates=4" in run.notes
    assert run.status == "ok"
    db.session.commit()
