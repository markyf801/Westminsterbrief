"""
Early Day Motions ingestion: Parliament's Oral Questions and Motions API ->
ha_edm / ha_edm_sponsor. The web pages (/edms) read only from these tables.

Usage:
    python scripts/ingest_edms.py                      # daily run: current session
    python scripts/ingest_edms.py --backfill           # current + previous session
    python scripts/ingest_edms.py --session 2024-26    # one session
    python scripts/ingest_edms.py --since 2026-09-01   # EDMs tabled on/after a date
    python scripts/ingest_edms.py --limit 50           # cap detail fetches (testing)
    python scripts/ingest_edms.py --dry-run            # list only: predict new/update/skip, no writes

How a run works:
1. Page through /EarlyDayMotions/list (Published + Withdrawn, oldest first,
   100 per page) for the date window.
2. Fetch /EarlyDayMotion/{id} (sponsors, amendments) only when the EDM is new,
   its status changed, its listed signature total differs from what we hold,
   or it was tabled in the last --refresh-days (default 90; catches withdrawn
   signatures, which do not change the listed total).
3. Upsert by Parliament EDM id; replace that EDM's signature rows.
4. Set slugs and related /brief topics for the EDMs written.

Re-running is safe and resumes a broken backfill: unchanged EDMs are skipped.
Parliament's API is slow (list pages 2-20s), so timeouts are long and calls
are paced. Exits 1 if most requests fail or the run hits a fatal error.

Railway: run with SKIP_MIGRATIONS=1 after migrations/edm_tables.py --execute.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extensions import db  # noqa: E402
from hansard_archive.models import (  # noqa: E402
    HaCronRun, HaEdm, HaEdmSponsor, HaPQTheme, HansardSessionTheme, THEME_TYPE_SPECIFIC,
)
from hansard_archive.slugs import slugify_edm, slugify_theme  # noqa: E402

logger = logging.getLogger(__name__)

API_BASE = "https://oralquestionsandmotions-api.parliament.uk"
REQUEST_TIMEOUT = 30
INTER_REQUEST_DELAY = 0.25
MAX_RETRIES = 3
LIST_PAGE_SIZE = 100          # API maximum
DEFAULT_REFRESH_DAYS = 90
SPONSOR_SLOTS = 6             # sponsoring order 1-6 = sponsors, later = supporters

# Commons sessions, newest first, keyed by the date EDM 1 of the session was
# tabled (looked up via parameters.uINWithAmendmentSuffix=1). The UIN restarts
# each session. Add a row when a new session starts.
SESSIONS: list[tuple[str, date]] = [
    ("2026-27", date(2026, 5, 13)),
    ("2024-26", date(2024, 7, 17)),
    ("2023-24", date(2023, 11, 7)),
    ("2022-23", date(2022, 5, 10)),
    ("2021-22", date(2021, 5, 11)),
    ("2019-21", date(2019, 12, 19)),
]
CURRENT_SESSION = SESSIONS[0][0]
PREVIOUS_SESSION = SESSIONS[1][0]


def session_for(d: date | None) -> str | None:
    if d is None:
        return None
    for label, start in SESSIONS:
        if d >= start:
            return label
    return None


def session_range(label: str) -> tuple[date, date | None]:
    """(first day, last day or None for the current session)."""
    for i, (name, start) in enumerate(SESSIONS):
        if name == label:
            end = SESSIONS[i - 1][1] - timedelta(days=1) if i > 0 else None
            return start, end
    raise ValueError(f"Unknown session {label!r}; known: {', '.join(s for s, _ in SESSIONS)}")


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Api:
    """Paced GETs with retry/backoff. Counts outcomes for the exit-code rule."""

    def __init__(self, sleep=time.sleep):
        self.ok = 0
        self.failed = 0
        self._sleep = sleep

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """Parsed JSON body, or None after MAX_RETRIES failures."""
        url = f"{API_BASE}{path}"
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
                elif resp.status_code in (429, 500, 502, 503, 504):
                    reason = f"HTTP {resp.status_code}"
                else:
                    # 4xx other than 429: permanent, do not retry
                    logger.warning("GET %s -> HTTP %d (not retried)", path, resp.status_code)
                    self.failed += 1
                    return None
            except (requests.RequestException, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                delay = 2 ** attempt
                print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {path}: {reason}; waiting {delay}s",
                      flush=True)
                self._sleep(delay)
            else:
                print(f"  [failed] {path}: {reason}", flush=True)
        self.failed += 1
        return None


def list_edms(api: Api, start: date, end: date | None) -> tuple[list[dict], bool]:
    """All listed EDMs tabled in [start, end]. Returns (rows, complete)."""
    rows: list[dict] = []
    skip = 0
    while True:
        params = {
            "parameters.statuses": ["Published", "Withdrawn"],
            "parameters.orderBy": "DateTabledAsc",
            "parameters.tabledStartDate": start.isoformat(),
            "parameters.take": LIST_PAGE_SIZE,
            "parameters.skip": skip,
        }
        if end:
            params["parameters.tabledEndDate"] = end.isoformat()
        body = api.get("/EarlyDayMotions/list", params)
        page = body.get("Response") if body else None
        if not isinstance(page, list):
            return rows, False
        rows.extend(r for r in page if isinstance(r, dict) and r.get("Id"))
        total = int((body.get("PagingInfo") or {}).get("Total") or 0)
        skip += LIST_PAGE_SIZE
        print(f"[edm-ingest] list: {min(skip, total)}/{total}", flush=True)
        if len(page) < LIST_PAGE_SIZE or skip >= total:
            return rows, True


def fetch_detail(api: Api, edm_id: int) -> tuple[dict | None, bool]:
    """(EDM detail, request_ok). Detail is None for unknown ids or failures."""
    body = api.get(f"/EarlyDayMotion/{edm_id}")
    if body is None:
        return None, False
    raw = body.get("Response")
    if body.get("StatusCode") == 404 or not isinstance(raw, dict):
        return None, True
    return raw, True


# ── Parsing / upsert ──────────────────────────────────────────────────────────

def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def _status(value) -> str:
    return "Withdrawn" if value in (1, "Withdrawn") else "Published"


def _active(sponsors: list | None) -> list[dict]:
    return [s for s in sponsors or [] if isinstance(s, dict) and not s.get("IsWithdrawn")]


def _amendment_summary(a: dict) -> dict:
    sponsor = a.get("PrimarySponsor") or {}
    tabled = _dt(a.get("DateTabled"))
    return {
        "id": a.get("Id"),
        "uin": a.get("UINWithAmendmentSuffix") or str(a.get("UIN") or ""),
        "text": (a.get("MotionText") or "").strip(),
        "sponsor_name": sponsor.get("Name") or "",
        "sponsor_party": sponsor.get("Party") or "",
        "date_tabled": tabled.date().isoformat() if tabled else None,
        "signature_count": len(_active(a.get("Sponsors"))),
    }


def upsert_edm(raw: dict, now: datetime) -> tuple[HaEdm, bool]:
    """Write one EDM detail payload and its signatures. Returns (edm, created). Caller commits."""
    edm = db.session.get(HaEdm, raw["Id"])
    created = edm is None
    if created:
        edm = HaEdm(id=raw["Id"], ingested_at=now)
        db.session.add(edm)

    tabled = _dt(raw.get("DateTabled"))
    sponsor = raw.get("PrimarySponsor") or {}
    edm.uin = raw.get("UINWithAmendmentSuffix") or str(raw.get("UIN") or "")
    edm.title = (raw.get("Title") or "").strip() or f"Early Day Motion {edm.uin}"
    edm.motion_text = (raw.get("MotionText") or "").strip() or None
    edm.status = _status(raw.get("Status"))
    edm.status_date = _dt(raw.get("StatusDate"))
    edm.date_tabled = tabled.date() if tabled else None
    edm.session = session_for(edm.date_tabled)
    edm.primary_sponsor_member_id = sponsor.get("MnisId") or raw.get("MemberId")
    edm.primary_sponsor_name = sponsor.get("Name")
    edm.primary_sponsor_party = sponsor.get("Party")
    edm.amendment_to_id = raw.get("AmendmentToMotionId")
    edm.is_prayer = bool(raw.get("PrayingAgainstNegativeStatutoryInstrumentId"))
    edm.amendments = [_amendment_summary(a) for a in raw.get("Amendments") or [] if isinstance(a, dict)]
    edm.slug = slugify_edm(edm.title)
    # Signatures live in ha_edm_sponsor; keep everything else from the payload.
    edm.raw_data = {k: v for k, v in raw.items() if k not in ("Sponsors", "Amendments")}
    edm.last_refreshed = now
    edm.updated_at = now
    db.session.flush()

    HaEdmSponsor.query.filter(HaEdmSponsor.edm_id == edm.id).delete(synchronize_session=False)
    active = 0
    for s in raw.get("Sponsors") or []:
        if not isinstance(s, dict):
            continue
        member = s.get("Member") or {}
        order = s.get("SponsoringOrder")
        withdrawn = bool(s.get("IsWithdrawn"))
        active += 0 if withdrawn else 1
        db.session.add(HaEdmSponsor(
            edm_id=edm.id,
            member_id=member.get("MnisId") or s.get("MemberId"),
            member_name=member.get("Name") or "Unknown member",
            party=member.get("Party"),
            constituency=member.get("Constituency"),
            is_primary=order == 1,
            is_sponsor=isinstance(order, int) and order <= SPONSOR_SLOTS,
            sponsoring_order=order,
            date_signed=_dt(s.get("CreatedWhen")),
            is_withdrawn=withdrawn,
            withdrawn_date=_dt(s.get("WithdrawnDate")),
        ))
    edm.signature_count = active
    return edm, created


def plan_fetches(listed: list[dict], refresh_days: int, today: date) -> dict[str, list[int]]:
    """Split listed EDMs into new / changed / recent / skip against the DB."""
    from sqlalchemy import func
    ids = [r["Id"] for r in listed]
    stored: dict[int, str] = {}
    sponsor_rows: dict[int, int] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        stored.update(db.session.query(HaEdm.id, HaEdm.status).filter(HaEdm.id.in_(chunk)).all())
        sponsor_rows.update(
            db.session.query(HaEdmSponsor.edm_id, func.count(HaEdmSponsor.id))
            .filter(HaEdmSponsor.edm_id.in_(chunk))
            .group_by(HaEdmSponsor.edm_id).all()
        )
    cutoff = today - timedelta(days=refresh_days)
    plan: dict[str, list[int]] = {"new": [], "changed": [], "recent": [], "skip": []}
    for r in listed:
        edm_id = r["Id"]
        tabled = _dt(r.get("DateTabled"))
        if edm_id not in stored:
            plan["new"].append(edm_id)
        elif (_status(r.get("Status")) != stored[edm_id]
              or int(r.get("SponsorsCount") or 0) != sponsor_rows.get(edm_id, 0)):
            plan["changed"].append(edm_id)
        elif tabled and tabled.date() >= cutoff:
            plan["recent"].append(edm_id)
        else:
            plan["skip"].append(edm_id)
    return plan


# ── Related /brief topics ─────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOPIC_ITEMS = 3      # matches the /brief "thin topic" threshold: only link indexed pages


def _tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall((s or "").lower().replace("'", "").replace("’", ""))


def build_topic_index() -> dict[str, list[tuple[tuple[str, ...], str, str, int]]]:
    """
    Specific topics with at least _MIN_TOPIC_ITEMS debates + written questions,
    keyed by first word: {word: [(tokens, label, slug, items)]}.
    """
    from sqlalchemy import func
    counts: dict[str, int] = {}
    for model in (HaPQTheme, HansardSessionTheme):
        rows = (db.session.query(model.theme, func.count())
                .filter(model.theme_type == THEME_TYPE_SPECIFIC)
                .group_by(model.theme).all())
        for theme, n in rows:
            counts[theme] = counts.get(theme, 0) + n
    index: dict[str, list] = {}
    for theme, n in counts.items():
        toks = tuple(_tokens(theme))
        if n < _MIN_TOPIC_ITEMS or not toks or len("".join(toks)) < 5:
            continue
        index.setdefault(toks[0], []).append((toks, theme, slugify_theme(theme), n))
    return index


def match_topics(title: str, text: str, index: dict, limit: int = 3) -> list[dict]:
    """Topics whose words appear as a phrase in the EDM. Single-word topics must be in the title."""
    best: dict[str, tuple[int, int, str]] = {}
    for where, weight in ((_tokens(title), 3), (_tokens(text), 1)):
        for i, word in enumerate(where):
            for toks, label, slug, n in index.get(word, ()):
                if len(toks) == 1 and weight == 1:
                    continue
                if tuple(where[i:i + len(toks)]) == toks:
                    score, _, _ = best.get(slug, (0, n, label))
                    best[slug] = (score + weight, n, label)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[1][2]))
    return [{"slug": slug, "label": label} for slug, (_, _, label) in ranked[:limit]]


def assign_related_topics(edm_ids: list[int]) -> int:
    if not edm_ids:
        return 0
    index = build_topic_index()
    linked = 0
    for i in range(0, len(edm_ids), 200):
        for edm in HaEdm.query.filter(HaEdm.id.in_(edm_ids[i:i + 200])).all():
            edm.related_topics = match_topics(edm.title, edm.motion_text or "", index)
            linked += bool(edm.related_topics)
        db.session.commit()
    return linked


# ── Main loop ─────────────────────────────────────────────────────────────────

def ingest(start: date, end: date | None, refresh_days: int = DEFAULT_REFRESH_DAYS,
           limit: int | None = None, dry_run: bool = False, api: Api | None = None,
           today: date | None = None) -> dict:
    """Run one ingest. Expects an app context. Returns a summary dict."""
    from sqlalchemy.exc import OperationalError

    api = api or Api()
    today = today or date.today()
    t0 = time.time()
    print(f"[edm-ingest] window {start} -> {end or 'today'} | refresh last {refresh_days} days"
          f"{' | DRY RUN' if dry_run else ''}", flush=True)

    listed, complete = list_edms(api, start, end)
    plan = plan_fetches(listed, refresh_days, today)
    print(f"[edm-ingest] listed {len(listed)} (complete={complete}) | new {len(plan['new'])} | "
          f"changed {len(plan['changed'])} | recent refresh {len(plan['recent'])} | "
          f"unchanged {len(plan['skip'])}", flush=True)

    to_fetch = plan["new"] + plan["changed"] + plan["recent"]
    if limit is not None:
        to_fetch = to_fetch[:limit]

    summary = {"listed": len(listed), "list_complete": complete,
               **{f"planned_{k}": len(v) for k, v in plan.items()},
               "inserted": 0, "updated": 0, "not_found": 0, "failed": 0,
               "topics_linked": 0, "dry_run": dry_run}
    if dry_run:
        summary.update(requests_ok=api.ok, requests_failed=api.failed,
                       elapsed_s=round(time.time() - t0, 1))
        return summary

    written: list[int] = []
    for n, edm_id in enumerate(to_fetch, 1):
        raw, ok = fetch_detail(api, edm_id)
        if raw is None:
            summary["failed" if not ok else "not_found"] += 1
            continue
        for attempt in (1, 2):
            try:
                _, created = upsert_edm(raw, datetime.utcnow())
                db.session.commit()
                summary["inserted" if created else "updated"] += 1
                written.append(edm_id)
                break
            except OperationalError as exc:
                db.session.rollback()
                db.engine.dispose()
                if attempt == 2:
                    logger.error("DB error on EDM %d: %s", edm_id, exc)
                    summary["failed"] += 1
            except Exception as exc:
                db.session.rollback()
                logger.error("Ingest error on EDM %d: %s", edm_id, exc, exc_info=True)
                summary["failed"] += 1
                break
        if n % 100 == 0:
            rate = n / max(time.time() - t0, 1)
            print(f"[edm-ingest] {n}/{len(to_fetch)} detail fetches | {rate:.2f}/s | "
                  f"ETA {int((len(to_fetch) - n) / rate)}s", flush=True)

    summary["topics_linked"] = assign_related_topics(written)
    summary.update(requests_ok=api.ok, requests_failed=api.failed,
                   elapsed_s=round(time.time() - t0, 1))
    return summary


def _print_summary(s: dict) -> None:
    print("\n[edm-ingest] ========== Run complete ==========", flush=True)
    for key in ("listed", "list_complete", "planned_new", "planned_changed", "planned_recent",
                "planned_skip", "inserted", "updated", "not_found", "failed", "topics_linked",
                "requests_ok", "requests_failed", "elapsed_s"):
        print(f"  {key:<16} {s.get(key)}", flush=True)


def mostly_failed(s: dict) -> bool:
    return s["requests_failed"] > s["requests_ok"] or (not s["list_complete"] and s["listed"] == 0)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest Early Day Motions")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--backfill", action="store_true",
                        help=f"Sessions {PREVIOUS_SESSION} and {CURRENT_SESSION}")
    window.add_argument("--session", help="One session label, e.g. 2024-26")
    window.add_argument("--since", type=date.fromisoformat, help="Tabled on/after YYYY-MM-DD")
    parser.add_argument("--refresh-days", type=int, default=DEFAULT_REFRESH_DAYS,
                        help="Always re-fetch EDMs tabled in the last N days (default 90)")
    parser.add_argument("--limit", type=int, help="Max detail fetches")
    parser.add_argument("--dry-run", action="store_true", help="List only; predict, write nothing")
    parser.add_argument("--service-name", default="edm-cron", help="ha_cron_run.service_name")
    args = parser.parse_args()

    if args.backfill:
        start, end = session_range(PREVIOUS_SESSION)[0], None
    elif args.session:
        start, end = session_range(args.session)
    elif args.since:
        start, end = args.since, None
    else:
        start, end = session_range(CURRENT_SESSION)

    from flask_app import app
    exit_code = 0
    with app.app_context():
        print(f"[edm-ingest] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        run = None
        if not args.dry_run:
            run = HaCronRun(service_name=args.service_name, started_at=datetime.utcnow(),
                            days_window=args.refresh_days, status="running")
            db.session.add(run)
            db.session.commit()
        try:
            summary = ingest(start, end, refresh_days=args.refresh_days,
                             limit=args.limit, dry_run=args.dry_run)
            _print_summary(summary)
            exit_code = 1 if mostly_failed(summary) else 0
            if run:
                run.sessions_ingested = summary["inserted"] + summary["updated"]
                run.errors = summary["failed"] + summary["requests_failed"]
                run.notes = (f"listed={summary['listed']} inserted={summary['inserted']} "
                             f"updated={summary['updated']} failed={summary['failed']}")
        except Exception as exc:
            db.session.rollback()
            print(f"[edm-ingest] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", flush=True)
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
