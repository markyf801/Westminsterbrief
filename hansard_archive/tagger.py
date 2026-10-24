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

For evaluation only (scripts/eval_tagging.py), a "claude-*" id passed as the
`model` argument uses the Claude backend (_claude_json) with the same prompts,
schema fields, evidence check and batching; `api_key` must then be an
Anthropic key. TAGGER_MODEL never selects Claude.
"""

from __future__ import annotations

import html
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
from sqlalchemy import text as sa_text

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


_TAG_RE = re.compile(r"<[^>]{0,200}>")


def plain_text(s: str) -> str:
    """Stored Hansard / PQ text can hold HTML entities (&amp;) and tags: plain text."""
    s = html.unescape(html.unescape(s or ""))      # some rows are double-escaped
    return " ".join(_TAG_RE.sub(" ", s).split())


def normalise_specific_theme(theme: str) -> Optional[str]:
    """Lowercase, trim and canonicalise a free-text theme; None if it should be dropped."""
    t = plain_text(theme).lower().strip(" .,;:")
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
    s = unicodedata.normalize("NFKC", plain_text(s)).translate(_CHAR_MAP).lower()
    s = s.replace("'", "")             # "women's" == "womens"
    return " ".join(_NON_ALNUM.sub(" ", s).split())


def evidence_in_source(quote: str, source_norm: str) -> bool:
    """
    True when the quote appears verbatim (normalised, on word boundaries) in the
    already-normalised source. A quote with an ellipsis must have every part in
    the source, and every part must be 2+ words ("the ... campaign" proves
    nothing). Quotes under two words fail.
    """
    if not isinstance(quote, str):
        return False
    parts = [normalise_for_evidence(p) for p in _ELLIPSIS.split(quote)]
    parts = [p for p in parts if p]
    if not parts or any(len(p.split()) < _EVIDENCE_MIN_WORDS for p in parts):
        return False
    haystack = f" {source_norm} "
    return all(f" {p} " in haystack for p in parts)


# Evidence that can never support a policy area, whatever the model says. Small
# models still pick "Welfare and benefits" on the word "welfare" (animal
# welfare, a child's welfare) despite the scope note. Matched on the normalised
# evidence quote; a vetoed area is dropped like an unsupported one.
_AREA_EVIDENCE_VETO: dict[str, re.Pattern] = {
    "Welfare and benefits": re.compile(
        r"(?<![a-z0-9])(?:"
        r"animals?|livestock|poultry|farmed|wildlife"
        r"|welfare standards?"
        r"|(?:hens?|chickens?|pigs?|birds?|fish|dogs?|cats?|pets?|horses?) welfare"
        r"|welfare of (?:the )?(?:animals?|livestock|birds?|hens?|chickens?|pigs?|fish"
        r"|dogs?|cats?|pets?|horses?|child|children|pupils?|students?)"
        r"|(?:child|childrens|pupils?|students?) welfare"
        r")(?![a-z0-9])"
    ),
}


# ...unless the same quote also names the area's own subject, e.g. "the
# two-child limit harms the welfare of children" or "PIP claimants with
# assistance animals" really are about benefits. Not "claims" (animal welfare
# claims on labels) or "benefits of" (the benefits of higher welfare).
_AREA_EVIDENCE_KEEP: dict[str, re.Pattern] = {
    "Welfare and benefits": re.compile(
        r"(?<![a-z0-9])(?:"
        r"benefits?(?! of)|universal credit|pip|personal independence payment|allowances?"
        r"|pensions?|claimants?|two child|social security|dwp"
        r"|department for work and pensions"
        r")(?![a-z0-9])"
    ),
}


def area_evidence_vetoed(area: str, quote) -> bool:
    """True when the evidence quote shows the area is the wrong sense of a word."""
    pattern = _AREA_EVIDENCE_VETO.get(area)
    if not (pattern and isinstance(quote, str)):
        return False
    q = normalise_for_evidence(quote)
    if not pattern.search(q):
        return False
    keep = _AREA_EVIDENCE_KEEP.get(area)
    return not (keep and keep.search(q))


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
    empty_reason: str = ""     # the model answered with no content (e.g. safety block)

    @property
    def ok(self) -> bool:
        """Writable: at least one verified policy area (topics alone are not enough —
        an item with no policy area drops off every policy page)."""
        return bool(self.policy_areas)


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
        elif area_evidence_vetoed(area, item.get("evidence")):
            out.dropped.append(("policy_area", area, "evidence is about another sense of the word"))
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
        line = f"{plain_text(name) or 'Speaker'}: {plain_text(text_)}"
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
    q = plain_text(pq.question_text)[:_PQ_SINGLE_QUESTION_CHARS]
    a = plain_text(pq.answer_text)[:_PQ_SINGLE_ANSWER_CHARS]
    heading = plain_text(pq.heading)
    answer_block = f"Answer (start):\n<<<\n{a}\n>>>" if a else ""
    prompt = _PQ_PROMPT.format(
        scope=_SCOPE_BLOCK, heading=heading or "(no heading)",
        answering_body=pq.answering_body or "unknown", chamber=pq.chamber or "Commons",
        tabled_date=pq.tabled_date or "unknown", question_text=q, answer_block=answer_block,
    )
    return prompt, f"{heading}\n{q}\n{a}"


def build_pq_batch_prompt(pqs: list[PQInput]) -> tuple[str, list[str]]:
    """(prompt, per-question source texts): heading + first 600 chars of the question."""
    blocks, sources = [], []
    for i, pq in enumerate(pqs):
        q = plain_text(pq.question_text)[:_PQ_BATCH_QUESTION_CHARS]
        heading = plain_text(pq.heading)
        blocks.append(
            f"[{i + 1}] UIN: {pq.uin}\n"
            f"Heading: {heading or '(none)'}\n"
            f"Department: {pq.answering_body or 'unknown'}\n"
            f"Question: {q}"
        )
        sources.append(f"{heading}\n{q}")
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
    "propertyOrdering": ["policy_areas", "topics"],
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
                "propertyOrdering": ["group", "policy_areas", "topics"],
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
        # uin first, so the model commits to which question it is tagging
        "propertyOrdering": ["uin", "policy_areas", "topics"],
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
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},  # not in the URL, so it can't leak into logs
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
    """
    Explicit model, else TAGGER_MODEL, else the auto-detected Flash-Lite.
    A Claude model is used only when passed explicitly (evaluation): free
    features run on Gemini, so a Claude TAGGER_MODEL is ignored.
    """
    if model:
        return model
    env_model = os.environ.get("TAGGER_MODEL")
    if is_claude_model(env_model):
        logger.error("Tagger: TAGGER_MODEL=%s ignored — Claude models are for evaluation only", env_model)
        env_model = None
    return env_model or _detect_model(api_key)


class GeminiCallFailed(Exception):
    """
    The call failed. final=True means retrying cannot help (bad request — e.g.
    a schema the API rejects —, unknown model, bad key): callers stop the run.
    Otherwise all retries failed (network, 5xx, rate limit, unparseable output).
    Also raised by the Claude backend (the name is kept for existing callers).
    """

    def __init__(self, message: str, final: bool = False):
        super().__init__(message)
        self.final = final


class GeminiEmptyResponse(Exception):
    """HTTP 200 without any content (safety block, no candidates). Counted as a
    tagging attempt for the item, never as an API failure."""

    def __init__(self, reason: str, usage: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.usage = usage or {}


_BODY_LOG_CHARS = 500


def _response_text(body: dict) -> Optional[str]:
    try:
        parts = (body.get("candidates") or [])[0]["content"]["parts"]
        text_ = "".join(p.get("text") or "" for p in parts)
    except (IndexError, KeyError, TypeError, AttributeError):
        return None
    return text_ if text_.strip() else None


def _empty_reason(body: dict) -> str:
    """finishReason / blockReason / safety categories of an empty response, for logs and attempts."""
    bits = []
    fb = body.get("promptFeedback") or {}
    if fb.get("blockReason"):
        bits.append(f"prompt blocked: {fb['blockReason']}")
    cands = body.get("candidates") or []
    if not cands:
        bits.append("no candidates")
    for c in cands[:1]:
        if c.get("finishReason"):
            bits.append(f"finishReason={c['finishReason']}")
        flagged = [r.get("category") for r in (c.get("safetyRatings") or fb.get("safetyRatings") or [])
                   if r.get("blocked") or r.get("probability") in ("MEDIUM", "HIGH")]
        if flagged:
            bits.append("safety: " + ",".join(str(f) for f in flagged))
    return "empty response (" + "; ".join(bits or ["no text"]) + ")"


def _gemini_json(api_key: str, model: str, prompt: str, schema: dict) -> tuple[object, dict]:
    """
    One structured-output call. Returns (parsed JSON, usage metadata).
    400 / 401 / 403 fail at once (final) with the response body logged; 404 on
    every API version is final too; 429, 5xx and network errors are retried.
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
        not_found = 0
        for version in ("v1beta", "v1"):
            url = (
                f"https://generativelanguage.googleapis.com/{version}/models/"
                f"{model}:generateContent"
            )
            try:
                r = requests.post(url, headers={"x-goog-api-key": api_key}, json=payload, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 200:
                    body = r.json()
                    raw = _response_text(body)
                    if raw is None:
                        # A real answer with nothing in it (safety block, empty
                        # candidates, MAX_TOKENS before any text): the item's
                        # problem, not the API's. Not retried, not an API failure.
                        why = _empty_reason(body)
                        logger.warning("Tagger: Gemini returned no content (%s, model %s)", why, model)
                        raise GeminiEmptyResponse(why, body.get("usageMetadata") or {})
                    return json.loads(raw), body.get("usageMetadata") or {}
                text_ = (r.text or "")[:_BODY_LOG_CHARS]
                if r.status_code in (400, 401, 403):
                    logger.error("Tagger: HTTP %d from Gemini (%s, model %s) — not retrying. Body: %s",
                                 r.status_code, version, model, text_)
                    raise GeminiCallFailed(f"HTTP {r.status_code}: {text_[:200]}", final=True)
                if r.status_code == 404:
                    not_found += 1
                    last_error = f"HTTP 404 ({version}): {text_[:200]}"
                    continue
                if r.status_code == 429:
                    logger.warning("Tagger: rate limited (429), backing off")
                    last_error = "HTTP 429"
                    time.sleep(5 * (attempt + 1))
                    break
                last_error = f"HTTP {r.status_code}: {text_[:200]}"
                logger.warning("Tagger: HTTP %d from Gemini, attempt %d/%d. Body: %s",
                               r.status_code, attempt + 1, _MAX_RETRIES + 1, text_)
            except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
                last_error = str(e)[:200]
                logger.warning("Tagger: attempt %d/%d failed: %s", attempt + 1, _MAX_RETRIES + 1, e)
        if not_found == 2:
            logger.error("Tagger: model %s not found on any API version — not retrying. %s",
                         model, last_error)
            raise GeminiCallFailed(last_error, final=True)
        if attempt < _MAX_RETRIES:
            time.sleep(2 ** attempt)
    raise GeminiCallFailed(last_error or "no response")


def _add_usage(total: dict, usage: dict) -> None:
    for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"):
        total[k] = total.get(k, 0) + int(usage.get(k) or 0)


# ---------------------------------------------------------------------------
# Claude client — for evaluation only (scripts/eval_tagging.py). Chosen only
# when a model id starting "claude-" is passed explicitly; never the default
# and never from TAGGER_MODEL. Same prompt, same schema fields, same evidence
# check and batching as Gemini: only the model call differs. The `anthropic`
# package is imported here, lazily, so the Gemini path never needs it.
# ---------------------------------------------------------------------------

CLAUDE_EFFORT = "low"          # classification task; Haiku 5.5 defaults to medium
_CLAUDE_MAX_TOKENS = 16000     # thinking + JSON; the largest (grouped) answers are a few thousand
_CLAUDE_TIMEOUT = 120.0        # seconds per request; the SDK retries timeouts
_CLAUDE_ARRAY_KEY = "results"  # a top-level array schema is wrapped in an object under this key

_CLAUDE_CLIENTS: dict[str, object] = {}


def is_claude_model(model: Optional[str]) -> bool:
    return (model or "").startswith("claude-")


def _claude_client(api_key: str):
    import anthropic
    client = _CLAUDE_CLIENTS.get(api_key)
    if client is None:
        client = anthropic.Anthropic(api_key=api_key, timeout=_CLAUDE_TIMEOUT, max_retries=_MAX_RETRIES)
        _CLAUDE_CLIENTS[api_key] = client
    return client


def _claude_schema(schema: dict) -> tuple[dict, bool]:
    """
    The Gemini response schema as JSON Schema for Claude structured outputs:
    lowercase types, no propertyOrdering (Claude keeps the properties' order),
    additionalProperties false on every object; constraints Claude doesn't
    enforce (maxItems) are moved into the description by the SDK. A top-level
    array is wrapped in an object; returns (schema, wrapped).
    """
    import anthropic

    def convert(s):
        if isinstance(s, dict):
            return {k: (v.lower() if k == "type" and isinstance(v, str) else convert(v))
                    for k, v in s.items() if k != "propertyOrdering"}
        if isinstance(s, list):
            return [convert(x) for x in s]
        return s

    out = convert(schema)
    wrapped = out.get("type") == "array"
    if wrapped:
        out = {"type": "object", "properties": {_CLAUDE_ARRAY_KEY: out}, "required": [_CLAUDE_ARRAY_KEY]}
    return anthropic.transform_schema(out), wrapped


def _claude_usage(usage) -> dict:
    """Claude usage in the Gemini usage keys, so _add_usage and usage_cost work
    unchanged. output_tokens already includes thinking (billed as output)."""
    if usage is None:
        return {}
    prompt = sum(int(getattr(usage, k, 0) or 0) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"promptTokenCount": prompt, "candidatesTokenCount": int(getattr(usage, "output_tokens", 0) or 0)}


def _claude_json(api_key: str, model: str, prompt: str, schema: dict) -> tuple[object, dict]:
    """
    One structured-output call to Claude, with the same contract as _gemini_json:
    returns (parsed JSON, usage); raises GeminiCallFailed (final for 4xx other
    than 429) or GeminiEmptyResponse (refusal, max_tokens, no text). HTTP
    retries (429, 5xx, connection) are the SDK's; unparseable JSON is retried here.
    """
    if not api_key or api_key.startswith("AIza"):
        # Never send a Gemini key to Anthropic, and never fall back to a key
        # found in the environment: the caller must pass an Anthropic key.
        raise GeminiCallFailed(f"model {model} needs an Anthropic API key (ANTHROPIC_API_KEY)", final=True)
    try:
        import anthropic
    except ImportError as e:
        raise GeminiCallFailed(f"model {model} needs the anthropic package: {e}", final=True) from e

    claude_schema, wrapped = _claude_schema(schema)
    client = _claude_client(api_key)
    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=_CLAUDE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": CLAUDE_EFFORT,
                               "format": {"type": "json_schema", "schema": claude_schema}},
            )
        except anthropic.RateLimitError as e:
            raise GeminiCallFailed(f"HTTP 429 from Claude: {str(e)[:200]}") from e
        except anthropic.APIStatusError as e:
            final = e.status_code < 500
            log = logger.error if final else logger.warning
            log("Tagger: HTTP %d from Claude (model %s)%s: %s", e.status_code, model,
                " — not retrying" if final else "", str(e)[:_BODY_LOG_CHARS])
            raise GeminiCallFailed(f"HTTP {e.status_code} from Claude: {str(e)[:200]}", final=final) from e
        except anthropic.APIConnectionError as e:   # includes timeouts
            raise GeminiCallFailed(f"Claude connection error: {str(e)[:200]}") from e
        except anthropic.APIError as e:
            raise GeminiCallFailed(f"Claude API error: {str(e)[:200]}") from e

        usage = _claude_usage(resp.usage)
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            why = f"empty response (Claude refusal, category={getattr(details, 'category', None)})"
            logger.warning("Tagger: %s, model %s", why, model)
            raise GeminiEmptyResponse(why, usage)
        if resp.stop_reason == "max_tokens":
            why = f"empty response (Claude stop_reason=max_tokens at {_CLAUDE_MAX_TOKENS})"
            logger.warning("Tagger: %s, model %s", why, model)
            raise GeminiEmptyResponse(why, usage)
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not raw.strip():
            why = f"empty response (Claude stop_reason={resp.stop_reason}, no text)"
            logger.warning("Tagger: %s, model %s", why, model)
            raise GeminiEmptyResponse(why, usage)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            last_error = str(e)[:200]
            logger.warning("Tagger: attempt %d/%d failed: unparseable JSON from Claude: %s",
                           attempt + 1, _MAX_RETRIES + 1, e)
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
            continue
        if wrapped and isinstance(parsed, dict) and _CLAUDE_ARRAY_KEY in parsed:
            parsed = parsed[_CLAUDE_ARRAY_KEY]
        return parsed, usage
    raise GeminiCallFailed(last_error or "no response")


def _model_json(api_key: str, model: str, prompt: str, schema: dict) -> tuple[object, dict]:
    """The structured-output call for `model`: Claude for "claude-*" ids, else Gemini."""
    if is_claude_model(model):
        return _claude_json(api_key, model, prompt, schema)
    return _gemini_json(api_key, model, prompt, schema)


# USD per 1M tokens: (input, output). Output includes thinking tokens, which
# Gemini bills as output. Paid-tier list prices for prompts under 200K tokens as
# last checked — confirm at https://ai.google.dev/gemini-api/docs/pricing before
# a large run (scripts/retag.py prints the estimate it uses).
MODEL_PRICES_PER_M: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash":      (0.30, 2.50),
    "gemini-2.5-pro":        (1.25, 10.00),
    # Anthropic list prices (Haiku 5.5: prompts up to 100K tokens, which every
    # tagging prompt is). Evaluation only. Output includes thinking tokens.
    "claude-haiku-5-5":      (0.10, 0.50),
    "claude-sonnet-5-5":     (2.00, 10.00),
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

def _empty_outcome(e: GeminiEmptyResponse, model: str) -> TagOutcome:
    out = TagOutcome(model=model, empty_reason=e.reason)
    _add_usage(out.usage, e.usage)
    return out


def tag_session_input(si: SessionInput, api_key: str, model: Optional[str] = None) -> TagOutcome:
    """
    Tag one session (no DB writes). Raises GeminiCallFailed on API failure; an
    empty (e.g. safety-blocked) response returns an outcome with nothing in it
    and empty_reason set.
    """
    model = resolve_model(api_key, model)
    try:
        return _tag_session_input(si, api_key, model)
    except GeminiEmptyResponse as e:
        return _empty_outcome(e, model)


def _tag_session_input(si: SessionInput, api_key: str, model: str) -> TagOutcome:
    if is_multi_question(si) and len(question_groups(si.contributions)) >= 2:
        prompt, sources = build_grouped_prompt(si)
        parsed, usage = _model_json(api_key, model, prompt, _GROUPED_RESPONSE_SCHEMA)
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
        parsed, usage = _model_json(api_key, model, prompt, _RESPONSE_SCHEMA)
        out = verify_tags(parsed if isinstance(parsed, dict) else {}, source)
    out.model = model
    _add_usage(out.usage, usage)
    return out


def tag_pq_input(pq: PQInput, api_key: str, model: Optional[str] = None) -> TagOutcome:
    """Tag one Written Question on its own (no DB writes); empty response -> empty outcome."""
    model = resolve_model(api_key, model)
    prompt, source = build_pq_prompt(pq)
    try:
        parsed, usage = _model_json(api_key, model, prompt, _RESPONSE_SCHEMA)
    except GeminiEmptyResponse as e:
        return _empty_outcome(e, model)
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
            parsed, usage = _model_json(api_key, model, prompt, _BATCH_RESPONSE_SCHEMA)
        except GeminiCallFailed as e:
            if e.final:
                raise
            logger.warning("Tagger batch at %d failed (%s) — retrying individually", start, e)
            parsed, usage, sources = None, {}, []
        except GeminiEmptyResponse as e:
            logger.warning("Tagger batch at %d: %s — retrying individually", start, e.reason)
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
                if e.final:
                    raise
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

# First key of the Postgres advisory lock taken while a session's tags are written.
_SESSION_TAGS_LOCK = 72_616_701
# write_session_tags / tag_session result when another run tagged the session
# between the caller's "already tagged?" check and the write: not a failure.
ALREADY_TAGGED = -1


def _lock_session_tags(session_id: int) -> None:
    """
    Postgres: hold a transaction-scoped advisory lock on one session's tags, so
    two writers (hourly archive cron, retag, backfill) can't interleave their
    delete/insert and leave a tag stored twice. Released at commit/rollback.
    ha_session_theme has no unique constraint to fall back on. SQLite
    serialises writers anyway.
    """
    if db.session.get_bind().dialect.name == "postgresql":
        db.session.execute(sa_text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), CAST(:i AS integer))"),
                           {"k": _SESSION_TAGS_LOCK, "i": session_id})


def write_session_tags(session_id: int, outcome: TagOutcome, replace: bool = False,
                       now: Optional[datetime] = None) -> int:
    """
    Store verified tags for a session (policy areas in centrality order). Commits.

    Each (theme, theme_type) is written once. Without replace, nothing is written
    if the session already has tags (another writer got there first since the
    caller checked) and ALREADY_TAGGED is returned; with replace, its tags are
    swapped for these. Otherwise returns the number of rows written.
    """
    now = now or datetime.utcnow()
    _lock_session_tags(session_id)
    if replace:
        HansardSessionTheme.query.filter_by(session_id=session_id).delete(synchronize_session=False)
    elif HansardSessionTheme.query.filter_by(session_id=session_id).first():
        db.session.commit()      # end the transaction (and the lock)
        logger.warning("Tagger: session %d was tagged by another run meanwhile — not written", session_id)
        return ALREADY_TAGGED
    count = 0
    seen: set[tuple[str, str]] = set()
    for theme, theme_type in ([(a, THEME_TYPE_POLICY_AREA) for a in outcome.policy_areas]
                              + [(t, THEME_TYPE_SPECIFIC) for t in outcome.topics]):
        if (theme, theme_type) in seen:
            continue
        seen.add((theme, theme_type))
        db.session.add(HansardSessionTheme(session_id=session_id, theme=theme, theme_type=theme_type,
                                           tagged_at=now, model_used=outcome.model))
        count += 1
    if count or replace:
        db.session.commit()
    else:
        db.session.rollback()    # release the lock
    return count


def write_pq_tags(pq_id: int, outcome: TagOutcome, replace: bool = False,
                  now: Optional[datetime] = None) -> int:
    """
    Store verified tags for a Written Question. Commits. ha_pq_theme has no row
    id, so centrality order is kept in tagged_at: each row is 1 ms after the
    previous one, most central policy area first (views order by tagged_at).
    """
    from datetime import timedelta
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
                                   tagged_at=now + timedelta(milliseconds=count),
                                   model_used=outcome.model))
        count += 1
    if count or replace:
        db.session.commit()
    return count


# ---------------------------------------------------------------------------
# Public entry points (crons)
# ---------------------------------------------------------------------------

# Stop a run after this many API failures in a row and report it as errors,
# so the cron's run record is "failed" and the alert email goes out.
MAX_CONSECUTIVE_API_FAILURES = int(os.environ.get("TAGGER_MAX_API_FAILURES", "5"))
# Give up on an item after this many attempts that verified no policy area.
TAG_MAX_ATTEMPTS = int(os.environ.get("TAGGER_MAX_ATTEMPTS", "3"))


def _api_key(api_key: Optional[str]) -> Optional[str]:
    if api_key:
        return api_key
    try:
        from flask import current_app
        return current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    except RuntimeError:
        return os.environ.get("GEMINI_API_KEY")


# --- attempt tracking (ha_tag_attempt; optional until the migration has run) ---

def _attempts_table_ready() -> bool:
    try:
        import sqlalchemy as sa
        return sa.inspect(db.engine).has_table("ha_tag_attempt")
    except Exception:  # noqa: BLE001
        return False


def _given_up_ids(kind: str):
    """Subquery of item ids that reached TAG_MAX_ATTEMPTS, or None without the table."""
    if not _attempts_table_ready():
        return None
    from hansard_archive.models import HaTagAttempt
    return (db.session.query(HaTagAttempt.item_id)
            .filter(HaTagAttempt.item_kind == kind, HaTagAttempt.attempts >= TAG_MAX_ATTEMPTS))


def given_up_count(kind: str) -> Optional[int]:
    """How many items of this kind the tagger has stopped trying (None without the table)."""
    q = _given_up_ids(kind)
    return q.count() if q is not None else None


def record_failed_attempt(kind: str, item_id: int, reason: str) -> None:
    """Count one attempt that verified no policy area. Never raises."""
    if not _attempts_table_ready():
        return
    from hansard_archive.models import HaTagAttempt
    try:
        row = db.session.get(HaTagAttempt, (kind, item_id))
        if row is None:
            row = HaTagAttempt(item_kind=kind, item_id=item_id, attempts=0)
            db.session.add(row)
        row.attempts = (row.attempts or 0) + 1
        row.last_attempt_at = datetime.utcnow()
        row.last_reason = (reason or "")[:300]
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        logger.warning("Tagger: could not record attempt for %s %s: %s", kind, item_id, e)


def _failure_reason(outcome: TagOutcome) -> str:
    if outcome.empty_reason:
        return outcome.empty_reason
    if not outcome.proposed_areas:
        return "model proposed no policy area"
    reasons = Counter(why for kind, _, why in outcome.dropped if kind == "policy_area")
    return "policy areas dropped: " + ", ".join(f"{why} x{n}" for why, n in reasons.items())


def tag_session(session_id: int, api_key: Optional[str] = None, model: Optional[str] = None) -> int:
    """
    Tag a single session. Skips if already tagged.
    Returns number of theme rows written (0 if skipped or failed), or
    ALREADY_TAGGED if another run tagged it while the model was answering.
    Raises GeminiCallFailed when the API call fails (so callers can count it).
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

    outcome = tag_session_input(si, api_key, model)
    if not outcome.ok:
        reason = _failure_reason(outcome)
        logger.warning("Tagger: session %d — nothing writable (%s)", session_id, reason)
        record_failed_attempt("session", session_id, reason)
        return 0
    return write_session_tags(session_id, outcome)


def tag_all_untagged(
    limit: Optional[int] = None,
    api_key: Optional[str] = None,
    verbose: bool = True,
    skip_types: tuple = ("other",),
) -> dict:
    """
    Batch tag untagged non-container sessions, newest first.

    Excludes containers, debate_type IN skip_types (default 'other'), and
    sessions that already had TAG_MAX_ATTEMPTS attempts with nothing writable.
    `limit` caps the run (crons pass a cap so a large backlog — e.g. after a
    reclassification — is worked through over several runs).

    Returns {total_eligible, tagged, failed, errors, api_errors, aborted,
    given_up, already_tagged}. already_tagged: another run tagged the session
    meanwhile (not a failure). API failures count as errors; after
    MAX_CONSECUTIVE_API_FAILURES in a row the run stops and aborted holds the
    reason. Expects to run inside a Flask app context.
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
    given_up = _given_up_ids("session")
    if given_up is not None:
        query = query.filter(~HansardSession.id.in_(given_up.subquery()))
    if limit:
        query = query.limit(limit)

    sessions = query.all()
    total_eligible = len(sessions)
    if verbose:
        print(f"[tagger] {total_eligible} sessions to tag (limit={limit})", flush=True)

    tagged = failed = errors = api_errors = consecutive = already_tagged = 0
    aborted = ""
    for i, session in enumerate(sessions):
        try:
            time.sleep(_INTER_REQUEST_DELAY)
            count = tag_session(session.id, api_key=api_key)
            consecutive = 0
            if count > 0:
                tagged += 1
                if verbose:
                    print(f"[tagger] [{i+1}/{total_eligible}] + {session.title[:60]!r} — {count} tags",
                          flush=True)
            elif count == ALREADY_TAGGED:
                already_tagged += 1
                if verbose:
                    print(f"[tagger] [{i+1}/{total_eligible}] already tagged by another run "
                          f"{session.title[:60]!r}", flush=True)
            else:
                failed += 1
                if verbose:
                    print(f"[tagger] [{i+1}/{total_eligible}] FAIL {session.title[:60]!r}", flush=True)
        except GeminiCallFailed as e:
            errors += 1
            api_errors += 1
            consecutive += 1
            print(f"[tagger] API ERROR session {session.id}: {e}", flush=True)
            if e.final or consecutive >= MAX_CONSECUTIVE_API_FAILURES:
                aborted = (f"stopped after {consecutive} consecutive API failure(s): {e}"
                           if not e.final else f"stopped: {e}")
                print(f"[tagger] {aborted}", flush=True)
                break
        except Exception as e:
            errors += 1
            db.session.rollback()
            logger.error("Tagger: unexpected error on session %d: %s", session.id, e)
            if verbose:
                print(f"[tagger] ERROR session {session.id}: {e}", flush=True)

    n_given_up = given_up_count("session")
    if verbose and n_given_up:
        print(f"[tagger] {n_given_up} session(s) skipped after {TAG_MAX_ATTEMPTS} attempts "
              "with nothing writable", flush=True)
    return {"total_eligible": total_eligible, "tagged": tagged, "failed": failed, "errors": errors,
            "api_errors": api_errors, "aborted": aborted, "given_up": n_given_up,
            "already_tagged": already_tagged}


def tag_pq(pq_id: int, api_key: Optional[str] = None, model: Optional[str] = None) -> int:
    """
    Tag a single Written Question. Skips if already tagged.
    Returns number of theme rows written (0 if skipped or failed).
    Raises GeminiCallFailed when the API call fails.
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
    outcome = tag_pq_input(pq_input_from_db(pq), api_key, model)
    if not outcome.ok:
        record_failed_attempt("pq", pq_id, _failure_reason(outcome))
        return 0
    return write_pq_tags(pq_id, outcome)


def tag_pq_all_untagged(
    limit: Optional[int] = None,
    api_key: Optional[str] = None,
    verbose: bool = True,
) -> dict:
    """
    Batch tag untagged Written Questions (batches of 5, mapped by position),
    skipping questions that already had TAG_MAX_ATTEMPTS attempts with nothing
    writable.

    Returns {total_eligible, tagged, failed, retried, errors, api_errors,
    aborted, given_up}. Questions with no result after retries count as API
    errors; the run stops after MAX_CONSECUTIVE_API_FAILURES of them in a row,
    or at once on a final API error (bad request / key / model).
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
    given_up = _given_up_ids("pq")
    if given_up is not None:
        query = query.filter(~HaPQ.id.in_(given_up.subquery()))
    if limit:
        query = query.limit(limit)
    pqs = [pq for pq in query.all() if (pq.question_text or "").strip()]
    total_eligible = len(pqs)
    if verbose:
        print(f"[pq-tagger] {total_eligible} PQs to tag in batches of {_PQ_BATCH_SIZE} "
              f"(limit={limit}, batch_delay={_PQ_BATCH_DELAY}s)", flush=True)

    tagged = failed = errors = retried = api_errors = consecutive = 0
    aborted = ""
    chunk = _PQ_BATCH_SIZE * 10
    for start in range(0, total_eligible, chunk):
        part = pqs[start:start + chunk]
        try:
            outcomes = tag_pq_inputs([pq_input_from_db(pq) for pq in part], api_key)
        except GeminiCallFailed as e:          # final: retrying cannot help
            errors += len(part)
            api_errors += len(part)
            aborted = f"stopped: {e}"
            print(f"[pq-tagger] {aborted}", flush=True)
            break
        for pq, outcome in zip(part, outcomes):
            if outcome is None:
                errors += 1
                api_errors += 1
                consecutive += 1
                continue
            consecutive = 0
            retried += bool(outcome.retried)
            if not outcome.ok:
                failed += 1
                record_failed_attempt("pq", pq.id, _failure_reason(outcome))
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
        if consecutive >= MAX_CONSECUTIVE_API_FAILURES:
            aborted = f"stopped after {consecutive} consecutive questions with no API result"
            print(f"[pq-tagger] {aborted}", flush=True)
            break

    n_given_up = given_up_count("pq")
    if verbose and n_given_up:
        print(f"[pq-tagger] {n_given_up} PQ(s) skipped after {TAG_MAX_ATTEMPTS} attempts "
              "with nothing writable", flush=True)
    return {"total_eligible": total_eligible, "tagged": tagged, "failed": failed,
            "retried": retried, "errors": errors, "api_errors": api_errors,
            "aborted": aborted, "given_up": n_given_up}
