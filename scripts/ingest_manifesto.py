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
_REQUEST_TIMEOUT = 120
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


def _extract_section_title(text: str) -> str:
    """
    Heuristically extract a section heading from the start of a text block.
    Returns the first line if it looks like a heading (short, not mid-sentence),
    otherwise returns an empty string.
    """
    lines = text.strip().splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    # Heading: short, doesn't end with comma/semicolon/lowercase continuation
    if len(first) <= 100 and not first.endswith((",", ";", ":")):
        last_char = first[-1] if first else ""
        # Reject if it ends mid-sentence (ends with lowercase letter or digit)
        if last_char and (last_char.isdigit() or (last_char.isalpha() and last_char == last_char.lower() and last_char != last_char.upper())):
            return ""
        return first
    return ""


def _extract_pdf(path: str) -> list[tuple[str, int, str]]:
    """
    Extract text from a PDF using pdfplumber.
    Returns list of (section_text, page_number, section_title) tuples.
    Each page is joined with '\\n\\n' separators; sections are split on that
    boundary. page_number is 1-indexed (the page where the section starts).
    """
    try:
        import pdfplumber
    except ImportError:
        print("Error: pdfplumber is not installed. Run: pip install pdfplumber")
        sys.exit(1)

    # Extract per-page texts and build a character-offset -> page-number index
    page_texts: list[str] = []
    page_start_offsets: list[tuple[int, int]] = []  # (char_offset, page_no)
    cumulative = 0

    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        print(f"Extracting text from {total} pages...")
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text(x_tolerance=2, y_tolerance=2)
            if text and text.strip():
                t = text.strip()
                page_start_offsets.append((cumulative, i))
                page_texts.append(t)
                cumulative += len(t) + 2  # +2 for the \n\n separator we'll add
            if i % 20 == 0:
                print(f"  {i}/{total} pages done")

    print(f"Extracted text from {len(page_texts)} pages.")

    full_text = "\n\n".join(page_texts)

    def _page_for_offset(offset: int) -> int:
        """Return the page number that contains this character offset."""
        result = page_start_offsets[0][1] if page_start_offsets else 1
        for start, page_no in page_start_offsets:
            if start <= offset:
                result = page_no
            else:
                break
        return result

    # Split on double-newlines and attribute each section to its starting page
    sections: list[tuple[str, int, str]] = []
    pos = 0
    for raw in full_text.split("\n\n"):
        block = raw.strip()
        if block and len(block) > 60:
            page_no = _page_for_offset(pos)
            title = _extract_section_title(block)
            sections.append((block, page_no, title))
        pos += len(raw) + 2

    print(f"Split into {len(sections)} sections.")
    return sections


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

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            print(f"  [RETRY] Timeout from Gemini — waiting {wait}s (attempt {attempt + 1}/3)...")
            time.sleep(wait)
            continue
        if resp.status_code == 503:
            wait = 2 ** attempt
            print(f"  [RETRY] 503 from Gemini — waiting {wait}s (attempt {attempt + 1}/3)...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        print("  [WARN] Gemini did not respond after 3 attempts — skipping section.")
        return []

    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return parsed.get("chunks", [])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"  [WARN] Could not parse Gemini response: {e}")
        return []


def _load_database_url() -> str:
    """Read DATABASE_URL from .env in the project root."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("DATABASE_URL", "")


def _ensure_tables(db_url: str) -> None:
    """Create manifesto tables if they don't exist, and add any missing columns."""
    import psycopg2
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS manifesto_chunk (
                        id             SERIAL PRIMARY KEY,
                        party_slug     TEXT NOT NULL,
                        manifesto_year INTEGER NOT NULL DEFAULT 2024,
                        source_section TEXT,
                        source_url     TEXT,
                        source_page    INTEGER,
                        pdf_url        TEXT,
                        chunk_text     TEXT NOT NULL,
                        ingested_at    TIMESTAMP NOT NULL,
                        review_status  TEXT NOT NULL DEFAULT 'pending',
                        CONSTRAINT uq_manifesto_chunk
                            UNIQUE (party_slug, manifesto_year, chunk_text)
                    )
                """)
                # Add columns added after initial release (safe to run on existing tables)
                for col_sql in [
                    "ALTER TABLE manifesto_chunk ADD COLUMN IF NOT EXISTS source_page INTEGER",
                    "ALTER TABLE manifesto_chunk ADD COLUMN IF NOT EXISTS pdf_url TEXT",
                ]:
                    cur.execute(col_sql)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS manifesto_chunk_tag (
                        chunk_id    INTEGER NOT NULL
                                        REFERENCES manifesto_chunk(id) ON DELETE CASCADE,
                        policy_area TEXT NOT NULL,
                        is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
                        PRIMARY KEY (chunk_id, policy_area)
                    )
                """)
        print("Tables ready (manifesto_chunk, manifesto_chunk_tag).")
    finally:
        conn.close()


def _delete_party_chunks(party_slug: str, manifesto_year: int, conn) -> int:
    """Delete all existing chunks for a party/year. Returns count deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM manifesto_chunk WHERE party_slug=%s AND manifesto_year=%s",
            (party_slug, manifesto_year),
        )
        return cur.rowcount


def _write_chunks(party_slug: str, source_url: str, pdf_url: str,
                  section_title: str, source_page: int,
                  chunks: list[dict], manifesto_year: int = 2024) -> int:
    """Write chunks to DB as 'pending' using psycopg2 directly (no flask_app import)."""
    import psycopg2

    db_url = _load_database_url()
    if not db_url:
        print("Error: DATABASE_URL not found in .env")
        return 0

    conn = psycopg2.connect(db_url)
    written = 0

    try:
        with conn:
            with conn.cursor() as cur:
                for chunk_data in chunks:
                    text = chunk_data.get("chunk_text", "").strip()
                    areas = chunk_data.get("policy_areas", [])
                    if not text or not areas:
                        continue

                    # Check for duplicate
                    cur.execute(
                        "SELECT id FROM manifesto_chunk WHERE party_slug=%s AND manifesto_year=%s AND chunk_text=%s",
                        (party_slug, manifesto_year, text),
                    )
                    if cur.fetchone():
                        print(f"  [SKIP] Already exists: {text[:60].encode('ascii','replace').decode('ascii')}...")
                        continue

                    cur.execute(
                        """INSERT INTO manifesto_chunk
                               (party_slug, manifesto_year, source_section, source_url,
                                source_page, pdf_url, chunk_text, ingested_at, review_status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), 'pending')
                           RETURNING id""",
                        (party_slug, manifesto_year,
                         section_title[:200] if section_title else None,
                         source_url or None,
                         source_page or None,
                         pdf_url or None,
                         text),
                    )
                    chunk_id = cur.fetchone()[0]

                    for i, area in enumerate(areas):
                        if area in POLICY_AREAS:
                            cur.execute(
                                """INSERT INTO manifesto_chunk_tag (chunk_id, policy_area, is_primary)
                                   VALUES (%s, %s, %s)
                                   ON CONFLICT DO NOTHING""",
                                (chunk_id, area, i == 0),
                            )
                    written += 1
    finally:
        conn.close()

    return written


def main():
    parser = argparse.ArgumentParser(description="Ingest party manifesto chunks into Westminster Brief.")
    parser.add_argument("--party",   required=True, help="Party slug (e.g. labour, conservative)")
    parser.add_argument("--file",    required=False, help="Path to manifesto file (PDF or plain text)")
    parser.add_argument("--url",     required=True, help="HTML manifesto page URL (for attribution)")
    parser.add_argument("--pdf-url", required=False, default="",
                        help="Direct PDF URL (for #page=N deep links, e.g. https://party.org/manifesto.pdf)")
    parser.add_argument("--year",    required=False, type=int, default=2024,
                        help="Manifesto year (default: 2024)")
    parser.add_argument("--execute", action="store_true", help="Write to DB (default: dry run)")
    parser.add_argument("--replace", action="store_true",
                        help="Delete existing chunks for this party/year before ingesting")
    args = parser.parse_args()

    if args.party not in VALID_SLUGS:
        print(f"Error: unknown party slug '{args.party}'. Valid: {', '.join(sorted(VALID_SLUGS))}")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY_PROD")
    if not api_key:
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

    # Read input — PDF returns list of (text, page_no, title) tuples
    is_pdf = False
    if args.file:
        if args.file.lower().endswith(".pdf"):
            is_pdf = True
            sections = _extract_pdf(args.file)  # list of (text, page_no, title)
        else:
            with open(args.file, encoding="utf-8") as f:
                raw = f.read()
            sections = [
                (s.strip(), None, "")
                for s in raw.split("\n\n")
                if s.strip() and len(s.strip()) > 60
            ]
    else:
        print("Reading from stdin (paste text, then Ctrl+Z / Ctrl+D to finish):")
        raw = sys.stdin.read()
        sections = [
            (s.strip(), None, "")
            for s in raw.split("\n\n")
            if s.strip() and len(s.strip()) > 60
        ]

    party_display = args.party.replace("-", " ").title()
    pdf_url = args.pdf_url.strip()

    print(f"\nParty:       {party_display}")
    print(f"Year:        {args.year}")
    print(f"Sections:    {len(sections)}")
    print(f"Source URL:  {args.url}")
    print(f"PDF URL:     {pdf_url or '(none — page numbers stored but no deep links)'}")
    print(f"Mode:        {'EXECUTE (writing to DB)' if args.execute else 'DRY RUN (no writes)'}")
    if args.replace:
        print(f"Replace:     YES — existing {args.year} chunks for {args.party} will be deleted first")
    print("-" * 60)

    if args.execute:
        import psycopg2
        db_url = _load_database_url()
        if not db_url:
            print("Error: DATABASE_URL not found in .env")
            sys.exit(1)
        _ensure_tables(db_url)

        if args.replace:
            conn = psycopg2.connect(db_url)
            try:
                with conn:
                    deleted = _delete_party_chunks(args.party, args.year, conn)
                print(f"Deleted {deleted} existing chunks for {args.party} {args.year}.")
            finally:
                conn.close()

    total_chunks = 0
    total_written = 0

    for i, (section_text, page_no, section_title) in enumerate(sections, 1):
        preview = section_text[:80].replace("\n", " ").encode("ascii", errors="replace").decode("ascii")
        page_tag = f" [p.{page_no}]" if page_no else ""
        title_tag = f" | {section_title[:40]}" if section_title else ""
        print(f"\n[{i}/{len(sections)}]{page_tag}{title_tag}")
        print(f"  {preview}...")

        chunks = _call_gemini(api_key, section_text, party_display)
        print(f"  -> {len(chunks)} chunk(s) extracted")

        for chunk in chunks:
            text = chunk.get("chunk_text", "")[:80].replace("\n", " ").encode("ascii", errors="replace").decode("ascii")
            areas = chunk.get("policy_areas", [])
            print(f"     * [{', '.join(areas)}] {text}...")
            total_chunks += 1

        if args.execute and chunks:
            written = _write_chunks(
                party_slug=args.party,
                source_url=args.url,
                pdf_url=pdf_url,
                section_title=section_title,
                source_page=page_no,
                chunks=chunks,
                manifesto_year=args.year,
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
