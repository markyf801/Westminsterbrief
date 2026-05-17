"""
Manifesto ingestion script — Tier 3 party pages.

Usage:
    python scripts/ingest_manifesto.py --party labour --file labour_manifesto.txt --url https://labour.org.uk/change/
    python scripts/ingest_manifesto.py --party labour --file labour_manifesto.txt --url https://labour.org.uk/change/ --execute

Without --execute: dry-run only — shows what chunks would be created, does not write to DB.
With --execute: writes chunks as review_status='pending' to the DB.

The --url argument is the canonical URL for the manifesto source (used for attribution on party pages).
It is stored on every chunk. You can provide a section-level URL after review if you want finer-grained attribution.

Input file format:
    Plain text, one section per blank-line-separated block. Each block should be a coherent section
    from the manifesto (a paragraph or short sub-section). The script sends blocks to Gemini which
    chunks each block into 2-5 sentence excerpts and tags them to policy areas.

    You can also pass a single block at a time by piping stdin (omit --file).

Workflow:
    1. Extract manifesto text from PDF/HTML (manual step)
    2. Save as plain text file, one section per paragraph block
    3. Run this script in dry-run mode first to check output quality
    4. Run with --execute to write pending chunks to DB
    5. Review and approve chunks via /admin/manifesto-review

After --execute, chunks are stored with review_status='pending'. They do NOT appear on party pages
until approved via the admin review interface.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

# ---------------------------------------------------------------------------
# Policy area vocabulary — must match hansard_archive/tagger.py exactly
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

VALID_SLUGS = {
    "labour", "conservative", "liberal-democrat", "scottish-national-party",
    "reform-uk", "green", "plaid-cymru", "democratic-unionist-party",
}

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_REQUEST_TIMEOUT = 60
_INTER_REQUEST_DELAY = 0.5
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
                m["name"] for m in resp.json().get("models", [])
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

# ---------------------------------------------------------------------------
# Response schema for Gemini structured output
# ---------------------------------------------------------------------------

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "chunks": {
            "type": "ARRAY",
            "description": "List of manifesto chunks extracted from this section.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "chunk_text": {
                        "type": "STRING",
                        "description": "A verbatim excerpt of 2-5 consecutive sentences from the section that substantively addresses one or more policy areas. Do not paraphrase — copy the text exactly.",
                    },
                    "policy_areas": {
                        "type": "ARRAY",
                        "description": "One to three GOV.UK policy taxonomy terms that this chunk substantively addresses. List the best-fit area first.",
                        "items": {
                            "type": "STRING",
                            "enum": POLICY_AREAS,
                        },
                        "minItems": 1,
                        "maxItems": 3,
                    },
                },
                "required": ["chunk_text", "policy_areas"],
            },
        },
    },
    "required": ["chunks"],
}

_SYSTEM_PROMPT = """You are a parliamentary research assistant helping to index party manifestos.

Your task: given a section of a party manifesto, extract verbatim excerpts (chunks) that substantively address GOV.UK policy areas.

Rules:
- Extract 2-5 consecutive sentences per chunk that form a coherent policy position
- Copy the text EXACTLY as written — do not paraphrase or summarise
- Only include chunks that genuinely address a specific policy area in depth
- Skip introductory passages, vague aspirations, and content that does not map to a specific policy area
- A single section may yield 0-4 chunks depending on how much substantive policy content it contains
- Tag each chunk to 1-3 policy areas. List the most specific/best-fit area first (is_primary)
- If the section contains no substantive policy content, return an empty chunks array"""


def _extract_pdf(path: str) -> str:
    """Extract text from a PDF using pdfplumber. Returns plain text."""
    try:
        import pdfplumber
    except ImportError:
        print("Error: pdfplumber is not installed. Run: pip install pdfplumber")
        sys.exit(1)

    pages = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        print(f"Extracting text from {total} pages…")
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text(x_tolerance=2, y_tolerance=2)
            if text:
                pages.append(text.strip())
            if i % 20 == 0:
                print(f"  {i}/{total} pages done")

    raw = "\n\n".join(pages)
    print(f"Extracted {len(raw):,} characters from {len(pages)} pages.")
    return raw


def _call_gemini(api_key: str, section_text: str, party_name: str) -> list[dict]:
    """Call Gemini Flash-Lite and return list of {chunk_text, policy_areas} dicts."""
    model = _detect_model(api_key)
    prompt = f"""Party: {party_name}
Manifesto section:

{section_text}

Extract verbatim policy chunks from this section and tag each to GOV.UK policy areas."""

    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            "temperature": 0.1,
        },
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return parsed.get("chunks", [])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"  [WARN] Could not parse Gemini response: {e}")
        return []


def _write_chunks(party_slug: str, source_url: str, section_title: str,
                  chunks: list[dict], execute: bool) -> int:
    """Write approved chunks to DB as 'pending'. Returns count written."""
    from flask_app import app
    from hansard_archive.models import ManifestoChunk, ManifestoChunkTag
    from extensions import db

    written = 0
    with app.app_context():
        for chunk_data in chunks:
            text = chunk_data.get("chunk_text", "").strip()
            areas = chunk_data.get("policy_areas", [])
            if not text or not areas:
                continue

            if execute:
                existing = ManifestoChunk.query.filter_by(
                    party_slug=party_slug,
                    manifesto_year=2024,
                    chunk_text=text,
                ).first()
                if existing:
                    print(f"  [SKIP] Already exists: {text[:60]}…")
                    continue

                chunk = ManifestoChunk(
                    party_slug=party_slug,
                    manifesto_year=2024,
                    source_section=section_title or None,
                    source_url=source_url or None,
                    chunk_text=text,
                    ingested_at=datetime.utcnow(),
                    review_status="pending",
                )
                db.session.add(chunk)
                db.session.flush()

                for i, area in enumerate(areas):
                    if area in POLICY_AREAS:
                        tag = ManifestoChunkTag(
                            chunk_id=chunk.id,
                            policy_area=area,
                            is_primary=(i == 0),
                        )
                        db.session.add(tag)

                written += 1

        if execute:
            db.session.commit()

    return written


def main():
    parser = argparse.ArgumentParser(description="Ingest party manifesto chunks into Westminster Brief.")
    parser.add_argument("--party",   required=True, help="Party slug (e.g. labour, conservative)")
    parser.add_argument("--file",    required=False, help="Path to plain-text manifesto file")
    parser.add_argument("--url",     required=True, help="Canonical source URL for this manifesto")
    parser.add_argument("--execute", action="store_true", help="Write to DB (default: dry run)")
    args = parser.parse_args()

    if args.party not in VALID_SLUGS:
        print(f"Error: unknown party slug '{args.party}'. Valid: {', '.join(sorted(VALID_SLUGS))}")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY_PROD")
    if not api_key:
        # Try loading from .env
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    if not api_key:
        print("Error: GEMINI_API_KEY not set.")
        sys.exit(1)

    # Read input
    if args.file:
        if args.file.lower().endswith(".pdf"):
            raw = _extract_pdf(args.file)
        else:
            with open(args.file, encoding="utf-8") as f:
                raw = f.read()
    else:
        print("Reading from stdin (paste text, then Ctrl+Z / Ctrl+D to finish):")
        raw = sys.stdin.read()

    # Split into sections on blank lines
    sections = [s.strip() for s in raw.split("\n\n") if s.strip() and len(s.strip()) > 60]
    party_display = args.party.replace("-", " ").title()

    print(f"\nParty: {party_display}")
    print(f"Sections found: {len(sections)}")
    print(f"Source URL: {args.url}")
    print(f"Mode: {'EXECUTE (writing to DB)' if args.execute else 'DRY RUN (no writes)'}")
    print("-" * 60)

    total_chunks = 0
    total_written = 0

    for i, section in enumerate(sections, 1):
        preview = section[:80].replace("\n", " ")
        print(f"\n[{i}/{len(sections)}] {preview}…")

        chunks = _call_gemini(api_key, section, party_display)
        print(f"  -> {len(chunks)} chunk(s) extracted")

        for chunk in chunks:
            text = chunk.get("chunk_text", "")[:80].replace("\n", " ")
            areas = chunk.get("policy_areas", [])
            print(f"     · [{', '.join(areas)}] {text}…")
            total_chunks += 1

        if args.execute and chunks:
            written = _write_chunks(
                party_slug=args.party,
                source_url=args.url,
                section_title=section[:200],
                chunks=chunks,
                execute=True,
            )
            total_written += written
            print(f"  -> {written} written to DB as pending")

        time.sleep(_INTER_REQUEST_DELAY)

    print("\n" + "=" * 60)
    print(f"Done. {total_chunks} chunks extracted from {len(sections)} sections.")
    if args.execute:
        print(f"{total_written} new chunks written to DB (pending review).")
        print("Review and approve at: /admin/manifesto-review")
    else:
        print("Dry run complete. Re-run with --execute to write to DB.")


if __name__ == "__main__":
    main()
