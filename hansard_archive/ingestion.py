"""
Hansard Archive — ingestion logic.

Two entry points:
  ingest_date(sitting_date, house="Commons") — ingest one day, returns session count
  ingest_date_range(start, end, house="Commons") — iterate over a date range

Session discovery uses /search/debates.json without a search term, filtered to
one day. This endpoint returns up to 25 sessions per call. The TotalResultCount
may exceed 25; the cap cannot be bypassed via pagination (skip is ignored by this
endpoint). In practice the 25 sessions returned cover all main chamber and most
Westminster Hall debates for a day. Deferred Divisions (procedural voting records
with no speech content) are filtered out.

Known limitation: on very busy sitting days, some Westminster Hall sessions near
the bottom of the relevance ranking may fall outside the 25-item cap. This is
acceptable for Phase 2A's browsable archive. Noted in docs/api-reference.md.

Contributions are fetched via:
  GET https://hansard-api.parliament.uk/debates/debate/{ext_id}.json
and stored flat (responds_to_id NULL in all rows — Q&A pairing is Week 2 work).

All functions are safe to re-run: sessions are skipped if ext_id already exists;
contributions are skipped if contributions_ingested is already True on the session.
"""

import re
import time
from datetime import date, timedelta
from typing import Optional

import requests

from extensions import db
from hansard_archive.models import (
    DEBATE_TYPE_DEBATE,
    DEBATE_TYPE_MINISTERIAL_STATEMENT,
    DEBATE_TYPE_ORAL_QUESTIONS,
    DEBATE_TYPE_OTHER,
    DEBATE_TYPE_PMQS,
    DEBATE_TYPE_STATUTORY_INSTRUMENT,
    DEBATE_TYPE_WESTMINSTER_HALL,
    HansardContribution,
    HansardSession,
)

HANSARD_API_BASE = "https://hansard-api.parliament.uk"
_REQUEST_TIMEOUT = 15
_INTER_REQUEST_DELAY = 0.3  # seconds between API calls — be a good citizen


# ---------------------------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------------------------

def _clean_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Debate type classification
# ---------------------------------------------------------------------------

def _classify_debate_type(title: str, section: Optional[str] = None) -> str:
    """
    Map a Hansard session title to a controlled debate type vocabulary.

    The section field from the overview API (e.g. 'Westminster Hall') is used
    as the first signal when available — it's more reliable than parsing the title.
    """
    t = (title or "").lower()
    s = (section or "").lower()

    if "westminster hall" in s or "westminster hall" in t:
        return DEBATE_TYPE_WESTMINSTER_HALL

    if "prime minister" in t and "question" in t:
        return DEBATE_TYPE_PMQS

    if "oral answers" in t or "question time" in t or (
        "questions" in t and "written" not in t and "urgent" not in t
    ):
        return DEBATE_TYPE_ORAL_QUESTIONS

    if "written ministerial statement" in t or (
        "statement" in t and "ministerial" in t
    ):
        return DEBATE_TYPE_MINISTERIAL_STATEMENT

    if (
        "statutory instrument" in t
        or "affirmative" in t
        or "delegated legislation" in t
        or ("draft" in t and ("regulation" in t or "order" in t))
    ):
        return DEBATE_TYPE_STATUTORY_INSTRUMENT

    if any(kw in t for kw in ("bill", "reading", "amendment", "committee stage")):
        return DEBATE_TYPE_DEBATE

    if "debate" in t or "motion" in t:
        return DEBATE_TYPE_DEBATE

    return DEBATE_TYPE_OTHER


# ---------------------------------------------------------------------------
# Hansard URL construction
# ---------------------------------------------------------------------------

def _build_hansard_url(house: str, sitting_date: date, ext_id: str, title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s]", "", title or "")
    slug = "".join(w.capitalize() for w in slug.split())
    return (
        f"https://hansard.parliament.uk/{house}/{sitting_date.isoformat()}"
        f"/debates/{ext_id}/{slug}"
    )


# ---------------------------------------------------------------------------
# Session discovery — overview endpoint
# ---------------------------------------------------------------------------

_SKIP_TITLES = {"deferred division", "deferred divisions"}


def _fetch_session_metadata_for_date(
    sitting_date: date, house: str = "Commons"
) -> list[dict]:
    """
    Fetch session metadata for a single sitting day.

    Uses /search/debates.json without a search term, filtered to one calendar day.
    Returns up to 25 sessions (hard API cap; pagination is ignored by this endpoint).
    Deferred Divisions are filtered out — they are procedural voting records with no
    speech content.

    Returns [] for non-sitting days.
    Raises requests.RequestException on network failures.
    """
    url = f"{HANSARD_API_BASE}/search/debates.json"
    date_str = sitting_date.isoformat()
    resp = requests.get(
        url,
        params={
            "queryParameters.house": house,
            "queryParameters.startDate": date_str,
            "queryParameters.endDate": date_str,
        },
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    data = resp.json()
    raw_results = data.get("Results") or []

    sessions = []
    for item in raw_results:
        ext_id = item.get("DebateSectionExtId") or ""
        title = (item.get("Title") or "").strip()
        section_hint = item.get("DebateSection") or ""

        if not ext_id or not title:
            continue

        # Filter procedural Deferred Divisions — no speech content
        if title.lower() in _SKIP_TITLES:
            continue

        debate_type = _classify_debate_type(title, section_hint)
        hansard_url = _build_hansard_url(house, sitting_date, ext_id, title)

        sessions.append(
            {
                "ext_id": ext_id,
                "title": title,
                "house": house,
                "debate_type": debate_type,
                "hansard_url": hansard_url,
            }
        )

    return sessions


# ---------------------------------------------------------------------------
# Contribution fetch
# ---------------------------------------------------------------------------

def _flatten_items(node: dict, order_counter: list) -> list[dict]:
    """
    Recursively flatten Items from a Hansard session response.
    order_counter is a single-element list used as a mutable integer.
    """
    result = []
    for item in node.get("Items", []):
        speech_html = item.get("Value") or item.get("value") or ""
        speech_text = _clean_html(speech_html)
        if not speech_text:
            continue

        member_id_raw = item.get("MemberId") or item.get("memberId")
        try:
            member_id = int(member_id_raw) if member_id_raw else None
        except (ValueError, TypeError):
            member_id = None

        member_name = (
            item.get("MemberName")
            or item.get("memberName")
            or item.get("AttributedTo")
            or item.get("attributedTo")
            or None
        )

        result.append(
            {
                "member_id": member_id,
                "member_name": member_name,
                "party": None,  # not always available in Items; can be resolved later
                "speech_text": speech_text,
                "speech_order": order_counter[0],
            }
        )
        order_counter[0] += 1

    for child in node.get("ChildDebates", []):
        result.extend(_flatten_items(child, order_counter))

    return result


def _classify_from_overview(title: str, location: str, hrs_tag: str) -> str:
    """
    Improve debate_type classification using Overview fields from the full session fetch.
    More accurate than title-only classification because Location and HRSTag are
    authoritative API signals rather than derived from free-text titles.
    """
    loc = (location or "").lower()
    tag = (hrs_tag or "").lower()
    t = (title or "").lower()

    if "westminster hall" in loc:
        return DEBATE_TYPE_WESTMINSTER_HALL

    # HRSTag signals from Hansard's own taxonomy
    if "question" in tag:
        if "prime minister" in t or "pmq" in t:
            return DEBATE_TYPE_PMQS
        return DEBATE_TYPE_ORAL_QUESTIONS

    if "billtitle" in tag or "billhd" in tag:
        return DEBATE_TYPE_DEBATE

    if "wms" in tag or "writtenstatement" in tag:
        return DEBATE_TYPE_MINISTERIAL_STATEMENT

    if "si" in tag or "statutoryinstrument" in tag:
        return DEBATE_TYPE_STATUTORY_INSTRUMENT

    # Fall back to title-based classification
    return _classify_debate_type(title, location)


def _fetch_contributions_for_session(ext_id: str) -> tuple[list[dict], dict]:
    """
    Fetch and flatten all contributions from a Hansard session.
    Also returns Overview metadata for classification improvement.

    Returns (contributions_list, overview_dict).
    On network error returns ([], {}).
    """
    url = f"{HANSARD_API_BASE}/debates/debate/{ext_id}.json"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return [], {}

    data = resp.json()
    overview = data.get("Overview") or {}
    order_counter = [0]
    contributions = _flatten_items(data, order_counter)
    return contributions, overview


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def _upsert_session(session_data: dict, sitting_date: date) -> Optional[HansardSession]:
    """
    Insert a session row if it doesn't exist. Returns the row.
    Returns None if the ext_id is already in the DB.
    """
    ext_id = session_data["ext_id"]
    existing = HansardSession.query.filter_by(ext_id=ext_id).first()
    if existing:
        return None  # Already ingested

    session = HansardSession(
        ext_id=ext_id,
        title=session_data["title"],
        date=sitting_date,
        house=session_data["house"],
        debate_type=session_data["debate_type"],
        hansard_url=session_data["hansard_url"],
        contributions_ingested=False,
    )
    db.session.add(session)
    db.session.flush()  # Get the id without committing
    return session


def _write_contributions(session: HansardSession, contributions: list[dict]) -> int:
    """Write contributions for a session. Returns count written."""
    count = 0
    for c in contributions:
        contrib = HansardContribution(
            session_id=session.id,
            member_id=c["member_id"],
            member_name=c["member_name"],
            party=c["party"],
            speech_text=c["speech_text"],
            speech_order=c["speech_order"],
            responds_to_id=None,  # Q&A pairing — Week 2
        )
        db.session.add(contrib)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def ingest_date(sitting_date: date, house: str = "Commons", verbose: bool = True) -> int:
    """
    Ingest all sessions for a single sitting day.

    Returns the number of NEW sessions ingested (0 for non-sitting days or
    if all sessions were already in the DB).

    This function expects to run inside a Flask app context with an active DB session.
    """
    if verbose:
        print(f"[archive] {sitting_date} {house} — fetching sessions...", flush=True)

    try:
        sessions_meta = _fetch_session_metadata_for_date(sitting_date, house)
    except requests.RequestException as e:
        print(f"[archive] ERROR fetching {sitting_date}: {e}", flush=True)
        return 0

    if not sessions_meta:
        if verbose:
            print(f"[archive] {sitting_date} — no sessions (non-sitting day or empty)", flush=True)
        return 0

    if verbose:
        print(f"[archive] {sitting_date} — {len(sessions_meta)} sessions found", flush=True)

    new_sessions = 0

    for meta in sessions_meta:
        session = _upsert_session(meta, sitting_date)
        if session is None:
            if verbose:
                print(f"[archive]   SKIP {meta['ext_id'][:20]}... (already ingested)", flush=True)
            continue

        # Fetch contributions + Overview metadata
        time.sleep(_INTER_REQUEST_DELAY)
        contributions, overview = _fetch_contributions_for_session(meta["ext_id"])

        # Improve debate_type using authoritative Overview fields
        location = overview.get("Location") or ""
        hrs_tag = overview.get("HRSTag") or ""
        session.location = location or None
        session.hrs_tag = hrs_tag or None
        session.debate_type = _classify_from_overview(meta["title"], location, hrs_tag)

        contrib_count = _write_contributions(session, contributions)
        session.contributions_ingested = True

        try:
            db.session.commit()
            new_sessions += 1
            if verbose:
                print(
                    f"[archive]   + {meta['title'][:60]!r} — {contrib_count} contributions",
                    flush=True,
                )
        except Exception as e:
            db.session.rollback()
            print(f"[archive]   ERROR committing {meta['ext_id']}: {e}", flush=True)

    return new_sessions


def ingest_date_range(
    start: date,
    end: date,
    house: str = "Commons",
    verbose: bool = True,
) -> dict:
    """
    Ingest all sessions for a date range (inclusive).

    Walks forwards from start to end, one day at a time.
    Non-sitting days produce 0 sessions and are skipped cleanly.

    Returns a summary dict: {total_days, sitting_days, total_sessions, errors}.
    """
    total_sessions = 0
    sitting_days = 0
    errors = 0
    current = start

    while current <= end:
        try:
            count = ingest_date(current, house=house, verbose=verbose)
            if count > 0:
                sitting_days += 1
                total_sessions += count
        except Exception as e:
            print(f"[archive] UNEXPECTED ERROR on {current}: {e}", flush=True)
            errors += 1

        current += timedelta(days=1)
        time.sleep(_INTER_REQUEST_DELAY)

    return {
        "total_days": (end - start).days + 1,
        "sitting_days": sitting_days,
        "total_sessions": total_sessions,
        "errors": errors,
    }
