"""
edms.py — Early Day Motions (EDMs).

/edms                    list (search ?q=, sort, ?member=<MNIS id>, pagination)
/edms/<id>               301 -> /edms/<id>/<slug>
/edms/<id>/<slug>        one EDM: full text, every signatory, amendments
/sitemap-edms.xml        built here, routed in flask_app.py

Reads only from ha_edm / ha_edm_sponsor, filled by scripts/ingest_edms.py.
No Parliament API calls on page view.

Behind FEATURE_EDMS (hidden / beta / live, see feature_flags.py) so the code
can ship before migrations/edm_tables.py and the first backfill have run.
"""
import re
from datetime import date
from urllib.parse import urlencode

from flask import Blueprint, abort, redirect, render_template, request
from flask_login import current_user
from sqlalchemy import or_, select, text
from sqlalchemy.orm import load_only

from extensions import db, limiter
from feature_flags import feature_enabled
from hansard_archive import policy_areas as _pa
from hansard_archive.models import HaEdm, HaEdmSponsor

edms_bp = Blueprint("edms", __name__)

FLAG = "FEATURE_EDMS"
BASE_URL = "https://westminsterbrief.co.uk"
EDM_PUBLIC_URL = "https://edm.parliament.uk/early-day-motion/{id}"
MEMBER_CONTACT_URL = "https://members.parliament.uk/member/{id}/contact"

_PER_PAGE = 20
_MAX_PAGES = 50
_MAX_Q_LEN = 100
_EXCERPT_CHARS = 240

SORTS = {
    "newest": "Newest",
    "signatures": "Most signatures",
}


def edms_enabled() -> bool:
    return feature_enabled(FLAG, current_user)


def edm_path(edm) -> str:
    return f"/edms/{edm.id}/{edm.slug}" if edm.slug else f"/edms/{edm.id}"


def _human_date(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _human_date_iso(value: str | None) -> str:
    try:
        return _human_date(date.fromisoformat(value)) if value else ""
    except (TypeError, ValueError):
        return ""


def _excerpt(text_: str, limit: int = _EXCERPT_CHARS) -> str:
    text_ = " ".join((text_ or "").split())
    if len(text_) <= limit:
        return text_
    return text_[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _is_postgres() -> bool:
    return db.engine.dialect.name == "postgresql"


# ── Related Westminster Brief topics ──────────────────────────────────────────

# Whole-word keywords for the /brief/<slug> policy areas. Deliberately short
# and specific: a missed link is better than a misleading one. Specific-topic
# matches are computed at ingest time (ha_edm.related_topics).
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


def related_topics(title: str, text_: str, specific: list | None = None, limit: int = 4) -> list[dict]:
    """
    /brief/<slug> links: specific topics matched at ingest time first, then
    policy areas whose keywords appear in the EDM. De-duplicated by slug.
    """
    out = [t for t in (specific or []) if isinstance(t, dict) and t.get("slug") and t.get("label")]
    scored = []
    for slug, pattern in _TOPIC_PATTERNS.items():
        score = 2 * len(pattern.findall(title or "")) + len(pattern.findall(text_ or ""))
        area = _pa.by_slug(slug)
        if score >= 2 and area is not None:
            scored.append((score, area.label, slug))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out += [{"slug": slug, "label": label} for _, label, slug in scored]
    seen, unique = set(), []
    for t in out:
        if t["slug"] not in seen:
            seen.add(t["slug"])
            unique.append(t)
    return unique[:limit]


# ── Queries ───────────────────────────────────────────────────────────────────

def _search_filter(query, q: str):
    if _is_postgres():
        # search_tsv: generated column + GIN index from migrations/edm_tables.py.
        # websearch_to_tsquery never raises on user input (quotes, OR, -word).
        return query.filter(
            text("ha_edm.search_tsv @@ websearch_to_tsquery('english', :edm_q)")
        ).params(edm_q=q)
    like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    return query.filter(or_(HaEdm.title.ilike(like, escape="\\"),
                            HaEdm.motion_text.ilike(like, escape="\\")))


def _list_item(edm: HaEdm) -> dict:
    return {
        "id": edm.id,
        "uin": edm.uin,
        "title": edm.title,
        "path": edm_path(edm),
        "sponsor_name": edm.primary_sponsor_name or "Unknown member",
        "sponsor_party": edm.primary_sponsor_party or "",
        "human_date": _human_date(edm.date_tabled),
        "signatures": edm.signature_count,
        "excerpt": _excerpt(edm.motion_text),
        "is_amendment": bool(edm.amendment_to_id),
        "is_prayer": edm.is_prayer,
    }


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        return 1


# ── Routes ────────────────────────────────────────────────────────────────────

@edms_bp.route("/edms")
@limiter.limit("120 per minute")
def edm_list():
    if not edms_enabled():
        abort(404)
    q = " ".join((request.args.get("q") or "").split())[:_MAX_Q_LEN]
    sort = request.args.get("sort", "newest")
    if sort not in SORTS:
        sort = "newest"
    member_id = request.args.get("member", type=int)
    page = _page_arg()
    if page > _MAX_PAGES:
        abort(404)

    query = HaEdm.query.filter(HaEdm.status == "Published")
    member_name = ""
    if member_id:
        signer = (HaEdmSponsor.query
                  .filter(HaEdmSponsor.member_id == member_id)
                  .order_by(HaEdmSponsor.date_signed.desc()).first())
        member_name = signer.member_name if signer else ""
        query = query.filter(HaEdm.id.in_(
            select(HaEdmSponsor.edm_id).where(
                HaEdmSponsor.member_id == member_id,
                HaEdmSponsor.is_withdrawn.is_(False),
            )
        ))
    if q:
        query = _search_filter(query, q)

    total = query.count()
    total_pages = min(_MAX_PAGES, max(1, (total + _PER_PAGE - 1) // _PER_PAGE))
    if page > total_pages:
        abort(404)

    if sort == "signatures":
        query = query.order_by(HaEdm.signature_count.desc(), HaEdm.date_tabled.desc(), HaEdm.id.desc())
    else:
        query = query.order_by(HaEdm.date_tabled.desc(), HaEdm.id.desc())
    rows = (query.options(load_only(
                HaEdm.id, HaEdm.uin, HaEdm.title, HaEdm.motion_text, HaEdm.date_tabled,
                HaEdm.primary_sponsor_name, HaEdm.primary_sponsor_party, HaEdm.signature_count,
                HaEdm.amendment_to_id, HaEdm.is_prayer, HaEdm.slug))
            .offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all())

    def page_url(n: int) -> str:
        args = {}
        if q:
            args["q"] = q
        if member_id:
            args["member"] = member_id
        if sort != "newest":
            args["sort"] = sort
        if n > 1:
            args["page"] = n
        return "/edms" + ("?" + urlencode(args) if args else "")

    meta_desc = (
        "Browse and search Early Day Motions tabled in the House of Commons: "
        "who sponsored each one, how many MPs have signed, and the full list of supporters."
    )
    filtered = bool(q or member_id or sort != "newest")
    return render_template(
        "edms/edm_list.html",
        items=[_list_item(e) for e in rows],
        total=total,
        q=q,
        sort=sort,
        sorts=SORTS,
        member_id=member_id,
        member_name=member_name,
        page=page,
        total_pages=total_pages,
        per_page=_PER_PAGE,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < total_pages else "",
        meta_desc=meta_desc,
        filtered=filtered,
        robots_meta="noindex, follow" if filtered else "index, follow",
    )


@edms_bp.route("/edms/<int:edm_id>")
def edm_short(edm_id: int):
    if not edms_enabled():
        abort(404)
    edm = db.session.get(HaEdm, edm_id)
    if edm is None:
        abort(404)
    return redirect(edm_path(edm), code=301)


@edms_bp.route("/edms/<int:edm_id>/<string:slug>")
@limiter.limit("120 per minute")
def edm_detail(edm_id: int, slug: str):
    if not edms_enabled():
        abort(404)
    edm = db.session.get(HaEdm, edm_id)
    if edm is None:
        abort(404)
    if edm.slug and slug != edm.slug:
        return redirect(edm_path(edm), code=301)

    rows = HaEdmSponsor.query.filter(HaEdmSponsor.edm_id == edm.id).all()
    active = sorted(
        (s for s in rows if not s.is_withdrawn),
        key=lambda s: (s.sponsoring_order if s.sponsoring_order is not None else 10_000,
                       s.date_signed.isoformat() if s.date_signed else "9999"),
    )
    signatories = [{
        "name": s.member_name,
        "party": s.party or "",
        "constituency": s.constituency or "",
        "role": "Primary sponsor" if s.is_primary else ("Sponsor" if s.is_sponsor else "Supporter"),
        "human_signed": _human_date(s.date_signed),
        "member_id": s.member_id,
        "contact_url": MEMBER_CONTACT_URL.format(id=s.member_id) if s.member_id else "",
    } for s in active]
    party_counts: dict[str, int] = {}
    for s in signatories:
        party_counts[s["party"] or "Unknown"] = party_counts.get(s["party"] or "Unknown", 0) + 1

    amendments = [
        {**a, "human_date": _human_date_iso(a.get("date_tabled"))}
        for a in (edm.amendments or []) if isinstance(a, dict)
    ]
    parent = db.session.get(HaEdm, edm.amendment_to_id) if edm.amendment_to_id else None

    who = edm.primary_sponsor_name or "an MP"
    if edm.primary_sponsor_party:
        who += f" ({edm.primary_sponsor_party})"
    n = len(signatories)
    human_date = _human_date(edm.date_tabled)
    meta_desc = _excerpt(
        f"Early Day Motion {edm.uin} tabled by {who}"
        f"{' on ' + human_date if human_date else ''}. "
        f"Signed by {n} MP{'s' if n != 1 else ''}. {edm.motion_text or ''}",
        155,
    )
    withdrawn = edm.status == "Withdrawn"
    return render_template(
        "edms/edm_detail.html",
        edm=edm,
        human_date=human_date,
        checked_date=_human_date(edm.last_refreshed),
        signatories=signatories,
        withdrawn_count=len(rows) - len(active),
        party_counts=sorted(party_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        amendments=amendments,
        parent=parent,
        parent_path=edm_path(parent) if parent else "",
        official_url=EDM_PUBLIC_URL.format(id=edm.id),
        meta_desc=meta_desc,
        canonical_path=edm_path(edm),
        related=related_topics(edm.title, edm.motion_text or "", edm.related_topics),
        robots_meta="noindex, follow" if withdrawn else "index, follow",
    )


# ── Sitemap ───────────────────────────────────────────────────────────────────

def build_sitemap_xml() -> str:
    """Every published EDM, by id, lastmod = last change in our copy."""
    rows = (db.session.query(HaEdm.id, HaEdm.slug, HaEdm.updated_at)
            .filter(HaEdm.status == "Published")
            .order_by(HaEdm.id).all())
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for edm_id, slug, updated in rows:
        path = f"/edms/{edm_id}/{slug}" if slug else f"/edms/{edm_id}"
        lines.append('  <url>')
        lines.append(f'    <loc>{BASE_URL}{path}</loc>')
        if updated:
            lines.append(f'    <lastmod>{updated.date().isoformat()}</lastmod>')
        lines.append('  </url>')
    lines.append('</urlset>')
    return '\n'.join(lines)
