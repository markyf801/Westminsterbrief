"""
Westminster Brief — Phase 2 Statistics Refresh

Flask CLI group: `flask stats refresh-all` / `flask stats refresh-theme <slug>`
Also invocable directly: `python stats_refresh.py [--theme <slug>]`

Refresh logic:
- ons_timeseries  → ONS Beta API /v1/data?uri=<timeseries path>
- govuk_bulletin  → fetch bulletin page → Gemini Flash-Lite extraction
- manual          → skipped (human updates rows directly via admin route)

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
    # Also matches "2023/2024"
    m = re.match(r'^(\d{4})[-/](\d{2,4})$', label)
    if m:
        y = int(m.group(1))
        return date(y, 4, 1), date(y + 1, 3, 31)

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

_EXTRACT_PROMPT = """\
You are extracting a statistical figure from a government bulletin.
Return ONLY a JSON object with these keys:
  "figure"       — the headline statistic as a string (e.g. "3.2" or "£12.4 billion")
  "unit"         — the unit (e.g. "%" or "£bn" or "dwellings")
  "period_label" — the time period the figure covers (e.g. "Q3 2024" or "2023-24")
  "release_date" — the publication/release date as YYYY-MM-DD or empty string
  "verbatim"     — one sentence from the bulletin that contains the figure, copied verbatim

If you cannot find a clear headline figure, return: {{"error": "no figure found"}}

Bulletin content (first 4000 characters):
{content}"""

# ── ONS timeseries handler ────────────────────────────────────────────────────

def _ons_timeseries_path_from_url(source_url: str) -> str:
    """
    Derive the ONS timeseries URI path from the source_url stored in the DB.
    Pattern: https://www.ons.gov.uk/{path} → /{path}
    """
    if "ons.gov.uk" in source_url:
        return source_url.replace("https://www.ons.gov.uk", "")
    return source_url


def _fetch_ons(stat) -> dict | None:
    """
    Fetch latest observation from ONS Beta API for an ons_timeseries row.

    Returns a dict with keys:
      latest_value, unit, period_label, period_start, period_end,
      release_date, release_url, source_wording
    Returns None on any failure.

    period_start / period_end: derived via _parse_ons_period(). When parsing
    fails both are None — _write_observation() will log a warning and skip
    the StatObservation write while legacy HeadlineStat fields still update.

    source_wording is synthesised because the ONS Beta API does not provide
    a verbatim sentence; it is a machine-generated summary, not verbatim text.
    """
    uri_path = _ons_timeseries_path_from_url(stat.source_url)
    try:
        r = requests.get(_ONS_DATA_URL, params={"uri": uri_path}, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("ONS fetch failed for %s: %s", stat.source_id, exc)
        return None

    # Extract latest observation from months / quarters / years (in preference order)
    desc = data.get("description") or {}
    for freq in ("months", "quarters", "years"):
        obs = data.get(freq) or []
        if obs:
            latest = obs[-1]
            value  = str(latest.get("value") or latest.get("v", "")).strip()
            if not value:
                continue
            label  = str(latest.get("label") or latest.get("year", "")).strip()
            release_raw = (desc.get("releaseDate") or "")[:10]
            try:
                release_date = datetime.strptime(release_raw, "%Y-%m-%d").date() if release_raw else None
            except ValueError:
                release_date = None

            unit         = stat.unit or desc.get("unit") or ""
            period_label = f"{label} ({freq.rstrip('s')})"
            source_wording = (
                f"{desc.get('title', stat.display_label)} was {value}{unit} in {label}."
            )
            period_start, period_end = _parse_ons_period(label, freq)
            if period_start is None:
                log.warning("ONS: could not parse period from label=%r freq=%s for %s",
                            label, freq, stat.source_id)

            return {
                "latest_value":  value,
                "unit":          unit,
                "period_label":  period_label,
                "period_start":  period_start,
                "period_end":    period_end,
                "release_date":  release_date,
                "release_url":   stat.source_url,  # ONS: timeseries URL is the stable landing page
                "source_wording": source_wording,
            }

    log.warning("ONS: no observations found for %s at %s", stat.source_id, uri_path)
    return None


# ── GOV.UK bulletin handler ───────────────────────────────────────────────────

def _fetch_govuk_bulletin(stat, gemini_key: str) -> dict | None:
    """
    Fetch the bulletin landing page and extract the headline figure using
    Gemini Flash-Lite. Returns same dict shape as _fetch_ons or None on failure.
    """
    try:
        r = requests.get(stat.source_url, timeout=_TIMEOUT,
                         headers={"User-Agent": "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"})
        r.raise_for_status()
        content = r.text[:4000]
    except Exception as exc:
        log.warning("govuk_bulletin fetch failed for %s: %s", stat.source_id, exc)
        return None

    prompt = _EXTRACT_PROMPT.format(content=content)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
        },
    }
    try:
        gr = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_EXTRACT_MODEL}:generateContent",
            params={"key": gemini_key},
            json=payload,
            timeout=60,
        )
        gr.raise_for_status()
        raw = gr.json()
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        extracted = json.loads(text)
    except Exception as exc:
        log.warning("govuk_bulletin Gemini extraction failed for %s: %s", stat.source_id, exc)
        return None

    if "error" in extracted:
        log.warning("govuk_bulletin extraction: no figure for %s: %s", stat.source_id, extracted["error"])
        return None

    required = {"figure", "unit", "period_label", "verbatim"}
    if not required.issubset(extracted):
        log.warning("govuk_bulletin extraction: malformed JSON for %s: %s", stat.source_id, extracted)
        return None

    release_raw = (extracted.get("release_date") or "")[:10]
    try:
        release_date = datetime.strptime(release_raw, "%Y-%m-%d").date() if release_raw else None
    except ValueError:
        release_date = None

    period_label = str(extracted["period_label"]).strip()
    period_start, period_end = _parse_govuk_period(period_label)
    if period_start is None:
        log.warning("govuk_bulletin: could not parse period from label=%r for %s",
                    period_label, stat.source_id)

    return {
        "latest_value":   str(extracted["figure"]).strip(),
        "unit":           str(extracted["unit"]).strip(),
        "period_label":   period_label,
        "period_start":   period_start,
        "period_end":     period_end,
        "release_date":   release_date,
        "release_url":    stat.source_url,  # gov.uk bulletin URL is stable per stat
        "source_wording": str(extracted["verbatim"]).strip(),
    }


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

def _refresh_stat(stat, gemini_key: str, dry_run: bool = False) -> str:
    """
    Refresh a single HeadlineStat row.
    Returns: 'ok' | 'skipped' | 'failed'

    On success:
    1. Write or update a StatObservation row (Phase 1.5).
    2. Mirror fetched values to legacy HeadlineStat fields so the admin UI
       and existing templates continue to work without changes.
       [TEMPORARY mirror — remove when admin UI migrates to StatObservation]
    3. Explicitly NULL out plain_english / plain_english_generated_at /
       rewrite_model on both the observation (always NULL) and the legacy
       HeadlineStat mirror (Phase 1.5: plain English generation deferred).

    SWR: on failure, only last_refreshed is updated; no field changes.
    """
    from flask_app import db

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if stat.source_type == "manual":
        log.info("SKIP (manual) %s / %s", stat.theme_slug, stat.source_id)
        return "skipped"

    if stat.source_type == "ons_timeseries":
        fetched = _fetch_ons(stat)
    elif stat.source_type == "govuk_bulletin":
        fetched = _fetch_govuk_bulletin(stat, gemini_key)
    else:
        log.warning("Unknown source_type %s for %s", stat.source_type, stat.source_id)
        fetched = None

    # SWR: always update last_refreshed regardless of success
    stat.last_refreshed = now

    if fetched is None:
        log.warning("FAIL %s / %s", stat.theme_slug, stat.source_id)
        if not dry_run:
            db.session.commit()
        return "failed"

    # Write StatObservation (Phase 1.5). Rejection (pre-cutoff, no period) is
    # logged inside _write_observation; we still proceed to mirror legacy fields.
    #
    # release_url note: StatObservation.release_url is currently set to
    # stat.source_url for both ons_timeseries and govuk_bulletin sources because
    # neither source provides a per-release URL at this stage:
    #   - ONS Beta API returns metadata for the timeseries as a whole; individual
    #     release URLs are not surfaced in /v1/data responses.
    #   - govuk_bulletin scrapes the bulletin landing page (stat.source_url);
    #     we do not yet capture the URL of the specific release edition.
    # This is a known temporary state. In a future phase:
    #   - govuk_bulletin: the specific bulletin edition URL will be captured
    #     from the bulletin's canonical link/redirect and stored per observation.
    #   - ONS: per-release dataset URLs may be derivable from the API response
    #     (e.g. the release calendar or a dataset endpoint) once that is explored.
    # Until then, release_url == source_url for all machine-fetched observations.
    # producer_url (HeadlineStat) is the correct field for the producer home page;
    # release_url is intended to point at the specific edition — they should
    # diverge once per-release URL capture is implemented.
    if not dry_run:
        _write_observation(db, stat, fetched, now)

    # Mirror to legacy HeadlineStat fields.
    # [TEMPORARY — remove this block when admin UI migrates to StatObservation]
    stat.latest_value             = fetched.get("latest_value")
    stat.unit                     = fetched.get("unit") or stat.unit
    stat.period_label             = fetched.get("period_label")
    stat.release_date             = fetched.get("release_date")
    stat.source_url               = fetched.get("release_url") or stat.source_url
    stat.source_wording           = fetched.get("source_wording")
    stat.plain_english            = None   # Phase 1.5: plain English deferred
    stat.plain_english_generated_at = None
    stat.rewrite_model            = None
    stat.last_success             = now

    log.info("OK %s / %s → %s %s (%s)",
             stat.theme_slug, stat.source_id,
             stat.latest_value, stat.unit, stat.period_label)

    if not dry_run:
        db.session.commit()
    return "ok"


def run_refresh(theme_slug: str | None = None, dry_run: bool = False) -> None:
    """Main entry point — refresh all rows or rows matching theme_slug."""
    from flask_app import app, db
    from hansard_archive.models import HeadlineStat

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set — cannot run refresh")
        sys.exit(1)

    with app.app_context():
        q = HeadlineStat.query
        if theme_slug:
            q = q.filter_by(theme_slug=theme_slug)
        rows = q.order_by(HeadlineStat.theme_slug, HeadlineStat.id).all()

        if not rows:
            log.warning("No rows found%s", f" for theme {theme_slug!r}" if theme_slug else "")
            return

        n_ok = n_fail = n_skip = 0
        for stat in rows:
            result = _refresh_stat(stat, gemini_key, dry_run=dry_run)
            if result == "ok":     n_ok   += 1
            elif result == "failed": n_fail += 1
            else:                    n_skip += 1
            time.sleep(0.5)   # polite rate limit between requests

        log.info("Refresh complete: ok=%d failed=%d skipped=%d%s",
                 n_ok, n_fail, n_skip, " (DRY RUN)" if dry_run else "")


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


# ── Direct invocation ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refresh headline_stat rows")
    parser.add_argument("--theme", help="Restrict to one theme slug")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = parser.parse_args()
    run_refresh(theme_slug=args.theme, dry_run=args.dry_run)
