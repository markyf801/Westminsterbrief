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
(Parliament's answeredWhenFrom/answeredWhenTo filter) for recent answers, and
with date_field="corrected" (correctedWhenFrom/correctedWhenTo) for answers
corrected recently: a correction keeps the original dateAnswered and sets
dateAnswerCorrected, so neither of the other windows finds an older one.
"""

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

import requests
from sqlalchemy import func

from extensions import db
from hansard_archive.html_sanitizer import sanitize_answer_html
from hansard_archive.models import HaPQ, HaPQDeadline, HaPQTheme

_log = logging.getLogger(__name__)

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
# (connect, read) seconds. Pages have taken 20-50 s and a 500 can take 40+ s
# to arrive, so 60 s risked cutting off good pages (30 s was already too tight).
_REQUEST_TIMEOUT = (10, 90)
_PAGE_SIZE = 500
_INTER_REQUEST_DELAY = 0.3   # seconds between paginated requests
# A list page that fails with a transient error (5xx, timeout, connection
# error) is tried up to 5 times, pausing 15, 30, 60 and 120 s (plus up to 20%
# jitter) in between. Then it is tried as smaller pages of _FALLBACK_PAGE_SIZE
# (same rows, skip moved on 100 at a time), each tried twice; two smaller pages
# failing in a row give the page up. A 4xx is never retried (429 = stop).
_PAGE_BACKOFF = (15, 30, 60, 120)
_PAGE_ATTEMPTS = len(_PAGE_BACKOFF) + 1
_FALLBACK_PAGE_SIZE = 100
_FALLBACK_ATTEMPTS = 2
_FALLBACK_FAILS_TO_GIVE_UP = 2
# Individual-record requests failing in a row before a pass treats the API as
# down (otherwise a dead API costs one timeout per row, 500 per page).
_MAX_FULL_FETCH_FAILURES = 10

_BULK_ANSWER_CAP = 258       # bulk endpoint cuts answerText at this length
_BULK_QUESTION_CAP = 255     # bulk endpoint cuts questionText at this length (no marker)
# A bulk questionText this long or longer may have been cut (the cut can be
# followed by a trim, or land a few characters early): it is never taken as
# the whole question, so a stored copy equal to it is fetched again.
_QUESTION_MAYBE_CUT = _BULK_QUESTION_CAP - 10
# Progress callback (and a commit) every this many rows or seconds inside a
# page, when the caller asks for progress (chunked runs' heartbeat).
_PROGRESS_ROWS = 50
_PROGRESS_SECONDS = 300
# "Looks truncated" window: near the cap and ending in the API's "...".
_TRUNC_MIN, _TRUNC_MAX = _BULK_ANSWER_CAP - 10, _BULK_ANSWER_CAP + 40


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
    "is_holding", "is_withdrawn", "answer_corrected_date",
)
# Belong with the stored answer text; only a complete record may change them
# once an answer is stored (see _planned_changes).
_ANSWER_META = ("answer_date", "is_holding", "answer_corrected_date")

_DATE_FIELDS = ("tabled", "answered", "corrected")   # Parliament's <x>WhenFrom/<x>WhenTo


class PQIngestAborted(RuntimeError):
    """A run stopped early. ``counts`` holds the totals up to that point."""

    def __init__(self, message: str, counts: dict):
        super().__init__(message)
        self.counts = counts


class PQIdentityMismatchError(PQIngestAborted):
    """Raised when a run sees more identity mismatches than allowed."""


class PQPageFailureError(PQIngestAborted):
    """Raised when too many consecutive API pages fail (or the API refuses)."""


class PQRateLimitedError(PQPageFailureError):
    """Parliament's API answered 429: the run stops at once, nothing retried."""


class PQTimeBudgetError(PQIngestAborted):
    """The pass reached its time budget (deadline=) and stopped early."""


class _APIRefused(Exception):
    """A 4xx from the WQ API: never retried."""

    def __init__(self, status: int, where: str):
        super().__init__(f"HTTP {status} at {where}")
        self.status = status


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
    bulk endpoint omits or truncates (full question and answer text).

    None when the request fails (the answer is left for the next run); {}
    for a 404 (the record has gone: the bulk record is used, not a failure).
    401/403/429 raise _APIRefused: a rate limit or refusal stops the run."""
    url = f"{WQ_API_BASE}/{api_id}"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
    except Exception as exc:
        _log.warning("Individual fetch failed for id=%s: %s", api_id, exc)
        return None
    if resp.status_code in (401, 403, 429):
        raise _APIRefused(resp.status_code, f"individual record id={api_id}")
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        return None
    try:
        return resp.json().get("value") or {}
    except Exception as exc:
        _log.warning("Individual fetch failed for id=%s: %s", api_id, exc)
        return None


def _jittered(seconds: float) -> float:
    return seconds + random.uniform(0, seconds * 0.2)


def _get_list(params: dict, attempts: int, where: str) -> dict | None:
    """One list request, retried on transient errors (5xx, timeout, connection
    error, a body without a results list) with growing pauses. None when every
    attempt failed. A 4xx is never retried: it raises _APIRefused."""
    for attempt in range(attempts):
        try:
            resp = requests.get(WQ_API_BASE, params=params, timeout=_REQUEST_TIMEOUT)
            if 400 <= resp.status_code < 500:
                raise _APIRefused(resp.status_code, where)
            if resp.status_code != 200:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            body = resp.json()
            # An error body or a page without a results list is a failed
            # attempt, not "no more questions" (which would end the run early).
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise ValueError(f"unexpected WQ API payload: {str(body)[:200]}")
            return body
        except _APIRefused:
            raise
        except Exception as exc:
            _log.warning("WQ API error at %s attempt %d/%d: %s", where, attempt + 1, attempts, exc)
            if attempt < attempts - 1:
                time.sleep(_jittered(_PAGE_BACKOFF[min(attempt, len(_PAGE_BACKOFF) - 1)]))
    return None


@dataclass
class _Page:
    results: list
    total: int | None
    complete: bool       # False: part of the page could not be fetched


def _fetch_page(params_base: dict, skip: int, take: int, where: str,
                delay: float) -> _Page | None:
    """The page at skip, with retries, then as smaller pages (skip moved on
    _FALLBACK_PAGE_SIZE at a time) if the full page keeps failing. None when
    nothing of the page could be fetched."""
    body = _get_list({**params_base, "skip": skip, "take": take}, _PAGE_ATTEMPTS,
                     f"skip={skip} ({where})")
    if body is not None:
        return _Page(body["results"], body.get("totalResults"), True)
    small = _FALLBACK_PAGE_SIZE
    if small >= take:
        _log.error("WQ API failed after %d attempts at skip=%d (%s)", _PAGE_ATTEMPTS, skip, where)
        return None
    _log.warning("WQ API page skip=%d take=%d kept failing - trying it as pages of %d (%s)",
                 skip, take, small, where)
    results: list = []
    total = None
    fetched = missed = fails_in_a_row = 0
    for sub_skip in range(skip, skip + take, small):
        if total is not None and sub_skip >= total:
            break
        time.sleep(delay)
        sub = _get_list({**params_base, "skip": sub_skip, "take": small}, _FALLBACK_ATTEMPTS,
                        f"skip={sub_skip} take={small} ({where})")
        if sub is None:
            missed += 1
            fails_in_a_row += 1
            if fails_in_a_row >= _FALLBACK_FAILS_TO_GIVE_UP:
                break
            continue
        fails_in_a_row = 0
        fetched += 1
        if sub.get("totalResults") is not None:
            total = sub["totalResults"]
        results.extend(sub["results"])
        if len(sub["results"]) < small:
            break
    if not fetched:
        _log.error("WQ API page skip=%d failed, smaller pages too (%s)", skip, where)
        return None
    if missed:
        _log.error("WQ API page skip=%d only partly fetched: %d row(s) from %d smaller "
                   "page(s), %d smaller page(s) failed (%s)", skip, len(results), fetched,
                   missed, where)
    return _Page(results, total, missed == 0)


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
        "answer_corrected_date": _parse_date(value.get("dateAnswerCorrected")),
        "is_answered":  bool(answer_text),
        "is_holding":   _opt_bool(value.get("answerIsHolding")),
        "is_withdrawn": _opt_bool(value.get("isWithdrawn")),
        "chamber":      (value.get("house") or "").strip() or None,
    }


def _extract_deadline(value: dict) -> dict | None:
    """Due-for-answer date and named-day flag (ha_pq_deadline), keyed by
    Parliament's question id. None when the record has no id."""
    api_id = value.get("id")
    if not api_id:
        return None
    return {
        "api_id": api_id,
        "date_for_answer": _parse_date(value.get("dateForAnswer")),
        "is_named_day": _opt_bool(value.get("isNamedDay")),
    }


def store_deadlines(rows: list[dict]) -> int:
    """Insert or refresh ha_pq_deadline rows (one IN query for the batch).

    Like the ha_pq update rule, a value the API leaves out never blanks a
    stored one. Returns the number of rows added or changed. Caller owns the
    transaction/savepoint.
    """
    by_id = {r["api_id"]: r for r in rows if r and r.get("api_id")}
    if not by_id:
        return 0
    existing = {d.api_id: d for d in
                HaPQDeadline.query.filter(HaPQDeadline.api_id.in_(list(by_id))).all()}
    now = datetime.utcnow()
    written = 0
    for api_id, r in by_id.items():
        values = {k: r[k] for k in ("date_for_answer", "is_named_day") if r[k] is not None}
        row = existing.get(api_id)
        if row is None:
            if values:
                db.session.add(HaPQDeadline(api_id=api_id, updated_at=now, **values))
                written += 1
            continue
        changed = {k: v for k, v in values.items() if getattr(row, k) != v}
        if changed:
            for k, v in changed.items():
                setattr(row, k, v)
            row.updated_at = now
            written += 1
    db.session.flush()
    return written


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

    # Without the full record, leave the answer's date, holding flag and
    # correction date alone while an answer is stored: they must change
    # together with its text. A holding answer later replaced by the real one,
    # or a corrected answer, keeps its old values, so the difference stays
    # visible and the next full-fetch pass (_full_record_already_stored, Stage B)
    # re-fetches it.
    keep_answer_meta = not text_complete and bool(existing.answer_text)
    for name in _REFRESH_IF_PRESENT:
        if keep_answer_meta and name in _ANSWER_META:
            continue
        new = fields.get(name)
        if new is not None:
            consider(name, new)

    # Text: the bulk endpoint truncates question/answer text, so only a
    # complete record (individual endpoint) may replace stored text. A
    # bulk-only pass fills missing question text but NEVER writes answer text:
    # a truncated answer would look complete and never be re-fetched. It still
    # sets is_answered (and answer_date while no answer is stored), which makes
    # the answered pass (and _full_record_already_stored) fetch the full answer.
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


def _apply_individual(fields: dict, full: dict | None) -> bool:
    """Replace bulk (truncated) text, flags and answer dates in fields with the
    individual record's. True if the record was complete enough to use."""
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
    # The answer's dates from the same record as its text.
    for key, api_key in (("answer_date", "dateAnswered"),
                         ("answer_corrected_date", "dateAnswerCorrected")):
        val = _parse_date(full.get(api_key))
        if val is not None:
            fields[key] = val
    return True


def _question_text_complete(stored: str | None, bulk: str, bulk_raw_len: int) -> bool:
    """True when the stored question text is the full text of this bulk record.

    The bulk endpoint cuts questionText at _BULK_QUESTION_CAP characters with
    no marker. A clearly shorter bulk text is the whole question, so the
    stored text must equal it. One of _QUESTION_MAYBE_CUT characters or more
    may be cut (cut and then trimmed, say), so it must be a strict prefix of
    the stored text: a stored copy equal to it is fetched again. (A question
    whose full text really is that long is fetched on every pass: a few per
    cent of questions, the price of never keeping a cut one.) Text that
    differs any other way (edited at source) is fetched again too."""
    if not stored:
        return False
    if bulk_raw_len < _QUESTION_MAYBE_CUT:
        return stored == bulk
    return len(stored) > len(bulk) and stored.startswith(bulk)


def _full_record_already_stored(fields: dict, bulk_question_len: int) -> bool:
    """True when the matching stored row already has everything the
    individual record would add: the full question text
    (_question_text_complete) and this answer (same answer date, holding flag
    and correction date, answered, full text present) — no need to fetch the
    full record. A new row, a cut question, a missing answer, one that looks
    like the truncated bulk copy, a holding answer since replaced (even on the
    same day, when answer_date is unchanged) and a newly corrected answer are
    fetched. An answer field the API leaves out is not compared."""
    try:
        existing, _by, ambiguous = find_existing_pq(
            fields.get("api_id"), fields.get("tabled_date"), fields.get("uin"))
    except Exception:
        return False
    if existing is None or ambiguous:
        return False
    if not _question_text_complete(existing.question_text, fields["question_text"],
                                   bulk_question_len):
        return False
    if fields.get("is_holding") is not None and bool(existing.is_holding) != fields["is_holding"]:
        return False
    corrected = fields.get("answer_corrected_date")
    if corrected is not None and existing.answer_corrected_date != corrected:
        return False
    return bool(
        existing.is_answered == fields["is_answered"]
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
    return _TRUNC_MIN <= n <= _TRUNC_MAX and answer_text.rstrip().endswith("...")


def answer_needs_fetch_sql(col):
    """SQL form of "missing or _looks_truncated" for an answer_text column
    (scripts and checks use the same rule as the ingestor)."""
    from sqlalchemy import and_, or_
    length = func.length(col)
    return or_(
        col.is_(None), col == "",
        length == _BULK_ANSWER_CAP,
        and_(length.between(_TRUNC_MIN, _TRUNC_MAX), func.rtrim(col).like("%...")),
    )


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
    page_size: int | None = None,
    seen_ids: set | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    deadline: float | None = None,
) -> dict:
    """
    Fetch and upsert WQs with tabledDate (or, with date_field="answered",
    dateAnswered; with date_field="corrected", dateAnswerCorrected) in
    [date_from, date_to].

    date_field: "tabled", "answered" or "corrected". Answers usually arrive 1-4
        weeks after tabling, so a tabled-date window alone misses them; the
        cron also runs "answered in the last N days" and "corrected in the
        last N days" passes.
    fetch_only_new_answers: only call the individual endpoint (full text) for
        rows that are new, whose stored question text is not the full text
        (the bulk endpoint cuts it at 255 characters), or whose answer is
        new/changed (including a holding answer replaced or an answer
        corrected) — keeps every pass cheap when its rows are already up to
        date. The cron uses it for all passes, the tabled one included.

    skip_answer_fetch: bulk records only (fast). Stored text is never replaced;
        missing question text is filled; answer text is never written (bulk
        answers are truncated) — only is_answered/answer_date, so a later
        full-fetch pass picks the answer up.
    dry_run: no writes; counts are predictions.
    max_identity_mismatches: abort (PQIdentityMismatchError) above this many.
    max_failed_pages: abort (PQPageFailureError) after this many consecutive
        pages fail all retries and the smaller-page fallback (default 3, env
        PQ_MAX_FAILED_PAGES). A single skipped or partly fetched page counts in
        page_errors, so the run is still reported as failed. A 4xx aborts at
        once without a retry (429: PQRateLimitedError). Paging stops at the
        API's totalResults, so a failed last page can't loop forever. Ten
        individual-record requests failing in a row also abort (API down).
    request_delay: seconds between API requests (default 0.3).
    page_size: list page size (default 500).
    seen_ids: optional set the caller owns; every api_id on the pages fetched
        is added to it (chunked runs compare it with api_total).
    on_outcome: optional callback(fields, outcome) per row, e.g. for sample diffs.
    on_progress: optional callback(skip, rows_done_on_page) after each page,
        fetched or not, and inside a page every 50 rows or 5 minutes (after a
        commit, so it may write to the DB itself). Chunked runs use it as a
        heartbeat.
    deadline: optional time.monotonic() value; past it no new page or
        individual-record request is started, what was written is kept and
        PQTimeBudgetError is raised (scheduled sweeps' time budget).

    Each page also refreshes ha_pq_deadline (date due for answer, named day;
    store_deadlines) for its rows, in a separate savepoint so a failure there
    never affects the questions. Not part of the returned counts.

    Returns {"inserted", "updated", "unchanged", "identity_mismatch", "failed",
    "page_errors", "rolled_back", "full_fetched", "full_fetch_failed",
    "errors", "api_total"} — errors = failed + page_errors + identity_mismatch
    (kept for callers that check a single number). rolled_back: writes
    discarded by a mismatch abort (not in inserted/updated). full_fetched /
    full_fetch_failed: individual-record requests that worked / failed (a
    failed one leaves that row's text for the next run; not an error).
    api_total: the API's totalResults on the latest page fetched (None if no
    page was). Aborts raise a PQIngestAborted subclass whose .counts has the
    same keys.
    """
    if max_identity_mismatches is None:
        max_identity_mismatches = DEFAULT_MAX_IDENTITY_MISMATCHES
    if max_failed_pages is None:
        max_failed_pages = DEFAULT_MAX_FAILED_PAGES
    delay = _INTER_REQUEST_DELAY if request_delay is None else request_delay
    take = _PAGE_SIZE if page_size is None else page_size

    counts = {"inserted": 0, "updated": 0, "unchanged": 0,
              "identity_mismatch": 0, "failed": 0, "page_errors": 0,
              "rolled_back": 0, "full_fetched": 0, "full_fetch_failed": 0}
    # Written since the last commit. "deadlines" = ha_pq_deadline rows, which
    # are not part of the inserted/updated counts.
    uncommitted = {"inserted": 0, "updated": 0, "deadlines": 0}
    deadlines = {"written": 0, "enabled": not dry_run}
    total_results = None      # from the latest successful page

    def result() -> dict:
        out = dict(counts)
        out["errors"] = counts["failed"] + counts["page_errors"] + counts["identity_mismatch"]
        out["api_total"] = total_results
        return out

    def _commit() -> None:
        db.session.commit()
        deadlines["written"] += uncommitted["deadlines"]
        for key in uncommitted:
            uncommitted[key] = 0

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
            if action in counts:
                counts[action] -= n
                counts["rolled_back"] += n
            uncommitted[action] = 0

    if date_field not in _DATE_FIELDS:
        raise ValueError(f"date_field must be one of {_DATE_FIELDS}, not {date_field!r}")
    params_base = {
        f"{date_field}WhenFrom": date_from.isoformat(),
        f"{date_field}WhenTo": date_to.isoformat(),
        "expandMember": "true",   # otherwise askingMember/answeringMember are null
    }
    where = f"{date_field} {date_from}..{date_to}"

    skip = 0
    page = 0
    failed_in_a_row = 0
    fetch_fails_in_a_row = 0
    progress = {"rows": 0, "at": time.monotonic()}

    def _check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            _finish()
            msg = (f"PQ ingest stopped at its time budget at skip={skip} ({where}); "
                   f"the rest is left for the next run")
            _log.error(msg)
            raise PQTimeBudgetError(msg, result())

    def _report_progress(rows_done: int) -> None:
        if on_progress is None:
            return
        if not dry_run and any(uncommitted.values()):
            _commit()
        on_progress(skip, rows_done)
        progress["rows"], progress["at"] = 0, time.monotonic()

    try:
        while True:
            _check_deadline()
            fetched = _fetch_page(params_base, skip, take, where, delay)

            if fetched is None:
                # All retries and the smaller pages failed. Give up after a few
                # in a row (the API is down) rather than walking skip forever.
                counts["page_errors"] += 1
                failed_in_a_row += 1
                _report_progress(0)
                if failed_in_a_row >= max_failed_pages:
                    _finish()   # rows already processed are good
                    msg = (f"PQ ingest aborted: {failed_in_a_row} consecutive API pages "
                           f"failed at skip={skip} ({where})")
                    _log.error(msg)
                    raise PQPageFailureError(msg, result())
                skip += take
                if total_results is not None and skip >= total_results:
                    break             # the failed page was the last one
                continue

            failed_in_a_row = 0
            if not fetched.complete:
                counts["page_errors"] += 1    # some rows of this page were not fetched
            if fetched.total is not None:
                total_results = fetched.total
            results = fetched.results
            if not results:
                if total_results and skip < total_results:
                    _log.error("WQ API returned an empty page at skip=%d of %d - stopping",
                               skip, total_results)
                    counts["page_errors"] += 1
                break

            page_deadlines = []
            for row_no, item in enumerate(results, 1):
                progress["rows"] += 1
                if on_progress is not None and (
                        progress["rows"] >= _PROGRESS_ROWS
                        or time.monotonic() - progress["at"] >= _PROGRESS_SECONDS):
                    _report_progress(row_no - 1)
                value = item.get("value") or item
                if seen_ids is not None and value.get("id"):
                    seen_ids.add(value["id"])
                fields = _extract_pq_fields(value)
                if fields is None or fields["tabled_date"] is None:
                    counts["failed"] += 1
                    continue

                # The bulk endpoint truncates questionText (255 chars); the
                # individual endpoint returns the full record. skip_answer_fetch
                # bypasses this for bulk passes where speed matters more.
                text_complete = False
                want_full = not skip_answer_fetch and fields.get("api_id")
                if want_full and fetch_only_new_answers and _full_record_already_stored(
                        fields, len(value.get("questionText") or "")):
                    want_full = False
                if want_full:
                    _check_deadline()
                    full = _fetch_individual(fields["api_id"])
                    if full is None:
                        counts["full_fetch_failed"] += 1
                        fetch_fails_in_a_row += 1
                        if fetch_fails_in_a_row >= _MAX_FULL_FETCH_FAILURES:
                            _finish()
                            msg = (f"PQ ingest aborted: {fetch_fails_in_a_row} individual "
                                   f"record requests failed in a row at skip={skip} ({where})")
                            _log.error(msg)
                            raise PQPageFailureError(msg, result())
                    else:
                        fetch_fails_in_a_row = 0
                        counts["full_fetched"] += 1
                    text_complete = _apply_individual(fields, full)
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
                if outcome.action != "identity_mismatch":
                    page_deadlines.append(_extract_deadline(value))
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

            if deadlines["enabled"] and page_deadlines:
                # Own savepoint: a deadline problem (e.g. the table not created
                # yet) must never cost the question rows themselves.
                try:
                    with db.session.begin_nested():
                        uncommitted["deadlines"] += store_deadlines(page_deadlines)
                except Exception as exc:
                    deadlines["enabled"] = False
                    _log.error("ha_pq_deadline not written - due dates skipped for the rest of "
                               "this run (has migrations/pq_deadline_table.py been run?): %s", exc)

            # Commit every page: keeps the window in which an overlapping run could
            # insert the same question small (until uq_ha_pq_api_id exists).
            if not dry_run and any(uncommitted.values()):
                _commit()

            page += 1
            if verbose:
                _log.info(
                    "Page %d (skip=%d): %d results — running totals ins=%d upd=%d same=%d "
                    "mismatch=%d failed=%d",
                    page, skip, len(results), counts["inserted"], counts["updated"],
                    counts["unchanged"], counts["identity_mismatch"], counts["failed"],
                )
            _report_progress(len(results))

            # A partly fetched page is short because rows are missing, not
            # because it is the last page: carry on to the next one.
            if fetched.complete and len(results) < take:
                break

            skip += take
            if total_results is not None and skip >= total_results:
                break
            time.sleep(delay)
    except _APIRefused as exc:
        counts["page_errors"] += 1
        _finish()
        if exc.status == 429:
            msg = (f"PQ ingest stopped: Parliament's API is rate limiting (HTTP 429) at "
                   f"{exc} ({where}) - not retried; wait before running again")
            _log.error(msg)
            raise PQRateLimitedError(msg, result()) from None
        msg = f"PQ ingest aborted: WQ API permanent error {exc} ({where})"
        _log.error(msg)
        raise PQPageFailureError(msg, result()) from None

    _finish()

    out = result()
    if verbose:
        _log.info(
            "ingest_pq_date_range (%s) done%s: inserted=%d updated=%d unchanged=%d "
            "identity_mismatch=%d failed=%d page_errors=%d full_fetched=%d "
            "full_fetch_failed=%d deadlines_written=%d",
            date_field, " (dry run)" if dry_run else "",
            out["inserted"], out["updated"], out["unchanged"],
            out["identity_mismatch"], out["failed"], out["page_errors"],
            out["full_fetched"], out["full_fetch_failed"], deadlines["written"],
        )
    return out
