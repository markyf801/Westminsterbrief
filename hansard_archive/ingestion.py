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

import logging
import re
import time
from datetime import date, timedelta
from typing import Optional

import requests

_log = logging.getLogger(__name__)

from extensions import db
from hansard_archive.slugs import make_slug
from hansard_archive.models import (
    DEBATE_TYPE_COMMITTEE_STAGE,
    DEBATE_TYPE_DEBATE,
    DEBATE_TYPE_MINISTERIAL_STATEMENT,
    DEBATE_TYPE_ORAL_QUESTIONS,
    DEBATE_TYPE_OTHER,
    DEBATE_TYPE_PETITION,
    DEBATE_TYPE_PMQS,
    DEBATE_TYPE_STATUTORY_INSTRUMENT,
    DEBATE_TYPE_WESTMINSTER_HALL,
    HansardContribution,
    HansardSession,
)

HANSARD_API_BASE = "https://hansard-api.parliament.uk"
_REQUEST_TIMEOUT = 30       # increased from 15 — Commons API is genuinely slow
_INTER_REQUEST_DELAY = 0.3  # seconds between API calls — be a good citizen


def _api_get(url: str, params: dict | None = None) -> requests.Response:
    """
    GET with one retry on transient errors (Timeout / ConnectionError).
    Non-2xx HTTP errors raise immediately — no retry.
    A single transient timeout that succeeds on retry is logged but not an error.
    Two consecutive failures re-raise the last exception so the caller can count it.
    """
    last_exc: requests.RequestException | None = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt == 0:
                _log.warning("[archive] transient error (attempt 1), retrying in 5s: %s", e)
                print(f"[archive] transient error, retrying in 5s: {e}", flush=True)
                time.sleep(5)
        except requests.HTTPError:
            raise
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------------------------

_NOT_PARTY_LABELS = frozenset({
    "Maiden Speech", "Valedictory Speech", "Urgent Question", "Maiden",
    "Your Party", "Restore Britain",
})


def _clean_html(html: str) -> str:
    """Strip HTML tags, preserving paragraph breaks as double newlines."""
    if not html:
        return ""
    # Normalize Windows line endings so paragraph-break detection is consistent
    text = html.replace("\r\n", "\n").replace("\r", "\n")
    # HTML paragraph/line breaks → newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Split on paragraph boundaries (double newline), clean each paragraph internally
    paragraphs = re.split(r"\n{2,}", text)
    clean_paras = []
    for para in paragraphs:
        lines = [re.sub(r" +", " ", ln).strip() for ln in para.split("\n")]
        clean = " ".join(ln for ln in lines if ln)
        if clean:
            clean_paras.append(clean)
    return "\n\n".join(clean_paras)


def _extract_party(member_name: str | None) -> str | None:
    """Extract party abbreviation from 'Name (Constituency) (Party)' format."""
    if not member_name:
        return None
    groups = re.findall(r'\(([^)]+)\)', member_name)
    if len(groups) >= 2:
        candidate = groups[-1].strip()
        if candidate not in _NOT_PARTY_LABELS:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Debate type classification
# ---------------------------------------------------------------------------

# is_container=True is used for two structurally different but functionally
# equivalent session patterns. Both are excluded from tagging and public pages.
#
#   1. CONTAINERS (duplicate-content): structural headers whose _flatten_items()
#      recursion captures contributions from all child sessions, producing
#      duplicate counts if shown alongside their children.
#      Detected via _CONTAINER_HRS_TAGS (hrs_tag match) or null-tag + known titles.
#
#   2. ANCHORS (zero-content): section-header sessions that announce a
#      parliamentary slot but carry no speech text. The actual debates within
#      the slot are ingested separately under their own titles — always 0
#      contributions. Detected via _ANCHOR_TITLES (title match).

# HRS tags for container pattern (duplicate-content):
_CONTAINER_HRS_TAGS = {
    "hs_6bdepartment",   # dept oral questions header — duplicates all child hs_8Question sessions
    "hs_3mainhdg",       # chain-head header — duplicates all WH or CC child sessions
    "hs_3oralanswers",   # full-day oral answers header — duplicates all hs_6bDepartment sessions
    "hs_6bpetitions",    # petitions section header — duplicates all hs_8Petition sessions
    "hs_venue",          # Lords opening ceremony (Prayers) — chain head, ceremonial content only
}

# Title patterns for anchor pattern (zero-content structural placeholders):
# Sessions whose entire content is structural/procedural with no attributed debate text.
# Matched against title.lower() == value (exact match after lowercasing).
_ANCHOR_TITLES = {
    "backbench business",        # announces backbench business slot; debates ingested separately
    "business without debate",   # SI bundle motions — formal "agreed" only, no debate
    "business before questions", # procedural slot announcement before Oral Questions
    "business of the house",     # business announcement (also appears as "Business of the House")
    "business of the house (today)",
    "opposition day",            # announces opposition day; individual debates ingested separately
    "delegated legislation",     # SI bundle section header
    "bill presented",            # first-reading procedural notice
    "bills presented",           # first-reading procedural notice (plural)
    "ways and means",            # Ways and Means resolution header
    "estimates day",             # Estimates Day header
}

# Procedural session titles — always 'other' regardless of venue/location.
# Prevents e.g. 'Arrangement of Business' in Grand Committee from inheriting
# committee_stage from the location check. Matched with startswith() so
# "Retirement of a Member: Lord X" is caught by "retirement of a member".
_PROCEDURAL_TITLE_STARTS = (
    "arrangement of business",
    "business of the house",
    "oaths and affirmations",
    "retirement of a member",
    "retirements of members",
    "lord speaker's statement",
    "standing orders",
    "clerk of the parliaments",
    "leave of absence",
    "deaths of members",
    "message from the king",
    "royal assent",
)

# Regex for made statutory instruments: "[Name] Regulations/Order(s)/Rules YYYY".
# Requires a 4-digit year to avoid false positives on "Standing Orders (Public Business)".
# Must fire before the "amendment" keyword check in the title fallback — many SIs
# have "(Amendment)" in their formal title.
_MADE_SI_RE = re.compile(r"\b(regulations|orders?|rules)\s+\d{4}\b", re.IGNORECASE)


def _classify_from_overview(title: str, location: str, hrs_tag: str) -> str:
    """
    Classify debate type using Overview fields, with hrs_tag as primary source of truth.

    Precedence: procedural override → location (venue) → hrs_tag (structured) → title (fallback only).
    Title matching is a last resort because blank or unusual titles produce
    silent misclassification (e.g. hs_3cOppositionDay → statutory_instrument).
    """
    loc = (location or "").lower()
    tag = (hrs_tag or "").lower()
    t = (title or "").lower()

    # --- Procedural title override — always 'other' regardless of venue ---
    if any(t.startswith(p) for p in _PROCEDURAL_TITLE_STARTS):
        return DEBATE_TYPE_OTHER

    # --- Location is authoritative for venue-specific types ---
    if "westminster hall" in loc:
        return DEBATE_TYPE_WESTMINSTER_HALL

    if "grand committee" in loc:
        return DEBATE_TYPE_COMMITTEE_STAGE

    if "public bill committee" in loc:
        return DEBATE_TYPE_COMMITTEE_STAGE

    if "general committee" in loc:
        return DEBATE_TYPE_STATUTORY_INSTRUMENT

    # --- HRS tag hierarchy (structured field, not title-derived) ---
    if tag in ("hs_8question", "hs_3oralanswers"):
        # PMQs: canonical title is "Engagements" (traditional first-question formula)
        if t == "engagements" or "prime minister" in t or "pmq" in t:
            return DEBATE_TYPE_PMQS
        return DEBATE_TYPE_ORAL_QUESTIONS

    if tag == "hs_2curgenquestion":
        return DEBATE_TYPE_ORAL_QUESTIONS

    if tag == "hs_2cstatement":
        # "Lord Mandelson: Response to Humble Address" is a debate on an address motion
        if "humble address" in t:
            return DEBATE_TYPE_DEBATE
        return DEBATE_TYPE_MINISTERIAL_STATEMENT

    if tag in ("hs_2cbilltitle", "hs_2billtitle", "hs_2debbill"):
        return DEBATE_TYPE_DEBATE

    if tag in ("hs_2cdebatedmotion", "hs_2debatedmotion"):
        return DEBATE_TYPE_DEBATE

    if tag == "hs_2businesswodebate":
        return DEBATE_TYPE_STATUTORY_INSTRUMENT

    if tag in ("hs_8petition", "hs_6bpetitions"):
        return DEBATE_TYPE_PETITION

    # SO24 emergency debate applications are substantive debates
    if tag == "hs_2cso24application":
        return DEBATE_TYPE_DEBATE

    # hs_3cOppositionDay and hs_3cMainHdg are structural/procedural headers
    if tag in ("hs_3coppositionday", "hs_3cmainhdg"):
        return DEBATE_TYPE_OTHER

    # --- Title fallback (used when hrs_tag is null or unrecognised) ---
    if "written ministerial statement" in t or ("statement" in t and "ministerial" in t):
        return DEBATE_TYPE_MINISTERIAL_STATEMENT

    if (
        "statutory instrument" in t
        or "delegated legislation" in t
        or ("draft" in t and ("regulation" in t or "order" in t))
    ):
        return DEBATE_TYPE_STATUTORY_INSTRUMENT

    # Made SIs: "[Name] Regulations/Order(s)/Rules YYYY" — placed before the
    # "amendment" check because many SIs have "(Amendment)" in their formal title.
    if _MADE_SI_RE.search(t):
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
# Contribution flattening
# ---------------------------------------------------------------------------

_SKIP_TITLES = {"deferred division", "deferred divisions"}

# ItemTypes that are never speech contributions.
_SKIP_ITEM_TYPES = {"Timestamp", "Amendment"}

# All ItemTypes observed in the wild — warn on anything new.
_KNOWN_ITEM_TYPES = {"Contribution", "Timestamp", "Amendment"}

# HRSTag values for structural/procedural items that are not speech contributions.
# All lowercased for comparison.
_SKIP_HRS_TAGS = {
    "hs_columnumber",       # column/page reference markers (usually empty text; guard)
    "hs_columnnumber",      # alternate capitalisation seen in API responses
    "hs_clheading",         # committee membership roster header
    "hs_clchairman",        # committee chair listing
    "hs_clmember",          # committee member listing
    "hs_clstaff",           # committee staff listing
    "hs_debatetype",        # section-type labels ("Commons Amendment", etc.)
    "hs_amendmentheading",  # amendment motion headers ("Motion A", etc.)
    "hs_tabledby",          # procedural "Moved by" / "Asked by" annotations
    "hs_procedure",         # procedural outcomes ("Motion agreed.", "House resumed.", etc.)
    "hs_76fchair",          # committee chair annotation "[Name in the Chair]"
    "hs_brev",              # bill/motion formal text blocks (not speech)
    "hs_clclerks",          # committee clerk listing
    "hs_amendmentlevel0",   # amendment formal text (root level)
    "hs_amendmentlevel1",   # amendment formal text (sub-clause)
    "hs_amendmentlevel2",   # amendment formal text (sub-sub-clause)
    "hs_amendmentlevel3",   # amendment formal text (deepest level)
    "err_tablewrapper",     # unattributed formal bill text blocks (same class as hs_brev)
}

# HRSTag values known to carry real speech text — used by the unknown-tag warning.
_KNOWN_SPEECH_HRS_TAGS = {
    "hs_para", "hs_2para",
}


def _flatten_items(node: dict, order_counter: list) -> list[dict]:
    """
    Recursively flatten Items from a Hansard session response.
    order_counter is a single-element list used as a mutable integer.

    Filters applied:
    - Skip ItemType in _SKIP_ITEM_TYPES (Timestamps, Amendment text)
    - Skip HRSTag in _SKIP_HRS_TAGS (structural/procedural non-speech items)
    - Skip items that produce empty text after HTML cleaning

    Discovery hooks:
    - Warn on ItemType values outside _KNOWN_ITEM_TYPES
    - Warn on unattributed items with HRSTag outside both skip and speech sets
      (may signal new structural patterns not yet classified)
    """
    result = []
    for item in node.get("Items", []):
        item_type = item.get("ItemType")
        hrs_raw = item.get("HRSTag") or ""
        hrs = hrs_raw.lower()

        # Warn on unknown ItemType — may signal a new API pattern needing classification
        if item_type not in _KNOWN_ITEM_TYPES:
            _log.warning(
                "Unknown Hansard ItemType %r (HRSTag=%r) — check whether it needs filtering",
                item_type, hrs_raw,
            )

        # Skip non-speech item types
        if item_type in _SKIP_ITEM_TYPES:
            continue

        # Warn on unattributed items with HRS tags outside our known sets
        # (neither in the skip set nor in the known-speech set)
        is_attributed = bool(
            item.get("MemberId") or item.get("memberId")
            or item.get("AttributedTo") or item.get("attributedTo")
            or item.get("MemberName") or item.get("memberName")
        )
        if not is_attributed and hrs and hrs not in _SKIP_HRS_TAGS and hrs not in _KNOWN_SPEECH_HRS_TAGS:
            _log.warning(
                "Unattributed item with unclassified HRSTag %r (ItemType=%r, text=%r) — "
                "check if it belongs in _SKIP_HRS_TAGS",
                hrs_raw, item_type, (item.get("Value") or "")[:60],
            )

        # Skip structural non-speech items
        if hrs in _SKIP_HRS_TAGS:
            continue

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
                "party": _extract_party(member_name),
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
    resp = _api_get(url, params=params)
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


def _fetch_session_full(ext_id: str) -> tuple[dict, list[dict], list[str]]:
    """
    Fetch a session's full JSON. Returns (overview, contributions, child_ext_ids).

    overview contains: Title, Date, Location, HRSTag, NextDebateExtId,
    PreviousDebateExtId (and other fields not used here).
    child_ext_ids: ExtIds extracted from top-level ChildDebates entries. Used by
    BFS to discover isolated sub-chains — the Lords Grand Committee wrapper has
    empty chain links but its ChildDebates contains all GC session ExtIds.
    Returns ({}, [], []) on network error.
    """
    url = f"{HANSARD_API_BASE}/debates/debate/{ext_id}.json"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        _log.warning("[archive] failed to fetch session %s: %s", ext_id, e)
        return {}, [], []

    data = resp.json()
    overview = data.get("Overview") or {}
    contributions = _flatten_items(data, [0])

    # Extract child ExtIds for BFS — handles Grand Committee wrapper pattern where
    # chain links are empty but ChildDebates lists all sub-chain session ExtIds.
    # The child's ExtId lives at ChildDebates[n]["Overview"]["ExtId"], not at the
    # top level. Top-level "Id" is an internal numeric DB id, not the API ExtId.
    child_ext_ids: list[str] = []
    for child in data.get("ChildDebates", []):
        child_overview = child.get("Overview") or {}
        child_id = (
            child_overview.get("ExtId")
            or child.get("ExternalId")
            or child.get("DebateSectionExtId")
            or ""
        )
        if child_id:
            child_ext_ids.append(str(child_id))

    return overview, contributions, child_ext_ids


def _collect_all_sessions_for_date(
    seeds: list[str],
    target_date: date,
) -> tuple[dict[str, tuple[dict, list[dict]]], dict[str, str]]:
    """
    BFS walk of all Hansard session chains for a given date.

    Commons Chamber and Westminster Hall debates are on separate linked lists.
    Lords Grand Committee sessions are on an isolated sub-chain: the Grand Committee
    wrapper session has empty chain links but its ChildDebates contains all GC ExtIds.
    Starting from seeds, this function follows NextDebateExtId / PreviousDebateExtId
    AND ChildDebates ExtIds to discover all sessions on target_date across all chains.

    Each session's full JSON is fetched exactly once (contributions + Overview
    in the same call). Traversal stops when a neighbour's date differs from
    target_date. Deferred Divisions are excluded from results but their chain
    links are followed so traversal continues past them.

    Returns:
      results  — dict: ext_id -> (overview, contributions)
      dept_map — dict: child_ext_id -> department_name, built from hs_6bDepartment
                 containers encountered during the walk. Used to set HansardSession.department
                 on child oral-questions sessions at write time.
    """
    target_date_str = target_date.isoformat()
    visited: set[str] = set()
    queue: list[str] = list(seeds)
    results: dict[str, tuple[dict, list[dict]]] = {}
    dept_map: dict[str, str] = {}  # child_ext_id -> dept name from hs_6bDepartment container

    while queue:
        ext_id = queue.pop(0)
        if ext_id in visited:
            continue
        visited.add(ext_id)

        time.sleep(_INTER_REQUEST_DELAY)
        overview, contributions, child_ext_ids = _fetch_session_full(ext_id)

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

        # When we encounter a department oral-questions container, record the mapping
        # from each child's ext_id to the department name (container title). This
        # populates HansardSession.department on the child sessions without a second pass.
        if (overview.get("HRSTag") or "").lower() == "hs_6bdepartment" and child_ext_ids:
            for child_id in child_ext_ids:
                dept_map[child_id] = title

        # Follow chain links to discover adjacent sessions in both directions
        for link_key in ("NextDebateExtId", "PreviousDebateExtId"):
            neighbour = overview.get(link_key)
            if neighbour and neighbour not in visited:
                queue.append(neighbour)

        # Queue ChildDebates ExtIds — discovers isolated sub-chains (e.g. Lords
        # Grand Committee wrapper whose chain links are empty but ChildDebates
        # lists all substantive GC sessions for the day).
        for child_id in child_ext_ids:
            if child_id not in visited:
                queue.append(child_id)

    return results, dept_map


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
        raise  # propagate to ingest_date_range() so it counts as an error

    if not seeds:
        if verbose:
            print(f"[archive] {sitting_date} — no sessions (non-sitting day or empty)", flush=True)
        return 0

    if verbose:
        print(f"[archive] {sitting_date} — {len(seeds)} seeds, walking chains...", flush=True)

    all_sessions, dept_map = _collect_all_sessions_for_date(seeds, sitting_date)

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

        # Lords oral questions: the structural classifier has no reliable HRS signal
        # for Lords OQs, so they land in 'other'. Override using the opening-phrase
        # signal: every Lords OQ starts "To ask His Majesty's Government..." —
        # this is constitutionally mandated phrasing, not a heuristic.
        if house == "Lords" and contributions:
            first_text = (contributions[0].get("speech_text") or "").strip().lower()
            if first_text.startswith("to ask his majesty"):
                debate_type = DEBATE_TYPE_ORAL_QUESTIONS

        hansard_url = _build_hansard_url(house, sitting_date, ext_id, title)

        # See module-level comments for the container vs anchor distinction.
        is_container = (
            (hrs_tag or "").lower() in _CONTAINER_HRS_TAGS                                        # duplicate-content containers + hs_venue
            or (not hrs_tag and title.lower() in {"commons chamber", "westminster hall",
                                                   "lords chamber", "grand committee"})            # null-tag containers
            or title.lower() in _ANCHOR_TITLES                                                     # zero-content anchors
        )

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
            is_container=is_container,
            slug=make_slug(title, ext_id),
            department=dept_map.get(ext_id) or None,
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
