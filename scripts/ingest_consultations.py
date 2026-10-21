"""
GOV.UK consultations ingestion: GOV.UK Search API + Content API ->
consultation / consultation_topic. The web pages (/consultations) read only
from these tables. UK Government (GOV.UK) only.

Usage:
    python scripts/ingest_consultations.py                    # daily run
    python scripts/ingest_consultations.py --since 2024-07-05 # backfill: closed/outcomes since a date
    python scripts/ingest_consultations.py --limit 20         # cap detail fetches (testing)
    python scripts/ingest_consultations.py --dry-run          # list only: predict new/changed/skip, no writes

How a run works:
1. Page through the Search API (100 per page) for:
   - open_consultation: all
   - closed_consultation: closing date within the last --closed-days (default 365)
   - consultation_outcome: published within the last --outcome-days (default 180)
   With --since, closed and outcome both use "closed on/after that date".
2. Fetch /api/content/<path> when the consultation is new, its status changed,
   GOV.UK's public timestamp is newer than ours, or it is open (open ones are
   always refreshed: ways to respond and documents change). Rows we hold as
   open that no listing returned are re-fetched too; a 404/410 marks them
   withdrawn.
3. Upsert by GOV.UK content_id; map to policy areas (deterministic, see
   consultations.assign_policy_areas) and replace consultation_topic rows.

Re-running is safe: unchanged consultations are skipped. Calls are paced
(GOV.UK asks for a sensible rate) and retried with backoff on 429/5xx.
Exits 1 if most requests fail or the run hits a fatal error.

Content: Crown copyright, Open Government Licence v3.0.
Railway: run with SKIP_MIGRATIONS=1 after migrations/consultation_tables.py --execute.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy.orm.attributes import flag_modified

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consultations import (  # noqa: E402
    DOC_TYPE_STATUS, STATUS_OPEN, Consultation, ConsultationTopic, assign_policy_areas,
)
from extensions import db  # noqa: E402
from hansard_archive.models import HaCronRun  # noqa: E402

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.gov.uk/api/search.json"
CONTENT_URL = "https://www.gov.uk/api/content"
GOVUK = "https://www.gov.uk"
SEARCH_FIELDS = ("title,link,description,organisations,public_timestamp,content_id,"
                 "start_date,end_date")
PAGE_SIZE = 100
MAX_PAGES = 100               # safety stop: 10,000 rows per document type
REQUEST_TIMEOUT = 30
INTER_REQUEST_DELAY = 0.25
MAX_RETRIES = 3
DEFAULT_CLOSED_DAYS = 365
DEFAULT_OUTCOME_DAYS = 180
USER_AGENT = "WestminsterBrief/1.0 (+https://westminsterbrief.co.uk/about)"


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Api:
    """Paced GETs with retry/backoff. Counts outcomes for the exit-code rule."""

    def __init__(self, sleep=time.sleep):
        self.ok = 0
        self.failed = 0
        self._sleep = sleep

    def get(self, url: str, params: dict | None = None) -> tuple[dict | None, int | None]:
        """(parsed JSON object or None, HTTP status or None). 404/410 count as ok."""
        reason = ""
        for attempt in range(1, MAX_RETRIES + 1):
            self._sleep(INTER_REQUEST_DELAY)
            try:
                resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                if resp.status_code == 200:
                    body = resp.json()
                    if isinstance(body, dict):
                        self.ok += 1
                        return body, 200
                    reason = "non-object JSON"
                elif resp.status_code in (404, 410):
                    self.ok += 1
                    return None, resp.status_code
                elif resp.status_code in (429, 500, 502, 503, 504):
                    reason = f"HTTP {resp.status_code}"
                else:
                    logger.warning("GET %s -> HTTP %d (not retried)", url, resp.status_code)
                    self.failed += 1
                    return None, resp.status_code
            except (requests.RequestException, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                delay = 2 ** attempt
                print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {url}: {reason}; waiting {delay}s", flush=True)
                self._sleep(delay)
            else:
                print(f"  [failed] {url}: {reason}", flush=True)
        self.failed += 1
        return None, None


def list_type(api: Api, doc_type: str, extra: dict | None = None) -> tuple[list[dict], bool]:
    """All Search API rows for one document type. Returns (rows, complete)."""
    rows: list[dict] = []
    start = 0
    for _ in range(MAX_PAGES):
        params = {
            "filter_content_store_document_type": doc_type,
            "fields": SEARCH_FIELDS,
            "order": "-public_timestamp",
            "count": PAGE_SIZE,
            "start": start,
            **(extra or {}),
        }
        body, _status = api.get(SEARCH_URL, params)
        page = body.get("results") if body else None
        if not isinstance(page, list):
            return rows, False
        for r in page:
            if isinstance(r, dict) and r.get("content_id") and r.get("link"):
                rows.append({**r, "_doc_type": doc_type})
        total = int(body.get("total") or 0)
        start += PAGE_SIZE
        print(f"[consultations-ingest] {doc_type}: {min(start, total)}/{total}", flush=True)
        if len(page) < PAGE_SIZE or start >= total:
            return rows, True
    return rows, False


def list_all(api: Api, closed_from: date, outcome_from: date, outcome_by_closing: bool
             ) -> tuple[list[dict], bool]:
    """Open + recently closed + recent outcomes, de-duplicated by content_id."""
    plan = [
        ("open_consultation", {}),
        ("closed_consultation", {"filter_end_date": f"from:{closed_from.isoformat()}"}),
        ("consultation_outcome",
         {"filter_end_date": f"from:{outcome_from.isoformat()}"} if outcome_by_closing
         else {"filter_public_timestamp": f"from:{outcome_from.isoformat()}"}),
    ]
    seen: dict[str, dict] = {}
    complete = True
    for doc_type, extra in plan:
        rows, ok = list_type(api, doc_type, extra)
        complete = complete and ok
        for r in rows:
            seen.setdefault(r["content_id"], r)
    return list(seen.values()), complete


# ── Parsing ───────────────────────────────────────────────────────────────────

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def clean(s: str | None) -> str:
    """Strip zero-width characters (GOV.UK titles carry them) and collapse whitespace."""
    s = (s or "").translate(_INVISIBLE).replace(" ", " ")
    return " ".join(s.split())


def parse_dt(value: str | None) -> datetime | None:
    """ISO 8601 with offset or Z -> naive UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def html_to_text(html: str | None) -> str:
    """govspeak HTML -> plain paragraphs separated by blank lines (list items as bullets)."""
    if not html:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    for el in soup.find_all(["p", "li", "h2", "h3", "h4"]):
        if el.name == "p" and el.find_parent("li"):
            continue
        t = clean(el.get_text(" "))
        if not t:
            continue
        blocks.append(("• " + t) if el.name == "li" else t)
    if not blocks:
        t = clean(soup.get_text(" "))
        return t
    return "\n\n".join(blocks)


def _abs(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return url if url.startswith(("http://", "https://")) else GOVUK + ("" if url.startswith("/") else "/") + url


def _document(a: dict) -> dict:
    kind = a.get("attachment_type") or "file"
    return {
        "id": str(a.get("id") or ""),
        "title": clean(a.get("title")) or "Document",
        "url": _abs(a.get("url")),
        "kind": kind,                                  # html | file | external
        "content_type": a.get("content_type") or "",
        "file_size": a.get("file_size"),
        "pages": a.get("number_of_pages"),
        "accessible": a.get("accessible"),
    }


def _taxons(links: dict) -> list[dict]:
    out = []
    for t in links.get("taxons") or []:
        if not isinstance(t, dict):
            continue
        ancestors, node = [], t
        for _ in range(10):
            parents = (node.get("links") or {}).get("parent_taxons") or []
            if not parents:
                break
            node = parents[0]
            if node.get("base_path"):
                ancestors.append(node["base_path"])
        out.append({"title": clean(t.get("title")), "base_path": t.get("base_path") or "",
                    "ancestors": ancestors})
    return out


def _organisations(search_row: dict | None, links: dict) -> list[dict]:
    """Prefer the Search API's richer org objects (acronym, type, parents); fall back to content links."""
    raw = (search_row or {}).get("organisations") or []
    out = []
    for o in raw:
        if isinstance(o, dict) and o.get("slug"):
            out.append({"title": clean(o.get("title")), "acronym": o.get("acronym") or "",
                        "slug": o["slug"], "type": o.get("organisation_type") or "",
                        "parents": list(o.get("parent_organisations") or [])})
    if out:
        lead = (links.get("primary_publishing_organisation") or [{}])[0].get("base_path", "")
        lead_slug = lead.rsplit("/", 1)[-1] if lead else ""
        out.sort(key=lambda o: o["slug"] != lead_slug)
        return out
    for o in links.get("organisations") or []:
        if isinstance(o, dict) and o.get("base_path"):
            details = o.get("details") or {}
            out.append({"title": clean(o.get("title")), "acronym": details.get("acronym") or "",
                        "slug": o["base_path"].rsplit("/", 1)[-1], "type": "", "parents": []})
    return out


def _applies_to(details: dict) -> list[str]:
    na = details.get("national_applicability")
    if not isinstance(na, dict):
        return []
    return [v.get("label") for v in na.values() if isinstance(v, dict) and v.get("applicable") and v.get("label")]


def _postal(value) -> str:
    if isinstance(value, str):
        return html_to_text(value).replace("\n\n", "\n") if "<" in value else value.strip()
    return ""


def slug_for(base_path: str) -> str:
    slug = base_path.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
    return slug[:200] or "consultation"


def parse_content(content: dict, search_row: dict | None = None) -> dict:
    """GOV.UK Content API consultation -> column values (no DB access)."""
    details = content.get("details") or {}
    links = content.get("links") or {}
    doc_type = content.get("document_type") or (search_row or {}).get("_doc_type") or ""
    status = DOC_TYPE_STATUS.get(doc_type, "closed")

    attachments = {str(a.get("id")): a for a in details.get("attachments") or [] if isinstance(a, dict)}
    outcome_ids = [str(i) for i in details.get("final_outcome_attachments") or []]
    featured = [str(i) for i in details.get("featured_attachments") or []]
    doc_ids = [i for i in (featured or list(attachments)) if i not in outcome_ids]
    documents = [_document(attachments[i]) for i in doc_ids if i in attachments]
    outcome_documents = [_document(attachments[i]) for i in outcome_ids if i in attachments]

    ways = details.get("ways_to_respond") or {}
    orgs = _organisations(search_row, links)
    taxons = _taxons(links)
    title = clean(content.get("title")) or "Untitled consultation"
    summary = clean(content.get("description"))
    outcome_summary = html_to_text(details.get("final_outcome_detail"))
    outcome_url = ""
    if outcome_documents:
        outcome_url = outcome_documents[0]["url"]
    elif status == "outcome":
        outcome_url = GOVUK + content.get("base_path", "")
    withdrawn = content.get("withdrawn_notice") or {}

    return {
        "content_id": content["content_id"],
        "base_path": content["base_path"],
        "title": title,
        "summary": summary or None,
        "body_text": html_to_text(details.get("body")) or None,
        "status": status,
        "opening_date": parse_dt(details.get("opening_date") or (search_row or {}).get("start_date")),
        "closing_date": parse_dt(details.get("closing_date") or (search_row or {}).get("end_date")),
        "lead_org_slug": orgs[0]["slug"] if orgs else None,
        "lead_org_title": orgs[0]["title"] if orgs else None,
        "organisations": orgs,
        "taxons": taxons,
        "response_url": _abs(ways.get("link_url")) or None,
        "response_email": (ways.get("email") or "").strip() or None,
        "response_postal": _postal(ways.get("postal_address")) or None,
        "response_form_url": _abs(ways.get("attachment_url")) or None,
        "documents": documents,
        "outcome_url": outcome_url or None,
        "outcome_summary": outcome_summary or None,
        "outcome_documents": outcome_documents,
        "applies_to": _applies_to(details),
        "is_withdrawn": bool(withdrawn.get("explanation")) if isinstance(withdrawn, dict) else False,
        "govuk_updated_at": parse_dt(content.get("public_updated_at")),
        "first_published_at": parse_dt(content.get("first_published_at")),
        "policy_areas": assign_policy_areas(title, summary, taxons, orgs),
        # Body and attachments are already parsed into columns; keep the rest.
        "raw_data": {
            "document_type": doc_type,
            "schema_name": content.get("schema_name"),
            "public_updated_at": content.get("public_updated_at"),
            "details": {k: v for k, v in details.items() if k not in ("body", "attachments")},
            "withdrawn_notice": withdrawn or None,
        },
    }


# ── Upsert ────────────────────────────────────────────────────────────────────

def _unique_slug(slug: str, content_id: str) -> str:
    with db.session.no_autoflush:   # the row being written has no slug yet
        clash = Consultation.query.filter(Consultation.slug == slug,
                                          Consultation.content_id != content_id).first()
    return f"{slug}-{content_id[:8]}" if clash else slug


def upsert(values: dict, now: datetime) -> tuple[Consultation, bool]:
    """Write one parsed consultation and its topic rows. Returns (row, created). Caller commits."""
    c = Consultation.query.filter(Consultation.content_id == values["content_id"]).first()
    created = c is None
    if created:
        c = Consultation(content_id=values["content_id"], ingested_at=now)
        db.session.add(c)
    areas = values["policy_areas"]
    area_slugs = [a["slug"] for a in areas]
    needs_slug = created or not c.slug or c.base_path != values["base_path"]
    fields = {k: v for k, v in values.items() if k not in ("content_id", "policy_areas", "raw_data")}
    changed = created or c.policy_areas != area_slugs or any(
        getattr(c, k) != v for k, v in fields.items())
    previous_update = c.updated_at
    for key, value in values.items():
        if key not in ("content_id", "policy_areas"):
            setattr(c, key, value)
    if needs_slug:
        c.slug = _unique_slug(slug_for(values["base_path"]), values["content_id"])
    c.policy_areas = area_slugs
    c.last_refreshed = now
    # updated_at feeds sitemap lastmod: move it only when something visible changed.
    c.updated_at = now if changed or previous_update is None else previous_update
    if not created:
        flag_modified(c, "updated_at")   # else the model's onupdate overrides an unchanged value
    db.session.flush()

    ConsultationTopic.query.filter(ConsultationTopic.consultation_id == c.id).delete(
        synchronize_session=False)
    for a in areas:
        db.session.add(ConsultationTopic(consultation_id=c.id, policy_area=a["slug"],
                                         source=a["source"], score=a["score"]))
    return c, created


def mark_withdrawn(content_id: str, now: datetime) -> bool:
    c = Consultation.query.filter(Consultation.content_id == content_id).first()
    if c is None or c.is_withdrawn:
        return False
    c.is_withdrawn = True
    c.last_refreshed = now
    c.updated_at = now
    return True


# ── Planning ──────────────────────────────────────────────────────────────────

def plan_fetches(listed: list[dict]) -> dict[str, list[dict]]:
    """Split listed rows into new / changed / open_refresh / skip against the DB, plus stale_open."""
    stored = {cid: (status, updated, withdrawn) for cid, status, updated, withdrawn in
              db.session.query(Consultation.content_id, Consultation.status,
                               Consultation.govuk_updated_at, Consultation.is_withdrawn).all()}
    plan: dict[str, list[dict]] = {"new": [], "changed": [], "open_refresh": [], "skip": [],
                                   "stale_open": []}
    listed_ids = set()
    for r in listed:
        cid = r["content_id"]
        listed_ids.add(cid)
        status = DOC_TYPE_STATUS.get(r["_doc_type"])
        if cid not in stored:
            plan["new"].append(r)
            continue
        old_status, old_updated, withdrawn = stored[cid]
        ts = parse_dt(r.get("public_timestamp"))
        if status != old_status or withdrawn or (ts and (old_updated is None or ts > old_updated)):
            plan["changed"].append(r)
        elif status == STATUS_OPEN:
            plan["open_refresh"].append(r)
        else:
            plan["skip"].append(r)
    for cid, (status, _updated, withdrawn) in stored.items():
        if status == STATUS_OPEN and not withdrawn and cid not in listed_ids:
            base_path = db.session.query(Consultation.base_path).filter(
                Consultation.content_id == cid).scalar()
            plan["stale_open"].append({"content_id": cid, "link": base_path, "_doc_type": None})
    return plan


def open_by_department(listed: list[dict]) -> list[tuple[str, int]]:
    """Open consultations per lead organisation (first listed org), from search rows only."""
    counts: Counter = Counter()
    for r in listed:
        if r["_doc_type"] != "open_consultation":
            continue
        orgs = r.get("organisations") or [{}]
        counts[clean(orgs[0].get("title")) or "Unknown"] += 1
    return counts.most_common()


# ── Main loop ─────────────────────────────────────────────────────────────────

def ingest(closed_days: int = DEFAULT_CLOSED_DAYS, outcome_days: int = DEFAULT_OUTCOME_DAYS,
           since: date | None = None, limit: int | None = None, dry_run: bool = False,
           api: Api | None = None, today: date | None = None) -> dict:
    """Run one ingest. Expects an app context. Returns a summary dict."""
    from sqlalchemy.exc import OperationalError

    api = api or Api()
    today = today or date.today()
    t0 = time.time()
    closed_from = since or (today - timedelta(days=closed_days))
    outcome_from = since or (today - timedelta(days=outcome_days))
    print(f"[consultations-ingest] closed since {closed_from} | outcomes "
          f"{'closed' if since else 'published'} since {outcome_from}"
          f"{' | DRY RUN' if dry_run else ''}", flush=True)

    listed, complete = list_all(api, closed_from, outcome_from, outcome_by_closing=since is not None)
    plan = plan_fetches(listed)
    by_type = Counter(r["_doc_type"] for r in listed)
    print(f"[consultations-ingest] listed {len(listed)} (complete={complete}) | "
          + " | ".join(f"{k} {v}" for k, v in sorted(by_type.items()))
          + f" | new {len(plan['new'])} | changed {len(plan['changed'])} | "
          f"open refresh {len(plan['open_refresh'])} | stale open {len(plan['stale_open'])} | "
          f"unchanged {len(plan['skip'])}", flush=True)

    summary = {"listed": len(listed), "list_complete": complete,
               "listed_open": by_type.get("open_consultation", 0),
               "listed_closed": by_type.get("closed_consultation", 0),
               "listed_outcome": by_type.get("consultation_outcome", 0),
               **{f"planned_{k}": len(v) for k, v in plan.items()},
               "inserted": 0, "updated": 0, "withdrawn": 0, "not_found": 0, "failed": 0,
               "open_by_department": open_by_department(listed), "dry_run": dry_run}

    to_fetch = plan["new"] + plan["changed"] + plan["open_refresh"] + plan["stale_open"]
    if limit is not None:
        to_fetch = to_fetch[:limit]
    if dry_run:
        summary.update(requests_ok=api.ok, requests_failed=api.failed,
                       elapsed_s=round(time.time() - t0, 1))
        return summary

    for n, row in enumerate(to_fetch, 1):
        content, status = api.get(CONTENT_URL + row["link"])
        now = datetime.utcnow()
        if content is None:
            if status in (404, 410):
                summary["not_found"] += 1
                if mark_withdrawn(row["content_id"], now):
                    summary["withdrawn"] += 1
                db.session.commit()
            else:
                summary["failed"] += 1
            continue
        if content.get("schema_name") != "consultation" or not content.get("content_id"):
            summary["not_found"] += 1
            continue
        for attempt in (1, 2):
            try:
                _, created = upsert(parse_content(content, row if row.get("_doc_type") else None), now)
                db.session.commit()
                summary["inserted" if created else "updated"] += 1
                break
            except OperationalError as exc:
                db.session.rollback()
                db.engine.dispose()
                if attempt == 2:
                    logger.error("DB error on %s: %s", row["link"], exc)
                    summary["failed"] += 1
            except Exception as exc:
                db.session.rollback()
                logger.error("Ingest error on %s: %s", row["link"], exc, exc_info=True)
                summary["failed"] += 1
                break
        if n % 50 == 0:
            rate = n / max(time.time() - t0, 1)
            print(f"[consultations-ingest] {n}/{len(to_fetch)} detail fetches | {rate:.2f}/s", flush=True)

    summary.update(requests_ok=api.ok, requests_failed=api.failed,
                   elapsed_s=round(time.time() - t0, 1))
    return summary


def _print_summary(s: dict) -> None:
    print("\n[consultations-ingest] ========== Run complete ==========", flush=True)
    for key in ("listed", "list_complete", "listed_open", "listed_closed", "listed_outcome",
                "planned_new", "planned_changed", "planned_open_refresh", "planned_stale_open",
                "planned_skip", "inserted", "updated", "withdrawn", "not_found", "failed",
                "requests_ok", "requests_failed", "elapsed_s"):
        print(f"  {key:<22} {s.get(key)}", flush=True)
    if s.get("open_by_department"):
        print("  open consultations by lead organisation:", flush=True)
        for org, n in s["open_by_department"]:
            print(f"    {n:>3}  {org}", flush=True)


def mostly_failed(s: dict) -> bool:
    return s["requests_failed"] > s["requests_ok"] or (not s["list_complete"] and s["listed"] == 0)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest GOV.UK consultations")
    parser.add_argument("--closed-days", type=int, default=DEFAULT_CLOSED_DAYS,
                        help="Closed consultations whose closing date is in the last N days (default 365)")
    parser.add_argument("--outcome-days", type=int, default=DEFAULT_OUTCOME_DAYS,
                        help="Outcomes published in the last N days (default 180)")
    parser.add_argument("--since", type=date.fromisoformat,
                        help="Backfill: closed consultations and outcomes that closed on/after YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Max detail fetches")
    parser.add_argument("--dry-run", action="store_true", help="List only; predict, write nothing")
    parser.add_argument("--service-name", default="consultations-cron", help="ha_cron_run.service_name")
    args = parser.parse_args()

    from flask_app import app
    exit_code = 0
    with app.app_context():
        print(f"[consultations-ingest] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        run = None
        if not args.dry_run:
            run = HaCronRun(service_name=args.service_name, started_at=datetime.utcnow(),
                            days_window=args.closed_days, status="running")
            db.session.add(run)
            db.session.commit()
        try:
            summary = ingest(closed_days=args.closed_days, outcome_days=args.outcome_days,
                             since=args.since, limit=args.limit, dry_run=args.dry_run)
            _print_summary(summary)
            exit_code = 1 if mostly_failed(summary) else 0
            if run:
                run.sessions_ingested = summary["inserted"] + summary["updated"]
                run.errors = summary["failed"] + summary["requests_failed"]
                run.notes = (f"listed={summary['listed']} inserted={summary['inserted']} "
                             f"updated={summary['updated']} withdrawn={summary['withdrawn']} "
                             f"failed={summary['failed']}")
        except Exception as exc:
            db.session.rollback()
            print(f"[consultations-ingest] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                  flush=True)
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
