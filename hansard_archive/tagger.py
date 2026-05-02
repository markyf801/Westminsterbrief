"""
Hansard Archive — Phase 2A Week 2 theme tagging.

Entry points:
  tag_session(session_id) -> int          tag one session, return rows written
  tag_all_untagged(limit=None) -> dict    batch tag all untagged non-container sessions
  tag_pq_all_untagged(limit=None) -> dict batch tag all untagged Written Questions

Tags each session with two theme types (separate ha_session_theme rows):
  policy_area  — 1+ terms from the 23-term GOV.UK policy taxonomy (controlled enum)
  specific     — 1-5 free-text topic phrases (e.g. "student loan repayments")

Uses Gemini Flash-Lite with JSON schema enforcement (response_schema + enum) so
policy_area values are hard-constrained to the approved vocabulary.

Container sessions (is_container=True) and debate_type='other' sessions are
excluded from the initial tagging run.
"""

import json
import logging
import os
import re
import time
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

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_INTER_REQUEST_DELAY = 0.4   # between single-PQ fallback calls
_MAX_RETRIES = 2

_PQ_BATCH_SIZE = 10
_PQ_BATCH_DELAY = 0.5   # between batch calls — paid tier

# ---------------------------------------------------------------------------
# GOV.UK policy area taxonomy (verified April 2026 against gov.uk/search/policy-papers)
# Three parliamentary additions: Energy, Parliament and constitution, Trade
# ---------------------------------------------------------------------------

POLICY_AREAS = [
    "Business and industry",
    "Children and families",
    "Crime, justice and law",
    "Defence and armed forces",
    "Economy",
    "Education, training and skills",
    "Employment and labour market",
    "Energy",
    "Environment",
    "Finance and taxation",
    "Foreign affairs and diplomacy",
    "Government and public administration",
    "Health and social care",
    "Housing and planning",
    "Immigration and borders",
    "International development",
    "Local government",
    "Parliament and constitution",
    "Science and technology",
    "Society and culture",
    "Trade",
    "Transport",
    "Welfare and benefits",
]

# ---------------------------------------------------------------------------
# Gemini client — structured output variant
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict[str, str] = {}

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "policy_areas": {
            "type": "ARRAY",
            "description": "One or more GOV.UK policy taxonomy terms that best describe the main subjects of this session. Include a term only if that policy area is substantively addressed — a passing mention does not qualify.",
            "items": {
                "type": "STRING",
                "enum": POLICY_AREAS,
            },
        },
        "themes": {
            "type": "ARRAY",
            "description": "One to five specific policy topic phrases describing what this session is actually about. Each phrase should be 2-5 words, lowercase, concrete and search-useful (e.g. 'student loan repayments', 'nhs waiting times', 'chalk stream pollution').",
            "items": {"type": "STRING"},
            "maxItems": 5,
        },
    },
    "required": ["policy_areas", "themes"],
}

_BATCH_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "uin": {"type": "STRING"},
            "policy_areas": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": POLICY_AREAS},
            },
            "themes": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "maxItems": 5,
            },
        },
        "required": ["uin", "policy_areas", "themes"],
    },
}

_PQ_BATCH_PROMPT_TEMPLATE = """\
You are tagging UK Written Parliamentary Questions for a parliamentary intelligence \
archive. Tag EACH of the {n} questions below.

For each question return:
1. uin — the UIN exactly as provided
2. policy_areas — 1+ terms from the allowed enum. Include only if SUBSTANTIVELY addressed.
3. themes — 1 to 5 specific topic phrases (2-5 words, lowercase, e.g. \
"student loan repayments", "nhs waiting times"). Do not repeat policy_area names.

All {n} questions must appear in the response, identified by their UIN.

{questions_block}"""


_PROMPT_TEMPLATE = """\
You are tagging UK parliamentary Hansard debate transcripts for a parliamentary \
intelligence archive used by civil servants, policy professionals, and researchers.

For the session below, return:
1. policy_areas — choose from the allowed enum values only. Include a term only if \
that policy area is SUBSTANTIVELY addressed (not just mentioned in passing). Minimum 1, \
no hard maximum.
2. themes — 1 to 5 specific policy topic phrases (2-5 words each, lowercase). These \
should describe what the session is concretely about (e.g. "student loan repayments", \
"chalk stream pollution", "nhs waiting times"). Do not repeat the policy_area names as themes.

Session title: {title}
Debate type: {debate_type}
Date: {date}
House: {house}

Transcript excerpt (first ~1200 words):
{text_excerpt}"""


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
    _MODEL_CACHE[api_key] = "gemini-2.5-flash-lite"
    return _MODEL_CACHE[api_key]


def _gemini_tag(api_key: str, prompt: str) -> Optional[dict]:
    """
    Call Gemini with JSON schema enforcement. Returns parsed dict or None on failure.
    Schema guarantees policy_areas values are from POLICY_AREAS enum.
    """
    model = _detect_model(api_key)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }

    for attempt in range(_MAX_RETRIES + 1):
        for version in ("v1beta", "v1"):
            url = (
                f"https://generativelanguage.googleapis.com/{version}/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                r = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 200:
                    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    # Schema enforcement means this should always parse cleanly
                    parsed = json.loads(raw)
                    # Defensive: validate policy_areas against enum in case of schema bypass
                    valid_areas = set(POLICY_AREAS)
                    filtered = [a for a in parsed.get("policy_areas", []) if a in valid_areas]
                    invalid = [a for a in parsed.get("policy_areas", []) if a not in valid_areas]
                    if invalid:
                        logger.warning("Tagger: invalid policy_areas returned (schema bypass): %s", invalid)
                    parsed["policy_areas"] = filtered
                    return parsed
                if r.status_code in (429,):
                    # Rate limited — don't retry immediately
                    logger.warning("Tagger: rate limited (429), backing off")
                    time.sleep(5 * (attempt + 1))
                    break
                if r.status_code in (401, 403):
                    logger.error("Tagger: auth error %d — check GEMINI_API_KEY", r.status_code)
                    return None
            except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
                logger.warning("Tagger: attempt %d/%d failed: %s", attempt + 1, _MAX_RETRIES + 1, e)
        if attempt < _MAX_RETRIES:
            time.sleep(2 ** attempt)

    return None


def _gemini_tag_batch(api_key: str, prompt: str) -> Optional[list]:
    """
    Call Gemini for a batch of PQs. Returns a list of dicts or None on total failure.
    Each dict has keys: uin, policy_areas, themes (schema-enforced).
    Caller is responsible for checking which UINs are present in the response.
    """
    model = _detect_model(api_key)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _BATCH_RESPONSE_SCHEMA,
        },
    }

    for attempt in range(_MAX_RETRIES + 1):
        for version in ("v1beta", "v1"):
            url = (
                f"https://generativelanguage.googleapis.com/{version}/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                r = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 200:
                    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return parsed
                    return None
                if r.status_code == 429:
                    logger.warning("Tagger batch: rate limited (429), backing off")
                    time.sleep(10 * (attempt + 1))
                    break
                if r.status_code in (401, 403):
                    logger.error("Tagger batch: auth error %d — check GEMINI_API_KEY", r.status_code)
                    return None
            except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
                logger.warning("Tagger batch: attempt %d/%d failed: %s", attempt + 1, _MAX_RETRIES + 1, e)
        if attempt < _MAX_RETRIES:
            time.sleep(2 ** attempt)

    return None


# ---------------------------------------------------------------------------
# Session text extraction
# ---------------------------------------------------------------------------

def _get_session_text(session_id: int, max_chars: int = 4000) -> str:
    """Concatenate contribution speech_text for a session, ordered by speech_order."""
    from sqlalchemy import text
    rows = db.session.execute(
        text("""
            SELECT speech_text FROM ha_contribution
            WHERE session_id = :sid
            ORDER BY speech_order
        """),
        {"sid": session_id},
    ).fetchall()
    combined = " ".join(r[0] for r in rows if r[0])
    return combined[:max_chars]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def tag_session(session_id: int, api_key: Optional[str] = None) -> int:
    """
    Tag a single session. Skips if already tagged.
    Returns number of theme rows written (0 if skipped or failed).
    Expects to run inside a Flask app context with an active DB session.
    """
    if api_key is None:
        from flask import current_app
        api_key = current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("Tagger: GEMINI_API_KEY not set")
        return 0

    session = HansardSession.query.get(session_id)
    if not session:
        return 0

    # Skip if already tagged
    existing = HansardSessionTheme.query.filter_by(session_id=session_id).first()
    if existing:
        return 0

    text_excerpt = _get_session_text(session_id, max_chars=4000)
    if not text_excerpt.strip():
        logger.warning("Tagger: session %d has no contribution text — skipping", session_id)
        return 0

    prompt = _PROMPT_TEMPLATE.format(
        title=session.title,
        debate_type=session.debate_type or "unknown",
        date=session.date.isoformat() if session.date else "unknown",
        house=session.house or "Commons",
        text_excerpt=text_excerpt,
    )

    result = _gemini_tag(api_key, prompt)
    if not result:
        logger.warning("Tagger: session %d (%r) — Gemini returned None", session_id, session.title[:50])
        return 0

    model_name = _MODEL_CACHE.get(api_key, "gemini-2.5-flash-lite")
    now = datetime.utcnow()
    count = 0

    for area in result.get("policy_areas", []):
        if area:
            db.session.add(HansardSessionTheme(
                session_id=session_id,
                theme=area,
                theme_type=THEME_TYPE_POLICY_AREA,
                tagged_at=now,
                model_used=model_name,
            ))
            count += 1

    for theme in result.get("themes", [])[:5]:
        if theme:
            db.session.add(HansardSessionTheme(
                session_id=session_id,
                theme=theme.lower().strip(),
                theme_type=THEME_TYPE_SPECIFIC,
                tagged_at=now,
                model_used=model_name,
            ))
            count += 1

    if count > 0:
        db.session.commit()

    return count


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

    Returns summary dict: {total_eligible, tagged, skipped_existing, failed, errors}.
    Expects to run inside a Flask app context.
    """
    if api_key is None:
        from flask import current_app
        api_key = current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    # Find all eligible untagged sessions
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

    tagged = 0
    failed = 0
    errors = 0

    for i, session in enumerate(sessions):
        try:
            time.sleep(_INTER_REQUEST_DELAY)
            count = tag_session(session.id, api_key=api_key)
            if count > 0:
                tagged += 1
                if verbose:
                    print(
                        f"[tagger] [{i+1}/{total_eligible}] + {session.title[:60]!r} — {count} tags",
                        flush=True,
                    )
            else:
                failed += 1
                if verbose:
                    print(
                        f"[tagger] [{i+1}/{total_eligible}] FAIL {session.title[:60]!r}",
                        flush=True,
                    )
        except Exception as e:
            errors += 1
            logger.error("Tagger: unexpected error on session %d: %s", session.id, e)
            if verbose:
                print(f"[tagger] ERROR session {session.id}: {e}", flush=True)

    return {
        "total_eligible": total_eligible,
        "tagged": tagged,
        "failed": failed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# PQ tagging
# ---------------------------------------------------------------------------

_PQ_PROMPT_TEMPLATE = """\
You are tagging UK Written Parliamentary Questions for a parliamentary intelligence \
archive used by civil servants, policy professionals, and researchers.

For the question below, return:
1. policy_areas — choose from the allowed enum values only. Include a term only if \
that policy area is SUBSTANTIVELY addressed. Minimum 1, no hard maximum.
2. themes — 1 to 5 specific policy topic phrases (2-5 words each, lowercase). These \
should describe what the question is concretely about (e.g. "student loan repayments", \
"nhs waiting times", "asylum seeker housing"). Do not repeat the policy_area names as themes.

Heading: {heading}
Answering department: {answering_body}
Chamber: {chamber}
Date tabled: {tabled_date}

Question:
{question_text}

{answer_block}"""


def _write_pq_tags(pq: "HaPQ", result: dict, model_name: str, now: "datetime") -> int:
    """Write theme rows for a single PQ from a parsed Gemini result dict. Returns row count."""
    valid_areas = set(POLICY_AREAS)
    count = 0
    for area in result.get("policy_areas", []):
        if area and area in valid_areas:
            db.session.merge(HaPQTheme(
                pq_id=pq.id,
                theme=area,
                theme_type=THEME_TYPE_POLICY_AREA,
                tagged_at=now,
                model_used=model_name,
            ))
            count += 1
    for theme in result.get("themes", [])[:5]:
        if theme:
            db.session.merge(HaPQTheme(
                pq_id=pq.id,
                theme=theme.lower().strip(),
                theme_type=THEME_TYPE_SPECIFIC,
                tagged_at=now,
                model_used=model_name,
            ))
            count += 1
    if count > 0:
        db.session.commit()
    return count


def tag_pq(pq_id: int, api_key: Optional[str] = None) -> int:
    """
    Tag a single Written Question. Skips if already tagged.
    Returns number of theme rows written (0 if skipped or failed).
    Expects to run inside a Flask app context with an active DB session.
    """
    if api_key is None:
        from flask import current_app
        api_key = current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("Tagger: GEMINI_API_KEY not set")
        return 0

    pq = db.session.get(HaPQ, pq_id)
    if not pq:
        return 0

    existing = HaPQTheme.query.filter_by(pq_id=pq_id).first()
    if existing:
        return 0

    question_text = (pq.question_text or "")[:800]
    if not question_text.strip():
        logger.warning("Tagger: PQ %d has no question text — skipping", pq_id)
        return 0

    answer_block = ""
    if pq.answer_text:
        answer_block = f"Answer:\n{pq.answer_text[:400]}"

    prompt = _PQ_PROMPT_TEMPLATE.format(
        heading=pq.heading or "(no heading)",
        answering_body=pq.answering_body or "unknown",
        chamber=pq.chamber or "Commons",
        tabled_date=pq.tabled_date.isoformat() if pq.tabled_date else "unknown",
        question_text=question_text,
        answer_block=answer_block,
    )

    result = _gemini_tag(api_key, prompt)
    if not result:
        logger.warning("Tagger: PQ %d (%r) — Gemini returned None", pq_id, (pq.heading or pq.uin)[:50])
        return 0

    model_name = _MODEL_CACHE.get(api_key, "gemini-2.5-flash-lite")
    now = datetime.utcnow()
    count = 0

    for area in result.get("policy_areas", []):
        if area and area in set(POLICY_AREAS):
            db.session.merge(HaPQTheme(
                pq_id=pq_id,
                theme=area,
                theme_type=THEME_TYPE_POLICY_AREA,
                tagged_at=now,
                model_used=model_name,
            ))
            count += 1

    for theme in result.get("themes", [])[:5]:
        if theme:
            db.session.merge(HaPQTheme(
                pq_id=pq_id,
                theme=theme.lower().strip(),
                theme_type=THEME_TYPE_SPECIFIC,
                tagged_at=now,
                model_used=model_name,
            ))
            count += 1

    if count > 0:
        db.session.commit()

    return count


def tag_pq_all_untagged(
    limit: Optional[int] = None,
    api_key: Optional[str] = None,
    verbose: bool = True,
) -> dict:
    """
    Batch tag all untagged Written Questions.

    Returns summary dict: {total_eligible, tagged, failed, errors}.
    Expects to run inside a Flask app context.
    """
    if api_key is None:
        from flask import current_app
        api_key = current_app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    already_tagged_subq = db.session.query(HaPQTheme.pq_id).distinct().subquery()
    query = (
        HaPQ.query
        .filter(~HaPQ.id.in_(already_tagged_subq))
        .order_by(HaPQ.tabled_date.desc())
    )
    if limit:
        query = query.limit(limit)

    pqs = query.all()
    total_eligible = len(pqs)

    if verbose:
        print(
            f"[pq-tagger] {total_eligible} PQs to tag in batches of {_PQ_BATCH_SIZE} "
            f"(limit={limit}, batch_delay={_PQ_BATCH_DELAY}s)",
            flush=True,
        )

    tagged = 0
    failed = 0
    errors = 0
    retried = 0
    model_name = _MODEL_CACHE.get(api_key, "gemini-2.5-flash-lite")

    # Process in batches
    batches = [pqs[i:i + _PQ_BATCH_SIZE] for i in range(0, len(pqs), _PQ_BATCH_SIZE)]

    for batch_num, batch in enumerate(batches):
        time.sleep(_PQ_BATCH_DELAY)
        now = datetime.utcnow()

        # Build batch prompt — short excerpts to keep prompt size manageable
        questions_block = ""
        for j, pq in enumerate(batch):
            q_text = (pq.question_text or "")[:300]
            a_snippet = f"\nAnswer: {pq.answer_text[:150]}" if pq.answer_text else ""
            questions_block += (
                f"[{j+1}] UIN: {pq.uin}\n"
                f"Heading: {pq.heading or '(none)'}\n"
                f"Dept: {pq.answering_body or 'unknown'}\n"
                f"Question: {q_text}{a_snippet}\n\n"
            )

        prompt = _PQ_BATCH_PROMPT_TEMPLATE.format(
            n=len(batch), questions_block=questions_block
        )

        batch_result = _gemini_tag_batch(api_key, prompt)
        processed = batch_num * _PQ_BATCH_SIZE

        if batch_result is not None:
            # Index by UIN — strip whitespace in case model adds spaces
            result_by_uin = {r.get("uin", "").strip(): r for r in batch_result}

            for pq in batch:
                result = result_by_uin.get(pq.uin)
                if result:
                    try:
                        count = _write_pq_tags(pq, result, model_name, now)
                        if count > 0:
                            tagged += 1
                            if verbose:
                                print(
                                    f"[pq-tagger] [{processed + batch.index(pq) + 1}/{total_eligible}]"
                                    f" + {pq.uin} {(pq.heading or '')[:50]!r} — {count} tags",
                                    flush=True,
                                )
                        else:
                            failed += 1
                    except Exception as e:
                        errors += 1
                        logger.error("Tagger: write error on PQ %d: %s", pq.id, e)
                else:
                    # UIN missing from batch response — retry individually
                    logger.warning("Tagger batch: UIN %s missing from response, retrying individually", pq.uin)
                    retried += 1
                    try:
                        time.sleep(_INTER_REQUEST_DELAY)
                        count = tag_pq(pq.id, api_key=api_key)
                        if count > 0:
                            tagged += 1
                        else:
                            failed += 1
                    except Exception as e:
                        errors += 1
                        logger.error("Tagger: individual retry error on PQ %d: %s", pq.id, e)
        else:
            # Whole batch failed — fall back to individual calls for each PQ
            logger.warning("Tagger batch %d failed entirely, falling back to individual calls", batch_num + 1)
            if verbose:
                print(f"[pq-tagger] Batch {batch_num + 1} failed — retrying {len(batch)} PQs individually", flush=True)
            for pq in batch:
                try:
                    time.sleep(_INTER_REQUEST_DELAY)
                    count = tag_pq(pq.id, api_key=api_key)
                    if count > 0:
                        tagged += 1
                        retried += 1
                    else:
                        failed += 1
                except Exception as e:
                    errors += 1
                    logger.error("Tagger: individual fallback error on PQ %d: %s", pq.id, e)

        if verbose and (batch_num + 1) % 10 == 0:
            print(
                f"[pq-tagger] Progress: {min(processed + _PQ_BATCH_SIZE, total_eligible)}/{total_eligible}"
                f" — tagged={tagged} failed={failed} retried={retried}",
                flush=True,
            )

    return {
        "total_eligible": total_eligible,
        "tagged": tagged,
        "failed": failed,
        "retried": retried,
        "errors": errors,
    }
