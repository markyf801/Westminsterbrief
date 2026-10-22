"""
Divisions (recorded votes) ingestion, both Houses -> ha_division / ha_division_vote.
The web pages (/divisions, member pages) read only from these tables.

Usage:
    python scripts/ingest_divisions.py                      # daily run: last 14 days
    python scripts/ingest_divisions.py --days 30            # wider rolling window
    python scripts/ingest_divisions.py --since 2024-07-04   # backfill: this Parliament
    python scripts/ingest_divisions.py --house commons      # one House only
    python scripts/ingest_divisions.py --limit 20           # cap Commons detail fetches (testing)
    python scripts/ingest_divisions.py --dry-run            # list only: predict new/changed/unchanged, no writes

How a run works:
1. Commons Votes API: page through /data/divisions.json/search for the window
   (25 per page, the API maximum). The list has no member lists, so fetch
   /data/division/{id}.json only when the division is new, Parliament's
   PublicationUpdated time has changed, or we hold no votes for it.
2. Lords Votes API: page through /data/Divisions/search (each row already
   carries the full member lists, so no detail calls).
3. Upsert by (House, division id). Votes are rewritten only when the member
   lists differ from what we hold (sha1 in ha_division.votes_hash).
4. Hansard /search/divisions.json for the window, both Houses: matched on
   (House, date, division number) to get the debate section id, the question
   put and the result line. The debate section id links the division to our
   ha_session row (fallback: same House, date and debate title, if unique).
   Divisions not yet linked are re-tried on later runs while in the window.

Re-running is safe and resumes a broken backfill: unchanged divisions are
skipped. Requests are paced. A 429 stops the run at once (CLAUDE.md: do not
loop on rate limits); 5xx / timeouts are retried with backoff. Exits 1 if most
requests fail, the run was rate limited, or it hit a fatal error.

Railway: run with SKIP_MIGRATIONS=1 after migrations/division_tables.py --execute.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from divisions_models import VOTE_AYE, VOTE_NO, HaDivision, HaDivisionVote  # noqa: E402
from extensions import db  # noqa: E402
from hansard_archive.models import HaCronRun, HansardSession  # noqa: E402

logger = logging.getLogger(__name__)

COMMONS_API = "https://commonsvotes-api.parliament.uk"
LORDS_API = "https://lordsvotes-api.parliament.uk"
HANSARD_API = "https://hansard-api.parliament.uk"

OFFICIAL_URL = {
    "Commons": "https://votes.parliament.uk/Votes/Commons/Division/{id}",
    "Lords": "https://votes.parliament.uk/Votes/Lords/Division/{id}",
}

PARLIAMENT_START = date(2024, 7, 4)   # general election; first division was days later
DEFAULT_DAYS = 14
PAGE_SIZE = 25                        # Commons and Lords Votes APIs: 25 is the Commons maximum
HANSARD_PAGE_SIZE = 100
REQUEST_TIMEOUT = 60
INTER_REQUEST_DELAY = 0.5
MAX_ATTEMPTS = 3                      # first try + 2 retries, transient errors only

_COMMONS_LISTS = ("Ayes", "Noes", "AyeTellers", "NoTellers", "NoVoteRecorded")
_LORDS_LISTS = ("contents", "notContents", "contentTellers", "notContentTellers")


class RateLimited(Exception):
    """Parliament returned 429. The run stops; nothing is retried."""


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Api:
    """Paced GETs; retries 5xx / timeouts with backoff; 429 raises RateLimited."""

    def __init__(self, sleep=time.sleep):
        self.ok = 0
        self.failed = 0
        self._sleep = sleep

    def get(self, url: str, params: dict | None = None):
        """Parsed JSON (dict or list), or None after failures / permanent errors."""
        reason = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._sleep(INTER_REQUEST_DELAY)
            try:
                resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                    headers={"Accept": "application/json"})
                if resp.status_code == 200:
                    body = resp.json()
                    if isinstance(body, (dict, list)):
                        self.ok += 1
                        return body
                    reason = "unexpected JSON"
                elif resp.status_code == 429:
                    self.failed += 1
                    raise RateLimited(f"HTTP 429 from {url}")
                elif resp.status_code in (500, 502, 503, 504):
                    reason = f"HTTP {resp.status_code}"
                else:
                    logger.warning("GET %s -> HTTP %d (not retried)", url, resp.status_code)
                    self.failed += 1
                    return None
            except (requests.RequestException, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_ATTEMPTS:
                delay = 2 ** attempt
                print(f"  [retry {attempt}/{MAX_ATTEMPTS - 1}] {url}: {reason}; waiting {delay}s", flush=True)
                self._sleep(delay)
        print(f"  [failed] {url}: {reason}", flush=True)
        self.failed += 1
        return None


def list_commons(api: Api, start: date, end: date) -> tuple[list[dict], bool]:
    """Commons division summaries dated [start, end]. Returns (rows, complete)."""
    rows: list[dict] = []
    skip = 0
    while True:
        page = api.get(f"{COMMONS_API}/data/divisions.json/search", {
            "queryParameters.startDate": start.isoformat(),
            "queryParameters.endDate": end.isoformat(),
            "queryParameters.skip": skip,
            "queryParameters.take": PAGE_SIZE,
        })
        if not isinstance(page, list):
            return rows, False
        rows.extend(r for r in page if isinstance(r, dict) and r.get("DivisionId"))
        skip += PAGE_SIZE
        print(f"[divisions] Commons list: {len(rows)}", flush=True)
        if len(page) < PAGE_SIZE:
            return rows, True


def fetch_commons_detail(api: Api, division_id: int) -> dict | None:
    body = api.get(f"{COMMONS_API}/data/division/{division_id}.json")
    return body if isinstance(body, dict) and body.get("DivisionId") else None


def list_lords(api: Api, start: date, end: date) -> tuple[list[dict], bool]:
    """Lords divisions dated [start, end], with member lists. Returns (rows, complete)."""
    rows: list[dict] = []
    skip = 0
    while True:
        page = api.get(f"{LORDS_API}/data/Divisions/search", {
            "StartDate": start.isoformat(),
            "EndDate": end.isoformat(),
            "skip": skip,
            "take": PAGE_SIZE,
        })
        if not isinstance(page, list):
            return rows, False
        rows.extend(r for r in page if isinstance(r, dict) and r.get("divisionId"))
        skip += PAGE_SIZE
        print(f"[divisions] Lords list: {len(rows)}", flush=True)
        if len(page) < PAGE_SIZE:
            return rows, True


def list_hansard(api: Api, house: str, start: date, end: date) -> tuple[dict, bool]:
    """{(date, number): Hansard division row} for chamber divisions. Returns (map, complete)."""
    found: dict[tuple[date, int], dict] = {}
    skip = 0
    while True:
        body = api.get(f"{HANSARD_API}/search/divisions.json", {
            "queryParameters.house": house,
            "queryParameters.startDate": start.isoformat(),
            "queryParameters.endDate": end.isoformat(),
            "queryParameters.skip": skip,
            "queryParameters.take": HANSARD_PAGE_SIZE,
        })
        results = body.get("Results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            return found, False
        for r in results:
            if not isinstance(r, dict) or r.get("IsCommitteeDivision"):
                continue
            d, n = _date(r.get("Date")), _int(r.get("Number"))
            if d and n is not None:
                found[(d, n)] = r
        skip += HANSARD_PAGE_SIZE
        total = int(body.get("TotalResultCount") or 0)
        if not results or skip >= total:
            return found, True


# ── Parsing ───────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_BOILERPLATE_RE = re.compile(r"\b(The House divided|The Committee divided|The Committee proceeded to a Division)\s*:?",
                             re.IGNORECASE)


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19]) if value else None
    except ValueError:
        return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def plain_text(fragment: str | None) -> str:
    """HTML fragment -> plain text, without Hansard's 'The House divided:' line."""
    if not fragment:
        return ""
    s = re.sub(r"<br\s*/?>|</p>", " ", fragment, flags=re.IGNORECASE)
    s = html.unescape(_TAG_RE.sub("", s))
    return _clean(_BOILERPLATE_RE.sub("", s))


def _vote_row(m: dict, vote: str, teller: bool) -> dict | None:
    member_id = _int(m.get("MemberId") or m.get("memberId"))
    if not member_id:
        return None
    return {
        "member_id": member_id,
        "vote": vote,
        "member_name": _clean(m.get("Name") or m.get("name")) or "Unknown member",
        "list_as": _clean(m.get("ListAs") or m.get("listAs")) or None,
        "party": _clean(m.get("Party") or m.get("party")) or None,
        "member_from": _clean(m.get("MemberFrom") or m.get("memberFrom")) or None,
        "is_teller": teller,
        "proxy_name": _clean(m.get("ProxyName")) or None,
    }


def _merge(groups: list[tuple[list | None, str, bool]]) -> list[dict]:
    """One row per (member, vote); a teller listed in the lobby too is one row with is_teller."""
    out: dict[tuple[int, str], dict] = {}
    for members, vote, teller in groups:
        for m in members or []:
            row = _vote_row(m, vote, teller) if isinstance(m, dict) else None
            if row is None:
                continue
            key = (row["member_id"], vote)
            if key in out:
                out[key]["is_teller"] = out[key]["is_teller"] or teller
            else:
                out[key] = row
    return sorted(out.values(), key=lambda r: (r["vote"], r["member_id"]))


def commons_votes(detail: dict) -> list[dict]:
    # Commons tellers are not in Ayes / Noes; NoVoteRecorded is not stored.
    return _merge([(detail.get("Ayes"), VOTE_AYE, False), (detail.get("Noes"), VOTE_NO, False),
                   (detail.get("AyeTellers"), VOTE_AYE, True), (detail.get("NoTellers"), VOTE_NO, True)])


def lords_votes(raw: dict) -> list[dict]:
    # Lords tellers also appear in contents / notContents.
    return _merge([(raw.get("contents"), VOTE_AYE, False), (raw.get("notContents"), VOTE_NO, False),
                   (raw.get("contentTellers"), VOTE_AYE, True), (raw.get("notContentTellers"), VOTE_NO, True)])


def votes_hash(rows: list[dict]) -> str:
    return hashlib.sha1(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def commons_summary(r: dict) -> dict:
    return {
        "house": "Commons",
        "pid": int(r["DivisionId"]),
        "number": _int(r.get("Number")),
        "date": _date(r.get("Date")),
        "title": _clean(r.get("Title")),
        "ayes": _int(r.get("AyeCount")) or 0,
        "noes": _int(r.get("NoCount")) or 0,
        "is_deferred": bool(r.get("IsDeferred")),
        "motion_text": None,
        "source_updated": _dt(r.get("PublicationUpdated")),
        "raw": {k: v for k, v in r.items() if k not in _COMMONS_LISTS},
    }


def lords_summary(r: dict) -> dict:
    contents = r.get("authoritativeContentCount")
    not_contents = r.get("authoritativeNotContentCount")
    return {
        "house": "Lords",
        "pid": int(r["divisionId"]),
        "number": _int(r.get("number")),
        "date": _date(r.get("date")),
        "title": _clean(r.get("title")),
        "ayes": _int(contents) if contents is not None else len(r.get("contents") or []),
        "noes": _int(not_contents) if not_contents is not None else len(r.get("notContents") or []),
        "is_deferred": False,
        "motion_text": plain_text(r.get("amendmentMotionNotes")) or None,
        "source_updated": None,
        "raw": {k: v for k, v in r.items() if k not in _LORDS_LISTS},
    }


# ── Upsert ────────────────────────────────────────────────────────────────────

def _stored(house: str, pids: list[int]) -> dict[int, HaDivision]:
    out: dict[int, HaDivision] = {}
    for i in range(0, len(pids), 500):
        for d in HaDivision.query.filter(HaDivision.house == house,
                                         HaDivision.parliament_division_id.in_(pids[i:i + 500])).all():
            out[d.parliament_division_id] = d
    return out


def upsert_division(s: dict, votes: list[dict] | None, now: datetime) -> tuple[HaDivision, str]:
    """
    Write one division. votes=None keeps the stored votes. Returns
    (division, 'created' | 'updated' | 'unchanged'). Caller commits.
    """
    div = HaDivision.query.filter_by(house=s["house"], parliament_division_id=s["pid"]).first()
    created = div is None
    if created:
        div = HaDivision(house=s["house"], parliament_division_id=s["pid"], ingested_at=now)
        db.session.add(div)

    new_hash = votes_hash(votes) if votes is not None else div.votes_hash
    changed = created or any((
        div.number != s["number"], div.date != s["date"], div.title != s["title"],
        div.ayes_count != s["ayes"], div.noes_count != s["noes"],
        bool(div.is_deferred) != s["is_deferred"], new_hash != div.votes_hash,
        (s["motion_text"] is not None and div.motion_text != s["motion_text"]),
        (s["source_updated"] is not None and div.source_updated != s["source_updated"]),
    ))

    div.number = s["number"]
    div.date = s["date"]
    div.title = s["title"] or f"Division {s['number'] or s['pid']}"
    div.ayes_count = s["ayes"]
    div.noes_count = s["noes"]
    div.is_deferred = s["is_deferred"]
    if s["motion_text"] is not None:
        div.motion_text = s["motion_text"]
    if s["source_updated"] is not None:
        div.source_updated = s["source_updated"]
    div.official_url = OFFICIAL_URL[s["house"]].format(id=s["pid"])
    div.raw_data = s["raw"]
    div.last_refreshed = now
    if changed:
        div.updated_at = now
    db.session.flush()

    if votes is not None and new_hash != div.votes_hash:
        HaDivisionVote.query.filter(HaDivisionVote.division_id == div.id).delete(synchronize_session=False)
        db.session.add_all(HaDivisionVote(division_id=div.id, **v) for v in votes)
        div.votes_hash = new_hash
        div.vote_rows = len(votes)
    return div, "created" if created else ("updated" if changed else "unchanged")


def _write(summary_counts: dict, label: str, fn) -> HaDivision | None:
    """Run fn() and commit, with one retry on a dropped DB connection."""
    from sqlalchemy.exc import OperationalError
    for attempt in (1, 2):
        try:
            div, status = fn()
            db.session.commit()
            summary_counts[status] += 1
            return div
        except OperationalError as exc:
            db.session.rollback()
            db.engine.dispose()
            if attempt == 2:
                logger.error("DB error on %s: %s", label, exc)
                summary_counts["failed"] += 1
        except Exception as exc:
            db.session.rollback()
            logger.error("Ingest error on %s: %s", label, exc, exc_info=True)
            summary_counts["failed"] += 1
            return None
    return None


def plan_commons(listed: list[dict]) -> dict[str, list[dict]]:
    """Split Commons summaries into new / changed / no_votes / skip against the DB."""
    stored = _stored("Commons", [int(r["DivisionId"]) for r in listed])
    plan: dict[str, list[dict]] = {"new": [], "changed": [], "no_votes": [], "skip": []}
    for r in listed:
        d = stored.get(int(r["DivisionId"]))
        if d is None:
            plan["new"].append(r)
        elif _dt(r.get("PublicationUpdated")) != d.source_updated:
            plan["changed"].append(r)
        elif not d.vote_rows:
            plan["no_votes"].append(r)
        else:
            plan["skip"].append(r)
    return plan


def plan_lords(listed: list[dict]) -> dict[str, list[dict]]:
    """Dry-run prediction for Lords rows (the real run upserts every listed row)."""
    stored = _stored("Lords", [int(r["divisionId"]) for r in listed])
    plan: dict[str, list[dict]] = {"new": [], "changed": [], "skip": []}
    for r in listed:
        d = stored.get(int(r["divisionId"]))
        if d is None:
            plan["new"].append(r)
        elif votes_hash(lords_votes(r)) != d.votes_hash or lords_summary(r)["ayes"] != d.ayes_count:
            plan["changed"].append(r)
        else:
            plan["skip"].append(r)
    return plan


# ── Hansard links ─────────────────────────────────────────────────────────────

def _find_session(div: HaDivision) -> int | None:
    """Our ha_session id for the division's debate: by Hansard section id, else unique title match."""
    ext = div.hansard_debate_ext_id
    if ext:
        row = (db.session.query(HansardSession.id)
               .filter(HansardSession.ext_id.in_({ext, ext.upper(), ext.lower()})).first())
        if row:
            return row[0]
    title = (div.hansard_debate_title or "").strip().lower()
    if title:
        from sqlalchemy import func
        rows = (db.session.query(HansardSession.id)
                .filter(HansardSession.house == div.house, HansardSession.date == div.date,
                        HansardSession.is_container.is_(False),
                        func.lower(HansardSession.title) == title)
                .limit(2).all())
        if len(rows) == 1:
            return rows[0][0]
    return None


def link_hansard(api: Api, houses: tuple[str, ...], start: date, end: date, summary: dict) -> None:
    """Fill Hansard ids / question / result for divisions in the window, then link ha_session rows."""
    now = datetime.utcnow()
    for house in houses:
        found, complete = list_hansard(api, house, start, end)
        summary["hansard_complete"] = summary["hansard_complete"] and complete
        divs = HaDivision.query.filter(HaDivision.house == house, HaDivision.date >= start,
                                       HaDivision.date <= end).all()
        for div in divs:
            h = found.get((div.date, div.number))
            if not h:
                continue
            ext = _clean(h.get("DebateSectionExtId")) or None
            division_ext = _clean(h.get("ExternalId")) or None
            # Deferred divisions sit in their own Hansard section (same id as the
            # division) whose heading is not a debate: no debate to link.
            own_section = ext is not None and ext == division_ext
            new = {
                "hansard_debate_ext_id": None if own_section else ext,
                "hansard_division_ext_id": division_ext,
                "hansard_debate_title": None if own_section else (_clean(h.get("DebateSection")) or None),
                "outcome_text": plain_text(h.get("TextAfterVote")) or div.outcome_text,
                "motion_text": div.motion_text or plain_text(h.get("TextBeforeVote")) or None,
            }
            if any(getattr(div, k) != v for k, v in new.items()):
                for k, v in new.items():
                    setattr(div, k, v)
                div.updated_at = now
                summary["hansard_matched"] += 1
        db.session.commit()

    # Any division (any date) with a Hansard section but no ha_session yet: the
    # archive cron may have added the debate since the last run.
    pending = (HaDivision.query.filter(HaDivision.ha_session_id.is_(None),
                                       HaDivision.hansard_debate_ext_id.isnot(None))
               .filter(HaDivision.house.in_(houses)).all())
    for div in pending:
        sid = _find_session(div)
        if sid:
            div.ha_session_id = sid
            div.updated_at = now
            summary["sessions_linked"] += 1
    db.session.commit()


# ── Main loop ─────────────────────────────────────────────────────────────────

def ingest(start: date, end: date, houses: tuple[str, ...] = ("Commons", "Lords"),
           limit: int | None = None, dry_run: bool = False, api: Api | None = None) -> dict:
    """Run one ingest. Expects an app context. Returns a summary dict."""
    api = api or Api()
    t0 = time.time()
    s = {"listed_commons": 0, "listed_lords": 0, "list_complete": True,
         "planned_new": 0, "planned_changed": 0, "planned_unchanged": 0,
         "created": 0, "updated": 0, "unchanged": 0, "failed": 0,
         "hansard_matched": 0, "sessions_linked": 0, "hansard_complete": True,
         "rate_limited": False, "dry_run": dry_run}
    print(f"[divisions] window {start} -> {end} | {', '.join(houses)}{' | DRY RUN' if dry_run else ''}",
          flush=True)
    try:
        if "Commons" in houses:
            listed, complete = list_commons(api, start, end)
            s["listed_commons"] = len(listed)
            s["list_complete"] = s["list_complete"] and complete
            plan = plan_commons(listed)
            s["planned_new"] += len(plan["new"])
            s["planned_changed"] += len(plan["changed"]) + len(plan["no_votes"])
            s["planned_unchanged"] += len(plan["skip"])
            print(f"[divisions] Commons: listed {len(listed)} (complete={complete}) | new {len(plan['new'])} | "
                  f"changed {len(plan['changed'])} | missing votes {len(plan['no_votes'])} | "
                  f"unchanged {len(plan['skip'])}", flush=True)
            to_fetch = plan["new"] + plan["changed"] + plan["no_votes"]
            if limit is not None:
                to_fetch = to_fetch[:limit]
            if not dry_run:
                now = datetime.utcnow()
                for n, row in enumerate(to_fetch, 1):
                    detail = fetch_commons_detail(api, int(row["DivisionId"]))
                    if detail is None:
                        s["failed"] += 1
                        continue
                    _write(s, f"Commons division {row['DivisionId']}",
                           lambda d=detail: upsert_division(commons_summary(d), commons_votes(d), now))
                    if n % 50 == 0:
                        print(f"[divisions] Commons details {n}/{len(to_fetch)} | "
                              f"{time.time() - t0:.0f}s", flush=True)
                s["unchanged"] += len(plan["skip"])

        if "Lords" in houses:
            listed, complete = list_lords(api, start, end)
            s["listed_lords"] = len(listed)
            s["list_complete"] = s["list_complete"] and complete
            plan = plan_lords(listed)
            s["planned_new"] += len(plan["new"])
            s["planned_changed"] += len(plan["changed"])
            s["planned_unchanged"] += len(plan["skip"])
            print(f"[divisions] Lords: listed {len(listed)} (complete={complete}) | new {len(plan['new'])} | "
                  f"changed {len(plan['changed'])} | unchanged {len(plan['skip'])}", flush=True)
            if not dry_run:
                # The rows already hold the member lists, so every listed division
                # goes through the upsert, which writes only what has changed.
                now = datetime.utcnow()
                for row in listed:
                    _write(s, f"Lords division {row['divisionId']}",
                           lambda r=row: upsert_division(lords_summary(r), lords_votes(r), now))

        if not dry_run:
            link_hansard(api, houses, start, end, s)
    except RateLimited as exc:
        db.session.rollback()
        s["rate_limited"] = True
        print(f"[divisions] STOPPED: {exc}. Parliament is rate limiting; wait before re-running "
              f"(CLAUDE.md: do not loop on 429).", flush=True)

    if s["listed_commons"] + s["listed_lords"] == 0 and s["list_complete"]:
        print("[divisions] No divisions in this window (recess, or no votes called).", flush=True)
    s.update(requests_ok=api.ok, requests_failed=api.failed, elapsed_s=round(time.time() - t0, 1))
    return s


_SUMMARY_KEYS = ("listed_commons", "listed_lords", "list_complete", "planned_new", "planned_changed",
                 "planned_unchanged", "created", "updated", "unchanged", "failed", "hansard_matched",
                 "sessions_linked", "hansard_complete", "rate_limited", "requests_ok", "requests_failed",
                 "elapsed_s")


def _print_summary(s: dict) -> None:
    print("\n[divisions] ========== Run complete ==========", flush=True)
    for key in _SUMMARY_KEYS:
        print(f"  {key:<18} {s.get(key)}", flush=True)


def mostly_failed(s: dict) -> bool:
    return (s["rate_limited"]
            or s["requests_failed"] > s["requests_ok"]
            or (not s["list_complete"] and s["listed_commons"] + s["listed_lords"] == 0))


def resolve_window(days: int, since: date | None, today: date) -> tuple[date, date]:
    return (since or today - timedelta(days=days)), today


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest Commons and Lords divisions (recorded votes)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Rolling window in days (default {DEFAULT_DAYS})")
    parser.add_argument("--since", type=date.fromisoformat,
                        help=f"Backfill from YYYY-MM-DD (this Parliament: {PARLIAMENT_START})")
    parser.add_argument("--house", choices=("both", "commons", "lords"), default="both")
    parser.add_argument("--limit", type=int, help="Max Commons detail fetches")
    parser.add_argument("--dry-run", action="store_true", help="List only; predict, write nothing")
    parser.add_argument("--service-name", default="divisions-cron", help="ha_cron_run.service_name")
    args = parser.parse_args()

    start, end = resolve_window(args.days, args.since, date.today())
    houses = {"both": ("Commons", "Lords"), "commons": ("Commons",), "lords": ("Lords",)}[args.house]

    from flask_app import app
    exit_code = 0
    with app.app_context():
        print(f"[divisions] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        run = None
        if not args.dry_run:
            run = HaCronRun(service_name=args.service_name, started_at=datetime.utcnow(),
                            days_window=(end - start).days, status="running")
            db.session.add(run)
            db.session.commit()
        try:
            summary = ingest(start, end, houses=houses, limit=args.limit, dry_run=args.dry_run)
            _print_summary(summary)
            exit_code = 1 if mostly_failed(summary) else 0
            if run:
                run.sessions_ingested = summary["created"] + summary["updated"]
                run.errors = summary["failed"] + summary["requests_failed"]
                run.notes = (f"commons={summary['listed_commons']} lords={summary['listed_lords']} "
                             f"created={summary['created']} updated={summary['updated']} "
                             f"failed={summary['failed']} linked={summary['sessions_linked']}"
                             f"{' RATE LIMITED (429)' if summary['rate_limited'] else ''}")
        except Exception as exc:
            db.session.rollback()
            print(f"[divisions] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", flush=True)
            exit_code = 1
            if run:
                run.notes = f"fatal: {type(exc).__name__}: {exc}"[:2000]
        if run:
            run.finished_at = datetime.utcnow()
            run.status = "failed" if exit_code else "ok"
            db.session.commit()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
