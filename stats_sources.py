"""
Westminster Brief — headline statistic sources.

Per-stat source configuration (STAT_SOURCES) and the fetchers that read each
source. Every fetcher returns a *candidate* dict; nothing here writes to the
database. stats_refresh.validate_candidate() decides whether a candidate is
accepted.

Candidate dict keys:
  value          str   value as it will be stored (e.g. "71,200", "55.8")
  period_label   str   human period label (e.g. "2024/25", "Q2 2026")
  period_start   date | None
  period_end     date | None
  release_date   date | None
  release_url    str   URL of the specific release / data file read
  source_wording str   public sentence shown under "Source wording"
  evidence       str   what the value was read from (verbatim sentence, data row)
  evidence_kind  "text" | "structured" | "derived"
  evidence_value str   (structured) the number exactly as it appears in evidence
  scale          float (structured) value == evidence_value * scale, rounded
  source_text    str   (text) the full cleaned source text the evidence must be in
  review         list[str]  reasons a human must check before it can be accepted

Strategies:
  ons_timeseries  ONS Beta API timeseries (CDID)
  ons_bulletin    ONS Beta API bulletin JSON → text extraction
  govuk_text      latest edition of a GOV.UK series → HTML publication → text
  govuk_csv       CSV attachment on a GOV.UK page, filtered rows
  govuk_ods       ODS attachment on a GOV.UK page, table cell
  ees_api         DfE Explore Education Statistics public API
  nhs_ae_csv      NHS England monthly A&E provider CSV (computed England figure)
  manual          not fetched; reason printed on every run

Text extraction (ons_bulletin, govuk_text) first tries the per-stat regex
patterns; only if none match does stats_refresh fall back to Gemini, and the
Gemini answer goes through the same validation.
"""

from __future__ import annotations

import calendar
import csv
import io
import json
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests

log = logging.getLogger("stats_refresh")

_TIMEOUT = 30
_MAX_BYTES = 25 * 1024 * 1024
_UA = {"User-Agent": "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"}

ONS_DATA_URL = "https://api.beta.ons.gov.uk/v1/data"
GOVUK_CONTENT_API = "https://www.gov.uk/api/content"
GOVUK_SEARCH_API = "https://www.gov.uk/api/search.json"
EES_API = "https://api.education.gov.uk/statistics/v1"

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


class SourceError(Exception):
    """A source could not be read or did not have the expected shape."""


# ── Per-stat configuration ────────────────────────────────────────────────────
#
# Keyed by HeadlineStat.source_id. Common keys:
#   strategy              see module docstring
#   unit                  canonical unit stored on HeadlineStat (never taken from a model)
#   min / max             sanity bounds; a value outside them is rejected
#   measure               exact description of the figure (used in wording and prompt)
#   max_period_age_days   if period_end is older than this, the series may have
#                         moved or stopped: NEEDS-REVIEW
#   source_url            canonical "view source" page for the stat
#
# A stat with no entry here falls back to HeadlineStat.extraction_config (JSON
# with the same keys); with neither, nothing can be validated and the stat is
# reported NEEDS-REVIEW.

STAT_SOURCES: dict[str, dict] = {
    # ── ONS timeseries ────────────────────────────────────────────────────────
    "IHYQ": {
        "strategy": "ons_timeseries",
        # qna (Quarterly National Accounts) carries revisions; pn2 (first
        # estimate) was left stale after the 30 Sept 2026 QNA release.
        "ons_path": "/economy/grossdomesticproductgdp/timeseries/ihyq/qna",
        "unit": "%", "min": -10, "max": 10,
        "max_period_age_days": 200,
    },
    "MGSX": {
        "strategy": "ons_timeseries",
        "ons_path": "/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms",
        "unit": "%", "min": 2, "max": 15,
        "max_period_age_days": 120,
    },
    "L55O": {
        "strategy": "ons_timeseries",
        "ons_path": "/economy/inflationandpriceindices/timeseries/l55o/mm23",
        "unit": "%", "min": -3, "max": 15,
        "max_period_age_days": 100,
    },
    "DZLS": {
        "strategy": "ons_timeseries",
        "ons_path": "/economy/governmentpublicsectorandtaxes/publicsectorfinance/timeseries/dzls/pusf",
        "unit": "£m", "min": -40000, "max": 60000,
        "max_period_age_days": 100,
    },
    "K22A": {
        "strategy": "ons_timeseries",
        "ons_path": "/economy/economicoutputandproductivity/output/timeseries/k22a/diop",
        "unit": "index (2022=100)", "min": 60, "max": 140,
        "max_period_age_days": 100,
    },

    # ── ONS bulletin (text) ───────────────────────────────────────────────────
    "ons-crime-survey": {
        "strategy": "ons_bulletin",
        "ons_path": "/peoplepopulationandcommunity/crimeandjustice/bulletins/crimeinenglandandwales/latest",
        "measure": "Estimated incidents of CSEW headline crime, England and Wales",
        "unit": "million incidents", "min": 3, "max": 20,
        "patterns": [
            r"(?P<value>\d{1,2}(?:\.\d)?) million incidents of CSEW headline crime",
        ],
        "evidence_must_match": r"million incidents",
        "unit_aliases": r"million",
        "max_period_age_days": 250,
    },

    # ── GOV.UK latest edition → HTML publication (text) ───────────────────────
    "dclg-housebuilding": {
        "strategy": "govuk_text",
        "collection": "/government/collections/net-supply-of-housing",
        "search_q": "Housing supply: net additional dwellings, England",
        "title_regex": r"^Housing supply: net additional dwellings, England: (?P<year>\d{4}) to \d{4}$",
        "html_title_regex": r"^Housing supply: net additional dwellings, England: \d{4} to \d{4}$",
        "measure": "Net additional dwellings, England",
        "unit": "dwellings", "min": 50000, "max": 400000,
        "patterns": [
            r"amounted to (?P<value>\d{2,3},\d{3}) net additional dwellings in (?P<period>\d{4}-\d{2})",
        ],
        "evidence_must_match": r"net additional dwellings",
        "unit_aliases": r"dwelling|home",
        "max_period_age_days": 650,
    },
    "dft-road-casualties": {
        "strategy": "govuk_text",
        "collection": "/government/collections/road-safety-statistics",
        "search_q": "Reported road casualties Great Britain final results",
        "title_regex": (r"^Reported road casualties Great Britain,? "
                        r"(?:annual report|final results):? (?P<year>\d{4})$"),
        "html_title_regex": r"Introduction and main findings",
        "measure": "Reported road casualties killed or seriously injured (KSI), Great Britain",
        "unit": "people", "min": 10000, "max": 60000,
        "patterns": [
            r"(?P<value>\d{2},\d{3}) killed or seriously injured \(\s*KSI\s*\) casualties",
            r"killed or seriously injured \(\s*KSI\s*\) road casualties in Great Britain was "
            r"(?P<value>\d{2},\d{3}) in (?P<period>\d{4})",
        ],
        "period_from_title": "year",
        "evidence_must_match": r"killed or seriously injured",
        "unit_aliases": r"casualt|people|KSI|killed",
        "max_period_age_days": 600,
    },

    # ── GOV.UK CSV / ODS attachments (structured) ─────────────────────────────
    "defra-farm-income": {
        "strategy": "govuk_csv",
        "page": "/government/statistics/farm-business-income",
        "attachment_title_regex": r"Farm Income Dataset \(machine readable",
        "filters": {"typology": "2017SO", "prices": "Current",
                    "fbs_variable": "farm.business.income", "farm_type": "All Types"},
        "period_column": "survey_year",
        "value_column": "value",
        "period_type": "financial_year",
        "measure": "Average Farm Business Income, all farm types, England (current prices)",
        "unit": "£ per farm", "min": 0, "max": 250000,
        "max_period_age_days": 650,
    },
    "desnz-renewables-share": {
        "strategy": "govuk_ods",
        "page": "/government/statistics/energy-trends-section-6-renewables",
        "attachment_title_regex": r"renewables quarterly tables \(ODS\)",
        "sheet": "6_1",
        "section_regex": r"^SHARES OF ELECTRICITY GENERATED",
        "row_regex": r"^All renewables$",
        "column": "last",
        "period_type": "ordinal_quarter",
        "decimals": 1,
        "measure": "Renewables' share of UK electricity generation",
        "unit": "%", "min": 10, "max": 90,
        "max_period_age_days": 200,
    },
    "fcdo-oda-spending": {
        "strategy": "govuk_ods",
        "collection": "/government/collections/statistics-on-international-development",
        "search_q": "Statistics on International Development UK ODA spend",
        "title_regex": (r"^Statistics on International Development: (?:final|provisional) UK "
                        r"(?:aid|ODA|Official Development Assistance) spend (?P<year>\d{4})$"),
        "prefer_regex": r": final ",
        "attachment_title_regex": r"^Tables: Statistics on International Development",
        "sheet": "Table_1",
        "header_regex": r"^(?P<year>\d{4})\s+ODA £ million$",
        "column": "latest_year",
        "period_type": "calendar_year",
        "scale": 0.001,           # £ million → £ billion
        "decimals": 1,
        "measure": "UK Official Development Assistance (ODA) spend",
        "unit": "£bn", "min": 5, "max": 25,
        "max_period_age_days": 560,
    },

    # ── DfE Explore Education Statistics API ──────────────────────────────────
    "dfe-gcse-results": {
        "strategy": "ees_api",
        "publication_id": "c8756008-ed50-4632-9b96-01b5ca002a43",   # Key stage 4 performance
        "dataset_title": "National characteristics summary data",
        "indicator_column": "engmath_94_percent",
        "filter_column": "breakdown_topic",
        "filter_label": "Total",
        "geographic_level": "NAT",
        "measure": ("Pupils achieving grade 4 or above in English and maths GCSEs, "
                    "state-funded schools, England"),
        "unit": "%", "min": 40, "max": 90,
        "source_url": "https://explore-education-statistics.service.gov.uk/find-statistics/key-stage-4-performance",
        "max_period_age_days": 450,
    },

    # ── NHS England A&E monthly CSV (computed) ────────────────────────────────
    "nhs-ae-performance": {
        "strategy": "nhs_ae_csv",
        "index_url": "https://www.england.nhs.uk/statistics/statistical-work-areas/ae-waiting-times-and-activity/",
        "measure": ("A&E attendances (all types, including booked appointments) admitted, "
                    "transferred or discharged within 4 hours, England"),
        "unit": "%", "min": 40, "max": 100,
        "max_period_age_days": 80,
    },

    # ── Manual: no reliable machine-readable source ───────────────────────────
    "mod-defence-spending-gdp": {
        "strategy": "manual",
        "reason": ("No GOV.UK statistic publishes defence spending as % of GDP; the figure is "
                   "NATO's. The configured page is the 2015 'UK defence in numbers'. "
                   "Update by hand from NATO's defence expenditure release."),
    },
    "desnz-energy-prices": {
        "strategy": "manual",
        "reason": ("The Ofgem page now redirects to an explainer with no annual figure; Ofgem "
                   "publishes unit rates, not a single typical bill. Choose a measure and update by hand."),
    },
    "dwp-benefit-claimants": {
        "strategy": "manual",
        "reason": ("'People on key DWP benefits' has no single published measure; the configured "
                   "collection holds forecast expenditure tables. Choose a measure (e.g. Universal "
                   "Credit claimants) and update by hand."),
    },
}


def get_stat_config(stat) -> dict | None:
    """Code config first, then HeadlineStat.extraction_config JSON, else None."""
    cfg = STAT_SOURCES.get(stat.source_id)
    if cfg is not None:
        return cfg
    raw = getattr(stat, "extraction_config", None)
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("extraction_config for %s is not valid JSON", stat.source_id)
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


# ── HTTP and text helpers ─────────────────────────────────────────────────────

def http_get(url: str, params: dict | None = None) -> requests.Response:
    try:
        r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        raise SourceError(f"GET {url} failed: {exc}") from exc
    if len(r.content or b"") > _MAX_BYTES:
        raise SourceError(f"GET {url}: response larger than {_MAX_BYTES} bytes")
    return r


def http_json(url: str, params: dict | None = None) -> dict:
    r = http_get(url, params)
    try:
        return r.json()
    except ValueError as exc:
        raise SourceError(f"GET {url}: response is not JSON") from exc


def normalise_ws(s: str) -> str:
    """Collapse whitespace and normalise characters that differ between copies."""
    s = (s or "").replace(" ", " ").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "br", "dt", "dd",
               "caption", "figcaption", "blockquote", "div", "section", "table"]


def html_to_text(html: str) -> str:
    """
    Visible text of an HTML page or fragment (scripts, styles, nav removed).
    One line per block element, so headings never run into the next sentence.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "head",
                     "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("main") or soup.body or soup
    for tag in root.find_all(_BLOCK_TAGS):
        tag.append("\n")
    lines = (normalise_ws(line) for line in root.get_text(" ").split("\n"))
    return "\n".join(line for line in lines if line)


def sentence_around(text: str, start: int, end: int, cap: int = 400) -> str:
    """The sentence (or bullet clause) containing text[start:end], verbatim."""
    lo = max(0, start - cap)
    left = text[lo:start]
    # Boundary = just after the last newline or sentence/clause end before the match.
    cuts = [left.rfind("\n") + 1] + [m.end() for m in re.finditer(r"[.!?;]\s", left)]
    s = lo + max(cuts)
    right = text[end:end + cap]
    m = re.search(r"[.!?](?=\s|$)|[;\n]", right)
    if m is None:
        e = end + len(right)
    else:
        e = end + (m.start() if m.group() in ";\n" else m.end())
    return normalise_ws(text[s:e])


def parse_number(value: str) -> float | None:
    """'71,200' → 71200.0; '-0.4' → -0.4; anything else (units, words, '2015 None') → None."""
    v = (value or "").strip()
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", v):
        return None
    return float(v.replace(",", ""))


def to_float(value: str) -> float | None:
    """Like parse_number but also accepts spreadsheet forms such as '5.6E-3'."""
    try:
        return float((value or "").replace(",", ""))
    except ValueError:
        return None


def format_number(x: float, decimals: int) -> str:
    return f"{x:,.{decimals}f}" if abs(x) >= 1000 else f"{x:.{decimals}f}"


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def period_bounds(label: str, period_type: str) -> tuple[date | None, date | None]:
    """Period dates for labels whose type the source tells us."""
    label = (label or "").strip()
    try:
        if period_type == "financial_year":           # "2024/25", "2024-25"
            m = re.fullmatch(r"(\d{4})[/-](\d{2}|\d{4})", label)
            if m:
                y = int(m.group(1))
                return date(y, 4, 1), date(y + 1, 3, 31)
        elif period_type == "academic_year":          # "2025/26", "2025/2026"
            m = re.fullmatch(r"(\d{4})[/-](\d{2}|\d{4})", label)
            if m:
                y = int(m.group(1))
                return date(y, 9, 1), date(y + 1, 8, 31)
        elif period_type == "calendar_year":
            m = re.fullmatch(r"(\d{4})", label)
            if m:
                y = int(m.group(1))
                return date(y, 1, 1), date(y, 12, 31)
        elif period_type == "ordinal_quarter":        # "2026 2nd quarter"
            m = re.fullmatch(r"(\d{4}) ([1-4])(?:st|nd|rd|th) quarter", label, re.I)
            if m:
                y, q = int(m.group(1)), int(m.group(2))
                return date(y, 3 * q - 2, 1), _month_end(y, 3 * q)
        elif period_type == "month":                  # "September 2026"
            m = re.fullmatch(r"([A-Za-z]+) (\d{4})", label)
            if m and m.group(1).lower() in _MONTHS:
                y, mo = int(m.group(2)), _MONTHS[m.group(1).lower()]
                return date(y, mo, 1), _month_end(y, mo)
    except ValueError:
        pass
    return None, None


def _iso_date(raw: str | None) -> date | None:
    try:
        return datetime.strptime((raw or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_long_date(raw: str | None) -> date | None:
    """'17 November 2026' → date; anything else (e.g. 'To be announced') → None."""
    try:
        return datetime.strptime((raw or "").strip(), "%d %B %Y").date()
    except ValueError:
        return None


def next_release_check(next_release: str | None, today: date, grace_days: int = 2) -> str | None:
    """A review reason if the source's own 'next release' date has already passed."""
    nxt = _parse_long_date(next_release)
    if nxt and (today - nxt).days > grace_days:
        return (f"source says next release was {nxt.isoformat()}, but the copy read has not been "
                "updated since; it may not be the latest release")
    return None


# ── ONS ───────────────────────────────────────────────────────────────────────

def _ons_period(label: str, freq: str) -> tuple[date | None, date | None]:
    from stats_refresh import _parse_ons_period
    return _parse_ons_period(label, freq)


def fetch_ons_timeseries(stat, cfg: dict, today: date) -> dict:
    path = cfg.get("ons_path") or stat.source_url.replace("https://www.ons.gov.uk", "")
    data = http_json(ONS_DATA_URL, {"uri": path})
    desc = data.get("description") or {}
    for freq in ("months", "quarters", "years"):
        obs = data.get(freq) or []
        if not obs:
            continue
        latest = obs[-1]
        value = str(latest.get("value") or "").strip()
        if not value:
            continue
        label = str(latest.get("label") or "").strip()
        start, end = _ons_period(label, freq)
        review = []
        stale = next_release_check(desc.get("nextRelease"), today)
        if stale:
            review.append(stale)
        unit = cfg.get("unit") or stat.unit or ""
        title = desc.get("title") or stat.display_label
        evidence = (f"ONS {desc.get('cdid', stat.source_id)} ({path.rsplit('/', 1)[-1]}), "
                    f"{freq} '{label}': value={value}")
        return {
            "value": value,
            "period_label": f"{label} ({freq.rstrip('s')})",
            "period_start": start, "period_end": end,
            "release_date": _iso_date(desc.get("releaseDate")),
            "release_url": "https://www.ons.gov.uk" + path,
            "source_wording": f"{title} was {with_unit(value, unit)} in {label}.",
            "evidence": evidence, "evidence_kind": "structured",
            "evidence_value": value, "scale": 1,
            "review": review,
        }
    raise SourceError(f"ONS: no observations at {path}")


def fetch_ons_bulletin_text(stat, cfg: dict, today: date) -> dict:
    """Clean text of the latest ONS bulletin plus release metadata."""
    data = http_json(ONS_DATA_URL, {"uri": cfg["ons_path"]})
    desc = data.get("description") or {}
    parts = [desc.get(f"headline{i}") or "" for i in (1, 2, 3)]
    for sec in data.get("sections") or []:
        parts.append(sec.get("title") or "")
        parts.append(sec.get("markdown") or "")
    lines = [normalise_ws(line) for p in parts for line in p.splitlines()]
    text = "\n".join(line for line in lines if line)
    review = []
    if desc.get("latestRelease") is not True:
        review.append("ONS does not mark this bulletin as the latest release")
    stale = next_release_check(desc.get("nextRelease"), today)
    if stale:
        review.append(stale)
    edition = normalise_ws(desc.get("edition") or "")
    from stats_refresh import _parse_govuk_period
    start, end = _parse_govuk_period(edition)
    return {
        "text": text,
        "release_date": _iso_date(desc.get("releaseDate")),
        "release_url": "https://www.ons.gov.uk" + (data.get("uri") or cfg["ons_path"]),
        "period_label": edition, "period_start": start, "period_end": end,
        "review": review,
    }


# ── GOV.UK ────────────────────────────────────────────────────────────────────

def govuk_content(path: str) -> dict:
    return http_json(GOVUK_CONTENT_API + path)


def latest_govuk_edition(cfg: dict) -> dict:
    """
    Newest edition of a GOV.UK statistics series.

    Candidates come from the series collection page *and* a GOV.UK search, so a
    new edition that the department has not yet added to the collection is
    still found. Ranked by the year in the title, then by prefer_regex (e.g.
    'final' beats 'provisional' for the same year), then by publication time.
    """
    title_re = re.compile(cfg["title_regex"], re.I)
    prefer_re = re.compile(cfg["prefer_regex"], re.I) if cfg.get("prefer_regex") else None
    cands: dict[str, dict] = {}
    if cfg.get("collection"):
        coll = govuk_content(cfg["collection"])
        for d in (coll.get("links") or {}).get("documents") or []:
            cands[d.get("base_path", "")] = {"title": d.get("title", ""),
                                             "ts": d.get("public_updated_at", "")}
    if cfg.get("search_q"):
        try:
            res = http_json(GOVUK_SEARCH_API, {
                "q": cfg["search_q"], "count": 40,
                "fields": "title,link,public_timestamp",
            })
            for d in res.get("results") or []:
                cands.setdefault(d.get("link", ""), {"title": d.get("title", ""),
                                                     "ts": d.get("public_timestamp", "")})
        except SourceError as exc:
            log.warning("GOV.UK search failed (collection only): %s", exc)
    ranked = []
    for path, info in cands.items():
        m = title_re.search(normalise_ws(info["title"]))
        # Announcements are future releases with no content yet.
        if not m or not path.startswith("/government/") or "/announcements/" in path:
            continue
        year = int(m.group("year")) if "year" in m.groupdict() and m.group("year") else 0
        pref = 1 if prefer_re and prefer_re.search(info["title"]) else 0
        ranked.append(((year, pref, info["ts"]), path, info["title"], m))
    if not ranked:
        raise SourceError(f"no edition matching {cfg['title_regex']!r}")
    ranked.sort(reverse=True)
    _, path, title, match = ranked[0]
    content = govuk_content(path)
    if content.get("withdrawn_notice"):
        raise SourceError(f"latest edition {path} is withdrawn")
    content["_title_match"] = match
    content["_path"] = path
    return content


def find_attachment(content: dict, title_regex: str) -> dict:
    rx = re.compile(title_regex, re.I)
    for att in (content.get("details") or {}).get("attachments") or []:
        if rx.search(normalise_ws(att.get("title", ""))):
            return att
    raise SourceError(f"no attachment matching {title_regex!r} on {content.get('base_path')}")


def _abs_url(url: str) -> str:
    return url if url.startswith("http") else "https://www.gov.uk" + url


def fetch_govuk_edition_text(stat, cfg: dict, today: date) -> dict:
    edition = latest_govuk_edition(cfg)
    if cfg.get("html_title_regex"):
        att = find_attachment(edition, cfg["html_title_regex"])
        html_path = att.get("url", "")
        if html_path.startswith("https://www.gov.uk"):
            html_path = html_path[len("https://www.gov.uk"):]
        doc = govuk_content(html_path)
        release_url = "https://www.gov.uk" + html_path
    else:
        doc = edition
        release_url = "https://www.gov.uk" + edition["_path"]
    text = html_to_text((doc.get("details") or {}).get("body") or "")
    if not text:
        raise SourceError(f"empty body at {release_url}")
    period_label = ""
    start = end = None
    m = edition["_title_match"]
    if cfg.get("period_from_title") == "year" and m.groupdict().get("year"):
        period_label = m.group("year")
        start, end = period_bounds(period_label, "calendar_year")
    return {
        "text": text,
        "release_date": _iso_date(edition.get("first_published_at")),
        "release_url": release_url,
        "period_label": period_label, "period_start": start, "period_end": end,
        "review": [],
    }


def _page_or_latest(cfg: dict) -> dict:
    if cfg.get("page"):
        content = govuk_content(cfg["page"])
        content["_path"] = cfg["page"]
        return content
    return latest_govuk_edition(cfg)


def fetch_govuk_csv(stat, cfg: dict, today: date) -> dict:
    page = _page_or_latest(cfg)
    att = find_attachment(page, cfg["attachment_title_regex"])
    url = _abs_url(att.get("url", ""))
    raw = http_get(url).content.decode("utf-8-sig")
    lines = raw.splitlines()
    reader = csv.DictReader(io.StringIO(raw))
    filters = cfg.get("filters") or {}
    matches = []
    for i, row in enumerate(reader, start=1):
        if all((row.get(k) or "").strip() == v for k, v in filters.items()):
            matches.append((row, lines[i] if i < len(lines) else ""))
    if not matches:
        raise SourceError(f"no CSV rows match {filters} in {url}")
    pcol, vcol = cfg["period_column"], cfg["value_column"]

    def _key(item):
        m = re.match(r"(\d{4})", item[0].get(pcol) or "")
        return int(m.group(1)) if m else -1
    latest_key = max(_key(x) for x in matches)
    latest = [x for x in matches if _key(x) == latest_key]
    if len(latest) != 1:
        raise SourceError(f"{len(latest)} CSV rows for the latest period — filters are ambiguous")
    row, line = latest[0]
    label = (row.get(pcol) or "").strip()
    value = (row.get(vcol) or "").strip()
    start, end = period_bounds(label, cfg.get("period_type", ""))
    return {
        "value": value,
        "period_label": label,
        "period_start": start, "period_end": end,
        "release_date": _iso_date(page.get("public_updated_at") if cfg.get("page")
                                  else page.get("first_published_at")),
        "release_url": "https://www.gov.uk" + page["_path"],
        "source_wording": _wording(cfg, stat, value, label),
        "evidence": f"{att.get('title', '')} ({url}) — CSV row: {line.strip()}",
        "evidence_kind": "structured", "evidence_value": value, "scale": 1,
        "review": [],
    }


def read_ods_sheet(data: bytes, sheet: str) -> list[list[str]]:
    """Rows of one sheet of an ODS file as lists of strings (stdlib only)."""
    t = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    txt = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    off = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("content.xml")
            if info.file_size > 100 * 1024 * 1024:
                raise SourceError("ODS content.xml too large")
            root = ET.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise SourceError(f"not a readable ODS file: {exc}") from exc
    for tbl in root.iter(t + "table"):
        if tbl.get(t + "name") != sheet:
            continue
        rows = []
        for row in tbl.iter(t + "table-row"):
            cells: list[str] = []
            for c in row.findall(t + "table-cell"):
                rep = min(int(c.get(t + "number-columns-repeated", "1")), 500)
                shown = " ".join("".join(p.itertext()) for p in c.findall(txt + "p"))
                cells.extend([c.get(off + "value") or normalise_ws(shown)] * rep)
            while cells and cells[-1] == "":
                cells.pop()
            rows.append(cells)
        return rows
    raise SourceError(f"sheet {sheet!r} not found")


def fetch_govuk_ods(stat, cfg: dict, today: date) -> dict:
    page = _page_or_latest(cfg)
    att = find_attachment(page, cfg["attachment_title_regex"])
    url = _abs_url(att.get("url", ""))
    rows = read_ods_sheet(http_get(url).content, cfg["sheet"])

    if cfg.get("section_regex"):
        # Header row starts the section; the value row follows before a blank row.
        sec = re.compile(cfg["section_regex"], re.I)
        rx = re.compile(cfg["row_regex"], re.I)
        hdr_i = next((i for i, r in enumerate(rows) if r and sec.search(r[0])), None)
        if hdr_i is None:
            raise SourceError(f"section {cfg['section_regex']!r} not found in {cfg['sheet']}")
        header = rows[hdr_i]
        val_row = None
        for r in rows[hdr_i + 1:]:
            if not r:
                break
            if rx.search(r[0]):
                val_row = r
                break
        if val_row is None:
            raise SourceError(f"row {cfg['row_regex']!r} not found in section")
        nums = [i for i, v in enumerate(val_row) if i > 0 and to_float(v) is not None]
        if not nums or nums[-1] >= len(header):
            raise SourceError("no numeric cell under a period header in the value row")
        j = nums[-1]
        label, raw, where = header[j], val_row[j], f"'{header[0]}' / '{val_row[0]}' / '{header[j]}'"
    else:
        hdr_rx = re.compile(cfg["header_regex"], re.I)
        found = None
        for i, r in enumerate(rows):
            cols = [(int(m.group("year")), j) for j, v in enumerate(r)
                    if (m := hdr_rx.match(normalise_ws(v)))]
            if cols:
                found = (i, max(cols))
                break
        if found is None:
            raise SourceError(f"header {cfg['header_regex']!r} not found in {cfg['sheet']}")
        i, (year, j) = found
        nxt = next((r for r in rows[i + 1:] if r and len(r) > j and r[j]), None)
        if nxt is None:
            raise SourceError("no value under the latest-year header")
        label, raw, where = str(year), nxt[j], f"'{normalise_ws(rows[i][j])}'"

    x = to_float(raw)
    if x is None:
        raise SourceError(f"cell {where} is not numeric: {raw!r}")
    scale = cfg.get("scale", 1)
    value = format_number(x * scale, cfg.get("decimals", 1))
    ptype = cfg.get("period_type", "")
    start, end = period_bounds(label, ptype)
    if ptype == "ordinal_quarter" and start:
        label = f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return {
        "value": value,
        "period_label": label,
        "period_start": start, "period_end": end,
        "release_date": _iso_date(page.get("public_updated_at") if cfg.get("page")
                                  else page.get("first_published_at")),
        "release_url": "https://www.gov.uk" + page["_path"],
        "source_wording": _wording(cfg, stat, value, label),
        "evidence": f"{att.get('title', '')} ({url}), sheet {cfg['sheet']}, {where} = {raw}",
        "evidence_kind": "structured", "evidence_value": raw, "scale": scale,
        "review": [],
    }


def with_unit(value: str, unit: str | None) -> str:
    """'13.1','£bn' → '£13.1bn'; '71,200','£ per farm' → '£71,200 per farm'; '64.8','%' → '64.8%'."""
    unit = unit or ""
    if unit == "%":
        return f"{value}%"
    if unit.startswith("£"):
        return f"£{value}{unit[1:]}"
    return f"{value} {unit}".strip()


def _wording(cfg: dict, stat, value: str, label: str) -> str:
    return f"{cfg.get('measure', stat.display_label)}: {with_unit(value, cfg.get('unit'))} in {label}."


# ── DfE Explore Education Statistics ──────────────────────────────────────────

def fetch_ees(stat, cfg: dict, today: date) -> dict:
    pub = cfg["publication_id"]
    sets = http_json(f"{EES_API}/publications/{pub}/data-sets").get("results") or []
    ds = next((d for d in sets if d.get("title") == cfg["dataset_title"]), None)
    if ds is None:
        raise SourceError(f"EES data set {cfg['dataset_title']!r} not found")
    meta = http_json(f"{EES_API}/data-sets/{ds['id']}/meta")
    ind = next((i for i in meta.get("indicators") or [] if i.get("column") == cfg["indicator_column"]), None)
    flt = next((f for f in meta.get("filters") or [] if f.get("column") == cfg["filter_column"]), None)
    if ind is None or flt is None:
        raise SourceError("EES indicator or filter column not found in data set metadata")
    opt = next((o for o in flt.get("options") or [] if o.get("label") == cfg["filter_label"]), None)
    if opt is None:
        raise SourceError(f"EES filter option {cfg['filter_label']!r} not found")
    periods = meta.get("timePeriods") or []
    if not periods:
        raise SourceError("EES data set has no time periods")
    tp = periods[-1]
    res = http_json(f"{EES_API}/data-sets/{ds['id']}/query", {
        "indicators": ind["id"], "filters.in": opt["id"],
        "geographicLevels.eq": cfg.get("geographic_level", "NAT"),
        "timePeriods.eq": f"{tp['period']}|{tp['code']}",
    }).get("results") or []
    if len(res) != 1:
        raise SourceError(f"EES query returned {len(res)} rows; expected exactly 1")
    raw = str((res[0].get("values") or {}).get(ind["id"], "")).strip()
    label = tp.get("label") or tp["period"]
    ptype = "academic_year" if tp.get("code") == "AY" else "financial_year" if tp.get("code") == "FY" else ""
    start, end = period_bounds(tp["period"], ptype)
    ver = ds.get("latestVersion") or {}
    return {
        "value": raw,
        "period_label": label,
        "period_start": start, "period_end": end,
        "release_date": _iso_date(ver.get("published")),
        "release_url": cfg.get("source_url") or stat.source_url,
        "source_wording": _wording(cfg, stat, raw, label),
        "evidence": (f"EES data set '{ds['title']}' v{ver.get('version', '?')}: "
                     f"{tp['period']}|{tp['code']}, {cfg.get('geographic_level', 'NAT')}, "
                     f"{cfg['filter_column']}={cfg['filter_label']}, {cfg['indicator_column']}={raw}"),
        "evidence_kind": "structured", "evidence_value": raw, "scale": 1,
        "review": [],
    }


# ── NHS England A&E ───────────────────────────────────────────────────────────

_NHS_CSV_LINK = re.compile(
    r'href="(?P<url>[^"]+\.csv)"[^>]*>\s*Monthly A&(?:amp;)?E (?P<month>[A-Za-z]+) (?P<year>\d{4}) \(CSV',
    re.I)
_NHS_YEAR_LINK = re.compile(
    r'href="(?P<url>[^"]*/ae-attendances-and-emergency-admissions-(?P<y>\d{4})-\d{2}/)"')


def fetch_nhs_ae(stat, cfg: dict, today: date) -> dict:
    index = http_get(cfg["index_url"]).text
    years = sorted({(int(m.group("y")), m.group("url")) for m in _NHS_YEAR_LINK.finditer(index)},
                   reverse=True)
    if not years:
        raise SourceError("no A&E year pages linked from the index page")
    links = []
    for _, page_url in years[:2]:     # the new financial year page can be empty in April
        page = http_get(page_url).text
        for m in _NHS_CSV_LINK.finditer(page):
            mo = _MONTHS.get(m.group("month").lower())
            if mo:
                links.append(((int(m.group("year")), mo), m.group("url"), m.group("month"), page_url))
    if not links:
        raise SourceError("no monthly A&E CSV links found")
    (year, month), url, month_name, page_url = max(links)
    raw = http_get(url).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    expected = f"MSitAE-{calendar.month_name[month].upper()}-{year}"
    providers = [r for r in rows if (r.get("Period") or "").strip() == expected]
    totals = [r for r in rows if (r.get("Period") or "").strip() == "TOTAL"]
    if not providers or len(totals) != 1 or len(providers) + 1 != len(rows):
        raise SourceError(f"A&E CSV layout changed: expected {expected} provider rows and one TOTAL row")
    att_cols = [c for c in rows[0] if c.startswith("A&E attendances")]
    over_cols = [c for c in rows[0] if c.startswith("Attendances over 4hrs")]
    if len(att_cols) != 6 or len(over_cols) != 6:
        raise SourceError("A&E CSV columns have changed; expected 6 attendance and 6 over-4-hour columns")
    try:
        total = sum(int(totals[0][c] or 0) for c in att_cols)
        over = sum(int(totals[0][c] or 0) for c in over_cols)
        prov_total = sum(int(r[c] or 0) for r in providers for c in att_cols)
        prov_over = sum(int(r[c] or 0) for r in providers for c in over_cols)
    except ValueError as exc:
        raise SourceError(f"non-integer value in A&E CSV: {exc}") from exc
    if total <= 0:
        raise SourceError("A&E CSV has no attendances")
    review = []
    if (prov_total, prov_over) != (total, over):
        review.append(f"provider rows sum to {prov_total:,}/{prov_over:,} but the TOTAL row "
                      f"says {total:,}/{over:,}")
    pct = 100 * (1 - over / total)
    value = f"{pct:.1f}"
    label = f"{calendar.month_name[month]} {year}"
    start, end = period_bounds(label, "month")
    total_line = next((ln for ln in raw.splitlines() if ln.startswith("TOTAL,")), "")
    return {
        "value": value,
        "period_label": label,
        "period_start": start, "period_end": end,
        "release_date": None,
        "release_url": page_url,
        "source_wording": (f"{value}% of A&E attendances in England were admitted, transferred or "
                           f"discharged within 4 hours in {label} (all A&E types, including booked "
                           f"appointments)."),
        "evidence": (f"Monthly A&E {month_name} {year} (CSV, {url}) TOTAL row "
                     f"(matches the sum of {len(providers)} provider rows): attendances {total:,}; "
                     f"over 4 hours {over:,}; within 4 hours = 1 - {over:,}/{total:,} = {pct:.2f}%. "
                     f"Row: {total_line[:200]}"),
        "evidence_kind": "derived",
        "review": review,
    }
