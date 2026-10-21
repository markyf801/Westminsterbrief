"""
Tagger accuracy rules: verbatim evidence per tag, at most 3 policy areas in
centrality order, per-question-group tagging for multi-question sessions, no
example values in prompts, PQ batches of 5 with 600 characters of question.
No network: _gemini_json is replaced in every test that would call Gemini.
"""

from datetime import date

import pytest

import hansard_archive.tagger as tg
from hansard_archive.policy_areas import HANSARD_POLICY_NAMES, POLICY_AREA_SCOPE
from hansard_archive.tagger import (
    PQInput,
    SessionInput,
    TagOutcome,
    aggregate_groups,
    build_grouped_prompt,
    build_pq_batch_prompt,
    build_pq_prompt,
    build_session_prompt,
    evidence_in_source,
    is_multi_question,
    normalise_for_evidence,
    question_groups,
    verify_tags,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(tg, "_detect_model", lambda key: "test-model")
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)


# ── evidence matching ─────────────────────────────────────────────────────────

SRC = normalise_for_evidence(
    "To ask the Secretary of State for Education, what steps she is taking to reduce "
    "delays in Education, Health and Care Plans for children with SEND in Women’s refuges."
)


@pytest.mark.parametrize("quote, ok", [
    ("reduce delays in Education, Health and Care Plans", True),
    ("REDUCE DELAYS in education health and care plans", True),       # case + punctuation
    ("children with SEND in Women's refuges", True),                  # straight vs curly apostrophe
    ("what steps she is taking ... children with SEND", True),        # ellipsis: every part present
    ("what steps she is taking … for pupils with SEND", False),       # second part not there
    ("reduce waiting lists for care plans", False),                   # paraphrase
    ("SEND", False),                                                  # one word proves nothing
    ("", False),
    (None, False),
    ("ducation health", False),                                       # word boundaries
])
def test_evidence_in_source(quote, ok):
    assert evidence_in_source(quote, SRC) is ok


def test_verify_tags_drops_unsupported_invalid_and_extra():
    source = ("Rail fares in the north. The Minister spoke about Northern Rail ticket prices, "
              "bus franchising in Greater Manchester and the cost of living for commuters.")
    raw = {
        "policy_areas": [
            {"area": "Transport", "evidence": "Northern Rail ticket prices"},
            {"area": "Transport", "evidence": "bus franchising in Greater Manchester"},   # duplicate
            {"area": "Economy", "evidence": "the cost of living for commuters"},
            {"area": "Housing and planning", "evidence": "new homes in Salford"},       # not in text
            {"area": "Made-up area", "evidence": "rail fares in the north"},            # not in list
            {"area": "Local government", "evidence": "in Greater Manchester"},
            {"area": "Employment and labour market", "evidence": "for commuters"},      # 4th valid
        ],
        "topics": [
            {"topic": "Northern Rail fares", "evidence": "Northern Rail ticket prices"},
            {"topic": "bus franchising", "evidence": "bus franchising in Greater Manchester"},
            {"topic": "transport", "evidence": "rail fares in the north"},              # generic
            {"topic": "rail electrification", "evidence": "electrify the line"},        # invented
        ],
    }
    out = verify_tags(raw, source)
    assert out.policy_areas == ["Transport", "Economy", "Local government"]   # order kept, max 3
    assert out.topics == ["northern rail fares", "bus franchising"]
    reasons = {(k, v): r for k, v, r in out.dropped}
    assert reasons[("policy_area", "Housing and planning")] == "evidence not found in input"
    assert reasons[("policy_area", "Made-up area")] == "not in the policy area list"
    assert reasons[("policy_area", "Employment and labour market")] == "over the limit of 3"
    assert reasons[("topic", "rail electrification")] == "evidence not found in input"
    assert ("topic", "None", "generic, too long or empty") not in out.dropped
    assert out.proposed_areas == 7 and out.proposed_topics == 4


def test_verify_tags_canonicalises_topics():
    out = verify_tags({"policy_areas": [], "topics": [
        {"topic": "SEND provision", "evidence": "children with SEND"}]}, SRC)
    assert out.topics == ["special educational needs"]


def test_verify_tags_tolerates_garbage():
    assert verify_tags(None, "x").policy_areas == []
    out = verify_tags({"policy_areas": ["Transport"], "topics": ["a topic"]}, "x")
    assert not out.ok


# ── prompts ───────────────────────────────────────────────────────────────────

_SI = SessionInput(title="Chagos Islands", debate_type="debate", date="2026-10-14", house="Commons",
                   contributions=[("Alice Smith (Lab)", "word " * 5000)])


def _all_prompts():
    pq = PQInput(uin="1", heading="Schools: Finance", question_text="What funding? " * 10)
    multi = SessionInput(title="Engagements", debate_type="pmqs", contributions=[
        ("Bob (Con)", "Question one"), ("The Prime Minister (Keir Starmer)", "Answer one"),
        ("Carol (LD)", "Question two"), ("The Prime Minister (Keir Starmer)", "Answer two")])
    return [build_session_prompt(_SI)[0], build_pq_prompt(pq)[0],
            build_pq_batch_prompt([pq])[0], build_grouped_prompt(multi)[0]]


def test_prompts_have_no_example_values_and_carry_scope_notes():
    for prompt in _all_prompts():
        for copied in ("student loan repayments", "nhs waiting times", "chalk stream pollution",
                       "asylum seeker housing", "e.g."):
            assert copied not in prompt.lower()
        for name in HANSARD_POLICY_NAMES:
            assert f"- {name}: {POLICY_AREA_SCOPE[name]}" in prompt
        assert "1 to 3" in prompt and "exactly as it appears" in prompt


def test_session_window_is_larger_and_source_matches_prompt():
    prompt, source = build_session_prompt(_SI)
    excerpt = source.split("\n", 1)[1]
    assert 11000 <= len(excerpt) <= 12000            # was 4,000 characters
    assert excerpt in prompt


def test_pq_batch_prompt_uses_600_chars_of_question_plus_heading():
    pqs = [PQInput(uin=str(i), heading=f"Heading {i}", question_text="x" * 590 + f" END{i} " + "y" * 500)
           for i in range(5)]
    prompt, sources = build_pq_batch_prompt(pqs)
    assert len(sources) == 5 and tg._PQ_BATCH_SIZE == 5
    for i, src in enumerate(sources):
        assert src.startswith(f"Heading {i}\n") and f"END{i}" in src
        assert len(src.split("\n", 1)[1]) == 600
    assert "Return exactly 5 entries in the order given" in prompt


# ── multi-question sessions ───────────────────────────────────────────────────

PMQS = [
    ("Mr Speaker", "Order."),
    ("Bob Jones (Bristol North) (Lab)", "Q1. If he will list his official engagements for Wednesday."),
    ("The Prime Minister (Keir Starmer)", "This morning I had meetings with ministerial colleagues."),
    ("Bob Jones (Bristol North) (Lab)", "Will the Prime Minister act on hospice funding in Bristol?"),
    ("The Prime Minister (Keir Starmer)", "We are putting extra money into hospice funding."),
    ("Mr Speaker", "I call the Leader of the Opposition."),
    ("Kemi Badenoch (North West Essex) (Con)", "Why have small boat crossings risen this summer?"),
    ("The Prime Minister (Keir Starmer)", "Small boat crossings are down on the year."),
    ("Kemi Badenoch (North West Essex) (Con)", "They are not."),
    ("The Prime Minister (Keir Starmer)", "They are."),
    ("Ed Davey (Kingston and Surbiton) (LD)", "Will he fix the crisis in adult social care waiting lists?"),
    ("The Prime Minister (Keir Starmer)", "Adult social care waiting lists are a priority."),
]


def test_question_groups_split_on_each_new_question():
    groups = question_groups(PMQS)
    assert [g[0][0].split(" (")[0] for g in groups] == ["Bob Jones", "Kemi Badenoch", "Ed Davey"]
    assert len(groups[1]) == 4                 # follow-ups stay with their question
    assert groups[0][-1][0] == "Mr Speaker"    # chair interventions join the current group


def test_question_groups_without_role_names_use_most_frequent_speaker():
    contribs = [("Ann (Lab)", "Q a"), ("Min", "A a"), ("Ben (Con)", "Q b"), ("Min", "A b"),
                ("Min", "more"), ("Cat (LD)", "Q c"), ("Min", "A c")]
    assert len(question_groups(contribs)) == 3


@pytest.mark.parametrize("title, dtype, multi", [
    ("Engagements", "pmqs", True),
    ("Topical Questions", "oral_questions", True),
    ("Business of the House", "other", True),
    ("Hospice Funding", "oral_questions", False),
    ("Chagos Islands", "debate", False),
])
def test_is_multi_question(title, dtype, multi):
    assert is_multi_question(SessionInput(title=title, debate_type=dtype)) is multi


def test_aggregate_groups_ranks_by_number_of_groups():
    g = [TagOutcome(policy_areas=["Health and social care"], topics=["hospice funding"]),
         TagOutcome(policy_areas=["Immigration and borders"], topics=["small boat crossings"]),
         TagOutcome(policy_areas=["Health and social care"], topics=["adult social care waiting lists"]),
         TagOutcome(policy_areas=["Economy"], topics=[]),
         TagOutcome(policy_areas=["Defence and armed forces"], topics=[])]
    out = aggregate_groups(g)
    assert out.policy_areas == ["Health and social care", "Immigration and borders", "Economy"]
    assert out.topics[0] == "hospice funding"


def test_grouped_session_verifies_each_group_against_its_own_text(monkeypatch):
    calls = []

    def fake(key, model, prompt, schema):
        calls.append(schema)
        return {"groups": [
            {"group": 1, "policy_areas": [{"area": "Health and social care",
                                           "evidence": "hospice funding in Bristol"}],
             "topics": [{"topic": "hospice funding", "evidence": "extra money into hospice funding"}]},
            # group 2 claims a quote from group 3: dropped
            {"group": 2, "policy_areas": [{"area": "Health and social care",
                                           "evidence": "adult social care waiting lists"}],
             "topics": [{"topic": "small boat crossings", "evidence": "small boat crossings risen"}]},
            {"group": 3, "policy_areas": [{"area": "Health and social care",
                                           "evidence": "adult social care waiting lists"}],
             "topics": [{"topic": "adult social care", "evidence": "adult social care waiting lists"}]},
            {"group": 9, "policy_areas": [{"area": "Economy", "evidence": "This morning I had"}],
             "topics": []},
        ]}, {"promptTokenCount": 100, "candidatesTokenCount": 20}

    monkeypatch.setattr(tg, "_gemini_json", fake)
    si = SessionInput(title="Engagements", debate_type="pmqs", contributions=PMQS)
    out = tg.tag_session_input(si, "key")
    assert calls == [tg._GROUPED_RESPONSE_SCHEMA]
    assert out.policy_areas == ["Health and social care"]
    assert out.topics == ["hospice funding", "small boat crossings", "adult social care"]
    assert ("policy_area", "Health and social care", "evidence not found in input") in out.dropped
    assert out.usage["promptTokenCount"] == 100 and out.model == "test-model"


def test_single_subject_session_uses_one_call(monkeypatch):
    monkeypatch.setattr(tg, "_gemini_json", lambda key, model, prompt, schema: (
        {"policy_areas": [{"area": "Foreign affairs and diplomacy", "evidence": "word word word"}],
         "topics": []}, {}))
    out = tg.tag_session_input(_SI, "key", model="gemini-2.5-pro")
    assert out.policy_areas == ["Foreign affairs and diplomacy"] and out.model == "gemini-2.5-pro"


def test_model_override_env(monkeypatch):
    monkeypatch.setenv("TAGGER_MODEL", "gemini-2.5-flash")
    assert tg.resolve_model("key") == "gemini-2.5-flash"
    assert tg.resolve_model("key", "gemini-2.5-pro") == "gemini-2.5-pro"
    monkeypatch.delenv("TAGGER_MODEL")
    assert tg.resolve_model("key") == "test-model"


# ── writing (in-memory DB) ────────────────────────────────────────────────────

@pytest.fixture
def app_db():
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import HansardContribution, HansardSession, HansardSessionTheme
    with _fa.app.app_context():
        db.create_all()
        HansardSessionTheme.query.delete()
        HansardContribution.query.delete()
        HansardSession.query.delete()
        db.session.commit()
        yield db
        HansardSessionTheme.query.delete()
        HansardContribution.query.delete()
        HansardSession.query.delete()
        db.session.commit()


def test_tag_session_writes_verified_tags_in_order(app_db, monkeypatch):
    from hansard_archive.models import HansardContribution, HansardSession, HansardSessionTheme
    s = HansardSession(ext_id="T1", title="Hospices", date=date(2026, 10, 14), house="Commons",
                       debate_type="debate", is_container=False, slug="hospices-t1")
    app_db.session.add(s)
    app_db.session.flush()
    app_db.session.add(HansardContribution(session_id=s.id, member_name="A (Lab)", speech_order=1,
                                           speech_text="Hospice funding is in crisis across the country."))
    app_db.session.commit()
    monkeypatch.setattr(tg, "_gemini_json", lambda key, model, prompt, schema: ({
        "policy_areas": [{"area": "Health and social care", "evidence": "Hospice funding is in crisis"},
                         {"area": "Economy", "evidence": "the economy is weak"}],
        "topics": [{"topic": "hospice funding", "evidence": "hospice funding is in crisis"}]}, {}))

    assert tg.tag_session(s.id, api_key="key") == 2
    rows = HansardSessionTheme.query.filter_by(session_id=s.id).order_by(HansardSessionTheme.id).all()
    assert [(r.theme, r.theme_type, r.model_used) for r in rows] == [
        ("Health and social care", "policy_area", "test-model"),
        ("hospice funding", "specific", "test-model")]
    assert tg.tag_session(s.id, api_key="key") == 0          # already tagged: skipped

    replaced = tg.write_session_tags(s.id, TagOutcome(policy_areas=["Economy"], model="m2"), replace=True)
    assert replaced == 1
    assert [r.theme for r in HansardSessionTheme.query.filter_by(session_id=s.id)] == ["Economy"]


def test_api_failure_writes_nothing(app_db, monkeypatch):
    from hansard_archive.models import HansardContribution, HansardSession, HansardSessionTheme
    s = HansardSession(ext_id="T2", title="X", date=date(2026, 10, 14), house="Commons",
                       debate_type="debate", is_container=False, slug="x-t2")
    app_db.session.add(s)
    app_db.session.flush()
    app_db.session.add(HansardContribution(session_id=s.id, member_name="A", speech_order=1,
                                           speech_text="Some words here."))
    app_db.session.commit()

    def boom(*a):
        raise tg.GeminiCallFailed("HTTP 500")
    monkeypatch.setattr(tg, "_gemini_json", boom)
    with pytest.raises(tg.GeminiCallFailed):        # callers count it as an error
        tg.tag_session(s.id, api_key="key")
    assert HansardSessionTheme.query.filter_by(session_id=s.id).count() == 0


# ── HTML entities, ellipsis parts, writable rule ──────────────────────────────

def test_html_entities_in_stored_text():
    src = normalise_for_evidence("Pressures on A&amp;E admissions and &lt;b&gt;bed&lt;/b&gt; capacity")
    assert evidence_in_source("A&E admissions", src)
    assert evidence_in_source("bed capacity", src)
    assert tg.normalise_specific_theme("A&amp;E waiting times") == "a&e waiting times"
    assert "&amp;" not in tg._transcript([("Ms A (Lab)", "A&amp;E <p>delays</p>")], 1000)
    prompt, source = build_pq_prompt(PQInput(uin="1", heading="A&amp;E", question_text="Q &amp; A"))
    assert "&amp;" not in prompt and source.startswith("A&E")


def test_every_ellipsis_part_needs_two_words():
    src = normalise_for_evidence("the national campaign to end the scheme and the regional campaign")
    assert not evidence_in_source("the ... campaign", src)
    assert evidence_in_source("the national campaign ... the regional campaign", src)


def test_topics_alone_are_not_writable():
    out = verify_tags({"policy_areas": [], "topics": [
        {"topic": "care plans", "evidence": "Education, Health and Care Plans"}]}, SRC)
    assert out.topics == ["care plans"] and not out.ok


def test_schemas_put_uin_and_areas_first():
    assert tg._BATCH_RESPONSE_SCHEMA["items"]["propertyOrdering"][0] == "uin"
    assert tg._RESPONSE_SCHEMA["propertyOrdering"] == ["policy_areas", "topics"]


# ── API errors ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, text="", body=None):
        self.status_code, self.text, self._body = status, text, body

    def json(self):
        return self._body


def test_http_400_is_final_and_logs_body(monkeypatch, caplog):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(url)
        return _Resp(400, '{"error": {"message": "Invalid JSON payload: unknown field propertyOrdering"}}')
    monkeypatch.setattr(tg.requests, "post", post)
    with pytest.raises(tg.GeminiCallFailed) as exc:
        tg._gemini_json("key", "m", "prompt", tg._RESPONSE_SCHEMA)
    assert exc.value.final and len(calls) == 1
    assert "unknown field propertyOrdering" in caplog.text


def test_http_500_is_retried_then_not_final(monkeypatch):
    calls = []
    monkeypatch.setattr(tg.requests, "post", lambda url, json=None, timeout=None: calls.append(1) or _Resp(500, "x"))
    with pytest.raises(tg.GeminiCallFailed) as exc:
        tg._gemini_json("key", "m", "prompt", tg._RESPONSE_SCHEMA)
    assert not exc.value.final and len(calls) == 6      # 3 attempts x 2 API versions


def _sessions(app_db, n):
    from hansard_archive.models import HansardContribution, HansardSession
    rows = [HansardSession(ext_id=f"A{i}", title=f"Item {i}", date=date(2026, 10, 1 + i), house="Commons",
                           debate_type="debate", is_container=False, slug=f"item-a{i}") for i in range(n)]
    app_db.session.add_all(rows)
    app_db.session.flush()
    for r in rows:
        app_db.session.add(HansardContribution(session_id=r.id, member_name="A", speech_order=1,
                                               speech_text="Hospice funding is in crisis."))
    app_db.session.commit()
    return rows


def test_run_stops_after_consecutive_api_failures_and_counts_errors(app_db, monkeypatch):
    _sessions(app_db, 8)
    calls = []

    def boom(*a):
        calls.append(1)
        raise tg.GeminiCallFailed("HTTP 503")
    monkeypatch.setattr(tg, "_gemini_json", boom)
    res = tg.tag_all_untagged(api_key="key", verbose=False)
    assert len(calls) == tg.MAX_CONSECUTIVE_API_FAILURES
    assert res["errors"] == res["api_errors"] == tg.MAX_CONSECUTIVE_API_FAILURES
    assert "consecutive API failure" in res["aborted"]


def test_final_api_error_stops_at_once(app_db, monkeypatch):
    _sessions(app_db, 4)
    calls = []

    def bad_request(*a):
        calls.append(1)
        raise tg.GeminiCallFailed("HTTP 400: schema", final=True)
    monkeypatch.setattr(tg, "_gemini_json", bad_request)
    res = tg.tag_all_untagged(api_key="key", verbose=False)
    assert len(calls) == 1 and res["errors"] == 1 and res["aborted"].startswith("stopped: HTTP 400")


def test_items_are_given_up_after_max_attempts(app_db, monkeypatch):
    from hansard_archive.models import HaTagAttempt
    app_db.create_all()                       # includes ha_tag_attempt
    HaTagAttempt.query.delete()
    app_db.session.commit()
    rows = _sessions(app_db, 2)
    calls = []

    def nothing_verified(*a):
        calls.append(1)
        return {"policy_areas": [{"area": "Economy", "evidence": "not in the text at all"}], "topics": []}, {}
    monkeypatch.setattr(tg, "_gemini_json", nothing_verified)
    for _ in range(tg.TAG_MAX_ATTEMPTS):
        res = tg.tag_all_untagged(api_key="key", verbose=False)
        assert res["failed"] == 2 and res["errors"] == 0
    assert len(calls) == 2 * tg.TAG_MAX_ATTEMPTS
    res = tg.tag_all_untagged(api_key="key", verbose=False)       # given up: not sent again
    assert res["total_eligible"] == 0 and res["given_up"] == 2 and len(calls) == 2 * tg.TAG_MAX_ATTEMPTS
    att = app_db.session.get(HaTagAttempt, ("session", rows[0].id))
    assert att.attempts == tg.TAG_MAX_ATTEMPTS and "evidence not found" in att.last_reason
    HaTagAttempt.query.delete()
    app_db.session.commit()


def test_pq_final_error_aborts_run(app_db, monkeypatch):
    from hansard_archive.models import HaPQ, HaPQTheme
    HaPQTheme.query.delete()
    HaPQ.query.delete()
    app_db.session.add_all([HaPQ(uin=f"U{i}", question_text="What about hospice funding in Kent?",
                                 tabled_date=date(2026, 10, 1)) for i in range(3)])
    app_db.session.commit()

    def bad_request(*a):
        raise tg.GeminiCallFailed("HTTP 400: schema", final=True)
    monkeypatch.setattr(tg, "_gemini_json", bad_request)
    res = tg.tag_pq_all_untagged(api_key="key", verbose=False)
    assert res["errors"] == 3 and res["aborted"].startswith("stopped: HTTP 400")
    HaPQ.query.delete()
    app_db.session.commit()


def test_question_groups_follow_hansard_name_forms():
    """A minister appears as 'The Secretary of State for X (Name)' then as 'Name';
    a backbencher as 'Name (Seat) (Party)' then 'Name'."""
    contribs = [
        ("Freddie van Mierlo (Henley and Thame) (LD)", "T1 question"),
        ("The Secretary of State for Business and Trade (Jonathan Reynolds)", "answer"),
        ("Freddie van Mierlo", "follow-up"),
        ("Jonathan Reynolds", "answer 2"),
        ("Alex McIntyre (Gloucester) (Lab)", "T2 question"),
        ("Madam Deputy Speaker (Ms Nusrat Ghani)", "Order."),
        ("The Minister for the Future of Work (Kate Dearden)", "answer"),
        ("Julia Lopez (Hornchurch and Upminster) (Con)", "shadow question"),
        ("Jonathan Reynolds", "answer"),
        ("Julia Lopez", "follow-up"),
        ("Jonathan Reynolds", "answer"),
    ]
    groups = question_groups(contribs)
    assert [len(g) for g in groups] == [4, 3, 4]
