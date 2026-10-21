"""
Hansard Archive — topic tagging for debate sessions and Written Questions.

Entry points (unchanged for the crons):
  tag_session(session_id) -> int           tag one session, return rows written
  tag_all_untagged(limit=None) -> dict     tag all untagged non-container sessions
  tag_pq(pq_id) -> int                     tag one Written Question
  tag_pq_all_untagged(limit=None) -> dict  tag all untagged Written Questions

Pure building blocks (used by scripts/retag.py and scripts/eval_tagging.py,
which must not write or must write differently):
  SessionInput / PQInput                   plain inputs, no DB
  tag_session_input(...) / tag_pq_inputs(...) -> TagOutcome per item
  write_session_tags(...) / write_pq_tags(...)

Each item gets two kinds of ha_*_theme row:
  policy_area — 1 to 3 names from the controlled list (policy_areas.py), most
                central first, chosen against a one-line scope note per area
  specific    — 1 to 5 concrete topics, canonicalised by topic_canon

Accuracy rules (owner standard: accuracy before cost):
  - The model must quote, verbatim, a short phrase from the input that shows
    each policy area and each topic. A tag whose quote is not in the input
    (after normalising case, punctuation and quote marks) is dropped. This
    removes tags invented from the title alone or copied from the prompt.
  - The prompt holds no example topic values a model could copy.
  - Sessions made of many unrelated questions (PMQs, Topical Questions,
    Business of the House) are tagged per question group and aggregated, so
    one long exchange doesn't stand for the whole session.

Gemini with JSON schema enforcement (policy areas are a hard enum).
Default model: gemini-2.5-flash-lite (auto-detected); override with the
TAGGER_MODEL environment variable or the `model` argument.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

from extensions import db
from hansard_archive.models import (
    THEME_TYPE_POLICY_AREA,
    THEME_TYPE_SPECIFIC,
    HansardSession,
    HansardSessionTheme,
    HaPQ,
    HaPQTheme,
)
from hansard_archive.policy_areas import HANSARD_POLICY_NAMES as _HANSARD_POLICY_NAMES
from hansard_archive.policy_areas import POLICY_AREA_SCOPE
from hansard_archive.topic_canon import canonical_topic

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 60
_INTER_REQUEST_DELAY = 0.4   # between single-item calls
_MAX_RETRIES = 2

_PQ_BATCH_SIZE = 5           # small batches: fewer cross-question mix-ups
_PQ_BATCH_DELAY = 0.5        # between batch calls — paid tier
_PQ_BATCH_QUESTION_CHARS = 600
_PQ_SINGLE_QUESTION_CHARS = 1200
_PQ_SINGLE_ANSWER_CHARS = 400

_SESSION_TEXT_CHARS = 12000  # ~2,000 words of transcript for single-subject items
_GROUP_TEXT_CHARS = 1500     # per question group in a multi-question session
_GROUPS_MAX = 40
_GROUPED_TEXT_CHARS = 30000

MAX_POLICY_AREAS = 3
MAX_TOPICS = 5

DEFAULT_MODEL = "gemini-2.5-flash-lite"

# ---------------------------------------------------------------------------
# Policy area vocabulary — canonical list lives in policy_areas.py.
# ---------------------------------------------------------------------------

POLICY_AREAS = _HANSARD_POLICY_NAMES
_VALID_AREAS = frozenset(POLICY_AREAS)

_SCOPE_BLOCK = "\n".join(f"- {name}: {POLICY_AREA_SCOPE[name]}" for name in POLICY_AREAS)

# ---------------------------------------------------------------------------
# Specific-topic hygiene — applied to every free-text theme before it is stored.
# Topic pages (/brief/<slug>) match stored themes, so a theme that is only a
# policy-area word or a type of business becomes its own misleading page.
# ---------------------------------------------------------------------------

# Spelling variants and acronyms ("SEND" lowercases to the verb "send") are
# mapped to one canonical topic by topic_canon, so each topic has one page.

# One-word themes that just restate a policy area, and names of kinds of
# parliamentary business, which say nothing about the subject.
_GENERIC_THEMES = frozenset({a.lower() for a in POLICY_AREAS} | {
    "housing", "health", "healthcare", "education", "defence", "immigration",
    "energy", "transport", "environment", "trade", "welfare", "crime", "justice",
    "economy", "business", "employment", "taxation", "finance", "planning",
    "policy", "government", "parliament", "foreign policy", "security",
    "oral questions", "topical questions", "written questions", "written question",
    "prime minister's questions", "pmqs", "engagements", "business of the house",
    "urgent question", "ministerial statement", "statement", "debate",
    "westminster hall debate", "e-petition", "petition", "government policy",
    "government response", "uk government", "legislation",
})
_TOPIC_MAX_WORDS = 8
_TOPIC_MAX_CHARS = 80


def normalise_specific_theme(theme: str) -> Optional[str]:
    """Lowercase, trim and canonicalise a free-text theme; None if it should be dropped."""
    t = " ".join((theme or "").lower().split()).strip(" .,;:")
    if not t:
        return None
    t = canonical_topic(t)
    if t in _GENERIC_THEMES:
        return None
    if len(t) > _TOPIC_MAX_CHARS or len(t.split()) > _TOPIC_MAX_WORDS:
        return None
    return t


def _clean_themes(themes) -> list[str]:
    """Normalised, de-duplicated specific themes in the model's order, at most 5."""
    out: list[str] = []
    for raw in themes or []:
        t = normalise_specific_theme(raw if isinstance(raw, str) else "")
        if t and t not in out:
            out.append(t)
    return out[:MAX_TOPICS]


# ---------------------------------------------------------------------------
# Evidence check
# ---------------------------------------------------------------------------

_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "–": "-", "—": "-", " ": " ",
})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ELLIPSIS = re.compile(r"\.\.\.|…")
_EVIDENCE_MIN_WORDS = 2


def normalise_for_evidence(s: str) -> str:
    """Lowercase, unify quotes and dashes, drop punctuation, collapse spaces."""
    s = unicodedata.normalize("NFKC", s or "").translate(_CHAR_MAP).lower()
    s = s.replace("'", "")             # "women's" == "womens"
    return " ".join(_NON_ALNUM.sub(" ", s).split())


def evidence_in_source(quote: str, source_norm: str) -> bool:
    """
    True when the quote appears verbatim (normalised, on word boundaries) in the
    already-normalised source. A quote with an ellipsis must have every part in
    the source. Quotes under two words prove nothing and fail.
    """
    if not isinstance(quote, str):
        return False
    parts = [normalise_for_evidence(p) for p in _ELLIPSIS.split(quote)]
    parts = [p for p in parts if p]
    if not parts or sum(len(p.split()) for p in parts) < _EVIDENCE_MIN_WORDS:
        return False
    haystack = f" {source_norm} "
    return all(f" {p} " in haystack for p in parts)


@dataclass
class TagOutcome:
    """Verified tags for one item, plus what was dropped and why (for eval / dry runs)."""
    policy_areas: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, value, reason)
    proposed_areas: int = 0
    proposed_topics: int = 0
    model: str = ""
    usage: dict = field(default_factory=dict)
    retried: bool = False      # PQ batches: came from a single-question retry

    @property
    def ok(self) -> bool:
        return bool(self.policy_areas or self.topics)


def verify_tags(raw: dict, source_text: str, outcome: Optional[TagOutcome] = None) -> TagOutcome:
    """
    Keep the policy areas and topics in a model result whose evidence quote is
    in source_text. Areas: valid names only, de-duplicated, at most 3 in the
    model's order. Topics: hygiene + canonicalisation, at most 5.
    """
    out = outcome or TagOutcome()
    src = normalise_for_evidence(source_text)
    raw = raw if isinstance(raw, dict) else {}

    for item in raw.get("policy_areas") or []:
        item = item if isinstance(item, dict) else {}
        area = item.get("area")
        out.proposed_areas += 1
        if area not in _VALID_AREAS:
            out.dropped.append(("policy_area", str(area), "not in the policy area list"))
        elif area in out.policy_areas:
            continue
        elif not evidence_in_source(item.get("evidence"), src):
            out.dropped.append(("policy_area", area, "evidence not found in input"))
        elif len(out.policy_areas) >= MAX_POLICY_AREAS:
            out.dropped.append(("policy_area", area, "over the limit of 3"))
        else:
            out.policy_areas.append(area)

    for item in raw.get("topics") or []:
        item = item if isinstance(item, dict) else {}
        out.proposed_topics += 1
        topic = normalise_specific_theme(item.get("topic") if isinstance(item.get("topic"), str) else "")
        if topic is None:
            out.dropped.append(("topic", str(item.get("topic")), "generic, too long or empty"))
        elif topic in out.topics:
            continue
        elif not evidence_in_source(item.get("evidence"), src):
            out.dropped.append(("topic", topic, "evidence not found in input"))
        elif len(out.topics) >= MAX_TOPICS:
            out.dropped.append(("topic", topic, "over the limit of 5"))
        else:
            out.topics.append(topic)
    return out


def aggregate_groups(group_outcomes: list[TagOutcome]) -> TagOutcome:
    """
    Combine per-question-group tags into session tags: the areas and topics
    raised in the most groups, first-raised first on a tie.
    """
    out = TagOutcome()
    area_n: Counter = Counter()
    topic_n: Counter = Counter()
    first: dict = {}
    for i, g in enumerate(group_outcomes):
        out.dropped += g.dropped
        out.proposed_areas += g.proposed_areas
        out.proposed_topics += g.proposed_topics
        for a in g.policy_areas:
            area_n[a] += 1
            first.setdefault(("a", a), i)
        for t in g.topics:
            topic_n[t] += 1
            first.setdefault(("t", t), i)
    out.policy_areas = sorted(area_n, key=lambda a: (-area_n[a], first[("a", a)]))[:MAX_POLICY_AREAS]
    out.topics = sorted(topic_n, key=lambda t: (-topic_n[t], first[("t", t)]))[:MAX_TOPICS]
    return out


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class SessionInput:
    title: str
    debate_type: str = ""
    date: str = ""
    house: str = "Commons"
    contributions: list[tuple[str, str]] = field(default_factory=list)   # (member_name, text)
    ref: str = ""            # session id or ext_id, for logs


@dataclass
class PQInput:
    uin: str
    heading: str = ""
    question_text: str = ""
    answer_text: str = ""
    answering_body: str = ""
    chamber: str = "Commons"
    tabled_date: str = ""
    ref: str = ""            # pq id, for logs


def session_input_from_db(session: HansardSession) -> SessionInput:
    from sqlalchemy import text
    rows = db.session.execute(
        text("""
            SELECT member_name, speech_text FROM ha_contribution
            WHERE session_id = :sid
            ORDER BY speech_order
        """),
        {"sid": session.id},
    ).fetchall()
    return SessionInput(
        title=session.title or "",
        debate_type=session.debate_type or "",
        date=session.date.isoformat() if session.date else "",
        house=session.house or "Commons",
        contributions=[(r[0] or "", r[1] or "") for r in rows if r[1]],
        ref=str(session.id),
    )


def pq_input_from_db(pq: HaPQ) -> PQInput:
    return PQInput(
        uin=pq.uin or "",
        heading=pq.heading or "",
        question_text=pq.question_text or "",
        answer_text=pq.answer_text or "",
        answering_body=pq.answering_body or "",
        chamber=pq.chamber or "Commons",
        tabled_date=pq.tabled_date.isoformat() if pq.tabled_date else "",
        ref=str(pq.id),
    )


# ---------------------------------------------------------------------------
# Multi-question sessions
# ---------------------------------------------------------------------------

_MULTI_QUESTION_TITLES = ("topical questions", "engagements", "business of the house",
                          "business statement")
_CHAIR_NAMES = re.compile(r"^(mr|madam|madame)?\s*(deputy\s+)?speaker\b|^the (deputy )?speaker\b|"
                          r"^the (lord )?speaker\b|^the chair", re.IGNORECASE)


def is_multi_question(si: SessionInput) -> bool:
    """PMQs, Topical Questions and Business of the House: many unrelated questions."""
    t = (si.title or "").strip().lower()
    return si.debate_type == "pmqs" or any(t.startswith(p) for p in _MULTI_QUESTION_TITLES)


_PROXY_NAME = re.compile(r"^The\s+[^(]+?\(([^)]+)\)\s*$")


def _person(name: str) -> str:
    """
    One identity per member across Hansard's name forms: a minister first
    appears as "The Secretary of State for X (Jo Bloggs)" and then as "Jo
    Bloggs"; a backbencher as "Jo Bloggs (Seat) (Party)" and then "Jo Bloggs".
    """
    n = (name or "").strip()
    m = _PROXY_NAME.match(n)
    if m:
        return m.group(1).strip()
    return re.sub(r"\s*\(.*$", "", n).strip()


def question_groups(contributions: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """
    Split a question session into groups: each starts when a new member asks a
    question after an answer, and holds the answer and that member's follow-ups.
    Answerers are the members who appear in ministerial form ("The Minister for
    X (Name)"), or, if nobody does, the most frequent speaker. Chair
    interventions stay in the current group.
    """
    speakers = [n for n, _ in contributions if n and not _CHAIR_NAMES.match(n)]
    answerers = {_person(n) for n in speakers if n.startswith("The ")}
    if not answerers and speakers:
        answerers = {Counter(_person(n) for n in speakers).most_common(1)[0][0]}
    groups: list[list[tuple[str, str]]] = []
    questioner = None
    prev_answer = True
    for name, text_ in contributions:
        if _CHAIR_NAMES.match(name or ""):
            if groups:
                groups[-1].append((name, text_))
            continue
        person = _person(name)
        answer = (name or "").startswith("The ") or person in answerers
        if not groups or (not answer and prev_answer and person != questioner):
            groups.append([])
            questioner = None
        if not answer and questioner is None:
            questioner = person
        groups[-1].append((name, text_))
        prev_answer = answer
    return [g for g in groups if g]


def _transcript(contributions: list[tuple[str, str]], max_chars: int) -> str:
    out, used = [], 0
    for name, text_ in contributions:
        line = f"{name or 'Speaker'}: {' '.join((text_ or '').split())}"
        if used + len(line) > max_chars:
            line = line[: max(0, max_chars - used)]
        if line:
            out.append(line)
            used += len(line) + 1
        if used >= max_chars:
            break
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Prompts — no example topic values (a model copies them)
# ---------------------------------------------------------------------------

_RULES = """\
Rules:
- policy_areas: 1 to 3 names from the list below, most central first. Choose by \
what the business is about, using each area's scope note, not by words that \
happen to appear. An area mentioned only in passing does not count. Prefer one \
area when one fits.
- topics: 1 to 5 specific subjects, most central first, each 2 to 6 words, \
lowercase: the concrete policy, programme, bill, place, organisation, event or \
group affected that the business is about. Not a policy-area name, not a single \
generic word, not the kind of business (question, debate, statement, petition).
- evidence: for every policy area and every topic, copy a short phrase (3 to 15 \
words) exactly as it appears in the text below — same words, same order — that \
shows it is central. Do not paraphrase, translate or join separate phrases. If \
you cannot quote support for a tag, leave the tag out.

Policy areas (name: scope):
{scope}
"""

_SESSION_PROMPT = """\
You are tagging one item of UK parliamentary business (Hansard) for a public \
research archive. Return the policy areas and specific topics it is about.

""" + _RULES + """
Item:
Title: {title}
Type: {debate_type}
House: {house}
Date: {date}

Transcript (start of the item):
<<<
{text}
>>>"""

_GROUPED_PROMPT = """\
You are tagging one item of UK parliamentary business (Hansard) for a public \
research archive. It is a session of {n} separate questions on unrelated \
subjects, numbered below. Tag EACH question group on its own: its policy areas \
(at most 2) and topics (at most 3), with evidence quoted from that group's own \
text. Return one entry per group, using the group number shown.

""" + _RULES + """
Item:
Title: {title}
Type: {debate_type}
House: {house}
Date: {date}

{groups}"""

_PQ_PROMPT = """\
You are tagging one UK Written Parliamentary Question for a public research \
archive. Return the policy areas and specific topics it is about.

""" + _RULES + """
Question:
Heading: {heading}
Answering department: {answering_body}
Chamber: {chamber}
Date tabled: {tabled_date}
Text:
<<<
{question_text}
>>>
{answer_block}"""

_PQ_BATCH_PROMPT = """\
You are tagging UK Written Parliamentary Questions for a public research \
archive. Tag EACH of the {n} questions below on its own, with evidence quoted \
from that question's own heading or text. Return exactly {n} entries in the \
order given, each with the question's UIN exactly as shown.

""" + _RULES + """
{questions}"""


def build_session_prompt(si: SessionInput) -> tuple[str, str]:
    """(prompt, source text the evidence must come from) for a single-subject item."""
    text_ = _transcript(si.contributions, _SESSION_TEXT_CHARS)
    prompt = _SESSION_PROMPT.format(
        scope=_SCOPE_BLOCK, title=si.title, debate_type=si.debate_type or "unknown",
        house=si.house or "Commons", date=si.date or "unknown", text=text_,
    )
    return prompt, f"{si.title}\n{text_}"


def build_grouped_prompt(si: SessionInput) -> tuple[str, list[str]]:
    """(prompt, per-group source texts) for a multi-question session."""
    groups = question_groups(si.contributions)[:_GROUPS_MAX]
    per_group = max(400, min(_GROUP_TEXT_CHARS, _GROUPED_TEXT_CHARS // max(1, len(groups))))
    texts = [_transcript(g, per_group) for g in groups]
    block = "\n\n".join(f"[Group {i + 1}]\n{t}" for i, t in enumerate(texts))
    prompt = _GROUPED_PROMPT.format(
        scope=_SCOPE_BLOCK, n=len(texts), title=si.title,
        debate_type=si.debate_type or "unknown", house=si.house or "Commons",
        date=si.date or "unknown", groups=block,
    )
    return prompt, texts


def build_pq_prompt(pq: PQInput) -> tuple[str, str]:
    q = (pq.question_text or "")[:_PQ_SINGLE_QUESTION_CHARS]
    a = (pq.answer_text or "")[:_PQ_SINGLE_ANSWER_CHARS]
    answer_block = f"Answer (start):\n<<<\n{a}\n>>>" if a else ""
    prompt = _PQ_PROMPT.format(
        scope=_SCOPE_BLOCK, heading=pq.heading or "(no heading)",
        answering_body=pq.answering_body or "unknown", chamber=pq.chamber or "Commons",
        tabled_date=pq.tabled_date or "unknown", question_text=q, answer_block=answer_block,
    )
    return prompt, f"{pq.heading}\n{q}\n{a}"


def build_pq_batch_prompt(pqs: list[PQInput]) -> tuple[str, list[str]]:
    """(prompt, per-question source texts): heading + first 600 chars of the question."""
    blocks, sources = [], []
    for i, pq in enumerate(pqs):
        q = (pq.question_text or "")[:_PQ_BATCH_QUESTION_CHARS]
        blocks.append(
            f"[{i + 1}] UIN: {pq.uin}\n"
            f"Heading: {pq.heading or '(none)'}\n"
            f"Department: {pq.answering_body or 'unknown'}\n"
            f"Question: {q}"
        )
        sources.append(f"{pq.heading}\n{q}")
    prompt = _PQ_BATCH_PROMPT.format(scope=_SCOPE_BLOCK, n=len(pqs), questions="\n\n".join(blocks))
    return prompt, sources


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

def _tag_fields(max_areas: int, max_topics: int) -> dict:
    return {
        "policy_areas": {
            "type": "ARRAY",
            "maxItems": max_areas,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "area": {"type": "STRING", "enum": POLICY_AREAS},
                    "evidence": {"type": "STRING"},
                },
                "required": ["area", "evidence"],
            },
        },
        "topics": {
            "type": "ARRAY",
            "maxItems": max_topics,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING"},
                    "evidence": {"type": "STRING"},
                },
                "required": ["topic", "evidence"],
            },
        },
    }


_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": _tag_fields(MAX_POLICY_AREAS, MAX_TOPICS),
    "required": ["policy_areas", "topics"],
}

_GROUPED_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "groups": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"group": {"type": "INTEGER"}, **_tag_fields(2, 3)},
                "required": ["group", "policy_areas", "topics"],
            },
        },
    },
    "required": ["groups"],
}

_BATCH_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"uin": {"type": "STRING"}, **_tag_fields(MAX_POLICY_AREAS, MAX_TOPICS)},
        "required": ["uin", "policy_areas", "topics"],
    },
}


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict[str, str] = {}


def _detect_model(api_key: str) -> str:
    if api_key in _MODEL_CACHE:
        return _MODEL_CACHE[api_key]
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
            timeout=5,
        )
        if resp.status_code == 200:
            available = [
                m["name"]
                for m in resp.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            for prefix in ["models/gemini-2.5-flash-lite", "models/gemini-2.5-flash"]:
                match = next((m for m in available if m.startswith(prefix)), None)
                if match:
                    _MODEL_CACHE[api_key] = match.removeprefix("models/")
                    return _MODEL_CACHE[api_key]
    except Exception:
        pass
    _MODEL_CACHE[api_key] = DEFAULT_MODEL
    return _MODEL_CACHE[api_key]


def resolve_model(api_key: str, model: Optional[str] = None) -> str:
    """Explicit model, else TAGGER_MODEL, else the auto-detected Flash-Lite."""
    return model or os.environ.get("TAGGER_MODEL") or _detect_model(api_key)


class GeminiCallFailed(Exception):
    """All retries failed (network, 5xx, rate limit or unparseable output)."""


def _gemini_json(api_key: str, model: str, prompt: str, schema: dict) -> tuple[object, dict]:
    """
    One structured-output call. Returns (parsed JSON, usage metadata).
    Raises GeminiCallFailed after retries; auth errors raise at once.
    """
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0,
        },
    }
    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        for version in ("v1beta", "v1"):
            url = (
                f"https://generativelanguage.googleapis.com/{version}/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                r = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 200:
                    body = r.json()
                    raw = body["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(raw), body.get("usageMetadata") or {}
                if r.status_code == 429:
                    logger.warning("Tagger: rate limited (429), backing off")
                    last_error = "429"
                    time.sleep(5 * (attempt + 1))
                    break
                if r.status_code in (401, 403):
                    logger.error("Tagger: auth error %d — check GEMINI_API_KEY", r.status_code)
                    raise GeminiCallFailed(f"auth error {r.status_code}")
                last_error = f"HTTP {r.status_code}"
            except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
                last_error = str(e)[:200]
                logger.warning("Tagger: attempt %d/%d failed: %s", attempt + 1, _MAX_RETRIES + 1, e)
        if attempt < _MAX_RETRIES:
            time.sleep(2 ** attempt)
    raise GeminiCallFailed(last_error or "no response")


def _add_usage(total: dict, usage: dict) -> None:
    for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"):
        total[k] = total.get(k, 0) + int(usage.get(k) or 0)


# USD per 1M tokens: (input, output). Output includes thinking tokens, which
# Gemini bills as output. Paid-tier list prices for prompts under 200K tokens as
# last checked — confirm at https://ai.google.dev/gemini-api/docs/pricing before
# a large run (scripts/retag.py prints the estimate it uses).
MODEL_PRICES_PER_M: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash":      (0.30, 2.50),
    "gemini-2.5-pro":        (1.25, 10.00),
}


def model_prices(model: str) -> Optional[tuple[float, float]]:
    """Prices for a model id, matching versioned ids ("gemini-2.5-flash-lite-preview-..")."""
    for name in sorted(MODEL_PRICES_PER_M, key=len, reverse=True):
        if (model or "").startswith(name):
            return MODEL_PRICES_PER_M[name]
    return None


def usage_cost(usage: dict, model: str) -> Optional[float]:
    """USD cost of the token usage recorded on a TagOutcome, or None for an unknown model."""
    prices = model_prices(model)
    if prices is None:
        return None
    out_tokens = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
    return (int(usage.get("promptTokenCount") or 0) * prices[0] + out_tokens * prices[1]) / 1_000_000


# ---------------------------------------------------------------------------
# Tagging without writing
# ---------------------------------------------------------------------------

def tag_session_input(si: SessionInput, api_key: str, model: Optional[str] = None) -> TagOutcome:
    """Tag one session (no DB writes). Raises GeminiCallFailed on API failure."""
    model = resolve_model(api_key, model)
    if is_multi_question(si) and len(question_groups(si.contributions)) >= 2:
        prompt, sources = build_grouped_prompt(si)
        parsed, usage = _gemini_json(api_key, model, prompt, _GROUPED_RESPONSE_SCHEMA)
        by_group: dict[int, dict] = {}
        groups = parsed.get("groups") if isinstance(parsed, dict) else None
        for g in groups or []:
            if isinstance(g, dict) and isinstance(g.get("group"), int):
                by_group.setdefault(g["group"], g)
        outcomes = []
        for i, src in enumerate(sources):
            g = by_group.get(i + 1)
            if g is not None:
                outcomes.append(verify_tags(g, src))
        out = aggregate_groups(outcomes)
    else:
        prompt, source = build_session_prompt(si)
        parsed, usage = _gemini_json(api_key, model, prompt, _RESPONSE_SCHEMA)
        out = verify_tags(parsed if isinstance(parsed, dict) else {}, source)
    out.model = model
    _add_usage(out.usage, usage)
    return out


def tag_pq_input(pq: PQInput, api_key: str, model: Optional[str] = None) -> TagOutcome:
    """Tag one Written Question on its own (no DB writes)."""
    model = resolve_model(api_key, model)
    prompt, source = build_pq_prompt(pq)
    parsed, usage = _gemini_json(api_key, model, prompt, _RESPONSE_SCHEMA)
    out = verify_tags(parsed if isinstance(parsed, dict) else {}, source)
    out.model = model
    _add_usage(out.usage, usage)
    return out


def tag_pq_inputs(pqs: list[PQInput], api_key: str, model: Optional[str] = None,
                  batch_size: int = _PQ_BATCH_SIZE) -> list[Optional[TagOutcome]]:
    """
    Tag Written Questions in batches (no DB writes). Results are mapped back by
    POSITION and accepted only when the UIN the model echoes agrees; anything
    that doesn't line up (or a failed batch) is retried on its own. One entry
    per input; None where even the single retry failed.
    """
    model = resolve_model(api_key, model)
    results: list[Optional[TagOutcome]] = [None] * len(pqs)
    for start in range(0, len(pqs), batch_size):
        batch = pqs[start:start + batch_size]
        retry: list[int] = []
        try:
            prompt, sources = build_pq_batch_prompt(batch)
            parsed, usage = _gemini_json(api_key, model, prompt, _BATCH_RESPONSE_SCHEMA)
        except GeminiCallFailed as e:
            logger.warning("Tagger batch at %d failed (%s) — retrying individually", start, e)
            parsed, usage, sources = None, {}, []
        if isinstance(parsed, list) and len(parsed) == len(batch):
            share = {k: int(v or 0) // len(batch) for k, v in usage.items() if isinstance(v, int)}
            for j, pq in enumerate(batch):
                cand = parsed[j] if isinstance(parsed[j], dict) else {}
                echoed = str(cand.get("uin") or "").strip().lower()
                if echoed == (pq.uin or "").strip().lower():
                    out = verify_tags(cand, sources[j])
                    out.model = model
                    _add_usage(out.usage, share)
                    results[start + j] = out
                    if not out.ok:
                        retry.append(j)     # nothing verified: one more try with more context
                else:
                    logger.warning("Tagger batch: position %d echoed UIN %r, expected %r (PQ %s)",
                                   j + 1, cand.get("uin"), pq.uin, pq.ref)
                    retry.append(j)
        else:
            if parsed is not None:
                logger.warning("Tagger batch at %d: %s results for %d questions — retrying individually",
                               start, len(parsed) if isinstance(parsed, list) else "non-list", len(batch))
            retry = list(range(len(batch)))
        for j in retry:
            time.sleep(_INTER_REQUEST_DELAY)
            try:
                single = tag_pq_input(batch[j], api_key, model)
            except GeminiCallFailed as e:
                logger.warning("Tagger: single retry failed for PQ %s: %s", batch[j].ref, e)
                continue
            if single is not None:
                single.retried = True
                if single.ok or results[start + j] is None:
                    results[start + j] = single
        if start + batch_size < len(pqs):
            time.sleep(_PQ_BATCH_DELAY)
    return results


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_session_tags(session_id: int, outcome: TagOutcome, replace: bool = False,
                       now: Optional[datetime] = None) -> int:
    """Store verified tags for a session (policy areas in centrality order). Commits."""
    now = now or datetime.utcnow()
    if replace:
        HansardSessionTheme.query.filter_by(session_id=session_id).delete(synchronize_session=False)
    count = 0
    for area in outcome.policy_areas:
        db.session.add(HansardSessionTheme(session_id=session_id, theme=area,
                                           theme_type=THEME_TYPE_POLICY_AREA,
                                           tagged_at=now, model_used=outcome.model))
        count += 1
    for theme in outcome.topics:
        db.session.add(HansardSessionTheme(session_id=session_id, theme=theme,
                                           theme_type=THEME_TYPE_SPECIFIC,
                                           tagged_at=now, model_used=outcome.model))
        count += 1
    if count or replace:
        db.session.commit()
    return count


def write_pq_tags(pq_id: int, outcome: TagOutcome, replace: bool = False,
                  now: Optional[datetime] = None) -> int:
    """Store verified tags for a Written Question. Commits."""
    now = now or datetime.utcnow()
    if replace:
        HaPQTheme.query.filter_by(pq_id=pq_id).delete(synchronize_session=False)
    count = 0
    seen: set[str] = set()
    for theme, theme_type in ([(a, THEME_TYPE_POLICY_AREA) for a in outcome.policy_areas]
                              + [(t, THEME_TYPE_SPECIFIC) for t in outcome.topics]):
        if theme in seen:          # (pq_id, theme) is the primary key
            continue
        seen.add(theme)
        db.session.merge(HaPQTheme(pq_id=pq_id, theme=theme, theme_type=theme_type,
                                   tagged_at=now, model_used=outcome.model))
        count += 1
    if count or replace:
        db.session.commit()
    return count


# ---------------------------------------------------------------------------
# Public entry points (crons)
# ---------------------------------------------------------------------------

def _api_key(api_key: Optional[str]) -> Optional[str]:
    if api_key:
        return api_key
    try:
        from flask import current_app
        return current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    except RuntimeError:
        return os.environ.get("GEMINI_API_KEY")


def tag_session(session_id: int, api_key: Optional[str] = None, model: Optional[str] = None) -> int:
    """
    Tag a single session. Skips if already tagged.
    Returns number of theme rows written (0 if skipped or failed).
    Expects to run inside a Flask app context with an active DB session.
    """
    api_key = _api_key(api_key)
    if not api_key:
        logger.error("Tagger: GEMINI_API_KEY not set")
        return 0

    session = db.session.get(HansardSession, session_id)
    if not session:
        return 0
    if HansardSessionTheme.query.filter_by(session_id=session_id).first():
        return 0

    si = session_input_from_db(session)
    if not any(t.strip() for _, t in si.contributions):
        logger.warning("Tagger: session %d has no contribution text — skipping", session_id)
        return 0

    try:
        outcome = tag_session_input(si, api_key, model)
    except GeminiCallFailed as e:
        logger.warning("Tagger: session %d (%r) — Gemini failed: %s", session_id, (session.title or "")[:50], e)
        return 0
    if not outcome.ok:
        logger.warning("Tagger: session %d — no tag survived the evidence check (%d dropped)",
                       session_id, len(outcome.dropped))
        return 0
    return write_session_tags(session_id, outcome)


def tag_all_untagged(
    limit: Optional[int] = None,
    api_key: Optional[str] = None,
    verbose: bool = True,
    skip_types: tuple = ("other",),
) -> dict:
    """
    Batch tag all untagged non-container sessions.

    Excludes:
      - is_container = True  (structural headers with duplicate contributions)
      - debate_type IN skip_types  (default: 'other' — procedural/mixed sessions)

    Returns summary dict: {total_eligible, tagged, failed, errors}.
    Expects to run inside a Flask app context.
    """
    api_key = _api_key(api_key)
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    already_tagged_subq = db.session.query(HansardSessionTheme.session_id).distinct().subquery()
    query = (
        HansardSession.query
        .filter(HansardSession.is_container.is_(False))
        .filter(~HansardSession.debate_type.in_(skip_types))
        .filter(~HansardSession.id.in_(already_tagged_subq))
        .order_by(HansardSession.date.desc())
    )
    if limit:
        query = query.limit(limit)

    sessions = query.all()
    total_eligible = len(sessions)
    if verbose:
        print(f"[tagger] {total_eligible} sessions to tag (limit={limit})", flush=True)

    tagged = failed = errors = 0
    for i, session in enumerate(sessions):
        try:
            time.sleep(_INTER_REQUEST_DELAY)
            count = tag_session(session.id, api_key=api_key)
            if count > 0:
                tagged += 1
                if verbose:
                    print(f"[tagger] [{i+1}/{total_eligible}] + {session.title[:60]!r} — {count} tags",
                          flush=True)
            else:
                failed += 1
                if verbose:
                    print(f"[tagger] [{i+1}/{total_eligible}] FAIL {session.title[:60]!r}", flush=True)
        except Exception as e:
            errors += 1
            db.session.rollback()
            logger.error("Tagger: unexpected error on session %d: %s", session.id, e)
            if verbose:
                print(f"[tagger] ERROR session {session.id}: {e}", flush=True)

    return {"total_eligible": total_eligible, "tagged": tagged, "failed": failed, "errors": errors}


def tag_pq(pq_id: int, api_key: Optional[str] = None, model: Optional[str] = None) -> int:
    """
    Tag a single Written Question. Skips if already tagged.
    Returns number of theme rows written (0 if skipped or failed).
    """
    api_key = _api_key(api_key)
    if not api_key:
        logger.error("Tagger: GEMINI_API_KEY not set")
        return 0
    pq = db.session.get(HaPQ, pq_id)
    if not pq:
        return 0
    if HaPQTheme.query.filter_by(pq_id=pq_id).first():
        return 0
    if not (pq.question_text or "").strip():
        logger.warning("Tagger: PQ %d has no question text — skipping", pq_id)
        return 0
    try:
        outcome = tag_pq_input(pq_input_from_db(pq), api_key, model)
    except GeminiCallFailed as e:
        logger.warning("Tagger: PQ %d (%r) — Gemini failed: %s", pq_id, (pq.heading or pq.uin)[:50], e)
        return 0
    if not outcome.ok:
        return 0
    return write_pq_tags(pq_id, outcome)


def tag_pq_all_untagged(
    limit: Optional[int] = None,
    api_key: Optional[str] = None,
    verbose: bool = True,
) -> dict:
    """
    Batch tag all untagged Written Questions (batches of 5, mapped by position).

    Returns summary dict: {total_eligible, tagged, failed, retried, errors}.
    Expects to run inside a Flask app context.
    """
    api_key = _api_key(api_key)
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    already_tagged_subq = db.session.query(HaPQTheme.pq_id).distinct().subquery()
    query = (
        HaPQ.query
        .filter(~HaPQ.id.in_(already_tagged_subq))
        .filter(HaPQ.question_text.isnot(None))
        .order_by(HaPQ.tabled_date.desc())
    )
    if limit:
        query = query.limit(limit)
    pqs = [pq for pq in query.all() if (pq.question_text or "").strip()]
    total_eligible = len(pqs)
    if verbose:
        print(f"[pq-tagger] {total_eligible} PQs to tag in batches of {_PQ_BATCH_SIZE} "
              f"(limit={limit}, batch_delay={_PQ_BATCH_DELAY}s)", flush=True)

    tagged = failed = errors = retried = 0
    chunk = _PQ_BATCH_SIZE * 10
    for start in range(0, total_eligible, chunk):
        part = pqs[start:start + chunk]
        outcomes = tag_pq_inputs([pq_input_from_db(pq) for pq in part], api_key)
        for pq, outcome in zip(part, outcomes):
            retried += bool(outcome is not None and outcome.retried)
            if outcome is None or not outcome.ok:
                failed += 1
                continue
            try:
                if write_pq_tags(pq.id, outcome) > 0:
                    tagged += 1
                else:
                    failed += 1
            except Exception as e:
                errors += 1
                db.session.rollback()
                logger.error("Tagger: write error on PQ %d: %s", pq.id, e)
        if verbose:
            print(f"[pq-tagger] Progress: {min(start + chunk, total_eligible)}/{total_eligible}"
                  f" — tagged={tagged} failed={failed} errors={errors}", flush=True)

    return {"total_eligible": total_eligible, "tagged": tagged, "failed": failed,
            "retried": retried, "errors": errors}
