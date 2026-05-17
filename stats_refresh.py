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

If source_wording changes after a successful fetch, plain_english is
regenerated using Gemini Flash and rewrite_model is updated.

Runs: weekly Railway cron, Mondays 06:00 UTC (stats-cron service).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

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


def _fetch_ons(stat) -> dict:
    """
    Fetch latest observation from ONS Beta API for an ons_timeseries row.
    Returns dict with keys: latest_value, unit, period_label, release_date,
    source_wording (we don't get verbatim from ONS, so we synthesise one).
    Returns None on any failure.
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

            unit = stat.unit or desc.get("unit") or ""
            period_label = f"{label} ({freq.rstrip('s')})"
            source_wording = (
                f"{desc.get('title', stat.display_label)} was {value}{unit} in {label}."
            )
            return {
                "latest_value":  value,
                "unit":          unit,
                "period_label":  period_label,
                "release_date":  release_date,
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

    return {
        "latest_value":   str(extracted["figure"]).strip(),
        "unit":           str(extracted["unit"]).strip(),
        "period_label":   str(extracted["period_label"]).strip(),
        "release_date":   release_date,
        "source_wording": str(extracted["verbatim"]).strip(),
    }


# ── Plain-English rewrite ─────────────────────────────────────────────────────

def _generate_rewrite(stat, fetched: dict, gemini_key: str) -> str | None:
    """Call Gemini Flash to rewrite source_wording as plain English."""
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


# ── Core refresh logic ────────────────────────────────────────────────────────

def _refresh_stat(stat, gemini_key: str, dry_run: bool = False) -> str:
    """
    Refresh a single HeadlineStat row.
    Returns: 'ok' | 'skipped' | 'failed'
    """
    from hansard_archive.models import HeadlineStat  # noqa: import inside fn for CLI compat
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

    wording_changed = (fetched.get("source_wording") or "") != (stat.source_wording or "")

    stat.latest_value   = fetched.get("latest_value")
    stat.unit           = fetched.get("unit") or stat.unit
    stat.period_label   = fetched.get("period_label")
    stat.release_date   = fetched.get("release_date")
    stat.source_wording = fetched.get("source_wording")
    stat.last_success   = now

    if wording_changed and fetched.get("source_wording"):
        rewrite = _generate_rewrite(stat, fetched, gemini_key)
        if rewrite:
            stat.plain_english = rewrite
            stat.plain_english_generated_at = now
            stat.rewrite_model = _REWRITE_MODEL

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
