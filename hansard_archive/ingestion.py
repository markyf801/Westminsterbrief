"""
Hansard Archive — ingestion logic.

Two entry points:
  ingest_date(sitting_date, house="Commons") — ingest one day, returns session count
  ingest_date_range(start, end, house="Commons") — iterate over a date range

Session discovery uses /search/debates.json without a search term, filtered to
one day. This endpoint returns up to 25 sessions (hard API cap — skip/take/orderBy
are silently ignored). On a busy sitting day the search may return sessions from
only ONE of the day's linked-list chains; the other chains are missed entirely.

Hansard organises sessions into SEPARATE LINKED LISTS by venue. For Commons:
  - Commons Chamber chain   (~22 sessions on a typical day)
  - Westminster Hall chain  (~6 sessions, entirely separate linked list)

Each session's Overview contains NextDebateExtId / PreviousDebateExtId linking
to its neighbours in the same chain. To ensure complete coverage, ingestion uses
BFS chain-walking starting from the seeds returned by the search endpoint:
  1. Fetch up to 25 seeds via /search/debates.json
  2. BFS: fetch each seed's full JSON (/debates/debate/{ext_id}.json), follow
     NextDebateExtId and PreviousDebateExtId to adjacent sessions
  3. Stop traversal when the session date changes from the target date
  4. Each session's full JSON is fetched exactly once (contributions + Overview)

Contributions are stored flat (responds_to_id NULL — Q&A pairing is Week 2 work).
Deferred Divisions (procedural voting records) are excluded from stored sessions
but their chain links are followed so traversal continues through them.

All functions are safe to re-run: sessions are skipped if ext_id already exists.
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
    Used as a fallback when Overview fields are not available.
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


def _classify_from_overview(title: str, location: str, hrs_tag: str) -> str:
    """
    Classify debate type using authoritative Overview fields.

    Location ("Westminster Hall") and HRSTag ("hs_8Question" etc.) are more
    reliable than free-text title parsing.
    """
    loc = (location or "").lower()
    tag = (hrs_tag or "").lower()
    t = (title or "").lower()

    if "westminster hall" in loc:
        return DEBATE_TYPE_WESTMINSTER_HALL

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

    return _classify_debate_type(title, location)


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
# Contribution flattening
# ---------------------------------------------------------------------------

_SKIP_TITLES = {"deferred division", "deferred divisions"}


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
                "party": None,  # not always in Items; can be resolved later
                "speech_text": speech_text,
                "speech_order": order_counter[0],
            }
        )
        order_counter[0] += 1

    for child in node.get("ChildDebates", []):
        result.extend(_flatten_items(child, order_counter))

    return result


# ---------------------------------------------------------------------------
# Session discovery — chain-walking
# ---------------------------------------------------------------------------

def _search_debates(
    sitting_date: date, house: str, search_term: Optional[str] = None
) -> list[str]:
    """
    Single call to /search/debates.json. Returns up to 25 ext_ids.
    Used by _get_seeds_for_date; not called directly.
    """
    url = f"{HANSARD_API_BASE}/search/debates.json"
    date_str = sitting_date.isoformat()
    params: dict = {
        "queryParameters.house": house,
        "queryParameters.startDate": date_str,
        "queryParameters.endDate": date_str,
    }
    if search_term:
        params["queryParameters.searchTerm"] = search_term
    resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return [
        item["DebateSectionExtId"]
        for item in (data.get("Results") or [])
        if item.get("DebateSectionExtId") and (item.get("Title") or "").strip()
    ]


def _get_seeds_for_date(sitting_date: date, house: str = "Commons") -> list[str]:
    """
    Fetch initial seed ext_ids for BFS chain-walking.

    Primary search returns up to 25 sessions but may cover only one chain.
    For Commons sittings, a secondary search for "Westminster Hall" ensures the
    WH chain is always seeded — every WH sitting day has a header session titled
    exactly "Westminster Hall" which is the start of that chain (prev=NONE).
    Without this, a day with 25+ Commons Chamber sessions would return zero WH
    seeds and the BFS would miss the Westminster Hall chain entirely.

    Returns [] for non-sitting days.
    Raises requests.RequestException on network failures.
    """
    seeds = _search_debates(sitting_date, house)
    if not seeds:
        return []

    # Anchor the Westminster Hall chain so BFS always reaches it even when
    # 25+ CC sessions crowd all WH sessions out of the primary results.
    if house == "Commons":
        time.sleep(_INTER_REQUEST_DELAY)
        wh_seeds = _search_debates(sitting_date, house, search_term="Westminster Hall")
        existing = set(seeds)
        seeds.extend(s for s in wh_seeds if s not in existing)

    return seeds


def _fetch_session_full(ext_id: str) -> tuple[dict, list[dict]]:
    """
    Fetch a session's full JSON. Returns (overview, contributions).

    overview contains: Title, Date, Location, HRSTag, NextDebateExtId,
    PreviousDebateExtId (and other fields not used here).
    Returns ({}, []) on network error.
    """
    url = f"{HANSARD_API_BASE}/debates/debate/{ext_id}.json"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return {}, []

    data = resp.json()
    overview = data.get("Overview") or {}
    contributions = _flatten_items(data, [0])
    return overview, contributions


def _collect_all_sessions_for_date(
    seeds: list[str],
    target_date: date,
) -> dict[str, tuple[dict, list[dict]]]:
    """
    BFS walk of all Hansard session chains for a given date.

    Commons Chamber and Westminster Hall debates are on separate linked lists.
    Starting from seeds (which may cover only one chain), this function follows
    NextDebateExtId / PreviousDebateExtId to discover all sessions on target_date
    across all chains.

    Each session's full JSON is fetched exactly once (contributions + Overview
    in the same call). Traversal stops when a neighbour's date differs from
    target_date. Deferred Divisions are excluded from results but their chain
    links are followed so traversal continues past them.

    Returns dict: ext_id -> (overview, contributions).
    """
    target_date_str = target_date.isoformat()
    visited: set[str] = set()
    queue: list[str] = list(seeds)
    results: dict[str, tuple[dict, list[dict]]] = {}

    while queue:
        ext_id = queue.pop(0)
        if ext_id in visited:
            continue
        visited.add(ext_id)

        time.sleep(_INTER_REQUEST_DELAY)
        overview, contributions = _fetch_session_full(ext_id)

        if not overview:
            continue

        # Stop traversal when we cross into a different day
        session_date_str = (overview.get("Date") or "")[:10]
        if session_date_str != target_date_str:
            continue

        # Store non-procedural sessions; still follow links through procedural ones
        title = (overview.get("Title") or "").strip()
        if title.lower() not in _SKIP_TITLES:
            results[ext_id] = (overview, contributions)

        # Follow chain links to discover adjacent sessions in both directions
        for link_key in ("NextDebateExtId", "PreviousDebateExtId"):
            neighbour = overview.get(link_key)
            if neighbour and neighbour not in visited:
                queue.append(neighbour)

    return results


# ---------------------------------------------------------------------------
# DB write helper
# ---------------------------------------------------------------------------

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

    Uses chain-walking to capture ALL sessions on the day, including those in
    separate venue chains (Westminster Hall) not reached by the search endpoint.

    Returns the number of NEW sessions ingested (0 for non-sitting days or if
    all sessions were already in the DB).

    Expects to run inside a Flask app context with an active DB session.
    """
    if verbose:
        print(f"[archive] {sitting_date} {house} — fetching seeds...", flush=True)

    try:
        seeds = _get_seeds_for_date(sitting_date, house)
    except requests.RequestException as e:
        print(f"[archive] ERROR fetching seeds for {sitting_date}: {e}", flush=True)
        return 0

    if not seeds:
        if verbose:
            print(f"[archive] {sitting_date} — no sessions (non-sitting day or empty)", flush=True)
        return 0

    if verbose:
        print(f"[archive] {sitting_date} — {len(seeds)} seeds, walking chains...", flush=True)

    all_sessions = _collect_all_sessions_for_date(seeds, sitting_date)

    if not all_sessions:
        if verbose:
            print(f"[archive] {sitting_date} — no sessions after chain walk", flush=True)
        return 0

    if verbose:
        print(f"[archive] {sitting_date} — {len(all_sessions)} sessions total", flush=True)

    new_sessions = 0

    for ext_id, (overview, contributions) in all_sessions.items():
        if HansardSession.query.filter_by(ext_id=ext_id).first():
            if verbose:
                print(f"[archive]   SKIP {ext_id[:20]}... (already ingested)", flush=True)
            continue

        title = (overview.get("Title") or "").strip()
        location = overview.get("Location") or ""
        hrs_tag = overview.get("HRSTag") or ""
        debate_type = _classify_from_overview(title, location, hrs_tag)
        hansard_url = _build_hansard_url(house, sitting_date, ext_id, title)

        session = HansardSession(
            ext_id=ext_id,
            title=title,
            date=sitting_date,
            house=house,
            debate_type=debate_type,
            location=location or None,
            hrs_tag=hrs_tag or None,
            hansard_url=hansard_url,
            contributions_ingested=False,
        )
        db.session.add(session)
        db.session.flush()

        contrib_count = _write_contributions(session, contributions)
        session.contributions_ingested = True

        try:
            db.session.commit()
            new_sessions += 1
            if verbose:
                print(
                    f"[archive]   + {title[:60]!r} — {contrib_count} contributions",
                    flush=True,
                )
        except Exception as e:
            db.session.rollback()
            print(f"[archive]   ERROR committing {ext_id}: {e}", flush=True)

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
