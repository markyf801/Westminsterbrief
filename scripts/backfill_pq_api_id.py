"""
Fill ha_pq.api_id (Parliament's id for the question) on rows that don't have it.

Each row is looked up by its own (tabled_date, uin) — Parliament's public key —
on the individual endpoint:
    GET /api/writtenquestions/questions/<tabled_date>/<uin>
and the api_id is written only when all of these hold:
  * the API returns that same uin and tabled date
  * the start of the question text matches what is stored (a row whose text
    belongs to another question is left alone — possible INC-008 damage)
  * no other row already has that api_id (that is a duplicate: run
    scripts/dedupe_pq.py)
Nothing else is ever changed: never uin, never tabled_date, never text.

DRY RUN BY DEFAULT — logs what would be written; --apply writes, committing
every batch. Not-found, mismatched and conflicting rows are logged as they are
met and listed again in the summary.

Resumable: rows that get an api_id drop out of the query, so a re-run carries
on. To skip rows already checked (e.g. not-found ones) pass --after-id with the
last id from the progress log.

Usage:
  python scripts/backfill_pq_api_id.py                     # dry run, all rows
  python scripts/backfill_pq_api_id.py --limit 50          # dry run, first 50
  python scripts/backfill_pq_api_id.py --apply
  python scripts/backfill_pq_api_id.py --apply --after-id 81234

Pacing: at least 0.5s between requests (~1 hour for 7,400 rows). A 429 stops
the run at once (exit 2) — wait, then resume with --after-id. Exit 1 when some
rows hit API errors or a write failed (e.g. a unique-index violation — counted
as failed, the run carries on); 0 otherwise. Every matched row is logged
(WRITTEN / MATCH id= uin= tabled= api_id=) so a run can be undone from its log.

Railway one-off: SKIP_MIGRATIONS=1 is set by default below. Not during a PQ
cron window.
"""

import argparse
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import quote

os.environ.setdefault("SKIP_MIGRATIONS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pq-api-id] %(message)s",
                    datefmt="%H:%M:%S")
_log = logging.getLogger("pq-api-id")

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
MIN_DELAY = 0.5
BATCH = 100
TIMEOUT = 60
RETRIES = 3                  # attempts per row on 5xx / timeouts
MAX_ERRORS_IN_A_ROW = 5      # then stop: the API is down
TEXT_PREFIX = 120            # normalised characters compared, after the "To ask …" opener
TEXT_MIN = 20                # shorter bodies must match exactly
LIST_LIMIT = 100             # rows listed per outcome in the summary


class RateLimited(Exception):
    pass


# The standard opener ("To ask the Secretary of State for Health and Social
# Care," / "To ask His Majesty's Government") is shared by thousands of
# questions, so it is removed before comparing.
_OPENER = re.compile(
    r"^\s*to ask (?:his|her) majesty'?s government\b[\s,]*"
    r"|^\s*to ask (?:the |my )?[^,?]{0,160},\s*",
    re.IGNORECASE)


def _norm(text: str | None) -> str:
    """Lower-case letters and digits only, for comparing question text."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _body(text: str | None) -> str:
    """Normalised question text without the standard "To ask …" opener."""
    plain = re.sub(r"<[^>]+>", " ", text or "")
    return _norm(_OPENER.sub("", plain, count=1))


def texts_match(stored: str | None, api: str | None) -> bool:
    """Same question text: the bodies (opener removed) agree on their first
    TEXT_PREFIX characters — stored text may be the bulk API's cut-off copy."""
    a, b = _body(stored), _body(api)
    n = min(len(a), len(b), TEXT_PREFIX)
    if n == 0 or a[:n] != b[:n]:
        return False
    return n >= TEXT_MIN or a == b


def fetch(tabled_date, uin: str, delay: float):
    """Return (status, value). status: ok | not_found | error. Raises RateLimited."""
    url = f"{WQ_API_BASE}/{tabled_date.isoformat()}/{quote(uin.strip(), safe='')}"
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(max(delay, 2 ** attempt))
        try:
            resp = requests.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            _log.warning("%s: %s (attempt %d/%d)", url, exc, attempt + 1, RETRIES)
            continue
        if resp.status_code == 429:
            raise RateLimited(url)
        if resp.status_code == 404:
            return "not_found", None
        if resp.status_code != 200:
            _log.warning("%s: HTTP %s (attempt %d/%d)", url, resp.status_code, attempt + 1, RETRIES)
            if resp.status_code < 500:
                return "error", None
            continue
        try:
            value = (resp.json() or {}).get("value")
        except ValueError:
            value = None
        if isinstance(value, dict):
            return "ok", value
        _log.warning("%s: no value in response (attempt %d/%d)", url, attempt + 1, RETRIES)
    return "error", None


def check(row, value, taken: dict) -> tuple[str, int | None, str]:
    """Decide for one row. Returns (outcome, api_id, detail)."""
    api_id = value.get("id")
    api_uin = (value.get("uin") or "").strip()
    api_date = str(value.get("dateTabled") or "")[:10]
    if not isinstance(api_id, int) or api_id <= 0:
        return "ambiguous", None, f"API returned no usable id ({api_id!r})"
    if api_uin.lower() != (row.uin or "").strip().lower() or api_date != row.tabled_date.isoformat():
        return "ambiguous", api_id, f"API returned uin={api_uin} tabled={api_date}"
    if not texts_match(row.question_text, value.get("questionText")):
        return ("text_mismatch", api_id,
                f"stored {_norm(row.question_text)[:40]!r} vs API "
                f"{_norm(value.get('questionText'))[:40]!r}")
    other = taken.get(api_id)
    if other is not None and other != row.id:
        return "conflict", api_id, f"api_id already on row id={other} (duplicate: run dedupe_pq.py)"
    return "match", api_id, ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fill missing ha_pq.api_id from Parliament")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Look up and report, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="Write api_id")
    parser.add_argument("--after-id", type=int, default=0,
                        help="Only rows with id greater than this (resume)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N rows")
    parser.add_argument("--delay", type=float, default=MIN_DELAY,
                        help=f"Seconds between requests (minimum {MIN_DELAY})")
    args = parser.parse_args(argv)
    apply = args.apply
    delay = max(MIN_DELAY, args.delay)

    from flask_app import app
    from sqlalchemy import select, update
    from sqlalchemy.exc import IntegrityError, OperationalError
    from extensions import db
    from hansard_archive.models import HaPQ

    tally = Counter()
    listed: dict[str, list[str]] = {}
    last_id = args.after_id
    stop_reason = ""
    taken_in_run: dict[int, int] = {}      # api_id -> row id written/planned this run

    def note(outcome, row, detail):
        line = f"id={row.id} {row.tabled_date} uin={row.uin}" + (f" — {detail}" if detail else "")
        if outcome != "match":
            _log.warning("%s: %s", outcome.upper(), line)
        bucket = listed.setdefault(outcome, [])
        if len(bucket) < LIST_LIMIT:
            bucket.append(line)

    with app.app_context():
        todo = db.session.execute(
            select(db.func.count(HaPQ.id)).where(HaPQ.api_id.is_(None), HaPQ.id > last_id)
        ).scalar()
        _log.info("=== %s === %d row(s) without api_id after id=%d, %.1fs between requests",
                  "APPLY" if apply else "DRY RUN (no writes)", todo, last_id, delay)
        processed = 0
        errors_in_a_row = 0
        while not stop_reason:
            try:
                batch = db.session.execute(
                    select(HaPQ.id, HaPQ.uin, HaPQ.tabled_date, HaPQ.question_text)
                    .where(HaPQ.api_id.is_(None), HaPQ.id > last_id)
                    .order_by(HaPQ.id).limit(BATCH)
                ).all()
            except OperationalError as exc:
                _log.warning("DB error loading batch after id=%d: %s — reconnecting", last_id, exc)
                db.session.rollback()
                db.engine.dispose()
                continue
            if not batch:
                break

            for row in batch:
                if args.limit is not None and processed >= args.limit:
                    stop_reason = f"--limit {args.limit} reached"
                    break
                try:
                    status, value = fetch(row.tabled_date, row.uin, delay)
                except RateLimited as exc:
                    stop_reason = f"429 rate limited at {exc} — wait, then resume with --after-id {last_id}"
                    break
                time.sleep(delay)
                processed += 1

                if status == "error":
                    errors_in_a_row += 1
                    tally["error"] += 1
                    note("error", row, "API error after retries")
                    if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                        last_id = row.id
                        stop_reason = (f"{errors_in_a_row} API errors in a row — stopped; "
                                       f"resume with --after-id {last_id}")
                        break
                    last_id = row.id
                    continue
                errors_in_a_row = 0

                if status == "not_found":
                    outcome, api_id, detail = "not_found", None, "404 for this (date, uin)"
                else:
                    api_id = value.get("id")
                    taken = dict(taken_in_run)
                    if isinstance(api_id, int):
                        holder = db.session.execute(
                            select(HaPQ.id).where(HaPQ.api_id == api_id).limit(1)).scalar()
                        if holder is not None:
                            taken[api_id] = holder
                    outcome, api_id, detail = check(row, value, taken)

                tally[outcome] += 1
                note(outcome, row, detail if outcome != "match" else f"api_id={api_id}")
                if outcome == "match":
                    taken_in_run[api_id] = row.id
                    written = 0
                    if apply:
                        try:
                            with db.session.begin_nested():
                                res = db.session.execute(
                                    update(HaPQ)
                                    .where(HaPQ.id == row.id, HaPQ.api_id.is_(None))
                                    .values(api_id=api_id, updated_at=datetime.utcnow()),
                                    execution_options={"synchronize_session": False})
                                written = res.rowcount or 0
                        except IntegrityError as exc:   # e.g. uq_ha_pq_api_id
                            tally["failed"] += 1
                            note("failed", row, f"api_id={api_id} not written: "
                                                f"{str(exc.orig).splitlines()[0]}")
                    tally["written"] += written
                    # One line per match so the run can be undone from the log.
                    _log.info("%s id=%d uin=%s tabled=%s api_id=%d",
                              "WRITTEN" if written else ("MATCH" if not apply else "NOT WRITTEN"),
                              row.id, row.uin, row.tabled_date, api_id)
                last_id = row.id

            if apply:
                try:
                    db.session.commit()
                except OperationalError as exc:
                    db.session.rollback()
                    db.engine.dispose()
                    stop_reason = (f"DB error committing batch: {exc} — this batch was not "
                                   f"saved; re-run (rows without api_id are picked up again)")
            else:
                db.session.rollback()
            _log.info("progress: %d checked, last_id=%d — match=%d not_found=%d "
                      "text_mismatch=%d ambiguous=%d conflict=%d error=%d",
                      processed, last_id, tally["match"], tally["not_found"],
                      tally["text_mismatch"], tally["ambiguous"], tally["conflict"],
                      tally["error"])

        remaining = db.session.execute(
            select(db.func.count(HaPQ.id)).where(HaPQ.api_id.is_(None))).scalar()
        db.session.rollback()

    _log.info("=== SUMMARY (%s) ===", "applied" if apply else "dry run — nothing written")
    _log.info("checked=%d %smatch=%d written=%d failed=%d not_found=%d text_mismatch=%d "
              "ambiguous=%d conflict=%d error=%d; rows still without api_id=%d; last_id=%d",
              processed, "" if apply else "(would write) ", tally["match"], tally["written"],
              tally["failed"], tally["not_found"], tally["text_mismatch"], tally["ambiguous"],
              tally["conflict"], tally["error"], remaining, last_id)
    for outcome in ("failed", "not_found", "text_mismatch", "ambiguous", "conflict", "error"):
        rows = listed.get(outcome) or []
        if rows:
            _log.info("%s (first %d):", outcome, len(rows))
            for line in rows:
                _log.info("  %s", line)
    if stop_reason:
        _log.error("STOPPED: %s", stop_reason)
        if not stop_reason.startswith("--limit"):
            return 2
    return 1 if (tally["error"] or tally["failed"]) else 0


if __name__ == "__main__":
    sys.exit(main())
