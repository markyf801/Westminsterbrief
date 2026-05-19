"""
Workstream B -- Hansard session links per bill stage.

attach_session_links(bill, status) mutates the StageState objects inside
`status.stages` to populate session link fields:

  local_session_slug / local_session_url_date
      Set when a matching ha_session exists in the local Hansard archive.
      Template builds: /archive/debate/{url_date}/{slug}

  hansard_fallback_url
      Set for debate stages that fall outside the local archive window, or
      where no matching session is found.  Links to Parliament's own Hansard
      at https://hansard.parliament.uk/{House}/{YYYY-MM-DD}.

Procedural stages (has_debate=False) are skipped entirely.

Matching strategy (per date + house):
  1. Normalise the bill title: strip "Act YYYY", "Bill", "[HL]", lowercase.
  2. Normalise each candidate session title the same way.
  3. Accept if norm_bill is a substring of norm_session_title (covers
     "Planning and Infrastructure Bill", "Planning and Infrastructure Bill
     -- Lords Grand Committee", etc.).
  4. If no title match and exactly one candidate exists, accept it (rare
     for bill committee stages, safe fallback).
  5. If no match: set hansard_fallback_url (within archive window) or
     hansard_fallback_url (outside window).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

from extensions import db
from hansard_archive.models import HansardSession
from hansard_archive.bill_stages import BillStatus, StageState

if TYPE_CHECKING:
    from hansard_archive.models import HaBill


_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_DEFAULT_ARCHIVE_START = date(2025, 4, 30)
_HANSARD_BASE = "https://hansard.parliament.uk"


# ---------------------------------------------------------------------------
# Date formatting (mirrors _url_date in views.py)
# ---------------------------------------------------------------------------

def _url_date(d: date) -> str:
    return f"{d.day}-{_MONTH_NAMES[d.month].lower()}-{d.year}"


# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

def _norm_bill_title(title: str, short_title: str | None) -> str:
    t = short_title or title
    t = re.sub(r"\s+Act\s+\d{4}$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+Bill(\s+\[?HL\]?|\s+\(HL\))?$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\[\(]HL[\]\)]", "", t, flags=re.IGNORECASE)
    return t.lower().strip()


def _norm_session_title(title: str) -> str:
    t = re.sub(r"\s+Bill(\s+\[?HL\]?|\s+\(HL\))?$", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\[\(]HL[\]\)]", "", t, flags=re.IGNORECASE)
    return t.lower().strip()


# ---------------------------------------------------------------------------
# Fallback URL
# ---------------------------------------------------------------------------

def _hansard_fallback_url(stage_date: date, house: str) -> str:
    house_path = "Lords" if house.lower() == "lords" else "Commons"
    return f"{_HANSARD_BASE}/{house_path}/{stage_date.isoformat()}"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def attach_session_links(
    bill: "HaBill",
    status: BillStatus,
    archive_start_date: date = _DEFAULT_ARCHIVE_START,
) -> None:
    """
    Mutate StageState objects in status.stages to populate session link fields.

    Single batch query for all candidate sessions across the bill's stage dates.
    Safe to call on every detail page render.
    """
    debate_stages: list[StageState] = [
        s for s in status.stages
        if s.has_debate and s.stage_date is not None
    ]
    if not debate_stages:
        return

    dates_needed = {(s.stage_date, s.house) for s in debate_stages}
    min_date = min(d for d, _ in dates_needed)
    max_date = max(d for d, _ in dates_needed)

    # Batch-load all non-container sessions with a slug across the date range.
    # filter_by is_container=False so we skip structural header rows.
    sessions_raw: list[HansardSession] = (
        db.session.query(HansardSession)
        .filter(
            HansardSession.date >= min_date,
            HansardSession.date <= max_date,
            HansardSession.is_container == False,  # noqa: E712
            HansardSession.slug.isnot(None),
        )
        .all()
    )

    sessions_by_date_house: dict[tuple, list[HansardSession]] = defaultdict(list)
    for sess in sessions_raw:
        key = (sess.date, sess.house.lower())
        sessions_by_date_house[key].append(sess)

    norm_bill = _norm_bill_title(bill.title, bill.short_title)

    for stage in debate_stages:
        if stage.stage_date < archive_start_date:
            stage.hansard_fallback_url = _hansard_fallback_url(
                stage.stage_date, stage.house
            )
            continue

        key = (stage.stage_date, stage.house.lower())
        candidates = sessions_by_date_house.get(key, [])

        if not candidates:
            stage.hansard_fallback_url = _hansard_fallback_url(
                stage.stage_date, stage.house
            )
            continue

        best: HansardSession | None = None
        for sess in candidates:
            if norm_bill in _norm_session_title(sess.title):
                best = sess
                break

        if best is None and len(candidates) == 1:
            best = candidates[0]

        if best is not None:
            stage.local_session_slug = best.slug
            stage.local_session_date = best.date
            stage.local_session_url_date = _url_date(best.date)
        else:
            stage.hansard_fallback_url = _hansard_fallback_url(
                stage.stage_date, stage.house
            )
