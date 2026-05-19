"""
Bills tagging pipeline — Gemini 2.5 Pro.

Tags each ingested bill with one or more policy areas from the GOV.UK taxonomy
(POLICY_AREAS in hansard_archive/tagger.py).  Writes results to ha_bill_theme.

Resume logic:
    Selects bills WHERE tagging_completed_at IS NULL
                    AND tagging_failure_reason IS NULL
    ordered by id — each daily run continues from where the previous left off.

Usage:
    python scripts/tag_bills.py                    # resume / full run
    python scripts/tag_bills.py --limit N          # tag at most N bills (calibration)
    python scripts/tag_bills.py --dry-run          # show which bills would be tagged
    python scripts/tag_bills.py --reset-failures   # clear permanent failures and retry

Failure handling:
    Transient (429, 503, timeout)      — retry 3x with exponential backoff, never permanent
    Validation: empty array            — retry up to 3 times, then permanent
    Validation: invalid theme names    — retry up to 2 times, then permanent
    Validation: malformed / non-list   — retry up to 2 times, then permanent
    Tag count > 4                      — accepted but flagged as suspicious in summary
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from extensions import db
from hansard_archive.models import HaBill, HaBillTheme
from hansard_archive.tagger import POLICY_AREAS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_GEMINI = "https://generativelanguage.googleapis.com"
PREFERRED_MODEL_PREFIXES = [
    "models/gemini-2.5-pro",
    "models/gemini-2.0-pro",
    "models/gemini-1.5-pro",
]
FALLBACK_MODEL = "gemini-2.5-pro-preview-05-06"

REQUEST_TIMEOUT = 60   # Pro model can be slower
INTER_BILL_DELAY = 0.5  # between bills

# Max attempts per validation failure category (first attempt + N retries)
MAX_ATTEMPTS = {
    "empty_array":     4,   # 3 retries
    "invalid_themes":  3,   # 2 retries
    "malformed_json":  3,   # 2 retries
    "not_a_list":      3,   # 2 retries
}
MAX_TRANSIENT_RETRIES = 3
SUSPICIOUS_THRESHOLD = 4    # flag but accept if tag count exceeds this

PROGRESS_EVERY_N = 25
PROGRESS_EVERY_SECS = 300   # 5 minutes

_BILL_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "STRING",
        "enum": POLICY_AREAS,
    },
}

_POLICY_AREAS_SET = set(POLICY_AREAS)
_POLICY_AREAS_LIST_STR = "\n".join(f"  - {a}" for a in POLICY_AREAS)

_BILL_PROMPT_TEMPLATE = """\
You are tagging a UK Parliament bill to one or more UK government policy areas.

Available policy areas (use exact names from this list):
{policy_areas_list}

Return ALL policy areas this bill substantively covers. A bill may cover multiple areas.\
 For example, a bill on apprenticeships would typically cover both \
"Education, training and skills" AND "Employment and labour market" — both should be returned.

Do not under-tag for the sake of simplicity. Equally, do not over-tag — only include \
areas the bill is substantively about, not areas tangentially affected.

Bill context:
Title: {title}
Long title: {long_title}
Summary: {summary}

Return a JSON array of the relevant policy area names. Use only exact names from the \
list above. Do not invent new names. Do not include explanations. Return only the \
JSON array.

Example output: ["Education, training and skills", "Employment and labour market"]"""


# ---------------------------------------------------------------------------
# Gemini model detection
# ---------------------------------------------------------------------------

_model_cache: dict[str, str] = {}


def _detect_pro_model(api_key: str) -> str:
    if api_key in _model_cache:
        return _model_cache[api_key]
    try:
        resp = requests.get(
            f"{BASE_GEMINI}/v1beta/models",
            params={"key": api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            available = [
                m["name"]
                for m in resp.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            for prefix in PREFERRED_MODEL_PREFIXES:
                match = next((m for m in available if m.startswith(prefix)), None)
                if match:
                    model = match.removeprefix("models/")
                    _model_cache[api_key] = model
                    return model
    except Exception:
        pass
    _model_cache[api_key] = FALLBACK_MODEL
    return FALLBACK_MODEL


# ---------------------------------------------------------------------------
# Gemini HTTP call (single attempt, no retry)
# ---------------------------------------------------------------------------

def _call_gemini_once(api_key: str, model: str, prompt: str) -> tuple[str | None, int, str | None]:
    """
    Single Gemini call.
    Returns (raw_text, http_status, error_message).
    raw_text is None on any failure.
    """
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _BILL_RESPONSE_SCHEMA,
        },
    }
    for version in ("v1beta", "v1"):
        url = f"{BASE_GEMINI}/{version}/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return raw, 200, None
            return None, resp.status_code, f"HTTP {resp.status_code}"
        except requests.Timeout:
            return None, 0, "timeout"
        except (requests.RequestException, KeyError, IndexError) as exc:
            return None, 0, str(exc)
    return None, 0, "exhausted versions"


def _call_with_backoff(api_key: str, model: str, prompt: str) -> tuple[str | None, str | None]:
    """
    Wraps _call_gemini_once with transient retry (429/503/timeout).
    Returns (raw_text, error) — raw_text is None only if transient retries exhausted.
    """
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        raw, status, err = _call_gemini_once(api_key, model, prompt)
        if raw is not None:
            return raw, None
        if status in (401, 403):
            return None, f"auth_error:{status}"
        if status in (429, 503) or err == "timeout":
            if attempt < MAX_TRANSIENT_RETRIES:
                delay = 2 ** (attempt + 1)
                logger.warning("Transient %s (attempt %d/%d) — waiting %ds",
                               err, attempt + 1, MAX_TRANSIENT_RETRIES + 1, delay)
                time.sleep(delay)
                continue
        return None, err or f"http_{status}"
    return None, "transient_retries_exhausted"


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

class _ValidationResult(NamedTuple):
    ok: bool
    themes: list[str]        # populated when ok=True
    failure_category: str    # populated when ok=False
    failure_detail: str      # extra context
    suspicious: bool         # True if len(themes) > SUSPICIOUS_THRESHOLD


def _validate(raw_text: str) -> _ValidationResult:
    """Parse and validate a Gemini response string."""
    _empty = _ValidationResult(False, [], "", "", False)

    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return _ValidationResult(False, [], "malformed_json", raw_text[:80], False)

    if not isinstance(parsed, list):
        return _ValidationResult(False, [], "not_a_list", type(parsed).__name__, False)

    if not parsed:
        return _ValidationResult(False, [], "empty_array", "", False)

    invalid = [t for t in parsed if t not in _POLICY_AREAS_SET]
    if invalid:
        return _ValidationResult(
            False, [], "invalid_themes", ", ".join(invalid[:3]), False
        )

    suspicious = len(parsed) > SUSPICIOUS_THRESHOLD
    return _ValidationResult(True, parsed, "", "", suspicious)


# ---------------------------------------------------------------------------
# Per-bill tagging (validation retry loop)
# ---------------------------------------------------------------------------

def _build_prompt(bill: HaBill) -> str:
    summary_part = bill.summary or "(not provided)"
    long_title_part = bill.long_title or "(not provided)"
    return _BILL_PROMPT_TEMPLATE.format(
        policy_areas_list=_POLICY_AREAS_LIST_STR,
        title=bill.title,
        long_title=long_title_part,
        summary=summary_part,
    )


def _tag_one_bill(
    bill: HaBill, api_key: str, model: str
) -> tuple[list[str] | None, str | None, bool]:
    """
    Tag a single bill with full retry logic.
    Returns (themes, failure_reason, suspicious).
      themes is None on permanent failure.
      suspicious is True if tag count > SUSPICIOUS_THRESHOLD (but themes is still returned).
    """
    prompt = _build_prompt(bill)
    attempt_counts: defaultdict[str, int] = defaultdict(int)
    OVERALL_CAP = max(MAX_ATTEMPTS.values()) + 1  # absolute ceiling

    for overall_attempt in range(OVERALL_CAP):
        raw, transient_err = _call_with_backoff(api_key, model, prompt)

        if raw is None:
            # Transient retries exhausted or auth error — permanent
            return None, f"api_error:{transient_err}", False

        result = _validate(raw)

        if result.ok:
            return result.themes, None, result.suspicious

        cat = result.failure_category
        attempt_counts[cat] += 1
        max_for_cat = MAX_ATTEMPTS.get(cat, 3)

        if attempt_counts[cat] >= max_for_cat:
            reason = f"{cat}:{result.failure_detail}" if result.failure_detail else cat
            return None, reason, False

        # Brief pause before retry
        time.sleep(1)

    return None, "max_attempts_exceeded", False


# ---------------------------------------------------------------------------
# Theme writer
# ---------------------------------------------------------------------------

def _write_themes(bill: HaBill, themes: list[str]) -> None:
    """Delete existing themes for a bill and write new rows, then update timestamps."""
    HaBillTheme.query.filter_by(bill_id=bill.id).delete()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for theme in themes:
        db.session.add(HaBillTheme(
            bill_id=bill.id,
            theme=theme,
            tagged_by="ai_gemini_pro",
            tagged_at=now,
        ))
    bill.tagging_completed_at = now
    bill.tagging_failure_reason = None
    db.session.commit()


# ---------------------------------------------------------------------------
# Progress and summary helpers
# ---------------------------------------------------------------------------

def _progress_line(done: int, failed: int, total: int, start_time: float) -> str:
    remaining = total - done - failed
    elapsed = time.time() - start_time
    avg_s = elapsed / (done + failed) if (done + failed) > 0 else 0
    return (
        f"[tag] {done}/{total} complete | {failed} failed | "
        f"{remaining} remaining | avg {avg_s:.1f}s/bill"
    )


def _print_summary(
    total: int,
    done: int,
    failed: int,
    suspicious_bills: list[tuple[int, str, int]],
    failed_bills: list[tuple[int, str, str]],
    theme_counter: Counter,
    elapsed: float,
    limit: int | None,
) -> None:
    print("\n[tag] ========== Run summary ==========", flush=True)
    print(f"  Attempted:  {done + failed}/{total}", flush=True)
    print(f"  Completed:  {done}", flush=True)
    print(f"  Failed:     {failed}", flush=True)
    print(f"  Elapsed:    {elapsed:.0f}s", flush=True)

    if limit:
        remaining_untagged = total - done - failed
        print(f"  Daily cap:  --limit {limit} applied. "
              f"{remaining_untagged} bills remain for next run.", flush=True)

    if suspicious_bills:
        print(f"\n  Suspicious (>{SUSPICIOUS_THRESHOLD} tags) — review recommended:", flush=True)
        for pid, title, n in suspicious_bills:
            print(f"    bill {pid} ({n} tags): {title[:60]}", flush=True)

    if failed_bills:
        print(f"\n  Failed bills:", flush=True)
        for pid, title, reason in failed_bills:
            print(f"    bill {pid}: {reason} — {title[:50]}", flush=True)

    if theme_counter:
        print(f"\n  Theme distribution ({len(theme_counter)} areas used):", flush=True)
        for theme, count in theme_counter.most_common():
            bar = "#" * min(count, 40)
            print(f"    {count:4d} {bar}  {theme}", flush=True)


# ---------------------------------------------------------------------------
# Main tagging loop
# ---------------------------------------------------------------------------

def tag_bills(
    api_key: str,
    limit: int | None = None,
    dry_run: bool = False,
    reset_failures: bool = False,
) -> dict:
    """
    Main tagging loop. Expects an active Flask app context.
    Returns a summary dict.
    """
    model = _detect_pro_model(api_key)
    print(f"[tag] Model: {model}", flush=True)

    if reset_failures:
        n = HaBill.query.filter(HaBill.tagging_failure_reason.isnot(None)).update(
            {"tagging_failure_reason": None, "tagging_attempted_at": None}
        )
        db.session.commit()
        print(f"[tag] Cleared permanent failures on {n} bills", flush=True)

    # Resume query: not yet completed and not permanently failed
    query = (
        HaBill.query
        .filter(HaBill.tagging_completed_at.is_(None))
        .filter(HaBill.tagging_failure_reason.is_(None))
        .order_by(HaBill.id)
    )
    total_eligible = query.count()

    if limit:
        bills = query.limit(limit).all()
    else:
        bills = query.all()

    total = len(bills)
    print(
        f"[tag] {total_eligible} bills eligible | "
        f"{'--limit applied: ' if limit else ''}{total} to tag this run",
        flush=True,
    )

    if dry_run:
        for b in bills:
            print(f"[tag] DRY {b.parliament_bill_id}: {b.title[:70]!r}", flush=True)
        return {"dry_run": True, "would_tag": total}

    start_time = time.time()
    last_progress_time = start_time

    done = failed = 0
    suspicious_bills: list[tuple[int, str, int]] = []
    failed_bills: list[tuple[int, str, str]] = []
    theme_counter: Counter = Counter()

    for i, bill in enumerate(bills):
        time.sleep(INTER_BILL_DELAY)

        # Mark attempt start
        bill.tagging_attempted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()

        try:
            themes, failure_reason, suspicious = _tag_one_bill(bill, api_key, model)
        except Exception as exc:
            logger.error("Unexpected error tagging bill %d: %s", bill.parliament_bill_id, exc)
            failure_reason = f"unexpected:{str(exc)[:80]}"
            themes = None
            suspicious = False

        if themes is not None:
            _write_themes(bill, themes)
            done += 1
            theme_counter.update(themes)
            if suspicious:
                suspicious_bills.append((bill.parliament_bill_id, bill.title, len(themes)))
        else:
            bill.tagging_failure_reason = failure_reason
            db.session.commit()
            failed += 1
            failed_bills.append((bill.parliament_bill_id, bill.title, failure_reason or "unknown"))

        # Progress every 25 bills or 5 minutes
        now = time.time()
        if (i + 1) % PROGRESS_EVERY_N == 0 or (now - last_progress_time) >= PROGRESS_EVERY_SECS:
            print(_progress_line(done, failed, total, start_time), flush=True)
            last_progress_time = now

    elapsed = time.time() - start_time
    _print_summary(
        total=total,
        done=done,
        failed=failed,
        suspicious_bills=suspicious_bills,
        failed_bills=failed_bills,
        theme_counter=theme_counter,
        elapsed=elapsed,
        limit=limit,
    )

    return {
        "total_eligible": total_eligible,
        "total_attempted": total,
        "completed": done,
        "failed": failed,
        "suspicious": len(suspicious_bills),
        "elapsed_s": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Tag Parliament bills with Gemini 2.5 Pro")
    parser.add_argument("--limit",          type=int,  help="Max bills to tag this run")
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--reset-failures", action="store_true",
                        help="Clear tagging_failure_reason to retry permanently-failed bills")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY not set")

    from flask_app import app
    with app.app_context():
        tag_bills(
            api_key=api_key,
            limit=args.limit,
            dry_run=args.dry_run,
            reset_failures=args.reset_failures,
        )


if __name__ == "__main__":
    main()
