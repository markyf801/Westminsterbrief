"""
Live tagging model selection (owner decision 24 Oct 2026): TAGGER_MODEL=
claude-haiku-5-5 makes every tagging entry point use Claude with
ANTHROPIC_API_KEY; unset keeps Gemini. No silent fallback: when Claude is
selected and its key is missing / rejected, or the API fails, the run stops
visibly (aborted) and items stay untagged — Gemini is never called instead.
A Gemini key never reaches Anthropic. No network: Gemini and the Anthropic
client are fakes.
"""

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import hansard_archive.tagger as tg

HAIKU = "claude-haiku-5-5"
ANTHROPIC_KEY = "sk-ant-live-test"
GEMINI_KEY = "AIzaFakeGeminiKey"

_SESSION_TAGS = {"policy_areas": [{"area": "Environment", "evidence": "a close season for brown hares"}],
                 "topics": [{"topic": "brown hares", "evidence": "close season for brown hares"}]}
_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _message(payload):
    return SimpleNamespace(
        stop_reason="end_turn", stop_details=None,
        content=[SimpleNamespace(type="thinking", thinking="", signature="s"),
                 SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                              cache_creation_input_tokens=None, cache_read_input_tokens=None))


def _answer(**kw):
    """A plausible answer for whichever tagging prompt was sent."""
    import re
    prompt = kw["messages"][0]["content"]
    if "results" in kw["output_config"]["format"]["schema"]["properties"]:
        return _message({"results": [
            {"uin": u, "policy_areas": [{"area": "Environment", "evidence": "farmland bird numbers"}],
             "topics": [{"topic": "farmland birds", "evidence": "farmland bird numbers"}]}
            for u in re.findall(r"UIN: (\S+)", prompt)]})
    return _message(_SESSION_TAGS)


class _Client:
    def __init__(self, reply=_answer):
        self.reply = reply
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.reply(**kw)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def claude(monkeypatch):
    """TAGGER_MODEL=claude-haiku-5-5, both keys set, Gemini forbidden; returns the fake client
    and the keys it was built with."""
    monkeypatch.setenv("TAGGER_MODEL", HAIKU)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    def no_gemini(*a, **k):
        raise AssertionError("Gemini must never be called when Claude is selected")
    monkeypatch.setattr(tg, "_gemini_json", no_gemini)
    monkeypatch.setattr(tg, "_detect_model", no_gemini)
    client = _Client()
    keys = []

    def make(api_key):
        keys.append(api_key)
        return client
    monkeypatch.setattr(tg, "_claude_client", make)
    client.keys = keys
    return client


@pytest.fixture
def db_app(monkeypatch):
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import (HansardContribution, HansardSession, HansardSessionTheme,
                                        HaPQ, HaPQTheme, HaTagAttempt)

    def clear():
        for m in (HansardSessionTheme, HansardContribution, HansardSession, HaPQTheme, HaPQ, HaTagAttempt):
            m.query.delete()
        db.session.commit()

    with _fa.app.app_context():
        db.create_all()
        clear()
        for i in range(3):
            s = HansardSession(ext_id=f"L{i}", title=f"Hares {i}", date=date(2026, 10, 13 + i),
                               house="Commons", debate_type="debate", is_container=False, slug=f"hares-l{i}")
            db.session.add(s)
            db.session.flush()
            db.session.add(HansardContribution(
                session_id=s.id, member_name="Ruth Jones (Lab)", speech_order=1,
                speech_text="I beg to move a Bill to establish a close season for brown hares."))
        for i in range(2):
            db.session.add(HaPQ(uin=f"90{i}", tabled_date=date(2026, 10, 13),
                                question_text="what assessment has been made of farmland bird numbers."))
        db.session.commit()
        yield _fa.app
        clear()


def _session_tags():
    from hansard_archive.models import HansardSessionTheme
    return sorted((t.session_id, t.theme, t.model_used) for t in HansardSessionTheme.query.all())


def _pq_tags():
    from hansard_archive.models import HaPQTheme
    return sorted((t.pq_id, t.theme, t.model_used) for t in HaPQTheme.query.all())


# ── selection ────────────────────────────────────────────────────────────────

def test_unset_tagger_model_stays_gemini(monkeypatch):
    monkeypatch.delenv("TAGGER_MODEL", raising=False)
    monkeypatch.setattr(tg, "_detect_model", lambda key: "gemini-2.5-flash-lite")
    assert tg.resolve_backend(GEMINI_KEY) == ("gemini-2.5-flash-lite", GEMINI_KEY)


def test_tagger_model_selects_claude_with_the_anthropic_key(claude):
    model, key = tg.resolve_backend(GEMINI_KEY)            # callers pass the Gemini key by habit
    assert (model, key) == (HAIKU, ANTHROPIC_KEY)
    si = tg.SessionInput(title="Hares", contributions=[
        ("Ruth Jones", "I beg to move a Bill to establish a close season for brown hares.")])
    out = tg.tag_session_input(si, GEMINI_KEY)
    assert out.ok and out.model == HAIKU
    assert claude.keys == [ANTHROPIC_KEY] and claude.calls[0]["model"] == HAIKU


def test_keys_never_cross_providers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    assert tg.key_for_model(HAIKU, GEMINI_KEY) == ANTHROPIC_KEY
    assert tg.key_for_model("gemini-2.5-flash-lite", ANTHROPIC_KEY) == GEMINI_KEY
    assert tg.resolve_model(ANTHROPIC_KEY) in (tg.DEFAULT_MODEL, HAIKU)    # no Google call with it
    monkeypatch.setenv("ANTHROPIC_API_KEY", GEMINI_KEY)                   # pasted the wrong key
    with pytest.raises(tg.GeminiCallFailed) as exc:
        tg._model_json(None, HAIKU, "prompt", tg._RESPONSE_SCHEMA)
    assert exc.value.final


def test_unapproved_claude_tagger_model_stops_not_falls_back(claude, monkeypatch, db_app):
    monkeypatch.setenv("TAGGER_MODEL", "claude-sonnet-5-5")
    res = tg.tag_all_untagged(verbose=False)
    assert res["tagged"] == 0 and res["errors"] == 1 and "not approved" in res["aborted"]
    assert claude.calls == [] and _session_tags() == []


# ── live entry points record the Claude model ────────────────────────────────

def test_archive_tagging_uses_claude_and_records_model(claude, db_app):
    res = tg.tag_all_untagged(verbose=False)
    assert res["tagged"] == 3 and not res["aborted"] and res["model"] == HAIKU
    assert {m for _, _, m in _session_tags()} == {HAIKU}
    assert claude.keys and set(claude.keys) == {ANTHROPIC_KEY}


def test_pq_tagging_uses_claude_and_records_model(claude, db_app):
    res = tg.tag_pq_all_untagged(verbose=False)
    assert res["tagged"] == 2 and res["model"] == HAIKU
    assert {m for _, _, m in _pq_tags()} == {HAIKU}
    assert len(claude.calls) == 1                     # one batch of 2, same batching as Gemini


# ── no silent fallback ───────────────────────────────────────────────────────

def test_missing_anthropic_key_skips_the_run_visibly(claude, monkeypatch, db_app, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    for res in (tg.tag_all_untagged(), tg.tag_pq_all_untagged()):
        assert res["tagged"] == 0 and res["errors"] == 1
        assert "ANTHROPIC_API_KEY" in res["aborted"] and "no fallback to Gemini" in res["aborted"]
    assert "STOPPED" in capsys.readouterr().out
    assert claude.calls == [] and claude.keys == []
    assert _session_tags() == [] and _pq_tags() == []          # left for the next run
    with pytest.raises(tg.GeminiCallFailed) as exc:           # single-item entry points too
        tg.tag_session(1)
    assert exc.value.final


@pytest.mark.parametrize("status, cls", [(401, anthropic.AuthenticationError),
                                         (403, anthropic.PermissionDeniedError)])
def test_rejected_key_stops_at_once(claude, db_app, status, cls):
    claude.reply = lambda **kw: cls("no", response=httpx2.Response(status, request=_REQ), body=None)
    res = tg.tag_all_untagged(verbose=False)
    assert res["tagged"] == 0 and len(claude.calls) == 1
    assert f"HTTP {status}" in res["aborted"] and "ANTHROPIC_API_KEY" in res["aborted"]
    res = tg.tag_pq_all_untagged(verbose=False)
    assert res["tagged"] == 0 and f"HTTP {status}" in res["aborted"]
    assert _session_tags() == [] and _pq_tags() == []


def test_claude_outage_stops_after_consecutive_failures(claude, db_app, monkeypatch):
    monkeypatch.setattr(tg, "MAX_CONSECUTIVE_API_FAILURES", 2)
    claude.reply = lambda **kw: anthropic.InternalServerError(
        "overloaded", response=httpx2.Response(529, request=_REQ), body=None)
    res = tg.tag_all_untagged(verbose=False)
    assert res["api_errors"] == 2 and "consecutive" in res["aborted"]
    assert _session_tags() == []


def test_refresh_retag_uses_the_same_model_and_never_falls_back(claude, monkeypatch, db_app):
    from hansard_archive import refresh as rf
    from hansard_archive.models import HansardSession, HansardSessionTheme
    from extensions import db
    sid = HansardSession.query.first().id
    db.session.add(HansardSessionTheme(session_id=sid, theme="Welfare and benefits", theme_type="policy_area"))
    db.session.commit()
    stats = rf.retag_refreshed([sid], verbose=False, sleep=lambda s: None)
    assert stats["retagged"] == 1 and {m for _, _, m in _session_tags()} == {HAIKU}

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    stats = rf.retag_refreshed([sid], verbose=False, sleep=lambda s: None)
    assert "ANTHROPIC_API_KEY" in stats["aborted"] and stats["pending"] == [sid]


# ── backfills: same key resolution and an outage probe that tests Claude ────

def _backfill():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import backfill_parliament_start
    return backfill_parliament_start


def test_backfill_probe_and_key_follow_tagger_model(claude, monkeypatch, db_app, capsys):
    bps = _backfill()
    assert bps.check_tagger_api() is None
    assert bps.gemini_healthy(ANTHROPIC_KEY) is True
    assert claude.calls and claude.calls[-1]["model"] == HAIKU          # probed Claude, not Gemini
    claude.reply = lambda **kw: anthropic.InternalServerError(
        "down", response=httpx2.Response(500, request=_REQ), body=None)
    assert bps.gemini_healthy(ANTHROPIC_KEY) is False

    assert bps.tagging_key("backfill") == (ANTHROPIC_KEY, False)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert bps.tagging_key("backfill") == ("", True)
    assert "ANTHROPIC_API_KEY not set (tagging model claude-haiku-5-5)" in capsys.readouterr().out
