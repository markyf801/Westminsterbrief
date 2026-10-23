"""
ministers.py — current government ministers, read from our own gov_minister table.

The table is rebuilt daily by scripts/refresh_ministers.py (Parliament Members
API government posts merged with GOV.UK's ministers page). Pages read it here;
nothing in this module calls an outside API.

- load_minister_list()  the dict the Research Tool has always used
                        ({by_norm, by_id, by_dept, member_ids}), or None while
                        the table is empty (callers then fall back to the old
                        file-cached GOV.UK fetch, so deploy order doesn't matter)
- post_on_date()        the post a member held on a given date, from the dated
                        history in member_profile (Members API Biography, weekly)
                        plus gov_minister; None when our records don't show one
- pq_answering_role()   "Minister of State (Education)" for a PQ's answering
                        minister: the post they held in the answering department
                        on the answer date
- table_status()        counts for /admin

Rows are cached per process for CACHE_TTL seconds, so a page that checks many
speakers makes one small query.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # seconds

# Parliament departments that hold whips and Household posts. Their holders are
# still flagged as government speakers (Lords whips answer for departments), but
# they are not offered as departments in the Research Tool pickers. "Prime
# Minister" is left out because the PM is also listed under the Cabinet Office.
NON_PICKER_DEPARTMENTS = frozenset({
    "Whips Office (Commons)", "Whips Office (Lords)", "HM Household", "Prime Minister",
})
WHIP_DEPARTMENTS = NON_PICKER_DEPARTMENTS - {"Prime Minister"}

_cache: dict = {"at": 0.0, "rows": None}
_lock = threading.Lock()


# ── Names ─────────────────────────────────────────────────────────────────────

def display_name(title: str) -> str:
    """Strip honorifics/post-nominals from a GOV.UK or Parliament name for display.
    Preserves Lords titles (Baroness/Lord) so name lookups can find peers.
    'The Rt Hon Baroness Smith of Malvern' → 'Baroness Smith of Malvern'
    'The Rt Hon Josh MacAlister MP' → 'Josh MacAlister'"""
    name = (title or "").strip()
    for prefix in ['The Rt Hon ', 'The Right Hon ', 'Rt Hon ', 'Right Hon ', 'The ']:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for prefix in ['Dame ', 'Sir ', 'Dr ', 'Mr ', 'Mrs ', 'Ms ', 'Miss ']:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = re.sub(r'\s+(MP|OBE|CBE|MBE|PC|QC|KC|DBE|KBE|CMG|KCMG|GCMG|CB|KCB|GCB|CVO|KCVO|GCVO|LVO)(\b.*)?$',
                  '', name)
    return name.strip()


def normalise_name(name: str) -> str:
    """Strip honorifics, titles, and post-nominal letters for name matching.
    Works on both Hansard speaker names and GOV.UK person titles.
    Applies prefix stripping in two passes to handle 'The Rt Hon Baroness ...'."""
    n = (name or "").lower().strip()
    n = re.sub(r'\s+(obe|cbe|mbe|pc|qc|kc|dbe|kbe|mp|obe mp|cbe mp|mbe mp)\s*$', '', n)
    prefixes = ['the rt hon ', 'the right hon ', 'rt hon ', 'right hon ',
                'the baroness ', 'baroness ', 'the lord ', 'lord ',
                'dame ', 'sir ', 'dr ', 'mr ', 'mrs ', 'ms ', 'miss ']
    for _ in range(2):
        for prefix in prefixes:
            if n.startswith(prefix):
                n = n[len(prefix):]
                break
    return n.strip()


def name_key(name: str) -> str:
    """Matching key for a person's name from either source: display_name first
    (drops every post-nominal, e.g. 'KC MP'), then normalise_name."""
    return normalise_name(display_name(name))


# Written Questions answering-body names that differ from the Members API
# department a post is filed under (keys as department_key gives them).
_DEPARTMENT_ALIASES = {
    "treasury": "hmtreasury",
    "leaderofthehouse": "officeoftheleaderofthehouseofcommons",
    "leaderofthehouseofcommons": "officeoftheleaderofthehouseofcommons",
    "leaderofthehouseoflords": "officeoftheleaderofthehouseoflords",
    "governmentequalitiesoffice": "womenandequalities",
    "chancelloroftheduchyoflancaster": "cabinetoffice",
    "chancelloroftheduchyoflancasterandministerforthecabinetoffice": "cabinetoffice",
    "ministerwithoutportfolio": "cabinetoffice",
}


def department_key(name: str) -> str:
    """'Foreign, Commonwealth & Development Office' and Parliament's
    'Foreign, Commonwealth and Development Office' give the same key, as do
    the Written Questions API's 'Treasury' and Parliament's 'HM Treasury'."""
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"^the\s+", "", n.strip())
    key = re.sub(r"[^a-z0-9]+", "", n)
    return _DEPARTMENT_ALIASES.get(key, key)


# ── Reading the table ─────────────────────────────────────────────────────────

def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, rows=None)


def _rows() -> list[dict]:
    """All gov_minister rows as plain dicts, cached per process. [] when the
    table is empty or missing (migration not yet run)."""
    now = time.time()
    with _lock:
        if _cache["rows"] is not None and now - _cache["at"] < CACHE_TTL:
            return _cache["rows"]
    from cache_models import GovMinister
    from extensions import db
    try:
        rows = [{
            "department": r.department, "govuk_department": r.govuk_department,
            "role_title": r.role_title, "member_name": r.member_name,
            "govuk_name": r.govuk_name, "name_norm": r.name_norm, "mnis_id": r.mnis_id,
            "house": r.house, "rank": r.rank or 0, "is_cabinet": bool(r.is_cabinet),
            "start_date": r.start_date, "fetched_at": r.fetched_at,
        } for r in GovMinister.query.order_by(
            GovMinister.department, GovMinister.rank, GovMinister.id).all()]
    except Exception as exc:
        db.session.rollback()
        logger.warning("gov_minister read failed (table missing?): %s", exc)
        rows = []
    with _lock:
        _cache.update(at=now, rows=rows)
    return rows


def _person_key(row: dict):
    return row["mnis_id"] or row["name_norm"]


def load_minister_list() -> dict | None:
    """The Research Tool's minister data built from gov_minister, or None when
    the table is empty. A new dict on every call (callers may add to it).

    by_norm    normalised name → role (Parliament and GOV.UK spellings)
    by_id      str(member id) → role
    by_dept    department → [{name, role, parliament_id, is_lord}] in rank order
    member_ids name → {parliament_id, is_lord}
    """
    rows = _rows()
    if not rows:
        return None
    primary: dict = {}
    for r in sorted(rows, key=lambda r: (not r["is_cabinet"], r["rank"])):
        primary.setdefault(_person_key(r), r)

    by_norm: dict = {}
    by_id: dict = {}
    by_dept: dict = {}
    member_ids: dict = {}
    for r in rows:
        role = primary[_person_key(r)]["role_title"]
        is_lord = r["house"] == "Lords"
        for key in (r["name_norm"], name_key(r["govuk_name"] or "")):
            if key:
                by_norm.setdefault(key, role)
        if r["mnis_id"]:
            by_id[str(r["mnis_id"])] = role
            member_ids[r["member_name"]] = {"parliament_id": r["mnis_id"], "is_lord": is_lord}
        if r["department"] in NON_PICKER_DEPARTMENTS:
            continue
        entries = by_dept.setdefault(r["department"], [])
        if not any(e["name"] == r["member_name"] for e in entries):
            entries.append({"name": r["member_name"], "role": r["role_title"],
                            "parliament_id": r["mnis_id"], "is_lord": is_lord})
    fetched = max(r["fetched_at"] for r in rows if r["fetched_at"])
    return {"_source": "db", "_fetched_at": fetched.isoformat() if fetched else None,
            "by_norm": by_norm, "by_id": by_id, "by_dept": by_dept, "member_ids": member_ids}


# ── Posts held on a date ──────────────────────────────────────────────────────

def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def post_histories(member_ids) -> dict[int, dict]:
    """Dated government posts from member_profile (the Members API Biography,
    stored weekly by scripts/refresh_members.py), for the members whose
    Biography has been fetched: {member_id: {"house", "refreshed", "posts":
    [(start, end, name, department)]}}. A member missing from the result has no dated record
    (never fetched), which is different from an empty list (never a minister).
    """
    from cache_models import MemberProfile
    from extensions import db
    ids = sorted({int(m) for m in member_ids if m})
    if not ids:
        return {}
    try:
        rows = (db.session.query(MemberProfile.member_id, MemberProfile.house,
                                 MemberProfile.government_posts, MemberProfile.details_refreshed_at)
                .filter(MemberProfile.member_id.in_(ids),
                        MemberProfile.details_refreshed_at.isnot(None))
                .all())
    except Exception as exc:
        db.session.rollback()
        logger.warning("member_profile read failed (table missing?): %s", exc)
        return {}
    out = {}
    for member_id, house, posts, refreshed in rows:
        if posts is None:
            continue
        out[member_id] = {"house": house, "refreshed": _as_date(refreshed), "posts": [
            (_as_date(p.get("start")), _as_date(p.get("end")), p.get("name") or "", p.get("info") or "")
            for p in posts if p.get("name")]}
    return out


# A post's place among those held on the same day: the main departmental post
# first, then junior posts; secondary posts (Women and Equalities, Lords
# spokesperson, whips' Household titles) last.
_SECONDARY_POST_WORDS = ("women and equalities", "equalities", "spokesperson", "whip",
                         "in waiting", "lord commissioner", "lords commissioner",
                         "household", "comptroller", "vice-chamberlain", "captain of")
_JUNIOR_POST_WORDS = ("under-secretary", "under secretary", "parliamentary secretary",
                      "economic secretary", "exchequer secretary")
# Checked before the senior words, so "Deputy Leader of the House" is not senior.
_MIDDLE_POST_WORDS = ("minister of state", "solicitor general", "advocate general",
                      "financial secretary", "paymaster general", "minister without portfolio",
                      "deputy leader")
_SENIOR_POST_WORDS = ("prime minister", "secretary of state", "chancellor of the exchequer",
                      "chief secretary", "attorney general", "lord chancellor", "leader of the house",
                      "chancellor of the duchy", "lord president", "lord privy seal")

# Parliament's post list can lag an appointment by a few days, so a
# gov_minister post that began this close before the history was fetched may
# still be missing from it.
HISTORY_LAG_DAYS = 7


def post_rank(title: str) -> int:
    """0 Secretary of State and other senior posts, 1 Minister of State,
    2 Parliamentary Under-Secretary, 3 other, 5 secondary posts."""
    t = (title or "").lower()
    for rank, words in ((5, _SECONDARY_POST_WORDS), (2, _JUNIOR_POST_WORDS),
                        (1, _MIDDLE_POST_WORDS), (0, _SENIOR_POST_WORDS)):
        if any(w in t for w in words):
            return rank
    return 3


def _held_on(member_id: int, day: date, history: dict | None) -> list[tuple]:
    """(start, title, department keys, is_whip) for every post the member held
    on `day`. A post's end date is the day the next one starts (Parliament
    sets them equal), so it is not counted as held on that day.

    Dated posts come from the member's history. gov_minister rows (refreshed
    daily) add current posts that began on or before `day`; when there is a
    history, only those that began after it was fetched (less a few days of
    lag), so a stale table can't bring back a post the history shows ended."""
    held = []
    for start, end, name, dept in (history or {}).get("posts", []):
        if start and start <= day and (end is None or day < end):
            held.append((start, name, {department_key(dept)}, dept in WHIP_DEPARTMENTS))
    newer_than = None
    if history is not None:
        refreshed = history.get("refreshed")
        newer_than = refreshed - timedelta(days=HISTORY_LAG_DAYS) if refreshed else date.max
    for r in _rows():
        if r["mnis_id"] != member_id or not r["start_date"] or r["start_date"] > day:
            continue
        if newer_than is not None and r["start_date"] <= newer_than:
            continue
        keys = {department_key(r["department"]), department_key(r["govuk_department"] or "")}
        held.append((r["start_date"], r["role_title"], keys - {""},
                     r["department"] in WHIP_DEPARTMENTS))
    return held


def post_on_date(member_id, on_date, department=None, *, strict_department=False,
                 histories=None) -> str | None:
    """The government post a member held on a date, or None when our records
    don't show one. Reads only our own tables (member_profile history and
    gov_minister); never an outside API.

    department: prefer a post in this department (a post naming it, e.g.
      "Jointly with HM Treasury", counts). With strict_department, a post in
      another department is not returned, except a Lords whip's (Lords whips
      answer for departments); used for Written Question answers.
    histories: a post_histories() result, so a page with many speakers makes
      one query.
    Among the remaining posts: a departmental post before a whip's, then the
    most senior (post_rank), then the longest held.
    """
    day = _as_date(on_date)
    if not member_id or day is None:
        return None
    member_id = int(member_id)
    if histories is None:
        histories = post_histories([member_id])
    history = histories.get(member_id)
    held = _held_on(member_id, day, history)
    if department:
        key = department_key(department)
        in_dept = [h for h in held if key in h[2] or key in department_key(h[1])]
        if in_dept:
            held = in_dept
        elif strict_department:
            is_lord = (history or {}).get("house") == "Lords" or any(
                r["mnis_id"] == member_id and r["house"] == "Lords" for r in _rows())
            held = [h for h in held if h[3]] if is_lord else []
    if not held:
        return None
    return min(held, key=lambda h: (h[3], post_rank(h[1]), h[0]))[1]


def pq_answering_role(mnis_id, answering_body, answer_date=None, histories=None):
    """Post title for a Written Question's answering minister: the post they
    held in the answering department on the answer date (a Lords whip's own
    post when they answered for another department). None when our records
    don't show one, so a page shows no title rather than a wrong one.
    """
    if not mnis_id or not answering_body or not answer_date:
        return None
    return post_on_date(mnis_id, answer_date, answering_body, strict_department=True,
                        histories=histories)


def table_status() -> dict:
    """Counts for the /admin minister card (uncached)."""
    from cache_models import GovMinister
    from extensions import db
    from sqlalchemy import func
    try:
        rows = db.session.query(GovMinister.department, GovMinister.mnis_id,
                                GovMinister.name_norm, GovMinister.fetched_at).all()
    except Exception as exc:
        db.session.rollback()
        return {"exists": False, "error": str(exc)[:200]}
    if not rows:
        return {"exists": True, "rows": 0}
    fetched = db.session.query(func.max(GovMinister.fetched_at)).scalar()
    age_hours = (datetime.utcnow() - fetched).total_seconds() / 3600 if fetched else None
    return {
        "exists": True,
        "rows": len(rows),
        "people": len({r.mnis_id or r.name_norm for r in rows}),
        "departments": len({r.department for r in rows}),
        "without_member_id": len({r.name_norm for r in rows if not r.mnis_id}),
        "fetched_at": fetched,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
    }
