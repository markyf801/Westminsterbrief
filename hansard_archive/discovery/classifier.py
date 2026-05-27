"""
LLM classifier for discovery candidates.

Uses Gemini Flash-Lite (matching the stats_refresh.py pattern) to determine
whether a candidate item is a genuine statistical publication and, if so,
to extract canonical name, description, update cadence, and subject area.

Never invoked for free-feature users — this is part of the stats-registry
data layer, not the public-facing toolkit.
"""
from __future__ import annotations

import json
import logging
import time

import requests

from hansard_archive.policy_areas import HANSARD_POLICY_NAMES

log = logging.getLogger("discovery.classifier")

_POLICY_AREA_LIST = "\n".join(f"  - {name}" for name in HANSARD_POLICY_NAMES)

_MODEL      = "gemini-2.5-flash-lite"
_GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
)
_TIMEOUT     = 60
_MAX_RETRIES = 3
_RETRY_BASE  = 2  # seconds; doubles each retry (1s, 2s, 4s)

# Prompt is a single call per candidate. Comments explain each section.
_PROMPT_TEMPLATE = """\
You are classifying a candidate item from a UK government statistics producer.

Determine whether this item is a genuine statistical publication — meaning a \
recurring or one-off release of data (e.g. an annual statistical bulletin, a \
data series, a census output). It is NOT a statistical publication if it is: \
a press release, a policy document, a navigation or index page, a corporate \
report, or a job posting.

# Producer context (helps you judge relevance)
Name:    {producer_name}
Type:    {producer_type}
Website: {web_root_url}

# Candidate item
Title:            {title}
URL:              {url}
Date hints:       {date_hints}
Description hint: {description_hint}

# Policy areas (controlled vocabulary — use ONLY these exact strings)
{policy_area_list}

# Instructions
Return a JSON object with this exact structure and no other text:
{{
  "is_publication": true or false,
  "reason": "brief explanation if false — omit this key if true",
  "name": "canonical publication name stripped of year/release suffixes",
  "description": "1-2 sentences describing what this publication covers",
  "update_cadence": one of the strings "daily","weekly","monthly","quarterly","annual","biennial","ad_hoc","one_off" — or JSON null (not the string "null") if the cadence is unknown or irregular,
  "subject_area": "free-text classification e.g. Higher Education, Crime Statistics",
  "policy_areas": ["Area 1", "Area 2"] — list of 1 to 4 strings from the controlled vocabulary above that best describe what this publication covers. Use ONLY the exact strings from the list. Return [] if none apply.
}}

If is_publication is false, name/description/update_cadence/subject_area/policy_areas may be omitted.
Return ONLY the JSON — no markdown, no code fences, no commentary."""

_REQUIRED_FIELDS = ("name", "description")


def classify_candidate(candidate: dict, producer, gemini_key: str) -> dict | None:
    """
    Classify a candidate item using Gemini Flash-Lite.

    Returns parsed JSON dict when the item is a publication, None otherwise.
    Retries up to _MAX_RETRIES times on transient errors (network, 5xx).
    Returns None (and logs) on invalid JSON, missing fields, or is_publication=false.
    """
    prompt = _PROMPT_TEMPLATE.format(
        producer_name=producer.name,
        producer_type=producer.producer_type,
        web_root_url=producer.web_root_url,
        title=candidate.get("title", ""),
        url=candidate.get("url", ""),
        date_hints=", ".join(candidate.get("date_hints", [])) or "unknown",
        description_hint=(candidate.get("description_hint") or "")[:500],
        policy_area_list=_POLICY_AREA_LIST,
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
        },
    }

    last_exc: Exception | None = None
    result: dict | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                _GEMINI_URL,
                params={"key": gemini_key},
                json=payload,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            raw  = resp.json()
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
            last_exc = None
            break
        except (requests.RequestException, KeyError, IndexError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BASE * (2 ** attempt))
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "classify_candidate: invalid JSON for %r — %s",
                candidate.get("title"), exc,
            )
            return None  # don't retry malformed JSON

    if last_exc is not None:
        log.warning(
            "classify_candidate: all %d attempts failed for %r — %s",
            _MAX_RETRIES, candidate.get("title"), last_exc,
        )
        return None

    if not result.get("is_publication"):
        log.debug(
            "classify_candidate: not a publication — %r: %s",
            candidate.get("title"), result.get("reason", "no reason"),
        )
        return None

    # Normalise null-like strings — Gemini sometimes returns "null" (string) instead
    # of JSON null, which violates the ha_stat_publication ck_stat_pub_cadence CHECK.
    if result.get("update_cadence") in ("null", "None", ""):
        result["update_cadence"] = None

    # Normalise policy_areas — keep only known controlled-vocab strings, cap at 4.
    raw_areas = result.get("policy_areas", [])
    if isinstance(raw_areas, list):
        _valid = set(HANSARD_POLICY_NAMES)
        result["policy_areas"] = [p for p in raw_areas if p in _valid][:4]
    else:
        result["policy_areas"] = []

    result["model_used"] = _MODEL

    for field_name in _REQUIRED_FIELDS:
        if not result.get(field_name):
            log.warning(
                "classify_candidate: missing required field %r for %r — skipping",
                field_name, candidate.get("title"),
            )
            return None

    return result
