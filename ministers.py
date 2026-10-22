"""
ministers.py — current government ministers, read from our own gov_minister table.

The table is rebuilt daily by scripts/refresh_ministers.py (Parliament Members
API government posts merged with GOV.UK's ministers page). Pages read it here;
nothing in this module calls an outside API.

- load_minister_list()  the dict the Research Tool has always used
                        ({by_norm, by_id, by_dept, member_ids}), or None while
                        the table is empty (callers then fall back to the old
                        file-cached GOV.UK fetch, so deploy order doesn't matter)
- pq_answering_role()   "Minister of State (Education)" for a PQ's answering
                        minister, only when they hold that post in the answering
                        department and held it on the answer date
- table_status()        counts for /admin

Rows are cached per process for CACHE_TTL seconds, so a page that checks many
speakers makes one small query.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # seconds

# Parliament departments that hold whips and Household posts. Their holders are
# still flagged as government speakers (Lords whips answer for departments), but
# they are not offered as departments in the Research Tool pickers. "Prime
# Minister" is left out because the PM is also listed under the Cabinet Office.
NON_PICKER_DEPARTMENTS = frozenset({
    "Whips Office (Commons)", "Whips Office (Lords)", "HM Household", "Prime Minister",
})

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


def department_key(name: str) -> str:
    """'Foreign, Commonwealth & Development Office' and Parliament's
    'Foreign, Commonwealth and Development Office' give the same key."""
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"^the\s+", "", n.strip())
    return re.sub(r"[^a-z0-9]+", "", n)


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


def pq_answering_role(mnis_id, answering_body, answer_date=None, fallback=None):
    """Post title for a Written Question's answering minister, from gov_minister.

    Only returned when the member currently holds a post in the answering
    department and the post began on or before the answer date, so an old
    answer never shows a post the minister was given later. Returns None when
    there is no such post, or `fallback` while the table is empty.
    """
    rows = _rows()
    if not rows:
        return fallback
    if not mnis_id or not answering_body:
        return None
    if isinstance(answer_date, datetime):
        answer_date = answer_date.date()
    dept = department_key(answering_body)
    matches = [
        r for r in rows
        if r["mnis_id"] == int(mnis_id)
        and dept in (department_key(r["department"]), department_key(r["govuk_department"] or ""))
        and (answer_date is None or r["start_date"] is None or r["start_date"] <= answer_date)
    ]
    if not matches:
        return None
    return min(matches, key=lambda r: r["rank"])["role_title"]


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
