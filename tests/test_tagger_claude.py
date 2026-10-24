"""
Claude backend of the tagger (evaluation only): same prompts, schema fields,
evidence check and batching as Gemini; only the model call differs. Gemini
stays the default. No network: the Anthropic client is replaced by a fake.
"""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import hansard_archive.tagger as tg
from hansard_archive.tagger import PQInput, SessionInput

KEY = "sk-ant-test"
HAIKU = "claude-haiku-5-5"

_SI = SessionInput(
    title="Rail Fares",
    debate_type="westminster_hall",
    contributions=[("Jo Bloggs", "The cost of rail fares for commuters in the north has risen again "
                                 "and the Department for Transport must act on season tickets.")],
)

_TAGS = {
    "policy_areas": [{"area": "Transport", "evidence": "cost of rail fares for commuters"}],
    "topics": [{"topic": "rail fares", "evidence": "cost of rail fares for commuters"},
               {"topic": "season tickets", "evidence": "invented quote not in the text"}],
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(tg, "_detect_model", lambda key: "gemini-2.5-flash-lite")
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    monkeypatch.delenv("TAGGER_MODEL", raising=False)


def _response(text=None, stop_reason="end_turn", category=None, usage=(1000, 200)):
    content = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(type="refusal", category=category, explanation="") if category else None,
        content=content,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1],
                              cache_creation_input_tokens=None, cache_read_input_tokens=None),
    )


class _FakeClient:
    """Stands in for anthropic.Anthropic: each create() returns (or raises) the next item,
    or calls `reply(**kwargs)` when given a function."""

    def __init__(self, replies):
        self.replies = list(replies) if isinstance(replies, list) else replies
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.replies(**kwargs) if callable(self.replies) else self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def fake_client(monkeypatch):
    def install(replies):
        client = _FakeClient(replies)
        monkeypatch.setattr(tg, "_claude_client", lambda api_key: client)
        return client
    return install


def _no_gemini(*a, **k):
    raise AssertionError("Gemini must not be called for a Claude model")


# ── normal response ───────────────────────────────────────────────────────────

def test_claude_session_same_prompt_schema_and_evidence_check(fake_client, monkeypatch):
    monkeypatch.setattr(tg, "_gemini_json", _no_gemini)
    client = fake_client([_response(json.dumps(_TAGS))])
    out = tg.tag_session_input(_SI, KEY, HAIKU)

    assert out.policy_areas == ["Transport"] and out.topics == ["rail fares"]
    assert ("topic", "season tickets", "evidence not found in input") in out.dropped
    assert out.model == HAIKU and not out.empty_reason
    assert out.usage == {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 0}
    assert tg.usage_cost(out.usage, HAIKU) == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)

    (call,) = client.calls
    assert call["model"] == HAIKU
    assert call["messages"] == [{"role": "user", "content": tg.build_session_prompt(_SI)[0]}]
    assert call["output_config"]["effort"] == "low"
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["required"] == ["policy_areas", "topics"]
    area = fmt["schema"]["properties"]["policy_areas"]["items"]["properties"]["area"]
    assert area == {"type": "string", "enum": tg.POLICY_AREAS}
    # Rejected or unwanted on these models: sampling, thinking budgets, prefill.
    for banned in ("temperature", "top_p", "top_k", "thinking", "system"):
        assert banned not in call
    assert "propertyOrdering" not in json.dumps(fmt["schema"])


def test_claude_pq_batch_unwraps_array_and_matches_uins(fake_client):
    pqs = [PQInput(uin="1001", heading="Rail Fares", question_text="What steps is he taking on rail fares in the north?"),
           PQInput(uin="1002", heading="Bus Services", question_text="What support is there for rural bus services?")]

    def reply(**kw):
        assert kw["output_config"]["format"]["schema"]["properties"]["results"]["type"] == "array"
        uins = re.findall(r"UIN: (\S+)", kw["messages"][0]["content"])
        quotes = {"1001": "rail fares in the north", "1002": "rural bus services"}
        return _response(json.dumps({"results": [
            {"uin": u, "policy_areas": [{"area": "Transport", "evidence": quotes[u]}],
             "topics": [{"topic": quotes[u], "evidence": quotes[u]}]} for u in uins]}))

    client = fake_client(reply)
    outs = tg.tag_pq_inputs(pqs, KEY, HAIKU)
    assert len(client.calls) == 1                      # one batch call, no single retries
    assert [o.policy_areas for o in outs] == [["Transport"], ["Transport"]]
    assert outs[1].topics == ["rural bus services"]
    assert outs[0].usage["promptTokenCount"] == 500     # batch usage shared across items


# ── refusal, max_tokens, empty and schema-invalid responses ───────────────────

def test_claude_refusal_is_an_empty_outcome_not_an_api_failure(fake_client):
    client = fake_client([_response(None, stop_reason="refusal", category="cyber")])
    out = tg.tag_session_input(_SI, KEY, HAIKU)
    assert not out.ok and out.policy_areas == [] and len(client.calls) == 1
    assert "refusal" in out.empty_reason and "cyber" in out.empty_reason
    assert out.usage["promptTokenCount"] == 1000       # billed tokens still counted


def test_claude_max_tokens_is_an_empty_outcome(fake_client):
    fake_client([_response('{"policy_areas": [{"area": "Tran', stop_reason="max_tokens")])
    out = tg.tag_pq_input(PQInput(uin="1", question_text="What about rail fares?"), KEY, HAIKU)
    assert not out.ok and "max_tokens" in out.empty_reason


def test_claude_no_text_is_an_empty_outcome(fake_client):
    fake_client([_response(None)])
    out = tg.tag_session_input(_SI, KEY, HAIKU)
    assert not out.ok and "no text" in out.empty_reason


def test_claude_schema_invalid_json_verifies_nothing(fake_client):
    fake_client([_response(json.dumps({"tags": ["rail fares"]}))])
    out = tg.tag_session_input(_SI, KEY, HAIKU)
    assert not out.ok and out.proposed_areas == 0 and not out.empty_reason


def test_claude_unparseable_json_is_retried_then_fails(fake_client):
    client = fake_client([_response("not json")] * 3)
    with pytest.raises(tg.GeminiCallFailed) as exc:
        tg.tag_session_input(_SI, KEY, HAIKU)
    assert not exc.value.final and len(client.calls) == tg._MAX_RETRIES + 1


def test_claude_batch_that_is_not_a_list_retries_singly(fake_client):
    pq = PQInput(uin="1", heading="Rail Fares", question_text="What steps on rail fares in the north?")
    single = {"policy_areas": [{"area": "Transport", "evidence": "rail fares in the north"}], "topics": []}
    client = fake_client([_response(json.dumps({"oops": 1})), _response(json.dumps(single))])
    (out,) = tg.tag_pq_inputs([pq], KEY, HAIKU)
    assert out.ok and out.retried and len(client.calls) == 2


# ── API errors ────────────────────────────────────────────────────────────────

_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize("error, final", [
    (anthropic.BadRequestError("bad schema", response=httpx2.Response(400, request=_REQ), body=None), True),
    (anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=_REQ), body=None), True),
    (anthropic.NotFoundError("no model", response=httpx2.Response(404, request=_REQ), body=None), True),
    (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=_REQ), body=None), False),
    (anthropic.InternalServerError("oops", response=httpx2.Response(500, request=_REQ), body=None), False),
    (anthropic.APIConnectionError(request=_REQ), False),
])
def test_claude_api_errors_map_to_call_failed(fake_client, error, final):
    fake_client([error])
    with pytest.raises(tg.GeminiCallFailed) as exc:
        tg.tag_session_input(_SI, KEY, HAIKU)
    assert exc.value.final is final


@pytest.mark.parametrize("key", ["", None, "AIzaSyFakeGeminiKey"])
def test_claude_refuses_without_an_anthropic_key(monkeypatch, key):
    monkeypatch.setattr(tg, "_claude_client", _no_gemini)   # must not even build a client
    with pytest.raises(tg.GeminiCallFailed) as exc:
        tg._claude_json(key, HAIKU, "prompt", tg._RESPONSE_SCHEMA)
    assert exc.value.final and "Anthropic API key" in str(exc.value)


# ── Gemini stays the default ──────────────────────────────────────────────────

def _fake_gemini(calls):
    def fake(key, model, prompt, schema):
        calls.append(model)
        return _TAGS, {"promptTokenCount": 10, "candidatesTokenCount": 2}
    return fake


def test_gemini_is_the_default(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_gemini_json", _fake_gemini(calls))
    monkeypatch.setattr(tg, "_claude_json", _no_gemini)
    assert tg.tag_session_input(_SI, "gemini-key").model == "gemini-2.5-flash-lite"
    assert tg.tag_session_input(_SI, "gemini-key", "gemini-2.5-pro").ok
    assert calls == ["gemini-2.5-flash-lite", "gemini-2.5-pro"]


def test_claude_tagger_model_env_is_ignored(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_gemini_json", _fake_gemini(calls))
    monkeypatch.setattr(tg, "_claude_json", _no_gemini)
    monkeypatch.setenv("TAGGER_MODEL", HAIKU)
    assert tg.resolve_model("gemini-key") == "gemini-2.5-flash-lite"
    assert tg.tag_session_input(_SI, "gemini-key").model == "gemini-2.5-flash-lite"
    assert calls == ["gemini-2.5-flash-lite"]


def test_claude_prices():
    assert tg.model_prices("claude-haiku-5-5") == (0.10, 0.50)
    assert tg.model_prices("claude-sonnet-5-5") == (2.00, 10.00)


# ── eval script: keys per model ───────────────────────────────────────────────

_SCRIPT = Path(__file__).parents[1] / "scripts" / "eval_tagging.py"


@pytest.fixture
def ev():
    spec = importlib.util.spec_from_file_location("eval_tagging", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eval_needs_only_the_keys_for_the_models_asked_for(ev, monkeypatch, capsys, fake_client):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert ev.main(["--models", "gemini-2.5-flash-lite", HAIKU, "--kind", "pq", "--limit", "1"]) == 2
    out = capsys.readouterr().out
    assert f"ANTHROPIC_API_KEY is not set — needed for {HAIKU}" in out
    assert "GEMINI_API_KEY is not set" not in out

    # Claude only: no Gemini key needed; runs through the production path to a table.
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    fake_client(lambda **kw: _response(json.dumps({"results": [
        {"uin": u, "policy_areas": [], "topics": []}
        for u in re.findall(r"UIN: (\S+)", kw["messages"][0]["content"])]})))
    assert ev.main(["--models", HAIKU, "--kind", "pq", "--limit", "1"]) == 0
    assert HAIKU in capsys.readouterr().out
