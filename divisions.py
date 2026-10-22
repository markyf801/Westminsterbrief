"""
divisions.py — recorded votes (divisions) in the Commons and Lords.

/divisions                       list: ?house=, ?from= / ?to= (YYYY-MM-DD), ?q= (titles),
                                 ?area=<policy slug>, ?member=<MNIS id>, pagination
/divisions/<house>/<id>          one division: result, counts, members by vote and party
/sitemap-divisions.xml           built here, routed in flask_app.py
divisions_member_votes(id)       Jinja global: recent votes for the member profile page

Reads only from ha_division / ha_division_vote, filled by
scripts/ingest_divisions.py. No Parliament API calls on page view.

Behind FEATURE_DIVISIONS (hidden / beta / live, see feature_flags.py) so the
code can ship before migrations/division_tables.py and the first backfill
have run.

Wording: these pages state how members voted and link to the official record.
They do not describe anyone as supporting or opposing a cause.
"""
import logging
from datetime import date
from urllib.parse import urlencode

from flask import Blueprint, abort, redirect, render_template, request
from flask_login import current_user
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from divisions_models import HOUSES, VOTE_AYE, VOTE_LABELS, VOTE_NO, HaDivision, HaDivisionVote
from extensions import db, limiter
from feature_flags import feature_enabled
from hansard_archive import policy_areas as _pa
from hansard_archive.models import THEME_TYPE_POLICY_AREA, HansardSession, HansardSessionTheme

log = logging.getLogger(__name__)

divisions_bp = Blueprint("divisions", __name__)

FLAG = "FEATURE_DIVISIONS"
BASE_URL = "https://westminsterbrief.co.uk"
MEMBER_CONTACT_URL = "https://members.parliament.uk/member/{id}/contact"

_PER_PAGE = 25
_MAX_PAGES = 50
_MAX_Q_LEN = 100
_MEMBER_VOTES = 10

HOUSE_SLUGS = {h.lower(): h for h in HOUSES}

# Plural lobby names, as each House reports a result.
LOBBY_NAMES = {
    "Commons": {VOTE_AYE: "Ayes", VOTE_NO: "Noes"},
    "Lords": {VOTE_AYE: "Contents", VOTE_NO: "Not contents"},
}

_MONTHS = ["", "january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]

# ha_session_theme.theme (policy_area) -> PolicyArea, for /brief/<slug> links.
_AREA_BY_HANSARD_NAME = {name: pa for pa in _pa.all_areas() for name in pa.hansard_names}


def divisions_enabled() -> bool:
    return feature_enabled(FLAG, current_user)


def _human_date(d) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _url_date(d) -> str:
    return f"{d.day}-{_MONTHS[d.month]}-{d.year}"


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


def vote_label(house: str, vote: str) -> str:
    return VOTE_LABELS.get(house, VOTE_LABELS["Commons"]).get(vote, vote)


def result_summary(div: HaDivision) -> str:
    """Neutral one-line result from the counts, e.g. 'Noes 279, Ayes 57: majority of 222 for the Noes'."""
    names = LOBBY_NAMES.get(div.house, LOBBY_NAMES["Commons"])
    ayes, noes = div.ayes_count or 0, div.noes_count or 0
    if ayes == noes:
        return f"{names[VOTE_AYE]} {ayes}, {names[VOTE_NO]} {noes}: a tie — see the official record for the outcome"
    winner = VOTE_AYE if ayes > noes else VOTE_NO
    return f"{names[VOTE_AYE]} {ayes}, {names[VOTE_NO]} {noes}: majority of {abs(ayes - noes)} for the {names[winner]}"


def areas_for_sessions(session_ids: list[int]) -> dict[int, list[dict]]:
    """{ha_session.id: [{slug, label}]} from the linked debate's policy-area tags."""
    ids = [i for i in session_ids if i]
    if not ids:
        return {}
    rows = (db.session.query(HansardSessionTheme.session_id, HansardSessionTheme.theme)
            .filter(HansardSessionTheme.session_id.in_(ids),
                    HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA)
            .all())
    out: dict[int, list[dict]] = {}
    for sid, theme in rows:
        pa = _AREA_BY_HANSARD_NAME.get(theme)
        if pa is None:
            continue
        items = out.setdefault(sid, [])
        if all(t["slug"] != pa.slug for t in items):
            items.append({"slug": pa.slug, "label": pa.label})
    return out


def _list_item(div: HaDivision, areas: list[dict], member_vote=None) -> dict:
    item = {
        "path": div.path,
        "title": div.title,
        "house": div.house,
        "number": div.number,
        "human_date": _human_date(div.date),
        "is_deferred": div.is_deferred,
        "result": result_summary(div),
        "areas": areas,
        "member_vote": "",
    }
    if member_vote is not None:
        item["member_vote"] = vote_label(div.house, member_vote.vote) + (" (teller)" if member_vote.is_teller else "")
    return item


# ── Routes ────────────────────────────────────────────────────────────────────

@divisions_bp.route("/divisions")
@limiter.limit("120 per minute")
def division_list():
    if not divisions_enabled():
        abort(404)
    q = " ".join((request.args.get("q") or "").split())[:_MAX_Q_LEN]
    house = HOUSE_SLUGS.get((request.args.get("house") or "").lower(), "")
    date_from, date_to = _date_arg("from"), _date_arg("to")
    area_slug = request.args.get("area", "")
    area = _pa.by_slug(area_slug)
    member_id = request.args.get("member", type=int)
    page = _page_arg()
    if page > _MAX_PAGES:
        abort(404)

    query = HaDivision.query
    if house:
        query = query.filter(HaDivision.house == house)
    if date_from:
        query = query.filter(HaDivision.date >= date_from)
    if date_to:
        query = query.filter(HaDivision.date <= date_to)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        query = query.filter(HaDivision.title.ilike(like, escape="\\"))
    if area:
        query = query.filter(HaDivision.ha_session_id.in_(
            select(HansardSessionTheme.session_id).where(
                HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA,
                HansardSessionTheme.theme.in_(area.hansard_names),
            )
        ))
    member_name = ""
    if member_id:
        latest = (db.session.query(HaDivisionVote.member_name)
                  .join(HaDivision, HaDivision.id == HaDivisionVote.division_id)
                  .filter(HaDivisionVote.member_id == member_id)
                  .order_by(HaDivision.date.desc()).first())
        member_name = latest[0] if latest else ""
        query = query.filter(HaDivision.id.in_(
            select(HaDivisionVote.division_id).where(HaDivisionVote.member_id == member_id)))

    total = query.count()
    total_pages = min(_MAX_PAGES, max(1, (total + _PER_PAGE - 1) // _PER_PAGE))
    if page > total_pages:
        abort(404)
    rows = (query.order_by(HaDivision.date.desc(), HaDivision.house, HaDivision.number.desc(),
                           HaDivision.id.desc())
            .offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all())

    areas = areas_for_sessions([d.ha_session_id for d in rows])
    their_votes: dict[int, HaDivisionVote] = {}
    if member_id and rows:
        for v in HaDivisionVote.query.filter(HaDivisionVote.member_id == member_id,
                                             HaDivisionVote.division_id.in_([d.id for d in rows])).all():
            their_votes.setdefault(v.division_id, v)
    items = [_list_item(d, areas.get(d.ha_session_id, []), their_votes.get(d.id) if member_id else None)
             for d in rows]

    def page_url(n: int) -> str:
        args = {}
        if q:
            args["q"] = q
        if house:
            args["house"] = house.lower()
        if date_from:
            args["from"] = date_from.isoformat()
        if date_to:
            args["to"] = date_to.isoformat()
        if area:
            args["area"] = area.slug
        if member_id:
            args["member"] = member_id
        if n > 1:
            args["page"] = n
        return "/divisions" + ("?" + urlencode(args) if args else "")

    filtered = bool(q or house or date_from or date_to or area or member_id)
    meta_desc = (
        "Recorded votes (divisions) in the House of Commons and House of Lords: the question, "
        "the result, and how each MP and peer voted, with links to the official record."
    )
    return render_template(
        "divisions/division_list.html",
        items=items,
        total=total,
        q=q,
        house=house,
        houses=HOUSES,
        date_from=date_from.isoformat() if date_from else "",
        date_to=date_to.isoformat() if date_to else "",
        area=area,
        areas=_pa.all_areas(),
        member_id=member_id,
        member_name=member_name,
        page=page,
        total_pages=total_pages,
        per_page=_PER_PAGE,
        prev_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page < total_pages else "",
        meta_desc=meta_desc,
        filtered=filtered,
        robots_meta="noindex, follow" if filtered or page > 1 else "index, follow",
    )


@divisions_bp.route("/divisions/<string:house_slug>/<int:division_id>")
@limiter.limit("120 per minute")
def division_detail(house_slug: str, division_id: int):
    if not divisions_enabled():
        abort(404)
    house = HOUSE_SLUGS.get(house_slug.lower())
    if house is None:
        abort(404)
    div = HaDivision.query.filter_by(house=house, parliament_division_id=division_id).first()
    if div is None:
        abort(404)
    if house_slug != div.house_slug:
        return redirect(div.path, code=301)

    votes = HaDivisionVote.query.filter(HaDivisionVote.division_id == div.id).all()
    lobbies = []
    for vote in (VOTE_AYE, VOTE_NO):
        members = sorted((v for v in votes if v.vote == vote),
                         key=lambda v: ((v.list_as or v.member_name).lower(), v.member_id))
        by_party: dict[str, list] = {}
        for v in members:
            by_party.setdefault(v.party or "Unknown", []).append({
                "member_id": v.member_id,
                "name": v.member_name,
                "member_from": v.member_from or "",
                "is_teller": v.is_teller,
                "proxy_name": v.proxy_name or "",
            })
        lobbies.append({
            "vote": vote,
            "label": LOBBY_NAMES[house][vote],
            "official_count": div.ayes_count if vote == VOTE_AYE else div.noes_count,
            "listed": len(members),
            "parties": sorted(by_party.items(), key=lambda kv: (-len(kv[1]), kv[0])),
        })

    session_row = db.session.get(HansardSession, div.ha_session_id) if div.ha_session_id else None
    debate = None
    if session_row is not None:
        debate = {
            "title": session_row.title,
            "path": f"/archive/debate/{_url_date(session_row.date)}/{session_row.slug}" if session_row.slug else "",
            "hansard_url": session_row.hansard_url or "",
        }
    areas = areas_for_sessions([div.ha_session_id]).get(div.ha_session_id, [])

    human_date = _human_date(div.date)
    result = result_summary(div)
    meta_desc = (f"House of {house} division {div.number or ''} on {human_date}: {div.title}. "
                 f"{result}. How each member voted, with a link to the official record.")[:300]
    return render_template(
        "divisions/division_detail.html",
        div=div,
        human_date=human_date,
        result=result,
        lobbies=lobbies,
        teller_note=("In the Commons the totals do not include the tellers."
                     if house == "Commons" else "In the Lords the totals include the tellers."),
        debate=debate,
        areas=areas,
        checked_date=_human_date(div.last_refreshed),
        canonical_path=div.path,
        meta_desc=meta_desc,
    )


# ── Member profile: recent votes ──────────────────────────────────────────────

@divisions_bp.app_template_global("divisions_member_votes")
def member_votes(member_id: int, limit: int = _MEMBER_VOTES) -> dict | None:
    """
    Recent votes for /members/<id>/<slug>, or None when the feature is off.
    {"total": int, "rows": [...], "all_url": str} or {"error": True}.
    """
    if not member_id or not divisions_enabled():
        return None
    try:
        total = (db.session.query(func.count(func.distinct(HaDivisionVote.division_id)))
                 .filter(HaDivisionVote.member_id == member_id).scalar() or 0)
        rows = (db.session.query(HaDivisionVote, HaDivision)
                .join(HaDivision, HaDivision.id == HaDivisionVote.division_id)
                .filter(HaDivisionVote.member_id == member_id)
                .order_by(HaDivision.date.desc(), HaDivision.number.desc(), HaDivision.id.desc())
                .limit(limit).all())
    except SQLAlchemyError as exc:
        db.session.rollback()
        log.warning("member votes for %s failed: %s", member_id, exc)
        return {"error": True, "total": 0, "rows": [], "all_url": ""}
    return {
        "total": total,
        "rows": [{
            "path": d.path,
            "title": d.title,
            "human_date": _human_date(d.date),
            "vote": vote_label(d.house, v.vote) + (" (teller)" if v.is_teller else ""),
            "result": result_summary(d),
        } for v, d in rows],
        "all_url": f"/divisions?member={member_id}",
    }


# ── Sitemap ───────────────────────────────────────────────────────────────────

def build_sitemap_xml() -> str:
    """Every division, lastmod = last change in our copy."""
    rows = (db.session.query(HaDivision.house, HaDivision.parliament_division_id, HaDivision.updated_at)
            .order_by(HaDivision.house, HaDivision.parliament_division_id).all())
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for house, pid, updated in rows:
        lines.append('  <url>')
        lines.append(f'    <loc>{BASE_URL}/divisions/{house.lower()}/{pid}</loc>')
        if updated:
            lines.append(f'    <lastmod>{updated.date().isoformat()}</lastmod>')
        lines.append('  </url>')
    lines.append('</urlset>')
    return '\n'.join(lines)
