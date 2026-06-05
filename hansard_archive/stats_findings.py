"""
Lift the source's OWN stated key findings from a GOV.UK HTML release page.

LIFT, not synthesise — this returns the source's verbatim findings under a known
findings heading, with the source URL for attribution. NO AI, no interpretation.
Same family as showing the abstract: faithful presentation of the source's words.

Returns None when there is no recognised findings section (→ the page falls back
to the floor: abstract + files + tags + dates + link). A heading that matches but
yields no items also returns None — never render an empty/broken findings box.

Slice scope: fetch-at-render, no DB writes, no schema. Rollout would extract at
ingest-time and cache; this module is for evaluating the rendered shape.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("stats_findings")

_UA = {"User-Agent": "WestminsterBriefBot/1.0 (+https://westminsterbrief.co.uk/)"}
_TIMEOUT = (10, 15)
_MAX_ITEMS = 8

# Bounded set of GOV.UK statistical-release findings headings (normalised:
# lowercased, punctuation stripped). A heading outside this set is a MISS → floor.
FINDINGS_HEADINGS: frozenset[str] = frozenset({
    "main points",
    "key findings",
    "main findings",
    "main stories",
    "summary",
    "headlines",
    "headline figures",
    "main facts and figures",
    "headline facts and figures",
    "key statistics",
})


def _norm_heading(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _fetch(url: str, http: requests.Session | None) -> str | None:
    sess = http or requests
    try:
        r = sess.get(url, headers=_UA, timeout=_TIMEOUT)
        if r.ok:
            return r.text
    except requests.RequestException as exc:
        log.info("findings fetch failed for %s: %s", url, exc)
    return None


def _extract_from_html(html: str) -> tuple[str | None, list[str]]:
    """Find a findings heading in the govspeak body and lift the section under it.

    The section runs until the next heading of the SAME level or higher (e.g. an
    h2 "Summary" runs to the next h2), so multi-part summaries with h3 sub-sections
    are captured. Prefer bullet lists (the usual findings form) over prose
    paragraphs. Verbatim. Returns (heading, items) or (None, [])."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(class_="govspeak") or soup
    for h in body.find_all(["h2", "h3"]):
        if _norm_heading(h.get_text()) not in FINDINGS_HEADINGS:
            continue
        level = int(h.name[1])
        bullets: list[str] = []
        paras: list[str] = []
        for sib in h.find_next_siblings():
            name = getattr(sib, "name", None)
            if name in ("h1", "h2", "h3", "h4", "h5", "h6") and int(name[1]) <= level:
                break  # next same-or-higher section
            if not hasattr(sib, "find_all"):
                continue
            # Bullets and paragraphs may be direct siblings OR wrapped in a div.
            for ul in ([sib] if name == "ul" else sib.find_all("ul")):
                for li in ul.find_all("li", recursive=False):
                    t = li.get_text(" ", strip=True)
                    if t:
                        bullets.append(t)
            if name == "p":
                t = sib.get_text(" ", strip=True)
                if t:
                    paras.append(t)
            else:
                for p in sib.find_all("p"):
                    t = p.get_text(" ", strip=True)
                    if t:
                        paras.append(t)
            if len(bullets) >= _MAX_ITEMS:
                break
        items = (bullets or paras)[:_MAX_ITEMS]
        if items:
            return h.get_text(strip=True), items
    return None, []


def _first_content_subpage(html: str) -> str | None:
    """First GOV.UK content sub-page link (piece-2b style) from attachment
    containers — for landing pages whose findings live on a sub-page. Excludes
    pre-release-access pages and direct file links."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select(".gem-c-attachment a[href]") or soup.select("section.attachment a[href]")
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.gov.uk" + href
        p = urlparse(href)
        if p.netloc not in ("www.gov.uk", ""):
            continue
        parts = [x for x in p.path.split("/") if x]
        if len(parts) < 4 or "." in parts[-1]:
            continue
        if any(seg.startswith("pre-release-access-") or seg.endswith("-pre-release-access-list")
               for seg in parts):
            continue
        return href
    return None


def lift_key_findings(pub_url: str, http: requests.Session | None = None) -> dict | None:
    """
    Resolve the source's stated key findings for a GOV.UK publication.

    Tries the publication page first; if it has no findings section but links to a
    content sub-page (the piece-2b HTML-release case), follows that once. Returns
    {heading, items (verbatim list), source_url} or None (→ floor).
    """
    if not pub_url.startswith("https://www.gov.uk/"):
        return None  # ONS / others: no page-level findings — floor

    html = _fetch(pub_url, http)
    if not html:
        return None

    heading, items = _extract_from_html(html)
    source = pub_url

    if not items:
        sub = _first_content_subpage(html)
        if sub:
            sub_html = _fetch(sub, http)
            if sub_html:
                heading, items = _extract_from_html(sub_html)
                source = sub

    if not items:
        return None
    return {"heading": heading, "items": items, "source_url": source}
