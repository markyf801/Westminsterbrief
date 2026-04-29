"""
Hansard Archive — Phase 2A Week 2 theme tagging.

Entry points:
  tag_session(session_id) -> int          tag one session, return rows written
  tag_all_untagged(limit=None) -> dict    batch tag all untagged non-container sessions

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
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_INTER_REQUEST_DELAY = 0.4   # be a good citizen
_MAX_RETRIES = 2

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
