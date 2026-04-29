"""
Phase 2A Week 2 prep — sample tagging quality test.

Tags 56 varied sessions with Gemini Flash-Lite and writes results to
data/tagging_results.json for manual quality review.

Usage:
  python scripts/sample_tagging_test.py
"""

import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, ".")
from flask_app import app

GEMINI_KEY = app.config.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
_MODEL_CACHE = {}

PROMPT = """\
You are analysing UK parliamentary Hansard debate transcripts for a parliamentary \
intelligence tool used by civil servants, policy professionals, and researchers.

For the session below, identify 1-3 specific policy themes discussed. Each theme should be:
- A noun phrase of 2-5 words
- Specific enough to be useful for search (not just "government policy" or "legislation")
- Using standard UK parliamentary/policy terminology, lowercase
- Concrete rather than procedural (do not return themes like "parliamentary procedure" \
or "house business")

Return ONLY a JSON array of strings. Examples:
["higher education funding", "student loan repayments"]
["nhs workforce", "nurse recruitment"]

Session title: {title}
Debate type: {debate_type}
Date: {date}

Transcript excerpt:
{text_excerpt}

Return only the JSON array, no other text."""


def _gemini_generate(prompt: str) -> str | None:
    api_key = GEMINI_KEY
    if api_key not in _MODEL_CACHE:
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
                        break
                else:
                    _MODEL_CACHE[api_key] = "gemini-2.5-flash-lite"
        except Exception:
            _MODEL_CACHE[api_key] = "gemini-2.5-flash-lite"

    model = _MODEL_CACHE.get(api_key, "gemini-2.5-flash-lite")
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    for version in ("v1", "v1beta"):
        url = (
            f"https://generativelanguage.googleapis.com/{version}/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return None


def main() -> None:
    sample_path = "data/tagging_sample.json"
    if not os.path.exists(sample_path):
        print(f"ERROR: {sample_path} not found. Run the sample selection first.")
        sys.exit(1)

    with open(sample_path, encoding="utf-8") as f:
        sessions = json.load(f)

    # Warm model detection
    _gemini_generate("test")
    model = _MODEL_CACHE.get(GEMINI_KEY, "unknown")
    print(f"Model: {model}  |  Sessions: {len(sessions)}", flush=True)
    print()

    results = []
    errors = 0
    for i, s in enumerate(sessions):
        prompt = PROMPT.format(
            title=s["title"],
            debate_type=s["debate_type"],
            date=s["date"],
            text_excerpt=s["text_excerpt"][:1200],
        )
        raw = _gemini_generate(prompt)
        themes: list[str] = []
        if raw:
            try:
                clean = re.sub(r"^```json?\s*|\s*```$", "", raw.strip())
                themes = json.loads(clean)
                if not isinstance(themes, list):
                    themes = [str(themes)]
            except Exception:
                themes = ["PARSE_ERROR: " + raw[:80]]
                errors += 1
        else:
            themes = ["NO_RESPONSE"]
            errors += 1

        results.append(
            {
                "id": s["id"],
                "title": s["title"],
                "date": s["date"],
                "debate_type": s["debate_type"],
                "themes": themes,
            }
        )
        print(
            f"[{i+1:02}/{len(sessions)}] {s['debate_type']:<22} {s['title'][:45]!r}",
            flush=True,
        )
        print(f"         => {themes}", flush=True)
        time.sleep(0.3)

    out_path = "data/tagging_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(results)} tagged, {errors} errors. Saved to {out_path}")


if __name__ == "__main__":
    main()
