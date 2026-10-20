"""
Westminster Brief — Phase 2 Statistics Refresh

Flask CLI group: `flask stats refresh-all` / `flask stats refresh-theme <slug>`
Also invocable directly: `python stats_refresh.py [--theme <slug>]`

Refresh logic (Oct 2026): each stat's source comes from stats_sources.STAT_SOURCES
(keyed by source_id; HeadlineStat.extraction_config JSON as a fallback):
- ONS timeseries, ONS bulletins, GOV.UK CSV/ODS attachments, the DfE Explore
  Education Statistics API and NHS England A&E CSVs are read as structured data
- bulletin text is read with per-stat regex patterns; Gemini Flash-Lite is only
  a fallback when the wording has changed, and its answer is validated the same way
- manual → skipped (human updates rows directly via admin route)

Strict validation (validate_candidate): a value is written ONLY if it is a plain
number inside the stat's configured bounds, its evidence (verbatim sentence or
source data row) contains it, its period parses, and the release is the latest.
Otherwise the stat is FAILED or NEEDS-REVIEW and the stored value is kept.

SWR (stale-while-revalidate): on failure, last_refreshed is updated but
existing values stay in place so pages continue to render.

Phase 1.5 (May 2026): each successful fetch also writes a StatObservation
row (upsert on period). Plain English generation is explicitly deferred —
plain_english, plain_english_generated_at, and rewrite_model are NULL on
every observation row and are also NULLed on the HeadlineStat legacy mirror.

Observations with period_end < 2024-07-01 (the current-government cutoff)
are rejected — StatObservation is not written, but legacy HeadlineStat
fields still update so pages continue to render (SWR preserved).

Runs: weekly Railway cron, Mondays 06:00 UTC (stats-cron service).
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import click
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

log = logging.getLogger("stats_refresh")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Constants ─────────────────────────────────────────────────────────────────

_ONS_DATA_URL    = "https://api.beta.ons.gov.uk/v1/data"
_TIMEOUT         = 20   # seconds per request
_REWRITE_MODEL   = "gemini-2.5-flash"
_EXTRACT_MODEL   = "gemini-2.5-flash-lite"

# Rewrite prompt verbatim from the Phase 2 brief
_REWRITE_PROMPT = """\
Rewrite the following figure and its source sentence in one or two plain-English sentences. \
You may simplify wording. You may not add information, comparisons, or implications not present \
in the source. Return only the rewrite — no preamble, no caveats, no quotation marks.

Source sentence: {source_wording}
Figure: {latest_value} {unit} ({period_label})"""

# ── Period parsing (Phase 1.5) ─────────────────────────────────────────────
# Month name → number, covering both abbreviations and full names.
_MONTH_MAP: dict[str, int] = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10,
    'november': 11, 'december': 12,
}

# Observations with period_end before this date are rejected at ingestion.
_OBSERVATION_CUTOFF = date(2024, 7, 1)


def _parse_ons_period(label: str, freq: str) -> tuple[date | None, date | None]:
    """
    Parse an ONS observation label and frequency into (period_start, period_end).

    ONS label formats observed in the wild:
      months:   "2024 JAN", "Jan 2024", "2024 January"
      quarters: "2024 Q3", "Q3 2024"
      years:    "2024", "2023-24" (financial year Apr–Mar)

    Returns (None, None) when parsing fails — caller should log a warning
    and skip the StatObservation write while still updating legacy fields.
    """
    label = (label or "").strip()

    if freq == "years":
        # Plain calendar year: "2024"
        m = re.match(r'^(\d{4})$', label)
        if m:
            y = int(m.group(1))
            return date(y, 1, 1), date(y, 12, 31)
        # UK financial year: "2023-24" or "2023/24" → Apr 2023 – Mar 2024
        m = re.match(r'^(\d{4})[-/](\d{2,4})$', label)
        if m:
            y = int(m.group(1))
            return date(y, 4, 1), date(y + 1, 3, 31)
        return None, None

    elif freq == "quarters":
        # "2024 Q3" → y=2024, q=3
        m = re.match(r'^(\d{4})\s+Q(\d)$', label, re.I)
        if m:
            y, q = int(m.group(1)), int(m.group(2))
        else:
            # "Q3 2024" → q=3, y=2024
            m = re.match(r'^Q(\d)\s+(\d{4})$', label, re.I)
            if m:
                q, y = int(m.group(1)), int(m.group(2))
            else:
                return None, None
        if not (1 <= q <= 4):
            return None, None
        start_month = (q - 1) * 3 + 1
        end_month   = q * 3
        last_day    = calendar.monthrange(y, end_month)[1]
        return date(y, start_month, 1), date(y, end_month, last_day)

    elif freq == "months":
        # Rolling three months, e.g. LFS "2026 JUN-AUG". ONS labels these with
        # the START year: "2025 NOV-JAN" is Nov 2025 – Jan 2026.
        m = re.match(r'^(\d{4})\s+([A-Za-z]{3})-([A-Za-z]{3})$', label)
        if m and m.group(2).lower() in _MONTH_MAP and m.group(3).lower() in _MONTH_MAP:
            y = int(m.group(1))
            m1, m2 = _MONTH_MAP[m.group(2).lower()], _MONTH_MAP[m.group(3).lower()]
            y2 = y + 1 if m2 < m1 else y
            return date(y, m1, 1), date(y2, m2, calendar.monthrange(y2, m2)[1])
        parts = label.split()
        if len(parts) == 2:
            a, b = parts
            # "Jan 2024" or "January 2024"
            if a.lower() in _MONTH_MAP and b.isdigit():
                m_num    = _MONTH_MAP[a.lower()]
                y        = int(b)
                last_day = calendar.monthrange(y, m_num)[1]
                return date(y, m_num, 1), date(y, m_num, last_day)
            # "2024 JAN" or "2024 January"
            if b.lower() in _MONTH_MAP and a.isdigit():
                m_num    = _MONTH_MAP[b.lower()]
                y        = int(a)
                last_day = calendar.monthrange(y, m_num)[1]
                return date(y, m_num, 1), date(y, m_num, last_day)
        return None, None

    return None, None


def _parse_govuk_period(period_label: str) -> tuple[date | None, date | None]:
    """
    Parse a free-text period_label from govuk_bulletin Gemini extraction into
    (period_start, period_end).

    Gemini extracts period_label as free text; the patterns below cover the
    common government statistical publication formats. When none match, returns
    (None, None) — the caller should log a warning and skip StatObservation
    write while still updating legacy HeadlineStat fields.

    Understood patterns (in order of specificity):
      - "Q3 2024" / "2024 Q3"        → quarter (Jan-Mar = Q1, etc.)
      - "January 2026" / "Jan 2026"  → calendar month
      - "2023/24" / "2023-24"        → UK financial year (Apr–Mar)
      - "2023/2024"                  → UK financial year
      - "2024"                        → calendar year (Jan–Dec)
    """
    label = (period_label or "").strip()
    if not label:
        return None, None

    # Quarter: "Q3 2024" → q=3, y=2024
    m = re.match(r'^Q(\d)\s+(\d{4})$', label, re.I)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        if 1 <= q <= 4:
            start_month = (q - 1) * 3 + 1
            end_month   = q * 3
            last_day    = calendar.monthrange(y, end_month)[1]
            return date(y, start_month, 1), date(y, end_month, last_day)

    # Quarter: "2024 Q3" → y=2024, q=3
    m = re.match(r'^(\d{4})\s+Q(\d)$', label, re.I)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        if 1 <= q <= 4:
            start_month = (q - 1) * 3 + 1
            end_month   = q * 3
            last_day    = calendar.monthrange(y, end_month)[1]
            return date(y, start_month, 1), date(y, end_month, last_day)

    # UK financial year: "2023/24" or "2023-24" → Apr 2023 – Mar 2024
    # Also matches "2023/2024" and GOV.UK title style "2023 to 2024"
    m = re.match(r'^(\d{4})(?:[-/]| to )(\d{2,4})$', label)
    if m:
        y = int(m.group(1))
        return date(y, 4, 1), date(y + 1, 3, 31)

    # Twelve months to a month end: "year ending March 2026" → Apr 2025 – Mar 2026
    m = re.match(r'^year ending ([A-Za-z]+)\s+(\d{4})$', label, re.I)
    if m and m.group(1).lower() in _MONTH_MAP:
        m_num, y = _MONTH_MAP[m.group(1).lower()], int(m.group(2))
        start_y, start_m = (y, m_num + 1) if m_num < 12 else (y + 1, 1)
        return (date(start_y - 1, start_m, 1),
                date(y, m_num, calendar.monthrange(y, m_num)[1]))

    # Month + year: "January 2026" or "Jan 2026"
    m = re.match(r'^([A-Za-z]+)\s+(\d{4})$', label)
    if m:
        month_str = m.group(1).lower()
        if month_str in _MONTH_MAP:
            m_num    = _MONTH_MAP[month_str]
            y        = int(m.group(2))
            last_day = calendar.monthrange(y, m_num)[1]
            return date(y, m_num, 1), date(y, m_num, last_day)

    # Plain calendar year: "2024"
    m = re.match(r'^(\d{4})$', label)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31)

    return None, None

# Gemini is only a fallback for text sources whose regex patterns no longer
# match. The prompt deliberately contains NO example values: the old prompt's
# examples ("3.2", "Q3 2024", "£12.4 billion") were copied back as answers
# when the model was shown a page with no figure in it.
_EXTRACT_PROMPT = """\
You are reading the text of a UK official statistics release.

Find this statistic: {measure}
Geography: {geography}
Expected unit: {unit}

Rules:
- Use only the release text below. Do not use outside knowledge.
- "value": the number exactly as written in the text, digits only with any commas \
or decimal point it has; no currency symbols, units or words.
- "unit": the unit the text gives for that number.
- "period_label": the time period the number refers to, as written in the text.
- "evidence": one sentence copied character for character from the text that contains the value.
- If the text does not state this statistic, return {{"error": "not found"}}.

Return only a JSON object with the keys value, unit, period_label, evidence.

Release text:
<<<
{content}
>>>"""

_GEMINI_MAX_CHARS = 60000

_FETCHERS = {
    "ons_timeseries": "fetch_ons_timeseries",
    "govuk_csv":      "fetch_govuk_csv",
    "govuk_ods":      "fetch_govuk_ods",
    "ees_api":        "fetch_ees",
    "nhs_ae_csv":     "fetch_nhs_ae",
}
_TEXT_FETCHERS = {
    "ons_bulletin": "fetch_ons_bulletin_text",
    "govuk_text":   "fetch_govuk_edition_text",
}


def _gemini_extract(stat, cfg: dict, text: str, gemini_key: str) -> dict:
    """Ask Gemini for the figure. Raises SourceError; the result is still validated."""
    from stats_sources import SourceError

    prompt = _EXTRACT_PROMPT.format(
        measure=cfg.get("measure") or stat.display_label,
        geography=stat.geography or "UK",
        unit=cfg.get("unit") or stat.unit or "not specified",
        content=text[:_GEMINI_MAX_CHARS],
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    try:
        gr = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_EXTRACT_MODEL}:generateContent",
            params={"key": gemini_key},
            json=payload,
            timeout=60,
        )
        gr.raise_for_status()
        out = json.loads(gr.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as exc:
        raise SourceError(f"Gemini extraction failed: {type(exc).__name__}") from exc
    if not isinstance(out, dict):
        raise SourceError("Gemini returned non-object JSON")
    if "error" in out:
        raise SourceError(f"Gemini found no figure: {out['error']}")
    missing = {"value", "unit", "period_label", "evidence"} - set(out)
    if missing:
        raise SourceError(f"Gemini JSON missing keys: {sorted(missing)}")
    return {k: str(out[k]).strip() for k in ("value", "unit", "period_label", "evidence")}


def _text_candidate(stat, cfg: dict, meta: dict, gemini_key: str) -> dict:
    """
    Build a candidate from cleaned release text: per-stat regex patterns first,
    Gemini only if none match. Period comes from the pattern, else from the
    release metadata (edition / title), else from the model's period_label.
    """
    from stats_sources import SourceError, normalise_ws, sentence_around

    text = meta["text"]
    cand = {
        "release_date": meta.get("release_date"),
        "release_url": meta.get("release_url") or stat.source_url,
        "evidence_kind": "text",
        "source_text": text,
        "review": list(meta.get("review") or []),
    }
    for pat in cfg.get("patterns") or []:
        m = re.search(pat, text)
        if not m:
            continue
        evidence = sentence_around(text, m.start(), m.end())
        label = m.groupdict().get("period") or meta.get("period_label") or ""
        start, end = _parse_govuk_period(label)
        if m.groupdict().get("period") is None and meta.get("period_start"):
            start, end = meta["period_start"], meta["period_end"]
        cand.update({
            "value": m.group("value"), "period_label": label,
            "period_start": start, "period_end": end,
            "evidence": evidence, "source_wording": evidence.lstrip("-•* "),
            "extracted_by": "pattern",
        })
        return cand

    if not gemini_key:
        raise SourceError("no configured pattern matched the release text and GEMINI_API_KEY "
                          "is not set, so there is no fallback")
    got = _gemini_extract(stat, cfg, text, gemini_key)
    start, end = _parse_govuk_period(got["period_label"])
    if meta.get("period_start") and (start, end) != (meta["period_start"], meta["period_end"]):
        cand["review"].append(
            f"model period {got['period_label']!r} differs from the release period "
            f"{meta.get('period_label')!r}")
    evidence = normalise_ws(got["evidence"])
    cand.update({
        "value": got["value"], "model_unit": got["unit"],
        "period_label": got["period_label"],
        "period_start": start, "period_end": end,
        "evidence": evidence, "source_wording": evidence.lstrip("-•* "),
        "extracted_by": "gemini",
    })
    return cand


def _fetch_candidate(stat, cfg: dict | None, gemini_key: str, today: date) -> dict:
    """Dispatch to the configured source. Raises SourceError on any read failure."""
    import stats_sources as src

    strategy = (cfg or {}).get("strategy") or stat.source_type
    if strategy in _FETCHERS:
        return getattr(src, _FETCHERS[strategy])(stat, cfg or {}, today)
    if strategy in _TEXT_FETCHERS:
        meta = getattr(src, _TEXT_FETCHERS[strategy])(stat, cfg, today)
        return _text_candidate(stat, cfg, meta, gemini_key)
    if strategy == "govuk_bulletin":
        # Unconfigured legacy row: read the visible page text (never raw HTML).
        page = src.http_get(stat.source_url)
        review = [] if cfg else ["no source configuration for this stat in stats_sources.STAT_SOURCES"]
        meta = {"text": src.html_to_text(page.text), "release_url": stat.source_url,
                "review": review}
        return _text_candidate(stat, cfg or {}, meta, gemini_key)
    raise src.SourceError(f"unknown source strategy {strategy!r}")


# ── Validation ────────────────────────────────────────────────────────────────

def _number_in(value: str, text: str) -> bool:
    """value appears in text as a whole number (so '3.2' does not match '13.2')."""
    return re.search(r"(?<![\d.,])" + re.escape(value) + r"(?![\d]|[.,]\d)", text) is not None


def validate_candidate(stat, cfg: dict | None, cand: dict, today: date) -> tuple[str, list[str]]:
    """
    Return ('ok' | 'needs_review' | 'failed', reasons).

    failed       — the value is wrong or unproven: not a plain number, out of
                   bounds, a year, evidence missing / not verbatim / without the
                   number, wrong unit, or value not equal to the source cell.
    needs_review — the value may be right but cannot be confirmed automatically:
                   no bounds configured, period not parsed, release not confirmed
                   as latest, or the latest period is older than expected.
    """
    from stats_sources import normalise_ws, parse_number, to_float

    cfg = cfg or {}
    failed: list[str] = []
    review: list[str] = list(cand.get("review") or [])
    value = (cand.get("value") or "").strip()
    unit = cfg.get("unit") or stat.unit or ""

    # (a) numeric, plausible range, not a year
    x = parse_number(value)
    if x is None:
        failed.append(f"value {value!r} is not a plain number")
    lo, hi = cfg.get("min"), cfg.get("max")
    if lo is None or hi is None:
        review.append("no sanity bounds (min/max) configured for this stat")
    elif x is not None and not (lo <= x <= hi):
        failed.append(f"value {value} is outside the expected range {lo} to {hi} ({unit})")
    if x is not None and x.is_integer() and 1990 <= x <= 2100 and "," not in value:
        context = f"{cand.get('period_label') or ''} {cand.get('evidence') or ''}"
        if re.search(r"\b" + re.escape(value) + r"\b", context):
            failed.append(f"value {value} looks like a year, not a statistic")
    model_unit = cand.get("model_unit")
    if model_unit is not None:
        aliases = cfg.get("unit_aliases")
        if not aliases:
            review.append(f"model gave unit {model_unit!r}; no unit_aliases configured to check it")
        elif not re.search(aliases, model_unit, re.I):
            failed.append(f"model unit {model_unit!r} does not match the expected unit {unit!r}")

    # (b) evidence
    evidence = normalise_ws(cand.get("evidence") or "")
    kind = cand.get("evidence_kind")
    if not evidence:
        failed.append("no evidence snippet")
    elif kind == "text":
        if evidence not in normalise_ws(cand.get("source_text") or ""):
            failed.append("evidence sentence is not in the source text verbatim")
        if value and not _number_in(value, evidence):
            failed.append(f"evidence sentence does not contain the value {value}")
        must = cfg.get("evidence_must_match")
        if must and not re.search(must, evidence, re.I):
            failed.append(f"evidence does not mention the measure ({must})")
    elif kind == "structured":
        raw = cand.get("evidence_value") or ""
        fx = to_float(raw)
        if not raw or raw not in evidence:
            failed.append("source cell value is missing from the evidence")
        elif fx is not None and x is not None:
            decimals = len(value.split(".")[1]) if "." in value else 0
            if abs(fx * cand.get("scale", 1) - x) > 0.5 * 10 ** -decimals + 1e-9:
                failed.append(f"value {value} does not match the source cell {raw}")
    elif kind != "derived":
        failed.append(f"unknown evidence kind {kind!r}")

    # (c) period
    p_start, p_end = cand.get("period_start"), cand.get("period_end")
    if p_start is None or p_end is None:
        review.append(f"period could not be parsed from {cand.get('period_label')!r}")

    # (d) latest release — fetchers add their own checks to cand['review'];
    # this catches series that have moved or stopped.
    max_age = cfg.get("max_period_age_days")
    if max_age and p_end and (today - p_end).days > max_age:
        review.append(f"latest period ends {p_end.isoformat()}, more than {max_age} days ago; "
                      "the series may have moved or stopped")

    if failed:
        return "failed", failed + review
    if review:
        return "needs_review", review
    return "ok", []


# ── Plain-English rewrite ─────────────────────────────────────────────────────
#
# _generate_rewrite() is intentionally kept here but NOT called.
#
# Plain English generation was deferred at Phase 1.5. The planned
# re-introduction point is Phase 1.8 (see docs/stats-index-design.md).
# At that point this function will be wired into _refresh_stat() once
# per successful observation write, with the result written to
# StatObservation.plain_english and plain_english_generated_at.
#
# DO NOT remove this function as dead code. It is preserved deliberately
# so Phase 1.8 can re-enable it without restructuring — the Gemini call,
# prompt, and error handling are already correct. If you are considering
# removing it, read docs/stats-index-design.md Phase 1.8 section first.

def _generate_rewrite(stat, fetched: dict, gemini_key: str) -> str | None:
    """
    Call Gemini Flash to rewrite source_wording as plain English.
    Not called in Phase 1.5 — plain English generation is deferred to Phase 1.8.
    """
    prompt = _REWRITE_PROMPT.format(
        source_wording = fetched.get("source_wording", ""),
        latest_value   = fetched.get("latest_value", ""),
        unit           = fetched.get("unit", ""),
        period_label   = fetched.get("period_label", ""),
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    try:
        gr = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_REWRITE_MODEL}:generateContent",
            params={"key": gemini_key},
            json=payload,
            timeout=60,
        )
        gr.raise_for_status()
        raw = gr.json()
        return raw["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:
        log.warning("Rewrite failed for %s: %s", stat.source_id, exc)
        return None


# ── StatObservation write (Phase 1.5) ─────────────────────────────────────────

def _write_observation(db, stat, fetched: dict, now: datetime) -> bool:
    """
    Upsert a StatObservation row from a successful fetch result.

    Rules:
    - period_end < _OBSERVATION_CUTOFF → reject; log warning; return False
    - period_start / period_end both None → parsing failed; log warning; return False
    - otherwise: upsert on (headline_stat_id, period_start, period_end)
    - plain_english / plain_english_generated_at / rewrite_model are always NULL
      (Phase 1.5 deferred; Phase 1.6 will add generation)

    Returns True if a row was written, False if skipped for any reason.
    """
    from hansard_archive.models import StatObservation
    from hansard_archive.stats_queries import is_straddling

    period_start = fetched.get("period_start")
    period_end   = fetched.get("period_end")

    # Reject pre-cutoff observations — they predate the current government.
    if period_end is not None and period_end < _OBSERVATION_CUTOFF:
        log.warning(
            "REJECT pre-cutoff obs for %s / %s: period_end=%s < cutoff=%s",
            stat.theme_slug, stat.source_id, period_end, _OBSERVATION_CUTOFF,
        )
        return False

    # Period parsing failed — we have a fetched value but can't anchor it in time.
    if period_start is None and period_end is None:
        log.warning(
            "SKIP obs for %s / %s: period could not be parsed from label=%r",
            stat.theme_slug, stat.source_id, fetched.get("period_label"),
        )
        return False

    straddles = is_straddling(period_start, period_end)

    existing = (
        StatObservation.query
        .filter_by(
            headline_stat_id=stat.id,
            period_start=period_start,
            period_end=period_end,
        )
        .first()
    )
    if existing:
        existing.value          = fetched.get("latest_value")
        existing.period_label   = fetched.get("period_label")
        existing.release_date   = fetched.get("release_date")
        existing.release_url    = fetched.get("release_url")
        existing.source_wording = fetched.get("source_wording")
        existing.straddles_cutoff = straddles
        # Phase 1.5: plain English deferred — leave NULL, never copy from legacy
        action = "UPSERT"
    else:
        obs = StatObservation(
            headline_stat_id           = stat.id,
            period_label               = fetched.get("period_label"),
            period_start               = period_start,
            period_end                 = period_end,
            value                      = fetched.get("latest_value"),
            release_date               = fetched.get("release_date"),
            release_url                = fetched.get("release_url"),
            source_wording             = fetched.get("source_wording"),
            plain_english              = None,   # Phase 1.5: plain English deferred
            plain_english_generated_at = None,
            rewrite_model              = None,
            straddles_cutoff           = straddles,
            created_at                 = now,
        )
        db.session.add(obs)
        action = "INSERT"

    log.info(
        "OBS %s %s / %s: %s–%s value=%r straddles=%s",
        action, stat.theme_slug, stat.source_id,
        period_start, period_end, fetched.get("latest_value"), straddles,
    )
    return True


# ── Core refresh logic ────────────────────────────────────────────────────────

_STATUS_LABEL = {"ok": "OK", "needs_review": "NEEDS-REVIEW", "failed": "FAILED", "skipped": "SKIP"}


def _report(stat, status: str, cand: dict | None, reasons: list[str], cfg: dict | None) -> None:
    """Print one block per stat: value, unit, period, source, evidence, status."""
    from stats_sources import parse_number, with_unit

    unit = (cfg or {}).get("unit") or stat.unit or ""
    lines = [f"[{_STATUS_LABEL[status]}] {stat.theme_slug} / {stat.source_id}"]
    if cand and cand.get("value") is not None:
        p = ""
        if cand.get("period_start"):
            p = f"  [{cand['period_start'].isoformat()} to {cand['period_end'].isoformat()}]"
        lines += [
            f"    value:    {with_unit(cand['value'], unit)}",
            f"    period:   {cand.get('period_label') or '(none)'}{p}",
            f"    release:  {cand.get('release_date') or 'date not given'}",
            f"    source:   {cand.get('release_url')}",
            f"    evidence: {cand.get('evidence')}"
            + (f"  (via {cand['extracted_by']})" if cand.get("extracted_by") else ""),
        ]
    for r in reasons:
        lines.append(f"    reason:   {r}")
    stored = (stat.latest_value or "").strip()
    flag = ""
    if stored:
        x = parse_number(stored)
        lo, hi = (cfg or {}).get("min"), (cfg or {}).get("max")
        if x is None or (lo is not None and hi is not None and not lo <= x <= hi):
            flag = "   <-- stored value fails validation; correct it in /admin/stats"
    shown = f"{stored} [unit: {stat.unit or 'none'}]" if stored else "(empty)"
    lines.append(f"    stored:   {shown}"
                 f"{' (' + stat.period_label + ')' if stat.period_label else ''}{flag}")
    click.echo("\n".join(lines))


def _refresh_stat(stat, gemini_key: str, dry_run: bool = False, today: date | None = None) -> str:
    """
    Refresh a single HeadlineStat row.
    Returns: 'ok' | 'needs_review' | 'failed' | 'skipped'

    Only 'ok' writes anything beyond last_refreshed:
    1. Write or update a StatObservation row (Phase 1.5).
    2. Mirror fetched values to legacy HeadlineStat fields so the admin UI
       and existing templates continue to work without changes. The unit
       always comes from the stat config, never from the source or a model.
       [TEMPORARY mirror — remove when admin UI migrates to StatObservation]
    3. Explicitly NULL out plain_english / plain_english_generated_at /
       rewrite_model (Phase 1.5: plain English generation deferred).

    'needs_review' / 'failed': the stored value is kept (SWR); only
    last_refreshed changes. Dry run writes nothing at all.
    """
    from flask_app import db
    from stats_sources import SourceError, get_stat_config

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = today or now.date()
    cfg = get_stat_config(stat)

    if stat.source_type == "manual" or (cfg or {}).get("strategy") == "manual":
        reason = (cfg or {}).get("reason") or "source_type is manual"
        log.info("SKIP (manual) %s / %s", stat.theme_slug, stat.source_id)
        _report(stat, "skipped", None, [reason], cfg)
        return "skipped"

    cand = None
    try:
        cand = _fetch_candidate(stat, cfg, gemini_key, today)
        status, reasons = validate_candidate(stat, cfg, cand, today)
    except SourceError as exc:
        status, reasons = "failed", [str(exc)]
    except Exception as exc:   # a source changing shape must not stop the run
        log.exception("Unexpected error refreshing %s", stat.source_id)
        status, reasons = "failed", [f"unexpected error: {type(exc).__name__}: {exc}"]

    _report(stat, status, cand, reasons, cfg)

    if status != "ok":
        log.warning("%s %s / %s: %s", _STATUS_LABEL[status], stat.theme_slug,
                    stat.source_id, "; ".join(reasons))
        if not dry_run:
            stat.last_refreshed = now   # SWR: value kept, attempt recorded
            db.session.commit()
        return status

    fetched = {
        "latest_value":   cand["value"],
        "period_label":   cand.get("period_label"),
        "period_start":   cand.get("period_start"),
        "period_end":     cand.get("period_end"),
        "release_date":   cand.get("release_date"),
        "release_url":    cand.get("release_url"),
        "source_wording": cand.get("source_wording"),
    }
    log.info("OK %s / %s → %s %s (%s)", stat.theme_slug, stat.source_id,
             fetched["latest_value"], (cfg or {}).get("unit") or stat.unit, fetched["period_label"])
    if dry_run:
        return "ok"

    # Write StatObservation (Phase 1.5). Pre-cutoff periods are rejected inside
    # _write_observation; the legacy mirror below still updates.
    _write_observation(db, stat, fetched, now)

    # Mirror to legacy HeadlineStat fields.
    # [TEMPORARY — remove this block when admin UI migrates to StatObservation]
    stat.latest_value             = fetched["latest_value"]
    stat.unit                     = (cfg or {}).get("unit") or stat.unit
    stat.period_label             = fetched["period_label"]
    stat.release_date             = fetched["release_date"]
    stat.source_url               = fetched["release_url"] or stat.source_url
    stat.source_wording           = fetched["source_wording"]
    stat.plain_english            = None   # Phase 1.5: plain English deferred
    stat.plain_english_generated_at = None
    stat.rewrite_model            = None
    stat.last_refreshed           = now
    stat.last_success             = now
    db.session.commit()
    return "ok"


def run_refresh(theme_slug: str | None = None, dry_run: bool = False) -> dict:
    """Main entry point — refresh all rows or rows matching theme_slug. Returns counts."""
    from flask_app import app
    from hansard_archive.models import HeadlineStat

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        log.warning("GEMINI_API_KEY not set — structured sources and regex patterns still run; "
                    "the Gemini fallback for changed bulletin wording is disabled")

    counts = {"ok": 0, "needs_review": 0, "failed": 0, "skipped": 0}
    with app.app_context():
        q = HeadlineStat.query
        if theme_slug:
            q = q.filter_by(theme_slug=theme_slug)
        rows = q.order_by(HeadlineStat.theme_slug, HeadlineStat.id).all()

        if not rows:
            log.warning("No rows found%s", f" for theme {theme_slug!r}" if theme_slug else "")
            return counts

        for stat in rows:
            counts[_refresh_stat(stat, gemini_key, dry_run=dry_run)] += 1
            time.sleep(0.5)   # polite rate limit between requests

        log.info("Refresh complete: ok=%d needs_review=%d failed=%d skipped=%d%s",
                 counts["ok"], counts["needs_review"], counts["failed"], counts["skipped"],
                 " (DRY RUN — nothing written)" if dry_run else "")
    return counts


# ── Flask CLI registration ────────────────────────────────────────────────────

def register_stats_cli(app) -> None:
    """Call from flask_app.py: register_stats_cli(app)"""
    import click

    @app.cli.group("stats")
    def stats_group():
        """Cross-government statistics refresh commands."""

    @stats_group.command("refresh-all")
    @click.option("--dry-run", is_flag=True, help="Fetch and log but do not write to DB")
    def cli_refresh_all(dry_run):
        """Refresh all headline_stat rows (ONS + govuk_bulletin)."""
        run_refresh(theme_slug=None, dry_run=dry_run)

    @stats_group.command("refresh-theme")
    @click.argument("slug")
    @click.option("--dry-run", is_flag=True)
    def cli_refresh_theme(slug, dry_run):
        """Refresh headline_stat rows for a single theme slug."""
        run_refresh(theme_slug=slug, dry_run=dry_run)

    from scripts.check_stats_state import register_stats_check_cli
    register_stats_check_cli(stats_group)


# ── Direct invocation ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refresh headline_stat rows")
    parser.add_argument("--theme", help="Restrict to one theme slug")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = parser.parse_args()
    run_refresh(theme_slug=args.theme, dry_run=args.dry_run)
