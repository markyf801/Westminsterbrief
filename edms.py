"""
edms.py — Early Day Motions (EDMs).

/edms        list of recent EDMs (search, sort, pagination)
/edms/<id>   one EDM: full text, every signatory, amendments

Live data from Parliament's Oral Questions and Motions API. Nothing is stored
in the database: responses are cached in-process (list pages 15 minutes,
detail pages 1 hour) and each request makes at most one API call.
"""
import logging
import re
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, render_template, request

from extensions import limiter
from hansard_archive import policy_areas as _pa

logger = logging.getLogger(__name__)

edms_bp = Blueprint("edms", __name__)

EDM_API_BASE = "https://oralquestionsandmotions-api.parliament.uk"
EDM_PUBLIC_URL = "https://edm.parliament.uk/early-day-motion/{id}"
MEMBER_CONTACT_URL = "https://members.parliament.uk/member/{id}/contact"

_TIMEOUT = 8               # seconds per API call
_PER_PAGE = 20
_MAX_PAGES = 50            # 1,000 most recent / most signed; nobody pages further
_MAX_Q_LEN = 100
_LIST_TTL = 15 * 60
_DETAIL_TTL = 60 * 60
_CACHE_MAX_ENTRIES = 500   # bounds memory when people search lots of terms
_EXCERPT_CHARS = 240
_SPONSOR_SLOTS = 6         # an EDM has up to six sponsors; later names are supporters

SORTS = {
    "newest": ("Newest", "DateTabledDesc"),
    "signatures": ("Most signatures", "SignatureCountDesc"),
}

_NOT_FOUND = object()      # cached marker for EDM ids the API does not know
_cache: dict[tuple, tuple[float, object]] = {}

_HEX_COLOUR_RE = re.compile(r"[0-9a-fA-F]{6}")


class EdmApiError(Exception):
    """Parliament's API could not be reached or returned something unusable."""

    def __init__(self, message: str, slow: bool = False):
        super().__init__(message)
        self.slow = slow


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_get(key: tuple, ttl: int):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_set(key: tuple, value) -> None:
    if key not in _cache and len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]
    _cache[key] = (time.time(), value)


# ── API ───────────────────────────────────────────────────────────────────────

def _api_get(path: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(f"{EDM_API_BASE}{path}", params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
    except requests.Timeout as exc:
        logger.warning("EDM API timed out: %s", path)
        raise EdmApiError(str(exc), slow=True) from exc
    except (requests.RequestException, ValueError) as exc:
        logger.warning("EDM API call failed: %s %s", path, exc)
        raise EdmApiError(str(exc)) from exc
    if not isinstance(body, dict):
        raise EdmApiError("unexpected response shape")
    return body


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def _human_date(d: datetime | None) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _excerpt(text: str, limit: int = _EXCERPT_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _member(raw: dict | None) -> dict:
    raw = raw or {}
    colour = raw.get("PartyColour") or ""
    mnis_id = raw.get("MnisId")
    return {
        "name": raw.get("Name") or "Unknown member",
        "party": raw.get("Party") or "",
        "constituency": raw.get("Constituency") or "",
        "colour": colour if _HEX_COLOUR_RE.fullmatch(colour) else "",
        "contact_url": MEMBER_CONTACT_URL.format(id=mnis_id) if isinstance(mnis_id, int) else "",
    }


def _summary(raw: dict) -> dict:
    tabled = _parse_date(raw.get("DateTabled"))
    text = (raw.get("MotionText") or "").strip()
    uin = raw.get("UINWithAmendmentSuffix") or str(raw.get("UIN") or "")
    return {
        "id": raw.get("Id"),
        "uin": uin,
        "title": raw.get("Title") or f"Early Day Motion {uin}",
        "sponsor": _member(raw.get("PrimarySponsor")),
        "date_tabled": tabled,
        "human_date": _human_date(tabled),
        "signatures": raw.get("SponsorsCount") or 0,
        "text": text,
        "excerpt": _excerpt(text),
        "is_amendment": bool(raw.get("AmendmentToMotionId")),
        "amends_id": raw.get("AmendmentToMotionId"),
        "si_title": raw.get("StatutoryInstrumentTitle") or "",
        "is_withdrawn": raw.get("Status") in (1, "Withdrawn"),
        "official_url": EDM_PUBLIC_URL.format(id=raw.get("Id")),
    }


def _signatories(sponsors: list | None) -> tuple[list[dict], int]:
    """Active signatories in signing order, plus how many names were withdrawn."""
    active, withdrawn = [], 0
    for s in sponsors or []:
        if s.get("IsWithdrawn"):
            withdrawn += 1
            continue
        order = s.get("SponsoringOrder")
        signed = _parse_date(s.get("CreatedWhen"))
        if order == 1:
            role = "Primary sponsor"
        elif isinstance(order, int) and order <= _SPONSOR_SLOTS:
            role = "Sponsor"
        else:
            role = "Supporter"
        active.append({
            **_member(s.get("Member")),
            "role": role,
            "order": order if isinstance(order, int) else 10_000,
            "date_signed": signed,
            "human_signed": _human_date(signed),
        })
    active.sort(key=lambda s: (s["order"], s["date_signed"] or datetime.max))
    return active, withdrawn


def fetch_edm_list(q: str, sort: str, page: int) -> dict:
    """One page of published EDMs: {'items': [...], 'total': int}. Cached 15 minutes."""
    key = ("list", q.lower(), sort, page)
    cached = _cache_get(key, _LIST_TTL)
    if cached is not None:
        return cached

    params = {
        "parameters.statuses": "Published",
        "parameters.orderBy": SORTS[sort][1],
        "parameters.take": _PER_PAGE,
        "parameters.skip": (page - 1) * _PER_PAGE,
    }
    if q:
        params["parameters.searchTerm"] = q
    body = _api_get("/EarlyDayMotions/list", params)
    rows = body.get("Response")
    if not isinstance(rows, list):
        raise EdmApiError("list response missing")
    paging = body.get("PagingInfo") or {}
    result = {
        "items": [_summary(r) for r in rows if isinstance(r, dict) and r.get("Id")],
        "total": int(paging.get("Total") or 0),
    }
    _cache_set(key, result)
    return result


def fetch_edm(edm_id: int) -> dict | None:
    """One EDM with signatories and amendments, or None if it does not exist. Cached 1 hour."""
    key = ("detail", edm_id)
    cached = _cache_get(key, _DETAIL_TTL)
    if cached is not None:
        return None if cached is _NOT_FOUND else cached

    body = _api_get(f"/EarlyDayMotion/{edm_id}")
    raw = body.get("Response")
    if body.get("StatusCode") == 404 or not raw:
        _cache_set(key, _NOT_FOUND)
        return None
    if not isinstance(raw, dict):
        raise EdmApiError("detail response missing")

    edm = _summary(raw)
    edm["signatories"], edm["withdrawn_count"] = _signatories(raw.get("Sponsors"))
    edm["signatures"] = len(edm["signatories"])
    edm["party_counts"] = Counter(
        s["party"] or "Unknown" for s in edm["signatories"]
    ).most_common()
    edm["amendments"] = []
    for a in raw.get("Amendments") or []:
        if not isinstance(a, dict):
            continue
        amendment = _summary(a)
        amendment["signatures"] = len(_signatories(a.get("Sponsors"))[0])
        edm["amendments"].append(amendment)
    _cache_set(key, edm)
    return edm


# ── Related Westminster Brief topics ──────────────────────────────────────────

# Whole-word keywords for the /brief/<slug> policy areas. Deliberately short
# and specific: a missed link is better than a misleading one.
_TOPIC_KEYWORDS = {
    "education": ("school", "schools", "pupils", "teachers", "university",
                  "universities", "students", "education", "childcare"),
    "health-and-social-care": ("nhs", "hospital", "hospitals", "hospice", "hospices",
                               "cancer", "patients", "social care", "mental health", "dementia"),
    "housing-and-planning": ("housing", "homes", "renters", "tenants", "homelessness", "planning"),
    "transport": ("rail", "railway", "bus", "buses", "roads", "transport", "airport", "trains"),
    "crime-justice-and-law": ("police", "policing", "crime", "prison", "prisons",
                              "courts", "justice", "victims"),
    "welfare-and-social-security": ("universal credit", "benefits", "welfare", "poverty"),
    "immigration-and-asylum": ("asylum", "immigration", "refugees", "visa", "migrants"),
    "environment-and-climate-change": ("climate", "net zero", "pollution", "sewage", "nature"),
    "defence-and-national-security": ("armed forces", "veterans", "defence", "military"),
    "energy-and-utilities": ("energy", "fuel poverty", "electricity", "gas bills", "water bills"),
    "work-and-pensions": ("pension", "pensions", "state pension"),
    "employment-and-labour-market": ("workers", "employment", "wages", "minimum wage"),
    "agriculture-environment-and-rural-affairs": ("farmers", "farming", "agriculture",
                                                  "rural", "animal welfare"),
    "culture-media-and-sport": ("football", "sport", "arts", "museum", "bbc", "music", "heritage"),
    "science-technology-and-innovation": ("science", "research", "technology",
                                          "artificial intelligence", "online safety"),
}
_TOPIC_PATTERNS = {
    slug: re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    for slug, words in _TOPIC_KEYWORDS.items()
}


def related_topics(title: str, text: str, limit: int = 3) -> list[dict]:
    """Up to `limit` /brief/<slug> policy areas whose keywords appear in the EDM."""
    scored = []
    for slug, pattern in _TOPIC_PATTERNS.items():
        score = 2 * len(pattern.findall(title)) + len(pattern.findall(text))
        area = _pa.by_slug(slug)
        if score >= 2 and area is not None:
            scored.append((score, area.label, slug))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [{"slug": slug, "label": label} for _, label, slug in scored[:limit]]


# ── Routes ────────────────────────────────────────────────────────────────────

def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        return 1


_AUTO_RETRIES = 2   # meta-refreshes after a timeout before we stop and just show the message


def _unavailable(heading: str, exc: EdmApiError):
    """
    Friendly 503 page. Parliament's API often needs 5-20s for an uncached search
    or sort, but it finishes the query after we give up and caches it briefly,
    so after a timeout the page refreshes itself (at most _AUTO_RETRIES times,
    10s apart) and the repeat request is usually fast.
    """
    args = request.args.to_dict()
    try:
        tries = int(args.pop("retry", 0) or 0)
    except (ValueError, TypeError):
        tries = _AUTO_RETRIES
    again_url = request.path + ("?" + urlencode(args) if args else "")
    retry_url = ""
    if exc.slow and tries < _AUTO_RETRIES:
        retry_url = request.path + "?" + urlencode({**args, "retry": tries + 1})
    return render_template("edms/edm_unavailable.html", heading=heading, slow=exc.slow,
                           again_url=again_url, retry_url=retry_url,
                           robots_meta="noindex, follow"), 503


@edms_bp.route("/edms")
@limiter.limit("60 per minute")
def edm_list():
    q = " ".join((request.args.get("q") or "").split())[:_MAX_Q_LEN]
    sort = request.args.get("sort", "newest")
    if sort not in SORTS:
        sort = "newest"
    page = _page_arg()
    if page > _MAX_PAGES:
        abort(404)

    try:
        data = fetch_edm_list(q, sort, page)
    except EdmApiError as exc:
        return _unavailable("Early Day Motions", exc)

    total = data["total"]
    total_pages = min(_MAX_PAGES, max(1, (total + _PER_PAGE - 1) // _PER_PAGE))
    if page > total_pages:
        abort(404)

    def page_url(n: int) -> str:
        args = {}
        if q:
            args["q"] = q
        if sort != "newest":
            args["sort"] = sort
        if n > 1:
            args["page"] = n
        return "/edms" + ("?" + urlencode(args) if args else "")

    meta_desc = (
        "Browse and search Early Day Motions tabled in the House of Commons: "
        "who sponsored each one, how many MPs have signed, and the full list of supporters."
    )
    return render_template(
        "edms/edm_list.html",
        items=data["items"],
        total=total,
        q=q,
        sort=sort,
        sorts=SORTS,
        page=page,
        total_pages=total_pages,
        per_page=_PER_PAGE,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < total_pages else "",
        meta_desc=meta_desc,
        robots_meta="noindex, follow" if (q or sort != "newest") else "index, follow",
    )


@edms_bp.route("/edms/<int:edm_id>")
@limiter.limit("60 per minute")
def edm_detail(edm_id: int):
    try:
        edm = fetch_edm(edm_id)
    except EdmApiError as exc:
        return _unavailable("Early Day Motion", exc)
    if edm is None:
        abort(404)

    who = edm["sponsor"]["name"]
    if edm["sponsor"]["party"]:
        who += f" ({edm['sponsor']['party']})"
    n = edm["signatures"]
    meta_desc = _excerpt(
        f"Early Day Motion {edm['uin']} tabled by {who}"
        f"{' on ' + edm['human_date'] if edm['human_date'] else ''}. "
        f"Signed by {n} MP{'s' if n != 1 else ''}. {edm['text']}",
        155,
    )
    return render_template(
        "edms/edm_detail.html",
        edm=edm,
        meta_desc=meta_desc,
        canonical_path=f"/edms/{edm_id}",
        related=related_topics(edm["title"], edm["text"]),
        robots_meta="index, follow",
    )
