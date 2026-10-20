"""
Member profiles served from our own database.

Read helpers for /members/<id>/<slug>, /mp_search and /biography, plus the
biography prompt and Gemini call used offline by scripts/refresh_members.py.

Nothing in this module calls Parliament on page view. Data is written by
scripts/refresh_members.py into member_profile / member_biography; until a
member has been refreshed, pages fall back to the basic row in cached_member.

Free feature: the biography uses Gemini only, never Claude (CLAUDE.md
"Output rules"). It summarises retrieved material with sources listed, and the
prompt forbids inference about a member's views or likely behaviour.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import unicodedata

import requests
from markupsafe import Markup, escape
from sqlalchemy.exc import SQLAlchemyError

from cache_models import CachedMember, MemberBiography, MemberProfile
from extensions import db

log = logging.getLogger(__name__)

BASE_URL = "https://westminsterbrief.co.uk"
MEMBERS_SITE = "https://members.parliament.uk/member"
PARLIAMENT_IMAGE_PREFIX = "https://members-api.parliament.uk/"

BIO_MODEL = "gemini-2.5-flash-lite"
PROMPT_VERSION = "2026-10-v1"
AI_LABEL = "AI-generated — check the official record"

_GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{BIO_MODEL}:generateContent"
_GEMINI_TIMEOUT = 60
_GEMINI_RETRIES = 3


class GeminiPermanentError(RuntimeError):
    """401/403/429 or similar — stop the run, do not retry (CLAUDE.md error table)."""


# ── Slugs ─────────────────────────────────────────────────────────────────────

_HONORIFIC_RE = re.compile(
    r"^(?:the\s+)?(?:rt\.?\s+hon\.?\s+)?(?:(?:sir|dame|dr|mr|mrs|ms|miss|prof|professor)\.?\s+)+",
    re.IGNORECASE,
)


def _slug_part(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’,.]", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def member_slug(name: str, house: str, membership_from: str = "") -> str:
    """MPs: firstname-lastname-constituency. Peers: their title (e.g. baroness-smith-of-malvern)."""
    if house == "Lords":
        return _slug_part(name) or "member"
    person = _HONORIFIC_RE.sub("", (name or "").strip())
    return _slug_part(f"{person} {membership_from or ''}") or "member"


def profile_path(member_id: int, slug: str) -> str:
    return f"/members/{int(member_id)}/{slug}"


# ── Safe DB reads (a missing table before the migration must not 500) ─────────

def _safe_read(fn, *args):
    try:
        return fn(*args)
    except SQLAlchemyError as exc:
        db.session.rollback()
        log.warning("[members] DB read failed in %s: %s", fn.__name__, exc.__class__.__name__)
        return None


def _get_profile(member_id):
    return db.session.get(MemberProfile, int(member_id))


def _get_biography(member_id):
    row = db.session.get(MemberBiography, int(member_id))
    return row if row is not None and row.bio_text else None


def get_profile(member_id) -> MemberProfile | None:
    return _safe_read(_get_profile, member_id)


def get_biography(member_id) -> MemberBiography | None:
    return _safe_read(_get_biography, member_id)


def get_cached_member(member_id) -> CachedMember | None:
    return _safe_read(CachedMember.get, member_id)


def _role_line(house: str, membership_from: str) -> str:
    if house == "Lords":
        return f"Member of the House of Lords ({membership_from})" if membership_from else "Member of the House of Lords"
    return f"MP for {membership_from}" if membership_from else "Member of Parliament"


def member_header(member_id) -> dict:
    """Header dict for templates, from member_profile or the cached_member fallback. {} if unknown."""
    profile = get_profile(member_id)
    if profile is not None:
        return {
            "id": profile.member_id,
            "name": profile.name,
            "party": profile.party or "",
            "role": _role_line(profile.house, profile.membership_from or ""),
            "photo_url": profile.thumbnail_url or "",
            "is_lord": profile.house == "Lords",
            "house": profile.house or "",
            "constituency": profile.membership_from or "",
            "current_role": profile.current_role or "",
            "is_current": bool(profile.is_current),
            "last_updated": _iso(profile.last_updated),
            "profile_url": profile_path(profile.member_id, profile.slug),
            "stored": True,
        }
    cached = get_cached_member(member_id)
    if cached is not None:
        house = cached.house or ""
        return {
            "id": cached.member_id,
            "name": cached.name or "",
            "party": cached.party or "",
            "role": _role_line(house, cached.constituency or ""),
            "photo_url": cached.image_url or "",
            "is_lord": house == "Lords",
            "house": house,
            "constituency": cached.constituency or "",
            "current_role": cached.ministerial_role or "",
            "is_current": True,
            "last_updated": _iso(cached.cached_at),
            "profile_url": profile_path(cached.member_id, member_slug(cached.name, house, cached.constituency)),
            "stored": False,
        }
    return {}


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def _as_api_posts(posts) -> list:
    """Stored {name,start,end,info} → the {name,startDate,endDate} shape the templates sort on."""
    return [
        {"name": p.get("name") or "", "startDate": p.get("start") or "",
         "endDate": p.get("end") or "", "info": p.get("info") or ""}
        for p in (posts or [])
    ]


def profile_tab_data(profile: MemberProfile | None) -> dict | None:
    """Career + interests for the MP Research Profile tab. None if details not stored yet."""
    if profile is None or profile.details_refreshed_at is None:
        return None
    return {
        "govt_posts": _as_api_posts(profile.government_posts),
        "opp_posts": _as_api_posts(profile.opposition_posts),
        "committees": _as_api_posts(profile.committees),
        "interests": list(profile.interests or []),
    }


def official_links(member_id: int) -> list[dict]:
    mid = int(member_id)
    return [
        {"label": "Parliament profile", "url": f"{MEMBERS_SITE}/{mid}/career"},
        {"label": "Register of interests", "url": f"{MEMBERS_SITE}/{mid}/registeredinterests"},
        {"label": "Contact details", "url": f"{MEMBERS_SITE}/{mid}/contact"},
        {"label": "Written questions (Parliament)", "url": f"{MEMBERS_SITE}/{mid}/writtenquestions"},
        {"label": "Spoken contributions (Hansard)",
         "url": f"https://hansard.parliament.uk/search/MemberContributions?memberId={mid}"},
    ]


def is_parliament_image(url: str) -> bool:
    return bool(url) and url.startswith(PARLIAMENT_IMAGE_PREFIX)


# ── Rendering + Word helpers ───────────────────────────────────────────────────

HEADING_RE = re.compile(r'^#{1,3}\s+(.+)$')
BULLET_RE = re.compile(r'^\s*[-*•]\s+(.+)$')
BOLD_RE = re.compile(r'\*\*(.+?)\*\*')


def render_bio_html(text: str) -> Markup:
    """Minimal markdown (headings, bullets, bold) → HTML. Escapes first: the text is AI output."""
    html, in_list = [], False
    for raw in (text or '').splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        safe = BOLD_RE.sub(r'<strong>\1</strong>', str(escape(line)))
        heading = HEADING_RE.match(safe)
        bullet = BULLET_RE.match(safe)
        if bullet:
            if not in_list:
                html.append('<ul>')
                in_list = True
            html.append(f'<li>{bullet.group(1)}</li>')
            continue
        if in_list:
            html.append('</ul>')
            in_list = False
        if heading:
            html.append(f'<h3>{heading.group(1)}</h3>')
        else:
            html.append(f'<p>{safe}</p>')
    if in_list:
        html.append('</ul>')
    return Markup('\n'.join(html))


def fetch_member_photo(url: str):
    """Photo for the Word header — Parliament's image host only, 4s cap. None on any failure."""
    if not is_parliament_image(url):
        return None
    try:
        resp = requests.get(url, timeout=4)
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/'):
            return io.BytesIO(resp.content)
    except requests.RequestException as exc:
        log.info('[members] photo fetch skipped: %s', exc.__class__.__name__)
    return None


# ── Member search (autocomplete) ──────────────────────────────────────────────

def _search(term: str, limit: int) -> list[dict]:
    pattern = f"%{term}%"
    results: dict[int, str] = {}
    for row in (MemberProfile.query
                .filter(MemberProfile.is_current.is_(True),
                        MemberProfile.name.ilike(pattern) | MemberProfile.name_list_as.ilike(pattern))
                .order_by(MemberProfile.name).limit(limit).all()):
        results[row.member_id] = row.name
    if len(results) < limit:
        for row in (CachedMember.query.filter(CachedMember.name.ilike(pattern))
                    .order_by(CachedMember.name).limit(limit).all()):
            results.setdefault(row.member_id, row.name)
    return [{"id": mid, "text": name} for mid, name in list(results.items())[:limit]]


def search_members(term: str, limit: int = 15) -> list[dict]:
    term = (term or "").strip()
    if len(term) < 3:
        return []
    found = _safe_read(_search, term, limit)
    if found is None:
        # member_profile missing (pre-migration): search cached_member alone
        found = _safe_read(lambda t, n: [{"id": r.member_id, "text": r.name} for r in
                                         CachedMember.query.filter(CachedMember.name.ilike(f"%{t}%"))
                                         .order_by(CachedMember.name).limit(n).all()], term, limit)
    return found or []


# ── Biography prompt, hash and generation ─────────────────────────────────────

def _posts_lines(posts) -> list[str]:
    lines = []
    for p in sorted(posts or [], key=lambda x: x.get("start") or "", reverse=True):
        start = (p.get("start") or "")[:7]
        end = (p.get("end") or "")[:7] or "present"
        if p.get("name"):
            lines.append(f"  - {p['name']} ({start}–{end})")
    return lines


def bio_inputs(profile: MemberProfile, wiki_text: str, wiki_url: str | None) -> dict:
    """Everything the prompt uses, as a deterministic structure (hashed for change detection).

    Activity counts are deliberately left out — only the set of subjects is used —
    so a bio is not regenerated every week just because a member spoke again.
    """
    return {
        "name": profile.name,
        "house": profile.house,
        "party": profile.party,
        "membership_from": profile.membership_from,
        "membership_start": profile.membership_start.isoformat() if profile.membership_start else None,
        "government_posts": _posts_lines(profile.government_posts),
        "opposition_posts": _posts_lines(profile.opposition_posts),
        "committees": _posts_lines(profile.committees),
        "interests": [f"[{i.get('category', '')}] {(i.get('description') or '')[:200]}"
                      for i in (profile.interests or [])[:30]],
        "interests_total": len(profile.interests or []),
        "debate_subjects": [t.get("theme") for t in (profile.top_policy_areas or [])][:6],
        "wq_departments": [d.get("department") for d in (profile.wq_departments or [])][:6],
        "wikipedia_text": (wiki_text or "")[:3500],
        "wikipedia_url": wiki_url,
    }


def bio_source_hash(inputs: dict) -> str:
    payload = json.dumps({"prompt_version": PROMPT_VERSION, "model": BIO_MODEL, **inputs},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bio_sources(member_id: int, inputs: dict) -> list[dict]:
    mid = int(member_id)
    sources = [
        {"label": "UK Parliament Members API — career, posts and committees",
         "url": f"{MEMBERS_SITE}/{mid}/career"},
        {"label": "Register of Members' Financial Interests / Register of Lords' Interests",
         "url": f"{MEMBERS_SITE}/{mid}/registeredinterests"},
        {"label": "Westminster Brief Hansard and written questions archive",
         "url": f"{BASE_URL}/archive/mp/{mid}"},
    ]
    if inputs.get("wikipedia_text") and inputs.get("wikipedia_url"):
        sources.append({"label": "Wikipedia (publicly editable)", "url": inputs["wikipedia_url"]})
    return sources


def _block(title: str, lines: list[str], empty: str) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else f"  {empty}")


def bio_prompt(inputs: dict) -> str:
    house = inputs.get("house") or ""
    seat = (f"Member of the House of Lords ({inputs.get('membership_from')})" if house == "Lords"
            else f"MP for {inputs.get('membership_from')}")
    interests = list(inputs.get("interests") or [])
    extra = (inputs.get("interests_total") or 0) - len(interests)
    if extra > 0:
        interests.append(f"... and {extra} further entries (see the official register)")

    parliament_block = "\n\n".join([
        f"Name: {inputs.get('name')}\nHouse: {house}\nParty: {inputs.get('party')}\n"
        f"Seat / peerage: {seat}\nCurrent membership began: {inputs.get('membership_start') or 'unknown'}",
        _block("Government roles", inputs.get("government_posts"), "None recorded."),
        _block("Opposition roles", inputs.get("opposition_posts"), "None recorded."),
        _block("Committee memberships (current and historical)", inputs.get("committees"), "None recorded."),
        _block("Registered interests", [f"  • {i}" for i in interests], "None declared."),
        _block("Policy areas of debates they spoke in (last 12 months, most frequent first)",
               [f"  - {t}" for t in inputs.get("debate_subjects") or []], "None in the archive."),
        _block("Departments their written questions were addressed to (last 12 months, most frequent first)",
               [f"  - {d}" for d in inputs.get("wq_departments") or []], "None in the archive."),
    ])
    wiki = inputs.get("wikipedia_text") or "No Wikipedia article was matched to this member."

    return f"""You are a UK parliamentary researcher writing a short, neutral reference profile of a Member of Parliament or Peer for policy professionals, journalists and researchers.

Write a structured profile of {inputs.get('name')} using ONLY the source data below. Do not add anything from your own training data or general knowledge. If a section has no source data, say "No information in the sources used." rather than guessing.

--- WIKIPEDIA EXTRACT (background and pre-parliamentary career only; publicly editable) ---
{wiki}

--- UK PARLIAMENT AND WESTMINSTER BRIEF ARCHIVE DATA ---
{parliament_block}

---

Use these exact markdown headings:

## Background
Profession, education and career before Parliament, from the Wikipedia extract only. Leave out family, health and other personal-life details.

## Parliamentary career
When they were elected or appointed, party, and constituency or peerage.

## Government and opposition roles
Ministerial and shadow roles with dates, keeping government and opposition roles distinct. If none, say so.

## Committee memberships
Select and other committees with dates, including past ones. If none, say so.

## Registered interests
A brief factual summary of declared interests by category. If none are declared, say so.

## Subjects of recent parliamentary activity
Describe which subjects their recent debates and written questions have concerned, using only the policy areas and departments listed. Describe what they did, not what they think.

## Summary
Two or three factual sentences giving orientation to this member's parliamentary work: current role, notable committee work, and the subjects of their recent activity.

Rules: plain UK English; factual and neutral; no speculation about the member's views, positions, sympathies, motives or likely future behaviour; no advice or recommendations."""


def generate_bio_text(prompt: str, api_key: str) -> str:
    """Call Gemini Flash-Lite. Retries transient errors; raises GeminiPermanentError on 4xx auth/quota."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    last_exc: Exception | None = None
    for attempt in range(_GEMINI_RETRIES):
        try:
            # Key in a header, not the URL, so it can't leak into exception messages or logs.
            resp = requests.post(_GEMINI_URL, headers={"x-goog-api-key": api_key},
                                 json=payload, timeout=_GEMINI_TIMEOUT)
            if resp.status_code in (401, 403, 429):
                raise GeminiPermanentError(f"Gemini HTTP {resp.status_code}")
            if resp.status_code == 400:
                raise RuntimeError("Gemini HTTP 400 (request rejected)")
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if "## " not in text:
                raise ValueError("response has no section headings")
            return text
        except GeminiPermanentError:
            raise
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            last_exc = exc
            if attempt < _GEMINI_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Gemini generation failed: {last_exc.__class__.__name__}: {last_exc}")
