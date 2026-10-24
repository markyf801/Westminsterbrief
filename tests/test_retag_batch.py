"""
scripts/retag.py --batch (Claude Message Batches API), with a fake Anthropic
client (no network): the dry run makes no API call unless --sample is given;
batches are submitted, polled and applied through the live verification and
write path, results matched by custom_id in any order; errored / expired /
changed items keep their old tags and are listed; a restart polls the batches
already submitted instead of paying again; PQs that don't line up get a single
retry round; cron windows hold the writes; --backup prints the undo SQL.
"""

import importlib.util
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import hansard_archive.tagger as tg

_SCRIPT = Path(__file__).parents[1] / "scripts" / "retag.py"
HAIKU = "claude-haiku-5-5"
BATCH = ["--batch", "--model", HAIKU, "--poll-seconds", "0"]
APPLY = [*BATCH, "--apply", "--no-backup", "--ignore-cron-windows"]
OLD = "Welfare and benefits"


@pytest.fixture(scope="module")
def retag():
    spec = importlib.util.spec_from_file_location("retag_batch_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _message(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason, stop_details=None, model=HAIKU,
        content=[SimpleNamespace(type="thinking", thinking="", signature="s"),
                 SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=300,
                              cache_creation_input_tokens=None, cache_read_input_tokens=None))


def good_answer(custom_id, params):
    """Tags supported by the seeded text, for whichever kind of request this is."""
    prompt = params["messages"][0]["content"]
    if custom_id.startswith("p"):
        return _message({"results": [
            {"uin": u, "policy_areas": [{"area": "Environment", "evidence": "farmland bird numbers"}],
             "topics": [{"topic": "farmland birds", "evidence": "farmland bird numbers"}]}
            for u in re.findall(r"UIN: (\S+)", prompt)]})
    if custom_id.startswith("q"):
        return _message({"policy_areas": [{"area": "Environment", "evidence": "farmland bird numbers"}],
                         "topics": [{"topic": "farmland birds", "evidence": "farmland bird numbers"}]})
    return _message({"policy_areas": [{"area": "Environment", "evidence": "a close season for brown hares"}],
                     "topics": [{"topic": "brown hares", "evidence": "close season for brown hares"}]})


class FakeBatches:
    """messages.batches: create / retrieve / results. A batch ends after `polls` retrieves;
    results come back in REVERSE order of the requests (any order is allowed)."""

    def __init__(self, answer=good_answer, polls=1):
        self.answer = answer
        self.polls = polls
        self.created: list[tuple[str, list[dict]]] = []
        self.retrieved: Counter = Counter()
        self.outcome: dict[str, str] = {}          # custom_id prefix match -> "errored" / "expired"

    def create(self, requests):
        bid = f"msgbatch_{len(self.created) + 1:03d}"
        self.created.append((bid, [dict(r) for r in requests]))
        return SimpleNamespace(id=bid, processing_status="in_progress")

    def retrieve(self, bid):
        self.retrieved[bid] += 1
        ended = self.retrieved[bid] >= self.polls
        n = len(dict(self.created)[bid])
        return SimpleNamespace(id=bid, processing_status="ended" if ended else "in_progress",
                               request_counts=SimpleNamespace(processing=0 if ended else n,
                                                              succeeded=n if ended else 0,
                                                              errored=0, canceled=0, expired=0))

    def results(self, bid):
        out = []
        for r in dict(self.created)[bid]:
            cid = r["custom_id"]
            kind = next((v for k, v in self.outcome.items() if cid.startswith(k)), "succeeded")
            if kind == "errored":
                result = SimpleNamespace(type="errored", error=SimpleNamespace(
                    type="error", error=SimpleNamespace(type="api_error", message="boom")))
            elif kind == "expired":
                result = SimpleNamespace(type="expired")
            else:
                result = SimpleNamespace(type="succeeded", message=self.answer(cid, r["params"]))
            out.append(SimpleNamespace(custom_id=cid, result=result))
        return list(reversed(out))

    def requests(self):
        return [r for _, reqs in self.created for r in reqs]


@pytest.fixture
def fake(monkeypatch):
    batches = FakeBatches()
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    built = []

    def make(api_key):
        built.append(api_key)
        return client
    monkeypatch.setattr(tg, "_claude_client", make)
    batches.built = built
    return batches


@pytest.fixture
def seeded(monkeypatch):
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import (HaCronRun, HansardContribution, HansardSession,
                                        HansardSessionTheme, HaPQ, HaPQTheme)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaGeminiKey")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-batch-test")
    monkeypatch.delenv("TAGGER_MODEL", raising=False)

    def no_gemini(*a, **k):
        raise AssertionError("no Gemini call in a --batch run")
    monkeypatch.setattr(tg, "_gemini_json", no_gemini)
    monkeypatch.setattr(tg, "_detect_model", no_gemini)

    def clear():
        for m in (HansardSessionTheme, HansardContribution, HansardSession, HaPQTheme, HaPQ):
            m.query.delete()
        HaCronRun.query.filter(HaCronRun.service_name.like("retag-%")).delete(synchronize_session=False)
        db.session.commit()

    with _fa.app.app_context():
        db.create_all()
        clear()
        sessions = [HansardSession(ext_id=f"B{i}", title=f"Hares {i}", date=date(2026, 10, 13),
                                   house="Commons", debate_type="debate", is_container=False,
                                   slug=f"hares-b{i}") for i in range(3)]
        db.session.add_all(sessions)
        db.session.flush()
        for s in sessions:
            db.session.add(HansardContribution(
                session_id=s.id, member_name="Ruth Jones (Lab)", speech_order=1,
                speech_text="I beg to move a Bill to establish a close season for brown hares."))
            db.session.add(HansardSessionTheme(session_id=s.id, theme=OLD, theme_type="policy_area"))
        for i in range(7):                       # two PQ requests: 5 + 2
            pq = HaPQ(uin=f"90{i}", question_text="what assessment has been made of farmland bird numbers.",
                      tabled_date=date(2026, 10, 13))
            db.session.add(pq)
            db.session.flush()
            db.session.add(HaPQTheme(pq_id=pq.id, theme=OLD, theme_type="policy_area"))
        db.session.commit()
        yield _fa.app
        clear()


def _tags(app):
    from hansard_archive.models import HansardSessionTheme, HaPQTheme
    with app.app_context():
        return (sorted((t.session_id, t.theme, t.model_used) for t in HansardSessionTheme.query.all()),
                sorted((t.pq_id, t.theme, t.model_used) for t in HaPQTheme.query.all()))


def _ids(app):
    from hansard_archive.models import HansardSession, HaPQ
    with app.app_context():
        return ([s.id for s in HansardSession.query.order_by(HansardSession.id)],
                [p.id for p in HaPQ.query.order_by(HaPQ.id)])


def _themes(rows, item_id=None):
    return {t for i, t, _ in rows if item_id is None or i == item_id}


# ── dry run ──────────────────────────────────────────────────────────────────

def test_dry_run_without_sample_makes_no_api_call(retag, seeded, fake, monkeypatch, capsys):
    before = _tags(seeded)
    assert retag.main(BATCH) == 0
    out = capsys.readouterr().out
    assert "estimated cost $" in out and "Batch API prices" in out and "no API call made" in out
    assert fake.built == [] and fake.created == []           # no client, no request
    assert _tags(seeded) == before


def test_sample_submits_only_the_sample_writes_nothing_and_is_not_paid_twice(retag, seeded, fake, capsys):
    before = _tags(seeded)
    assert retag.main([*BATCH, "--sample", "2"]) == 0
    out = capsys.readouterr().out
    assert "SAMPLE" in out and f"before: ['{OLD}']" in out and "after : ['Environment']" in out
    assert len(fake.requests()) == 2 + 1                    # 2 sessions + 1 PQ group of 2
    assert _tags(seeded) == before
    n = len(fake.created)
    assert retag.main([*BATCH, "--sample", "2"]) == 0      # same command again
    assert len(fake.created) == n                          # polled/reused, nothing resubmitted
    assert "after : ['Environment']" in capsys.readouterr().out


# ── apply ────────────────────────────────────────────────────────────────────

def test_apply_out_of_order_results_replace_tags_with_the_claude_model(retag, seeded, fake, capsys):
    assert retag.main(APPLY) == 0
    sessions, pqs = _tags(seeded)
    assert _themes(sessions) == {"Environment", "brown hares"} and len(sessions) == 6
    assert _themes(pqs) == {"Environment", "farmland birds"} and len(pqs) == 14
    assert {m for *_, m in sessions + pqs} == {HAIKU}                 # model_used = Claude model id
    cids = [r["custom_id"] for r in fake.requests()]
    assert sum(c.startswith("s") for c in cids) == 3 and sum(c.startswith("p") for c in cids) == 2
    assert all(len(c) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", c) for c in cids)
    params = fake.requests()[0]["params"]
    assert params["model"] == HAIKU and params["output_config"]["effort"] == "medium"
    assert "temperature" not in params and "thinking" not in params
    assert fake.built == ["sk-ant-batch-test"]


def test_errored_and_expired_items_keep_old_tags_and_are_listed(retag, seeded, fake, capsys):
    sids, _ = _ids(seeded)
    fake.outcome = {f"s{sids[0]}-": "errored", f"s{sids[1]}-": "expired"}
    assert retag.main([*APPLY, "--what", "sessions"]) == 0
    sessions, _ = _tags(seeded)
    assert _themes(sessions, sids[0]) == {OLD} and _themes(sessions, sids[1]) == {OLD}
    assert _themes(sessions, sids[2]) == {"Environment", "brown hares"}
    out = capsys.readouterr().out
    assert "errored/expired/changed (old tags kept)=2" in out
    assert f"--ids {sids[0]},{sids[1]} --batch" in out


def test_pq_group_that_fails_or_misaligns_is_retried_singly(retag, seeded, fake):
    _, pids = _ids(seeded)

    def answer(cid, params):
        if cid.startswith("p"):           # first group echoes a wrong UIN at position 1
            msg = good_answer(cid, params)
            data = json.loads(msg.content[1].text)
            data["results"][0]["uin"] = "nonsense"
            return _message(data)
        return good_answer(cid, params)
    fake.answer = answer
    fake.outcome = {f"p{pids[5]}_": "errored"}          # second group (2 PQs) errors
    assert retag.main([*APPLY, "--what", "pqs"]) == 0
    singles = sorted(int(r["custom_id"][1:].split("-")[0]) for r in fake.requests()
                     if r["custom_id"].startswith("q"))
    assert singles == [pids[0], pids[5], pids[6]]
    _, pqs = _tags(seeded)
    assert all(_themes(pqs, p) == {"Environment", "farmland birds"} for p in pids)


def test_a_single_retry_that_verifies_nothing_keeps_old_tags(retag, seeded, fake):
    _, pids = _ids(seeded)

    def answer(cid, params):
        if cid.startswith("q"):
            return _message({"policy_areas": [{"area": "Economy", "evidence": "not in the text at all"}],
                             "topics": []})
        msg = good_answer(cid, params)
        if cid.startswith("p"):
            data = json.loads(msg.content[1].text)
            data["results"][0]["uin"] = "nonsense"
            return _message(data)
        return msg
    fake.answer = answer
    assert retag.main([*APPLY, "--what", "pqs"]) == 0
    _, pqs = _tags(seeded)
    assert _themes(pqs, pids[0]) == {OLD}
    assert _themes(pqs, pids[1]) == {"Environment", "farmland birds"}


def test_text_changed_after_submission_keeps_old_tags(retag, seeded, fake, monkeypatch):
    sids, _ = _ids(seeded)
    real_results = fake.results

    def results_after_edit(bid):
        from extensions import db
        from hansard_archive.models import HansardContribution
        c = HansardContribution.query.filter_by(session_id=sids[0]).first()
        c.speech_text = "A completely different speech about bus routes."
        db.session.commit()
        return real_results(bid)
    monkeypatch.setattr(fake, "results", results_after_edit)
    assert retag.main([*APPLY, "--what", "sessions"]) == 0
    sessions, _ = _tags(seeded)
    assert _themes(sessions, sids[0]) == {OLD}
    assert _themes(sessions, sids[1]) == {"Environment", "brown hares"}


def test_refusal_keeps_old_tags(retag, seeded, fake):
    sids, _ = _ids(seeded)
    fake.answer = lambda cid, params: (_message({}, stop_reason="refusal")
                                       if cid.startswith(f"s{sids[0]}-") else good_answer(cid, params))
    assert retag.main([*APPLY, "--what", "sessions"]) == 0
    sessions, _ = _tags(seeded)
    assert _themes(sessions, sids[0]) == {OLD}


# ── resume ───────────────────────────────────────────────────────────────────

def test_restart_polls_existing_batches_instead_of_resubmitting(retag, seeded, fake, capsys):
    fake.polls = 3
    before = _tags(seeded)
    assert retag.main([*APPLY, "--max-wait-hours", "0"]) == 5          # still processing
    assert "still processing" in capsys.readouterr().out
    assert _tags(seeded) == before
    submitted = len(fake.created)
    assert submitted == 2                                               # sessions + pqs
    assert retag.main(APPLY) == 0                                       # same command later
    assert len(fake.created) == submitted                               # nothing paid twice
    assert "resuming 1 batch(es)" in capsys.readouterr().out
    sessions, pqs = _tags(seeded)
    assert _themes(sessions) == {"Environment", "brown hares"}
    assert _themes(pqs) == {"Environment", "farmland birds"}


def test_cron_window_holds_writes_and_resume_applies_without_resubmitting(retag, seeded, fake, monkeypatch):
    import hansard_archive.cron_windows as cw
    monkeypatch.setattr(cw, "blocked_window", lambda now: "test window")
    before = _tags(seeded)
    args = [*BATCH, "--apply", "--no-backup", "--what", "sessions"]
    assert retag.main(args) == 4
    assert _tags(seeded) == before and len(fake.created) == 1          # submitted, not written
    monkeypatch.setattr(cw, "blocked_window", lambda now: None)
    assert retag.main(args) == 0
    assert len(fake.created) == 1
    assert _themes(_tags(seeded)[0]) == {"Environment", "brown hares"}


def test_backup_is_taken_before_the_first_write_and_undo_sql_printed(retag, seeded, fake, capsys):
    from sqlalchemy import inspect
    from extensions import db
    assert retag.main([*BATCH, "--apply", "--backup", "--ignore-cron-windows", "--what", "sessions"]) == 0
    with seeded.app_context():
        bak = [n for n in inspect(db.engine).get_table_names() if n.startswith("ha_session_theme_bak_")]
    assert bak
    out = capsys.readouterr().out
    assert "BEGIN;" in out and f"FROM {sorted(bak)[-1]}" in out and f"model_used = '{HAIKU}'" in out


def test_undo_sql_restores_the_old_tags(retag, seeded, fake, capsys):
    """Run the printed undo SQL (SQLite) and check the old tags are back."""
    from sqlalchemy import text
    from extensions import db
    from hansard_archive.models import HansardSessionTheme
    with seeded.app_context():
        HansardSessionTheme.query.update({"confidence": 0.7})
        db.session.commit()
    assert retag.main([*BATCH, "--apply", "--backup", "--ignore-cron-windows", "--what", "sessions"]) == 0
    sql = capsys.readouterr().out.split("-- Undo re-tag")[1]
    sql = sql[sql.index("BEGIN;") + len("BEGIN;"): sql.index("COMMIT;")]
    with seeded.app_context():
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                db.session.execute(text(stmt))
        db.session.commit()
        db.session.execute(text("DROP TABLE IF EXISTS retag_undo_sessions"))
        assert {t.confidence for t in HansardSessionTheme.query.all()} == {0.7}
    sessions, _ = _tags(seeded)
    assert _themes(sessions) == {OLD} and len(sessions) == 3


def test_undo_sql_temp_tables_differ_per_kind(retag):
    s = retag.undo_sql("sessions", "ha_session_theme_bak_x", HAIKU, "2026-10-24 23:30:00")
    p = retag.undo_sql("pqs", "ha_pq_theme_bak_x", HAIKU, "2026-10-24 23:30:00")
    assert "retag_undo_sessions" in s and "retag_undo_pqs" in p and "retag_undo_pqs" not in s
    assert "confidence" in s and "confidence" not in p        # ha_pq_theme has no such column


# ── items gone, duplicates, lock, custom_id ──────────────────────────────────

def test_items_deleted_after_submission_keep_nothing_written_and_others_apply(retag, seeded, fake,
                                                                               monkeypatch):
    sids, pids = _ids(seeded)
    real_results = fake.results

    def results_after_delete(bid):
        from extensions import db
        from hansard_archive.models import (HansardContribution, HansardSession, HansardSessionTheme,
                                            HaPQ, HaPQTheme)
        HansardSessionTheme.query.filter_by(session_id=sids[0]).delete()
        HansardContribution.query.filter_by(session_id=sids[0]).delete()
        HansardSession.query.filter_by(id=sids[0]).delete()
        HaPQTheme.query.filter_by(pq_id=pids[1]).delete()
        HaPQ.query.filter_by(id=pids[1]).delete()
        db.session.commit()
        monkeypatch.setattr(fake, "results", real_results)
        return real_results(bid)
    monkeypatch.setattr(fake, "results", results_after_delete)
    assert retag.main(APPLY) == 0
    sessions, pqs = _tags(seeded)
    assert sids[0] not in {i for i, *_ in sessions}           # nothing written for a deleted session
    assert all(_themes(sessions, s) == {"Environment", "brown hares"} for s in sids[1:])
    assert pids[1] not in {i for i, *_ in pqs}
    # the rest of the deleted PQ's group was asked again on its own, then written
    singles = sorted(int(r["custom_id"][1:].split("-")[0]) for r in fake.requests()
                     if r["custom_id"].startswith("q"))
    assert singles == [pids[0], pids[2], pids[3], pids[4]]
    assert all(_themes(pqs, p) == {"Environment", "farmland birds"} for p in pids if p != pids[1])


def test_duplicate_tags_in_one_answer_are_written_once(retag, seeded, fake):
    dup = {"area": "Environment", "evidence": "a close season for brown hares"}
    top = {"topic": "brown hares", "evidence": "close season for brown hares"}
    fake.answer = lambda cid, params: _message({"policy_areas": [dup, dup], "topics": [top, top, top]})
    assert retag.main([*APPLY, "--what", "sessions"]) == 0
    sessions, _ = _tags(seeded)
    assert len(sessions) == 6 and len(set(sessions)) == 6     # one row per (session, theme)


def test_batch_session_writes_take_the_live_taggers_lock(retag, seeded, fake, monkeypatch):
    sids, _ = _ids(seeded)
    locked = []
    monkeypatch.setattr(tg, "_lock_session_tags", lambda sid: locked.append(sid))
    assert retag.main([*APPLY, "--what", "sessions"]) == 0
    assert sorted(locked) == sids


def test_custom_id_over_the_api_limit_is_refused(retag):
    assert retag._custom_id("p", [123456789] * 5, "abcdef12")       # 59 chars: fine
    with pytest.raises(ValueError):
        retag._custom_id("p", [1234567890123] * 5, "abcdef12")


# ── guards ───────────────────────────────────────────────────────────────────

def test_batch_needs_a_claude_model(retag, seeded, fake, capsys):
    assert retag.main(["--batch"]) == 2
    assert "claude-haiku-5-5" in capsys.readouterr().out
    assert fake.built == []


def test_unapproved_claude_model_cannot_apply(retag, seeded, fake):
    assert retag.main(["--batch", "--model", "claude-sonnet-5-5", "--apply", "--no-backup"]) == 2
    assert fake.created == []


def test_missing_anthropic_key(retag, seeded, fake, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert retag.main([*BATCH, "--sample", "1"]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out
    assert fake.created == []


def test_submit_failure_stops_with_what_was_submitted_saved(retag, seeded, fake, monkeypatch):
    import anthropic
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    real_create = fake.create
    calls = []

    def create(requests):
        calls.append(1)
        if len(calls) > 1:                      # sessions submitted, PQs refused (queue full)
            raise anthropic.RateLimitError("queue full", response=httpx2.Response(429, request=req), body=None)
        return real_create(requests)
    monkeypatch.setattr(fake, "create", create)
    assert retag.main([*APPLY, "--max-failures", "2"]) == 3
    monkeypatch.setattr(fake, "create", real_create)
    assert retag.main(APPLY) == 0
    cids = [r["custom_id"][0] for r in fake.requests()]
    assert cids.count("s") == 3                 # sessions were not submitted twice


def test_submit_without_an_answer_is_not_retried(retag, seeded, fake, monkeypatch, capsys):
    """A timed-out submit may have created the batch: stop, don't send it again (no double pay)."""
    import anthropic
    import httpx2
    calls = []

    def create(requests):
        calls.append(1)
        raise anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/x"))
    monkeypatch.setattr(fake, "create", create)
    assert retag.main([*APPLY, "--what", "sessions"]) == 3
    assert calls == [1]
    assert "Console > Batches" in capsys.readouterr().out
