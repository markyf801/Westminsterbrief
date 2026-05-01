"""
Hansard Archive — public-facing routes (Phase 2A Week 3).

Blueprint: archive_bp, url_prefix=/archive

Routes:
  GET /archive                               search/home — recent sessions + filters
  GET /archive/debate/<date>/<slug>          session detail + full transcript
  GET /archive/debate/<date>/<slug>/word     Word export of session transcript

URL conventions (locked — do not change after indexing):
  date format: 22-july-2025  (day no leading zero, lowercase full month, 4-digit year)
  slug format: {title-slug}-{4-char-hex}
"""

import re
import time
from collections import defaultdict
from datetime import date as date_type, timedelta
from io import BytesIO
from urllib.parse import urlencode

from docx import Document
from docx.shared import Pt, RGBColor
from flask import Blueprint, abort, render_template, make_response, request
from sqlalchemy import func

from extensions import db
from hansard_archive.models import (
    HansardContribution,
    HansardSession,
    HansardSessionTheme,
    THEME_TYPE_POLICY_AREA,
    THEME_TYPE_SPECIFIC,
)

archive_bp = Blueprint("archive", __name__, url_prefix="/archive")

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
      "Name (Constituency) (Party)"  — standard Commons
      "Name (Party)"                 — standard Lords
      "The [Role] (Name)"            — ministerial proxy format
      "Name"                         — short reference form
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

    # Strip leading salutation (Mr/Mrs etc — not Lord/Baroness which are titles)
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
    """date → '27 April 2026'"""
    return f"{d.day} {_MONTH_NAMES[d.month]} {d.year}"


def _url_date(d) -> str:
    """date → '27-april-2026'"""
    return f"{d.day}-{_MONTH_NAMES[d.month].lower()}-{d.year}"


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
        ("Draft Warm Home Discount Regulations" → "Warm Home Discount Regulations")
      - Lords Bill pattern: strips '[Lords]' suffix
        ("Finance Bill [Lords]" → "Finance Bill")
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
# NOT be suppressed. Only titles that are the same event repeated — not stages
# of one legislative item — belong here.
_PROCEDURAL_TITLE_NOISE: frozenset[str] = frozenset({
    "Topical Questions",
    "Arrangement of Business",
    "Points of Order",
    "Point of Order",
    "Business of the House",
    "Engagements",
    "Speaker's Statement",
    "Speaker’s Statement",   # curly apostrophe variant
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

    Matching strategy (exact normalised title only — no fuzzy matching):
      - Exact match covers: Lords SI GC→Chamber (identical title), cross-house
        Bill stages where title is unchanged, topical recurrences.
      - 'Draft X' normalisation covers: SI draft laid before passage vs
        approved instrument (SI-specific pattern).
      - '[Lords]' normalisation covers: Lords-originated Bills whose Commons
        title drops the '[Lords]' suffix.

    Window: 365 days — Bill stages span Commons Second Reading (Oct) to Lords
    Third Reading (Mar); SIs typically resolve within days.

    Noise guard: denylist of known procedural titles that recur constantly with
    no relationship between instances (PMQs 'Engagements', 'Business of the
    House', etc.). Bills with 48+ stages are NOT noise — they're genuine related
    stages and must not be suppressed by a frequency threshold.

    Returns dict:
      items      — up to _RELATED_PANEL_MAX cards, sorted by date proximity
      total      — total related sessions found (for overflow badge)
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

    # Sort by date proximity; take display cap for batch-loading
    related.sort(key=lambda c: abs((c.date - session.date).days))
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

    return {
        "session":           session,
        "contributions":     contributions,
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
# Archive home — search and browse
# ---------------------------------------------------------------------------

_PER_PAGE       = 25
_GROUPS_PER_PAGE = 7   # groups shown per page in grouped OQ / PMQs view


def _build_oq_group_header(dtype_filter: str, date, house: str, dept: str) -> str:
    ds = _human_date(date)
    if dtype_filter == "pmqs":
        return f"{ds} — Prime Minister's Questions"
    if house == "Lords":
        return f"{ds} — Lords Oral Questions"
    if dept:
        return f"{ds} — {dept} Oral Questions"
    return f"{ds} — Oral Questions"


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


def _all_policy_areas() -> list[str]:
    """Sorted list of all policy area terms present in the corpus."""
    return sorted(
        r[0] for r in db.session.query(
            func.distinct(HansardSessionTheme.theme)
        ).filter(HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA).all()
    )


def _all_departments() -> list[str]:
    """Sorted list of departments that have attributed oral questions sessions."""
    return sorted(
        r[0] for r in db.session.query(
            func.distinct(HansardSession.department)
        ).filter(HansardSession.department.isnot(None)).all()
    )


@archive_bp.route("")
def archive_home():
    q             = request.args.get("q", "").strip()
    house_filter  = request.args.get("house", "")
    dtype_filter  = request.args.get("dtype", "")
    dept_filter   = "" if house_filter == "Lords" else request.args.get("dept", "")
    policy_filter = request.args.get("policy", "")
    date_from     = request.args.get("from", "")
    date_to       = request.args.get("to", "")
    title_only    = request.args.get("title_only") == "1"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    stmt = HansardSession.query.filter_by(is_container=False)

    if house_filter in ("Commons", "Lords"):
        stmt = stmt.filter(HansardSession.house == house_filter)

    if dtype_filter and dtype_filter in _DEBATE_TYPE_LABELS:
        stmt = stmt.filter(HansardSession.debate_type == dtype_filter)

    if dept_filter:
        stmt = stmt.filter(HansardSession.department == dept_filter)

    if policy_filter:
        policy_sub = (
            db.session.query(HansardSessionTheme.session_id)
            .filter(
                HansardSessionTheme.theme == policy_filter,
                HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
            )
            .subquery()
        )
        stmt = stmt.filter(HansardSession.id.in_(policy_sub))

    if date_from:
        try:
            stmt = stmt.filter(HansardSession.date >= date_type.fromisoformat(date_from))
        except ValueError:
            date_from = ""

    if date_to:
        try:
            stmt = stmt.filter(HansardSession.date <= date_type.fromisoformat(date_to))
        except ValueError:
            date_to = ""

    if q:
        title_match = HansardSession.title.ilike(f"%{q}%")
        if title_only:
            stmt = stmt.filter(title_match)
        else:
            theme_sub = (
                db.session.query(HansardSessionTheme.session_id)
                .filter(HansardSessionTheme.theme.ilike(f"%{q}%"))
                .subquery()
            )
            # Speech-text search: find sessions where any contribution contains
            # the query. This is the primary match path for policy-area queries
            # where the topic appears in debate text but not in the session title
            # (e.g. "Lifelong Learning" debated inside "Post-16 Education").
            contrib_sub = (
                db.session.query(HansardContribution.session_id)
                .filter(HansardContribution.speech_text.ilike(f"%{q}%"))
                .subquery()
            )
            stmt = stmt.filter(
                db.or_(
                    title_match,
                    HansardSession.id.in_(theme_sub),
                    HansardSession.id.in_(contrib_sub),
                )
            )

    # Build base query string and flags (needed in both grouped and flat paths)
    filter_params = {k: v for k, v in {
        "q":          q,
        "house":      house_filter,
        "dtype":      dtype_filter,
        "dept":       dept_filter,
        "policy":     policy_filter,
        "from":       date_from,
        "to":         date_to,
        "title_only": "1" if title_only else "",
    }.items() if v}
    base_qs    = urlencode(filter_params)
    has_filters = bool(q or house_filter or dtype_filter or dept_filter or policy_filter or date_from or date_to)

    # --- Grouped view: oral_questions or pmqs without a search query ---
    grouped = dtype_filter in ("oral_questions", "pmqs") and not q

    if grouped:
        # Lightweight pass: fetch only grouping fields for all matching sessions
        lite_rows = (
            stmt
            .with_entities(
                HansardSession.id,
                HansardSession.date,
                HansardSession.house,
                HansardSession.department,
            )
            .order_by(HansardSession.date.desc(), HansardSession.id)
            .all()
        )

        groups_dict: dict[tuple, list[int]] = defaultdict(list)
        for sid, row_date, row_house, row_dept in lite_rows:
            key = (row_date, row_house, row_dept or "")
            groups_dict[key].append(sid)

        # Sort groups: newest date first; within same date Commons before Lords
        sorted_keys = sorted(
            groups_dict,
            key=lambda k: (-k[0].toordinal(), 0 if k[1] == "Commons" else 1),
        )
        total_groups   = len(sorted_keys)
        total_sessions = sum(len(v) for v in groups_dict.values())
        total_pages    = max(1, (total_groups + _GROUPS_PER_PAGE - 1) // _GROUPS_PER_PAGE)
        page_keys      = sorted_keys[(page - 1) * _GROUPS_PER_PAGE : page * _GROUPS_PER_PAGE]

        page_session_ids = [sid for key in page_keys for sid in groups_dict[key]]

        sessions_full  = (
            HansardSession.query.filter(HansardSession.id.in_(page_session_ids)).all()
            if page_session_ids else []
        )
        sessions_by_id = {s.id: s for s in sessions_full}

        g_contrib_counts: dict[int, int] = {}
        if page_session_ids:
            g_contrib_counts = dict(
                db.session.query(
                    HansardContribution.session_id,
                    func.count(HansardContribution.id),
                )
                .filter(
                    HansardContribution.session_id.in_(page_session_ids),
                    HansardContribution.member_name.isnot(None),
                )
                .group_by(HansardContribution.session_id)
                .all()
            )

        g_policy_areas:    dict[int, list[str]] = defaultdict(list)
        g_specific_topics: dict[int, list[str]] = defaultdict(list)
        if page_session_ids:
            for t in (
                db.session.query(HansardSessionTheme)
                .filter(HansardSessionTheme.session_id.in_(page_session_ids))
                .all()
            ):
                if t.theme_type == THEME_TYPE_POLICY_AREA:
                    g_policy_areas[t.session_id].append(t.theme)
                elif t.theme_type == THEME_TYPE_SPECIFIC:
                    g_specific_topics[t.session_id].append(t.theme)

        def _make_item(s):
            return {
                "session":           s,
                "human_date":        _human_date(s.date),
                "url_date":          _url_date(s.date),
                "debate_type_label": _DEBATE_TYPE_LABELS.get(s.debate_type, "Proceedings"),
                "contrib_count":     g_contrib_counts.get(s.id, 0),
                "policy_areas":      sorted(g_policy_areas.get(s.id, [])),
                "specific_topics":   sorted(g_specific_topics.get(s.id, [])),
                "department":        s.department or "",
            }

        group_items = []
        for key in page_keys:
            row_date, row_house, row_dept = key
            sids = sorted(groups_dict[key])  # session ID order = Hansard chain order
            group_sessions = [sessions_by_id[sid] for sid in sids if sid in sessions_by_id]
            group_items.append({
                "date":          row_date,
                "house":         row_house,
                "dept":          row_dept,
                "header":        _build_oq_group_header(dtype_filter, row_date, row_house, row_dept),
                "session_count": len(sids),
                "sessions":      [_make_item(s) for s in group_sessions],
            })

        return render_template(
            "hansard_archive/archive_home.html",
            grouped=True,
            group_items=group_items,
            total_groups=total_groups,
            total_sessions=total_sessions,
            session_items=[],
            total=total_groups,
            total_pages=total_pages,
            per_page=_GROUPS_PER_PAGE,
            page=page,
            q=q,
            house_filter=house_filter,
            dtype_filter=dtype_filter,
            dept_filter=dept_filter,
            policy_filter=policy_filter,
            date_from=date_from,
            date_to=date_to,
            title_only=title_only,
            all_policy_areas=_all_policy_areas(),
            all_departments=_all_departments(),
            debate_type_labels=_DEBATE_TYPE_LABELS,
            base_qs=base_qs,
            has_filters=has_filters,
            last_ingested=_last_ingested_label(),
            today_str=date_type.today().isoformat(),
        )

    # --- Flat list (default) ---
    stmt = stmt.order_by(HansardSession.date.desc())

    total       = stmt.count()
    sessions    = stmt.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()
    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    # Batch-load contrib counts and policy tags for result set (avoids N+1)
    session_ids = [s.id for s in sessions]

    contrib_counts: dict[int, int] = {}
    if session_ids:
        contrib_counts = dict(
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

    session_policy_areas: dict[int, list[str]] = defaultdict(list)
    session_specific_topics: dict[int, list[str]] = defaultdict(list)
    if session_ids:
        for t in (
            db.session.query(HansardSessionTheme)
            .filter(HansardSessionTheme.session_id.in_(session_ids))
            .all()
        ):
            if t.theme_type == THEME_TYPE_POLICY_AREA:
                session_policy_areas[t.session_id].append(t.theme)
            elif t.theme_type == THEME_TYPE_SPECIFIC:
                session_specific_topics[t.session_id].append(t.theme)

    session_items = [
        {
            "session":           s,
            "human_date":        _human_date(s.date),
            "url_date":          _url_date(s.date),
            "debate_type_label": _DEBATE_TYPE_LABELS.get(s.debate_type, "Proceedings"),
            "contrib_count":     contrib_counts.get(s.id, 0),
            "policy_areas":      sorted(session_policy_areas.get(s.id, [])),
            "specific_topics":   sorted(session_specific_topics.get(s.id, [])),
            "department":        s.department or "",
        }
        for s in sessions
    ]

    last_ingested = _last_ingested_label()
    today_str     = date_type.today().isoformat()

    return render_template(
        "hansard_archive/archive_home.html",
        grouped=False,
        group_items=[],
        session_items=session_items,
        q=q,
        house_filter=house_filter,
        dtype_filter=dtype_filter,
        dept_filter=dept_filter,
        policy_filter=policy_filter,
        date_from=date_from,
        date_to=date_to,
        title_only=title_only,
        page=page,
        total=total,
        total_pages=total_pages,
        per_page=_PER_PAGE,
        all_policy_areas=_all_policy_areas(),
        all_departments=_all_departments(),
        debate_type_labels=_DEBATE_TYPE_LABELS,
        base_qs=base_qs,
        has_filters=has_filters,
        last_ingested=last_ingested,
        today_str=today_str,
    )


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
# Word export — session transcript
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
    meta.add_run(f"{ctx['human_date']}  ·  {session.house}  ·  {ctx['debate_type_label']}").font.size = Pt(10)

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
        f"Exported from Westminster Brief — westminsterbrief.co.uk"
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
