"""
Hansard Archive â€” public-facing routes (Phase 2A Week 3).

Blueprint: archive_bp, url_prefix=/archive

Routes:
  GET /archive                               browse/home â€” recent sessions + filters
  GET /archive/search                        FTS text search (noindex)
  GET /archive/debate/<date>/<slug>          session detail + full transcript
  GET /archive/debate/<date>/<slug>/word     Word export of session transcript
  GET /archive/date/<date_str>               all sessions on a date
  GET /archive/mp/<member_id>               sessions a member contributed to
  GET /archive/department/<slug>             sessions by answering department
  GET /archive/policy/<slug>                 sessions by GOV.UK policy area tag
  GET /archive/theme/<slug>                  sessions by specific theme tag
  GET /archive/pq/<uin>                      Written Question detail page

URL conventions (locked â€” do not change after indexing):
  date format: 22-july-2025  (day no leading zero, lowercase full month, 4-digit year)
  slug format: {title-slug}-{4-char-hex}
"""

import os
import re
import time
from collections import defaultdict
from datetime import date as date_type, timedelta
from io import BytesIO
from urllib.parse import urlencode

from docx import Document
from docx.shared import Pt, RGBColor
from flask import Blueprint, abort, redirect, render_template, make_response, request
from markupsafe import Markup
from sqlalchemy import func, text as sqla_text

from cache_models import CachedMember
from extensions import db
from hansard_archive.models import (
    HansardContribution,
    HansardSession,
    HansardSessionTheme,
    HaPQ,
    HaPQTheme,
    ManifestoChunk,
    ManifestoChunkTag,
    MpAnalytics,
    UpcomingRelease,
    THEME_TYPE_POLICY_AREA,
    THEME_TYPE_SPECIFIC,
)
from hansard_archive.slugs import slugify_theme

archive_bp  = Blueprint("archive",  __name__, url_prefix="/archive")
brief_bp    = Blueprint("brief",    __name__, url_prefix="/brief")
stats_bp    = Blueprint("stats",    __name__, url_prefix="/stats")
hansard_bp2 = Blueprint("hansard2", __name__, url_prefix="/hansard")

# ---------------------------------------------------------------------------
# Party colours + attribution parsing
# ---------------------------------------------------------------------------

_PARTY_COLOURS: dict[str, str] = {
    "Lab":        "#E4003B",
    "Lab/Co-op":  "#E4003B",
    "Lab/ Co-op": "#E4003B",
    "Lab Co-op":  "#E4003B",
    "Con":        "#0087DC",
    "LD":         "#FAA61A",
    "SNP":        "#c9a800",
    "PC":         "#005B54",
    "Green":      "#02A95B",
    "Reform":     "#12B6CF",
    "DUP":        "#CF1F25",
    "SDLP":       "#2AA82C",
    "UUP":        "#48A5EE",
    "Alliance":   "#c9960a",
    "TUV":        "#0C3A6A",
    "CB":         "#7a8a9a",
    "Ind":        "#7a8a9a",
    "Non-Afl":    "#7a8a9a",
}
_DEFAULT_PARTY_COLOUR = "#1a4a6e"

_NOT_PARTY = frozenset({
    "Maiden Speech", "Valedictory Speech", "Urgent Question", "Maiden",
    "Your Party", "Restore Britain",
})
_STRIP_PREFIXES = frozenset({"Mr", "Mrs", "Ms", "Miss", "Dr"})


def _parse_attribution(raw: str | None) -> dict:
    """
    Parse raw member_name into display-ready fields.

    Handles:
      "Name (Constituency) (Party)"  â€” standard Commons
      "Name (Party)"                 â€” standard Lords
      "The [Role] (Name)"            â€” ministerial proxy format
      "Name"                         â€” short reference form
    """
    if not raw:
        return {"name": "Speaker", "role": "", "party": None,
                "party_colour": _DEFAULT_PARTY_COLOUR}

    raw = raw.strip()

    # Ministerial proxy: "The Secretary of State for X (Name)"
    m = re.match(r'^(The\s+[^(]+?)\s*\(([^)]+)\)\s*$', raw)
    if m:
        role = m.group(1).strip()
        name = m.group(2).strip()
        return {"name": name, "role": role, "party": None,
                "party_colour": _DEFAULT_PARTY_COLOUR}

    # Standard: extract parenthetical groups
    groups = re.findall(r'\(([^)]+)\)', raw)
    base = re.sub(r'\s*\([^)]+\)', '', raw).strip()

    # Strip leading salutation (Mr/Mrs etc â€” not Lord/Baroness which are titles)
    parts = base.split(None, 1)
    if parts and parts[0] in _STRIP_PREFIXES:
        base = parts[1] if len(parts) > 1 else base

    party = None
    if len(groups) >= 2:
        candidate = groups[-1].strip()
        if candidate not in _NOT_PARTY:
            party = candidate

    colour = _PARTY_COLOURS.get(party, _DEFAULT_PARTY_COLOUR) if party else _DEFAULT_PARTY_COLOUR
    return {"name": base or raw, "role": "", "party": party, "party_colour": colour}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_DEBATE_TYPE_LABELS = {
    "oral_questions":       "Oral Questions",
    "pmqs":                 "Prime Minister's Questions",
    "westminster_hall":     "Westminster Hall",
    "debate":               "Debate",
    "ministerial_statement":"Ministerial Statement",
    "statutory_instrument": "Statutory Instrument",
    "committee_stage":      "Committee Stage",
    "petition":             "Petition",
    "other":                "Proceedings",
}


def _human_date(d) -> str:
    """date â†’ '27 April 2026'"""
    return f"{d.day} {_MONTH_NAMES[d.month]} {d.year}"


def _url_date(d) -> str:
    """date â†’ '27-april-2026'"""
    return f"{d.day}-{_MONTH_NAMES[d.month].lower()}-{d.year}"


def _parse_url_date(date_str: str) -> date_type | None:
    """Parse '27-april-2026' â†’ date object, or None if invalid."""
    parts = date_str.split('-')
    if len(parts) != 3:
        return None
    day_str, month_str, year_str = parts
    try:
        day = int(day_str)
        year = int(year_str)
        month_idx = next(
            (i for i, m in enumerate(_MONTH_NAMES) if m.lower() == month_str.lower()),
            0,
        )
        if month_idx == 0:
            return None
        return date_type(year, month_idx, day)
    except (ValueError, IndexError):
        return None




def _is_postgres() -> bool:
    """True when connected to Postgres (FTS available), False for SQLite."""
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


def _build_session_items(sessions: list, contrib_counts: dict,
                         policy_areas: dict, specific_topics: dict) -> list:
    """Build template-ready item dicts from a list of HansardSession objects."""
    return [
        {
            "session":           s,
            "human_date":        _human_date(s.date),
            "url_date":          _url_date(s.date),
            "debate_type_label": _DEBATE_TYPE_LABELS.get(s.debate_type, "Proceedings"),
            "contrib_count":     contrib_counts.get(s.id, 0),
            "policy_areas":      sorted(policy_areas.get(s.id, [])),
            "specific_topics":   sorted(specific_topics.get(s.id, [])),
            "department":        s.department or "",
        }
        for s in sessions
    ]


def _batch_load_tags(session_ids: list) -> tuple[dict, dict]:
    """Batch-load policy area and specific topic tags for a list of session IDs."""
    policy: dict[int, list[str]] = defaultdict(list)
    specific: dict[int, list[str]] = defaultdict(list)
    if session_ids:
        for t in (
            db.session.query(HansardSessionTheme)
            .filter(HansardSessionTheme.session_id.in_(session_ids))
            .all()
        ):
            if t.theme_type == THEME_TYPE_POLICY_AREA:
                policy[t.session_id].append(t.theme)
            elif t.theme_type == THEME_TYPE_SPECIFIC:
                specific[t.session_id].append(t.theme)
    return policy, specific


def _batch_load_contrib_counts(session_ids: list) -> dict:
    """Batch-load speaker contribution counts for a list of session IDs."""
    if not session_ids:
        return {}
    return dict(
        db.session.query(
            HansardContribution.session_id,
            func.count(HansardContribution.id),
        )
        .filter(
            HansardContribution.session_id.in_(session_ids),
            HansardContribution.member_name.isnot(None),
        )
        .group_by(HansardContribution.session_id)
        .all()
    )


def _session_or_404(slug: str) -> HansardSession:
    """Fetch a non-container session by slug or 404."""
    session = (
        HansardSession.query
        .filter_by(slug=slug, is_container=False)
        .first()
    )
    if session is None:
        abort(404)
    return session


# ---------------------------------------------------------------------------
# Related sessions
# ---------------------------------------------------------------------------

def _normalise_title(title: str) -> str:
    """
    Normalise a session title for related-session matching.

    Handles two legislative patterns:
      - SI pattern: strips 'Draft ' prefix
        ("Draft Warm Home Discount Regulations" â†’ "Warm Home Discount Regulations")
      - Lords Bill pattern: strips '[Lords]' suffix
        ("Finance Bill [Lords]" â†’ "Finance Bill")
      - Whitespace: collapses double-spaces (seen in some Hansard API titles)
    """
    t = title.strip()
    t = re.sub(r'^\s*Draft\s+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*\[Lords\]\s*$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.lower()


# Procedural titles that recur constantly with no relationship between instances.
# A denylist is more precise than a frequency threshold: high-frequency Bills
# (Crime and Policing Bill: 48 sessions) are genuine related stages and must
# NOT be suppressed. Only titles that are the same event repeated â€” not stages
# of one legislative item â€” belong here.
_PROCEDURAL_TITLE_NOISE: frozenset[str] = frozenset({
    "Topical Questions",
    "Arrangement of Business",
    "Points of Order",
    "Point of Order",
    "Business of the House",
    "Engagements",
    "Speaker's Statement",
    "Speakerâ€™s Statement",   # curly apostrophe variant
    "Business without Debate",
    "Retirements of Members",
    "Retirement of a Member",
    "Oral Questions",
    "Written Statements",
})

_RELATED_PANEL_MAX = 6   # max cards shown inline; Bills with more stages get a count badge


def _is_procedural_noise(title: str) -> bool:
    return title.strip() in _PROCEDURAL_TITLE_NOISE


def _related_sessions(session: HansardSession) -> dict:
    """
    Find sessions related to the given one by normalised title match.

    Matching strategy (exact normalised title only â€” no fuzzy matching):
      - Exact match covers: Lords SI GCâ†’Chamber (identical title), cross-house
        Bill stages where title is unchanged, topical recurrences.
      - 'Draft X' normalisation covers: SI draft laid before passage vs
        approved instrument (SI-specific pattern).
      - '[Lords]' normalisation covers: Lords-originated Bills whose Commons
        title drops the '[Lords]' suffix.

    Window: 365 days â€” Bill stages span Commons Second Reading (Oct) to Lords
    Third Reading (Mar); SIs typically resolve within days.

    Noise guard: denylist of known procedural titles that recur constantly with
    no relationship between instances (PMQs 'Engagements', 'Business of the
    House', etc.). Bills with 48+ stages are NOT noise â€” they're genuine related
    stages and must not be suppressed by a frequency threshold.

    Returns dict:
      items      â€” up to _RELATED_PANEL_MAX cards, sorted by date proximity
      total      â€” total related sessions found (for overflow badge)
    """
    if _is_procedural_noise(session.title):
        return {"items": [], "total": 0}

    norm = _normalise_title(session.title)
    if not norm:
        return {"items": [], "total": 0}

    date_min = session.date - timedelta(days=365)
    date_max = session.date + timedelta(days=365)

    # Broad DB filter using title prefix; Python post-filters to exact normalised match.
    prefix = norm[:20]
    candidates = (
        HansardSession.query
        .filter(
            HansardSession.is_container == False,
            HansardSession.id != session.id,
            HansardSession.date >= date_min,
            HansardSession.date <= date_max,
            HansardSession.title.ilike(f"%{prefix}%"),
        )
        .order_by(HansardSession.date)
        .limit(60)
        .all()
    )

    related = [
        c for c in candidates
        if _normalise_title(c.title) == norm
        and not _is_procedural_noise(c.title)
    ]

    if not related:
        return {"items": [], "total": 0}

    # Chronological ascending â€” shows parliamentary journey oldest-to-newest.
    related.sort(key=lambda c: c.date)
    total = len(related)
    display = related[:_RELATED_PANEL_MAX]

    # Batch-load contrib counts (avoids N+1)
    display_ids = [c.id for c in display]
    counts = dict(
        db.session.query(
            HansardContribution.session_id,
            func.count(HansardContribution.id),
        )
        .filter(
            HansardContribution.session_id.in_(display_ids),
            HansardContribution.member_name.isnot(None),
        )
        .group_by(HansardContribution.session_id)
        .all()
    )

    items = [
        {
            "session":           c,
            "human_date":        _human_date(c.date),
            "url_date":          _url_date(c.date),
            "debate_type_label": _DEBATE_TYPE_LABELS.get(c.debate_type, "Proceedings"),
            "contrib_count":     counts.get(c.id, 0),
        }
        for c in display
    ]

    return {"items": items, "total": total}


def _day_navigation(session: HansardSession) -> dict:
    """
    Return the previous and next non-container session on the same date and
    in the same house, ordered by DB insertion id (which approximates chain
    order from the BFS walk).
    """
    base = dict(
        date=session.date,
        house=session.house,
        is_container=False,
    )
    prev_s = (
        HansardSession.query
        .filter_by(**base)
        .filter(HansardSession.id < session.id)
        .order_by(HansardSession.id.desc())
        .first()
    )
    next_s = (
        HansardSession.query
        .filter_by(**base)
        .filter(HansardSession.id > session.id)
        .order_by(HansardSession.id.asc())
        .first()
    )

    def _nav_item(s):
        return {
            "title": s.title,
            "url":   f"/archive/debate/{_url_date(s.date)}/{s.slug}",
            "dtype": _DEBATE_TYPE_LABELS.get(s.debate_type, "Proceedings"),
        }

    return {
        "prev": _nav_item(prev_s) if prev_s else None,
        "next": _nav_item(next_s) if next_s else None,
    }


def _session_context(session: HansardSession) -> dict:
    """Build common template context for a session."""
    raw_contribs = (
        session.contributions
        .filter(HansardContribution.member_name.isnot(None))
        .order_by(HansardContribution.speech_order)
        .all()
    )
    themes = session.themes.all()
    policy_areas = sorted(
        {t.theme for t in themes if t.theme_type == THEME_TYPE_POLICY_AREA}
    )
    specific_topics = sorted(
        {t.theme for t in themes if t.theme_type == THEME_TYPE_SPECIFIC}
    )

    contributions = []
    for c in raw_contribs:
        attr = _parse_attribution(c.member_name)
        party_name = attr["party"] or (c.party if c.party else None)
        contributions.append({
            "id":           c.id,
            "member_id":    c.member_id,
            "raw_name":     c.member_name,
            "name":         attr["name"],
            "role":         attr["role"],
            "party":        party_name,
            "party_colour": _PARTY_COLOURS.get(party_name, _DEFAULT_PARTY_COLOUR) if party_name else _DEFAULT_PARTY_COLOUR,
            "speech_text":  c.speech_text or "",
        })

    unique_speakers = len({c.member_id for c in raw_contribs if c.member_id is not None})

    return {
        "session":           session,
        "contributions":     contributions,
        "unique_speakers":   unique_speakers,
        "policy_areas":      policy_areas,
        "specific_topics":   specific_topics,
        "human_date":        _human_date(session.date),
        "url_date":          _url_date(session.date),
        "debate_type_label": _DEBATE_TYPE_LABELS.get(session.debate_type, "Proceedings"),
        "department":        session.department or "",
        "related_sessions":  _related_sessions(session),
        "day_nav":           _day_navigation(session),
    }


# ---------------------------------------------------------------------------
# Archive home â€” search and browse
# ---------------------------------------------------------------------------

_PER_PAGE       = 25
_GROUPS_PER_PAGE = 7   # groups shown per page in grouped OQ / PMQs view


def _build_oq_group_header(dtype_filter: str, date, house: str, dept: str) -> str:
    ds = _human_date(date)
    if dtype_filter == "pmqs":
        return f"{ds} â€” Prime Minister's Questions"
    if house == "Lords":
        return f"{ds} â€” Lords Oral Questions"
    if dept:
        return f"{ds} â€” {dept} Oral Questions"
    return f"{ds} â€” Oral Questions"


def _last_ingested_label() -> str:
    """Human-readable label for the most recently ingested session date."""
    row = (
        db.session.query(func.max(HansardSession.date))
        .filter(HansardSession.is_container == False)
        .scalar()
    )
    if not row:
        return ""
    return _human_date(row)


def _archive_start_label() -> str:
    """Human-readable label for the earliest session date in the archive."""
    row = (
        db.session.query(func.min(HansardSession.date))
        .filter(HansardSession.is_container == False)
        .scalar()
    )
    if not row:
        return ""
    return _human_date(row)


_VOCAB_CACHE_TTL  = 300   # 5 minutes â€” refreshed after each ingest cycle
_RECENT_CACHE_TTL = 900   # 15 minutes â€” recent-additions widget
_policy_area_cache: tuple[list, float] | None = None
_dept_cache: tuple[list, float] | None = None
_recent_additions_cache: tuple[dict, float] | None = None


def _all_policy_areas() -> list[str]:
    """Sorted list of all policy area terms present in the corpus (cached 5 min)."""
    global _policy_area_cache
    now = time.monotonic()
    if _policy_area_cache and now - _policy_area_cache[1] < _VOCAB_CACHE_TTL:
        return _policy_area_cache[0]
    result = sorted(
        r[0] for r in db.session.query(
            func.distinct(HansardSessionTheme.theme)
        ).filter(HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA).all()
    )
    _policy_area_cache = (result, now)
    return result


def _all_departments() -> list[str]:
    """Sorted list of departments that have attributed oral questions sessions (cached 5 min)."""
    global _dept_cache
    now = time.monotonic()
    if _dept_cache and now - _dept_cache[1] < _VOCAB_CACHE_TTL:
        return _dept_cache[0]
    result = sorted(
        r[0] for r in db.session.query(
            func.distinct(HansardSession.department)
        ).filter(HansardSession.department.isnot(None)).all()
    )
    _dept_cache = (result, now)
    return result


def _recent_additions() -> dict:
    """
    Count of non-container sessions ingested in the last 24 hours and the
    most recent ingestion timestamp. Cached for 15 minutes â€” no need for
    real-time freshness on the browse page.
    """
    global _recent_additions_cache
    now = time.monotonic()
    if _recent_additions_cache and now - _recent_additions_cache[1] < _RECENT_CACHE_TTL:
        return _recent_additions_cache[0]

    from datetime import datetime as dt, timedelta
    cutoff = dt.utcnow() - timedelta(hours=24)

    count_24h = (
        db.session.query(func.count(HansardSession.id))
        .filter(
            HansardSession.is_container == False,
            HansardSession.ingested_at >= cutoff,
        )
        .scalar()
    ) or 0

    last_at = db.session.query(func.max(HansardSession.ingested_at)).scalar()

    result = {"count_24h": count_24h, "last_ingested_at": last_at}
    _recent_additions_cache = (result, now)
    return result


@archive_bp.route("")
def archive_home():
    # /archive (root) is now merged into /hansard. 301 everything there.
    qs = request.query_string.decode()
    target = f"/hansard?{qs}" if qs else "/hansard"
    return redirect(target, 301)



# ---------------------------------------------------------------------------
# Session detail
# ---------------------------------------------------------------------------

@archive_bp.route("/debate/<string:date_str>/<string:slug>")
def session_detail(date_str: str, slug: str):
    session = _session_or_404(slug)
    if _url_date(session.date) != date_str:
        abort(404)
    ctx = _session_context(session)
    return render_template("hansard_archive/session_detail.html", **ctx)


# ---------------------------------------------------------------------------
# Word export â€” session transcript
# ---------------------------------------------------------------------------

@archive_bp.route("/debate/<string:date_str>/<string:slug>/word")
def session_word(date_str: str, slug: str):
    session = _session_or_404(slug)
    if _url_date(session.date) != date_str:
        abort(404)
    ctx = _session_context(session)

    doc = Document()

    # Title
    title_para = doc.add_heading(session.title, level=1)
    title_para.runs[0].font.size = Pt(16)

    # Metadata line
    meta = doc.add_paragraph()
    meta.add_run(f"{ctx['human_date']}  Â·  {session.house}  Â·  {ctx['debate_type_label']}").font.size = Pt(10)

    if ctx["policy_areas"]:
        pa = doc.add_paragraph()
        pa.add_run("Policy areas: ").bold = True
        pa.add_run(", ".join(ctx["policy_areas"])).font.size = Pt(10)

    if ctx["specific_topics"]:
        st = doc.add_paragraph()
        st.add_run("Topics: ").bold = True
        st.add_run(", ".join(ctx["specific_topics"])).font.size = Pt(10)

    if session.hansard_url:
        src = doc.add_paragraph()
        src.add_run("Source: ").bold = True
        src.add_run(session.hansard_url).font.size = Pt(10)

    doc.add_paragraph()  # spacer

    # Contributions
    for item in ctx["contributions"]:
        speaker = item["name"]
        if item["role"]:
            speaker += f" ({item['role']})"
        elif item["party"]:
            speaker += f" ({item['party']})"
        spk_para = doc.add_paragraph()
        spk_run = spk_para.add_run(speaker)
        spk_run.bold = True
        spk_run.font.size = Pt(11)
        spk_run.font.color.rgb = RGBColor(0x1A, 0x4A, 0x6E)

        for para_text in (item["speech_text"] or "").split("\n"):
            para_text = para_text.strip()
            if para_text:
                p = doc.add_paragraph(para_text)
                p.runs[0].font.size = Pt(11)
        doc.add_paragraph()  # spacer between contributions

    # Footer
    footer = doc.add_paragraph()
    footer.add_run(
        f"Source: Hansard (Parliament Open Parliament Licence v3.0). "
        f"Exported from Westminster Brief â€” westminsterbrief.co.uk"
    ).font.size = Pt(9)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)

    safe_title = "".join(c if c.isalnum() or c in " -" else "" for c in session.title)[:60]
    filename = f"{ctx['url_date']}-{safe_title.lower().replace(' ', '-')}.docx"

    response = make_response(buf.read())
    response.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# Date browse â€” /archive/date/<date_str>
# ---------------------------------------------------------------------------

@archive_bp.route("/date/<string:date_str>")
def archive_date(date_str: str):
    d = _parse_url_date(date_str)
    if d is None:
        abort(404)

    sessions = (
        HansardSession.query
        .filter_by(is_container=False, date=d)
        .order_by(HansardSession.id)   # Hansard chain order
        .all()
    )
    if not sessions:
        abort(404)

    session_ids    = [s.id for s in sessions]
    contrib_counts = _batch_load_contrib_counts(session_ids)
    policy_areas, specific_topics = _batch_load_tags(session_ids)
    items = _build_session_items(sessions, contrib_counts, policy_areas, specific_topics)

    human = _human_date(d)
    return render_template(
        "hansard_archive/archive_collection.html",
        page_type      = "date",
        heading        = human,
        subtitle       = f"{len(items)} session{'s' if len(items) != 1 else ''}",
        breadcrumb     = [("Hansard Archive", "/archive"), (human, None)],
        items          = items,
        page           = 1,
        total_pages    = 1,
        total          = len(items),
        per_page       = len(items),
        canonical_path = f"/archive/date/{date_str}",
        og_title       = f"{human} â€” Hansard Archive",
        meta_desc      = (
            f"Parliamentary debates from {human}. "
            f"{len(items)} Hansard session{'s' if len(items) != 1 else ''} including "
            f"Commons and Lords proceedings."
        ),
        json_ld_type   = "CollectionPage",
    )


# ---------------------------------------------------------------------------
# MP page â€” /archive/mp/<member_id>
# ---------------------------------------------------------------------------

@archive_bp.route("/mp/<int:member_id>")
def archive_mp(member_id: int):
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    policy_filter = request.args.get("policy", "").strip()

    # Representative name row
    name_row = (
        HansardContribution.query
        .filter_by(member_id=member_id)
        .filter(HansardContribution.member_name.isnot(None))
        .first()
    )
    if name_row is None:
        abort(404)
    member_attr = _parse_attribution(name_row.member_name)

    # Distinct sessions this member contributed to, newest first.
    # SELECT includes session_id AND date so ORDER BY date satisfies Postgres's
    # rule that DISTINCT queries can only order by selected columns.
    session_ids_q = (
        db.session.query(HansardContribution.session_id, HansardSession.date)
        .join(HansardSession, HansardSession.id == HansardContribution.session_id)
        .filter(
            HansardContribution.member_id == member_id,
            HansardContribution.member_name.isnot(None),
            HansardSession.is_container == False,
        )
        .distinct()
        .order_by(HansardSession.date.desc())
    )

    if policy_filter:
        tagged_sids = (
            db.session.query(HansardSessionTheme.session_id)
            .filter(
                HansardSessionTheme.theme == policy_filter,
                HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
            )
        )
        session_ids_q = session_ids_q.filter(
            HansardContribution.session_id.in_(tagged_sids)
        )

    total = session_ids_q.count()
    if total == 0 and not policy_filter:
        abort(404)

    total_pages  = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    page_sids    = [r[0] for r in session_ids_q.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()]

    sessions_by_id = {s.id: s for s in HansardSession.query.filter(HansardSession.id.in_(page_sids)).all()}

    # Contributions for this member on this page
    page_contribs = (
        HansardContribution.query
        .filter(
            HansardContribution.session_id.in_(page_sids),
            HansardContribution.member_id == member_id,
            HansardContribution.member_name.isnot(None),
        )
        .order_by(HansardContribution.speech_order)
        .all()
    )
    contribs_by_session: dict[int, list[str]] = defaultdict(list)
    for c in page_contribs:
        contribs_by_session[c.session_id].append(c.speech_text or "")

    items = [
        {
            "session":           sessions_by_id[sid],
            "human_date":        _human_date(sessions_by_id[sid].date),
            "url_date":          _url_date(sessions_by_id[sid].date),
            "debate_type_label": _DEBATE_TYPE_LABELS.get(sessions_by_id[sid].debate_type, "Proceedings"),
            "department":        sessions_by_id[sid].department or "",
            "speech_texts":      contribs_by_session.get(sid, []),
            "contrib_count":     len(contribs_by_session.get(sid, [])),
        }
        for sid in page_sids
        if sid in sessions_by_id
    ]

    name  = member_attr["name"]
    party = member_attr["party"] or ""
    qs_params: dict[str, str] = {}
    if policy_filter:
        qs_params["policy"] = policy_filter
    base_qs = urlencode(qs_params)

    # Load precomputed analytics (only populated for Commons MPs by compute job)
    analytics = db.session.get(MpAnalytics, member_id)

    return render_template(
        "hansard_archive/archive_mp.html",
        member_id          = member_id,
        member_name        = name,
        member_party       = party,
        member_party_slug  = _CONTRIB_CODE_TO_SLUG.get(party) if party else None,
        member_colour      = member_attr["party_colour"],
        items         = items,
        total         = total,
        total_pages   = total_pages,
        per_page      = _PER_PAGE,
        page          = page,
        base_qs       = base_qs,
        policy_filter = policy_filter,
        analytics     = analytics,
        og_title      = f"{name} â€” Hansard Archive",
        meta_desc     = (
            f"Parliamentary contributions by {name}{' (' + party + ')' if party else ''} "
            f"in the Hansard Archive. {total} session{'s' if total != 1 else ''} on record."
        ),
    )


# ---------------------------------------------------------------------------
# Department page â€” /archive/department/<slug>
# ---------------------------------------------------------------------------

@archive_bp.route("/department/<string:dept_slug>")
def archive_department(dept_slug: str):
    # Reverse-lookup: find department whose slugified name matches
    all_depts = _all_departments()
    dept_name = next((d for d in all_depts if slugify_theme(d) == dept_slug), None)
    if not dept_name:
        abort(404)

    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    stmt = (
        HansardSession.query
        .filter_by(is_container=False, department=dept_name)
        .order_by(HansardSession.date.desc())
    )
    total       = stmt.count()
    sessions    = stmt.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()
    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    session_ids    = [s.id for s in sessions]
    contrib_counts = _batch_load_contrib_counts(session_ids)
    policy_areas, specific_topics = _batch_load_tags(session_ids)
    items          = _build_session_items(sessions, contrib_counts, policy_areas, specific_topics)

    base_qs = urlencode({"page": page}) if page > 1 else ""

    return render_template(
        "hansard_archive/archive_collection.html",
        page_type      = "department",
        heading        = dept_name,
        subtitle       = f"{total} oral questions session{'s' if total != 1 else ''}",
        breadcrumb     = [("Hansard Archive", "/archive"), (dept_name, None)],
        items          = items,
        page           = page,
        total_pages    = total_pages,
        total          = total,
        per_page       = _PER_PAGE,
        base_qs        = base_qs,
        canonical_path = f"/archive/department/{dept_slug}",
        og_title       = f"{dept_name} â€” Hansard Archive",
        meta_desc      = (
            f"Parliamentary questions and debates answered by the {dept_name}. "
            f"{total} Hansard session{'s' if total != 1 else ''} in the archive."
        ),
        json_ld_type   = "GovernmentOrganization",
    )


# ---------------------------------------------------------------------------
# Policy area page â€” /archive/policy/<slug>
# ---------------------------------------------------------------------------

@archive_bp.route("/policy/<string:policy_slug>")
def archive_policy(policy_slug: str):
    # Reverse-lookup: find policy area whose slugified name matches
    all_policies = _all_policy_areas()
    policy_name  = next((p for p in all_policies if slugify_theme(p) == policy_slug), None)
    if not policy_name:
        abort(404)

    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    policy_sub = (
        db.session.query(HansardSessionTheme.session_id)
        .filter(
            HansardSessionTheme.theme == policy_name,
            HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
        )
        .subquery()
    )
    stmt = (
        HansardSession.query
        .filter_by(is_container=False)
        .filter(HansardSession.id.in_(policy_sub))
        .order_by(HansardSession.date.desc())
    )
    total       = stmt.count()
    sessions    = stmt.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()
    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    session_ids    = [s.id for s in sessions]
    contrib_counts = _batch_load_contrib_counts(session_ids)
    policy_areas, specific_topics = _batch_load_tags(session_ids)
    items          = _build_session_items(sessions, contrib_counts, policy_areas, specific_topics)

    base_qs = urlencode({"page": page}) if page > 1 else ""

    return render_template(
        "hansard_archive/archive_collection.html",
        page_type      = "policy",
        heading        = policy_name,
        subtitle       = f"{total} session{'s' if total != 1 else ''} tagged with this policy area",
        breadcrumb     = [("Hansard Archive", "/archive"), (policy_name, None)],
        items          = items,
        page           = page,
        total_pages    = total_pages,
        total          = total,
        per_page       = _PER_PAGE,
        base_qs        = base_qs,
        canonical_path = f"/archive/policy/{policy_slug}",
        og_title       = f"{policy_name} â€” Hansard Archive",
        meta_desc      = (
            f"UK parliamentary debates on {policy_name}. "
            f"{total} Hansard session{'s' if total != 1 else ''} tagged with this GOV.UK policy area."
        ),
        json_ld_type   = "CollectionPage",
    )


# ---------------------------------------------------------------------------
# Topic brief page â€” /brief/<slug>   (canonical)
# Archive theme page â€” /archive/theme/<slug>  â†’  301 â†’ /brief/<slug>
# ---------------------------------------------------------------------------

# Westminster Brief brief slugs â†’ actual Hansard DB policy area name(s).
# The two taxonomies differ (e.g. "education" â‰  "Education, training and skills"),
# so we bridge them statically rather than relying on slugify round-trips.
_BRIEF_SLUG_TO_POLICY_AREAS: dict[str, list[str]] = {
    "economy":                                   ["Economy"],
    "employment-and-labour-market":              ["Employment and labour market"],
    "finance-and-taxation":                      ["Finance and taxation"],
    "government-and-public-administration":      ["Government and public administration"],
    "business-and-industry":                     ["Business and industry"],
    "education":                                 ["Education, training and skills", "Children and families"],
    "health-and-social-care":                    ["Health and social care"],
    "housing-and-planning":                      ["Housing and planning"],
    "transport":                                 ["Transport"],
    "crime-justice-and-law":                     ["Crime, justice and law"],
    "welfare-and-social-security":               ["Welfare and benefits"],
    "immigration-and-asylum":                    ["Immigration and borders"],
    "environment-and-climate-change":            ["Environment"],
    "defence-and-national-security":             ["Defence and armed forces"],
    "international-affairs":                     ["International development", "Foreign affairs and diplomacy"],
    "science-technology-and-innovation":         ["Science and technology"],
    "energy-and-utilities":                      ["Energy"],
    "work-and-pensions":                         ["Welfare and benefits", "Employment and labour market"],
    "agriculture-environment-and-rural-affairs": ["Environment"],
    "culture-media-and-sport":                   ["Society and culture"],
    "constitutional-affairs":                    ["Parliament and constitution"],
    "foreign-affairs":                           ["Foreign affairs and diplomacy"],
    "parliamentary-affairs":                     ["Parliament and constitution"],
}

# Headings for slugs where slugâ†’title-case produces awkward punctuation.
_BRIEF_SLUG_HEADING: dict[str, str] = {
    "crime-justice-and-law":                     "Crime, Justice and Law",
    "science-technology-and-innovation":         "Science, Technology and Innovation",
    "agriculture-environment-and-rural-affairs": "Agriculture, Environment and Rural Affairs",
    "culture-media-and-sport":                   "Culture, Media and Sport",
}


def _brief_theme_response(theme_slug: str, canonical_prefix: str):
    """
    Render /brief/<slug> pages.

    Lookup order:
    0. Bridge mapping (_BRIEF_SLUG_TO_POLICY_AREAS) â€” resolves the mismatch
       between brief page slugs and Hansard DB policy area taxonomy strings.
    1. Policy area slug reverse-lookup â€” catches any policy areas not in the map.
    2. Specific topic match (THEME_TYPE_SPECIFIC) â€” fallback for /archive/theme/ redirects.
    """
    # â”€â”€ 0. Bridge mapping: brief slug â†’ DB policy area name(s) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    policy_names = _BRIEF_SLUG_TO_POLICY_AREAS.get(theme_slug)
    if policy_names:
        theme_name      = _BRIEF_SLUG_HEADING.get(theme_slug) or theme_slug.replace("-", " ").title()
        theme_type_used = THEME_TYPE_POLICY_AREA
        session_filter  = (
            HansardSessionTheme.theme.in_(policy_names),
            HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
        )
    else:
        # â”€â”€ 1. Try policy area match via slug reverse-lookup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        all_policies = _all_policy_areas()
        policy_name  = next((p for p in all_policies if slugify_theme(p) == theme_slug), None)

        if policy_name:
            theme_name      = policy_name
            theme_type_used = THEME_TYPE_POLICY_AREA
            session_filter  = (
                HansardSessionTheme.theme == theme_name,
                HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
            )
        else:
            # â”€â”€ 2. Fallback: specific topic match â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            rows = (
                db.session.query(HansardSessionTheme.theme)
                .filter(HansardSessionTheme.theme_type == THEME_TYPE_SPECIFIC)
                .distinct()
                .all()
            )
            theme_name = next((r[0] for r in rows if slugify_theme(r[0]) == theme_slug), None)
            theme_type_used = THEME_TYPE_SPECIFIC
            session_filter  = (
                HansardSessionTheme.theme == theme_name,
                HansardSessionTheme.theme_type == THEME_TYPE_SPECIFIC,
            ) if theme_name else None

    if not theme_name:
        display_name = theme_slug.replace("-", " ").title()
        return render_template(
            "hansard_archive/brief_theme.html",
            page_type         = "theme",
            heading           = display_name,
            subtitle          = "No sessions on this topic in the archive",
            breadcrumb        = [("Hansard Archive", "/archive"), (display_name, None)],
            items             = [],
            page              = 1,
            total_pages       = 1,
            total             = 0,
            base_qs           = "",
            canonical_path    = f"{canonical_prefix}/{theme_slug}",
            og_title          = f"{display_name} â€” Westminster Brief",
            meta_desc         = f"No sessions found in the Westminster Brief archive for {display_name}.",
            json_ld_type      = "CollectionPage",
            headline_stat     = None,
        )

    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    theme_sub = (
        db.session.query(HansardSessionTheme.session_id)
        .filter(*session_filter)
        .subquery()
    )
    stmt = (
        HansardSession.query
        .filter_by(is_container=False)
        .filter(HansardSession.id.in_(theme_sub))
        .order_by(HansardSession.date.desc())
    )
    total       = stmt.count()
    sessions    = stmt.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()
    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    session_ids    = [s.id for s in sessions]
    contrib_counts = _batch_load_contrib_counts(session_ids)
    policy_areas, specific_topics = _batch_load_tags(session_ids)
    items          = _build_session_items(sessions, contrib_counts, policy_areas, specific_topics)

    base_qs = urlencode({"page": page}) if page > 1 else ""

    subtitle = (
        f"{total} session{'s' if total != 1 else ''} tagged with this policy area"
        if theme_type_used == THEME_TYPE_POLICY_AREA
        else f"{total} session{'s' if total != 1 else ''} on this topic"
    )

    # Headline stat teaser â€” shown as one-line link to /stats/<slug>
    headline_stat = None
    if theme_type_used == THEME_TYPE_POLICY_AREA and theme_slug in _BRIEF_SLUG_TO_POLICY_AREAS:
        from hansard_archive.models import HeadlineStat as _HeadlineStat
        headline_stat = (
            _HeadlineStat.query
            .filter_by(theme_slug=theme_slug)
            .filter(_HeadlineStat.latest_value.isnot(None))
            .first()
        )

    return render_template(
        "hansard_archive/brief_theme.html",
        page_type         = "policy" if theme_type_used == THEME_TYPE_POLICY_AREA else "theme",
        heading           = theme_name,
        subtitle          = subtitle,
        breadcrumb        = [("Hansard Archive", "/archive"), (theme_name, None)],
        items             = items,
        page              = page,
        total_pages       = total_pages,
        total             = total,
        per_page          = _PER_PAGE,
        base_qs           = base_qs,
        canonical_path    = f"{canonical_prefix}/{theme_slug}",
        og_title          = f"{theme_name} â€” Westminster Brief",
        meta_desc         = (
            f"UK parliamentary debates on {theme_name}. "
            f"{total} Hansard session{'s' if total != 1 else ''} tagged with this topic."
        ),
        json_ld_type      = "CollectionPage",
        headline_stat     = headline_stat,
        theme_slug        = theme_slug,
    )


@brief_bp.route("/<string:theme_slug>")
def brief_theme(theme_slug: str):
    return _brief_theme_response(theme_slug, canonical_prefix="/brief")


# ---------------------------------------------------------------------------
# Specific theme page â€” /archive/theme/<slug>  â†’  301 â†’ /brief/<slug>
# ---------------------------------------------------------------------------

@archive_bp.route("/theme/<string:theme_slug>")
def archive_theme(theme_slug: str):
    """301: /archive/theme/<slug> â†’ /brief/<slug>"""
    qs = ('?' + request.query_string.decode()) if request.query_string else ''
    return redirect(f"/brief/{theme_slug}{qs}", code=301)


# ---------------------------------------------------------------------------
# FTS search â€” /archive/search  (noindex)
# ---------------------------------------------------------------------------

_FTS_HEADLINE_OPTS = (
    "MaxFragments=1,StartSel=<mark>,StopSel=</mark>,MaxWords=35,MinWords=15"
)
# WQ question text should show in full â€” questions are typically 40-120 words
_FTS_PQ_HEADLINE_OPTS = (
    "MaxFragments=1,StartSel=<mark>,StopSel=</mark>,MaxWords=200,MinWords=40"
)


def _fts_search(
    q_raw: str,
    page: int,
    policy_filter: str = "",
    house_filter: str = "",
    dtype_filter: str = "",
    dept_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    title_only: bool = False,
) -> tuple[list, int]:
    """
    Full-text search using Postgres tsvectors. Returns (results, total_count).

    Each result dict contains:
      session_id, title, date, house, debate_type, slug, department,
      snippet (Markup â€” safe HTML with <mark> highlights), final_rank
    """
    # Detect phrase query (user wrapped in double quotes)
    if q_raw.startswith('"') and q_raw.endswith('"') and len(q_raw) > 2:
        ts_func = "phraseto_tsquery"
        q_clean = q_raw[1:-1]
    else:
        ts_func = "plainto_tsquery"
        q_clean = q_raw

    offset = (page - 1) * _PER_PAGE

    policy_join = (
        "JOIN ha_session_theme sth ON sth.session_id = s.id"
        "  AND sth.theme = :policy AND sth.theme_type = 'policy_area'"
        if policy_filter else ""
    )

    # Build optional WHERE conditions for extra filters
    extra_where_parts = []
    extra_params: dict = {}

    if house_filter in ("Commons", "Lords"):
        extra_where_parts.append("AND s.house = :house")
        extra_params["house"] = house_filter

    if dtype_filter and dtype_filter in _DEBATE_TYPE_LABELS:
        extra_where_parts.append("AND s.debate_type = :dtype")
        extra_params["dtype"] = dtype_filter

    if dept_filter:
        extra_where_parts.append("AND s.department ILIKE :dept")
        extra_params["dept"] = f"%{dept_filter}%"

    if date_from:
        extra_where_parts.append("AND s.date >= :date_from")
        extra_params["date_from"] = date_from

    if date_to:
        extra_where_parts.append("AND s.date <= :date_to")
        extra_params["date_to"] = date_to

    extra_where_sql = " ".join(extra_where_parts)

    body_match_clause = "" if title_only else f"OR c.id IS NOT NULL"

    count_sql = sqla_text(f"""
        SELECT COUNT(DISTINCT s.id)
        FROM ha_session s
        {policy_join}
        LEFT JOIN ha_contribution c ON c.session_id = s.id
            AND c.speech_tsv @@ {ts_func}('english', :q)
        WHERE s.is_container = false
          AND (
              s.title_tsv @@ {ts_func}('english', :q)
              {body_match_clause}
          )
          {extra_where_sql}
    """)
    rows_sql = sqla_text(f"""
        WITH best_contrib AS (
            SELECT DISTINCT ON (session_id)
                session_id,
                ts_rank(speech_tsv, {ts_func}('english', :q))      AS body_rank,
                ts_headline('english', speech_text,
                            {ts_func}('english', :q),
                            :hl_opts)                               AS snippet
            FROM ha_contribution
            WHERE speech_tsv @@ {ts_func}('english', :q)
            ORDER BY session_id, body_rank DESC
        ),
        title_match AS (
            SELECT id AS session_id,
                   ts_rank(title_tsv, {ts_func}('english', :q))    AS title_rank
            FROM ha_session
            WHERE title_tsv @@ {ts_func}('english', :q)
              AND is_container = false
        )
        SELECT
            s.id, s.title, s.date, s.house, s.debate_type, s.slug, s.department,
            COALESCE(tm.title_rank, 0.0) * 3
                + COALESCE(bc.body_rank, 0.0)                       AS final_rank,
            bc.snippet
        FROM ha_session s
        {policy_join}
        LEFT JOIN best_contrib bc ON bc.session_id = s.id
        LEFT JOIN title_match  tm ON tm.session_id = s.id
        WHERE s.is_container = false
          AND ({f"tm.session_id IS NOT NULL" if title_only else "bc.session_id IS NOT NULL OR tm.session_id IS NOT NULL"})
          {extra_where_sql}
        ORDER BY final_rank DESC, s.date DESC
        LIMIT :lim OFFSET :off
    """)

    count_params = {"q": q_clean}
    if policy_filter:
        count_params["policy"] = policy_filter
    count_params.update(extra_params)

    params = {"q": q_clean, "hl_opts": _FTS_HEADLINE_OPTS,
              "lim": _PER_PAGE, "off": offset}
    if policy_filter:
        params["policy"] = policy_filter
    params.update(extra_params)

    total   = db.session.execute(count_sql, count_params).scalar() or 0
    rows    = db.session.execute(rows_sql, params).fetchall()

    results = []
    for row in rows:
        sid, title, d, house, dtype, slug, dept, rank, snippet = row
        results.append({
            "session_id":        sid,
            "title":             title,
            "date":              d,
            "house":             house,
            "debate_type":       dtype,
            "debate_type_label": _DEBATE_TYPE_LABELS.get(dtype, "Proceedings"),
            "slug":              slug,
            "department":        dept or "",
            "human_date":        _human_date(d),
            "url_date":          _url_date(d),
            "snippet":           Markup(snippet) if snippet else None,
        })
    return results, total


def _pq_fts_search(q_raw: str, limit: int = 20, policy_filter: str = "") -> list:
    """
    Full-text search over ha_pq using question_tsv. Returns up to `limit` results.

    Each result dict:
      result_type, uin, heading, asking_member, answering_body,
      tabled_date, is_answered, url, human_date, snippet
    """
    if q_raw.startswith('"') and q_raw.endswith('"') and len(q_raw) > 2:
        ts_func = "phraseto_tsquery"
        q_clean = q_raw[1:-1]
    else:
        ts_func = "plainto_tsquery"
        q_clean = q_raw

    policy_join = (
        "JOIN ha_pq_theme pth ON pth.pq_id = p.id"
        "  AND pth.theme = :policy AND pth.theme_type = 'policy_area'"
        if policy_filter else ""
    )

    sql = sqla_text(f"""
        SELECT
            p.id, p.uin, p.heading, p.asking_member, p.answering_body,
            p.tabled_date, p.is_answered,
            ts_rank(p.question_tsv, {ts_func}('english', :q)) AS rank,
            ts_headline('english',
                coalesce(p.question_text, ''),
                {ts_func}('english', :q),
                :hl_opts) AS snippet,
            p.question_text
        FROM ha_pq p
        {policy_join}
        WHERE p.question_tsv @@ {ts_func}('english', :q)
        ORDER BY rank DESC
        LIMIT :lim
    """)

    sql_params = {"q": q_clean, "hl_opts": _FTS_PQ_HEADLINE_OPTS, "lim": limit}
    if policy_filter:
        sql_params["policy"] = policy_filter

    rows = db.session.execute(sql, sql_params).fetchall()

    results = []
    for row in rows:
        pq_id, uin, heading, asking, answering, tabled, is_answered, rank, snippet, question_text = row
        results.append({
            "result_type":   "pq",
            "uin":           uin,
            "heading":       heading or uin,
            "asking_member": asking or "",
            "answering_body":answering or "",
            "tabled_date":   tabled,
            "is_answered":   is_answered,
            "url":           f"/archive/pq/{uin}",
            "human_date":    _human_date(tabled) if tabled else "",
            "snippet":       Markup(snippet) if snippet else None,
            "question_text": question_text or "",
        })
    return results


def _ilike_search(q_raw: str, page: int) -> tuple[list, int]:
    """ilike fallback for SQLite local development."""
    stmt = (
        HansardSession.query
        .filter_by(is_container=False)
        .filter(HansardSession.title.ilike(f"%{q_raw}%"))
        .order_by(HansardSession.date.desc())
    )
    total    = stmt.count()
    sessions = stmt.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()
    results  = [
        {
            "session_id":        s.id,
            "title":             s.title,
            "date":              s.date,
            "house":             s.house,
            "debate_type":       s.debate_type,
            "debate_type_label": _DEBATE_TYPE_LABELS.get(s.debate_type, "Proceedings"),
            "slug":              s.slug,
            "department":        s.department or "",
            "human_date":        _human_date(s.date),
            "url_date":          _url_date(s.date),
            "snippet":           None,
        }
        for s in sessions
    ]
    return results, total


@archive_bp.route("/search")
def archive_search():
    q             = request.args.get("q", "").strip()
    policy_filter = request.args.get("policy", "").strip()
    house_filter  = request.args.get("house", "").strip()
    dtype_filter  = request.args.get("dtype", "").strip()
    date_from     = request.args.get("from", "").strip()
    date_to       = request.args.get("to", "").strip()
    title_only    = request.args.get("title_only") == "1"
    tab_param     = request.args.get("tab", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    results, total, total_pages = [], 0, 1
    pq_results = []
    error_msg = ""

    if q:
        try:
            if _is_postgres():
                results, total = _fts_search(
                    q, page,
                    policy_filter=policy_filter,
                    house_filter=house_filter,
                    dtype_filter=dtype_filter,
                    date_from=date_from,
                    date_to=date_to,
                    title_only=title_only,
                )
                pq_results = _pq_fts_search(q, limit=20)
            else:
                results, total = _ilike_search(q, page)
            total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
        except Exception as exc:
            error_msg = "Search is temporarily unavailable."
            print(f"[archive_search] error: {exc}", flush=True)

    pq_count = len(pq_results)

    # Active tab: explicit URL param wins; otherwise auto-switch when debates
    # are empty but PQs have results, so the user lands on content not emptiness.
    if tab_param in ("hansard", "pq"):
        active_tab = tab_param
        auto_switched = False
    elif total == 0 and pq_count > 0:
        active_tab = "pq"
        auto_switched = True
    else:
        active_tab = "hansard"
        auto_switched = False

    qs_parts: dict = {"q": q}
    for k, v in [("policy", policy_filter), ("house", house_filter),
                 ("dtype", dtype_filter), ("from", date_from), ("to", date_to)]:
        if v:
            qs_parts[k] = v
    if title_only:
        qs_parts["title_only"] = "1"
    if page > 1:
        qs_parts["page"] = page
    # Preserve explicit tab choice across pagination
    if tab_param in ("hansard", "pq"):
        qs_parts["tab"] = tab_param
    base_qs = urlencode(qs_parts)

    # Separate base_qs without tab for the auto-switch notice link
    notice_qs_parts = {k: v for k, v in qs_parts.items() if k != "tab"}
    notice_qs = urlencode(notice_qs_parts)

    has_filters = any([house_filter, dtype_filter,
                       policy_filter, date_from, date_to, title_only])

    resp = make_response(render_template(
        "hansard_archive/archive_search.html",
        q                   = q,
        policy_filter       = policy_filter,
        house_filter        = house_filter,
        dtype_filter        = dtype_filter,
        date_from           = date_from,
        date_to             = date_to,
        title_only          = title_only,
        has_filters         = has_filters,
        all_policy_areas    = _all_policy_areas(),
        debate_type_labels  = _DEBATE_TYPE_LABELS,
        results             = results,
        pq_results          = pq_results,
        pq_count            = pq_count,
        total               = total,
        total_pages         = total_pages,
        per_page            = _PER_PAGE,
        page                = page,
        base_qs             = base_qs,
        notice_qs           = notice_qs,
        error_msg           = error_msg,
        is_postgres         = _is_postgres(),
        active_tab          = active_tab,
        auto_switched       = auto_switched,
    ))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


# ---------------------------------------------------------------------------
# Written Question detail â€” /archive/pq/<uin>
# ---------------------------------------------------------------------------

@archive_bp.route("/pq/<string:uin>")
def pq_detail(uin: str):
    pq = HaPQ.query.filter_by(uin=uin.upper()).first()
    if pq is None:
        # Try as-is (some UIDs may not be uppercase)
        pq = HaPQ.query.filter_by(uin=uin).first()
    if pq is None:
        abort(404)

    themes = HaPQTheme.query.filter_by(pq_id=pq.id).all()
    policy_areas = sorted({t.theme for t in themes if t.theme_type == THEME_TYPE_POLICY_AREA})
    specific_topics = sorted({t.theme for t in themes if t.theme_type == THEME_TYPE_SPECIFIC})

    # Enrich asking and answering members with party + constituency from cached_member
    asking_party = asking_role = None
    if pq.asking_mnis_id:
        cached = CachedMember.get(pq.asking_mnis_id)
        if cached:
            asking_party = cached.party
            asking_role = ("Life Peer" if cached.house == "Lords"
                           else f"MP for {cached.constituency}" if cached.constituency else None)

    answering_party = answering_role = None
    if pq.answering_mnis_id:
        cached = CachedMember.get(pq.answering_mnis_id)
        if cached:
            answering_party = cached.party
            if cached.ministerial_role:
                answering_role = cached.ministerial_role
            elif cached.house == "Lords":
                answering_role = "Life Peer"
            elif cached.constituency:
                answering_role = f"MP for {cached.constituency}"

    seo_title = f"{pq.heading or pq.uin} â€” {pq.uin} â€” Westminster Brief"

    return render_template(
        "hansard_archive/archive_pq_detail.html",
        pq              = pq,
        asking_party    = asking_party,
        asking_role     = asking_role,
        answering_party = answering_party,
        answering_role  = answering_role,
        policy_areas    = policy_areas,
        specific_topics = specific_topics,
        human_tabled    = _human_date(pq.tabled_date) if pq.tabled_date else "",
        human_answered  = _human_date(pq.answer_date) if pq.answer_date else "",
        human_updated   = _human_date(pq.updated_at.date()) if pq.updated_at else "",
        seo_title       = seo_title,
        meta_desc       = (
            f"Written Question {pq.uin} â€” {pq.heading or 'Written Question'} "
            f"tabled by {pq.asking_member or 'an MP'} to {pq.answering_body or 'a department'}."
        ),
        canonical_path  = f"/archive/pq/{pq.uin}",
    )


# ---------------------------------------------------------------------------
# Party page â€” /archive/party/<slug>
# ---------------------------------------------------------------------------

_PARTY_SLUG_MAP: dict[str, dict] = {
    "labour": {
        "full_name": "Labour",
        "description": "UK political party, currently in government following the July 2024 general election. Founded 1900.",
        "contrib_codes": {"Lab", "Lab/Co-op", "Lab Co-op", "Lab/ Co-op"},
        "cached_names": {"Labour", "Labour (Co-op)"},
        "website": "https://labour.org.uk",
        "manifesto_labels": {
            2024: {"label": "Labour Party General Election Manifesto 2024", "heading": "2024 general election"},
        },
        "is_government": True,
        "sinn_fein_note": False,
    },
    "conservative": {
        "full_name": "Conservative",
        "description": "UK political party, currently the official opposition following the July 2024 general election. Founded 1834.",
        "contrib_codes": {"Con"},
        "cached_names": {"Conservative"},
        "website": "https://www.conservatives.com",
        "manifesto_labels": {
            2024: {"label": "Conservative Party General Election Manifesto 2024", "heading": "2024 general election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "liberal-democrat": {
        "full_name": "Liberal Democrats",
        "description": "UK political party, founded 1988.",
        "contrib_codes": {"LD"},
        "cached_names": {"Liberal Democrat"},
        "website": "https://www.libdems.org.uk",
        "manifesto_labels": {
            2024: {"label": "Liberal Democrat General Election Manifesto 2024", "heading": "2024 general election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "scottish-national-party": {
        "full_name": "Scottish National Party",
        "description": "Scottish political party advocating Scottish independence, standing candidates only in Scottish constituencies. Founded 1934.",
        "contrib_codes": {"SNP"},
        "cached_names": {"Scottish National Party"},
        "website": "https://www.snp.org",
        "manifesto_labels": {
            2024: {"label": "SNP General Election Manifesto 2024", "heading": "2024 general election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "reform-uk": {
        "full_name": "Reform UK",
        "description": "UK political party, founded 2021.",
        "contrib_codes": {"Reform"},
        "cached_names": {"Reform UK"},
        "website": "https://www.reformparty.uk",
        "manifesto_labels": {
            2024: {"label": "Reform UK Contract with the People 2024", "heading": "2024 general election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "green": {
        "full_name": "Green Party",
        "description": "UK political party prioritising environmental policy, founded 1990.",
        "contrib_codes": {"Green"},
        "cached_names": {"Green Party"},
        "website": "https://www.greenparty.org.uk",
        "manifesto_labels": {
            2024: {"label": "Green Party of England and Wales General Election Manifesto 2024", "heading": "2024 general election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "plaid-cymru": {
        "full_name": "Plaid Cymru",
        "description": "Welsh political party advocating Welsh independence, standing candidates only in Welsh constituencies. Founded 1925.",
        "contrib_codes": {"PC"},
        "cached_names": {"Plaid Cymru"},
        "website": "https://www.plaid.cymru",
        "manifesto_labels": {
            2026: {"label": "Plaid Cymru Senedd Election Manifesto 2026", "heading": "2026 Senedd election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "democratic-unionist-party": {
        "full_name": "Democratic Unionist Party",
        "description": "Northern Irish political party advocating Northern Ireland's continued union with the United Kingdom. Founded 1971.",
        "contrib_codes": {"DUP"},
        "cached_names": {"Democratic Unionist Party"},
        "website": "https://www.mydup.com",
        "manifesto_labels": {
            2024: {"label": "Democratic Unionist Party General Election Manifesto 2024", "heading": "2024 general election"},
        },
        "is_government": False,
        "sinn_fein_note": False,
    },
    "sinn-fein": {
        "full_name": "Sinn FÃ©in",
        "description": "Irish republican party with seats across Northern Ireland constituencies. Founded 1905.",
        "contrib_codes": set(),
        "cached_names": {"Sinn FÃ©in"},
        "website": "https://www.sinnfein.ie",
        "manifesto_labels": {},
        "is_government": False,
        "sinn_fein_note": True,
    },
}

_CONTRIB_CODE_TO_SLUG: dict[str, str] = {
    code: slug
    for slug, cfg in _PARTY_SLUG_MAP.items()
    for code in cfg["contrib_codes"]
}

_PARTY_SLUG_COLOURS: dict[str, str] = {
    "labour":                   "#E4003B",
    "conservative":             "#0087DC",
    "liberal-democrat":         "#FAA61A",
    "scottish-national-party":  "#c9a800",
    "reform-uk":                "#12B6CF",
    "green":                    "#02A95B",
    "plaid-cymru":              "#005B54",
    "democratic-unionist-party":"#CF1F25",
    "sinn-fein":                "#326760",
}

_party_data_cache: dict[str, tuple[dict, float]] = {}
_PARTY_CACHE_TTL = 3600  # 1 hour


def _compute_party_policy_positions(
    party_slug: str,
    contrib_codes: list[str],
    is_government: bool,
    manifesto_labels: dict,
) -> list[dict]:
    """
    Return a list of year-groups, each containing per-policy-area records:
    - chunk texts (all approved chunks where this area is primary)
    - up to 3 recent sessions where the party contributed on that area
    - up to 2 WMS (Labour only)
    Groups are sorted newest-year-first; policy areas alphabetical within each group.
    """
    chunk_rows = (
        db.session.query(ManifestoChunk, ManifestoChunkTag.policy_area)
        .join(
            ManifestoChunkTag,
            (ManifestoChunkTag.chunk_id == ManifestoChunk.id) &
            (ManifestoChunkTag.is_primary == True),
        )
        .filter(
            ManifestoChunk.party_slug == party_slug,
            ManifestoChunk.review_status == "approved",
        )
        .all()
    )

    if not chunk_rows:
        return []

    # Group by (year, policy_area)
    year_area_chunks: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for chunk, policy_area in chunk_rows:
        year_area_chunks[chunk.manifesto_year][policy_area].append(chunk)

    year_groups = []
    for year in sorted(year_area_chunks, reverse=True):  # newest year first
        area_chunks = year_area_chunks[year]
        year_meta = manifesto_labels.get(year, {})
        year_label = year_meta.get("label", f"{party_slug.replace('-', ' ').title()} {year} Manifesto")
        year_heading = year_meta.get("heading", f"{year} election")

        positions = []
        for policy_area in sorted(area_chunks):
            all_chunks = sorted(area_chunks[policy_area], key=lambda c: c.id)

            sessions: list[dict] = []
            if contrib_codes:
                contrib_sids = (
                    db.session.query(HansardContribution.session_id)
                    .join(HansardSession, HansardSession.id == HansardContribution.session_id)
                    .filter(
                        HansardSession.is_container == False,
                        HansardContribution.party.in_(contrib_codes),
                    )
                )
                tagged_sids = (
                    db.session.query(HansardSessionTheme.session_id)
                    .filter(
                        HansardSessionTheme.theme == policy_area,
                        HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
                    )
                )
                session_rows = (
                    HansardSession.query
                    .filter(
                        HansardSession.is_container == False,
                        HansardSession.id.in_(contrib_sids),
                        HansardSession.id.in_(tagged_sids),
                    )
                    .order_by(HansardSession.date.desc())
                    .limit(3)
                    .all()
                )
                for s in session_rows:
                    sessions.append({
                        "title":             s.title,
                        "slug":              s.slug,
                        "human_date":        _human_date(s.date),
                        "url_date":          _url_date(s.date),
                        "debate_type_label": _DEBATE_TYPE_LABELS.get(s.debate_type, "Proceedings"),
                    })

            wms: list[dict] = []
            if is_government:
                wms_tagged = (
                    db.session.query(HansardSessionTheme.session_id)
                    .filter(
                        HansardSessionTheme.theme == policy_area,
                        HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
                    )
                )
                wms_rows = (
                    HansardSession.query
                    .filter(
                        HansardSession.debate_type == "wms",
                        HansardSession.id.in_(wms_tagged),
                    )
                    .order_by(HansardSession.date.desc())
                    .limit(2)
                    .all()
                )
                for s in wms_rows:
                    wms.append({
                        "title":      s.title,
                        "slug":       s.slug,
                        "human_date": _human_date(s.date),
                        "url_date":   _url_date(s.date),
                    })

            positions.append({
                "policy_area":   policy_area,
                "slug":          slugify_theme(policy_area),
                "chunk_count":   len(all_chunks),
                "chunks": [
                    {
                        "text":           c.chunk_text,
                        "source_section": c.source_section or "",
                        "source_url":     c.source_url or "",
                        "source_page":    c.source_page,
                        "pdf_url":        c.pdf_url or "",
                        "year_label":     year_label,
                    }
                    for c in all_chunks
                ],
                "sessions":      sessions,
                "wms":           wms,
            })

        year_groups.append({
            "year":     year,
            "heading":  year_heading,
            "positions": positions,
        })

    return year_groups


def _compute_party_data(slug: str) -> dict | None:
    cfg = _PARTY_SLUG_MAP.get(slug)
    if cfg is None:
        return None

    contrib_codes = list(cfg["contrib_codes"])
    cached_names  = list(cfg["cached_names"])

    # Member counts from cached_member (current members only)
    member_counts: dict[str, int] = {}
    for house, count in (
        db.session.query(CachedMember.house, func.count(CachedMember.id))
        .filter(CachedMember.party.in_(cached_names))
        .group_by(CachedMember.house)
        .all()
    ):
        member_counts[house] = count

    total_contributions = 0
    dtype_breakdown:    list[dict] = []
    monthly_data:       list[dict] = []
    top_policy_areas:   list[dict] = []
    top_voices:         list[dict] = []
    recent_activity:    list[dict] = []

    if contrib_codes:
        # Total contributions
        total_contributions = (
            db.session.query(func.count(HansardContribution.id))
            .join(HansardSession, HansardSession.id == HansardContribution.session_id)
            .filter(
                HansardSession.is_container == False,
                HansardContribution.party.in_(contrib_codes),
            )
            .scalar()
        ) or 0

        # Debate type breakdown
        dtype_rows = (
            db.session.query(
                HansardSession.debate_type,
                func.count(HansardContribution.id).label("n"),
            )
            .join(HansardContribution, HansardContribution.session_id == HansardSession.id)
            .filter(
                HansardSession.is_container == False,
                HansardContribution.party.in_(contrib_codes),
            )
            .group_by(HansardSession.debate_type)
            .order_by(func.count(HansardContribution.id).desc())
            .all()
        )
        dtype_breakdown = [
            {
                "debate_type": row.debate_type,
                "label":       _DEBATE_TYPE_LABELS.get(row.debate_type, "Proceedings"),
                "count":       row.n,
            }
            for row in dtype_rows
        ]

        # Monthly activity timeline â€” Postgres only
        if _is_postgres():
            twelve_months_ago = (date_type.today().replace(day=1) - timedelta(days=365))
            monthly_sql = sqla_text("""
                SELECT DATE_TRUNC('month', s.date) AS mo, COUNT(c.id) AS n
                FROM ha_contribution c
                JOIN ha_session s ON s.id = c.session_id
                WHERE s.is_container = FALSE
                  AND c.party = ANY(:codes)
                  AND s.date >= :cutoff
                GROUP BY mo
                ORDER BY mo
            """)
            monthly_rows = db.session.execute(monthly_sql, {
                "codes": contrib_codes,
                "cutoff": twelve_months_ago,
            }).fetchall()
            if monthly_rows:
                max_n = max(r[1] for r in monthly_rows) or 1
                monthly_data = [
                    {
                        "month":       r[0],
                        "label":       r[0].strftime("%b %Y") if r[0] else "",
                        "month_abbr":  r[0].strftime("%b") if r[0] else "",
                        "count":       r[1],
                        "pct":         round(r[1] / max_n * 100),
                    }
                    for r in monthly_rows
                ]

        # Top policy areas â€” count distinct sessions with party contributions
        contrib_session_q = (
            db.session.query(HansardContribution.session_id)
            .join(HansardSession, HansardSession.id == HansardContribution.session_id)
            .filter(
                HansardSession.is_container == False,
                HansardContribution.party.in_(contrib_codes),
            )
        )
        policy_rows = (
            db.session.query(
                HansardSessionTheme.theme,
                func.count(HansardSessionTheme.session_id).label("n"),
            )
            .filter(
                HansardSessionTheme.session_id.in_(contrib_session_q),
                HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
            )
            .group_by(HansardSessionTheme.theme)
            .order_by(func.count(HansardSessionTheme.session_id).desc())
            .limit(8)
            .all()
        )
        top_policy_areas = [
            {
                "theme": row.theme,
                "slug":  slugify_theme(row.theme),
                "count": row.n,
            }
            for row in policy_rows
        ]

        # Most active voices â€” top 10 contributors by contribution count
        voice_rows = (
            db.session.query(
                HansardContribution.member_id,
                func.max(HansardContribution.member_name).label("member_name"),
                func.count(HansardContribution.id).label("n"),
            )
            .join(HansardSession, HansardSession.id == HansardContribution.session_id)
            .filter(
                HansardSession.is_container == False,
                HansardContribution.party.in_(contrib_codes),
                HansardContribution.member_id.isnot(None),
            )
            .group_by(HansardContribution.member_id)
            .order_by(func.count(HansardContribution.id).desc())
            .limit(10)
            .all()
        )
        voice_member_ids = [r.member_id for r in voice_rows]
        cached_voice_members = {
            cm.member_id: cm
            for cm in CachedMember.query.filter(
                CachedMember.member_id.in_(voice_member_ids)
            ).all()
        } if voice_member_ids else {}
        for row in voice_rows:
            cm = cached_voice_members.get(row.member_id)
            display_name = cm.name if cm else _parse_attribution(row.member_name)["name"]
            top_voices.append({
                "member_id": row.member_id,
                "name":      display_name,
                "count":     row.n,
            })

        # Recent activity â€” 20 most recent contributions
        recent_rows = (
            db.session.query(HansardContribution, HansardSession)
            .join(HansardSession, HansardSession.id == HansardContribution.session_id)
            .filter(
                HansardSession.is_container == False,
                HansardContribution.party.in_(contrib_codes),
                HansardContribution.member_name.isnot(None),
            )
            .order_by(HansardSession.date.desc(), HansardContribution.speech_order)
            .limit(20)
            .all()
        )
        for contrib, hs in recent_rows:
            attr = _parse_attribution(contrib.member_name)
            text = contrib.speech_text or ""
            recent_activity.append({
                "contribution_id":  contrib.id,
                "member_id":        contrib.member_id,
                "name":             attr["name"],
                "speech_preview":   text[:280],
                "speech_truncated": len(text) > 280,
                "session_title":    hs.title,
                "session_slug":     hs.slug,
                "human_date":       _human_date(hs.date),
                "url_date":         _url_date(hs.date),
                "debate_type_label": _DEBATE_TYPE_LABELS.get(hs.debate_type, "Proceedings"),
            })

    # Written Questions â€” via cached_member member_id lookup
    recent_pqs: list[dict] = []
    if cached_names:
        member_ids = [
            r[0] for r in
            db.session.query(CachedMember.member_id)
            .filter(CachedMember.party.in_(cached_names))
            .all()
        ]
        if member_ids:
            pq_rows = (
                HaPQ.query
                .filter(HaPQ.asking_mnis_id.in_(member_ids))
                .order_by(HaPQ.tabled_date.desc())
                .limit(10)
                .all()
            )
            recent_pqs = [
                {
                    "uin":           pq.uin,
                    "heading":       pq.heading or pq.uin,
                    "asking_member": pq.asking_member or "",
                    "answering_body":pq.answering_body or "",
                    "human_date":    _human_date(pq.tabled_date) if pq.tabled_date else "",
                    "is_answered":   pq.is_answered,
                }
                for pq in pq_rows
            ]

    policy_positions = _compute_party_policy_positions(
        party_slug=slug,
        contrib_codes=contrib_codes,
        is_government=cfg.get("is_government", False),
        manifesto_labels=cfg.get("manifesto_labels", {}),
    )

    return {
        "slug":               slug,
        "cfg":                cfg,
        "colour":             _PARTY_SLUG_COLOURS.get(slug, _DEFAULT_PARTY_COLOUR),
        "member_counts":      member_counts,
        "total_contributions":total_contributions,
        "dtype_breakdown":    dtype_breakdown,
        "monthly_data":       monthly_data,
        "top_policy_areas":   top_policy_areas,
        "top_voices":         top_voices,
        "recent_activity":    recent_activity,
        "recent_pqs":         recent_pqs,
        "policy_positions":   policy_positions,
    }


def _get_party_data(slug: str) -> dict | None:
    now = time.monotonic()
    cached = _party_data_cache.get(slug)
    if cached and (now - cached[1]) < _PARTY_CACHE_TTL:
        return cached[0]
    data = _compute_party_data(slug)
    if data is not None:
        _party_data_cache[slug] = (data, now)
    return data


@archive_bp.route("/party/<string:party_slug>")
def archive_party(party_slug: str):
    data = _get_party_data(party_slug)
    if data is None:
        abort(404)

    cfg  = data["cfg"]
    name = cfg["full_name"]

    commons_count = data["member_counts"].get("Commons", 0)
    lords_count   = data["member_counts"].get("Lords", 0)

    member_summary_parts = []
    if commons_count:
        member_summary_parts.append(f"{commons_count} MP{'s' if commons_count != 1 else ''}")
    if lords_count:
        member_summary_parts.append(f"{lords_count} Lord{'s' if lords_count != 1 else ''}")
    member_summary = " Â· ".join(member_summary_parts) if member_summary_parts else "No current members"

    return render_template(
        "hansard_archive/archive_party.html",
        party_slug      = party_slug,
        party_name      = name,
        party_desc      = cfg["description"],
        party_website   = cfg["website"],
        party_colour    = data["colour"],
        sinn_fein_note  = cfg["sinn_fein_note"],
        member_summary  = member_summary,
        commons_count   = commons_count,
        lords_count     = lords_count,
        total_contributions = data["total_contributions"],
        dtype_breakdown = data["dtype_breakdown"],
        monthly_data    = data["monthly_data"],
        top_policy_areas= data["top_policy_areas"],
        top_voices      = data["top_voices"],
        recent_activity = data["recent_activity"],
        recent_pqs      = data["recent_pqs"],
        policy_year_groups = data["policy_positions"],
        is_government   = cfg.get("is_government", False),
        og_title        = f"{name} â€” Hansard Archive â€” Westminster Brief",
        meta_desc       = (
            f"{name} parliamentary activity in the Westminster Brief Hansard Archive. "
            f"{cfg['description']}"
        ),
    )


# ---------------------------------------------------------------------------
# Stats section â€” /stats (index) + /stats/<slug> (per-theme)
# ---------------------------------------------------------------------------

# Ordered list for the /stats index table (same 23 themes as brief pages).
_STATS_THEME_ORDER: list[tuple[str, str]] = [
    ("economy",                                   "Economy"),
    ("employment-and-labour-market",              "Employment and labour market"),
    ("finance-and-taxation",                      "Finance and taxation"),
    ("government-and-public-administration",      "Government and public administration"),
    ("business-and-industry",                     "Business and industry"),
    ("education",                                 "Education"),
    ("health-and-social-care",                    "Health and social care"),
    ("housing-and-planning",                      "Housing and planning"),
    ("transport",                                 "Transport"),
    ("crime-justice-and-law",                     "Crime, justice and law"),
    ("welfare-and-social-security",               "Welfare and social security"),
    ("immigration-and-asylum",                    "Immigration and asylum"),
    ("environment-and-climate-change",            "Environment and climate change"),
    ("defence-and-national-security",             "Defence and national security"),
    ("international-affairs",                     "International affairs"),
    ("science-technology-and-innovation",         "Science, technology and innovation"),
    ("energy-and-utilities",                      "Energy and utilities"),
    ("work-and-pensions",                         "Work and pensions"),
    ("agriculture-environment-and-rural-affairs", "Agriculture, environment and rural affairs"),
    ("culture-media-and-sport",                   "Culture, media and sport"),
    ("constitutional-affairs",                    "Constitutional affairs"),
    ("foreign-affairs",                           "Foreign affairs"),
    ("parliamentary-affairs",                     "Parliamentary affairs"),
]


@stats_bp.route("")
def stats_index():
    from hansard_archive.models import HeadlineStat as _HS
    stats_by_slug = {r.theme_slug: r for r in _HS.query.all()}
    rows = [
        {"slug": slug, "name": name, "stat": stats_by_slug.get(slug)}
        for slug, name in _STATS_THEME_ORDER
    ]
    return render_template(
        "hansard_archive/stats_index.html",
        rows           = rows,
        heading        = "Cross-government statistics",
        canonical_path = "/stats",
        og_title       = "Cross-government statistics â€” Westminster Brief",
        meta_desc      = (
            "Official UK government statistics by policy theme, sourced from ONS, "
            "NHS England, and government departments. Updated weekly."
        ),
        json_ld_type   = "CollectionPage",
    )


@stats_bp.route("/<string:theme_slug>")
def stats_theme(theme_slug: str):
    valid_slugs = {slug for slug, _ in _STATS_THEME_ORDER}
    if theme_slug not in valid_slugs:
        abort(404)

    theme_name = _BRIEF_SLUG_HEADING.get(theme_slug) or theme_slug.replace("-", " ").title()

    from hansard_archive.models import HeadlineStat as _HS
    headline_stat = (
        _HS.query
        .filter_by(theme_slug=theme_slug)
        .filter(_HS.latest_value.isnot(None))
        .first()
    )

    today = date_type.today()
    upcoming = (
        UpcomingRelease.query
        .filter_by(theme_slug=theme_slug)
        .filter(UpcomingRelease.release_date >= today)
        .order_by(UpcomingRelease.release_date.asc())
        .limit(8)
        .all()
    )
    org_slugs = list(dict.fromkeys(r.organisation_slug for r in upcoming))
    govuk_calendar_url = (
        "https://www.gov.uk/search/statistics-announcements?"
        + "&".join(f"organisations[]={s}" for s in org_slugs)
        if org_slugs else ""
    )

    return render_template(
        "hansard_archive/stats_theme.html",
        heading            = theme_name,
        theme_slug         = theme_slug,
        headline_stat      = headline_stat,
        upcoming           = upcoming,
        govuk_calendar_url = govuk_calendar_url,
        canonical_path     = f"/stats/{theme_slug}",
        og_title           = f"{theme_name} statistics â€” Westminster Brief",
        meta_desc          = (
            f"Official UK statistics on {theme_name.lower()}, upcoming releases, "
            "and parliamentary activity."
        ),
        json_ld_type       = "CollectionPage",
    )


# ---------------------------------------------------------------------------
# Hansard search â€” /hansard (merged entry point replacing /archive root)
# ---------------------------------------------------------------------------

@hansard_bp2.route("")
def hansard_home():
    """
    Lightweight merged search page. No query â†’ search box + recent sessions.
    With query â†’ FTS results (top 25) + escalation block to /debates.
    /archive (root only) 301-redirects here.
    """
    q = request.args.get("q", "").strip()

    results, total, pq_results = [], 0, []
    error_msg = ""

    if q:
        try:
            if _is_postgres():
                results, total = _fts_search(q, page=1)
                pq_results = _pq_fts_search(q, limit=5)
            else:
                results, total = _ilike_search(q, page=1)
        except Exception as exc:
            error_msg = "Search is temporarily unavailable."
            print(f"[hansard_home] error: {exc}", flush=True)

    # No query: show recent sessions
    recent_sessions = []
    if not q:
        recent_rows = (
            HansardSession.query
            .filter_by(is_container=False)
            .order_by(HansardSession.date.desc())
            .limit(10)
            .all()
        )
        sids = [s.id for s in recent_rows]
        cc = _batch_load_contrib_counts(sids)
        pa, st = _batch_load_tags(sids)
        recent_sessions = _build_session_items(recent_rows, cc, pa, st)

    try:
        _sc = db.session.query(func.count(HansardSession.id)).filter(HansardSession.is_container == False).scalar() or 0
        _pqc = db.session.query(func.count(HaPQ.id)).scalar() or 0
        archive_session_count = f"{_sc:,}" if _sc else ""
        archive_pq_count = f"{(_pqc // 10000) * 10000:,}+" if _pqc else ""
    except Exception:
        archive_session_count = ""
        archive_pq_count = ""

    return render_template(
        "hansard_archive/hansard_home.html",
        q                     = q,
        results               = results,
        total                 = total,
        pq_results            = pq_results,
        recent_sessions       = recent_sessions,
        error_msg             = error_msg,
        last_ingested         = _last_ingested_label(),
        archive_start         = _archive_start_label(),
        archive_session_count = archive_session_count,
        archive_pq_count      = archive_pq_count,
        canonical_path        = "/hansard",
        og_title              = "Hansard â€” Westminster Brief",
        meta_desc             = (
            "Search UK parliamentary debates. Fast access to Hansard transcripts "
            "from Commons and Lords, with AI theme tagging."
        ),
    )


@archive_bp.route("/redirect-to-hansard")
def archive_home_redirect():
    """301: /archive â†’ /hansard (root only â€” deep links like /archive/debate/... remain unchanged)."""
    qs = request.query_string.decode()
    target = f"/hansard?{qs}" if qs else "/hansard"
    return redirect(target, 301)
