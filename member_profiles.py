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
import os
import re
import time
import unicodedata

import requests
from markupsafe import Markup, escape
from sqlalchemy.exc import SQLAlchemyError

from cache_models import CachedMember, MemberBiography, MemberName, MemberProfile
from extensions import db

log = logging.getLogger(__name__)

BASE_URL = "https://westminsterbrief.co.uk"
MEMBERS_SITE = "https://members.parliament.uk/member"
PARLIAMENT_IMAGE_PREFIX = "https://members-api.parliament.uk/"

# Generation uses the stronger Flash model (accuracy first); a cheaper model double-checks each
# sentence against the sources. Override with env vars; MEMBER_BIO_VERIFY_MODEL=off disables the
# model check (deterministic checks always run). Both models are part of the bio input hash.
BIO_MODEL = os.environ.get("MEMBER_BIO_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
_verify = os.environ.get("MEMBER_BIO_VERIFY_MODEL", "gemini-2.5-flash-lite").strip()
VERIFY_MODEL = None if _verify.lower() in ("", "off", "none") else _verify
PROMPT_VERSION = "2026-10-v3"
AI_LABEL = "AI-generated — check the official record"

_GEMINI_TIMEOUT = 90
_GEMINI_RETRIES = 3


class GeminiPermanentError(RuntimeError):
    """401/403/429 or similar — stop the run, do not retry (CLAUDE.md error table)."""


# ── Slugs ─────────────────────────────────────────────────────────────────────

_HONORIFIC_RE = re.compile(
    r"^(?:the\s+)?(?:rt\.?\s+hon\.?\s+)?(?:(?:sir|dame|dr|mr|mrs|ms|miss|prof|professor)\.?\s+)+",
    re.IGNORECASE,
)
_BISHOP_RE = re.compile(r"^(?:the\s+)?(?:lord\s+)?((?:arch)?bishop)\s+of\s+(.+)$", re.IGNORECASE)
_OF_PLACE_RE = re.compile(r"\bof\s+(.+)$", re.IGNORECASE)
_RANK_WORDS = {"lord": "baron", "baroness": "baroness", "lady": "lady", "viscount": "viscount",
               "viscountess": "viscountess", "earl": "earl", "countess": "countess",
               "marquess": "marquess", "marchioness": "marchioness", "duke": "duke", "duchess": "duchess"}


def _slug_part(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’,.]", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def strip_honorifics(name: str) -> str:
    return _HONORIFIC_RE.sub("", (name or "").strip()).strip()


def slug_for(name: str, house: str, membership_from: str = "",
             personal_name: str | None = None) -> tuple[str, str]:
    """(slug, source) following CLAUDE.md "SEO and URLs".

    MPs:    firstname-lastname-constituency                 (source 'personal')
    Peers:  firstname-lastname-of-place, from the official title; else firstname-lastname-<rank>
            (e.g. jacqui-smith-of-malvern, dominic-hubbard-baron). Bishops (not covered by the
            locked rule): firstname-lastname-bishop-of-place.
            Without a personal name, the title itself (baroness-smith-of-malvern, source 'title').
    """
    if house != "Lords":
        person = strip_honorifics(personal_name or name)
        return (_slug_part(f"{person} {membership_from or ''}") or "member"), "personal"
    if not personal_name:
        return (_slug_part(name) or "member"), "title"
    title = re.sub(r"^the\s+", "", (name or "").strip(), flags=re.IGNORECASE)
    bishop = _BISHOP_RE.match(title)
    if bishop:
        tail = f"{bishop.group(1)} of {bishop.group(2)}"
    elif _OF_PLACE_RE.search(title):
        tail = f"of {_OF_PLACE_RE.search(title).group(1)}"
    else:
        first = title.split()[0].lower() if title else ""
        tail = _RANK_WORDS.get(first, "baron")
    return (_slug_part(f"{personal_name} {tail}") or "member"), "personal"


def member_slug(name: str, house: str, membership_from: str = "", personal_name: str | None = None) -> str:
    return slug_for(name, house, membership_from, personal_name)[0]


def profile_path(member_id: int, slug: str) -> str:
    return f"/members/{int(member_id)}/{slug}"


# ── Personal names (Wikidata first, Parliament fallback, never guessed) ───────

_SYNOPSIS_NAME_RE = re.compile(r"\b(?:His|Her|Their) name is ([^,.<]+?)\s*(?:,|\.|$)")


def synopsis_name(synopsis: str) -> str | None:
    """Parliament's own statement of a peer's name, e.g. 'Her name is Jacqui Smith, and …'."""
    m = _SYNOPSIS_NAME_RE.search(synopsis or "")
    return m.group(1).strip() if m else None


def parliament_personal_name(summary: dict, synopsis: str | None) -> tuple[str | None, str | None, str | None]:
    """(personal_name, given, family) from Parliament's own fields only.

    MPs: the display name is the personal name; nameListAs is 'Family, Given'.
    Peers: only the synopsis sentence; given/family are not split (that would be a guess).
    """
    if summary.get("house") == "Lords":
        return synopsis_name(synopsis), None, None
    personal = strip_honorifics(summary.get("name") or "") or None
    given = family = None
    list_as = summary.get("name_list_as") or ""
    if ", " in list_as:
        family, given = (x.strip() for x in list_as.split(", ", 1))
        given = strip_honorifics(given) or None
    return personal, given, family


def _surname_token(name: str) -> str:
    parts = _slug_part(name).split("-") if name else []
    return parts[-1] if parts else ""


def names_consistent(a: str, b: str) -> bool:
    """Same person-name if the final name token matches ('Michael Beasley' ~ 'Noel Michael Roy Beasley')."""
    return bool(a and b) and _surname_token(a) == _surname_token(b)


def resolve_name(summary: dict, wd: dict | None, parliament_name: tuple, member_id: int) -> dict:
    """Pick the personal name for one member. Returns member_name column values (no DB access).

    wd: {'status': 'matched'|'not_found'|'ambiguous'|'error', 'qid', 'name', 'given', 'family'} or None.
    parliament_name: (personal, given, family) from parliament_personal_name().
    """
    p_name, p_given, p_family = parliament_name
    wd_ok = wd is not None and wd.get("status") == "matched" and wd.get("name")
    w_name = wd.get("name") if wd_ok else None
    notes = []
    if wd is not None and wd.get("status") == "ambiguous":
        notes.append("wikidata: more than one item carries this parliament.uk member ID")
    if wd is not None and wd.get("status") == "error":
        notes.append("wikidata: lookup failed this run")

    out = {
        "wikidata_id": wd.get("qid") if wd_ok else None,
        "wikidata_name": w_name,
        "parliament_name": p_name,
        "peerage_title": summary.get("name") if summary.get("house") == "Lords" else None,
        "full_title": summary.get("full_title"),
    }
    if w_name and p_name and not names_consistent(w_name, p_name):
        notes.append(f"conflict: wikidata '{w_name}' vs parliament '{p_name}' — using Parliament's")
        out.update(personal_name=p_name, given_name=p_given, family_name=p_family, source="parliament",
                   source_url=f"{MEMBERS_SITE}/{member_id}/career", confidence="low")
    elif w_name:
        family = wd.get("family")
        given = wd.get("given")
        out.update(personal_name=w_name, given_name=given or p_given, family_name=family or p_family,
                   source="wikidata", source_url=f"https://www.wikidata.org/wiki/{wd['qid']}",
                   confidence="high" if p_name else "medium")
    elif p_name:
        out.update(personal_name=p_name, given_name=p_given, family_name=p_family, source="parliament",
                   source_url=f"{MEMBERS_SITE}/{member_id}/career",
                   confidence="high" if summary.get("house") != "Lords" else "medium")
    else:
        notes.append("missing: no personal name from Wikidata or Parliament")
        out.update(personal_name=None, given_name=None, family_name=None, source=None,
                   source_url=None, confidence=None)
    out["audit_notes"] = "; ".join(notes)[:1000] or None
    return out


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


def _get_name(member_id):
    return db.session.get(MemberName, int(member_id))


def get_name(member_id) -> MemberName | None:
    return _safe_read(_get_name, member_id)


def display_name(title: str, house: str, personal_name: str | None) -> str:
    """'Baroness Smith of Malvern (Jacqui Smith)' for peers; MPs keep their own name."""
    if house == "Lords" and personal_name and _slug_part(personal_name) not in _slug_part(title):
        return f"{title} ({personal_name})"
    return title


def _role_line(house: str, membership_from: str) -> str:
    if house == "Lords":
        return f"Member of the House of Lords ({membership_from})" if membership_from else "Member of the House of Lords"
    return f"MP for {membership_from}" if membership_from else "Member of Parliament"


def member_header(member_id) -> dict:
    """Header dict for templates, from member_profile or the cached_member fallback. {} if unknown."""
    name_row = get_name(member_id)
    personal = name_row.personal_name if name_row is not None else None
    profile = get_profile(member_id)
    if profile is not None:
        return {
            "id": profile.member_id,
            "name": profile.name,
            "personal_name": personal or "",
            "display_name": display_name(profile.name, profile.house, personal),
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
            "personal_name": personal or "",
            "display_name": display_name(cached.name or "", house, personal),
            "party": cached.party or "",
            "role": _role_line(house, cached.constituency or ""),
            "photo_url": cached.image_url or "",
            "is_lord": house == "Lords",
            "house": house,
            "constituency": cached.constituency or "",
            "current_role": cached.ministerial_role or "",
            "is_current": True,
            "last_updated": _iso(cached.cached_at),
            "profile_url": profile_path(cached.member_id,
                                        member_slug(cached.name, house, cached.constituency, personal)),
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


PHOTO_MAX_BYTES = 2 * 1024 * 1024


def fetch_member_photo(url: str):
    """Photo for the Word header — Parliament's image host only, no redirects, 4s and 2 MB caps.

    None on any failure (the document is built without the photo).
    """
    if not is_parliament_image(url):
        return None
    try:
        with requests.get(url, timeout=4, allow_redirects=False, stream=True) as resp:
            if resp.status_code != 200 or not resp.headers.get('Content-Type', '').startswith('image/'):
                return None
            declared = resp.headers.get('Content-Length')
            if declared and (not declared.isdigit() or int(declared) > PHOTO_MAX_BYTES):
                return None
            buf = io.BytesIO()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                buf.write(chunk)
                if buf.tell() > PHOTO_MAX_BYTES:
                    log.info('[members] photo fetch skipped: larger than %d bytes', PHOTO_MAX_BYTES)
                    return None
            buf.seek(0)
            return buf
    except requests.RequestException as exc:
        log.info('[members] photo fetch skipped: %s', exc.__class__.__name__)
    return None


# ── Member search (autocomplete) ──────────────────────────────────────────────

def _search(term: str, limit: int) -> list[dict]:
    """One LIMITed query over member_profile + member_name, matching any part of either name.

    "Jacq", "Jacqui Smith" and "Smith of Mal" all find Baroness Smith of Malvern, labelled
    "Baroness Smith of Malvern (Jacqui Smith)". Before member_name is filled the outer join simply
    matches Parliament's names; cached_member tops up members not yet in member_profile.
    """
    pattern = f"%{term}%"
    results: dict[int, str] = {}
    rows = (db.session.query(MemberProfile.member_id, MemberProfile.name, MemberProfile.house,
                             MemberName.personal_name)
            .outerjoin(MemberName, MemberName.member_id == MemberProfile.member_id)
            .filter(MemberProfile.is_current.is_(True),
                    MemberProfile.name.ilike(pattern) | MemberProfile.name_list_as.ilike(pattern)
                    | MemberName.personal_name.ilike(pattern) | MemberName.given_name.ilike(pattern)
                    | MemberName.family_name.ilike(pattern) | MemberName.peerage_title.ilike(pattern)
                    | MemberName.parliament_name.ilike(pattern))
            .order_by(MemberProfile.name).limit(limit).all())
    for mid, name, house, personal in rows:
        results[mid] = display_name(name, house, personal)
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


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# Occupations that only restate that the member is in Parliament — they produce filler.
_GENERIC_OCCUPATIONS = {"politician", "member of parliament", "peer", "life peer", "statesperson",
                        "legislator", "british politician"}


def wikidata_background(entry: dict | None, profile=None) -> tuple[list[str], list[str]]:
    """(education, occupations) from Wikidata, used ONLY when there is no Wikipedia article.

    P69 "educated at" values are dropped when the same institution is also an employer (P108)
    or appears in the member's registered interests or other posts — Wikidata sometimes records
    a place someone worked as a place they studied. When unsure, omit.
    """
    if not entry or entry.get("status") != "matched":
        return [], []
    workplaces = [_norm(e) for e in entry.get("employers") or []]
    if profile is not None:
        workplaces += [_norm(i.get("description")) for i in (profile.interests or [])]
        workplaces += [_norm(p.get("name")) for p in (profile.other_posts or [])]
    workplaces = [w for w in workplaces if w]

    def is_workplace(value: str) -> bool:
        v = _norm(value)
        return any(v == w or v in w or (len(w) > 8 and w in v) for w in workplaces)

    education = sorted(e for e in entry.get("education") or [] if e and not is_workplace(e))[:8]
    occupations = sorted(o for o in entry.get("occupations") or []
                         if o and _norm(o) not in _GENERIC_OCCUPATIONS)[:8]
    return education, occupations


def bio_inputs(profile: MemberProfile, wiki_text: str, wiki_url: str | None,
               personal_name: str | None = None, wikidata_entry: dict | None = None) -> dict:
    """Everything the prompt uses, as a deterministic structure (hashed for change detection).

    Source priority for background: the Wikipedia article text. Wikidata statements are used
    only when there is no article, and then only filtered education/occupation values. The two
    are never mixed. Activity counts are left out (only the set of subjects is used) so a bio is
    not regenerated every week just because a member spoke again.
    """
    wiki_text = (wiki_text or "")[:3500]
    education, occupations = ([], []) if wiki_text else wikidata_background(wikidata_entry, profile)
    return {
        "name": profile.name,
        "personal_name": personal_name,
        "house": profile.house,
        "party": profile.party,
        "membership_from": profile.membership_from,
        "membership_start": profile.membership_start.isoformat() if profile.membership_start else None,
        "house_memberships": _posts_lines(profile.house_memberships),
        "representations": _posts_lines(profile.representations),
        "government_posts": _posts_lines(profile.government_posts),
        "opposition_posts": _posts_lines(profile.opposition_posts),
        "committees": _posts_lines(profile.committees),
        "interests": [f"[{i.get('category', '')}] {(i.get('description') or '')[:200]}"
                      for i in (profile.interests or [])[:30]],
        "interests_total": len(profile.interests or []),
        "debate_subjects": [t.get("theme") for t in (profile.top_policy_areas or [])][:6],
        "wq_departments": [d.get("department") for d in (profile.wq_departments or [])][:6],
        "wikidata_id": (wikidata_entry or {}).get("qid") if (education or occupations) else None,
        "wikidata_education": education,
        "wikidata_occupations": occupations,
        "wikipedia_text": wiki_text,
        "wikipedia_url": wiki_url if wiki_text else None,
    }


def bio_source_hash(inputs: dict) -> str:
    payload = json.dumps({"prompt_version": PROMPT_VERSION, "model": BIO_MODEL,
                          "verify_model": VERIFY_MODEL, **inputs},
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
    elif inputs.get("wikidata_id") and (inputs.get("wikidata_education") or inputs.get("wikidata_occupations")):
        sources.append({"label": "Wikidata (education and occupation statements; publicly editable)",
                        "url": f"https://www.wikidata.org/wiki/{inputs['wikidata_id']}"})
    return sources


def _block(title: str, lines: list[str], empty: str) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else f"  {empty}")


def bio_source_texts(inputs: dict) -> dict:
    """The source text each part of the bio may draw on: 'background' and 'record'."""
    house = inputs.get("house") or ""
    seat = (f"Member of the House of Lords ({inputs.get('membership_from')})" if house == "Lords"
            else f"MP for {inputs.get('membership_from')}")
    interests = list(inputs.get("interests") or [])
    extra = (inputs.get("interests_total") or 0) - len(interests)
    if extra > 0:
        interests.append(f"... and {extra} further entries (see the official register)")
    record = "\n\n".join([
        f"Name: {inputs.get('name')}\nPersonal name: {inputs.get('personal_name') or 'not recorded'}\n"
        f"House: {house}\nParty: {inputs.get('party')}\n"
        f"Seat / peerage: {seat}\nCurrent membership began: {inputs.get('membership_start') or 'unknown'}",
        _block("House memberships", inputs.get("house_memberships"), "None recorded."),
        _block("Constituencies represented", inputs.get("representations"), "None recorded."),
        _block("Government roles", inputs.get("government_posts"), "None recorded."),
        _block("Opposition roles", inputs.get("opposition_posts"), "None recorded."),
        _block("Committee memberships (current and historical)", inputs.get("committees"), "None recorded."),
        _block("Registered interests", [f"  • {i}" for i in interests], "None declared."),
        _block("Policy areas of debates they spoke in (last 12 months, most frequent first)",
               [f"  - {t}" for t in inputs.get("debate_subjects") or []], "None in the archive."),
        _block("Departments their written questions were addressed to (last 12 months, most frequent first)",
               [f"  - {d}" for d in inputs.get("wq_departments") or []], "None in the archive."),
    ])
    if inputs.get("wikipedia_text"):
        background = "WIKIPEDIA ARTICLE EXTRACT (publicly editable):\n" + inputs["wikipedia_text"]
    elif inputs.get("wikidata_education") or inputs.get("wikidata_occupations"):
        background = "WIKIDATA STATEMENTS (publicly editable; no Wikipedia article was matched):\n" + "\n\n".join([
            _block("Educated at", [f"  - {e}" for e in inputs.get("wikidata_education") or []], "Not recorded."),
            _block("Occupations", [f"  - {o}" for o in inputs.get("wikidata_occupations") or []], "Not recorded."),
        ])
    else:
        background = "No background source was found for this member."
    return {"background": background, "record": record}


def bio_prompt(inputs: dict) -> str:
    texts = bio_source_texts(inputs)
    return f"""You are a UK parliamentary researcher writing a short, neutral, accurate reference profile of a Member of Parliament or Peer for policy professionals, journalists and researchers. Accuracy matters more than completeness.

Write a structured profile of {inputs.get('name')} using ONLY the source data below. Do not add anything from your own training data or general knowledge. If a section has no source data, write exactly "No information in the sources used."

--- BACKGROUND SOURCE ---
{texts['background']}

--- UK PARLIAMENT AND WESTMINSTER BRIEF ARCHIVE DATA ---
{texts['record']}

---

Use these exact markdown headings:

## Background
Education and career before Parliament, from the BACKGROUND SOURCE only. Keep where someone studied separate from where they worked: only say someone was educated at an institution if the source says they studied there. Leave out family, health and other personal-life details.

## Parliamentary career
When they were elected or appointed, party, and constituency or peerage, from the Parliament data.

## Government and opposition roles
Ministerial and shadow roles with dates, keeping government and opposition roles distinct. If none, say so.

## Committee memberships
Select and other committees with dates, including past ones. If none, say so.

## Registered interests
A brief factual summary of declared interests by category. If none are declared, say so.

## Subjects of recent parliamentary activity
Which subjects their recent debates and written questions have concerned, using only the policy areas and departments listed. Describe what they did, not what they think.

## Summary
Two or three factual sentences giving orientation to this member's parliamentary work: current role, notable committee work, and the subjects of their recent activity.

Rules:
- Every sentence must state a specific fact found in the sources. No filler or statements of the obvious (for example "She is a politician.", "He is a member of the House of Lords.", "is a politician and teacher").
- Never combine facts from different sources into one sentence or list.
- No speculation, no adjectives about the member or their views (for example "prominent", "passionate", "respected"), no views, positions, sympathies, motives or likely future behaviour, no advice.
- Plain UK English."""


# ── Verification ──────────────────────────────────────────────────────────────

_INSTITUTION_WORDS = (
    "University|College|School|Academy|Institute|Polytechnic|Hospital|Infirmary|Council|Trust|"
    "Union|Society|Foundation|Company|Corporation|Ltd|Limited|plc|PLC|Bank|Party|Committee|"
    "Department|Ministry|Office|Commission|Agency|Authority|Board|Church|Cathedral|Diocese|"
    "Chambers|Court|Inn|Group|Association|Federation|Institution|Service|Regiment|Army|Navy|"
    "Force|Seminary|Conservatoire|Museum|Library|Charity|Centre|Center"
)
_INSTITUTION_RE = re.compile(
    r"(?:\b[A-Z][\w'’&.-]*\s+(?:(?:of|and|for|the|de|upon|on|in)\s+)?){0,6}\b(?:" + _INSTITUTION_WORDS + r")\b"
    r"(?:\s+(?:of|for)\s+(?:the\s+)?[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,4})?"
)
_YEAR_RE = re.compile(r"\b(?:1[89]|20)\d{2}\b")
_STOP = {"of", "and", "for", "the", "de", "upon", "on", "in", "a", "an"}
_FILLER_RE = re.compile(
    r"^(?:[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,6}|She|He|They)\s+(?:is|was|has been|are)\s+(?:a|an)\s+"
    r"(?:[a-z]+\s+){0,2}(?:politician|peer|life peer|parliamentarian|member of (?:the house of lords|parliament)"
    r"|mp|lord|baroness)(?:\s+and\s+(?:a|an\s+)?[a-z ]+)?\.?$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")
NO_INFO = "No information in the sources used."


def _sentences_of(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        out.extend(s for s in _SENTENCE_SPLIT_RE.split(line) if s.strip())
    return out


def _supported(phrase: str, source_sentences: list[str], source_norm: str) -> bool:
    p = _norm(phrase)
    if not p:
        return True
    if p in source_norm:
        return True
    tokens = [t for t in p.split() if t not in _STOP]
    return any(all(t in set(_norm(s).split()) for t in tokens) for s in source_sentences)


def is_filler(sentence: str) -> bool:
    plain = re.sub(r"[*_`]", "", sentence).strip().lstrip("-•* ").strip()
    return bool(_FILLER_RE.match(plain))


def _claims(sentence: str) -> list[str]:
    plain = re.sub(r"[*_`]", "", sentence)
    return [m.group(0).strip() for m in _INSTITUTION_RE.finditer(plain)] + _YEAR_RE.findall(plain)


def deterministic_check(bio_text: str, texts: dict) -> tuple[str, list[dict], int]:
    """Drop filler and any sentence naming an institution or year absent from its section's source.

    Background is checked against the background source; Summary against both; every other
    section against the Parliament/archive record. Returns (clean_text, dropped, kept_count).
    """
    sources = {
        "background": texts["background"],
        "record": texts["record"],
        "both": texts["background"] + "\n" + texts["record"],
    }
    prepared = {k: (_sentences_of(v), _norm(v)) for k, v in sources.items()}
    out_lines, dropped, kept = [], [], 0
    section = None
    section_kept: dict = {}
    for raw in (bio_text or "").splitlines():
        line = raw.rstrip()
        heading = HEADING_RE.match(line)
        if heading:
            section = heading.group(1).strip().lower()
            section_kept[section] = 0
            out_lines.append(line)
            continue
        if not line.strip():
            out_lines.append("")
            continue
        scope = "background" if section == "background" else "both" if section == "summary" else "record"
        sents, norm = prepared[scope]
        bullet = BULLET_RE.match(line)
        prefix = line[: line.index(bullet.group(1))] if bullet else ""
        body = bullet.group(1) if bullet else line
        kept_parts = []
        for sentence in _sentences_of(body) or [body]:
            if NO_INFO.lower() in sentence.lower():
                kept_parts.append(sentence)
                continue
            if is_filler(sentence):
                dropped.append({"sentence": sentence, "reason": "filler", "section": section})
                continue
            missing = [c for c in _claims(sentence) if not _supported(c, sents, norm)]
            if missing:
                dropped.append({"sentence": sentence, "section": section,
                                "reason": "not in source: " + ", ".join(missing)})
                continue
            kept_parts.append(sentence)
            kept += 1
        if kept_parts:
            out_lines.append(prefix + " ".join(kept_parts))
            if section is not None:
                section_kept[section] = section_kept.get(section, 0) + 1
    return _fill_empty_sections(out_lines, section_kept), dropped, kept


def _fill_empty_sections(lines: list[str], section_kept: dict) -> str:
    out, current = [], None
    for line in lines + ["## __end__"]:
        heading = HEADING_RE.match(line)
        if heading:
            if current is not None and not section_kept.get(current):
                out.append(NO_INFO)
            current = heading.group(1).strip().lower()
            if current == "__end__":
                break
        out.append(line)
    return "\n".join(line for i, line in enumerate(out) if line or (i and out[i - 1])).strip()


def model_unsupported_claims(bio_text: str, texts: dict, api_key: str) -> list[dict]:
    """Second opinion from the verification model: sentences not supported by the sources."""
    prompt = f"""You are checking a draft profile of a UK parliamentarian against its sources, sentence by sentence.

A sentence is UNSUPPORTED if any fact in it is not stated in the sources: a wrong or missing date, an institution the sources do not name, a place of work described as a place of study (or the reverse), a role the sources do not give, an opinion, or a generic statement such as "is a politician".

--- BACKGROUND SOURCE ---
{texts['background']}

--- PARLIAMENT AND ARCHIVE RECORD ---
{texts['record']}

--- DRAFT ---
{bio_text}

Return JSON only: {{"unsupported": [{{"text": "<the exact sentence or bullet from the draft>", "reason": "<short reason>"}}]}}. Return {{"unsupported": []}} if every sentence is supported."""
    raw = _gemini_call(prompt, api_key, VERIFY_MODEL, json_mode=True)
    data = json.loads(raw)
    items = data.get("unsupported") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("verification response has no 'unsupported' list")
    return [i for i in items if isinstance(i, dict) and i.get("text")]


def _drop_sentences(bio_text: str, targets: list[dict]) -> tuple[str, list[dict]]:
    wanted = [(_norm(t["text"]), t) for t in targets if _norm(t["text"])]
    out, dropped, section_kept, section = [], [], {}, None
    for raw in bio_text.splitlines():
        heading = HEADING_RE.match(raw)
        if heading:
            section = heading.group(1).strip().lower()
            section_kept[section] = 0
            out.append(raw)
            continue
        bullet = BULLET_RE.match(raw)
        prefix = raw[: raw.index(bullet.group(1))] if bullet else ""
        body = bullet.group(1) if bullet else raw
        keep = []
        for sentence in _sentences_of(body):
            ns = _norm(sentence)
            hit = next((t for w, t in wanted if ns and (ns == w or ns in w or (len(w) > 20 and w in ns))), None)
            if hit is not None and NO_INFO.lower() not in sentence.lower():
                dropped.append({"sentence": sentence, "section": section,
                                "reason": "verification model: " + str(hit.get("reason") or "unsupported")})
            else:
                keep.append(sentence)
        if keep:
            out.append(prefix + " ".join(keep))
            if section is not None:
                section_kept[section] = section_kept.get(section, 0) + 1
        elif not raw.strip():
            out.append("")
    return _fill_empty_sections(out, section_kept), dropped


def verify_bio(bio_text: str, inputs: dict, api_key: str | None) -> tuple[str, dict]:
    """Deterministic checks, then (if VERIFY_MODEL is set) a model check. Returns (clean_text, report).

    Raises if the model check cannot be completed — an unverified bio is not published.
    """
    texts = bio_source_texts(inputs)
    clean, dropped, kept = deterministic_check(bio_text, texts)
    model_dropped: list[dict] = []
    if VERIFY_MODEL:
        clean, model_dropped = _drop_sentences(clean, model_unsupported_claims(clean, texts, api_key))
    all_dropped = dropped + model_dropped
    report = {
        "method": "deterministic+model" if VERIFY_MODEL else "deterministic",
        "verify_model": VERIFY_MODEL,
        "checked_claims": kept + len(dropped),
        "verified_claims": kept - len(model_dropped),
        "dropped_claims": len(all_dropped),
        "dropped": all_dropped,
    }
    return clean, report


# ── Gemini ────────────────────────────────────────────────────────────────────

def _gemini_call(prompt: str, api_key: str, model: str, json_mode: bool = False) -> str:
    """One Gemini generateContent call. Retries transient errors; GeminiPermanentError on 401/403/429."""
    config = {"temperature": 0 if json_mode else 0.2, "maxOutputTokens": 8192}
    if json_mode:
        config["responseMimeType"] = "application/json"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last_exc: Exception | None = None
    for attempt in range(_GEMINI_RETRIES):
        try:
            # Key in a header, not the URL, so it can't leak into exception messages or logs.
            resp = requests.post(url, headers={"x-goog-api-key": api_key}, json=payload, timeout=_GEMINI_TIMEOUT)
            if resp.status_code in (401, 403, 429):
                raise GeminiPermanentError(f"Gemini HTTP {resp.status_code}")
            if resp.status_code == 400:
                raise RuntimeError("Gemini HTTP 400 (request rejected)")
            resp.raise_for_status()
            parts = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if not text:
                raise ValueError("empty response")
            return text
        except (GeminiPermanentError, RuntimeError):
            raise
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            last_exc = exc
            if attempt < _GEMINI_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Gemini call failed: {last_exc.__class__.__name__}: {last_exc}")


def generate_bio_text(prompt: str, api_key: str) -> str:
    """Generate the draft biography with BIO_MODEL."""
    text = _gemini_call(prompt, api_key, BIO_MODEL)
    if "## " not in text:
        raise RuntimeError("Gemini response has no section headings")
    return text
