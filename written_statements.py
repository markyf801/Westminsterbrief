"""
written_statements.py — Written ministerial statements (WMS).

/written-statements                        list, newest first: ?q= (search),
                                           ?dept=<answering body id>, ?house=commons|lords,
                                           ?from= / ?to= (YYYY-MM-DD), ?page=
/written-statements/<date>/<uin>           one statement, e.g. /written-statements/21-october-2026/hlws411
/written-statements/2026-10-21/HLWS411     301 -> the canonical form above
/sitemap-written-statements.xml            built here, routed in flask_app.py

Reads only from ha_written_statement, filled by
scripts/ingest_written_statements.py from Parliament's Written Questions and
Statements API. No Parliament API calls on page view.

Oral statements made in the chamber are Hansard sessions
(debate_type='ministerial_statement'); written statements are separate
documents and live here.

UINs (HCWS123 / HLWS456) restart each session, so a statement is identified
in URLs by date made + UIN, as on Parliament's own site.

Policy areas are deterministic (no AI): the answering department mapped to
GOV.UK organisation slugs, then consultations.assign_policy_areas() (department
+ title/opening-text keywords). See policy_areas_for().

Behind FEATURE_WRITTEN_STATEMENTS (hidden / beta / live, see feature_flags.py)
so the code can ship before migrations/written_statement_tables.py and the
first backfill have run.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlencode

from flask import Blueprint, abort, redirect, render_template, request
from flask_login import current_user
from sqlalchemy import func, or_, text

from extensions import db, limiter
from feature_flags import feature_enabled
from hansard_archive import policy_areas as _pa

written_statements_bp = Blueprint("written_statements", __name__)

FLAG = "FEATURE_WRITTEN_STATEMENTS"
BASE_URL = "https://westminsterbrief.co.uk"
OFFICIAL_URL = "https://questions-statements.parliament.uk/written-statements/detail/{date}/{uin}"
MEMBER_URL = "https://members.parliament.uk/member/{id}"

HOUSES = {"commons": "Commons", "lords": "Lords"}

_PER_PAGE = 20
_MAX_PAGES = 50
_MAX_Q_LEN = 100
_EXCERPT_CHARS = 240
_AREA_TEXT_CHARS = 600      # opening text used for keyword matching (title counts double)

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


# ── Model ─────────────────────────────────────────────────────────────────────
# Production table comes from migrations/written_statement_tables.py (the web
# service runs with SKIP_MIGRATIONS=1). Declared here so SQLite create_all()
# works locally and in tests. Postgres also gets a generated search_tsv column
# + GIN index from the migration, deliberately not declared here.

class WrittenStatement(db.Model):
    """
    One written ministerial statement. id is Parliament's statement id (stable).
    uin restarts each session, so (date_made, uin) identifies it in URLs.
    A statement made in both Houses has two rows (one per House), linked by
    linked_statements (link_type 'JointStatement').

    text_html is Parliament's HTML sanitised with the written-answer allowlist
    (hansard_archive/html_sanitizer.py); text_plain is the same as plain text.
    updated_at only moves when the content we show changes (sitemap lastmod).
    """

    __tablename__ = "ha_written_statement"
    __table_args__ = (
        db.Index("idx_ha_ws_date_made", "date_made"),
        db.Index("idx_ha_ws_date_uin", "date_made", "uin"),
        db.Index("idx_ha_ws_answering_body", "answering_body_id"),
        db.Index("idx_ha_ws_house", "house"),
        db.Index("idx_ha_ws_member", "member_id"),
    )

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=False)
    uin                 = db.Column(db.String(20), nullable=False)
    date_made           = db.Column(db.Date, nullable=False)
    house               = db.Column(db.String(10), nullable=False)        # Commons / Lords
    answering_body_id   = db.Column(db.Integer, nullable=True)
    answering_body_name = db.Column(db.Text, nullable=True)
    member_id           = db.Column(db.Integer, nullable=True)            # MNIS id of the minister
    member_name         = db.Column(db.Text, nullable=True)
    member_party        = db.Column(db.Text, nullable=True)
    member_role         = db.Column(db.Text, nullable=True)
    title               = db.Column(db.Text, nullable=False)
    text_html           = db.Column(db.Text, nullable=True)
    text_plain          = db.Column(db.Text, nullable=True)
    notice_number       = db.Column(db.Integer, nullable=True)
    attachments         = db.Column(db.JSON, nullable=True)   # [{url, title, file_type, file_size}]
    linked_statements   = db.Column(db.JSON, nullable=True)   # [{id, link_type, link_date}]
    policy_areas        = db.Column(db.JSON, nullable=True)   # [slug] ranked
    raw_data            = db.Column(db.JSON, nullable=True)   # API payload minus text
    ingested_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<WrittenStatement {self.id} {self.uin} {self.date_made}>"


# ── Policy areas (deterministic) ──────────────────────────────────────────────

# Parliament answering-body id -> GOV.UK organisation slug, so the department
# mapping in consultations.py (_ORG_AREAS) is reused rather than duplicated.
# Ids from members-api /api/Reference/AnsweringBodies (checked 2026-10-22).
# Bodies with no single policy area (Prime Minister, Leader of the House,
# territorial offices) are left out: keywords only.
_BODY_ORG_SLUGS: dict[int, str] = {
    88: "attorney-generals-office",
    214: "department-for-business-and-trade",
    220: "department-for-business-innovation-science-and-trade",
    53: "cabinet-office",
    205: "cabinet-office",
    209: "cabinet-office",
    217: "department-for-culture-media-and-sport",
    10: "department-for-digital-culture-media-and-sport",
    11: "ministry-of-defence",
    60: "department-for-education",
    215: "department-for-energy-security-and-net-zero",
    13: "department-for-environment-food-rural-affairs",
    208: "foreign-commonwealth-development-office",
    17: "department-of-health-and-social-care",
    1: "home-office",
    7: "ministry-of-housing-communities-local-government",
    211: "ministry-of-housing-communities-local-government",
    54: "ministry-of-justice",
    216: "department-for-science-innovation-and-technology",
    27: "department-for-transport",
    14: "hm-treasury",
    29: "department-for-work-pensions",
}


def policy_areas_for(title: str, text_plain: str, answering_body_id: int | None) -> list[str]:
    """Ranked policy-area slugs for one statement (at most 3)."""
    from consultations import assign_policy_areas

    org = _BODY_ORG_SLUGS.get(answering_body_id or 0)
    orgs = [{"slug": org, "parents": []}] if org else []
    opening = " ".join((text_plain or "").split())[:_AREA_TEXT_CHARS]
    return [a["slug"] for a in assign_policy_areas(title or "", opening, None, orgs)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def written_statements_enabled() -> bool:
    return feature_enabled(FLAG, current_user)


def url_date(d: date) -> str:
    """date -> '21-october-2026' (site URL date format)."""
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"


def parse_url_date(value: str) -> date | None:
    parts = (value or "").lower().split("-")
    if len(parts) != 3 or parts[1] not in _MONTHS:
        return None
    try:
        return date(int(parts[2]), _MONTHS.index(parts[1]) + 1, int(parts[0]))
    except ValueError:
        return None


def statement_path(ws) -> str:
    return f"/written-statements/{url_date(ws.date_made)}/{ws.uin.lower()}"


def official_url(ws) -> str:
    return OFFICIAL_URL.format(date=ws.date_made.isoformat(), uin=ws.uin)


def _human_date(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _excerpt(text_: str, limit: int = _EXCERPT_CHARS) -> str:
    text_ = " ".join((text_ or "").split())
    if len(text_) <= limit:
        return text_
    return text_[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _is_postgres() -> bool:
    return db.engine.dialect.name == "postgresql"


def _area_links(slugs: list | None) -> list[dict]:
    out = []
    for slug in slugs or []:
        area = _pa.by_slug(slug)
        if area:
            out.append({"slug": slug, "label": area.label})
    return out


def _file_size(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def _search_filter(query, q: str):
    if _is_postgres():
        # search_tsv: generated column + GIN index from migrations/written_statement_tables.py.
        # websearch_to_tsquery never raises on user input (quotes, OR, -word).
        return query.filter(
            text("ha_written_statement.search_tsv @@ websearch_to_tsquery('english', :ws_q)")
            .bindparams(ws_q=q)
        )
    like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    return query.filter(or_(WrittenStatement.title.ilike(like, escape="\\"),
                            WrittenStatement.text_plain.ilike(like, escape="\\")))


def _date_arg(name: str) -> date | None:
    try:
        return date.fromisoformat((request.args.get(name) or "").strip()[:10])
    except ValueError:
        return None


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        return 1


def _list_item(ws: WrittenStatement) -> dict:
    return {
        "title": ws.title,
        "uin": ws.uin,
        "house": ws.house,
        "path": statement_path(ws),
        "human_date": _human_date(ws.date_made),
        "department": ws.answering_body_name or "",
        "minister": ws.member_name or "",
        "excerpt": _excerpt(ws.text_plain or ""),
        "has_attachments": bool(ws.attachments),
    }


# ── Debate Prep helper ────────────────────────────────────────────────────────

def has_statements() -> bool:
    """True once the table exists and holds at least one row. Never raises."""
    try:
        return db.session.query(WrittenStatement.id).limit(1).first() is not None
    except Exception as exc:   # table not created yet (migration pending)
        db.session.rollback()
        print(f"[written-statements] table check failed: {type(exc).__name__}: {exc}", flush=True)
        return False


_WORD_RE = re.compile(r"[a-z0-9]+")


def candidates_for_words(words: list[str], from_date: date | None, to_date: date | None,
                         limit: int = 300) -> list[WrittenStatement]:
    """
    Up to `limit` statements in the date range containing ANY of the words.
    Callers post-filter for relevance and sort. Postgres: full-text OR query,
    best ts_rank first; SQLite (tests/local): LIKE, newest first.
    """
    words = [w for w in dict.fromkeys(_WORD_RE.findall(" ".join(words).lower())) if len(w) > 2][:12]
    if not words:
        return []
    query = WrittenStatement.query
    if from_date:
        query = query.filter(WrittenStatement.date_made >= from_date)
    if to_date:
        query = query.filter(WrittenStatement.date_made <= to_date)
    if _is_postgres():
        tsq = " | ".join(f"{w}:*" for w in words)     # words are [a-z0-9] only
        query = (query
                 .filter(text("ha_written_statement.search_tsv @@ to_tsquery('english', :ws_any)")
                         .bindparams(ws_any=tsq))
                 .order_by(text("ts_rank(ha_written_statement.search_tsv, "
                                "to_tsquery('english', :ws_rank)) DESC").bindparams(ws_rank=tsq)))
    else:
        query = query.filter(or_(*[
            or_(WrittenStatement.title.ilike(f"%{w}%"), WrittenStatement.text_plain.ilike(f"%{w}%"))
            for w in words
        ]))
    return (query.order_by(WrittenStatement.date_made.desc(), WrittenStatement.id.desc())
            .limit(limit).all())


def prep_row(ws: WrittenStatement, link_to_site: bool) -> dict:
    """A statement in the row shape Debate Prep / the Research Tool use for WMS."""
    body = ws.text_plain or ""
    return {
        "body_clean": body,
        "body_export": body[:2000],
        "body_word_count": len(body.split()),
        "speaker_name": ws.member_name or "",
        "speaker_party": ws.member_party or "",
        "hdate": ws.date_made.isoformat(),
        "debate_title": ws.title,
        "source": "wms",
        "source_label": "Written Ministerial Statement",
        "relevance": 0,
        "debate_type": "📜 Ministerial Statement",
        "listurl": statement_path(ws) if link_to_site else official_url(ws),
        "dept": ws.answering_body_name or "",
        "is_minister": True,
        "debate_section_ext_id": "",
        "member_role": ws.member_role or "",
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@written_statements_bp.route("/written-statements")
@limiter.limit("120 per minute")
def statement_list():
    if not written_statements_enabled():
        abort(404)
    q = " ".join((request.args.get("q") or "").split())[:_MAX_Q_LEN]
    dept = request.args.get("dept", type=int)
    house = (request.args.get("house") or "").strip().lower()
    if house not in HOUSES:
        house = ""
    date_from = _date_arg("from")
    date_to = _date_arg("to")
    page = _page_arg()
    if page > _MAX_PAGES:
        abort(404)

    base = WrittenStatement.query
    if q:
        base = _search_filter(base, q)
    if house:
        base = base.filter(WrittenStatement.house == HOUSES[house])
    if date_from:
        base = base.filter(WrittenStatement.date_made >= date_from)
    if date_to:
        base = base.filter(WrittenStatement.date_made <= date_to)

    # Department options reflect the other filters, not the department itself.
    dept_rows = (base.with_entities(WrittenStatement.answering_body_id,
                                    func.max(WrittenStatement.answering_body_name),
                                    func.count(WrittenStatement.id))
                 .filter(WrittenStatement.answering_body_id.isnot(None))
                 .group_by(WrittenStatement.answering_body_id)
                 .all())
    departments = sorted(({"id": i, "name": n or f"Department {i}", "count": c} for i, n, c in dept_rows),
                         key=lambda d: d["name"].lower())

    query = base.filter(WrittenStatement.answering_body_id == dept) if dept else base
    total = query.count()
    total_pages = min(_MAX_PAGES, max(1, (total + _PER_PAGE - 1) // _PER_PAGE))
    if page > total_pages:
        abort(404)
    rows = (query.order_by(WrittenStatement.date_made.desc(), WrittenStatement.id.desc())
            .offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all())

    def page_url(n: int) -> str:
        args = {}
        if q:
            args["q"] = q
        if dept:
            args["dept"] = dept
        if house:
            args["house"] = house
        if date_from:
            args["from"] = date_from.isoformat()
        if date_to:
            args["to"] = date_to.isoformat()
        if n > 1:
            args["page"] = n
        return "/written-statements" + ("?" + urlencode(args) if args else "")

    dept_name = next((d["name"] for d in departments if d["id"] == dept), "")
    filtered = bool(q or dept or house or date_from or date_to)
    meta_desc = (
        "Written ministerial statements to the House of Commons and House of Lords, newest "
        "first: what the government announced, which minister made it and the full text."
    )
    return render_template(
        "written_statements/ws_list.html",
        items=[_list_item(ws) for ws in rows],
        total=total,
        q=q,
        dept=dept,
        dept_name=dept_name,
        departments=departments,
        house=house,
        houses=HOUSES,
        date_from=date_from.isoformat() if date_from else "",
        date_to=date_to.isoformat() if date_to else "",
        page=page,
        total_pages=total_pages,
        per_page=_PER_PAGE,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < total_pages else "",
        meta_desc=meta_desc,
        filtered=filtered,
        robots_meta="noindex, follow" if filtered or page > 1 else "index, follow",
    )


def _find(d: date, uin: str) -> WrittenStatement | None:
    return (WrittenStatement.query
            .filter(WrittenStatement.date_made == d,
                    func.lower(WrittenStatement.uin) == uin.lower())
            .order_by(WrittenStatement.id.desc()).first())


@written_statements_bp.route("/written-statements/<string:date_str>/<string:uin>")
@limiter.limit("120 per minute")
def statement_detail(date_str: str, uin: str):
    if not written_statements_enabled():
        abort(404)
    d = parse_url_date(date_str)
    if d is None:
        try:   # Parliament-style /2026-10-21/HLWS411 -> canonical
            d = date.fromisoformat(date_str)
        except ValueError:
            abort(404)
    ws = _find(d, uin[:20])
    if ws is None:
        abort(404)
    canonical = statement_path(ws)
    if f"/written-statements/{date_str}/{uin}" != canonical:
        return redirect(canonical, code=301)

    from consultations import related_items
    from hansard_archive.html_sanitizer import sanitize_answer_html

    linked = []
    for link in ws.linked_statements or []:
        if not isinstance(link, dict) or not link.get("id"):
            continue
        other = db.session.get(WrittenStatement, link["id"])
        linked.append({
            "link_type": link.get("link_type") or "",
            "path": statement_path(other) if other else "",
            "uin": other.uin if other else "",
            "house": other.house if other else "",
            "human_date": _human_date(other.date_made) if other else "",
        })
    attachments = [
        {**a, "size": _file_size(a.get("file_size"))}
        for a in (ws.attachments or [])
        if isinstance(a, dict) and str(a.get("url") or "").startswith("https://")
    ]
    areas = _area_links(ws.policy_areas)
    related = related_items(ws.policy_areas or [])

    human_date = _human_date(ws.date_made)
    who = ws.member_name or "a minister"
    if ws.answering_body_name:
        who += f", {ws.answering_body_name}"
    meta_desc = _excerpt(
        f"Written statement {ws.uin} to the House of {ws.house} by {who}, {human_date}. "
        f"{ws.text_plain or ''}",
        155,
    )
    return render_template(
        "written_statements/ws_detail.html",
        ws=ws,
        human_date=human_date,
        # Sanitised at ingest; sanitised again here so the page never trusts stored HTML.
        body_html=sanitize_answer_html(ws.text_html) or "",
        linked=linked,
        attachments=attachments,
        areas=areas,
        related=related,
        has_related=any(related.values()),
        member_url=MEMBER_URL.format(id=ws.member_id) if ws.member_id else "",
        official_link=official_url(ws),
        meta_desc=meta_desc,
        canonical_path=canonical,
        checked_date=_human_date(ws.last_refreshed),
    )


# ── Sitemap ───────────────────────────────────────────────────────────────────

def build_sitemap_xml() -> str:
    """Every statement we hold, newest first; lastmod = last content change in our copy."""
    rows = (db.session.query(WrittenStatement.date_made, WrittenStatement.uin,
                             WrittenStatement.updated_at)
            .order_by(WrittenStatement.date_made.desc(), WrittenStatement.id.desc()).all())
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for made, uin, updated in rows:
        lines.append('  <url>')
        lines.append(f'    <loc>{BASE_URL}/written-statements/{url_date(made)}/{uin.lower()}</loc>')
        if updated:
            lines.append(f'    <lastmod>{updated.date().isoformat()}</lastmod>')
        lines.append('  </url>')
    lines.append('</urlset>')
    return '\n'.join(lines)
