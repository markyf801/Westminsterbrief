"""
Written ministerial statements ingestion: Parliament's Written Questions and
Statements API -> ha_written_statement. The web pages (/written-statements)
and Debate Prep read only from this table.

Usage:
    python scripts/ingest_written_statements.py                       # daily: statements made in the last 7 days
    python scripts/ingest_written_statements.py --days 14             # wider rolling window
    python scripts/ingest_written_statements.py --since 2024-07-04    # backfill (start of the current Parliament)
    python scripts/ingest_written_statements.py --limit 50            # cap detail fetches (testing)
    python scripts/ingest_written_statements.py --dry-run             # list only: predict new/update/skip, no writes

How a run works:
1. Page through /api/writtenstatements/statements (100 per page) for the
   date window. The list gives every field except the full text (it is cut
   to ~250 characters) and the minister's name.
2. Fetch /api/writtenstatements/statements/{id}?expandMember=true only when
   the statement is new, its title / linked statements / attachments differ
   from what we hold, or it was made in the last --refresh-days (default 2).
3. Upsert by Parliament's statement id. Sanitise the HTML (same allowlist as
   written answers), keep a plain-text copy, map policy areas from the
   department + keywords (no AI calls).

Re-running is safe and resumes a broken backfill: statements we already hold
and that have not changed are skipped. Calls are paced; 5xx, timeouts and
connection errors are retried with backoff, other 4xx are not, and a 429
stops the run (CLAUDE.md: rate limited = stop, don't loop). Exits 1 if most
requests fail, on a 429, or on a fatal error. Logs a row in ha_cron_run (not
in --dry-run).

Railway: run with SKIP_MIGRATIONS=1 after migrations/written_statement_tables.py --execute.
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extensions import db  # noqa: E402
from hansard_archive.html_sanitizer import sanitize_answer_html, to_plain_text  # noqa: E402
from hansard_archive.models import HaCronRun  # noqa: E402
from written_statements import WrittenStatement, policy_areas_for  # noqa: E402

logger = logging.getLogger(__name__)

API_BASE = "https://questions-statements-api.parliament.uk/api/writtenstatements/statements"
REQUEST_TIMEOUT = 60          # list pages of 100 take 10-15s
INTER_REQUEST_DELAY = 0.5
MAX_RETRIES = 3
LIST_PAGE_SIZE = 100
DEFAULT_DAYS = 7
DEFAULT_REFRESH_DAYS = 2
PARLIAMENT_START = date(2024, 7, 4)   # general election; the backfill start

# Fields whose change moves updated_at (sitemap lastmod).
_CONTENT_FIELDS = ("uin", "date_made", "house", "answering_body_id", "answering_body_name",
                   "member_id", "member_name", "member_party", "member_role", "title",
                   "text_html", "notice_number", "attachments", "linked_statements")


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Api:
    """Paced GETs with retry/backoff. Counts outcomes for the exit-code rule."""

    def __init__(self, sleep=time.sleep):
        self.ok = 0
        self.failed = 0
        self.rate_limited = False
        self._sleep = sleep

    def get(self, url: str, params: dict | None = None) -> dict | None:
        """Parsed JSON object, or None after MAX_RETRIES failures / a 4xx / a 429."""
        if self.rate_limited:
            return None
        for attempt in range(1, MAX_RETRIES + 1):
            self._sleep(INTER_REQUEST_DELAY)
            try:
                resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    body = resp.json()
                    if isinstance(body, dict):
                        self.ok += 1
                        return body
                    reason = "non-object JSON"
                elif resp.status_code == 429:
                    print(f"  [stopped] {url}: HTTP 429 rate limited; ending the run", flush=True)
                    self.rate_limited = True
                    self.failed += 1
                    return None
                elif resp.status_code in (500, 502, 503, 504):
                    reason = f"HTTP {resp.status_code}"
                else:
                    logger.warning("GET %s -> HTTP %d (not retried)", url, resp.status_code)
                    self.failed += 1
                    return None
            except (requests.RequestException, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                delay = 2 ** attempt
                print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {url}: {reason}; waiting {delay}s", flush=True)
                self._sleep(delay)
            else:
                print(f"  [failed] {url}: {reason}", flush=True)
        self.failed += 1
        return None


def list_statements(api: Api, start: date, end: date | None) -> tuple[list[dict], bool]:
    """Every statement made in [start, end] (list shape). Returns (rows, complete)."""
    rows: dict[int, dict] = {}
    skip = 0
    while True:
        params = {"madeWhenFrom": start.isoformat(), "take": LIST_PAGE_SIZE, "skip": skip,
                  "sessionStatus": "Any"}
        if end:
            params["madeWhenTo"] = end.isoformat()
        body = api.get(API_BASE, params)
        page = body.get("results") if body else None
        if not isinstance(page, list):
            return list(rows.values()), False
        for item in page:
            v = item.get("value") if isinstance(item, dict) else None
            if isinstance(v, dict) and v.get("id"):
                rows[v["id"]] = v          # de-duplicates if paging shifts mid-run
        total = int(body.get("totalResults") or 0)
        skip += LIST_PAGE_SIZE
        print(f"[wms-ingest] list: {min(skip, total)}/{total}", flush=True)
        if len(page) < LIST_PAGE_SIZE or skip >= total:
            return list(rows.values()), True


def fetch_detail(api: Api, statement_id: int) -> tuple[dict | None, bool]:
    """(statement, request_ok). Statement is None for failures or an unexpected body."""
    body = api.get(f"{API_BASE}/{statement_id}", {"expandMember": "true", "sessionStatus": "Any"})
    if body is None:
        return None, False
    raw = body.get("value")
    if not isinstance(raw, dict) or not raw.get("id"):
        return None, True
    return raw, True


# ── Parsing ───────────────────────────────────────────────────────────────────

def _date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(value[:19]).date() if value else None
    except ValueError:
        return None


def _clean(value) -> str | None:
    value = " ".join(str(value or "").split())
    return value or None


def html_to_plain(text_html: str | None) -> str:
    """Sanitised HTML -> plain paragraphs (entities decoded, one blank line between blocks)."""
    plain = html.unescape(to_plain_text(text_html)).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in plain.split("\n")]
    return "\n\n".join(line for line in lines if line)


def _attachment(a: dict) -> dict | None:
    url = (a.get("url") or "").strip()
    if not url.startswith("https://"):
        return None
    return {
        "url": quote(url, safe=":/?=&#%~+"),     # Parliament's file URLs contain spaces
        "title": _clean(a.get("title")) or "Attachment",
        "file_type": _clean(a.get("fileType")) or "",
        "file_size": a.get("fileSizeBytes"),
    }


def _linked(links: list | None) -> list[dict]:
    out = []
    for link in links or []:
        if isinstance(link, dict) and link.get("linkedStatementId"):
            made = _date(link.get("linkDate"))
            out.append({"id": link["linkedStatementId"], "link_type": link.get("linkType") or "",
                        "link_date": made.isoformat() if made else None})
    return out


def parse(raw: dict) -> dict:
    """API statement (detail shape) -> column values."""
    member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
    text_html = sanitize_answer_html(raw.get("text") or "")
    text_plain = html_to_plain(text_html) or None
    title = _title(raw)
    body_id = raw.get("answeringBodyId")
    return {
        "id": raw["id"],
        "uin": (_clean(raw.get("uin")) or str(raw["id"]))[:20],
        "date_made": _date(raw.get("dateMade")),
        "house": _clean(raw.get("house")) or "Commons",
        "answering_body_id": body_id,
        "answering_body_name": _clean(raw.get("answeringBodyName")),
        "member_id": raw.get("memberId"),
        "member_name": _clean(member.get("name")),
        "member_party": _clean(member.get("party")),
        "member_role": _clean(raw.get("memberRole")),
        "title": title,
        "text_html": text_html,
        "text_plain": text_plain,
        "notice_number": raw.get("noticeNumber") or None,
        "attachments": _attachments(raw),
        "linked_statements": _linked(raw.get("linkedStatements")),
        "policy_areas": policy_areas_for(title, text_plain or "", body_id),
        "raw_data": {k: v for k, v in raw.items() if k != "text"},
    }


def upsert(values: dict, now: datetime) -> tuple[WrittenStatement, bool, bool]:
    """Write one statement. Returns (row, created, content_changed). Caller commits."""
    if values["date_made"] is None:
        raise ValueError(f"statement {values['id']} has no dateMade")
    ws = db.session.get(WrittenStatement, values["id"])
    created = ws is None
    if created:
        ws = WrittenStatement(id=values["id"], ingested_at=now)
        db.session.add(ws)
    changed = created or any(getattr(ws, f) != values[f] for f in _CONTENT_FIELDS)
    for key, value in values.items():
        if key != "id":
            setattr(ws, key, value)
    ws.last_refreshed = now
    if changed:
        ws.updated_at = now
    return ws, created, changed


# ── Planning ──────────────────────────────────────────────────────────────────

def _title(raw: dict) -> str:
    return _clean(raw.get("title")) or f"Written statement {raw.get('uin') or raw['id']}"


def _attachments(raw: dict) -> list[dict]:
    return [x for x in (_attachment(a) for a in raw.get("attachments") or [] if isinstance(a, dict)) if x]


def _list_signature(v: dict) -> tuple:
    """What the list shows that we can compare with our copy (same cleaning as parse())."""
    return _title(v), len(_linked(v.get("linkedStatements"))), len(_attachments(v))


def plan_fetches(listed: list[dict], refresh_days: int, today: date) -> dict[str, list[int]]:
    """Split listed statements into new / changed / recent / skip against the DB."""
    ids = [v["id"] for v in listed]
    stored: dict[int, tuple] = {}
    for i in range(0, len(ids), 500):
        rows = (db.session.query(WrittenStatement.id, WrittenStatement.title,
                                 WrittenStatement.linked_statements, WrittenStatement.attachments)
                .filter(WrittenStatement.id.in_(ids[i:i + 500])).all())
        for sid, title, linked, attachments in rows:
            stored[sid] = (title or "", len(linked or []), len(attachments or []))
    cutoff = today - timedelta(days=refresh_days)
    plan: dict[str, list[int]] = {"new": [], "changed": [], "recent": [], "skip": []}
    for v in listed:
        sid = v["id"]
        made = _date(v.get("dateMade"))
        if sid not in stored:
            plan["new"].append(sid)
        elif _list_signature(v) != stored[sid]:
            plan["changed"].append(sid)
        elif made and made >= cutoff:
            plan["recent"].append(sid)
        else:
            plan["skip"].append(sid)
    return plan


# ── Main loop ─────────────────────────────────────────────────────────────────

def ingest(start: date, end: date | None = None, refresh_days: int = DEFAULT_REFRESH_DAYS,
           limit: int | None = None, dry_run: bool = False, api: Api | None = None,
           today: date | None = None) -> dict:
    """Run one ingest. Expects an app context. Returns a summary dict."""
    from sqlalchemy.exc import OperationalError

    api = api or Api()
    today = today or date.today()
    t0 = time.time()
    print(f"[wms-ingest] window {start} -> {end or 'today'} | refresh last {refresh_days} days"
          f"{' | DRY RUN' if dry_run else ''}", flush=True)

    listed, complete = list_statements(api, start, end)
    plan = plan_fetches(listed, refresh_days, today)
    print(f"[wms-ingest] listed {len(listed)} (complete={complete}) | new {len(plan['new'])} | "
          f"changed {len(plan['changed'])} | recent refresh {len(plan['recent'])} | "
          f"unchanged {len(plan['skip'])}", flush=True)

    to_fetch = plan["new"] + plan["changed"] + plan["recent"]
    if limit is not None:
        to_fetch = to_fetch[:limit]

    summary = {"listed": len(listed), "list_complete": complete,
               **{f"planned_{k}": len(v) for k, v in plan.items()},
               "inserted": 0, "updated": 0, "unchanged": 0, "not_found": 0, "failed": 0,
               "dry_run": dry_run}
    if dry_run:
        summary.update(requests_ok=api.ok, requests_failed=api.failed, rate_limited=api.rate_limited,
                       elapsed_s=round(time.time() - t0, 1))
        return summary

    for n, sid in enumerate(to_fetch, 1):
        if api.rate_limited:
            break
        raw, ok = fetch_detail(api, sid)
        if raw is None:
            summary["failed" if not ok else "not_found"] += 1
            continue
        for attempt in (1, 2):
            try:
                _, created, changed = upsert(parse(raw), datetime.utcnow())
                db.session.commit()
                summary["inserted" if created else ("updated" if changed else "unchanged")] += 1
                break
            except OperationalError as exc:
                db.session.rollback()
                db.engine.dispose()
                if attempt == 2:
                    logger.error("DB error on statement %d: %s", sid, exc)
                    summary["failed"] += 1
            except Exception as exc:
                db.session.rollback()
                logger.error("Ingest error on statement %d: %s", sid, exc, exc_info=True)
                summary["failed"] += 1
                break
        if n % 100 == 0:
            rate = n / max(time.time() - t0, 1)
            print(f"[wms-ingest] {n}/{len(to_fetch)} detail fetches | {rate:.2f}/s | "
                  f"ETA {int((len(to_fetch) - n) / rate)}s", flush=True)

    summary.update(requests_ok=api.ok, requests_failed=api.failed, rate_limited=api.rate_limited,
                   elapsed_s=round(time.time() - t0, 1))
    return summary


def _print_summary(s: dict) -> None:
    print("\n[wms-ingest] ========== Run complete ==========", flush=True)
    for key in ("listed", "list_complete", "planned_new", "planned_changed", "planned_recent",
                "planned_skip", "inserted", "updated", "unchanged", "not_found", "failed",
                "requests_ok", "requests_failed", "rate_limited", "elapsed_s"):
        print(f"  {key:<16} {s.get(key)}", flush=True)


def mostly_failed(s: dict) -> bool:
    return (s["requests_failed"] > s["requests_ok"] or s.get("rate_limited", False)
            or (not s["list_complete"] and s["listed"] == 0))


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest written ministerial statements")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Rolling window: statements made in the last N days (default {DEFAULT_DAYS})")
    window.add_argument("--since", type=date.fromisoformat,
                        help=f"Made on/after YYYY-MM-DD (backfill; the current Parliament began {PARLIAMENT_START})")
    parser.add_argument("--refresh-days", type=int, default=DEFAULT_REFRESH_DAYS,
                        help=f"Always re-fetch statements made in the last N days (default {DEFAULT_REFRESH_DAYS})")
    parser.add_argument("--limit", type=int, help="Max detail fetches")
    parser.add_argument("--dry-run", action="store_true", help="List only; predict, write nothing")
    parser.add_argument("--service-name", default="wms-cron", help="ha_cron_run.service_name")
    args = parser.parse_args()

    today = date.today()
    start = args.since or today - timedelta(days=args.days)

    from flask_app import app
    exit_code = 0
    with app.app_context():
        print(f"[wms-ingest] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        run = None
        if not args.dry_run:
            run = HaCronRun(service_name=args.service_name, started_at=datetime.utcnow(),
                            days_window=(today - start).days, status="running")
            db.session.add(run)
            db.session.commit()
        try:
            summary = ingest(start, None, refresh_days=args.refresh_days,
                             limit=args.limit, dry_run=args.dry_run, today=today)
            _print_summary(summary)
            exit_code = 1 if mostly_failed(summary) else 0
            if run:
                run.sessions_ingested = summary["inserted"] + summary["updated"]
                run.errors = summary["failed"] + summary["requests_failed"]
                run.notes = (f"listed={summary['listed']} inserted={summary['inserted']} "
                             f"updated={summary['updated']} unchanged={summary['unchanged']} "
                             f"failed={summary['failed']}")
        except Exception as exc:
            db.session.rollback()
            print(f"[wms-ingest] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", flush=True)
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
