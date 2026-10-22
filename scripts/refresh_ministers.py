"""
Refresh the current list of government ministers into gov_minister.

The Research Tool's department and minister pickers, its minister flags and
the PQ pages' answering-minister roles read this table (ministers.py), so this
script is the one place that asks GOV.UK and Parliament who the ministers are.
It replaces the old per-process fetch that wrote minister_cache.json on
Railway's ephemeral disk and was redone after every deploy.

Sources (about 30 polite requests, under a minute)
  1. Parliament Members API — /Posts/Departments/Government, then
     /Posts/GovernmentPosts?departmentId=N for each department, plus
     /Posts/GovernmentPosts (the Cabinet-level posts) to mark Cabinet members.
     Gives each minister's Parliament member id, post name
     ("Minister of State (Education)"), order and start date. Primary source:
     ids mean no guessing from names, and the post names are the ones
     Parliament prints on Written Answers.
  2. GOV.UK content API — /api/content/government/ministers, one request: every
     ministerial department with its ministers and their current roles embedded
     (the old code fetched each department page separately for the same data).
     Adds the portfolio title ("Minister for School Standards") and anyone
     Parliament's list is missing (in Oct 2026 the Financial Secretary to the
     Treasury was on GOV.UK but not in the Members API).

A GOV.UK minister is matched to a Parliament post holder in the same department
by name (honorifics and post-nominals stripped; "Daniel" vs "Dan" handled by
surname + first initial when unique in that department). Anyone still without a
member id is looked up in our own member_profile / cached_member tables.

Safety
  - Any failed request (after one retry on a timeout/5xx) aborts the run and
    leaves the table as it was. 401/403/429 stop at once (CLAUDE.md API rules).
  - A result with too few people or departments, or under 70% of the people
    already stored, is treated as a bad fetch: nothing is written.
  - Writes happen in one transaction: matched rows updated, new rows inserted,
    rows no longer current deleted by id.

Usage
  python scripts/refresh_ministers.py              # fetch, check, write
  python scripts/refresh_ministers.py --dry-run    # fetch, check, predict insert/update/delete; no writes

Railway: Dockerfile.cron, SKIP_MIGRATIONS=1, after migrations/gov_minister_table.py --execute.
Logs a row in ha_cron_run (service_name=minister-refresh). Exit 1 on failure.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ministers import department_key, display_name, name_key  # noqa: E402

MEMBERS_POSTS_API = "https://members-api.parliament.uk/api/Posts"
GOVUK_MINISTERS_API = "https://www.gov.uk/api/content/government/ministers"
GOVUK_SITE = "https://www.gov.uk"
USER_AGENT = "WestminsterBrief/1.0 (+https://westminsterbrief.co.uk; daily minister refresh)"

TIMEOUT = 30
DELAY = 0.5            # seconds between requests
RETRY_PAUSE = 5

MIN_PEOPLE = 60        # a full government is ~120 people including whips
MIN_DEPARTMENTS = 15
MIN_SHARE_OF_PREVIOUS = 0.7

FIELDS = ("department", "govuk_department", "role_title", "govuk_role_title", "member_name",
          "govuk_name", "name_norm", "mnis_id", "house", "rank", "is_cabinet", "start_date",
          "source", "source_url")


class StopRun(RuntimeError):
    """Permanent API error (401/403/429): stop, report, exit non-zero."""


class FetchFailed(RuntimeError):
    """A request failed after its retry; the table is left as it was."""


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Api:
    def __init__(self, session=None, delay: float = DELAY, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.delay = delay
        self.sleep = sleep
        self.ok = 0

    def get_json(self, url: str, params: dict | None = None):
        """GET JSON with one retry on timeout/connection error/5xx/bad JSON.
        401/403/429 raise StopRun; any other failure raises FetchFailed."""
        for attempt in (1, 2):
            self.sleep(self.delay)
            try:
                resp = self.session.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                reason = exc.__class__.__name__
            else:
                if resp.status_code in (401, 403, 429):
                    raise StopRun(f"HTTP {resp.status_code} from {url}")
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except ValueError:
                        reason = "invalid JSON"
                    else:
                        self.ok += 1
                        return body
                else:
                    reason = f"HTTP {resp.status_code}"
                    if resp.status_code < 500:
                        raise FetchFailed(f"{url}: {reason}")
            if attempt == 1:
                print(f"  [retry] {url} {params or ''}: {reason}", flush=True)
                self.sleep(RETRY_PAUSE)
        raise FetchFailed(f"{url} {params or ''}: {reason}")


def fetch_sources(api: Api) -> dict:
    """Every response the merge needs. Raises on any failure."""
    departments = api.get_json(f"{MEMBERS_POSTS_API}/Departments/Government")
    if not isinstance(departments, list) or not departments:
        raise FetchFailed("Members API returned no government departments")
    dept_list = []
    for d in departments:
        v = d.get("value") or d
        if v.get("id") and v.get("name"):
            dept_list.append((int(v["id"]), v["name"].strip()))
    cabinet_posts = api.get_json(f"{MEMBERS_POSTS_API}/GovernmentPosts")
    posts_by_dept = {}
    for dept_id, _name in dept_list:
        posts_by_dept[dept_id] = api.get_json(f"{MEMBERS_POSTS_API}/GovernmentPosts",
                                              params={"departmentId": dept_id}) or []
    govuk = api.get_json(GOVUK_MINISTERS_API)
    return {"departments": dept_list, "cabinet_posts": cabinet_posts or [],
            "posts_by_dept": posts_by_dept, "govuk": govuk or {}}


# ── Parsing ───────────────────────────────────────────────────────────────────

def _date(value) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _current_holders(post: dict):
    v = post.get("value") or {}
    for h in v.get("postHolders") or []:
        if h.get("endDate"):
            continue
        m = (h.get("member") or {}).get("value") or {}
        if m.get("id"):
            yield v, h, m


def parse_parliament(departments, cabinet_posts, posts_by_dept) -> list[dict]:
    """One row per (department, post, current holder) from the Members API."""
    cabinet_ids = {int(m["id"]) for post in cabinet_posts for _v, _h, m in _current_holders(post)}
    rows, seen = [], set()
    for dept_id, dept_name in departments:
        for post in posts_by_dept.get(dept_id) or []:
            for v, h, m in _current_holders(post):
                role = _clean(v.get("name"))
                key = (dept_name, int(m["id"]), role)
                if not role or key in seen:
                    continue
                seen.add(key)
                name = display_name(m.get("nameDisplayAs") or "")
                house = "Lords" if (m.get("latestHouseMembership") or {}).get("house") == 2 else "Commons"
                rows.append({
                    "department": dept_name, "govuk_department": None,
                    "role_title": role, "govuk_role_title": None,
                    "member_name": name, "govuk_name": None, "name_norm": name_key(name),
                    "mnis_id": int(m["id"]), "house": house,
                    "rank": int(v.get("order") or 0), "is_cabinet": int(m["id"]) in cabinet_ids,
                    "start_date": _date(h.get("startDate")), "source": "parliament",
                    "source_url": f"https://members.parliament.uk/member/{int(m['id'])}/career",
                })
    return rows


def _role_from_appointment(person_title: str, appointment_title: str) -> str:
    """GOV.UK appointment titles read 'Person title - Role'."""
    t = appointment_title or ""
    prefix = f"{person_title} - "
    return _clean(t[len(prefix):] if t.startswith(prefix) else t.split(" - ", 1)[-1])


def parse_govuk(govuk: dict) -> tuple[list[dict], set[str]]:
    """([{title, people: [...]}], name keys of Cabinet members) from the ministers page."""
    links = govuk.get("links") or {}
    depts = []
    for d in links.get("ordered_ministerial_departments") or []:
        dl = d.get("links") or {}
        dept_roles = {_clean(r.get("title")) for r in dl.get("ordered_roles") or []}
        people = []
        for order, m in enumerate(dl.get("ordered_ministers") or []):
            title = _clean(m.get("title"))
            if not title:
                continue
            current = [ra for ra in (m.get("links") or {}).get("role_appointments") or []
                       if (ra.get("details") or {}).get("current")]
            roles, starts = [], []
            for ra in current:
                role = _role_from_appointment(title, ra.get("title"))
                if role and role not in roles:
                    roles.append(role)
                    starts.append((role, _date((ra.get("details") or {}).get("started_on"))))
            here = [r for r in roles if r in dept_roles] or roles
            start_dates = [s for r, s in starts if r in here and s]
            people.append({
                "title": title, "name": display_name(title), "key": name_key(title),
                "roles": here, "start": min(start_dates) if start_dates else None,
                "url": GOVUK_SITE + m["base_path"] if m.get("base_path") else GOVUK_SITE + "/government/ministers",
                "order": order,
            })
        if d.get("title"):
            depts.append({"title": _clean(d["title"]), "people": people})
    cabinet = {name_key(m.get("title") or "")
               for section in ("ordered_cabinet_ministers", "ordered_also_attends_cabinet")
               for m in links.get(section) or []} - {""}
    return depts, cabinet


# ── Merge ─────────────────────────────────────────────────────────────────────

def _one_person(rows: list[dict]) -> list[dict]:
    """rows if they all belong to one member id, else []."""
    return rows if rows and len({r["mnis_id"] for r in rows}) == 1 else []


def match_person(person: dict, rows: list[dict], fuzzy: bool = True) -> list[dict]:
    """The Parliament rows (one member) for a GOV.UK person, or []."""
    exact = _one_person([r for r in rows if r["name_norm"] == person["key"]])
    if exact or not fuzzy:
        return exact
    parts = person["key"].split()
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]
    return _one_person([r for r in rows if len(r["name_norm"].split()) >= 2
                        and r["name_norm"].split()[-1] == last
                        and r["name_norm"].split()[0][:1] == first[:1]])


def merge(parl_rows: list[dict], govuk_depts: list[dict], cabinet_keys: set[str],
          parl_dept_names: list[str]) -> list[dict]:
    rows = [dict(r) for r in parl_rows]
    by_dept = defaultdict(list)
    for r in rows:
        by_dept[r["department"]].append(r)
    dept_names = {department_key(n): n for n in parl_dept_names}

    added = []
    for gd in govuk_depts:
        dept = dept_names.get(department_key(gd["title"])) or gd["title"].replace("&", "and")
        for p in gd["people"]:
            govuk_role = "; ".join(p["roles"]) or None
            matched = match_person(p, by_dept.get(dept, []))
            if matched:
                for r in matched:
                    r.update(govuk_department=gd["title"], govuk_role_title=govuk_role,
                             govuk_name=p["title"], source="parliament+govuk", source_url=p["url"])
                    r["is_cabinet"] = r["is_cabinet"] or p["key"] in cabinet_keys
                continue
            # Not under this department in Parliament's data. The same person may
            # hold a post elsewhere (exact name only, across departments).
            elsewhere = match_person(p, rows, fuzzy=False)
            known = elsewhere[0] if elsewhere else None
            member_name = known["member_name"] if known else p["name"]
            added.append({
                "department": dept, "govuk_department": gd["title"],
                "role_title": govuk_role or "Minister", "govuk_role_title": govuk_role,
                "member_name": member_name, "govuk_name": p["title"], "name_norm": name_key(member_name),
                "mnis_id": known["mnis_id"] if known else None,
                "house": known["house"] if known else ("Lords" if p["name"].startswith(("Lord ", "Baroness ")) else None),
                "rank": 900 + p["order"],
                "is_cabinet": p["key"] in cabinet_keys or bool(known and known["is_cabinet"]),
                "start_date": p["start"], "source": "govuk", "source_url": p["url"],
            })
    return rows + added


def resolve_missing_ids(rows: list[dict]) -> int:
    """Fill mnis_id for GOV.UK-only rows from verified overrides, then our own
    member tables (no API calls). Returns how many are still unmatched."""
    from debate_scanner import KNOWN_MINISTER_MEMBER_IDS, _local_member_lookup
    unmatched = set()
    for r in rows:
        if r["mnis_id"]:
            continue
        known = KNOWN_MINISTER_MEMBER_IDS.get(r["member_name"])
        if known:
            member_id, is_lord = known["parliament_id"], known["is_lord"]
        else:
            member_id, is_lord = _local_member_lookup(r["member_name"])
        if member_id:
            r["mnis_id"] = int(member_id)
            r["house"] = "Lords" if is_lord else "Commons"
        else:
            unmatched.add(r["member_name"])
    for name in sorted(unmatched):
        print(f"  [warn]  no Parliament member id for {name!r}", flush=True)
    return len(unmatched)


def _person(r: dict):
    return r["mnis_id"] or r["name_norm"]


def problems(rows: list[dict], previous_people: int) -> list[str]:
    """Reasons to treat this fetch as bad and keep the stored list."""
    people = len({_person(r) for r in rows})
    departments = len({r["department"] for r in rows})
    out = []
    if people < MIN_PEOPLE:
        out.append(f"only {people} people (minimum {MIN_PEOPLE})")
    if departments < MIN_DEPARTMENTS:
        out.append(f"only {departments} departments (minimum {MIN_DEPARTMENTS})")
    if not any(r["source"] != "govuk" for r in rows):
        out.append("no Parliament post holders")
    if not any(r["source"] != "parliament" for r in rows):
        out.append("nothing matched from GOV.UK")
    if previous_people and people < previous_people * MIN_SHARE_OF_PREVIOUS:
        out.append(f"{people} people is under {MIN_SHARE_OF_PREVIOUS:.0%} of the {previous_people} stored")
    return out


# ── Write ─────────────────────────────────────────────────────────────────────

def _row_key(r) -> tuple:
    get = r.get if isinstance(r, dict) else lambda k: getattr(r, k)
    return (get("department"), get("mnis_id") or get("name_norm"), get("role_title"))


def plan(rows: list[dict], existing: list) -> dict:
    """Match new rows to stored ones by (department, person, post)."""
    stored = {}
    duplicates = []
    for obj in existing:
        if _row_key(obj) in stored:
            duplicates.append(obj)
        else:
            stored[_row_key(obj)] = obj
    inserts, changed, same, seen = [], [], [], set()
    for r in rows:
        key = _row_key(r)
        if key in seen:
            continue
        seen.add(key)
        obj = stored.get(key)
        if obj is None:
            inserts.append(r)
        elif any(getattr(obj, f) != r[f] for f in FIELDS):
            changed.append((obj, r))
        else:
            same.append((obj, r))
    deletes = [obj for key, obj in stored.items() if key not in seen] + duplicates
    return {"inserts": inserts, "changed": changed, "same": same, "deletes": deletes}


def apply(changes: dict, fetched_at: datetime) -> None:
    """Write the plan in one transaction (caller commits or rolls back)."""
    from cache_models import GovMinister
    from extensions import db
    for obj, r in changes["changed"] + changes["same"]:  # unchanged rows are restamped too
        for f in FIELDS:
            setattr(obj, f, r[f])
        obj.fetched_at = fetched_at
    for r in changes["inserts"]:
        db.session.add(GovMinister(**{f: r[f] for f in FIELDS}, fetched_at=fetched_at))
    ids = [obj.id for obj in changes["deletes"]]
    if ids:
        GovMinister.query.filter(GovMinister.id.in_(ids)).delete(synchronize_session=False)


def _people_names(rows) -> dict:
    get = (lambda r, k: r[k]) if rows and isinstance(rows[0], dict) else getattr
    return {get(r, "mnis_id") or get(r, "name_norm"): get(r, "member_name") for r in rows}


def refresh(api: Api, dry_run: bool = False) -> dict:
    """Fetch, merge, check and (unless dry_run) write. Returns a summary dict;
    summary['ok'] is False when nothing was written because of a problem."""
    from cache_models import GovMinister
    from extensions import db

    t0 = time.time()
    src = fetch_sources(api)
    parl = parse_parliament(src["departments"], src["cabinet_posts"], src["posts_by_dept"])
    govuk_depts, cabinet = parse_govuk(src["govuk"])
    rows = merge(parl, govuk_depts, cabinet, [n for _id, n in src["departments"]])
    unmatched = resolve_missing_ids(rows)

    existing = GovMinister.query.all()
    previous = _people_names(existing)
    issues = problems(rows, len(previous))
    current = _people_names(rows)
    summary = {
        "requests_ok": api.ok, "parliament_rows": len(parl),
        "govuk_people": sum(len(d["people"]) for d in govuk_depts),
        "rows": len(rows), "people": len(current), "departments": len({r["department"] for r in rows}),
        "govuk_only_rows": sum(1 for r in rows if r["source"] == "govuk"),
        "parliament_only_rows": sum(1 for r in rows if r["source"] == "parliament"),
        "without_member_id": unmatched,
        "people_added": sorted(current[k] for k in current.keys() - previous.keys()) if previous else [],
        "people_removed": sorted(previous[k] for k in previous.keys() - current.keys()),
        "problems": issues, "dry_run": dry_run, "ok": not issues,
    }
    if issues:
        summary["elapsed_s"] = round(time.time() - t0, 1)
        return summary

    changes = plan(rows, existing)
    summary.update(inserted=len(changes["inserts"]), updated=len(changes["changed"]),
                   unchanged=len(changes["same"]), deleted=len(changes["deletes"]))
    if not dry_run:
        try:
            apply(changes, datetime.utcnow())
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    summary["elapsed_s"] = round(time.time() - t0, 1)
    return summary


def _print_summary(s: dict) -> None:
    print("\n[minister-refresh] ========== Run complete ==========", flush=True)
    for key in ("requests_ok", "parliament_rows", "govuk_people", "rows", "people", "departments",
                "govuk_only_rows", "parliament_only_rows", "without_member_id",
                "inserted", "updated", "unchanged", "deleted", "elapsed_s"):
        if key in s:
            print(f"  {key:<22} {s[key]}", flush=True)
    for key in ("people_added", "people_removed"):
        if s.get(key):
            print(f"  {key:<22} {', '.join(s[key])}", flush=True)
    if s.get("problems"):
        print(f"  NOT WRITTEN — {'; '.join(s['problems'])}. The stored list is unchanged.", flush=True)
    elif s.get("dry_run"):
        print("  Dry run — nothing written.", flush=True)


def run(api: Api, dry_run: bool = False, service_name: str = "minister-refresh") -> int:
    """One refresh inside an app context, logged in ha_cron_run unless dry_run.
    Returns the exit code."""
    from extensions import db
    from hansard_archive.models import HaCronRun

    exit_code = 0
    cron = None
    if not dry_run:
        cron = HaCronRun(service_name=service_name, started_at=datetime.utcnow(),
                         days_window=0, status="running")
        db.session.add(cron)
        db.session.commit()
    try:
        summary = refresh(api, dry_run=dry_run)
        _print_summary(summary)
        exit_code = 0 if summary["ok"] else 1
        if cron:
            cron.sessions_ingested = summary.get("inserted", 0) + summary.get("updated", 0)
            cron.errors = len(summary["problems"])
            cron.notes = (f"people={summary['people']} rows={summary['rows']} "
                          f"inserted={summary.get('inserted', 0)} updated={summary.get('updated', 0)} "
                          f"deleted={summary.get('deleted', 0)} no_id={summary['without_member_id']}"
                          + (f" NOT WRITTEN: {'; '.join(summary['problems'])}" if summary["problems"] else ""))[:2000]
    except (StopRun, FetchFailed) as exc:
        db.session.rollback()
        print(f"[minister-refresh] STOPPED {type(exc).__name__}: {exc}. "
              "The stored list is unchanged.", flush=True)
        exit_code = 1
        if cron:
            cron.errors = 1
            cron.notes = f"{type(exc).__name__}: {exc}"[:2000]
    except Exception as exc:
        db.session.rollback()
        print(f"[minister-refresh] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", flush=True)
        exit_code = 1
        if cron:
            cron.errors = 1
            cron.notes = f"fatal: {type(exc).__name__}: {exc}"[:2000]
    if cron:
        cron.finished_at = datetime.utcnow()
        cron.status = "failed" if exit_code else "ok"
        db.session.commit()
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh gov_minister from Parliament + GOV.UK")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and predict insert/update/delete; write nothing")
    parser.add_argument("--service-name", default="minister-refresh", help="ha_cron_run.service_name")
    args = parser.parse_args()

    from extensions import db
    from flask_app import app

    with app.app_context():
        print(f"[minister-refresh] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        exit_code = run(Api(), dry_run=args.dry_run, service_name=args.service_name)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
