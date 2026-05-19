"""
Parliament Bills ingestion.

Fetches all bills from sessions 38 (2024-25) and 39 (2025-26) via the public
Parliament Bills API, deduplicates by billId, and upserts into ha_bill /
ha_bill_sponsor / ha_bill_stage.

Usage:
    python scripts/ingest_bills.py               # full ingest of sessions 38+39
    python scripts/ingest_bills.py --limit N     # stop after N bills (for testing)
    python scripts/ingest_bills.py --bill-id N   # ingest a single bill
    python scripts/ingest_bills.py --dry-run     # fetch but do not write to DB

No API key required.  Rate-limits: exponential backoff on 429/503.
"""

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from extensions import db
from hansard_archive.models import HaBill, HaBillSponsor, HaBillStage

logger = logging.getLogger(__name__)

BASE_URL = "https://bills-api.parliament.uk/api/v1"
SESSIONS = [38, 39]
REQUEST_TIMEOUT = 30
INTER_REQUEST_DELAY = 0.15   # polite pacing between calls
MAX_RETRIES = 3

BILL_TYPE_NAMES = {
    1: "Government Bill",
    2: "Private Members' Bill (Lords ballot)",
    3: "Consolidation Bill",
    4: "Hybrid Bill",
    5: "Private Members' Bill (Ten Minute Rule)",
    6: "Private Bill",
    7: "Private Members' Bill (Ballot)",
    8: "Private Members' Bill (Presentation)",
    9: "Draft Bill",
    10: "Supply and Appropriation Bill",
}

SESSION_LABELS = {
    36: "2022-23",
    37: "2023-24",
    38: "2024-25",
    39: "2025-26",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> dict | list | None:
    """GET with exponential backoff on 429/503. Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 503):
                delay = 2 ** (attempt + 1)
                print(f"  [rate-limit {resp.status_code}] waiting {delay}s ...", flush=True)
                time.sleep(delay)
                continue
            if resp.status_code == 404:
                return None
            logger.warning("GET %s returned HTTP %d", url, resp.status_code)
            return None
        except requests.Timeout:
            delay = 2 ** attempt
            logger.warning("GET %s timed out (attempt %d/%d)", url, attempt + 1, MAX_RETRIES)
            time.sleep(delay)
        except requests.RequestException as exc:
            delay = 2 ** attempt
            logger.warning("GET %s error: %s", url, exc)
            time.sleep(delay)
    return None


def _fetch_session_bill_ids(session_id: int) -> list[int]:
    """Return all bill IDs from a session (handles pagination)."""
    bill_ids: list[int] = []
    skip = 0
    take = 100
    while True:
        data = _get(f"{BASE_URL}/Bills", {"Session": session_id, "take": take, "skip": skip})
        if not data:
            break
        items = data.get("items", [])
        bill_ids.extend(b["billId"] for b in items if "billId" in b)
        total = data.get("totalResults", 0)
        skip += len(items)
        if skip >= total or not items:
            break
    return bill_ids


def _fetch_bill_detail(bill_id: int) -> dict | None:
    return _get(f"{BASE_URL}/Bills/{bill_id}")


def _fetch_bill_stages(bill_id: int) -> list[dict]:
    data = _get(f"{BASE_URL}/Bills/{bill_id}/Stages")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items", [])
    return []


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str | None) -> date | None:
    """Parse ISO date string (with or without time component) to a date."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except (ValueError, TypeError):
        return None


def _introduced_date_from_stages(stages: list[dict]) -> date | None:
    """
    Derive the bill's introduction date from stage data.
    The first reading (lowest sortOrder) with a sitting gives the introduced date.
    API doesn't always populate introducedDate on the detail endpoint.
    """
    candidates = sorted(stages, key=lambda s: (s.get("sortOrder", 99), s.get("id", 0)))
    for stage in candidates:
        sittings = stage.get("stageSittings") or []
        if sittings:
            d = _parse_date(sittings[0].get("date"))
            if d:
                return d
    return None


# ---------------------------------------------------------------------------
# Upsert functions
# ---------------------------------------------------------------------------

def _upsert_bill(detail: dict, stages: list[dict]) -> HaBill | None:
    """
    Upsert one ha_bill row from the detail API response.
    Returns the HaBill object (flushed but not committed).
    """
    pid = detail.get("billId")
    if not pid:
        return None

    current_stage_obj = detail.get("currentStage") or {}
    intro_session_id = detail.get("introducedSessionId")
    session_label = SESSION_LABELS.get(intro_session_id, f"session-{intro_session_id}")

    # introducedDate is null in API for most bills — derive from first stage sitting
    introduced_date = (
        _parse_date(detail.get("introducedDate"))
        or _introduced_date_from_stages(stages)
    )

    fields = {
        "parliament_bill_id": pid,
        "title":           detail.get("shortTitle") or f"Bill {pid}",
        "short_title":     detail.get("shortTitle"),
        "long_title":      detail.get("longTitle"),
        "summary":         detail.get("summary"),
        "house_of_origin": detail.get("originatingHouse") or "",
        "session":         session_label,
        "bill_type":       BILL_TYPE_NAMES.get(detail.get("billTypeId")),
        "is_act":               bool(detail.get("isAct", False)),
        "is_defeated":          bool(detail.get("isDefeated", False)),
        "bill_withdrawn_date":  _parse_date(detail.get("billWithdrawn")),
        "is_carried_over":      intro_session_id == 38 and 39 in (detail.get("includedSessionIds") or []),
        "current_stage":        current_stage_obj.get("description"),
        "current_house":   detail.get("currentHouse") or current_stage_obj.get("house"),
        "introduced_date": introduced_date,
        "last_updated_date": _parse_date(detail.get("lastUpdate")),
        "royal_assent_date": _parse_date(detail.get("royalAssentDate")),
        "parliament_url":  f"https://bills.parliament.uk/bills/{pid}",
        "raw_data":        detail,
        "last_refreshed":  datetime.now(timezone.utc).replace(tzinfo=None),
    }

    existing = HaBill.query.filter_by(parliament_bill_id=pid).first()
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        return existing

    fields["ingested_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    bill = HaBill(**fields)
    db.session.add(bill)
    db.session.flush()   # populate bill.id for FK children
    return bill


def _upsert_sponsors(bill: HaBill, detail: dict, valid_member_ids: set[int]) -> int:
    """
    Replace all sponsors for a bill. Returns count written.
    member_id is set only if the Parliament member ID exists in cached_member —
    some sponsors are retired/historical MPs not in the current cache.
    """
    HaBillSponsor.query.filter_by(bill_id=bill.id).delete()

    sponsors_raw = detail.get("sponsors") or []
    count = 0
    for sp in sponsors_raw:
        member = sp.get("member") or {}
        raw_member_id = member.get("memberId")
        sort_order = sp.get("sortOrder", 99)
        name = member.get("name") or "Unknown"

        # Only use the FK if the member is actually in our cache
        resolved_id = int(raw_member_id) if raw_member_id else None
        if resolved_id is not None and resolved_id not in valid_member_ids:
            resolved_id = None   # sponsor exists but isn't in current member cache

        db.session.add(HaBillSponsor(
            bill_id=bill.id,
            member_id=resolved_id,
            member_name=name,
            party=member.get("party"),
            is_primary=(sort_order == 1),
            house=member.get("house"),
        ))
        count += 1
    return count


def _upsert_stages(bill: HaBill, stages: list[dict]) -> int:
    """Replace all stages for a bill. Returns count written."""
    HaBillStage.query.filter_by(bill_id=bill.id).delete()

    count = 0
    for stage in stages:
        stage_instance_id = stage.get("id")
        if not stage_instance_id:
            continue

        sittings = stage.get("stageSittings") or []
        stage_date = _parse_date(sittings[0].get("date")) if sittings else None

        db.session.add(HaBillStage(
            bill_id=bill.id,
            parliament_stage_id=int(stage_instance_id),
            stage_name=stage.get("description") or "Unknown",
            house=stage.get("house") or "",
            stage_date=stage_date,
            stage_order=stage.get("sortOrder", 99),
        ))
        count += 1
    return count


# ---------------------------------------------------------------------------
# Main ingestion loop
# ---------------------------------------------------------------------------

def ingest_all(
    limit: int | None = None,
    dry_run: bool = False,
    single_bill_id: int | None = None,
) -> dict:
    """
    Main ingestion loop. Expects an active Flask app context.
    Returns a summary dict.
    """
    start_time = time.time()

    # ── Collect bill IDs ────────────────────────────────────────────────────
    if single_bill_id:
        bill_ids = [single_bill_id]
        print(f"[ingest] Single-bill mode: {single_bill_id}", flush=True)
    else:
        seen: set[int] = set()
        bill_ids: list[int] = []
        for sid in SESSIONS:
            print(f"[ingest] Fetching session {sid} bill list ...", flush=True)
            ids = _fetch_session_bill_ids(sid)
            new = [i for i in ids if i not in seen]
            seen.update(new)
            bill_ids.extend(new)
            print(f"  -> {len(ids)} returned, {len(new)} new unique", flush=True)
        print(f"[ingest] {len(bill_ids)} unique bills to process", flush=True)

    if limit:
        bill_ids = bill_ids[:limit]
        print(f"[ingest] --limit {limit}: processing {len(bill_ids)} bills", flush=True)

    # Load valid member IDs once — used to safe-null unresolvable sponsor FKs
    from sqlalchemy import text as _text
    rows = db.session.execute(_text("SELECT member_id FROM cached_member")).fetchall()
    valid_member_ids: set[int] = {r[0] for r in rows}
    print(f"[ingest] {len(valid_member_ids)} members in cache for sponsor FK resolution",
          flush=True)

    total = len(bill_ids)
    ingested = failed = sponsors_total = stages_total = 0
    failed_bills: list[tuple[int, str]] = []
    last_progress_time = time.time()

    for i, pid in enumerate(bill_ids):
        time.sleep(INTER_REQUEST_DELAY)

        try:
            detail = _fetch_bill_detail(pid)
            if detail is None:
                print(f"[ingest] [{i+1}/{total}] SKIP {pid} — 404", flush=True)
                failed += 1
                failed_bills.append((pid, "404 not found"))
                continue

            time.sleep(INTER_REQUEST_DELAY)
            stages = _fetch_bill_stages(pid)

            if dry_run:
                title = detail.get("shortTitle", f"Bill {pid}")
                print(f"[ingest] [{i+1}/{total}] DRY {pid} {title[:60]!r}", flush=True)
                ingested += 1
                continue

            bill = _upsert_bill(detail, stages)
            if bill is None:
                failed += 1
                failed_bills.append((pid, "parse error — no billId"))
                continue

            n_sp = _upsert_sponsors(bill, detail, valid_member_ids)
            n_st = _upsert_stages(bill, stages)
            db.session.commit()

            ingested += 1
            sponsors_total += n_sp
            stages_total += n_st

        except Exception as exc:
            db.session.rollback()
            logger.error("Ingest error on bill %d: %s", pid, exc, exc_info=True)
            failed += 1
            failed_bills.append((pid, str(exc)[:100]))

        # Progress every 25 bills or 5 minutes
        now = time.time()
        if (i + 1) % 25 == 0 or (now - last_progress_time) >= 300:
            elapsed = now - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = total - (i + 1)
            eta_s = int(remaining / rate) if rate > 0 else 0
            print(
                f"[ingest] {ingested}/{total} done | {failed} failed | "
                f"{remaining} remaining | {rate:.1f} bills/s | ETA {eta_s}s",
                flush=True,
            )
            last_progress_time = now

    # ── End-of-run summary ─────────────────────────────────────────────────
    elapsed = time.time() - start_time
    summary = {
        "total": total,
        "ingested": ingested,
        "failed": failed,
        "sponsors": sponsors_total,
        "stages": stages_total,
        "elapsed_s": round(elapsed, 1),
        "failed_bills": failed_bills,
    }

    print(f"\n[ingest] ========== Run complete ==========", flush=True)
    print(f"  Bills processed:  {total}", flush=True)
    print(f"  Ingested:         {ingested}", flush=True)
    print(f"  Failed:           {failed}", flush=True)
    print(f"  Sponsors written: {sponsors_total}", flush=True)
    print(f"  Stages written:   {stages_total}", flush=True)
    print(f"  Elapsed:          {elapsed:.0f}s", flush=True)
    if failed_bills:
        print(f"\n  Failed bills:", flush=True)
        for bid, reason in failed_bills:
            print(f"    {bid}: {reason}", flush=True)

    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Ingest Parliament Bills")
    parser.add_argument("--limit",   type=int, help="Max bills to ingest (testing)")
    parser.add_argument("--bill-id", type=int, dest="bill_id",
                        help="Ingest a single bill by Parliament bill ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch API but do not write to DB")
    args = parser.parse_args()

    from flask_app import app
    with app.app_context():
        ingest_all(
            limit=args.limit,
            dry_run=args.dry_run,
            single_bill_id=args.bill_id,
        )


if __name__ == "__main__":
    main()
