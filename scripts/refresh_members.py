"""
Refresh stored member profiles (member_profile) and, optionally, the
pre-generated AI biographies (member_biography).

Pages under /members, /mp_search and /biography read only from these tables,
so this script is the single place that talks to Parliament for member data.

Phases
  1. Summary  — Members API Search, all current MPs + Peers (~75 pages of 20),
                or /Members/{id} for each --member-id.
  2. Details  — /Biography (posts, committees), /Contact, /RegisteredInterests
                per member. A failed fetch keeps the previously stored details.
  3. Activity — counts from our own DB (ha_pq, ha_contribution), no API calls.
  4. Names    — personal names into member_name. Wikidata first, matched on
                P10428 ("parliament.uk member ID") in bulk SPARQL queries; Parliament's
                own fields as fallback (MPs: display/list name; peers: the /Synopsis
                sentence "Her name is …"). Never guessed; conflicts are recorded.
                Weekly: only members with no member_name row. --audit-names: all
                members, plus an audit report (log + member_name_audit row).
                Peer slugs move from the title fallback to firstname-lastname-of-place
                once a personal name is known (old slugs 301).
  5. Bios     — only with --regenerate-bios. Wikipedia article from the same
                Wikidata match (wiki_status found/not_found/ambiguous/error is
                recorded); Wikidata education/occupation statements and Parliament's
                house/constituency history are the fallback when there is no article.
                Hashes all prompt inputs and calls Gemini Flash-Lite only for
                members whose hash changed or who have no bio yet.

Usage
  python scripts/refresh_members.py                       # phases 1-4 (names for new members only)
  python scripts/refresh_members.py --dry-run             # predict insert/update/unchanged; no writes, no Gemini
  python scripts/refresh_members.py --limit 50            # 50 members (never-detailed members first)
  python scripts/refresh_members.py --member-id 4046 --member-id 269
  python scripts/refresh_members.py --audit-names         # re-check every member's name + audit report
  python scripts/refresh_members.py --regenerate-bios     # phases 1-5

Railway (Dockerfile.cron, SKIP_MIGRATIONS=1, needs migrations/members_profile_tables.py applied first)
  member-profile-refresh   0 3 * * 0   python scripts/refresh_members.py
  member-bio-refresh       0 4 2 * *   python scripts/refresh_members.py --audit-names --regenerate-bios

Exit code 1 if most members failed, the member listing could not be fetched,
or an API returned 401/403/429 (stop and check — do not loop).

Environment: DATABASE_URL, GEMINI_API_KEY (bios only).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from sqlalchemy import func  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

MEMBERS_API = "https://members-api.parliament.uk/api/Members"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "WestminsterBrief/1.0 (+https://westminsterbrief.co.uk; weekly member refresh)"

PAGE_SIZE = 20            # Members API hard-caps Search at 20 per page
TIMEOUT = 15
PARLIAMENT_DELAY = 0.25   # seconds between Parliament requests
WIKI_DELAY = 0.5          # seconds between Wikimedia requests
GEMINI_DELAY = 0.5
WIKIDATA_CHUNK = 50       # member IDs per SPARQL query
WIKIDATA_TIMEOUT = 60
WIKIDATA_PROPERTY = "P10428"  # "parliament.uk member ID" — verified 2026-10-20

SUMMARY_FIELDS = ("name", "name_list_as", "full_title", "party", "party_abbreviation",
                  "party_colour", "house", "membership_from", "membership_start",
                  "is_current", "status_description", "thumbnail_url")
DETAIL_FIELDS = ("current_role", "government_posts", "opposition_posts", "other_posts",
                 "committees", "contact", "interests", "representations", "house_memberships")
NAME_FIELDS = ("personal_name", "given_name", "family_name", "peerage_title", "full_title",
               "wikidata_id", "wikidata_name", "parliament_name", "source", "source_url",
               "confidence", "audit_notes")


class StopRun(RuntimeError):
    """Permanent API error (401/403/429): stop, report, exit non-zero."""


# ── HTTP ──────────────────────────────────────────────────────────────────────

_http = requests.Session()
_http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def _get_json(url: str, params: dict | None = None, delay: float = PARLIAMENT_DELAY,
              timeout: float = TIMEOUT):
    """GET JSON with one retry on timeout/5xx. Returns None on failure; raises StopRun on 401/403/429."""
    for attempt in (1, 2):
        time.sleep(delay)
        try:
            resp = _http.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            reason = exc.__class__.__name__
        else:
            if resp.status_code in (401, 403, 429):
                raise StopRun(f"HTTP {resp.status_code} from {url}")
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    reason = "invalid JSON"
            elif resp.status_code == 404:
                return None
            else:
                reason = f"HTTP {resp.status_code}"
        if attempt == 1:
            print(f"  [retry] {url}: {reason}", flush=True)
            time.sleep(2)
    print(f"  [fail]  {url}: {reason}", flush=True)
    return None


# ── Parsing ───────────────────────────────────────────────────────────────────

def _day(value) -> str | None:
    return value.split("T")[0] if value else None


def _to_date(value) -> date | None:
    try:
        return date.fromisoformat(_day(value)) if value else None
    except ValueError:
        return None


def parse_summary(v: dict) -> dict:
    membership = v.get("latestHouseMembership") or {}
    party = v.get("latestParty") or {}
    status = membership.get("membershipStatus") or {}
    colour = (party.get("backgroundColour") or "")[:6] or None
    return {
        "member_id": int(v["id"]),
        "name": (v.get("nameDisplayAs") or "Unknown").strip(),
        "name_list_as": v.get("nameListAs"),
        "full_title": v.get("nameFullTitle"),
        "party": party.get("name"),
        "party_abbreviation": party.get("abbreviation"),
        "party_colour": colour,
        "house": "Lords" if membership.get("house") == 2 else "Commons",
        "membership_from": membership.get("membershipFrom") or "",
        "membership_start": _to_date(membership.get("membershipStartDate")),
        "is_current": bool(status.get("statusIsActive")) and not membership.get("membershipEndDate"),
        "status_description": status.get("statusDescription"),
        "thumbnail_url": v.get("thumbnailUrl"),
    }


def _posts(items) -> list[dict]:
    out = [{"name": (p.get("name") or "").strip(), "start": _day(p.get("startDate")),
            "end": _day(p.get("endDate")), "info": p.get("additionalInfo")}
           for p in (items or []) if p.get("name")]
    return sorted(out, key=lambda p: p["start"] or "", reverse=True)


def parse_details(bio: dict, contact: list, interests: list) -> dict:
    govt = _posts(bio.get("governmentPosts"))
    opp = _posts(bio.get("oppositionPosts"))
    current = [p["name"] for p in govt + opp if not p["end"]]
    contacts = []
    for c in contact or []:
        if c.get("isWebAddress"):   # website / social link: the URL is in line1
            address, website = "", c.get("website") or c.get("line1")
        else:
            address = ", ".join(x for x in (c.get(f"line{i}") for i in range(1, 6)) if x)
            if c.get("postcode"):
                address = f"{address}, {c['postcode']}" if address else c["postcode"]
            website = c.get("website")
        contacts.append({"type": c.get("type") or "", "address": address,
                         "phone": c.get("phone"), "email": c.get("email"), "website": website})
    interest_rows = []
    for cat in interests or []:
        for item in cat.get("interests") or []:
            desc = (item.get("interest") or "").strip()
            if desc and not item.get("deletedWhen"):
                interest_rows.append({"category": cat.get("name") or "", "description": desc[:300]})
    return {
        "current_role": "; ".join(current)[:500] or None,
        "government_posts": govt,
        "opposition_posts": opp,
        "other_posts": _posts(bio.get("otherPosts")),
        "committees": _posts(bio.get("committeeMemberships")),
        "contact": contacts,
        "interests": interest_rows,
        "representations": _posts(bio.get("representations")),
        "house_memberships": _posts(bio.get("houseMemberships")),
    }


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_current_members() -> tuple[list[dict], bool]:
    """All current members via Search. Returns (summaries, complete)."""
    out, skip, total = [], 0, None
    while True:
        data = _get_json(f"{MEMBERS_API}/Search",
                         {"IsCurrentMember": "true", "take": PAGE_SIZE, "skip": skip})
        if data is None:
            return out, False
        total = data.get("totalResults", 0) if total is None else total
        items = data.get("items") or []
        if not items:
            break
        out.extend(parse_summary(i["value"]) for i in items if (i.get("value") or {}).get("id"))
        skip += len(items)
        if skip >= total:
            break
    return out, len(out) >= total


def fetch_member(member_id: int) -> dict | None:
    data = _get_json(f"{MEMBERS_API}/{int(member_id)}")
    value = (data or {}).get("value")
    return parse_summary(value) if value and value.get("id") else None


def fetch_details(member_id: int) -> dict | None:
    """Posts, committees, contact and interests. None if any call failed (keep stored data)."""
    bio = _get_json(f"{MEMBERS_API}/{member_id}/Biography")
    contact = _get_json(f"{MEMBERS_API}/{member_id}/Contact")
    interests = _get_json(f"{MEMBERS_API}/{member_id}/RegisteredInterests")
    if bio is None or contact is None or interests is None:
        return None
    return parse_details(bio.get("value") or {}, contact.get("value") or [], interests.get("value") or [])


def fetch_synopsis(member_id: int) -> str | None:
    """Parliament's one-paragraph synopsis (for peers it states the personal name). None on failure."""
    data = _get_json(f"{MEMBERS_API}/{int(member_id)}/Synopsis")
    return (data or {}).get("value") if data is not None else None


_WIKIDATA_QUERY = """SELECT ?mnis ?item ?label ?article
 (GROUP_CONCAT(DISTINCT ?given; separator="|") AS ?givens)
 (GROUP_CONCAT(DISTINCT ?family; separator="|") AS ?families)
 (GROUP_CONCAT(DISTINCT ?edu; separator="|") AS ?education)
 (GROUP_CONCAT(DISTINCT ?occ; separator="|") AS ?occupations)
WHERE {
 VALUES ?mnis { %s }
 ?item wdt:%s ?mnis .
 OPTIONAL { ?item rdfs:label ?label FILTER(lang(?label) = "en") }
 OPTIONAL { ?item wdt:P735 ?g . ?g rdfs:label ?given FILTER(lang(?given) = "en") }
 OPTIONAL { ?item wdt:P734 ?f . ?f rdfs:label ?family FILTER(lang(?family) = "en") }
 OPTIONAL { ?item wdt:P69 ?e . ?e rdfs:label ?edu FILTER(lang(?edu) = "en") }
 OPTIONAL { ?item wdt:P106 ?o . ?o rdfs:label ?occ FILTER(lang(?occ) = "en") }
 OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
} GROUP BY ?mnis ?item ?label ?article"""


def _split(value: str) -> list[str]:
    return sorted({v for v in (value or "").split("|") if v})


def parse_wikidata(bindings: list, member_ids: list[int]) -> dict[int, dict]:
    """SPARQL bindings → {member_id: entry}. One item = 'matched'; several = 'ambiguous'; none = 'not_found'."""
    rows: dict[int, list] = {mid: [] for mid in member_ids}
    for b in bindings:
        mid = int(b["mnis"]["value"])
        rows.setdefault(mid, []).append({k: v["value"] for k, v in b.items()})
    out = {}
    for mid, items in rows.items():
        qids = {r["item"].rsplit("/", 1)[-1] for r in items}
        if not items:
            out[mid] = {"status": "not_found"}
            continue
        if len(qids) > 1:
            out[mid] = {"status": "ambiguous", "qids": sorted(qids)}
            continue
        r = items[0]
        label = (r.get("label") or "").strip()
        # Labels for peers are often "Brenda Hale, Baroness Hale of Richmond": the personal name is before the comma.
        name = label.split(",")[0].strip() or None
        givens, families = _split(r.get("givens")), _split(r.get("families"))
        family = families[0] if len(families) == 1 and name and name.endswith(families[0]) else None
        given = givens[0] if len(givens) == 1 and name and name.startswith(givens[0]) else None
        out[mid] = {
            "status": "matched", "qid": r["item"].rsplit("/", 1)[-1], "name": name,
            "given": given, "family": family, "article": r.get("article"),
            "education": _split(r.get("education")), "occupations": _split(r.get("occupations")),
        }
    return out


def fetch_wikidata(member_ids: list[int]) -> dict[int, dict]:
    """Bulk Wikidata lookup on P10428. A failed chunk marks its members 'error' (never 'not_found')."""
    out: dict[int, dict] = {}
    for start in range(0, len(member_ids), WIKIDATA_CHUNK):
        chunk = member_ids[start:start + WIKIDATA_CHUNK]
        query = _WIKIDATA_QUERY % (" ".join(f'"{int(m)}"' for m in chunk), WIKIDATA_PROPERTY)
        data = _get_json(WIKIDATA_SPARQL, {"query": query, "format": "json"},
                         delay=WIKI_DELAY, timeout=WIKIDATA_TIMEOUT)
        if data is None:
            out.update({m: {"status": "error"} for m in chunk})
            continue
        out.update(parse_wikidata((data.get("results") or {}).get("bindings") or [], chunk))
    return out


def fetch_wikipedia_extract(article_url: str) -> tuple[str, bool]:
    """(plain-text extract, ok) for an English Wikipedia article URL."""
    title = unquote(article_url.rsplit("/wiki/", 1)[-1])
    page = _get_json(WIKIPEDIA_API, {"action": "query", "prop": "extracts", "explaintext": 1,
                                     "redirects": 1, "format": "json", "titles": title},
                     delay=WIKI_DELAY)
    if page is None:
        return "", False
    pages = (page.get("query") or {}).get("pages") or {}
    return next((p.get("extract") or "" for p in pages.values()), "")[:4000], True


def wiki_status_for(entry: dict | None) -> str:
    status = (entry or {}).get("status", "error")
    if status == "matched":
        return "found" if entry.get("article") else "not_found"
    return status  # not_found | ambiguous | error


# ── DB writes ─────────────────────────────────────────────────────────────────

def upsert_profile(summary: dict, details: dict | None, now: datetime, dry_run: bool) -> str:
    """Insert or update one member_profile row. Returns 'inserted' | 'updated' | 'unchanged'."""
    from cache_models import MemberName, MemberProfile
    from extensions import db
    from member_profiles import slug_for

    row = db.session.get(MemberProfile, summary["member_id"])
    values = {f: summary[f] for f in SUMMARY_FIELDS}
    if details is not None:
        values.update({f: details[f] for f in DETAIL_FIELDS})

    if row is None:
        action = "inserted"
        if not dry_run:
            name_row = db.session.get(MemberName, summary["member_id"])
            slug, slug_source = slug_for(summary["name"], summary["house"], summary["membership_from"],
                                         name_row.personal_name if name_row else None)
            row = MemberProfile(member_id=summary["member_id"], slug=slug, slug_source=slug_source,
                                created_at=now)
            db.session.add(row)
    else:
        action = "updated" if any(getattr(row, f) != v for f, v in values.items()) else "unchanged"

    if dry_run:
        return action
    for f, v in values.items():
        setattr(row, f, v)
    row.summary_refreshed_at = now
    if details is not None:
        row.details_refreshed_at = now
    if action != "unchanged":
        row.updated_at = now   # content changed: drives sitemap lastmod
    db.session.commit()
    return action


def mark_departed(current_ids: set[int], now: datetime, dry_run: bool) -> int:
    """Members stored as current but missing from a COMPLETE current listing have left."""
    from cache_models import MemberProfile
    from extensions import db

    rows = MemberProfile.query.filter(MemberProfile.is_current.is_(True)).all()
    gone = [r for r in rows if r.member_id not in current_ids]
    if rows and len(gone) * 4 > len(rows):
        # A quarter of the House vanishing is more likely an API glitch than an election — check by hand.
        print(f"[members] {len(gone)} of {len(rows)} stored members missing from the listing; "
              "not marking anyone as departed.", flush=True)
        return 0
    if not dry_run:
        for r in gone:
            r.is_current = False
            r.updated_at = now
        db.session.commit()
    return len(gone)


def store_wiki_status(profile, entry: dict | None, status: str, now: datetime) -> None:
    """Record the Wikidata/Wikipedia lookup outcome on the profile (caller commits)."""
    profile.wiki_status = status
    profile.wiki_checked_at = now
    if entry and entry.get("status") == "matched":
        profile.wikidata_id = entry["qid"]
        profile.wikidata_facts = {"education": entry.get("education") or [],
                                  "occupations": entry.get("occupations") or []}
        profile.wikipedia_url = entry.get("article")
    elif status in ("not_found", "ambiguous"):
        profile.wikidata_id = None
        profile.wikidata_facts = None
        profile.wikipedia_url = None


def apply_slug(profile, personal_name: str | None) -> bool:
    """Stable-first: only upgrade a title-fallback slug to a personal-name slug. True if changed."""
    from member_profiles import slug_for
    if profile.slug_source == "personal" or not personal_name:
        return False
    slug, source = slug_for(profile.name, profile.house, profile.membership_from, personal_name)
    if source != "personal" or slug == profile.slug:
        return False
    profile.slug, profile.slug_source = slug, source   # the old slug 301s: the route keys on the id
    return True


def refresh_names(member_ids: list[int], wikidata: dict[int, dict], now: datetime,
                  dry_run: bool, audit: bool) -> dict:
    """Resolve and store personal names; returns audit counts + gaps."""
    from cache_models import MemberName, MemberNameAudit, MemberProfile
    from extensions import db
    from member_profiles import parliament_personal_name, resolve_name

    counts = {k: 0 for k in ("checked", "matched", "missing", "from_wikidata", "from_parliament",
                             "manual", "conflicts", "changed", "low_confidence", "slugs_changed", "lookup_errors",
                             "wikidata_not_found", "wiki_found", "wiki_not_found", "wiki_ambiguous",
                             "wiki_error")}
    gaps: list[dict] = []
    for mid in member_ids:
        profile = db.session.get(MemberProfile, mid)
        if profile is None:
            continue
        entry = wikidata.get(mid)
        counts["checked"] += 1
        status = wiki_status_for(entry)
        counts[f"wiki_{status}"] += 1
        if (entry or {}).get("status") == "not_found":
            counts["wikidata_not_found"] += 1
        if status in ("not_found", "ambiguous"):
            gaps.append({"member_id": mid, "name": profile.name, "issue": f"wikipedia_{status}", "detail": None})
        if not dry_run:
            store_wiki_status(profile, entry, status, now)

        existing = db.session.get(MemberName, mid)
        if existing is not None and existing.source == "manual":
            counts["manual"] += 1
            counts["matched"] += 1 if existing.personal_name else 0
            if not dry_run:
                existing.last_audited_at = now
                apply_slug(profile, existing.personal_name)
                db.session.commit()
            continue

        synopsis = fetch_synopsis(mid) if profile.house == "Lords" else None
        lookup_failed = ((entry or {}).get("status", "error") == "error"
                         or (profile.house == "Lords" and synopsis is None))
        if lookup_failed and existing is not None:
            # A failed lookup must not overwrite a good stored name — keep it and retry next run.
            counts["lookup_errors"] += 1
            counts["matched"] += 1 if existing.personal_name else 0
            continue
        summary = {"house": profile.house, "name": profile.name, "name_list_as": profile.name_list_as,
                   "full_title": profile.full_title}
        resolved = resolve_name(summary, entry, parliament_personal_name(summary, synopsis), mid)
        name = resolved["personal_name"]
        if name:
            counts["matched"] += 1
            counts["from_wikidata" if resolved["source"] == "wikidata" else "from_parliament"] += 1
        else:
            counts["missing"] += 1
            gaps.append({"member_id": mid, "name": profile.name, "issue": "name_missing", "detail": None})
        if resolved["confidence"] == "low":
            counts["low_confidence"] += 1
        if resolved["audit_notes"] and "conflict" in resolved["audit_notes"]:
            counts["conflicts"] += 1
            gaps.append({"member_id": mid, "name": profile.name, "issue": "conflict",
                         "detail": resolved["audit_notes"]})
        if existing is not None and existing.personal_name and name and existing.personal_name != name:
            counts["changed"] += 1
            gaps.append({"member_id": mid, "name": profile.name, "issue": "changed",
                         "detail": f"'{existing.personal_name}' -> '{name}'"})

        if dry_run:
            continue
        if existing is None:
            existing = MemberName(member_id=mid)
            db.session.add(existing)
        if any(getattr(existing, f) != resolved[f] for f in NAME_FIELDS):
            for f in NAME_FIELDS:
                setattr(existing, f, resolved[f])
            existing.updated_at = now
        existing.last_audited_at = now
        if apply_slug(profile, name):
            counts["slugs_changed"] += 1
        db.session.commit()

    if audit:
        print("[names] audit: " + " ".join(f"{k}={v}" for k, v in counts.items()), flush=True)
        for g in gaps[:25]:
            print(f"  [gap] {g['member_id']} {g['name']}: {g['issue']} {g['detail'] or ''}", flush=True)
        if len(gaps) > 25:
            print(f"  ... {len(gaps) - 25} more gaps in member_name_audit", flush=True)
        if not dry_run:
            db.session.add(MemberNameAudit(run_at=now, counts=counts, gaps=gaps))
            db.session.commit()
    return counts


def compute_activity(member_ids: list[int], today: date) -> dict[int, dict]:
    """Activity counts per member from ha_pq and ha_contribution (grouped queries, no API)."""
    from extensions import db
    from hansard_archive.models import (HansardContribution, HansardSession,
                                        HansardSessionTheme, HaPQ, THEME_TYPE_POLICY_AREA)

    since = today - timedelta(days=365)
    out = {mid: {"wq_12m": 0, "wq_total": 0, "sessions_12m": 0, "contributions_12m": 0,
                 "last_spoke_on": None, "top_policy_areas": [], "wq_departments": []}
           for mid in member_ids}
    for start in range(0, len(member_ids), 500):
        ids = member_ids[start:start + 500]
        for mid, n in (db.session.query(HaPQ.asking_mnis_id, func.count(HaPQ.id))
                       .filter(HaPQ.asking_mnis_id.in_(ids)).group_by(HaPQ.asking_mnis_id)):
            out[mid]["wq_total"] = n
        for mid, n in (db.session.query(HaPQ.asking_mnis_id, func.count(HaPQ.id))
                       .filter(HaPQ.asking_mnis_id.in_(ids), HaPQ.tabled_date >= since)
                       .group_by(HaPQ.asking_mnis_id)):
            out[mid]["wq_12m"] = n
        for mid, dept, n in (db.session.query(HaPQ.asking_mnis_id, HaPQ.answering_body, func.count(HaPQ.id))
                             .filter(HaPQ.asking_mnis_id.in_(ids), HaPQ.tabled_date >= since,
                                     HaPQ.answering_body.isnot(None))
                             .group_by(HaPQ.asking_mnis_id, HaPQ.answering_body)):
            out[mid]["wq_departments"].append({"department": dept, "questions": n})
        for mid, n_contrib, n_sess, last in (
                db.session.query(HansardContribution.member_id,
                                 func.count(HansardContribution.id),
                                 func.count(func.distinct(HansardContribution.session_id)),
                                 func.max(HansardSession.date))
                .join(HansardSession, HansardSession.id == HansardContribution.session_id)
                .filter(HansardContribution.member_id.in_(ids), HansardSession.date >= since,
                        HansardSession.is_container.is_(False))
                .group_by(HansardContribution.member_id)):
            out[mid].update(contributions_12m=n_contrib, sessions_12m=n_sess, last_spoke_on=last)
        for mid, theme, n in (
                db.session.query(HansardContribution.member_id, HansardSessionTheme.theme,
                                 func.count(func.distinct(HansardSessionTheme.session_id)))
                .join(HansardSession, HansardSession.id == HansardContribution.session_id)
                .join(HansardSessionTheme, HansardSessionTheme.session_id == HansardSession.id)
                .filter(HansardContribution.member_id.in_(ids), HansardSession.date >= since,
                        HansardSessionTheme.theme_type == THEME_TYPE_POLICY_AREA)
                .group_by(HansardContribution.member_id, HansardSessionTheme.theme)):
            out[mid]["top_policy_areas"].append({"theme": theme, "sessions": n})
    for vals in out.values():
        vals["top_policy_areas"] = sorted(vals["top_policy_areas"],
                                          key=lambda t: (-t["sessions"], t["theme"]))[:8]
        vals["wq_departments"] = sorted(vals["wq_departments"],
                                        key=lambda d: (-d["questions"], d["department"]))[:8]
    return out


def store_activity(activity: dict[int, dict], now: datetime) -> int:
    from cache_models import MemberProfile
    from extensions import db

    written = 0
    for mid, vals in activity.items():
        row = db.session.get(MemberProfile, mid)
        if row is None:
            continue
        for k, v in vals.items():
            setattr(row, k, v)
        row.activity_refreshed_at = now
        written += 1
        if written % 200 == 0:
            db.session.commit()
    db.session.commit()
    return written


def refresh_bio(profile, entry: dict | None, api_key: str | None, now: datetime, dry_run: bool) -> str:
    """'generated' | 'unchanged' | 'would_generate' | 'skipped' | 'failed'. Raises StopRun on 401/403/429.

    entry is this member's Wikidata lookup (fetch_wikidata). With no Wikipedia article the bio
    falls back to Wikidata statements plus Parliament's own records.
    """
    from cache_models import MemberBiography, MemberName
    from extensions import db
    from member_profiles import (BIO_MODEL, PROMPT_VERSION, GeminiPermanentError, bio_inputs,
                                 bio_prompt, bio_source_hash, bio_sources, generate_bio_text)

    status = wiki_status_for(entry)
    if status == "error":
        return "skipped"   # a lookup error must not look like "no article" and trigger a worse bio
    wiki_text, wiki_url = "", None
    if status == "found":
        wiki_text, ok = fetch_wikipedia_extract(entry["article"])
        if not ok:
            if not dry_run:
                store_wiki_status(profile, entry, "error", now)
                db.session.commit()
            return "skipped"
        wiki_url = entry["article"]
    if not dry_run:
        store_wiki_status(profile, entry, status, now)

    name_row = db.session.get(MemberName, profile.member_id)
    inputs = bio_inputs(profile, wiki_text, wiki_url, name_row.personal_name if name_row else None)
    matched = (entry or {}).get("status") == "matched"
    inputs.update(wikidata_id=entry["qid"] if matched else None,
                  wikidata_education=sorted(entry.get("education") or [])[:8] if matched else [],
                  wikidata_occupations=sorted(entry.get("occupations") or [])[:8] if matched else [])
    source_hash = bio_source_hash(inputs)
    existing = db.session.get(MemberBiography, profile.member_id)
    if existing is not None and existing.bio_text and existing.source_hash == source_hash:
        if not dry_run:
            db.session.commit()   # keep the wiki_status / checked-at stamp
        return "unchanged"
    if dry_run:
        return "would_generate"

    if existing is None:
        existing = MemberBiography(member_id=profile.member_id)
        db.session.add(existing)
    existing.last_attempt_at = now
    try:
        time.sleep(GEMINI_DELAY)
        text = generate_bio_text(bio_prompt(inputs), api_key)
    except GeminiPermanentError as exc:
        existing.last_error = str(exc)[:500]
        db.session.commit()
        raise StopRun(str(exc)) from exc
    except Exception as exc:  # keep the previous bio; record why this attempt failed
        existing.last_error = f"{exc.__class__.__name__}: {exc}"[:500]
        db.session.commit()
        return "failed"
    existing.bio_text = text
    existing.model_used = BIO_MODEL
    existing.prompt_version = PROMPT_VERSION
    existing.source_hash = source_hash
    existing.sources = bio_sources(profile.member_id, inputs)
    existing.generated_at = now
    existing.last_error = None
    db.session.commit()
    return "generated"


# ── Orchestration ─────────────────────────────────────────────────────────────

def _with_reconnect(fn, *args):
    """Run one member's DB work; on a dropped connection, dispose the pool and retry once."""
    from extensions import db
    try:
        return fn(*args)
    except OperationalError:
        db.session.rollback()
        db.engine.dispose()
        return fn(*args)


def run(member_ids: list[int] | None = None, limit: int | None = None, dry_run: bool = False,
        regenerate_bios: bool = False, api_key: str | None = None, today: date | None = None,
        audit_names: bool = False) -> dict:
    """Run inside an app context. Returns summary counts (and 'exit_code')."""
    from cache_models import MemberName, MemberProfile
    from extensions import db

    now = datetime.utcnow()
    today = today or now.date()
    stats = {"listed": 0, "inserted": 0, "updated": 0, "unchanged": 0, "detail_failed": 0,
             "failed": 0, "departed": 0, "activity": 0, "bio_generated": 0, "bio_unchanged": 0,
             "bio_would_generate": 0, "bio_skipped": 0, "bio_failed": 0, "names": None, "stopped": None}

    try:
        if member_ids:
            summaries = [s for s in (fetch_member(m) for m in member_ids) if s]
            complete = False
        else:
            summaries, complete = fetch_current_members()
        stats["listed"] = len(summaries)
        if not member_ids and not complete:
            print("[members] WARNING: member listing incomplete — refreshing what was fetched.", flush=True)
        if not summaries:
            print("[members] No members fetched — Parliament API unavailable?", flush=True)
            stats["exit_code"] = 1
            return stats

        if complete and not limit:
            stats["departed"] = mark_departed({s["member_id"] for s in summaries}, now, dry_run)

        # Never-detailed members first so repeated --limit runs work through a backfill.
        stored = {r.member_id: r.details_refreshed_at for r in
                  db.session.query(MemberProfile.member_id, MemberProfile.details_refreshed_at)}
        summaries.sort(key=lambda s: (stored.get(s["member_id"]) is not None,
                                      stored.get(s["member_id"]) or datetime.min, s["member_id"]))
        if limit:
            summaries = summaries[:limit]

        processed: list[int] = []
        for n, summary in enumerate(summaries, 1):
            details = fetch_details(summary["member_id"])
            if details is None:
                stats["detail_failed"] += 1
            try:
                action = _with_reconnect(upsert_profile, summary, details, now, dry_run)
                stats[action] += 1
                processed.append(summary["member_id"])
            except Exception as exc:
                db.session.rollback()
                stats["failed"] += 1
                print(f"  [err] member {summary['member_id']}: {exc.__class__.__name__}: {exc}", flush=True)
            if n % 100 == 0:
                print(f"[members] {n}/{len(summaries)} processed", flush=True)

        if processed and not dry_run:
            stats["activity"] = _with_reconnect(store_activity, compute_activity(processed, today), now)

        # Names + Wikipedia status: new members every run, everyone with --audit-names.
        name_ids = processed if audit_names else [m for m in processed if db.session.get(MemberName, m) is None]
        wikidata_ids = sorted(set(name_ids) | (set(processed) if regenerate_bios else set()))
        wikidata = fetch_wikidata(wikidata_ids) if wikidata_ids else {}
        if name_ids:
            stats["names"] = _with_reconnect(refresh_names, name_ids, wikidata, now, dry_run, audit_names)

        if regenerate_bios:
            if not api_key and not dry_run:
                print("[members] GEMINI_API_KEY not set — cannot regenerate biographies.", flush=True)
                stats["exit_code"] = 1
                return stats
            for mid in processed:
                profile = db.session.get(MemberProfile, mid)
                if profile is None or profile.details_refreshed_at is None:
                    stats["bio_skipped"] += 1
                    continue
                result = _with_reconnect(refresh_bio, profile, wikidata.get(mid), api_key, now, dry_run)
                stats[f"bio_{result}"] += 1
    except StopRun as exc:
        db.session.rollback()
        stats["stopped"] = str(exc)
        print(f"[members] STOPPED: {exc} — check the key / wait before re-running.", flush=True)

    attempted = stats["inserted"] + stats["updated"] + stats["unchanged"] + stats["failed"]
    bio_attempted = stats["bio_generated"] + stats["bio_failed"]
    names = stats["names"] or {}
    mostly_failing = (
        (attempted and (stats["failed"] + stats["detail_failed"]) * 2 > attempted)
        or (bio_attempted and stats["bio_failed"] * 2 > bio_attempted)
        or (names.get("checked") and names.get("wiki_error", 0) * 2 > names["checked"])
    )
    stats["exit_code"] = 1 if (stats["stopped"] or mostly_failing) else 0
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refresh stored member profiles and AI biographies.")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and compare, write nothing, call no AI")
    parser.add_argument("--limit", type=int, help="process at most N members")
    parser.add_argument("--member-id", type=int, action="append", dest="member_ids",
                        help="only this member (repeatable)")
    parser.add_argument("--regenerate-bios", action="store_true",
                        help="regenerate biographies whose inputs changed")
    parser.add_argument("--audit-names", action="store_true",
                        help="re-check every member's personal name and write an audit report")
    args = parser.parse_args(argv)

    from flask_app import app
    with app.app_context():
        t0 = time.monotonic()
        stats = run(member_ids=args.member_ids, limit=args.limit, dry_run=args.dry_run,
                    regenerate_bios=args.regenerate_bios, audit_names=args.audit_names,
                    api_key=os.environ.get("GEMINI_API_KEY"))
    mode = "DRY RUN — nothing written. " if args.dry_run else ""
    print(f"[members] {mode}Done in {time.monotonic() - t0:.0f}s: "
          + " ".join(f"{k}={v}" for k, v in stats.items() if k not in ("exit_code", "names")), flush=True)
    if stats.get("names"):
        print("[members] names: " + " ".join(f"{k}={v}" for k, v in stats["names"].items()), flush=True)
    return stats["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
