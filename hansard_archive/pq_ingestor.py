"""
Hansard Archive — Written Questions ingestion from Parliament WQ API.

Entry point:
  ingest_pq_date_range(date_from, date_to) — fetch and upsert all WQs
      with tabledDate in [date_from, date_to]. Returns insert/update/error counts.

Upsert key is UIN. Re-running over the same window is safe — already-inserted
rows are updated if the answer status has changed, otherwise left untouched.

No answered filter — fetches all statuses to capture both new questions and
answer updates on re-runs. The 7-day rolling window per cron run is sufficient
to catch questions answered after tabling.
"""

import logging
import re
import time
from datetime import date, datetime

import requests

_log = logging.getLogger(__name__)

from extensions import db
from hansard_archive.models import HaPQ, HaPQTheme

WQ_API_BASE = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_REQUEST_TIMEOUT = 30
_PAGE_SIZE = 500
_COMMIT_BATCH = 200
_INTER_REQUEST_DELAY = 0.3   # seconds between paginated requests


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


def _extract_pq_fields(value: dict) -> dict | None:
    """Extract and normalise fields from a WQ API value object."""
    uin = (value.get("uin") or "").strip()
    if not uin:
        return None

    question_raw = value.get("questionText") or ""
    question_text = _clean_whitespace(_strip_html(question_raw))
    if not question_text:
        return None

    answer_raw = value.get("answerText") or ""
    answer_text = _clean_whitespace(_strip_html(answer_raw)) or None

    asking = value.get("askingMember") or {}
    answering_member_obj = value.get("answeringMember") or {}
    answering_body_obj = value.get("answeringBody") or {}

    return {
        "uin": uin,
        "heading": (value.get("heading") or "").strip() or None,
        "question_text": question_text,
        "answer_text": answer_text,
        "asking_member": (asking.get("name") or "").strip() or None,
        "asking_mnis_id": asking.get("memberId") or None,
        "answering_member": (answering_member_obj.get("name") or "").strip() or None,
        "answering_body": (answering_body_obj.get("name") or "").strip() or None,
        "answering_body_id": answering_body_obj.get("id") or None,
        "tabled_date": _parse_date(value.get("dateTabled")),
        "answer_date": _parse_date(value.get("dateAnswered")),
        "is_answered": bool(answer_text),
        "chamber": (value.get("house") or "").strip() or None,
    }


def ingest_pq_date_range(
    date_from: date,
    date_to: date,
    verbose: bool = True,
) -> dict:
    """
    Fetch and upsert WQs with tabledDate in [date_from, date_to].

    Returns {"inserted": int, "updated": int, "errors": int}.
    """
    inserted = updated = errors = 0
    pending = 0       # rows buffered since last commit

    params_base = {
        "tabledWhenFrom": date_from.isoformat(),
        "tabledWhenTo": date_to.isoformat(),
        "take": _PAGE_SIZE,
    }

    skip = 0
    page = 0

    while True:
        params = {**params_base, "skip": skip}
        try:
            resp = requests.get(WQ_API_BASE, params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            _log.error("WQ API error at skip=%d: %s", skip, exc)
            errors += 1
            break

        results = payload.get("results") or []
        if not results:
            break

        for item in results:
            value = item.get("value") or item
            fields = _extract_pq_fields(value)
            if fields is None or fields["tabled_date"] is None:
                errors += 1
                continue

            try:
                existing = db.session.query(HaPQ).filter_by(uin=fields["uin"]).first()
                if existing:
                    existing.answer_text = fields["answer_text"]
                    existing.answer_date = fields["answer_date"]
                    existing.is_answered = fields["is_answered"]
                    existing.answering_member = fields["answering_member"]
                    existing.updated_at = datetime.utcnow()
                    updated += 1
                else:
                    db.session.add(HaPQ(**fields))
                    inserted += 1
                pending += 1
            except Exception as exc:
                _log.error("Row upsert error uin=%s: %s", fields.get("uin"), exc)
                db.session.rollback()
                errors += 1
                pending = 0
                continue

            if pending >= _COMMIT_BATCH:
                db.session.commit()
                pending = 0

        page += 1
        if verbose:
            _log.info(
                "Page %d (skip=%d): %d results — running totals ins=%d upd=%d err=%d",
                page, skip, len(results), inserted, updated, errors,
            )

        if len(results) < _PAGE_SIZE:
            break

        skip += _PAGE_SIZE
        time.sleep(_INTER_REQUEST_DELAY)

    if pending:
        db.session.commit()

    if verbose:
        _log.info(
            "ingest_pq_date_range done: inserted=%d updated=%d errors=%d",
            inserted, updated, errors,
        )

    return {"inserted": inserted, "updated": updated, "errors": errors}
