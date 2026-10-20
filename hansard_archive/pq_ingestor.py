"""
Hansard Archive — Written Questions ingestion from Parliament WQ API.

Entry point:
  ingest_pq_date_range(date_from, date_to) — fetch and upsert all WQs
      with tabledDate in [date_from, date_to]. Returns a dict of counts
      (inserted / updated / unchanged / identity_mismatch / failed / errors).

Identity (INC-008): Parliament reuses UINs every session, so a UIN alone never
identifies a question. A row is matched by:
  1. api_id — Parliament's own numeric id, unique across sessions; then
  2. (tabled_date, uin) — case-insensitive uin — for rows that have no api_id.
Never by uin alone. No match → insert.

Tripwire: a row found by api_id whose uin or tabled_date differs from the API
(or a (tabled_date, uin) row carrying a different api_id) is counted as an
identity_mismatch, logged, and left completely untouched. More than
``max_identity_mismatches`` in one run aborts it with PQIdentityMismatchError.

Updates only fill or refresh: a non-null stored value is never overwritten
with None, and updated_at moves only when something actually changed.

No answered filter — fetches all statuses. A tabled-date window does NOT catch
answers that arrive after the window has moved on (answers typically come 1-4
weeks after tabling), so the cron also calls this with date_field="answered"
(Parliament's answeredWhenFrom/answeredWhenTo filter) for recent answers.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

import requests
from sqlalchemy import func

from extensions import db
from hansard_archive.html_sanitizer import sanitize_answer_html
from hansard_archive.models import HaPQ, HaPQTheme

_log = logging.getLogger(__name__)

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_REQUEST_TIMEOUT = 60        # Parliament API is slow; 30s was too tight
_PAGE_SIZE = 500
_COMMIT_BATCH = 200
_INTER_REQUEST_DELAY = 0.3   # seconds between paginated requests
_PAGE_RETRIES = 3            # retries on transient errors before skipping a page

_BULK_ANSWER_CAP = 258       # bulk endpoint cuts answerText at this length


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
        if val < 0:
            raise ValueError
        return val
    except ValueError:
        _log.error("Ignoring invalid %s=%r - using default %d", name, raw, default)
        return default


# Abort a run when more than this many identity mismatches are seen.
# Override per call (max_identity_mismatches=) or with the env var.
DEFAULT_MAX_IDENTITY_MISMATCHES = _env_int("PQ_MAX_IDENTITY_MISMATCHES", 5)
# Abort a run after this many consecutive API pages fail (all retries used).
DEFAULT_MAX_FAILED_PAGES = _env_int("PQ_MAX_FAILED_PAGES", 3) or 1

# Refreshed on update when the API gives a non-null value; never blanked.
_REFRESH_IF_PRESENT = (
    "heading", "chamber", "answer_date",
    "asking_member", "asking_mnis_id",
    "answering_member", "answering_mnis_id",
    "answering_body", "answering_body_id",
    "is_holding", "is_withdrawn",
)


class PQIngestAborted(RuntimeError):
    """A run stopped early. ``counts`` holds the totals up to that point."""

    def __init__(self, message: str, counts: dict):
        super().__init__(message)
        self.counts = counts


class PQIdentityMismatchError(PQIngestAborted):
    """Raised when a run sees more identity mismatches than allowed."""


class PQPageFailureError(PQIngestAborted):
    """Raised when too many consecutive API pages fail."""


@dataclass
class UpsertOutcome:
    action: str                      # inserted | updated | unchanged | identity_mismatch
    changes: dict = field(default_factory=dict)   # field -> (old, new)
    detail: str = ""
    pq_id: int | None = None


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html).strip()


def _clean_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(val) -> date | None:
    if not val:
        return None
    try:
        return date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return None


def _opt_bool(val) -> bool | None:
    return None if val is None else bool(val)


def _fetch_individual(api_id: int) -> dict | None:
    """Fetch the full individual question record, which includes fields the
    bulk endpoint omits or truncates (full question and answer text)."""
    url = f"{WQ_API_BASE}/{api_id}"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json().get("value") or {}
    except Exception as exc:
        _log.warning("Individual fetch failed for id=%s: %s", api_id, exc)
        return None


def _member_name(obj) -> str | None:
    if not isinstance(obj, dict):
        return None
    return (obj.get("name") or "").strip() or None


def _extract_pq_fields(value: dict) -> dict | None:
    """Extract and normalise fields from a WQ API value object.

    is_holding / is_withdrawn are None when the API omits them, so an update
    never resets a stored True to False on a missing field.
    """
    uin = (value.get("uin") or "").strip()
    if not uin:
        return None

    question_raw = value.get("questionText") or ""
    question_text = _clean_whitespace(_strip_html(question_raw))
    if not question_text:
        return None

    answer_raw = value.get("answerText") or ""
    answer_text = sanitize_answer_html(answer_raw)  # stored as sanitised HTML

    # askingMember / answeringMember are objects only when the request has
    # expandMember=true; member ids and body name/id are flat fields.
    return {
        "api_id": value.get("id"),       # Parliament numeric id — the identity key
        "uin": uin,
        "heading": (value.get("heading") or "").strip() or None,
        "question_text": question_text,
        "answer_text": answer_text,
        "asking_member": _member_name(value.get("askingMember")),
        "asking_mnis_id": value.get("askingMemberId") or None,
        "answering_member": _member_name(value.get("answeringMember")),
        "answering_mnis_id": value.get("answeringMemberId") or None,
        "answering_body": (value.get("answeringBodyName") or "").strip() or None,
        "answering_body_id": value.get("answeringBodyId") or None,
        "tabled_date":  _parse_date(value.get("dateTabled")),
        "answer_date":  _parse_date(value.get("dateAnswered")),
        "is_answered":  bool(answer_text),
        "is_holding":   _opt_bool(value.get("answerIsHolding")),
        "is_withdrawn": _opt_bool(value.get("isWithdrawn")),
        "chamber":      (value.get("house") or "").strip() or None,
    }


def _same_uin(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def find_existing_pq(api_id: int | None, tabled_date: date | None, uin: str | None):
    """Return (row, matched_by, ambiguous_detail).

    matched_by is "api_id", "date_uin" or None. ambiguous_detail is set when
    more than one stored row matches (should be impossible once the Phase 2
    unique indexes exist) — the caller treats that as a mismatch.
    """
    if api_id:
        rows = HaPQ.query.filter(HaPQ.api_id == api_id).limit(2).all()
        if len(rows) > 1:
            return None, "api_id", f"api_id={api_id} matches several rows"
        if rows:
            return rows[0], "api_id", ""
    if tabled_date and uin:
        rows = (
            HaPQ.query
            .filter(HaPQ.tabled_date == tabled_date,
                    func.lower(HaPQ.uin) == uin.strip().lower())
            .limit(2)
            .all()
        )
        if len(rows) > 1:
            return None, "date_uin", f"({tabled_date}, {uin}) matches several rows"
        if rows:
            return rows[0], "date_uin", ""
    return None, None, ""


def _planned_changes(existing: HaPQ, fields: dict, matched_by: str,
                     text_complete: bool) -> dict:
    changes: dict = {}

    def consider(name, new):
        old = getattr(existing, name)
        if new != old:
            changes[name] = (old, new)

    # Identity fields: only a case-only uin difference, or a missing api_id.
    if fields["uin"] != existing.uin:
        consider("uin", fields["uin"])
    if matched_by == "date_uin" and fields.get("api_id") and not existing.api_id:
        consider("api_id", fields["api_id"])

    for name in _REFRESH_IF_PRESENT:
        new = fields.get(name)
        if new is not None:
            consider(name, new)

    # Text: the bulk endpoint truncates question/answer text, so only a
    # complete record (individual endpoint) may replace stored text. A
    # bulk-only pass fills missing question text but NEVER writes answer text:
    # a truncated answer would look complete and never be re-fetched. It still
    # sets is_answered/answer_date, which makes the answered pass (and
    # _answer_already_stored) fetch the full answer.
    q_new, a_new = fields["question_text"], fields["answer_text"] or None
    if text_complete:
        consider("question_text", q_new)
        if a_new:
            consider("answer_text", a_new)
    elif not existing.question_text:
        consider("question_text", q_new)
    if fields["is_answered"] and not existing.is_answered:
        consider("is_answered", True)
    return changes


def upsert_pq(fields: dict, *, text_complete: bool = True,
              dry_run: bool = False) -> UpsertOutcome:
    """Insert or update one question. Caller owns the transaction/savepoint.

    With dry_run=True nothing is added or modified; the outcome says what would
    have happened.
    """
    existing, matched_by, ambiguous = find_existing_pq(
        fields.get("api_id"), fields.get("tabled_date"), fields.get("uin"))

    if ambiguous:
        return UpsertOutcome("identity_mismatch", detail=ambiguous)

    if existing is None:
        if not dry_run:
            row_fields = dict(fields)
            if not text_complete:
                row_fields["answer_text"] = None   # bulk answer is truncated
            row_fields["is_holding"] = bool(row_fields["is_holding"])
            row_fields["is_withdrawn"] = bool(row_fields["is_withdrawn"])
            pq = HaPQ(**row_fields)
            db.session.add(pq)
            db.session.flush()
            return UpsertOutcome("inserted", pq_id=pq.id)
        return UpsertOutcome("inserted")

    # Tripwire — the stored row and the API disagree about which question this is.
    if matched_by == "api_id" and (
        not _same_uin(existing.uin, fields["uin"])
        or existing.tabled_date != fields["tabled_date"]
    ):
        return UpsertOutcome(
            "identity_mismatch", pq_id=existing.id,
            detail=(f"api_id={fields.get('api_id')} row id={existing.id} stored "
                    f"uin={existing.uin} tabled={existing.tabled_date}; API "
                    f"uin={fields['uin']} tabled={fields['tabled_date']}"),
        )
    if (matched_by == "date_uin" and existing.api_id and fields.get("api_id")
            and existing.api_id != fields["api_id"]):
        return UpsertOutcome(
            "identity_mismatch", pq_id=existing.id,
            detail=(f"({fields['tabled_date']}, {fields['uin']}) row id={existing.id} "
                    f"stored api_id={existing.api_id}; API id={fields['api_id']}"),
        )

    changes = _planned_changes(existing, fields, matched_by, text_complete)
    if not changes:
        return UpsertOutcome("unchanged", pq_id=existing.id)

    if not dry_run:
        for name, (_old, new) in changes.items():
            setattr(existing, name, new)
        existing.updated_at = datetime.utcnow()
        if "question_text" in changes:
            # Tags describe the old text — drop them so the tagger redoes this row.
            HaPQTheme.query.filter_by(pq_id=existing.id).delete(synchronize_session=False)
        db.session.flush()
    return UpsertOutcome("updated", changes=changes, pq_id=existing.id)


def _complete_from_individual(fields: dict) -> bool:
    """Replace bulk (truncated) text with the individual record. True if it worked."""
    api_id = fields.get("api_id")
    if not api_id:
        return False
    full = _fetch_individual(api_id)
    if not full:
        return False
    full_q = _clean_whitespace(_strip_html(full.get("questionText") or "")) or None
    if not full_q:
        return False
    full_a = None
    if fields["is_answered"]:
        full_a = sanitize_answer_html(full.get("answerText") or "")
        if not full_a:
            return False   # keep bulk (truncated) answer from replacing stored text
    fields["question_text"] = full_q
    if full_a:
        fields["answer_text"] = full_a
    for key, api_key in (("is_holding", "answerIsHolding"), ("is_withdrawn", "isWithdrawn")):
        val = _opt_bool(full.get(api_key))
        if val is not None:
            fields[key] = val
    return True


def _answer_already_stored(fields: dict) -> bool:
    """True when the matching stored row already has this answer (same answer
    date, answered, full text present) — no need to re-fetch the full record.
    A missing answer, or one that looks like the truncated bulk copy, is
    re-fetched."""
    try:
        existing, _by, ambiguous = find_existing_pq(
            fields.get("api_id"), fields.get("tabled_date"), fields.get("uin"))
    except Exception:
        return False
    return bool(
        existing is not None and not ambiguous
        and existing.is_answered == fields["is_answered"]
        and existing.answer_date == fields["answer_date"]
        and (not fields["is_answered"]
             or (existing.answer_text and not _looks_truncated(existing.answer_text)))
    )


def _looks_truncated(answer_text: str | None) -> bool:
    """True for a stored answer that is probably the bulk endpoint's cut-off
    copy: exactly the cap length (the rule scripts/backfill_pq_answers.py
    Stage B uses), or close to it and ending in the "..." the API appends
    (sanitising can shift the length by a few characters)."""
    if not answer_text:
        return False
    n = len(answer_text)
    if n == _BULK_ANSWER_CAP:
        return True
    return (_BULK_ANSWER_CAP - 10 <= n <= _BULK_ANSWER_CAP + 40
            and answer_text.rstrip().endswith("..."))


def ingest_pq_date_range(
    date_from: date,
    date_to: date,
    verbose: bool = True,
    skip_answer_fetch: bool = False,
    dry_run: bool = False,
    max_identity_mismatches: int | None = None,
    request_delay: float | None = None,
    on_outcome: Callable[[dict, UpsertOutcome], None] | None = None,
    date_field: str = "tabled",
    fetch_only_new_answers: bool = False,
    max_failed_pages: int | None = None,
) -> dict:
    """
    Fetch and upsert WQs with tabledDate (or, with date_field="answered",
    dateAnswered) in [date_from, date_to].

    date_field: "tabled" or "answered". Answers usually arrive 1-4 weeks after
        tabling, so a tabled-date window alone misses them; the cron also runs
        an "answered in the last N days" pass.
    fetch_only_new_answers: only call the individual endpoint (full text) for
        rows that are new or whose answer is new/changed — keeps the answered
        pass cheap when its rows were already refreshed by the tabled pass.

    skip_answer_fetch: bulk records only (fast). Stored text is never replaced;
        missing question text is filled; answer text is never written (bulk
        answers are truncated) — only is_answered/answer_date, so a later
        full-fetch pass picks the answer up.
    dry_run: no writes; counts are predictions.
    max_identity_mismatches: abort (PQIdentityMismatchError) above this many.
    max_failed_pages: abort (PQPageFailureError) after this many consecutive
        pages fail all retries (default 3, env PQ_MAX_FAILED_PAGES). A single
        skipped page counts in page_errors, so the run is still reported as
        failed. A permanent API error (400/401/403) aborts at once. Paging stops
        at the API's totalResults, so a failed last page can't loop forever.
    request_delay: seconds between API requests (default 0.3).
    on_outcome: optional callback(fields, outcome) per row, e.g. for sample diffs.

    Returns {"inserted", "updated", "unchanged", "identity_mismatch", "failed",
    "page_errors", "rolled_back", "errors"} — errors = failed + page_errors +
    identity_mismatch (kept for callers that check a single number).
    rolled_back: writes discarded by a mismatch abort (not in inserted/updated).
    Aborts raise a PQIngestAborted subclass whose .counts has the same keys.
    """
    if max_identity_mismatches is None:
        max_identity_mismatches = DEFAULT_MAX_IDENTITY_MISMATCHES
    if max_failed_pages is None:
        max_failed_pages = DEFAULT_MAX_FAILED_PAGES
    delay = _INTER_REQUEST_DELAY if request_delay is None else request_delay

    counts = {"inserted": 0, "updated": 0, "unchanged": 0,
              "identity_mismatch": 0, "failed": 0, "page_errors": 0,
              "rolled_back": 0}
    uncommitted = {"inserted": 0, "updated": 0}   # written since the last commit

    def result() -> dict:
        out = dict(counts)
        out["errors"] = counts["failed"] + counts["page_errors"] + counts["identity_mismatch"]
        return out

    def _commit() -> None:
        db.session.commit()
        uncommitted["inserted"] = uncommitted["updated"] = 0

    def _finish() -> None:
        """End of run (normal or page-failure abort): keep what was written."""
        if dry_run:
            db.session.rollback()
        elif any(uncommitted.values()):
            _commit()

    def _discard() -> None:
        """Mismatch abort: roll back uncommitted writes and stop counting them."""
        db.session.rollback()
        for action, n in uncommitted.items():
            counts[action] -= n
            counts["rolled_back"] += n
            uncommitted[action] = 0

    if date_field not in ("tabled", "answered"):
        raise ValueError(f"date_field must be 'tabled' or 'answered', not {date_field!r}")
    params_base = {
        f"{date_field}WhenFrom": date_from.isoformat(),
        f"{date_field}WhenTo": date_to.isoformat(),
        "take": _PAGE_SIZE,
        "expandMember": "true",   # otherwise askingMember/answeringMember are null
    }

    skip = 0
    page = 0
    total_results = None      # from the latest successful page
    failed_in_a_row = 0

    while True:
        params = {**params_base, "skip": skip}
        payload = None
        for attempt in range(_PAGE_RETRIES):
            try:
                resp = requests.get(WQ_API_BASE, params=params, timeout=_REQUEST_TIMEOUT)
                if resp.status_code in (401, 403, 400):
                    counts["page_errors"] += 1
                    _finish()
                    msg = (f"PQ ingest aborted: WQ API permanent error {resp.status_code} "
                           f"at skip={skip} ({date_field} {date_from}..{date_to})")
                    _log.error(msg)
                    raise PQPageFailureError(msg, result())
                resp.raise_for_status()
                body = resp.json()
                # An error body or a page without a results list is a failed
                # attempt, not "no more questions" (which would end the run early).
                if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                    raise ValueError(f"unexpected WQ API payload: {str(body)[:200]}")
                payload = body
                break
            except PQPageFailureError:
                raise
            except Exception as exc:
                _log.warning("WQ API error at skip=%d attempt %d/%d: %s", skip, attempt + 1, _PAGE_RETRIES, exc)
                if attempt < _PAGE_RETRIES - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s
                else:
                    _log.error("WQ API failed after %d attempts at skip=%d — skipping page", _PAGE_RETRIES, skip)
                    counts["page_errors"] += 1

        if payload is None:
            # All retries exhausted for this page. Give up after a few in a
            # row (the API is down) rather than walking skip forever.
            failed_in_a_row += 1
            if failed_in_a_row >= max_failed_pages:
                _finish()   # rows already processed are good
                msg = (f"PQ ingest aborted: {failed_in_a_row} consecutive API pages "
                       f"failed at skip={skip} ({date_field} {date_from}..{date_to})")
                _log.error(msg)
                raise PQPageFailureError(msg, result())
            skip += _PAGE_SIZE
            if total_results is not None and skip >= total_results:
                break             # the failed page was the last one
            continue

        failed_in_a_row = 0
        if payload.get("totalResults") is not None:
            total_results = payload["totalResults"]
        results = payload.get("results") or []
        if not results:
            if total_results and skip < total_results:
                _log.error("WQ API returned an empty page at skip=%d of %d - stopping",
                           skip, total_results)
                counts["page_errors"] += 1
            break

        for item in results:
            value = item.get("value") or item
            fields = _extract_pq_fields(value)
            if fields is None or fields["tabled_date"] is None:
                counts["failed"] += 1
                continue

            # The bulk endpoint truncates questionText (255 chars); the
            # individual endpoint returns the full record. skip_answer_fetch
            # bypasses this for bulk passes where speed matters more.
            text_complete = False
            want_full = not skip_answer_fetch and fields.get("api_id")
            if want_full and fetch_only_new_answers and _answer_already_stored(fields):
                want_full = False
            if want_full:
                text_complete = _complete_from_individual(fields)
                time.sleep(delay)

            try:
                if dry_run:
                    outcome = upsert_pq(fields, text_complete=text_complete, dry_run=True)
                else:
                    with db.session.begin_nested():
                        outcome = upsert_pq(fields, text_complete=text_complete)
            except Exception as exc:
                _log.error("Row upsert error api_id=%s uin=%s tabled=%s: %s",
                           fields.get("api_id"), fields.get("uin"), fields.get("tabled_date"), exc)
                counts["failed"] += 1
                continue

            counts[outcome.action] += 1
            if outcome.action in ("inserted", "updated") and not dry_run:
                uncommitted[outcome.action] += 1
            if on_outcome is not None:
                on_outcome(fields, outcome)

            if outcome.action == "identity_mismatch":
                _log.warning("PQ identity mismatch (left untouched): %s", outcome.detail)
                if counts["identity_mismatch"] > max_identity_mismatches:
                    _discard()   # drop this batch's uncommitted writes
                    msg = (f"PQ ingest aborted: {counts['identity_mismatch']} identity "
                           f"mismatches (limit {max_identity_mismatches}) in "
                           f"{date_from}..{date_to}; {counts['rolled_back']} uncommitted "
                           f"row write(s) rolled back")
                    _log.error(msg)
                    raise PQIdentityMismatchError(msg, result())

            if not dry_run and sum(uncommitted.values()) >= _COMMIT_BATCH:
                _commit()

        page += 1
        if verbose:
            _log.info(
                "Page %d (skip=%d): %d results — running totals ins=%d upd=%d same=%d "
                "mismatch=%d failed=%d",
                page, skip, len(results), counts["inserted"], counts["updated"],
                counts["unchanged"], counts["identity_mismatch"], counts["failed"],
            )

        if len(results) < _PAGE_SIZE:
            break

        skip += _PAGE_SIZE
        if total_results is not None and skip >= total_results:
            break
        time.sleep(delay)

    _finish()

    out = result()
    if verbose:
        _log.info(
            "ingest_pq_date_range (%s) done%s: inserted=%d updated=%d unchanged=%d "
            "identity_mismatch=%d failed=%d page_errors=%d",
            date_field, " (dry run)" if dry_run else "",
            out["inserted"], out["updated"], out["unchanged"],
            out["identity_mismatch"], out["failed"], out["page_errors"],
        )
    return out
