"""
consultations.py — UK Government consultations from GOV.UK.

/consultations                 list: open now (closing soonest first) by default;
                               ?status=open|closed|outcome|all, ?dept=<org slug>,
                               ?area=<policy area slug>, ?q=, ?page=
/consultations/<slug>          one consultation: summary, who is consulting,
                               closing date + days left, how to respond,
                               documents, outcome, related Westminster Brief items
/sitemap-consultations.xml     built here, routed in flask_app.py

Reads only from the consultation / consultation_topic tables, filled by
scripts/ingest_consultations.py from the GOV.UK Search and Content APIs.
No GOV.UK calls on page view. UK Government only (GOV.UK); devolved
governments are out of scope.

Content is Crown copyright under the Open Government Licence v3.0; pages
credit GOV.UK and link to the original.

Behind FEATURE_CONSULTATIONS (hidden / beta / live, see feature_flags.py) so
the code can ship before migrations/consultation_tables.py and the first
ingest have run.

Policy-area mapping is deterministic (GOV.UK taxonomy, then department, then
title/summary keywords): see assign_policy_areas().
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from flask import Blueprint, abort, redirect, render_template, request
from flask_login import current_user
from sqlalchemy import func, or_, text

from extensions import db, limiter
from feature_flags import feature_enabled
from hansard_archive import policy_areas as _pa

consultations_bp = Blueprint("consultations", __name__)

FLAG = "FEATURE_CONSULTATIONS"
BASE_URL = "https://westminsterbrief.co.uk"
GOVUK = "https://www.gov.uk"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_OUTCOME = "outcome"
DOC_TYPE_STATUS = {
    "open_consultation": STATUS_OPEN,
    "closed_consultation": STATUS_CLOSED,
    "consultation_outcome": STATUS_OUTCOME,
}
STATUS_FILTERS = {
    "open": "Open now",
    "closed": "Closed, awaiting outcome",
    "outcome": "Outcome published",
    "all": "All",
}

_PER_PAGE = 20
_MAX_PAGES = 50
_MAX_Q_LEN = 100
_EXCERPT_CHARS = 240
_RELATED_MAX = 5
_RELATED_WINDOW_DAYS = 365
_MAX_AREAS = 3


# ── Models ────────────────────────────────────────────────────────────────────
# Production tables come from migrations/consultation_tables.py (the web
# service runs with SKIP_MIGRATIONS=1). Declared here so SQLite create_all()
# works locally and in tests. Postgres also gets a generated search_tsv
# column + GIN index from the migration, deliberately not declared here.

class Consultation(db.Model):
    """
    One GOV.UK consultation. content_id is GOV.UK's stable id; base_path is
    its URL path on www.gov.uk. status follows GOV.UK's document type
    (open_consultation / closed_consultation / consultation_outcome).
    Dates are stored as naive UTC.
    """

    __tablename__ = "consultation"
    __table_args__ = (
        db.Index("idx_consultation_status", "status"),
        db.Index("idx_consultation_closing", "closing_date"),
        db.Index("idx_consultation_lead_org", "lead_org_slug"),
    )

    id                 = db.Column(db.Integer, primary_key=True)
    content_id         = db.Column(db.String(64), nullable=False, unique=True)
    base_path          = db.Column(db.Text, nullable=False, unique=True)
    slug               = db.Column(db.String(255), nullable=False, unique=True)
    title              = db.Column(db.Text, nullable=False)
    summary            = db.Column(db.Text, nullable=True)
    body_text          = db.Column(db.Text, nullable=True)
    status             = db.Column(db.String(20), nullable=False)
    opening_date       = db.Column(db.DateTime, nullable=True)
    closing_date       = db.Column(db.DateTime, nullable=True)
    lead_org_slug      = db.Column(db.String(200), nullable=True)
    lead_org_title     = db.Column(db.Text, nullable=True)
    organisations      = db.Column(db.JSON, nullable=True)   # [{title, acronym, slug, type, parents}]
    policy_areas       = db.Column(db.JSON, nullable=True)   # [slug] ranked; mirrors consultation_topic
    taxons             = db.Column(db.JSON, nullable=True)   # [{title, base_path, ancestors}]
    response_url       = db.Column(db.Text, nullable=True)   # online survey / form
    response_email     = db.Column(db.Text, nullable=True)
    response_postal    = db.Column(db.Text, nullable=True)
    response_form_url  = db.Column(db.Text, nullable=True)   # downloadable response form
    documents          = db.Column(db.JSON, nullable=True)   # [{title, url, kind, content_type, file_size, pages}]
    outcome_url        = db.Column(db.Text, nullable=True)
    outcome_summary    = db.Column(db.Text, nullable=True)
    outcome_documents  = db.Column(db.JSON, nullable=True)
    applies_to         = db.Column(db.JSON, nullable=True)   # ["England", "Wales", ...]
    is_withdrawn       = db.Column(db.Boolean, nullable=False, default=False)
    govuk_updated_at   = db.Column(db.DateTime, nullable=True)  # GOV.UK public_updated_at
    first_published_at = db.Column(db.DateTime, nullable=True)
    raw_data           = db.Column(db.JSON, nullable=True)
    ingested_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                   onupdate=datetime.utcnow)

    topics = db.relationship("ConsultationTopic", backref="consultation", lazy="dynamic",
                             cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Consultation {self.slug} [{self.status}]>"


class ConsultationTopic(db.Model):
    """Consultation -> Westminster Brief policy area (slug from policy_areas.py)."""

    __tablename__ = "consultation_topic"
    __table_args__ = (
        db.UniqueConstraint("consultation_id", "policy_area", name="uq_consultation_topic"),
        db.Index("idx_consultation_topic_area", "policy_area"),
    )

    id              = db.Column(db.Integer, primary_key=True)
    consultation_id = db.Column(db.Integer, db.ForeignKey("consultation.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    policy_area     = db.Column(db.String(100), nullable=False)
    source          = db.Column(db.String(20), nullable=False)   # taxon | department | keyword
    score           = db.Column(db.Integer, nullable=False, default=0)


# ── Policy-area mapping (deterministic) ───────────────────────────────────────

# GOV.UK level-two taxons that point somewhere more specific than their root.
_TAXON_AREAS: dict[str, tuple[str, ...]] = {
    "/environment/climate-change-energy": ("energy-and-utilities", "environment-and-climate-change"),
    "/environment/climate-change-energy-energy-infrastructure": ("energy-and-utilities",),
    "/environment/food-and-farming": ("agriculture-environment-and-rural-affairs",),
    "/environment/rural-and-countryside": ("agriculture-environment-and-rural-affairs",),
    "/environment/fishing-fisheries-and-vessels": ("agriculture-environment-and-rural-affairs",),
    "/environment/wildlife-animals-biodiversity-and-ecosystems": ("agriculture-environment-and-rural-affairs",
                                                                  "environment-and-climate-change"),
    "/business-and-industry/financial-services": ("finance-and-taxation",),
    "/business-and-industry/science-and-innovation": ("science-technology-and-innovation",),
    "/business-and-industry/uk-economy": ("economy",),
    "/business-and-industry/media-and-communications": ("culture-media-and-sport",),
    "/work/pensions-and-ageing-society": ("work-and-pensions",),
    "/society-and-culture/online-safety": ("science-technology-and-innovation",),
    "/society-and-culture/digital-inclusion-and-accessibility-in-society": ("science-technology-and-innovation",),
    "/society-and-culture/young-people": ("education",),
    "/government/democracy": ("constitutional-affairs",),
    "/government/national-security": ("defence-and-national-security",),
    "/government/cyber-security": ("defence-and-national-security",),
    "/government/europe": ("foreign-affairs",),
    "/government/brexit": ("foreign-affairs",),
    "/regional-and-local-government/devolution": ("constitutional-affairs",),
    "/housing-local-and-community/household-energy": ("energy-and-utilities",),
    "/international/foreign-affairs": ("foreign-affairs",),
    # No good policy-area fit: suppress the root ("Society and culture") match.
    "/society-and-culture/equality-rights-and-citizenship": (),
    "/international/international-aid-and-development": ("international-affairs",),
}

# GOV.UK level-one taxons (www.gov.uk/api/content/ -> links.level_one_taxons).
_ROOT_AREAS: dict[str, tuple[str, ...]] = {
    "/health-and-social-care": ("health-and-social-care",),
    "/money": ("finance-and-taxation",),
    "/crime-justice-and-law": ("crime-justice-and-law",),
    "/defence-and-armed-forces": ("defence-and-national-security",),
    "/work": ("employment-and-labour-market",),
    "/employment": ("employment-and-labour-market",),
    "/welfare": ("welfare-and-social-security",),
    "/housing-local-and-community": ("housing-and-planning",),
    "/society-and-culture": ("culture-media-and-sport",),
    "/international": ("international-affairs",),
    "/regional-and-local-government": ("government-and-public-administration",),
    "/government": ("government-and-public-administration",),
    "/environment": ("environment-and-climate-change",),
    "/business-and-industry": ("business-and-industry",),
    "/transport": ("transport",),
    "/education": ("education",),
    "/childcare-parenting": ("children-and-families",),
    "/entering-staying-uk": ("immigration-and-asylum",),
}

# Lead organisation (GOV.UK slug) -> policy areas. Parent departments are
# checked too, so an agency inherits its department's areas.
_ORG_AREAS: dict[str, tuple[str, ...]] = {
    # GOV.UK slugs checked against live search listings on 2026-10-20 (some
    # departments have been renamed). Agencies not listed inherit via parents.
    "department-for-education": ("education",),
    "ofsted": ("education",),
    "ofqual": ("education",),
    "department-of-health-and-social-care": ("health-and-social-care",),
    "food-standards-agency": ("health-and-social-care",),
    "ministry-of-housing-communities-local-government": ("housing-and-planning",),
    "building-safety-regulator": ("housing-and-planning",),
    "office-for-equality-and-opportunity": ("government-and-public-administration",),
    "department-for-transport": ("transport",),
    "ministry-of-justice": ("crime-justice-and-law",),
    "home-office": ("crime-justice-and-law",),
    "attorney-generals-office": ("crime-justice-and-law",),
    "department-for-work-pensions": ("welfare-and-social-security", "work-and-pensions"),
    "department-for-environment-food-rural-affairs": ("agriculture-environment-and-rural-affairs",
                                                      "environment-and-climate-change"),
    "environment-agency": ("environment-and-climate-change",),
    "natural-england": ("environment-and-climate-change",),
    "department-for-energy-security-and-net-zero": ("energy-and-utilities",),
    "ofgem": ("energy-and-utilities",),
    "ofwat": ("energy-and-utilities",),
    "ministry-of-defence": ("defence-and-national-security",),
    "foreign-commonwealth-development-office": ("foreign-affairs", "international-affairs"),
    "hm-treasury": ("finance-and-taxation", "economy"),
    "hm-revenue-customs": ("finance-and-taxation",),
    "financial-conduct-authority": ("finance-and-taxation",),
    "department-for-business-innovation-science-and-trade": ("business-and-industry",),
    "department-for-business-and-trade": ("business-and-industry",),
    "low-pay-commission": ("employment-and-labour-market",),
    "department-for-science-innovation-and-technology": ("science-technology-and-innovation",),
    "intellectual-property-office": ("science-technology-and-innovation",),
    "ofcom": ("science-technology-and-innovation",),
    "department-for-culture-media-and-sport": ("culture-media-and-sport",),
    "department-for-digital-culture-media-and-sport": ("culture-media-and-sport",),
    "cabinet-office": ("government-and-public-administration",),
    "uk-statistics-authority": ("government-and-public-administration",),
}

# Whole-word keywords (title counts double). Short and specific: a missed
# link is better than a misleading one.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "education": ("school", "schools", "pupils", "teachers", "university", "universities",
                  "students", "apprenticeships", "send"),
    # Owner decision 2026-10-20: childcare and family items have their own page.
    "children-and-families": ("childcare", "early years", "nursery", "nurseries", "fostering",
                              "adoption", "kinship care", "children's social care", "child poverty"),
    "health-and-social-care": ("nhs", "hospital", "hospitals", "patients", "social care",
                               "mental health", "medicines", "public health", "tobacco", "vaping"),
    "housing-and-planning": ("housing", "homes", "renters", "tenants", "landlords",
                             "homelessness", "planning", "building safety", "leasehold"),
    "transport": ("rail", "railway", "bus", "buses", "roads", "airport", "aviation",
                  "vehicles", "driving", "parking", "maritime"),
    "crime-justice-and-law": ("police", "policing", "crime", "prison", "prisons", "courts",
                              "sentencing", "victims", "legal aid", "tribunal"),
    "welfare-and-social-security": ("universal credit", "benefits", "welfare", "poverty",
                                    "disability", "pip"),
    "immigration-and-asylum": ("asylum", "immigration", "refugees", "visa", "visas", "migrants",
                               "border"),
    "environment-and-climate-change": ("climate", "net zero", "pollution", "sewage", "nature",
                                       "biodiversity", "waste", "recycling", "flood", "emissions"),
    "energy-and-utilities": ("energy", "electricity", "gas", "heat pump", "heat pumps", "clean heat", "capacity market",
                             "heating", "fuel poverty", "water", "nuclear", "renewables"),
    "agriculture-environment-and-rural-affairs": ("farmers", "farming", "agriculture",
                                                  "rural", "animal welfare", "fisheries", "food"),
    "work-and-pensions": ("pension", "pensions", "state pension", "auto-enrolment"),
    "employment-and-labour-market": ("workers", "employment", "employers", "wages",
                                     "minimum wage", "equal pay", "trade union", "trade unions"),
    "finance-and-taxation": ("tax", "taxes", "vat", "duty", "hmrc", "financial services",
                             "banks", "insurance"),
    "business-and-industry": ("business", "businesses", "competition", "consumer",
                              "consumers", "regulation", "industry"),
    "culture-media-and-sport": ("sport", "football", "arts", "museum", "museums", "bbc",
                                "broadcasting", "gambling", "heritage", "charities"),
    "science-technology-and-innovation": ("science", "research", "technology", "data",
                                          "artificial intelligence", "online safety",
                                          "cyber", "telecoms", "broadband"),
    "defence-and-national-security": ("armed forces", "veterans", "defence", "military",
                                      "national security"),
    "government-and-public-administration": ("civil service", "public bodies",
                                             "public procurement", "local government",
                                             "councils"),
    "constitutional-affairs": ("elections", "electoral", "devolution", "constitution"),
}
_KEYWORD_PATTERNS = {
    slug: re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    for slug, words in _KEYWORDS.items()
}

_WEIGHTS = {"taxon": 3, "department": 3}
_MIN_SCORE = 3


def assign_policy_areas(title: str, summary: str, taxons: list | None,
                        organisations: list | None, limit: int = _MAX_AREAS) -> list[dict]:
    """
    Ranked [{slug, source, score}] for one consultation.

    Scores: GOV.UK taxon 3 (level-two override, else its root), lead
    department (or its parent) 3, keywords 2 per title hit + 1 per summary
    hit (capped at 4). Areas below 3 are dropped, so one title keyword never
    links on its own. source = the largest contributor.
    """
    scores: dict[str, dict[str, int]] = {}

    def add(slug: str, source: str, points: int) -> None:
        if _pa.by_slug(slug) is None or points <= 0:
            return
        per = scores.setdefault(slug, {})
        per[source] = max(per.get(source, 0), points)

    for t in taxons or []:
        paths = [t.get("base_path") or ""] + list(t.get("ancestors") or [])
        hit = next((_TAXON_AREAS[p] for p in paths if p in _TAXON_AREAS), None)
        if hit is None:
            root = next((p for p in reversed(paths) if p in _ROOT_AREAS), None)
            if root is None:
                first = "/" + (paths[0].strip("/").split("/")[0] if paths[0] else "")
                root = first if first in _ROOT_AREAS else None
            hit = _ROOT_AREAS.get(root, ()) if root else ()
        for slug in hit:
            add(slug, "taxon", _WEIGHTS["taxon"])

    lead = (organisations or [{}])[0] if organisations else {}
    for org_slug in [lead.get("slug")] + list(lead.get("parents") or []):
        if org_slug in _ORG_AREAS:
            for slug in _ORG_AREAS[org_slug]:
                add(slug, "department", _WEIGHTS["department"])
            break

    for slug, pattern in _KEYWORD_PATTERNS.items():
        points = 2 * len(pattern.findall(title or "")) + len(pattern.findall(summary or ""))
        add(slug, "keyword", min(points, 4))

    ranked = []
    for slug, per in scores.items():
        total = sum(per.values())
        if total >= _MIN_SCORE:
            source = max(per.items(), key=lambda kv: (kv[1], kv[0] == "taxon"))[0]
            ranked.append({"slug": slug, "source": source, "score": total})
    order = {pa.slug: i for i, pa in enumerate(_pa.all_areas())}
    ranked.sort(key=lambda r: (-r["score"], order.get(r["slug"], 99)))
    return ranked[:limit]


# ── Dates ─────────────────────────────────────────────────────────────────────

def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def uk_local(dt_utc: datetime | None) -> datetime | None:
    """Naive UTC -> naive UK wall-clock time (BST: last Sunday of March to last Sunday of October, 01:00 UTC)."""
    if dt_utc is None:
        return None
    start = datetime.combine(_last_sunday(dt_utc.year, 3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(dt_utc.year, 10), datetime.min.time()) + timedelta(hours=1)
    return dt_utc + timedelta(hours=1) if start <= dt_utc < end else dt_utc


def _human_date(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _human_time(dt: datetime) -> str:
    if dt.hour == 23 and dt.minute == 59:
        return "11:59pm"
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def _utcnow() -> datetime:
    return datetime.utcnow()


def deadline_info(c: Consultation, now: datetime | None = None) -> dict:
    """Closing-date facts for badges: days_left, label, urgent, is_open, human strings (UK time)."""
    now = now or _utcnow()
    closing_local = uk_local(c.closing_date)
    opening_local = uk_local(c.opening_date)
    is_open = (c.status == STATUS_OPEN and not c.is_withdrawn
               and c.closing_date is not None and c.closing_date >= now)
    not_yet = is_open and c.opening_date is not None and c.opening_date > now
    days_left = None
    if closing_local is not None:
        days_left = (closing_local.date() - uk_local(now).date()).days
    if c.is_withdrawn:
        label = "Withdrawn"
    elif not is_open:
        label = "Outcome published" if c.status == STATUS_OUTCOME else "Closed"
    elif not_yet:
        label = f"Opens {_human_date(opening_local)}"
    elif days_left == 0:
        label = "Closes today"
    elif days_left == 1:
        label = "Closes tomorrow"
    else:
        label = f"{days_left} days left"
    return {
        "is_open": is_open,
        "not_yet_open": not_yet,
        "days_left": days_left if is_open else None,
        "label": label,
        "urgent": bool(is_open and days_left is not None and days_left <= 7),
        "closing_human": (f"{_human_time(closing_local)} on {_human_date(closing_local)}"
                          if closing_local else ""),
        "closing_date_human": _human_date(closing_local),
        "opening_human": _human_date(opening_local),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def consultations_enabled() -> bool:
    return feature_enabled(FLAG, current_user)


def consultation_path(c) -> str:
    return f"/consultations/{c.slug}"


def govuk_url(c) -> str:
    return GOVUK + c.base_path


def _excerpt(text_: str, limit: int = _EXCERPT_CHARS) -> str:
    text_ = " ".join((text_ or "").split())
    if len(text_) <= limit:
        return text_
    return text_[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _is_postgres() -> bool:
    return db.engine.dialect.name == "postgresql"


def _org_label(org: dict) -> str:
    return org.get("title") or org.get("acronym") or ""


def _area_links(slugs: list | None) -> list[dict]:
    out = []
    for slug in slugs or []:
        area = _pa.by_slug(slug)
        if area:
            out.append({"slug": slug, "label": area.label})
    return out


def _list_item(c: Consultation, now: datetime) -> dict:
    return {
        "title": c.title,
        "path": consultation_path(c),
        "org": c.lead_org_title or "",
        "other_orgs": max(0, len(c.organisations or []) - 1),
        "excerpt": _excerpt(c.summary or ""),
        "deadline": deadline_info(c, now),
        "areas": _area_links(c.policy_areas),
        "status": c.status,
    }


def _status_filter(query, status: str, now: datetime):
    live = query.filter(Consultation.is_withdrawn.is_(False))
    if status == "open":
        return live.filter(Consultation.status == STATUS_OPEN, Consultation.closing_date >= now)
    if status == "closed":
        # GOV.UK flips open -> closed shortly after the deadline; until our next
        # ingest an "open" row past its closing date is shown as closed.
        return live.filter(or_(Consultation.status == STATUS_CLOSED,
                               (Consultation.status == STATUS_OPEN) & (Consultation.closing_date < now)))
    if status == "outcome":
        return live.filter(Consultation.status == STATUS_OUTCOME)
    return live


def _order(query, status: str):
    if status == "open":
        return query.order_by(Consultation.closing_date.asc(), Consultation.id.asc())
    if status == "outcome":
        return query.order_by(Consultation.govuk_updated_at.desc(), Consultation.id.desc())
    return query.order_by(Consultation.closing_date.desc(), Consultation.id.desc())


def _search_filter(query, q: str):
    if _is_postgres():
        # search_tsv: generated column + GIN index from migrations/consultation_tables.py.
        # Value bound on the clause (not Query.params) so it survives use in subqueries.
        return query.filter(
            text("consultation.search_tsv @@ websearch_to_tsquery('english', :cons_q)")
            .bindparams(cons_q=q)
        )
    like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    return query.filter(or_(Consultation.title.ilike(like, escape="\\"),
                            Consultation.summary.ilike(like, escape="\\")))


def _area_filter(query, area: str):
    return query.filter(Consultation.id.in_(
        db.session.query(ConsultationTopic.consultation_id)
        .filter(ConsultationTopic.policy_area == area)
    ))


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        return 1


# ── Digest / alerts helper ────────────────────────────────────────────────────

def open_consultations(policy_areas: list[str] | None = None, closing_within_days: int | None = None,
                       limit: int = 20, now: datetime | None = None) -> list[dict]:
    """
    Open consultations, closing soonest first, optionally limited to some
    policy-area slugs and/or a closing window. For the weekly digest
    ("Opportunities to influence") and alerts. Needs an app context.
    Each item: title, url (Westminster Brief), govuk_url, org, closing_human,
    days_left, label, policy_areas [{slug, label}].
    """
    now = now or _utcnow()
    query = _status_filter(Consultation.query, "open", now)
    if policy_areas:
        query = query.filter(Consultation.id.in_(
            db.session.query(ConsultationTopic.consultation_id)
            .filter(ConsultationTopic.policy_area.in_(list(policy_areas)))
        ))
    if closing_within_days is not None:
        query = query.filter(Consultation.closing_date <= now + timedelta(days=closing_within_days + 1))
    out = []
    for c in _order(query, "open").limit(limit).all():
        info = deadline_info(c, now)
        if closing_within_days is not None and (info["days_left"] or 0) > closing_within_days:
            continue
        out.append({
            "title": c.title,
            "url": BASE_URL + consultation_path(c),
            "govuk_url": govuk_url(c),
            "org": c.lead_org_title or "",
            "closing_human": info["closing_human"],
            "days_left": info["days_left"],
            "label": info["label"],
            "policy_areas": _area_links(c.policy_areas),
        })
    return out


def open_consultations_by_area(policy_areas: list[str], limit_per_area: int = 5,
                               now: datetime | None = None) -> dict[str, list[dict]]:
    """{policy area slug: open_consultations(...)} for each requested slug (empty lists kept)."""
    return {slug: open_consultations([slug], limit=limit_per_area, now=now) for slug in policy_areas}


# ── Related Westminster Brief items ───────────────────────────────────────────

def related_items(slugs: list[str], now: datetime | None = None) -> dict:
    """
    Recent debates, written questions and current bills tagged with the same
    policy areas (ha_session_theme / ha_pq_theme / ha_bill_theme). Matched on
    policy area, not on this consultation specifically: the page says so.
    Any failure (e.g. a table missing locally) returns empty lists.
    """
    from hansard_archive.models import (
        HaBill, HaBillTheme, HaPQ, HaPQTheme, HansardSession, HansardSessionTheme,
        THEME_TYPE_POLICY_AREA, pq_url_path,
    )

    names = sorted({n for s in slugs for n in _pa.hansard_names_for_slug(s)})
    out = {"debates": [], "pqs": [], "bills": []}
    if not names:
        return out
    since = (now or _utcnow()).date() - timedelta(days=_RELATED_WINDOW_DAYS)
    try:
        sessions = (HansardSession.query
                    .filter(HansardSession.is_container.is_(False),
                            HansardSession.slug.isnot(None),
                            HansardSession.date >= since,
                            HansardSession.id.in_(
                                db.session.query(HansardSessionTheme.session_id).filter(
                                    HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
                                    HansardSessionTheme.theme.in_(names))))
                    .order_by(HansardSession.date.desc(), HansardSession.id.desc())
                    .limit(_RELATED_MAX).all())
        out["debates"] = [{
            "title": s.title,
            "url": f"/archive/debate/{s.date.day}-{s.date.strftime('%B').lower()}-{s.date.year}/{s.slug}",
            "meta": f"{s.house} · {_human_date(s.date)}",
        } for s in sessions]

        pqs = (db.session.query(HaPQ.uin, HaPQ.heading, func.substr(HaPQ.question_text, 1, 160),
                                HaPQ.tabled_date, HaPQ.answering_body)
               .filter(HaPQ.tabled_date >= since,
                       HaPQ.is_withdrawn.isnot(True),
                       HaPQ.id.in_(db.session.query(HaPQTheme.pq_id).filter(
                           HaPQTheme.theme_type == THEME_TYPE_POLICY_AREA,
                           HaPQTheme.theme.in_(names))))
               .order_by(HaPQ.tabled_date.desc(), HaPQ.id.desc())
               .limit(_RELATED_MAX).all())
        out["pqs"] = [{
            "title": heading or _excerpt(start or "", 100) or f"Written question {uin}",
            "url": pq_url_path(tabled, uin),
            "meta": " · ".join(x for x in (body or "", _human_date(tabled)) if x),
        } for uin, heading, start, tabled, body in pqs]

        bills = (HaBill.query
                 .filter(HaBill.slug.isnot(None),
                         HaBill.is_act.is_(False), HaBill.is_defeated.is_(False),
                         HaBill.bill_withdrawn_date.is_(None),
                         HaBill.id.in_(db.session.query(HaBillTheme.bill_id).filter(
                             HaBillTheme.theme.in_(names))))
                 .order_by(HaBill.last_updated_date.desc().nullslast(), HaBill.id.desc())
                 .limit(_RELATED_MAX).all())
        out["bills"] = [{
            "title": b.short_title or b.title,
            "url": f"/bill/{b.slug}",
            "meta": " · ".join(x for x in (b.current_stage or "", b.session or "") if x),
        } for b in bills]
    except Exception as exc:  # related links are a nice-to-have, never break the page
        db.session.rollback()
        print(f"[consultations] related items failed: {type(exc).__name__}: {exc}", flush=True)
        return {"debates": [], "pqs": [], "bills": []}
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@consultations_bp.route("/consultations")
@limiter.limit("120 per minute")
def consultation_list():
    if not consultations_enabled():
        abort(404)
    now = _utcnow()
    q = " ".join((request.args.get("q") or "").split())[:_MAX_Q_LEN]
    status = request.args.get("status", "open")
    if status not in STATUS_FILTERS:
        status = "open"
    dept = (request.args.get("dept") or "").strip()[:200]
    area = (request.args.get("area") or "").strip()
    if area and _pa.by_slug(area) is None:
        area = ""
    page = _page_arg()
    if page > _MAX_PAGES:
        abort(404)

    base = _status_filter(Consultation.query, status, now)
    if q:
        base = _search_filter(base, q)

    # Filter options reflect the chosen status + search, not the other filters.
    dept_rows = (base.with_entities(Consultation.lead_org_slug, Consultation.lead_org_title,
                                    func.count(Consultation.id))
                 .filter(Consultation.lead_org_slug.isnot(None))
                 .group_by(Consultation.lead_org_slug, Consultation.lead_org_title)
                 .all())
    departments = sorted(({"slug": s, "title": t or s, "count": n} for s, t, n in dept_rows),
                         key=lambda d: d["title"].lower())
    area_counts = dict(
        db.session.query(ConsultationTopic.policy_area, func.count(ConsultationTopic.id))
        .filter(ConsultationTopic.consultation_id.in_(base.with_entities(Consultation.id)))
        .group_by(ConsultationTopic.policy_area).all()
    )
    areas = [{"slug": pa.slug, "label": pa.label, "count": area_counts.get(pa.slug, 0)}
             for pa in _pa.all_areas() if area_counts.get(pa.slug) or pa.slug == area]

    query = base
    if dept:
        query = query.filter(Consultation.lead_org_slug == dept)
    if area:
        query = _area_filter(query, area)

    total = query.count()
    total_pages = min(_MAX_PAGES, max(1, (total + _PER_PAGE - 1) // _PER_PAGE))
    if page > total_pages:
        abort(404)
    rows = _order(query, status).offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()

    def page_url(n: int) -> str:
        args = {}
        if q:
            args["q"] = q
        if status != "open":
            args["status"] = status
        if dept:
            args["dept"] = dept
        if area:
            args["area"] = area
        if n > 1:
            args["page"] = n
        return "/consultations" + ("?" + urlencode(args) if args else "")

    dept_title = next((d["title"] for d in departments if d["slug"] == dept), "")
    filtered = bool(q or dept or area or status != "open")
    meta_desc = (
        "UK Government consultations open now, closing soonest first: who is consulting, "
        "the closing date, how to respond and related parliamentary activity."
    )
    return render_template(
        "consultations/consultation_list.html",
        items=[_list_item(c, now) for c in rows],
        total=total,
        q=q,
        status=status,
        statuses=STATUS_FILTERS,
        dept=dept,
        dept_title=dept_title,
        departments=departments,
        area=area,
        area_label=_pa.display_name_from_slug(area) if area else "",
        areas=areas,
        page=page,
        total_pages=total_pages,
        per_page=_PER_PAGE,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < total_pages else "",
        meta_desc=meta_desc,
        filtered=filtered,
        robots_meta="noindex, follow" if filtered or page > 1 else "index, follow",
    )


@consultations_bp.route("/consultations/<string:slug>")
@limiter.limit("120 per minute")
def consultation_detail(slug: str):
    if not consultations_enabled():
        abort(404)
    c = Consultation.query.filter(Consultation.slug == slug.lower()).first()
    if c is None:
        abort(404)
    if slug != c.slug:
        return redirect(consultation_path(c), code=301)

    now = _utcnow()
    info = deadline_info(c, now)
    orgs = [o for o in (c.organisations or []) if isinstance(o, dict)]
    areas = _area_links(c.policy_areas)
    related = related_items(c.policy_areas or [], now)
    who = c.lead_org_title or "the UK Government"
    if info["is_open"]:
        when = f"Closes {info['closing_human']}."
    elif info["closing_date_human"]:
        when = f"Closed {info['closing_date_human']}."
    else:
        when = ""
    meta_desc = _excerpt(f"Consultation by {who}. {when} {c.summary or ''}", 155)
    paragraphs = [p for p in (c.body_text or "").split("\n\n") if p.strip()]
    return render_template(
        "consultations/consultation_detail.html",
        c=c,
        info=info,
        orgs=orgs,
        org_names=[_org_label(o) for o in orgs if _org_label(o)],
        areas=areas,
        related=related,
        has_related=any(related.values()),
        paragraphs=paragraphs,
        documents=[d for d in (c.documents or []) if isinstance(d, dict)],
        outcome_documents=[d for d in (c.outcome_documents or []) if isinstance(d, dict)],
        govuk_link=govuk_url(c),
        meta_desc=meta_desc,
        canonical_path=consultation_path(c),
        checked_date=_human_date(c.last_refreshed),
        robots_meta="noindex, follow" if c.is_withdrawn else "index, follow",
    )


# ── Sitemap ───────────────────────────────────────────────────────────────────

def build_sitemap_xml() -> str:
    """Every consultation we hold that is not withdrawn; lastmod = last change in our copy."""
    rows = (db.session.query(Consultation.slug, Consultation.updated_at)
            .filter(Consultation.is_withdrawn.is_(False))
            .order_by(Consultation.closing_date.desc(), Consultation.id.desc()).all())
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for slug, updated in rows:
        lines.append('  <url>')
        lines.append(f'    <loc>{BASE_URL}/consultations/{slug}</loc>')
        if updated:
            lines.append(f'    <lastmod>{updated.date().isoformat()}</lastmod>')
        lines.append('  </url>')
    lines.append('</urlset>')
    return '\n'.join(lines)
