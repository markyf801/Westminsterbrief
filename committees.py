"""
committees.py — Parliamentary committees and All-Party Parliamentary Groups.

/committees                    current committees (filter ?house=, ?type=, ?topic=, ?q=)
                               plus the open calls for evidence
/committees/<id>               301 -> /committees/<id>/<slug>
/committees/<id>/<slug>        members and roles, open calls for evidence,
                               current inquiries, recent reports
/appgs                         APPGs (search ?q=, ?topic=, ?category=, pagination)
/appgs/<id>                    301 -> /appgs/<id>/<slug>
/appgs/<id>/<slug>             purpose, officers, secretariat, benefits
/sitemap-committees.xml        built here (build_sitemap_xml), routed in flask_app.py

Reads only from the tables in committees_models.py, filled by
scripts/ingest_committees.py and scripts/ingest_appgs.py. No Parliament API
calls on page view.

Behind FEATURE_COMMITTEES (hidden / beta / live, see feature_flags.py) so the
code can ship before migrations/committees_appgs_tables.py and the first
ingest runs.

Helpers for other features:
  get_member_committees_appgs(member_id)  committees + APPG roles for a member page
  open_calls_for_evidence(keyword=, topic=)  "Opportunities to influence" (weekly digest)
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from urllib.parse import urlencode

from flask import Blueprint, abort, current_app, redirect, render_template, request
from flask_login import current_user
from sqlalchemy import or_, select

from committees_models import (
    Appg, AppgOfficer, Committee, CommitteeInquiry, CommitteeInquiryLink,
    CommitteeMembership, CommitteePublication,
)
from extensions import db, limiter
from feature_flags import feature_enabled
from hansard_archive import policy_areas as _pa

committees_bp = Blueprint("committees", __name__)

FLAG = "FEATURE_COMMITTEES"
BASE_URL = "https://westminsterbrief.co.uk"
COMMITTEE_PUBLIC_URL = "https://committees.parliament.uk/committee/{id}/"
INQUIRY_PUBLIC_URL = "https://committees.parliament.uk/work/{id}/"
SUBMIT_EVIDENCE_URL = "https://committees.parliament.uk/call-for-evidence/{id}/"
MEMBER_PUBLIC_URL = "https://members.parliament.uk/member/{id}/"
APPG_REGISTER_URL = ("https://www.parliament.uk/mps-lords-and-offices/standards-and-financial-interests/"
                     "parliamentary-commissioner-for-standards/registers-of-interests/"
                     "register-of-all-party-party-parliamentary-groups/")

_MAX_Q_LEN = 100
_APPG_PER_PAGE = 50
_RECENT_REPORTS = 10
_EXCERPT_CHARS = 220

HOUSES = {"commons": "Commons", "lords": "Lords", "joint": "Joint"}
TYPES = {
    "select": "Select committees",
    "general": "Bill and delegated legislation committees",
    "other": "Other committees",
    "all": "All committees",
}
DEFAULT_TYPE = "select"
LINKED_CONFIDENCE = ("high", "medium")   # APPG officer matches we link to a member page
APPG_CATEGORIES = {  # ?category= key -> (register value, label)
    "subject": ("Subject Group", "Subject groups"),
    "country": ("Country, Area or Region Group", "Country, area and region groups"),
}


def committees_enabled() -> bool:
    return feature_enabled(FLAG, current_user)


# ── Text helpers (also used by the ingest scripts) ────────────────────────────

_MOJIBAKE_RE = re.compile("[ÂÃâ][\u0080-¿ŒœŠšŸŽž"
                          "ƒˆ˜–—‘’‚“”„†"
                          "‡•…‰‹›€™]")


def fix_mojibake(s: str) -> str:
    """
    Undo UTF-8 text that was decoded as Windows-1252 somewhere upstream
    (the Committees API returns e.g. 'Childrenâ€™s'). Only applied when the
    tell-tale pattern is present, and only kept if the round trip succeeds.
    """
    if not s or not _MOJIBAKE_RE.search(s):
        return s
    try:
        fixed = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed


_BLOCK_TAG_RE = re.compile(r"(?i)</?(p|div|br|li|ul|ol|h[1-6]|tr|table)\b[^>]*>")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    """HTML or plain text -> plain text with paragraphs separated by blank lines."""
    if not value:
        return ""
    s = _BLOCK_TAG_RE.sub("\n", value)
    s = _TAG_RE.sub("", s)
    s = fix_mojibake(html.unescape(s)).replace(" ", " ")
    paras = [" ".join(p.split()) for p in s.split("\n")]
    return "\n\n".join(p for p in paras if p)


def excerpt(text_: str | None, limit: int = _EXCERPT_CHARS) -> str:
    text_ = " ".join((text_ or "").split())
    if len(text_) <= limit:
        return text_
    return text_[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _human_date(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _human_deadline(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.hour == 23 and dt.minute >= 59 or (dt.hour == 0 and dt.minute == 0):
        return _human_date(dt)
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    minutes = f":{dt.minute:02d}" if dt.minute else ""
    return f"{hour}{minutes}{suffix}, {_human_date(dt)}"


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


# ── Topics ────────────────────────────────────────────────────────────────────

# Whole-word keywords per /brief policy area. Short and specific on purpose:
# a missed topic is better than a misleading one.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "economy": ("economy", "economic", "inflation", "productivity", "cost of living"),
    "employment-and-labour-market": ("employment", "unemployment", "workers", "workforce", "wages",
                                     "minimum wage", "apprenticeships", "skills"),
    "finance-and-taxation": ("treasury", "tax", "taxation", "banking", "banks", "financial services",
                             "monetary policy", "public finances"),
    "government-and-public-administration": ("public accounts", "public administration", "civil service",
                                             "local government", "public services", "procurement"),
    "business-and-industry": ("business", "businesses", "industry", "manufacturing", "trade",
                              "small business", "retail", "entrepreneurship"),
    "education": ("education", "school", "schools", "pupils", "teachers", "university",
                  "universities", "students", "childcare", "early years", "send"),
    "health-and-social-care": ("health", "nhs", "hospital", "hospitals", "hospice", "hospices", "cancer",
                               "patients", "social care", "mental health", "dementia", "disease",
                               "medicines", "carers", "end of life"),
    "housing-and-planning": ("housing", "homes", "renters", "tenants", "homelessness", "planning",
                             "social housing", "leasehold"),
    "transport": ("transport", "rail", "railway", "railways", "bus", "buses", "roads", "aviation",
                  "airport", "airports", "cycling", "motoring", "shipping", "maritime"),
    "crime-justice-and-law": ("justice", "police", "policing", "crime", "prison", "prisons", "courts",
                              "victims", "abuse", "violence", "legal aid", "home affairs"),
    "welfare-and-social-security": ("welfare", "benefits", "universal credit", "poverty",
                                    "child poverty", "social security", "disability"),
    "immigration-and-asylum": ("immigration", "asylum", "refugees", "migrants", "migration", "visa", "visas"),
    "environment-and-climate-change": ("environment", "environmental", "climate", "net zero", "pollution",
                                       "sewage", "nature", "biodiversity", "air quality", "adaptation"),
    "defence-and-national-security": ("defence", "armed forces", "veterans", "military", "national security",
                                      "intelligence", "cyber security"),
    "international-affairs": ("international development", "aid", "humanitarian", "international trade",
                              "overseas"),
    "science-technology-and-innovation": ("science", "technology", "research", "innovation",
                                          "artificial intelligence", "digital", "online safety", "space"),
    "energy-and-utilities": ("energy", "electricity", "fuel poverty", "nuclear", "renewable",
                             "renewables", "water industry", "utilities"),
    "work-and-pensions": ("pension", "pensions", "work and pensions", "state pension", "jobcentre"),
    "agriculture-environment-and-rural-affairs": ("agriculture", "farming", "farmers", "rural", "food",
                                                  "fisheries", "animal welfare", "animals", "zoos"),
    "culture-media-and-sport": ("culture", "media", "sport", "sports", "arts", "museums", "heritage",
                                "music", "football", "bbc", "tourism", "libraries", "reading"),
    "constitutional-affairs": ("constitution", "constitutional", "devolution", "elections", "electoral",
                               "human rights", "democracy"),
    "foreign-affairs": ("foreign affairs", "foreign policy", "diplomacy", "diplomatic", "sanctions"),
    "parliamentary-affairs": ("procedure", "standards", "privileges", "modernisation", "select committees"),
}
_TOPIC_PATTERNS = {
    slug: re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    for slug, words in TOPIC_KEYWORDS.items()
}


def topic_slugs_for(title: str, *texts: str, limit: int = 4, threshold: int = 2) -> str:
    """
    Policy-area slugs for a committee / inquiry / APPG, stored as
    ",slug-a,slug-b," so a LIKE '%,slug,%' filter works on SQLite and Postgres.
    A title hit counts 3, each hit in the other text 1.
    """
    body = " ".join(t for t in texts if t)
    scored = []
    for slug, pattern in _TOPIC_PATTERNS.items():
        score = 3 * len(pattern.findall(title or "")) + len(pattern.findall(body))
        if score >= threshold:
            scored.append((score, slug))
    scored.sort(key=lambda t: (-t[0], t[1]))
    slugs = [s for _, s in scored[:limit]]
    return ("," + ",".join(slugs) + ",") if slugs else ""


def topics_from_slugs(value: str | None) -> list[dict]:
    out = []
    for slug in (value or "").strip(",").split(","):
        area = _pa.by_slug(slug) if slug else None
        if area is not None:
            out.append({"slug": slug, "label": area.label})
    return out


def topic_options() -> list[dict]:
    return [{"slug": a.slug, "label": a.label}
            for a in sorted(_pa.all_areas(), key=lambda a: a.label) if a.slug in TOPIC_KEYWORDS]


# ── Paths ─────────────────────────────────────────────────────────────────────

def committee_path(c) -> str:
    return f"/committees/{c.id}/{c.slug}" if c.slug else f"/committees/{c.id}"


def appg_path(a) -> str:
    return f"/appgs/{a.id}/{a.slug}" if a.slug else f"/appgs/{a.id}"


def _known_member_ids(member_ids: set[int]) -> set[int]:
    """The ids /members/<id> can serve (member_profile or cached_member); empty on any error."""
    if not member_ids:
        return set()
    try:
        from cache_models import CachedMember, MemberProfile
        known = {i for (i,) in db.session.query(MemberProfile.member_id)
                 .filter(MemberProfile.member_id.in_(member_ids)).all()}
        known |= {i for (i,) in db.session.query(CachedMember.member_id)
                  .filter(CachedMember.member_id.in_(member_ids)).all()}
        return known
    except Exception:
        db.session.rollback()
        return set()


def member_urls(member_ids) -> dict[int, str]:
    """
    {member_id: url}: our /members/<id> page when this app has the route and
    holds the member (otherwise that route 404s), else members.parliament.uk.
    """
    ids = {int(i) for i in member_ids if i}
    try:
        has_member_pages = any(r.rule == "/members/<int:member_id>" for r in current_app.url_map.iter_rules())
    except RuntimeError:
        has_member_pages = False
    known = _known_member_ids(ids) if has_member_pages else set()
    return {i: (f"/members/{i}" if i in known else MEMBER_PUBLIC_URL.format(id=i)) for i in ids}


# ── Shared queries ────────────────────────────────────────────────────────────

def _open_call_item(inq: CommitteeInquiry, committees: list[Committee], now: datetime) -> dict:
    periods = inq.submission_periods or []
    first = next((p for p in periods if p.get("id")), {})
    days_left = None
    if inq.evidence_deadline:
        days_left = (inq.evidence_deadline.date() - now.date()).days
    return {
        "inquiry_id": inq.id,
        "title": inq.title,
        "business_type": inq.business_type or "",
        "committees": [{"id": c.id, "name": c.name, "path": committee_path(c), "house": c.house}
                       for c in committees],
        "deadline": inq.evidence_deadline,
        "human_deadline": _human_deadline(inq.evidence_deadline),
        "days_left": days_left,
        "scope_excerpt": excerpt(inq.scope, 260),
        "inquiry_url": INQUIRY_PUBLIC_URL.format(id=inq.id),
        "submit_url": SUBMIT_EVIDENCE_URL.format(id=first["id"]) if first.get("id") else "",
        "topics": topics_from_slugs(inq.topic_slugs),
    }


def _committees_for_inquiries(inquiry_ids: list[int]) -> dict[int, list[Committee]]:
    if not inquiry_ids:
        return {}
    rows = (db.session.query(CommitteeInquiryLink.inquiry_id, Committee)
            .join(Committee, Committee.id == CommitteeInquiryLink.committee_id)
            .filter(CommitteeInquiryLink.inquiry_id.in_(inquiry_ids))
            .order_by(Committee.name).all())
    out: dict[int, list[Committee]] = {}
    for inquiry_id, c in rows:
        out.setdefault(inquiry_id, []).append(c)
    return out


def open_calls_for_evidence(keyword: str | None = None, topic: str | None = None,
                            committee_id: int | None = None, house: str | None = None,
                            limit: int | None = None, now: datetime | None = None) -> list[dict]:
    """
    Calls for evidence open now, soonest deadline first (open-ended ones last).

    keyword: whole phrase matched (case-insensitive) in the inquiry title or
             scope, or the committee name.
    topic:   a /brief policy-area slug (see TOPIC_KEYWORDS).
    Intended for the weekly digest's "Opportunities to influence" section:
        open_calls_for_evidence(topic="health-and-social-care")
    Each item: title, committees [{id, name, path}], deadline (datetime|None),
    human_deadline, days_left, inquiry_url, submit_url, scope_excerpt, topics.
    """
    now = now or datetime.utcnow()
    query = (CommitteeInquiry.query
             .filter(CommitteeInquiry.status == "Open", CommitteeInquiry.evidence_open.is_(True))
             .filter(or_(CommitteeInquiry.evidence_deadline.is_(None),
                         CommitteeInquiry.evidence_deadline >= now)))
    if committee_id:
        query = query.filter(CommitteeInquiry.id.in_(
            select(CommitteeInquiryLink.inquiry_id).where(CommitteeInquiryLink.committee_id == committee_id)))
    if house:
        query = query.filter(CommitteeInquiry.id.in_(
            select(CommitteeInquiryLink.inquiry_id)
            .join(Committee, Committee.id == CommitteeInquiryLink.committee_id)
            .where(Committee.house == house)))
    if keyword:
        like = _like(" ".join(keyword.split())[:_MAX_Q_LEN])
        query = query.filter(or_(
            CommitteeInquiry.title.ilike(like, escape="\\"),
            CommitteeInquiry.scope.ilike(like, escape="\\"),
            CommitteeInquiry.id.in_(
                select(CommitteeInquiryLink.inquiry_id)
                .join(Committee, Committee.id == CommitteeInquiryLink.committee_id)
                .where(Committee.name.ilike(like, escape="\\"))),
        ))
    if topic:
        query = query.filter(CommitteeInquiry.topic_slugs.like(f"%,{topic},%"))
    rows = query.all()
    rows.sort(key=lambda i: (i.evidence_deadline is None, i.evidence_deadline or datetime.max, i.title))
    if limit:
        rows = rows[:limit]
    by_inquiry = _committees_for_inquiries([i.id for i in rows])
    return [_open_call_item(i, by_inquiry.get(i.id, []), now) for i in rows]


def get_member_committees_appgs(member_id: int) -> dict:
    """
    Current committee memberships and APPG officer roles for one member (MNIS id).
    For a member page "Committees and APPGs" section:
        {"committees": [{id, name, path, house, role, is_chair}],
         "appgs": [{id, name, path, role, is_chair}]}
    Only APPG officers matched with high/medium confidence carry a member_id.
    Returns empty lists (never raises) if the tables are missing or empty.
    """
    out: dict = {"committees": [], "appgs": []}
    if not member_id:
        return out
    try:
        rows = (db.session.query(CommitteeMembership, Committee)
                .join(Committee, Committee.id == CommitteeMembership.committee_id)
                .filter(CommitteeMembership.member_id == member_id, Committee.is_current.is_(True))
                .order_by(CommitteeMembership.is_chair.desc(), Committee.name).all())
        out["committees"] = [{"id": c.id, "name": c.name, "path": committee_path(c), "house": c.house,
                              "role": m.role, "is_chair": m.is_chair} for m, c in rows]
        rows = (db.session.query(AppgOfficer, Appg)
                .join(Appg, Appg.id == AppgOfficer.appg_id)
                .filter(AppgOfficer.member_id == member_id, Appg.is_current.is_(True),
                        AppgOfficer.match_confidence.in_(LINKED_CONFIDENCE))
                .order_by(AppgOfficer.is_chair.desc(), Appg.short_name).all())
        out["appgs"] = [{"id": a.id, "name": a.short_name, "path": appg_path(a), "role": o.role,
                         "is_chair": o.is_chair} for o, a in rows]
    except Exception:  # missing tables before the migration has run
        db.session.rollback()
        return {"committees": [], "appgs": []}
    return out


# ── /committees ───────────────────────────────────────────────────────────────

def _clean_q() -> str:
    return " ".join((request.args.get("q") or "").split())[:_MAX_Q_LEN]


def _topic_arg() -> str:
    topic = request.args.get("topic", "")
    return topic if topic in TOPIC_KEYWORDS else ""


@committees_bp.route("/committees")
@limiter.limit("120 per minute")
def committee_list():
    if not committees_enabled():
        abort(404)
    q = _clean_q()
    house_key = request.args.get("house", "")
    house = HOUSES.get(house_key, "")
    ctype = request.args.get("type", DEFAULT_TYPE)
    if ctype not in TYPES:
        ctype = DEFAULT_TYPE
    topic = _topic_arg()

    query = Committee.query.filter(Committee.is_current.is_(True), Committee.show_on_website.is_(True))
    if house:
        query = query.filter(Committee.house == house)
    if ctype != "all":
        query = query.filter(Committee.category == ctype.capitalize())
    if topic:
        query = query.filter(Committee.topic_slugs.like(f"%,{topic},%"))
    if q:
        like = _like(q)
        query = query.filter(or_(
            Committee.name.ilike(like, escape="\\"),
            Committee.purpose.ilike(like, escape="\\"),
            Committee.id.in_(
                select(CommitteeInquiryLink.committee_id)
                .join(CommitteeInquiry, CommitteeInquiry.id == CommitteeInquiryLink.inquiry_id)
                .where(CommitteeInquiry.status == "Open",
                       or_(CommitteeInquiry.title.ilike(like, escape="\\"),
                           CommitteeInquiry.scope.ilike(like, escape="\\")))),
        ))
    house_order = {"Commons": 0, "Lords": 1, "Joint": 2}
    rows = sorted(query.all(), key=lambda c: (house_order.get(c.house, 3), c.name.lower()))
    ids = [c.id for c in rows]

    chairs: dict[int, list[str]] = {}
    sizes: dict[int, int] = {}
    open_counts: dict[int, int] = {}
    if ids:
        for m in (CommitteeMembership.query
                  .filter(CommitteeMembership.committee_id.in_(ids)).all()):
            sizes[m.committee_id] = sizes.get(m.committee_id, 0) + 1
            if m.is_chair:
                chairs.setdefault(m.committee_id, []).append(m.name)
        for cid, n in (db.session.query(CommitteeInquiryLink.committee_id, db.func.count())
                       .join(CommitteeInquiry, CommitteeInquiry.id == CommitteeInquiryLink.inquiry_id)
                       .filter(CommitteeInquiryLink.committee_id.in_(ids), CommitteeInquiry.status == "Open")
                       .group_by(CommitteeInquiryLink.committee_id).all()):
            open_counts[cid] = n

    calls = open_calls_for_evidence(keyword=q or None, topic=topic or None, house=house or None)
    calls_by_committee: dict[int, int] = {}
    for call in calls:
        for c in call["committees"]:
            calls_by_committee[c["id"]] = calls_by_committee.get(c["id"], 0) + 1

    items = [{
        "id": c.id,
        "name": c.name,
        "path": committee_path(c),
        "house": c.house or "",
        "category": c.category or "",
        "chairs": chairs.get(c.id, []),
        "members": sizes.get(c.id, 0),
        "open_inquiries": open_counts.get(c.id, 0),
        "open_calls": calls_by_committee.get(c.id, 0),
        "purpose_excerpt": excerpt(c.purpose, 200),
    } for c in rows]

    filtered = bool(q or house or topic or ctype != DEFAULT_TYPE)
    meta_desc = ("Find the UK Parliament select committees that cover your issue: who sits on each "
                 "committee, current inquiries, open calls for evidence and deadlines, and recent reports.")
    return render_template(
        "committees/committee_list.html",
        items=items,
        calls=calls,
        q=q, house_key=house_key if house else "", houses=HOUSES,
        ctype=ctype, types=TYPES, topic=topic, topics=topic_options(),
        topic_label=_pa.by_slug(topic).label if topic else "",
        filtered=filtered,
        meta_desc=meta_desc,
        robots_meta="noindex, follow" if filtered else "index, follow",
    )


@committees_bp.route("/committees/<int:committee_id>")
def committee_short(committee_id: int):
    if not committees_enabled():
        abort(404)
    c = db.session.get(Committee, committee_id)
    if c is None:
        abort(404)
    return redirect(committee_path(c), code=301)


_ROLE_ORDER = {"Chair": 0}


@committees_bp.route("/committees/<int:committee_id>/<string:slug>")
@limiter.limit("120 per minute")
def committee_detail(committee_id: int, slug: str):
    if not committees_enabled():
        abort(404)
    c = db.session.get(Committee, committee_id)
    if c is None:
        abort(404)
    if c.slug and slug != c.slug:
        return redirect(committee_path(c), code=301)

    members = CommitteeMembership.query.filter(CommitteeMembership.committee_id == c.id).all()
    members.sort(key=lambda m: (not m.is_chair, m.role == "Member", m.is_lay_member, m.name.lower()))
    urls = member_urls(m.member_id for m in members)
    member_rows = [{
        "name": m.name,
        "party": m.party or "",
        "member_from": m.member_from or "",
        "house": m.house or "",
        "role": m.role,
        "is_chair": m.is_chair,
        "is_lay_member": m.is_lay_member,
        "since": _human_date(m.start_date),
        "member_id": m.member_id,
        "url": urls.get(m.member_id, ""),
        "official_url": MEMBER_PUBLIC_URL.format(id=m.member_id) if m.member_id else "",
    } for m in members]

    now = datetime.utcnow()
    calls = open_calls_for_evidence(committee_id=c.id, now=now)
    call_ids = {call["inquiry_id"] for call in calls}
    inquiries = (CommitteeInquiry.query
                 .filter(CommitteeInquiry.status == "Open",
                         CommitteeInquiry.id.in_(select(CommitteeInquiryLink.inquiry_id)
                                                 .where(CommitteeInquiryLink.committee_id == c.id)))
                 .all())
    inquiries.sort(key=lambda i: (not i.is_inquiry, -(i.open_date or date.min).toordinal(), i.title))
    joint = _committees_for_inquiries([i.id for i in inquiries])
    inquiry_rows = [{
        "id": i.id,
        "title": i.title,
        "business_type": i.business_type or "",
        "is_inquiry": i.is_inquiry,
        "opened": _human_date(i.open_date),
        "scope_excerpt": excerpt(i.scope, 300),
        "url": INQUIRY_PUBLIC_URL.format(id=i.id),
        "has_open_call": i.id in call_ids,
        "next_oral_evidence": _human_date(i.next_oral_evidence),
        "joint_with": [{"name": o.name, "path": committee_path(o)} for o in joint.get(i.id, []) if o.id != c.id],
    } for i in inquiries]

    reports = (CommitteePublication.query
               .filter(CommitteePublication.committee_id == c.id)
               .order_by(CommitteePublication.publication_date.desc().nullslast(),
                         CommitteePublication.publication_id.desc())
               .limit(_RECENT_REPORTS).all())
    report_rows = [{
        "description": r.description,
        "type": r.publication_type or "",
        "date": _human_date(r.publication_date),
        "paper_number": r.paper_number or "",
        "url": r.document_url or "",
        "inquiry_title": r.inquiry_title or "",
        "inquiry_url": INQUIRY_PUBLIC_URL.format(id=r.inquiry_id) if r.inquiry_id else "",
        "response_due": _human_date(r.response_deadline) if r.response_deadline and r.response_deadline >= now.date() else "",
    } for r in reports]

    chair_names = [m["name"] for m in member_rows if m["is_chair"]]
    house_label = {"Commons": "House of Commons", "Lords": "House of Lords",
                   "Joint": "Joint (Commons and Lords)"}.get(c.house or "", "UK Parliament")
    lead = f"{c.name} ({house_label})"
    if chair_names:
        lead += f", chaired by {' and '.join(chair_names)}"
    meta_desc = excerpt(
        f"{lead}. {len(member_rows)} members, {len(inquiry_rows)} current inquiries"
        f"{', ' + str(len(calls)) + ' open calls for evidence' if calls else ''}. {c.purpose or ''}", 155)

    sub_committees = Committee.query.filter(Committee.parent_id == c.id, Committee.is_current.is_(True)).all()
    parent = db.session.get(Committee, c.parent_id) if c.parent_id else None
    return render_template(
        "committees/committee_detail.html",
        c=c,
        house_label=house_label,
        purpose_paras=[p for p in (c.purpose or "").split("\n\n") if p],
        members=member_rows,
        calls=calls,
        inquiries=inquiry_rows,
        reports=report_rows,
        topics=topics_from_slugs(c.topic_slugs),
        sub_committees=[{"name": s.name, "path": committee_path(s)} for s in sorted(sub_committees, key=lambda s: s.name)],
        parent={"name": parent.name, "path": committee_path(parent)} if parent else None,
        official_url=COMMITTEE_PUBLIC_URL.format(id=c.id),
        checked_date=_human_date(c.last_refreshed),
        canonical_path=committee_path(c),
        meta_desc=meta_desc,
        robots_meta="index, follow" if c.is_current and c.show_on_website else "noindex, follow",
    )


# ── /appgs ────────────────────────────────────────────────────────────────────

def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        return 1


@committees_bp.route("/appgs")
@limiter.limit("120 per minute")
def appg_list():
    if not committees_enabled():
        abort(404)
    q = _clean_q()
    topic = _topic_arg()
    cat_key = request.args.get("category", "")
    category = APPG_CATEGORIES.get(cat_key, ("", ""))[0]
    page = _page_arg()

    query = Appg.query.filter(Appg.is_current.is_(True))
    if category:
        query = query.filter(Appg.category == category)
    if topic:
        query = query.filter(Appg.topic_slugs.like(f"%,{topic},%"))
    if q:
        like = _like(q)
        query = query.filter(or_(Appg.title.ilike(like, escape="\\"),
                                 Appg.purpose.ilike(like, escape="\\"),
                                 Appg.secretariat.ilike(like, escape="\\")))
    total = query.count()
    total_pages = max(1, (total + _APPG_PER_PAGE - 1) // _APPG_PER_PAGE)
    if page > total_pages:
        abort(404)
    rows = (query.order_by(Appg.short_name)
            .offset((page - 1) * _APPG_PER_PAGE).limit(_APPG_PER_PAGE).all())

    chairs: dict[int, list[str]] = {}
    if rows:
        for o in (AppgOfficer.query
                  .filter(AppgOfficer.appg_id.in_([a.id for a in rows]), AppgOfficer.is_chair.is_(True))
                  .order_by(AppgOfficer.sort_order).all()):
            chairs.setdefault(o.appg_id, []).append(o.name)

    items = [{
        "id": a.id,
        "name": a.short_name,
        "path": appg_path(a),
        "category": a.category or "",
        "purpose_excerpt": excerpt(a.purpose, 220),
        "chairs": chairs.get(a.id, []),
        "topics": topics_from_slugs(a.topic_slugs),
    } for a in rows]

    def page_url(n: int) -> str:
        args = {}
        if q:
            args["q"] = q
        if topic:
            args["topic"] = topic
        if category:
            args["category"] = cat_key
        if n > 1:
            args["page"] = n
        return "/appgs" + ("?" + urlencode(args) if args else "")

    edition = db.session.query(db.func.max(Appg.register_edition)).scalar()
    filtered = bool(q or topic or category)
    meta_desc = ("Search every All-Party Parliamentary Group (APPG) on the official register: what each "
                 "group is for, its chair and officers, and who provides its secretariat.")
    return render_template(
        "committees/appg_list.html",
        items=items,
        total=total,
        q=q, topic=topic, topics=topic_options(),
        topic_label=_pa.by_slug(topic).label if topic else "",
        cat_key=cat_key if category else "", categories=APPG_CATEGORIES,
        page=page, total_pages=total_pages, per_page=_APPG_PER_PAGE,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < total_pages else "",
        edition=_human_date(edition),
        register_url=APPG_REGISTER_URL,
        filtered=filtered,
        meta_desc=meta_desc,
        robots_meta="noindex, follow" if (filtered or page > 1) else "index, follow",
    )


@committees_bp.route("/appgs/<int:appg_id>")
def appg_short(appg_id: int):
    if not committees_enabled():
        abort(404)
    a = db.session.get(Appg, appg_id)
    if a is None:
        abort(404)
    return redirect(appg_path(a), code=301)


@committees_bp.route("/appgs/<int:appg_id>/<string:slug>")
@limiter.limit("120 per minute")
def appg_detail(appg_id: int, slug: str):
    if not committees_enabled():
        abort(404)
    a = db.session.get(Appg, appg_id)
    if a is None:
        abort(404)
    if a.slug and slug != a.slug:
        return redirect(appg_path(a), code=301)

    officers = (AppgOfficer.query.filter(AppgOfficer.appg_id == a.id)
                .order_by(AppgOfficer.sort_order, AppgOfficer.id).all())
    urls = member_urls(o.member_id for o in officers if o.match_confidence in LINKED_CONFIDENCE)
    officer_rows = [{
        "name": o.name,
        "role": o.role,
        "party": o.party or "",
        "is_chair": o.is_chair,
        "member_id": o.member_id if o.match_confidence in LINKED_CONFIDENCE else None,
        "url": urls.get(o.member_id, "") if o.match_confidence in LINKED_CONFIDENCE else "",
    } for o in officers]
    chair_names = [o["name"] for o in officer_rows if o["is_chair"]]
    benefits = [b for b in (a.benefits or []) if isinstance(b, dict)]
    meta_desc = excerpt(
        f"{a.title}{', chaired by ' + ' and '.join(chair_names) if chair_names else ''}. "
        f"{a.purpose or ''}", 155)
    return render_template(
        "committees/appg_detail.html",
        a=a,
        officers=officer_rows,
        benefits=benefits,
        topics=topics_from_slugs(a.topic_slugs),
        edition=_human_date(a.register_edition),
        canonical_path=appg_path(a),
        register_url=APPG_REGISTER_URL,
        meta_desc=meta_desc,
        robots_meta="index, follow" if a.is_current else "noindex, follow",
    )


# ── Sitemap ───────────────────────────────────────────────────────────────────

def build_sitemap_xml() -> str:
    """The two index pages, every current committee and every current APPG."""
    urls: list[tuple[str, str]] = [(f"{BASE_URL}/committees", ""), (f"{BASE_URL}/appgs", "")]
    for cid, slug, updated in (db.session.query(Committee.id, Committee.slug, Committee.updated_at)
                               .filter(Committee.is_current.is_(True), Committee.show_on_website.is_(True))
                               .order_by(Committee.id).all()):
        path = f"/committees/{cid}/{slug}" if slug else f"/committees/{cid}"
        urls.append((f"{BASE_URL}{path}", updated.date().isoformat() if updated else ""))
    for aid, slug, updated in (db.session.query(Appg.id, Appg.slug, Appg.updated_at)
                               .filter(Appg.is_current.is_(True))
                               .order_by(Appg.id).all()):
        path = f"/appgs/{aid}/{slug}" if slug else f"/appgs/{aid}"
        urls.append((f"{BASE_URL}{path}", updated.date().isoformat() if updated else ""))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in urls:
        lines.append('  <url>')
        lines.append(f'    <loc>{loc}</loc>')
        if lastmod:
            lines.append(f'    <lastmod>{lastmod}</lastmod>')
        lines.append('  </url>')
    lines.append('</urlset>')
    return '\n'.join(lines)
