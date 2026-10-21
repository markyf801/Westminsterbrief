"""
Committees ingestion: Parliament Committees API -> committee,
committee_membership, committee_inquiry, committee_inquiry_link,
committee_publication. The /committees pages read only from these tables.

Usage:
    python scripts/ingest_committees.py                 # daily run: every current committee
    python scripts/ingest_committees.py --limit 10      # first 10 committees only (testing)
    python scripts/ingest_committees.py --committee 203 # one committee
    python scripts/ingest_committees.py --dry-run       # fetch lists, predict new/update, write nothing

How a run works (all GETs, https://committees-api.parliament.uk/api):
1. /Committees?CommitteeStatus=Current  -> upsert committees; committees that
   drop off the current list are marked is_current=False (never deleted).
2. Per committee:
   /Committees/{id}                       purpose + contact (new committees, Mondays, --full)
   /Committees/{id}/Members?MembershipStatus=Current  -> replace that committee's members
   /CommitteeBusiness?CommitteeId={id}&Status=Open   -> upsert open inquiries + links
   /Publications?CommitteeId={id}&PublicationTypeIds=1,12,2  -> 10 latest reports /
                                           special reports / government responses
3. /CommitteeBusiness/{id} (scope text) only for inquiries that are new, changed
   since the last run, or not detail-fetched for 14 days.
4. Inquiries we hold as Open that no committee lists any more are marked Closed
   (only when every committee they belong to was fetched completely this run).

Idempotent upserts keyed by Parliament ids: re-running is safe. Calls are paced
(0.25s) with retry/backoff. Exits 1 if most requests fail or the run hits a
fatal error. Logs a row in ha_cron_run (service_name committees-cron).

Railway (proposed): cron service `committees-cron`, schedule `30 1 * * *`
(daily 01:30 UTC; a full run takes ~15 min, well clear of the 02:00 Sunday
member-cache refresh and the 03:00+ windows), start command
`python scripts/ingest_committees.py`, SKIP_MIGRATIONS=1. Run
migrations/committees_appgs_tables.py --execute first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from committees import clean_text, fix_mojibake, topic_slugs_for  # noqa: E402
from committees_models import (  # noqa: E402
    Committee, CommitteeInquiry, CommitteeInquiryLink, CommitteeMembership, CommitteePublication,
)
from extensions import db  # noqa: E402
from hansard_archive.models import HaCronRun  # noqa: E402
from hansard_archive.slugs import slugify_edm  # noqa: E402

logger = logging.getLogger(__name__)

API_BASE = "https://committees-api.parliament.uk/api"
REQUEST_TIMEOUT = 30
INTER_REQUEST_DELAY = 0.25
MAX_RETRIES = 3
PAGE_SIZE = 100
DETAIL_REFRESH_DAYS = 14
RECENT_PUBLICATIONS = 10
PUBLICATION_TYPE_IDS = [1, 12, 2]      # Report, Special Report, Government Response
EVIDENCE_TYPES = ("WrittenEvidence", "OralEvidence")
PUBLICATION_DOC_URL = "https://committees.parliament.uk/publications/{id}/documents/{doc}/default/"
HEADERS = {"Accept": "application/json",
           "User-Agent": "WestminsterBrief/1.0 (+https://westminsterbrief.co.uk)"}


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Api:
    """Paced GETs with retry/backoff. Counts outcomes for the exit-code rule."""

    def __init__(self, sleep=time.sleep):
        self.ok = 0
        self.failed = 0
        self._sleep = sleep

    def get(self, path: str, params: dict | None = None):
        """Parsed JSON body (dict or list), or None after MAX_RETRIES failures."""
        url = f"{API_BASE}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            self._sleep(INTER_REQUEST_DELAY)
            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    body = resp.json()
                    if isinstance(body, (dict, list)):
                        self.ok += 1
                        return body
                    reason = "unexpected JSON"
                elif resp.status_code in (429, 500, 502, 503, 504):
                    reason = f"HTTP {resp.status_code}"
                else:
                    logger.warning("GET %s -> HTTP %d (not retried)", path, resp.status_code)
                    self.failed += 1
                    return None
            except (requests.RequestException, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                delay = 2 ** attempt
                print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {path}: {reason}; waiting {delay}s", flush=True)
                self._sleep(delay)
            else:
                print(f"  [failed] {path}: {reason}", flush=True)
        self.failed += 1
        return None

    def get_all(self, path: str, params: dict | None = None, take: int = PAGE_SIZE) -> tuple[list[dict], bool]:
        """Every item of a paged {items, totalResults} endpoint. Returns (items, complete)."""
        items: list[dict] = []
        skip = 0
        while True:
            body = self.get(path, {**(params or {}), "Skip": skip, "Take": take})
            page = body.get("items") if isinstance(body, dict) else None
            if not isinstance(page, list):
                return items, False
            items.extend(i for i in page if isinstance(i, dict))
            total = int(body.get("totalResults") or 0)
            skip += take
            if len(page) < take or skip >= total:
                return items, True


# ── Parsing ───────────────────────────────────────────────────────────────────

def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def _d(value: str | None) -> date | None:
    dt = _dt(value)
    return dt.date() if dt else None


def _text(value: str | None) -> str:
    return " ".join(fix_mojibake(value or "").split())


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def upsert_committee(raw: dict, detail: dict | None, now: datetime) -> tuple[Committee, bool]:
    """Write one committee (list item + optional detail payload). Caller commits."""
    c = db.session.get(Committee, raw["id"])
    created = c is None
    if created:
        c = Committee(id=raw["id"], ingested_at=now)
        db.session.add(c)
    src = {**raw, **(detail or {})}
    parent = src.get("parentCommittee") or {}
    contact = src.get("contact") or {}
    c.name = _text(src.get("name")) or f"Committee {raw['id']}"
    c.slug = slugify_edm(c.name)
    c.house = src.get("house")
    c.category = (src.get("category") or {}).get("name")
    c.committee_types = [t.get("name") for t in src.get("committeeTypes") or [] if isinstance(t, dict)]
    c.parent_id = parent.get("id")
    c.parent_name = _text(parent.get("name")) or None
    if detail is not None:
        c.purpose = clean_text(detail.get("purpose")) or None
    c.contact_email = (contact.get("email") or "").strip() or None
    c.contact_phone = (contact.get("phone") or "").strip() or None
    c.contact_address = (contact.get("address") or "").strip() or None
    c.start_date = _d(src.get("startDate"))
    c.end_date = _d(src.get("endDate"))
    c.is_current = c.end_date is None or c.end_date >= now.date()
    c.show_on_website = bool(src.get("showOnWebsite", True))
    c.raw_data = {k: v for k, v in src.items() if k not in ("purpose", "banner", "contentLinks")}
    c.last_refreshed = now
    return c, created


def membership_rows(committee_id: int, items: list[dict]) -> list[CommitteeMembership]:
    """One row per person: the most senior current role wins (Chair > other > Member)."""
    rows = []
    for item in items:
        info = item.get("memberInfo") or {}
        roles = [r for r in item.get("roles") or [] if isinstance(r, dict) and not r.get("endDate")]
        names = [((r.get("role") or {}).get("name") or "Member") for r in roles] or ["Member"]
        is_chair = any((r.get("role") or {}).get("isChair") for r in roles) or bool(info.get("isChair"))
        senior = [n for n in names if n != "Member"]
        role = "Chair" if is_chair and "Chair" in names else (senior[0] if senior else "Member")
        starts = [_d(r.get("startDate")) for r in roles if r.get("startDate")]
        rows.append(CommitteeMembership(
            committee_id=committee_id,
            member_id=info.get("mnisId"),
            name=_text(info.get("displayAs") or item.get("name")) or "Unknown member",
            party=info.get("party"),
            member_from=info.get("memberFrom"),
            house=info.get("house"),
            role=role,
            roles=sorted(set(names)),
            is_chair=is_chair,
            is_lay_member=bool(item.get("isLayMember")),
            is_ex_officio=any(r.get("exOfficio") for r in roles),
            start_date=min(starts) if starts else None,
            end_date=None,
        ))
    return rows


def replace_memberships(committee_id: int, items: list[dict]) -> int:
    CommitteeMembership.query.filter(CommitteeMembership.committee_id == committee_id).delete(
        synchronize_session=False)
    rows = membership_rows(committee_id, items)
    db.session.add_all(rows)
    return len(rows)


def evidence_window(item: dict, now: datetime) -> tuple[bool, datetime | None, list[dict]]:
    """(open?, deadline, open periods) from a CommitteeBusiness payload's openSubmissionPeriods."""
    periods = [p for p in item.get("openSubmissionPeriods") or []
               if isinstance(p, dict) and p.get("submissionType") in EVIDENCE_TYPES]
    summary = [{"id": p.get("id"), "type": p.get("submissionType"),
                "start": p.get("startDate"), "end": p.get("endDate")} for p in periods]
    if not periods:
        return False, None, summary
    ends = [_dt(p.get("endDate")) for p in periods]
    future = [e for e in ends if e and e >= now]
    if future:
        return True, min(future), summary
    if any(e is None for e in ends):
        return True, None, summary
    return False, max(e for e in ends if e), summary


def upsert_inquiry(item: dict, detail: dict | None, now: datetime) -> tuple[CommitteeInquiry, bool]:
    """Write one open CommitteeBusiness (list item + optional detail). Caller commits."""
    inq = db.session.get(CommitteeInquiry, item["id"])
    created = inq is None
    if created:
        inq = CommitteeInquiry(id=item["id"], ingested_at=now)
        db.session.add(inq)
    btype = item.get("type") or {}
    inq.title = _text(item.get("title")) or f"Committee inquiry {item['id']}"
    inq.slug = slugify_edm(inq.title)
    inq.business_type = btype.get("name")
    inq.is_inquiry = bool(btype.get("isInquiry"))
    inq.open_date = _d(item.get("openDate"))
    inq.close_date = _d(item.get("closeDate"))
    inq.status = "Closed" if inq.close_date and inq.close_date <= now.date() else "Open"
    if detail is not None:
        inq.scope = clean_text(detail.get("scope")) or None
        inq.detail_fetched_at = now
    inq.evidence_open, inq.evidence_deadline, inq.submission_periods = evidence_window(item, now)
    nxt = item.get("nextOralEvidenceSession") or {}
    inq.next_oral_evidence = _dt(nxt.get("startDate") or nxt.get("meetingDate")) if isinstance(nxt, dict) else None
    report = item.get("latestReport")
    inq.latest_report = ({"id": report.get("id"), "description": _text(report.get("description")),
                          "date": report.get("publicationStartDate")}
                         if isinstance(report, dict) else None)
    inq.topic_slugs = topic_slugs_for(inq.title, inq.scope or "")
    inq.list_hash = _hash(item)
    inq.raw_data = {k: v for k, v in {**item, **(detail or {})}.items() if k not in ("scope", "banner")}
    inq.last_refreshed = now
    return inq, created


def needs_detail(inq: CommitteeInquiry | None, item: dict, now: datetime) -> bool:
    if inq is None or inq.detail_fetched_at is None:
        return True
    if inq.list_hash != _hash(item):
        return True
    return inq.detail_fetched_at < now - timedelta(days=DETAIL_REFRESH_DAYS)


def ensure_link(committee_id: int, inquiry_id: int) -> None:
    if db.session.get(CommitteeInquiryLink, (committee_id, inquiry_id)) is None:
        db.session.add(CommitteeInquiryLink(committee_id=committee_id, inquiry_id=inquiry_id))


def _publication_url(p: dict) -> str | None:
    extra = p.get("additionalContentUrl") or ""
    if extra.startswith("https://"):
        return extra
    for doc in p.get("documents") or []:
        if isinstance(doc, dict) and doc.get("documentId"):
            return PUBLICATION_DOC_URL.format(id=p["id"], doc=doc["documentId"])
    return None


def upsert_publication(committee_id: int, p: dict, now: datetime) -> bool:
    """Write one publication for one committee. Returns created. Caller commits."""
    row = CommitteePublication.query.filter_by(publication_id=p["id"], committee_id=committee_id).first()
    created = row is None
    if created:
        row = CommitteePublication(publication_id=p["id"], committee_id=committee_id, ingested_at=now)
        db.session.add(row)
    business = next((b for b in p.get("businesses") or [] if isinstance(b, dict)), {})
    hc = (p.get("hcNumber") or {}).get("number")
    hl = (p.get("hlPaper") or {}).get("number")
    responses = p.get("governmentResponses") or {}
    row.inquiry_id = business.get("id")
    row.inquiry_title = _text(business.get("title")) or None
    row.publication_type = (p.get("type") or {}).get("name")
    row.description = _text(p.get("description")) or "Committee publication"
    row.publication_date = _dt(p.get("publicationStartDate"))
    row.paper_number = hc or hl
    row.document_url = _publication_url(p)
    row.response_deadline = None if responses.get("publication") else _d(responses.get("responseDeadline"))
    row.updated_at = now
    return created


# ── Main loop ─────────────────────────────────────────────────────────────────

def ingest(committee_ids: list[int] | None = None, limit: int | None = None, dry_run: bool = False,
           full: bool = False, api: Api | None = None, now: datetime | None = None) -> dict:
    """Run one ingest. Expects an app context. Returns a summary dict."""
    api = api or Api()
    now = now or datetime.utcnow()
    t0 = time.time()
    s = {"committees_listed": 0, "list_complete": False, "committees_new": 0, "committees_updated": 0,
         "committees_retired": 0, "memberships": 0, "inquiries_open": 0, "inquiries_new": 0,
         "inquiries_updated": 0, "inquiries_closed": 0, "inquiry_details": 0, "open_calls": 0,
         "publications_new": 0, "publications_updated": 0, "committee_failures": 0, "dry_run": dry_run}

    listed, complete = api.get_all("/Committees", {"CommitteeStatus": "Current"}, take=300)
    s["committees_listed"], s["list_complete"] = len(listed), complete
    if committee_ids:
        listed = [c for c in listed if c.get("id") in set(committee_ids)]
    if limit is not None:
        listed = listed[:limit]
    print(f"[committees-ingest] {s['committees_listed']} current committees listed (complete={complete}); "
          f"processing {len(listed)}{' | DRY RUN' if dry_run else ''}", flush=True)

    existing_committees = {cid for (cid,) in db.session.query(Committee.id).all()}
    seen_open: set[int] = set()
    incomplete_committees: set[int] = set()
    open_ids_by_committee: dict[int, set[int]] = {}

    for n, raw in enumerate(listed, 1):
        cid = raw.get("id")
        if not cid:
            continue
        stored = db.session.get(Committee, cid)
        want_detail = full or stored is None or now.weekday() == 0
        detail = api.get(f"/Committees/{cid}") if want_detail else None
        members, members_ok = api.get_all(f"/Committees/{cid}/Members", {"MembershipStatus": "Current"})
        business, business_ok = api.get_all("/CommitteeBusiness", {"CommitteeId": cid, "Status": "Open"})
        pubs_body = api.get("/Publications", {"CommitteeId": cid, "PublicationTypeIds": PUBLICATION_TYPE_IDS,
                                              "SortOrder": "PublicationDateDescending",
                                              "Take": RECENT_PUBLICATIONS})
        pubs = pubs_body.get("items") if isinstance(pubs_body, dict) else None
        if not business_ok:
            incomplete_committees.add(cid)
        if not (members_ok and business_ok and isinstance(pubs, list)) or (want_detail and detail is None):
            s["committee_failures"] += 1
        open_items = [b for b in business if b.get("id")]
        open_ids_by_committee[cid] = {b["id"] for b in open_items}
        seen_open.update(open_ids_by_committee[cid])
        s["memberships"] += len(members)

        if dry_run:
            s["committees_new" if cid not in existing_committees else "committees_updated"] += 1
            for b in open_items:
                inq = db.session.get(CommitteeInquiry, b["id"])
                s["inquiries_new" if inq is None else "inquiries_updated"] += 1
                s["inquiry_details"] += needs_detail(inq, b, now)
            continue

        try:
            c, created = upsert_committee(raw, detail if isinstance(detail, dict) else None, now)
            s["committees_new" if created else "committees_updated"] += 1
            db.session.flush()
            if members_ok:
                replace_memberships(cid, members)
            for b in open_items:
                inq = db.session.get(CommitteeInquiry, b["id"])
                bdetail = None
                if needs_detail(inq, b, now):
                    bdetail = api.get(f"/CommitteeBusiness/{b['id']}")
                    bdetail = bdetail if isinstance(bdetail, dict) else None
                    s["inquiry_details"] += bdetail is not None
                inq, i_created = upsert_inquiry(b, bdetail, now)
                s["inquiries_new" if i_created else "inquiries_updated"] += 1
                db.session.flush()
                ensure_link(cid, inq.id)
            for p in pubs or []:
                if isinstance(p, dict) and p.get("id"):
                    s["publications_new" if upsert_publication(cid, p, now) else "publications_updated"] += 1
            c.topic_slugs = topic_slugs_for(c.name, c.purpose or "",
                                            *[_text(b.get("title")) for b in open_items])
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            incomplete_committees.add(cid)
            s["committee_failures"] += 1
            logger.error("Committee %s failed: %s", cid, exc, exc_info=True)
        if n % 25 == 0:
            print(f"[committees-ingest] {n}/{len(listed)} committees | {api.ok} ok / {api.failed} failed requests",
                  flush=True)

    s["inquiries_open"] = len(seen_open)
    full_run = complete and not committee_ids and limit is None
    if not dry_run:
        if full_run:
            listed_ids = {c["id"] for c in listed if c.get("id")}
            for c in Committee.query.filter(Committee.is_current.is_(True)).all():
                if c.id not in listed_ids:
                    c.is_current = False
                    s["committees_retired"] += 1
        s["inquiries_closed"] = close_missing_inquiries(seen_open, incomplete_committees,
                                                        set(open_ids_by_committee), full_run, now)
        db.session.commit()
        s["open_calls"] = CommitteeInquiry.query.filter(CommitteeInquiry.status == "Open",
                                                        CommitteeInquiry.evidence_open.is_(True)).count()
    s.update(requests_ok=api.ok, requests_failed=api.failed, elapsed_s=round(time.time() - t0, 1))
    return s


def close_missing_inquiries(seen_open: set[int], incomplete: set[int], fetched: set[int],
                            full_run: bool, now: datetime) -> int:
    """
    Mark Open inquiries Closed when no committee listed them this run. Only
    when every committee linked to the inquiry was fetched completely (on a
    partial run: only inquiries whose committees were all part of this run).
    """
    links: dict[int, set[int]] = {}
    for cid, iid in db.session.query(CommitteeInquiryLink.committee_id, CommitteeInquiryLink.inquiry_id).all():
        links.setdefault(iid, set()).add(cid)
    closed = 0
    for inq in CommitteeInquiry.query.filter(CommitteeInquiry.status == "Open").all():
        if inq.id in seen_open:
            continue
        cids = links.get(inq.id, set())
        if cids & incomplete:
            continue
        if not full_run and not cids <= fetched:
            continue
        inq.status = "Closed"
        inq.evidence_open = False
        inq.close_date = inq.close_date or now.date()
        inq.updated_at = now
        closed += 1
    return closed


def _print_summary(s: dict) -> None:
    print("\n[committees-ingest] ========== Run complete ==========", flush=True)
    for key, value in s.items():
        print(f"  {key:<22} {value}", flush=True)


def mostly_failed(s: dict) -> bool:
    return s["requests_failed"] > s["requests_ok"] or s["committees_listed"] == 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest parliamentary committees")
    parser.add_argument("--committee", type=int, action="append", help="Only this committee id (repeatable)")
    parser.add_argument("--limit", type=int, help="Process the first N committees only")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and predict; write nothing")
    parser.add_argument("--full", action="store_true",
                        help="Re-fetch every committee's detail (purpose/contact); default: new ones + Mondays")
    parser.add_argument("--service-name", default="committees-cron", help="ha_cron_run.service_name")
    args = parser.parse_args()

    from flask_app import app
    exit_code = 0
    with app.app_context():
        print(f"[committees-ingest] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        run = None
        if not args.dry_run:
            run = HaCronRun(service_name=args.service_name, started_at=datetime.utcnow(),
                            days_window=0, status="running")
            db.session.add(run)
            db.session.commit()
        try:
            summary = ingest(committee_ids=args.committee, limit=args.limit, dry_run=args.dry_run,
                             full=args.full)
            _print_summary(summary)
            exit_code = 1 if mostly_failed(summary) else 0
            if run:
                run.sessions_ingested = summary["committees_new"] + summary["committees_updated"]
                run.errors = summary["committee_failures"] + summary["requests_failed"]
                run.notes = (f"committees={summary['committees_listed']} inquiries_open={summary['inquiries_open']} "
                             f"closed={summary['inquiries_closed']} open_calls={summary['open_calls']} "
                             f"failures={summary['committee_failures']}")
        except Exception as exc:
            db.session.rollback()
            print(f"[committees-ingest] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", flush=True)
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
