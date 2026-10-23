"""
Refreshing debates that were stored cut short (hansard_archive/refresh.py,
ingest_date(refresh_existing=True) in the archive cron, and the one-off
scripts/refresh_partial_debates.py). In-memory SQLite + mocked Hansard HTTP.

Covers: a debate stored while still going on is refreshed when it grows (same
session id and slug, no duplicate, existing row ids kept with their speech);
an unchanged debate is untouched; Hansard returning fewer contributions is
applied for a small drop and held for a big one; zero contributions or a fetch
error never wipe a session; re-tagging keeps old tags unless the new ones
verify; the backfill's dry run writes nothing, --execute keeps an undo table,
resumes from its checkpoint, stops on 429 and leaves pre-May Lords rows to the
attribution repair.

Review follow-ups (end of file): ids stay with their speech in repeating-speaker
debates and are never reused for another speaker's words; writers lock the
session and read rows after the lock; the cron's stricter shrink guard; debate
type for sessions stored before any speech; re-tag error handling and
carry-over; the backfill's default --until, cron windows for dry runs, 404s,
and the undo SQL round trip.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from hansard_archive import ingestion
from hansard_archive import refresh as rf
from hansard_archive import tagger as tg

_SCRIPT = Path(__file__).parents[1] / "scripts" / "refresh_partial_debates.py"
DAY = date(2026, 10, 12)
EXT = "54FB2EDB-83AF-40FE-8A98-A914AD221CD7"
SATURDAY_NOON = datetime(2026, 10, 24, 12, 0)
WEDNESDAY_NOON = datetime(2026, 10, 21, 12, 0)

PM = (4514, "The Prime Minister (Keir Starmer)")
LOTO = (4011, "Kemi Badenoch (North West Essex) (Con)")
LD = (188, "Ed Davey (Kingston and Surbiton) (LD)")


def item(member, text, hrs="hs_Para"):
    mid, name = member if member else (None, None)
    return {"ItemType": "Contribution", "HRSTag": hrs, "MemberId": mid, "MemberName": name,
            "Value": f"<p>{text}</p>"}


def speeches(n: int) -> list[dict]:
    """PM opens, then alternating questions and answers."""
    out = [item(PM, "With permission, I will make a statement on the international situation.")]
    for i in range(1, n):
        member = PM if i % 2 == 0 else (LOTO if i % 4 == 1 else LD)
        out.append(item(member, f"Speech number {i}."))
    return out


def session_json(items, ext_id=EXT, title="International Update", day=DAY) -> dict:
    return {"Overview": {"ExtId": ext_id, "Title": title, "Date": f"{day.isoformat()}T00:00:00",
                         "Location": "Commons Chamber", "HRSTag": "hs_2cStatement",
                         "NextDebateExtId": None, "PreviousDebateExtId": None},
            "Items": items, "ChildDebates": []}


class FakeResp:
    def __init__(self, url, status=200, payload=None):
        self.url, self.status_code, self._payload = url, status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for {self.url}", response=self)


class FakeHansard:
    """requests.get stand-in: one sitting day; sessions[ext_id] = JSON, an int status, or an exception."""

    def __init__(self):
        self.sessions: dict = {}
        self.calls: list[str] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/search/debates.json"):
            if params.get("queryParameters.searchTerm"):
                return FakeResp(url, payload={"Results": []})
            return FakeResp(url, payload={"Results": [{"DebateSectionExtId": e, "Title": "x"}
                                                      for e in self.sessions]})
        ext_id = url.rsplit("/", 1)[-1].removesuffix(".json")
        got = self.sessions.get(ext_id, 404)
        if isinstance(got, Exception):
            raise got
        if isinstance(got, int):
            return FakeResp(url, got)
        return FakeResp(url, payload=got)


@pytest.fixture
def app(monkeypatch):
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import (HaCronRun, HansardContribution, HansardSession,
                                        HansardSessionTheme, HaTagAttempt)
    monkeypatch.setattr(ingestion.time, "sleep", lambda s: None)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    def clear():
        for m in (HansardSessionTheme, HansardContribution, HansardSession, HaTagAttempt):
            m.query.delete()
        HaCronRun.query.filter(HaCronRun.service_name.like("refresh-partial-%")).delete(
            synchronize_session=False)
        db.session.commit()

    with _fa.app.app_context():
        db.create_all()
        clear()
        yield _fa.app
        db.session.rollback()
        clear()


@pytest.fixture
def hansard(monkeypatch):
    fake = FakeHansard()
    monkeypatch.setattr(ingestion.requests, "get", fake)
    return fake


def _rows(session_id):
    return rf.stored_rows(session_id)


def _session():
    from hansard_archive.models import HansardSession
    sessions = HansardSession.query.filter_by(ext_id=EXT).all()
    assert len(sessions) == 1, "never a duplicate session"
    return sessions[0]


def _ingest(refresh=True):
    stats = ingestion._new_refresh_stats()
    new = ingestion.ingest_date(DAY, "Commons", verbose=False, refresh_existing=refresh,
                                refresh_stats=stats)
    return new, stats


# ---------------------------------------------------------------------------
# The cron path: ingest_date(refresh_existing=True)
# ---------------------------------------------------------------------------

def test_debate_stored_while_in_progress_is_refreshed_when_it_grows(app, hansard):
    from extensions import db
    from hansard_archive.models import HansardSessionTheme

    # 15:00 run: the PM's statement has only just started; his speech is cut off.
    first = speeches(1)
    first[0] = item(PM, "With permission, I will make a statement on the")
    hansard.sessions[EXT] = session_json(first)
    assert _ingest() == (1, ingestion._new_refresh_stats())
    s = _session()
    sid, slug, url = s.id, s.slug, s.hansard_url
    db.session.add(HansardSessionTheme(session_id=sid, theme="Foreign affairs",
                                       theme_type="policy_area"))
    db.session.commit()
    first_row_id = _rows(sid)[0].id

    # 16:00 run: Hansard now has the whole exchange.
    hansard.sessions[EXT] = session_json(speeches(200))
    new, stats = _ingest()
    assert new == 0
    assert stats["refreshed"] == 1 and stats["retag_ids"] == [sid]

    s = _session()
    assert (s.id, s.slug, s.hansard_url) == (sid, slug, url)
    rows = _rows(sid)
    assert len(rows) == 200
    assert rows[0].id == first_row_id, "the opening speech keeps its row (and #contrib- link)"
    assert rows[0].speech_text.endswith("international situation.")
    assert [r.speech_order for r in rows] == list(range(200))
    assert HansardSessionTheme.query.filter_by(session_id=sid).count() == 1, "tags kept"


def test_unchanged_debate_is_untouched(app, hansard):
    hansard.sessions[EXT] = session_json(speeches(5))
    _ingest()
    sid = _session().id
    before = [(r.id, r.speech_text, r.ingested_at) for r in _rows(sid)]

    # Only whitespace differs: not a change.
    hansard.sessions[EXT] = session_json(
        [dict(i, Value=i["Value"].replace(" ", "  ")) for i in speeches(5)])
    new, stats = _ingest()
    assert (new, stats["refreshed"], stats["unchanged"], stats["held"]) == (0, 0, 1, 0)
    assert [(r.id, r.speech_text, r.ingested_at) for r in _rows(sid)] == before


def test_without_refresh_flag_stored_sessions_are_skipped_as_before(app, hansard):
    hansard.sessions[EXT] = session_json(speeches(2))
    _ingest(refresh=False)
    hansard.sessions[EXT] = session_json(speeches(9))
    new, stats = _ingest(refresh=False)
    assert new == 0 and stats["refreshed"] == 0
    assert len(_rows(_session().id)) == 2


def test_small_drop_is_applied_and_trailing_rows_removed(app, hansard):
    hansard.sessions[EXT] = session_json(speeches(10))
    _ingest()
    sid = _session().id
    ids = [r.id for r in _rows(sid)]

    hansard.sessions[EXT] = session_json(speeches(7))
    _, stats = _ingest()
    assert stats["refreshed"] == 1
    assert [r.id for r in _rows(sid)] == ids[:7]


def test_big_drop_is_held(app, hansard, capsys):
    hansard.sessions[EXT] = session_json(speeches(10))
    _ingest()
    sid = _session().id
    ids = [r.id for r in _rows(sid)]

    hansard.sessions[EXT] = session_json(speeches(3))
    _, stats = _ingest()
    assert stats["held"] == 1 and stats["refreshed"] == 0
    assert [r.id for r in _rows(sid)] == ids
    assert "HOLD" in capsys.readouterr().out


def test_zero_contributions_or_fetch_error_never_wipe_a_session(app, hansard):
    hansard.sessions[EXT] = session_json(speeches(6))
    _ingest()
    sid = _session().id

    hansard.sessions[EXT] = session_json([])          # 200 with no speeches
    _, stats = _ingest()
    assert stats["held"] == 1
    assert len(_rows(sid)) == 6

    hansard.sessions[EXT] = 500                        # fetch error: session not in the walk
    _, stats = _ingest()
    assert stats == ingestion._new_refresh_stats()
    assert len(_rows(sid)) == 6


def test_speech_inserted_in_the_middle_keeps_later_ids_with_their_speech(app, hansard):
    hansard.sessions[EXT] = session_json(speeches(4))
    _ingest()
    sid = _session().id
    by_text = {r.speech_text: r.id for r in _rows(sid)}

    grown = speeches(4)
    grown.insert(2, item(LD, "A point of order, Madam Deputy Speaker."))
    hansard.sessions[EXT] = session_json(grown)
    _, stats = _ingest()
    assert stats["refreshed"] == 1
    rows = _rows(sid)
    assert len(rows) == 5
    for r in rows:
        if r.speech_text in by_text:
            assert r.id == by_text[r.speech_text], "each kept row still holds its own speech"
    assert rows[2].speech_text == "A point of order, Madam Deputy Speaker."
    assert rows[2].id not in by_text.values(), "the new speech is a new row"


def test_containers_are_not_refreshed(app, hansard):
    from extensions import db
    hansard.sessions[EXT] = session_json(speeches(2))
    _ingest()
    s = _session()
    s.is_container = True
    db.session.commit()
    hansard.sessions[EXT] = session_json(speeches(8))
    _, stats = _ingest()
    assert stats["refreshed"] == 0
    assert len(_rows(s.id)) == 2


def test_refresh_lets_the_tagger_try_again(app, hansard):
    from extensions import db
    from hansard_archive.models import HaTagAttempt
    hansard.sessions[EXT] = session_json(speeches(1))
    _ingest()
    sid = _session().id
    db.session.add(HaTagAttempt(item_kind="session", item_id=sid, attempts=3))
    db.session.commit()
    hansard.sessions[EXT] = session_json(speeches(30))
    _, stats = _ingest()
    assert stats["refreshed"] == 1
    assert db.session.get(HaTagAttempt, ("session", sid)) is None


# ---------------------------------------------------------------------------
# Pure alignment rules
# ---------------------------------------------------------------------------

def _d(member, text, order):
    return {"member_id": member[0], "member_name": member[1], "party": None,
            "speech_text": text, "speech_order": order}


def test_plan_reuses_rows_by_speaker_not_position():
    stored = [_d(PM, "a", 0), _d(LOTO, "b", 1), _d(PM, "c", 2)]
    fresh = [_d(PM, "a", 0), _d(LD, "x", 1), _d(LOTO, "b", 2), _d(PM, "c", 3)]
    ops = rf.plan_ops(stored, fresh)
    assert [(a, i) for a, i, _ in ops] == [("keep", 0), ("insert", None), ("update", 1), ("update", 2)]
    # rows 1 and 2 only change speech_order; their speaker and text stay theirs
    assert [e["speech_text"] for a, i, e in ops if a == "update"] == ["b", "c"]


def test_needs_retag_only_for_material_changes():
    base = [_d(PM, "x" * 1000, 0)]
    assert not rf.assess(base, [_d(PM, "x" * 1050, 0)]).needs_retag      # 5% longer
    assert rf.assess(base, [_d(PM, "x" * 1200, 0)]).needs_retag          # 20% longer
    assert rf.assess(base, base + [_d(LD, "y", 1)]).needs_retag          # one more speech


# ---------------------------------------------------------------------------
# Re-tagging refreshed sessions
# ---------------------------------------------------------------------------

def test_retag_replaces_tags_only_when_new_ones_verify(app, hansard, monkeypatch):
    from extensions import db
    from hansard_archive.models import HansardSession, HansardSessionTheme
    ids = []
    for i in range(3):
        s = HansardSession(ext_id=f"T{i}", title=f"Debate {i}", date=DAY, house="Commons",
                           debate_type="debate", is_container=False, slug=f"debate-t{i}")
        db.session.add(s)
        db.session.flush()
        db.session.add(ingestion.HansardContribution(session_id=s.id, member_name="A (Lab)",
                                                     speech_text="Hares.", speech_order=0))
        if i < 2:
            db.session.add(HansardSessionTheme(session_id=s.id, theme="Old area",
                                               theme_type="policy_area"))
        ids.append(s.id)
    db.session.commit()

    def fake_tag(si, api_key, model=None):
        if si.title == "Debate 0":
            return tg.TagOutcome(policy_areas=["Environment"], topics=["brown hares"])
        return tg.TagOutcome()
    monkeypatch.setattr(tg, "tag_session_input", fake_tag)

    stats = rf.retag_refreshed(ids, api_key="k", verbose=False, sleep=lambda s: None)
    assert (stats["retagged"], stats["kept_old"], stats["untagged"]) == (1, 1, 1)
    themes = lambda sid: sorted(t.theme for t in HansardSessionTheme.query.filter_by(session_id=sid))  # noqa: E731
    assert themes(ids[0]) == ["Environment", "brown hares"]
    assert themes(ids[1]) == ["Old area"]
    assert themes(ids[2]) == []


# ---------------------------------------------------------------------------
# The one-off backfill script
# ---------------------------------------------------------------------------

@pytest.fixture
def script(monkeypatch):
    spec = importlib.util.spec_from_file_location("refresh_partial_debates", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _args(script, *extra):
    return script.build_parser().parse_args(["--since", "2026-10-01", *extra])


def _seed_backfill():
    """Three stored sessions: one cut short, one complete, one Lords legacy (Asked by row)."""
    from extensions import db
    from hansard_archive.models import HansardSession
    made = {}
    for ext, house, n in (("A-SHORT", "Commons", 2), ("B-SAME", "Commons", 4), ("C-LEGACY", "Lords", None)):
        s = HansardSession(ext_id=ext, title=f"Debate {ext}", date=DAY, house=house,
                           debate_type="debate", is_container=False, slug=f"d-{ext.lower()}",
                           contributions_ingested=True)
        db.session.add(s)
        db.session.flush()
        for c in ingestion._flatten_items({"Items": speeches(n)}, [0]) if n else []:
            db.session.add(ingestion.HansardContribution(session_id=s.id, **c))
        made[ext] = s.id
    # Legacy Lords rows: "Asked by" row kept, texts as stored before May 2026.
    asker, minister = (100, "Lord Asker (Con)"), (200, "Baroness Minister (Lab)")
    for order, (m, text) in enumerate(((asker, "Asked by Lord Asker: To ask His Majesty's Government..."),
                                       (minister, "My Lords, the Government..."),
                                       (asker, "I thank the Minister."))):
        db.session.add(ingestion.HansardContribution(
            session_id=made["C-LEGACY"], member_id=m[0], member_name=m[1], speech_text=text,
            speech_order=order))
    db.session.commit()
    lords_items = [item(asker, "Asked by Lord Asker: To ask His Majesty's Government...", hrs="hs_TabledBy"),
                   item(minister, "My Lords, the Government..."),
                   item(asker, "I thank the Minister.")]
    return made, {"A-SHORT": session_json(speeches(9), ext_id="A-SHORT"),
                  "B-SAME": session_json(speeches(4), ext_id="B-SAME"),
                  "C-LEGACY": session_json(lords_items, ext_id="C-LEGACY")}


def _patch_http(script, monkeypatch, payloads):
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        ext = url.rsplit("/", 1)[-1].removesuffix(".json")
        got = payloads.get(ext, 404)
        return FakeResp(url, got) if isinstance(got, int) else FakeResp(url, payload=got)
    monkeypatch.setattr(script.requests, "get", fake_get)
    return calls


def test_backfill_dry_run_writes_nothing(app, script, monkeypatch, capsys):
    made, payloads = _seed_backfill()
    _patch_http(script, monkeypatch, payloads)
    before = {sid: [(r.id, r.speech_text) for r in _rows(sid)] for sid in made.values()}
    res = script.run(_args(script), sleep=lambda s: None, clock=lambda: SATURDAY_NOON)
    assert res["counts"]["refresh"] == 1 and res["counts"]["unchanged"] == 1
    assert res["counts"]["legacy_rows"] == 1
    assert res["undo_table"] is None and res["exit_code"] == 0
    assert {sid: [(r.id, r.speech_text) for r in _rows(sid)] for sid in made.values()} == before
    assert "dry run" in capsys.readouterr().out


def test_backfill_execute_refreshes_with_undo_table_and_resumes(app, script, monkeypatch):
    from extensions import db
    from sqlalchemy import text
    made, payloads = _seed_backfill()
    calls = _patch_http(script, monkeypatch, payloads)
    short_ids = [r.id for r in _rows(made["A-SHORT"])]

    res = script.run(_args(script, "--execute"), sleep=lambda s: None, clock=lambda: SATURDAY_NOON)
    assert res["counts"]["refresh"] == 1 and res["exit_code"] == 0
    rows = _rows(made["A-SHORT"])
    assert len(rows) == 9 and [r.id for r in rows[:2]] == short_ids
    undo = db.session.execute(text(f"SELECT action, COUNT(*) FROM {res['undo_table']} "
                                   f"GROUP BY action")).fetchall()
    assert dict(undo) == {"insert": 7}
    assert len(_rows(made["C-LEGACY"])) == 3, "legacy Lords rows left for the attribution repair"

    # Same command again: resumes after the last id, makes no calls, reuses the undo table.
    n_calls = len(calls)
    again = script.run(_args(script, "--execute"), sleep=lambda s: None, clock=lambda: SATURDAY_NOON)
    assert len(calls) == n_calls and again["undo_table"] == res["undo_table"]


def test_backfill_stops_on_429_and_in_cron_windows(app, script, monkeypatch):
    made, payloads = _seed_backfill()
    payloads["A-SHORT"] = 429
    _patch_http(script, monkeypatch, payloads)
    res = script.run(_args(script, "--execute"), sleep=lambda s: None, clock=lambda: SATURDAY_NOON)
    assert res["exit_code"] == 3 and "429" in res["stopped"]
    assert len(_rows(made["A-SHORT"])) == 2

    res = script.run(_args(script, "--execute"), sleep=lambda s: None, clock=lambda: WEDNESDAY_NOON)
    assert res["exit_code"] == 4


def test_backfill_needs_since_or_ext_id(script):
    assert script.main([]) == 2


# ---------------------------------------------------------------------------
# The cron reports refreshes in its log and in ha_cron_run
# ---------------------------------------------------------------------------

def test_archive_cron_counts_refreshes_and_retags(app, monkeypatch, capsys):
    from hansard_archive.models import HaCronRun
    spec = importlib.util.spec_from_file_location(
        "archive_cron", Path(__file__).parents[1] / "scripts" / "archive_cron.py")
    cron = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cron)

    from extensions import db

    seen = {}
    ranges = {"Commons": {"refreshed": 2, "refresh_errors": 0, "retag_ids": [11, 12]},
              "Lords": {"refreshed": 1, "refresh_errors": 0, "retag_ids": []}}

    def fake_range(start, end, house="Commons", verbose=True, refresh_existing=False):
        seen[house] = refresh_existing
        return {"total_days": 3, "sitting_days": 1, "total_sessions": 1, "errors": 0,
                "unchanged": 5, "held": 0, **ranges[house]}

    retag_calls = []
    pending_out = [[12]]          # run 1 leaves 12 over the cap; run 2 leaves nothing

    def fake_retag(ids, limit=30):
        retag_calls.append(list(ids))
        return {"retagged": 1, "kept_old": 0, "untagged": 0, "api_errors": 0, "errors": 0,
                "over_cap": 1, "aborted": "", "pending": pending_out.pop(0) if pending_out else []}

    monkeypatch.setattr(cron, "ingest_date_range", fake_range)
    monkeypatch.setattr(cron, "retag_refreshed", fake_retag)
    monkeypatch.setattr(cron, "tag_all_untagged", lambda limit, verbose: {"tagged": 0, "errors": 0})
    monkeypatch.setattr(cron.sys, "argv", ["archive_cron.py", "--service-name", "test-refresh"])
    try:
        cron.main()
        assert seen == {"Commons": True, "Lords": True}
        assert retag_calls == [[11, 12]]
        run = HaCronRun.query.filter_by(service_name="test-refresh").one()
        assert run.status == "ok" and run.sessions_ingested == 2
        assert run.notes.splitlines()[0] == "retag_pending: 12"
        assert "refresh: refreshed=3 held=0 retag: carried_in=0 retagged=1" in run.notes
        assert "refreshed=3" in capsys.readouterr().out

        # Next run: nothing new refreshed, the leftover is carried in and re-tagged.
        ranges["Commons"] = {"refreshed": 0, "refresh_errors": 0, "retag_ids": []}
        cron.main()
        assert retag_calls[1] == [12]
        latest = (HaCronRun.query.filter_by(service_name="test-refresh")
                  .order_by(HaCronRun.id.desc()).first())
        assert "carried_in=1" in latest.notes and "retag_pending" not in latest.notes

        # A refresh error makes the run fail (so the alert rules see it).
        ranges["Lords"] = {"refreshed": 0, "refresh_errors": 1, "retag_ids": []}
        with pytest.raises(SystemExit):
            cron.main()
    finally:
        HaCronRun.query.filter_by(service_name="test-refresh").delete()
        db.session.commit()


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------

def _texts_by_id(sid):
    return {r.id: (r.member_name, r.speech_text) for r in _rows(sid)}


# ---------------------------------------------------------------------------
# Row ids stay with their speech
# ---------------------------------------------------------------------------

def test_point_of_order_near_the_start_moves_no_ids(app, hansard):
    """Probe 1: 5 stored speeches, Hansard now 61 with a point of order inserted near the start."""
    hansard.sessions[EXT] = session_json(speeches(5))
    _ingest()
    sid = _session().id
    before = _texts_by_id(sid)
    opening_id = _rows(sid)[0].id
    grown = speeches(60)
    grown.insert(2, item((999, "Mr Speaker"), "Order. Point of order."))
    hansard.sessions[EXT] = session_json(grown)
    _, stats = _ingest()
    assert stats["refreshed"] == 1
    after = _texts_by_id(sid)
    assert len(after) == 61
    assert all(after[i] == before[i] for i in before), "no stored id holds a different speech"
    assert _rows(sid)[0].id == opening_id, "the PM's opening statement keeps its id"


def test_repeating_speakers_with_a_mid_debate_insert(app, hansard):
    """Alternating questioner / PM pattern, a new speech inserted mid-debate, then growth."""
    pattern = []
    for i in range(12):
        pattern.append(item(LOTO if i % 2 == 0 else LD, f"Question {i}."))
        pattern.append(item(PM, f"Answer {i}."))
    hansard.sessions[EXT] = session_json(pattern[:16])
    _ingest()
    sid = _session().id
    before = _texts_by_id(sid)
    grown = list(pattern)
    grown.insert(7, item(PM, "I should correct something I said earlier."))
    hansard.sessions[EXT] = session_json(grown)
    _ingest()
    after = _texts_by_id(sid)
    assert len(after) == 25
    assert all(after[i] == before[i] for i in before)


def test_dropped_speech_does_not_hand_its_id_to_the_same_speakers_next_speech():
    """Probe 2: stored [A a1, B b1, A a2], Hansard [A a1, C c1, A a2, B b2]."""
    a, b, c = PM, LOTO, LD
    stored = [_d(a, "a1", 0), _d(b, "b1", 1), _d(a, "a2", 2)]
    fresh = [_d(a, "a1", 0), _d(c, "c1", 1), _d(a, "a2", 2), _d(b, "b2", 3)]
    ops = rf.plan_ops(stored, fresh)
    paired = {i: e["speech_text"] for act, i, e in ops if act in ("keep", "update")}
    assert paired == {0: "a1", 2: "a2"}
    assert ("delete", 1, None) in ops
    assert sorted(e["speech_text"] for act, _, e in ops if act == "insert") == ["b2", "c1"]


def test_a_row_is_never_reused_for_a_different_speakers_words():
    """Probe 3: a row is reused across speakers only when the words are identical."""
    stored = [_d(PM, "p", 0), _d(LOTO, "Badenoch question", 1)]
    fresh = [_d(PM, "p", 0), _d(LD, "Davey question", 1)]
    ops = rf.plan_ops(stored, fresh)
    assert ("delete", 1, None) in ops
    assert [e["speech_text"] for act, _, e in ops if act == "insert"] == ["Davey question"]
    # Same words, corrected attribution: the row (and its link) is kept.
    fixed = [_d(PM, "p", 0), _d(LD, "Badenoch question", 1)]
    assert [(act, i) for act, i, _ in rf.plan_ops(stored, fixed)] == [("keep", 0), ("update", 1)]


def test_cut_off_speech_gets_its_full_text_in_place():
    stored = [_d(PM, "With permission, I will", 0)]
    fresh = [_d(PM, "With permission, I will make a statement.", 0), _d(LOTO, "q", 1)]
    assert [(act, i) for act, i, _ in rf.plan_ops(stored, fresh)] == [("update", 0), ("insert", None)]


# ---------------------------------------------------------------------------
# Locking, strict guard, errors, debate type
# ---------------------------------------------------------------------------

def test_writers_lock_the_session_and_read_rows_after_the_lock(app, hansard, monkeypatch):
    """Two refreshers of one session (cron + backfill) can't both add the growth."""
    from sqlalchemy.dialects import postgresql
    from extensions import db
    from hansard_archive.models import HansardSession
    hansard.sessions[EXT] = session_json(speeches(3))
    _ingest()
    s = _session()
    fresh = ingestion._flatten_items(session_json(speeches(40)), [0])

    order = []
    real_lock, real_rows = rf.lock_session, rf.stored_rows

    def spy_lock(sid):
        order.append("lock")
        real_lock(sid)

    def spy_rows(sid):
        order.append("read")
        return real_rows(sid)
    monkeypatch.setattr(rf, "lock_session", spy_lock)
    monkeypatch.setattr(rf, "stored_rows", spy_rows)
    rf.refresh_session(s, fresh)
    db.session.commit()
    assert order == ["lock", "read"]
    # The second writer reads after the first committed: nothing left to add.
    assert rf.refresh_session(s, fresh).state == rf.STATE_UNCHANGED
    db.session.rollback()
    assert len(_rows(s.id)) == 40
    # On Postgres the lock is a real SELECT ... FOR UPDATE.
    q = db.session.query(HansardSession.id).filter(HansardSession.id == 1).with_for_update()
    assert "FOR UPDATE" in str(q.statement.compile(dialect=postgresql.dialect()))


def test_cron_holds_a_net_loss_the_backfill_would_apply(app, hansard):
    hansard.sessions[EXT] = session_json(speeches(40))
    _ingest()
    sid = _session().id
    hansard.sessions[EXT] = session_json(speeches(34))       # 6 fewer: > 3 rows and > 10%
    _, stats = _ingest()
    assert stats["held"] == 1 and len(_rows(sid)) == 40
    stored = [rf._row_dict(r) for r in _rows(sid)]
    fresh = ingestion._flatten_items(session_json(speeches(34)), [0])
    assert rf.assess(stored, fresh, strict=False).state == rf.STATE_CHANGED
    hansard.sessions[EXT] = session_json(speeches(37))       # 3 fewer: applied
    _, stats = _ingest()
    assert stats["refreshed"] == 1 and len(_rows(sid)) == 37


def test_refresh_error_is_counted(app, hansard, monkeypatch):
    hansard.sessions[EXT] = session_json(speeches(2))
    _ingest()
    hansard.sessions[EXT] = session_json(speeches(9))

    def boom(*a, **k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(rf, "refresh_session", boom)
    _, stats = _ingest()
    assert stats["refresh_errors"] == 1 and stats["refreshed"] == 0


def test_session_stored_before_any_speech_gets_its_type_worked_out(app, hansard):
    from extensions import db
    hansard.sessions[EXT] = session_json([])
    _ingest()
    s = _session()
    s.hrs_tag, s.debate_type = "hs_2DebBill", "debate"   # the final record re-tags statements
    db.session.commit()
    hansard.sessions[EXT] = session_json(speeches(6))
    _, stats = _ingest()
    assert stats["refreshed"] == 1
    assert _session().debate_type == "ministerial_statement"
    # A session that already had speeches keeps its type.
    s = _session()
    s.debate_type = "debate"
    db.session.commit()
    hansard.sessions[EXT] = session_json(speeches(9))
    _ingest()
    assert _session().debate_type == "debate"


# ---------------------------------------------------------------------------
# Re-tagging
# ---------------------------------------------------------------------------

def _make_tagged(n):
    from extensions import db
    from hansard_archive.models import HansardSession, HansardSessionTheme
    ids = []
    for i in range(n):
        s = HansardSession(ext_id=f"P{i}", title=f"Debate {i}", date=DAY, house="Commons",
                           debate_type="debate", is_container=False, slug=f"debate-p{i}")
        db.session.add(s)
        db.session.flush()
        db.session.add(ingestion.HansardContribution(session_id=s.id, member_name="A (Lab)",
                                                     speech_text="Hares.", speech_order=0))
        db.session.add(HansardSessionTheme(session_id=s.id, theme="Old area",
                                           theme_type="policy_area"))
        ids.append(s.id)
    db.session.commit()
    return ids


def test_retag_survives_an_unexpected_error_and_reports_pending(app, monkeypatch):
    ids = _make_tagged(3)

    def fake_tag(si, api_key, model=None):
        if si.title == "Debate 0":
            raise RuntimeError("not a Gemini error")
        return tg.TagOutcome(policy_areas=["Environment"])
    monkeypatch.setattr(tg, "tag_session_input", fake_tag)
    stats = rf.retag_refreshed(ids, api_key="k", limit=2, verbose=False, sleep=lambda s: None)
    assert (stats["errors"], stats["retagged"], stats["over_cap"]) == (1, 1, 1)
    assert stats["pending"] == [ids[2]]


# ---------------------------------------------------------------------------
# Backfill script
# ---------------------------------------------------------------------------

def test_backfill_defaults_until_to_four_days_ago(script):
    args = _args(script)
    assert script.effective_until(args, date(2026, 10, 24)) == "2026-10-20"
    args.until = "2026-10-23"
    assert script.effective_until(args, date(2026, 10, 24)) == "2026-10-23"


def test_backfill_dry_run_respects_cron_windows(app, script, monkeypatch):
    _, payloads = _seed_backfill()
    calls = _patch_http(script, monkeypatch, payloads)
    res = script.run(_args(script), sleep=lambda s: None, clock=lambda: WEDNESDAY_NOON)
    assert res["exit_code"] == 4 and calls == []
    res = script.run(_args(script, "--ignore-cron-windows"), sleep=lambda s: None,
                     clock=lambda: WEDNESDAY_NOON)
    assert res["exit_code"] == 0


def test_backfill_404s_do_not_stop_the_run(app, script, monkeypatch):
    from extensions import db
    from hansard_archive.models import HansardSession
    for i in range(4):
        db.session.add(HansardSession(ext_id=f"GONE{i}", title="Gone", date=DAY, house="Commons",
                                      debate_type="debate", is_container=False, slug=f"gone-{i}"))
    db.session.commit()
    _patch_http(script, monkeypatch, {})
    res = script.run(_args(script), sleep=lambda s: None, clock=lambda: SATURDAY_NOON)
    assert res["exit_code"] == 0 and res["counts"]["not_found"] == 4


def _snapshot(sid):
    return [(r.id, r.member_id, r.member_name, r.party, r.speech_order, r.speech_text)
            for r in _rows(sid)]


def test_backfill_undo_sql_restores_everything(app, script, monkeypatch):
    from extensions import db
    from sqlalchemy import text
    from hansard_archive.models import HansardSession
    s = HansardSession(ext_id="U1", title="Undo", date=DAY, house="Commons", debate_type="debate",
                       is_container=False, slug="undo-u1", contributions_ingested=True)
    e = HansardSession(ext_id="U2", title="Was empty", date=DAY, house="Commons",
                       debate_type="debate", hrs_tag="hs_2DebBill", is_container=False,
                       slug="was-empty-u2", contributions_ingested=True)
    db.session.add_all([s, e])
    db.session.flush()
    stored = [item(PM, "a"), item(LOTO, "b"), item(LD, "dropped"), item(PM, "c"), item(LOTO, "d")]
    for c in ingestion._flatten_items({"Items": stored}, [0]):
        db.session.add(ingestion.HansardContribution(session_id=s.id, **c))
    db.session.commit()
    sid, eid = s.id, e.id
    before = _snapshot(sid)
    fresh = [item(PM, "a"), item(LD, "new1"), item(LOTO, "b changed"), item(PM, "c"),
             item(LOTO, "d"), item(PM, "e"), item(LD, "f")]
    _patch_http(script, monkeypatch, {"U1": session_json(fresh, ext_id="U1"),
                                      "U2": session_json(speeches(3), ext_id="U2")})
    res = script.run(_args(script, "--execute"), sleep=lambda s: None, clock=lambda: SATURDAY_NOON)
    assert res["counts"]["refresh"] == 2
    assert db.session.get(HansardSession, eid).debate_type == "ministerial_statement"
    assert _snapshot(sid) != before

    sql = script.UNDO_SQL.format(bak=res["undo_table"])
    body = "".join(line for line in sql.splitlines(keepends=True)
                   if not line.lstrip().startswith("--"))
    for statement in (st for st in body.split(";") if st.strip()):
        db.session.execute(text(statement))
    db.session.commit()
    db.session.expire_all()
    assert _snapshot(sid) == before
    assert _rows(eid) == []
    assert db.session.get(HansardSession, eid).debate_type == "debate"
