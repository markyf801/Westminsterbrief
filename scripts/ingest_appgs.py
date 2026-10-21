"""
APPG ingestion: the Register of All-Party Parliamentary Groups (published by
the House of Commons, HTML edition) -> appg / appg_officer. The /appgs pages
read only from these tables.

Source: https://www.parliament.uk/mps-lords-and-offices/standards-and-financial-interests/
        parliamentary-commissioner-for-standards/registers-of-interests/
        register-of-all-party-party-parliamentary-groups/
A new edition is published roughly every six weeks at
https://publications.parliament.uk/pa/cm/cmallparty/<YYMMDD>/contents.htm with
one page per group. Parliamentary information licensed under the Open
Parliament Licence v3.0.

Usage:
    python scripts/ingest_appgs.py                    # weekly check: ingest only if a newer edition exists
    python scripts/ingest_appgs.py --force            # re-ingest the latest edition anyway
    python scripts/ingest_appgs.py --edition 261012   # a specific edition
    python scripts/ingest_appgs.py --limit 20         # first 20 groups (testing; no retirements)
    python scripts/ingest_appgs.py --dry-run          # fetch + parse + match, predict new/update, write nothing

How a run works:
1. Find the latest edition from the register's "Registers published in <year>" page.
2. Read its contents page (one link per group), then each group page (paced 0.5s).
3. Parse title, purpose, category, officers, registered contact (name only),
   secretariat, website, AGM date and registrable benefits.
4. Match officer names to current MPs/peers (Members API, ~75 calls) with a
   confidence level; unmatched names are kept as registered.
5. Upsert by the register's page name (stable across editions); replace each
   group's officers. Groups missing from a complete new edition get
   is_current=False (never deleted).

Personal contact details (phone numbers, email addresses, the public enquiry
point's name) are deliberately not stored: the page links to the official
register instead.

Exits 1 if most page fetches fail or the run hits a fatal error. Logs a row in
ha_cron_run (service_name appg-cron).

Railway (proposed): cron service `appg-cron`, schedule `30 2 * * 2` (Tuesdays
02:30 UTC; a no-op unless a new edition is out, otherwise ~10 min), start command
`python scripts/ingest_appgs.py`, SKIP_MIGRATIONS=1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
import traceback
import unicodedata
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from committees import topic_slugs_for  # noqa: E402
from committees_models import Appg, AppgOfficer  # noqa: E402
from extensions import db  # noqa: E402
from hansard_archive.models import HaCronRun  # noqa: E402

logger = logging.getLogger(__name__)

REGISTER_INDEX_URL = ("https://www.parliament.uk/mps-lords-and-offices/standards-and-financial-interests/"
                      "parliamentary-commissioner-for-standards/registers-of-interests/"
                      "register-of-all-party-party-parliamentary-groups/")
EDITION_BASE = "https://publications.parliament.uk/pa/cm/cmallparty/{edition}/"
MEMBERS_SEARCH_URL = "https://members-api.parliament.uk/api/Members/Search"
HEADERS = {"User-Agent": "WestminsterBrief/1.0 (+https://westminsterbrief.co.uk)"}
REQUEST_TIMEOUT = 30
PAGE_DELAY = 0.5
API_DELAY = 0.25
MAX_RETRIES = 3
_NOT_FOUND_MARKER = "Page cannot be found"


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Fetcher:
    """Paced GETs with retry/backoff; counts outcomes for the exit-code rule."""

    def __init__(self, sleep=time.sleep):
        self.ok = 0
        self.failed = 0
        self._sleep = sleep

    def _get(self, url: str, params: dict | None, delay: float):
        for attempt in range(1, MAX_RETRIES + 1):
            self._sleep(delay)
            try:
                resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504):
                    logger.warning("GET %s -> HTTP %d (not retried)", url, resp.status_code)
                    return None
                reason = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                self._sleep(2 ** attempt)
            else:
                print(f"  [failed] {url}: {reason}", flush=True)
        return None

    def html(self, url: str) -> str | None:
        """Page text, or None on failure / Parliament's 'Page cannot be found' (served as 200)."""
        resp = self._get(url, None, PAGE_DELAY)
        text = resp.content.decode("utf-8", errors="replace") if resp is not None else None
        if text is None or _NOT_FOUND_MARKER in text[:20000]:
            self.failed += 1
            return None
        self.ok += 1
        return text

    def json(self, url: str, params: dict) -> dict | None:
        resp = self._get(url, params, API_DELAY)
        try:
            body = resp.json() if resp is not None else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.failed += 1
            return None
        self.ok += 1
        return body


# ── Editions ──────────────────────────────────────────────────────────────────

_EDITION_RE = re.compile(r"cmallparty/(\d{6})/contents\.htm")
_YEAR_PAGE_RE = re.compile(r'href="([^"]*registers-published-in-(\d{4})/?)"')


def edition_date(code: str) -> date:
    return date(2000 + int(code[:2]), int(code[2:4]), int(code[4:6]))


def editions_in(html_text: str) -> list[str]:
    return sorted(set(_EDITION_RE.findall(html_text or "")), reverse=True)


def find_latest_edition(fetch: Fetcher) -> str | None:
    """Newest edition code (YYMMDD) linked from the register's year pages."""
    index = fetch.html(REGISTER_INDEX_URL)
    if not index:
        return None
    found = editions_in(index)
    years = sorted({(y, href) for href, y in _YEAR_PAGE_RE.findall(index)}, reverse=True)
    for _year, href in years[:2]:
        url = href if href.startswith("http") else "https://www.parliament.uk" + href
        found += editions_in(fetch.html(url) or "")
        if found:
            break
    return max(found) if found else None


# ── Parsing ───────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return " ".join((s or "").replace(" ", " ").split())


def parse_contents(html_text: str) -> list[tuple[str, str]]:
    """[(register_slug, short_name)] in register order, from an edition's contents page."""
    soup = BeautifulSoup(html_text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*\.htm", href) or href in ("contents.htm", "introduction.htm"):
            continue
        slug = href[:-4]
        name = _clean(a.get_text(" "))
        if slug in seen or not name:
            continue
        seen.add(slug)
        out.append((slug, name))
    return out


def _cells(tr) -> list[str]:
    return [_clean(td.get_text(" ")) for td in tr.find_all(["td", "th"], recursive=False)]


def _parse_ddmmyyyy(s: str) -> date | None:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _contact_sections(td) -> dict[str, list]:
    """{label: [paragraph Tag, ...]} for the Contact Details cell (labels are <strong>…:</strong>)."""
    sections: dict[str, list] = {}
    current = None
    for p in td.find_all("p"):
        strong = p.find("strong")
        text = _clean(p.get_text(" "))
        if strong and _clean(strong.get_text(" ")).endswith(":") and _clean(strong.get_text(" ")) == text:
            current = text.rstrip(":").strip().lower()
            sections[current] = []
        elif current and text:
            sections[current].append(p)
    return sections


def _first_url(paras) -> str | None:
    for p in paras:
        for a in p.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("http://", "https://")):
                return href
        m = re.search(r"https?://\S+", p.get_text(" "))
        if m:
            return m.group(0).rstrip(".,)")
    return None


def _without_contacts(text: str) -> str:
    """Drop email addresses and phone numbers (personal data we don't republish)."""
    text = re.sub(r"\bEmail:?\s*\S+@\S+", "", text, flags=re.I)
    text = re.sub(r"\S+@\S+", "", text)
    text = re.sub(r"\b(Tel|Telephone|Phone)\b:?\s*[\d\s()+\-]{6,}\.?", "", text, flags=re.I)
    return _clean(text).strip(" .,;") or ""


def parse_group(html_text: str) -> dict | None:
    """
    One register page -> {title, purpose, category, officers[{role, name, party}],
    registered_contact_name, secretariat, website, agm_date, benefits[...]}.
    None if the page has no Title/Purpose table (not a group page).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    root = soup.find(id="mainTextBlock") or soup
    out: dict = {"title": "", "purpose": "", "category": "", "officers": [], "registered_contact_name": None,
                 "secretariat": None, "website": None, "agm_date": None, "benefits": []}
    for table in root.find_all("table"):
        rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        if not rows:
            continue
        head = _cells(rows[0])
        head0 = head[0].lower() if head else ""
        if head0 == "title" or (len(head) == 2 and head0 in ("purpose", "category")):
            for tr in rows:
                cells = _cells(tr)
                if len(cells) >= 2 and cells[0].lower() in ("title", "purpose", "category"):
                    out[cells[0].lower()] = cells[1]
        elif head0 == "officers":
            for tr in rows[1:]:
                cells = _cells(tr)
                if len(cells) < 2 or (cells[0].lower() == "role" and cells[1].lower() == "name"):
                    continue
                if not cells[1]:
                    continue
                out["officers"].append({"role": cells[0] or "Officer", "name": cells[1],
                                        "party": cells[2] if len(cells) > 2 else ""})
        elif head0 == "contact details" and len(rows) > 1:
            td = rows[1].find("td")
            sections = _contact_sections(td) if td else {}
            contact = sections.get("registered contact") or []
            if contact:
                out["registered_contact_name"] = _clean(contact[0].get_text(" ")).split(",")[0].strip() or None
            sec = sections.get("secretariat") or []
            if sec:
                out["secretariat"] = _without_contacts(" ".join(p.get_text(" ") for p in sec)) or None
            website = sections.get("group's website") or sections.get("groups website") or []
            out["website"] = _first_url(website)
        elif head0.startswith("inaugural and annual general meetings"):
            for tr in rows[1:]:
                cells = _cells(tr)
                if len(cells) >= 2 and cells[0].lower().startswith("date of igm"):
                    out["agm_date"] = _parse_ddmmyyyy(cells[1])
        elif head0 in ("benefits in kind", "financial benefits"):
            kind = "In kind" if head0 == "benefits in kind" else "Financial"
            for tr in rows[1:]:
                cells = _cells(tr)
                if len(cells) < 2 or cells[0].lower() == "source":
                    continue
                out["benefits"].append({
                    "kind": kind,
                    "source": cells[0],
                    "description": cells[1],
                    "value": cells[2] if len(cells) > 2 else "",
                    "received": cells[3] if len(cells) > 3 else "",
                    "registered": cells[4] if len(cells) > 4 else "",
                })
    if not out["title"] and not out["purpose"]:
        return None
    return out


def role_flags(role: str) -> tuple[bool, bool]:
    """(is_chair, is_registered_contact). Co-chairs count as chairs; vice-chairs don't."""
    r = role.lower()
    is_chair = bool(re.search(r"(^|[^-\w])(co-?\s?)?chair", r)) and "vice" not in r
    return is_chair, "registered contact" in r


# ── Member matching ───────────────────────────────────────────────────────────

_HONORIFICS = {"the", "rt", "hon", "right", "honourable", "dr", "sir", "dame", "mr", "mrs", "ms", "miss", "mx",
               "prof", "professor", "rev", "reverend", "revd", "cllr"}
_POSTNOMINALS = {"mp", "kc", "qc", "cbe", "obe", "mbe", "kbe", "dbe", "cb", "frs", "pc", "msp", "ms", "mla",
                 "frcp", "frcs", "frse", "ma", "phd", "bt"}
_PEER_TITLES = {"lord", "baroness", "lady", "viscount", "earl", "marquess", "duke", "countess", "bishop"}


def _norm_tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("’", "'").replace("'", "")
    return re.findall(r"[a-z0-9]+", s)


def name_keys(name: str) -> tuple[str, str, str]:
    """(full, stripped, peer_short) normalised keys for one name."""
    toks = _norm_tokens(name)
    full = " ".join(toks)
    core = [t for t in toks if t not in _HONORIFICS]
    while core and core[-1] in _POSTNOMINALS:
        core.pop()
    if core[:2] == ["lord", "bishop"]:
        core = core[1:]
    stripped = " ".join(core)
    peer_short = ""
    if core and core[0] in _PEER_TITLES and "of" in core[1:]:
        peer_short = " ".join(core[:core.index("of", 1)])
    return full, stripped, peer_short


def _party_key(party: str | None) -> str:
    toks = _norm_tokens(party or "")
    return toks[0] if toks else ""


class MemberIndex:
    """Current members by normalised name, for matching register names to MNIS ids."""

    def __init__(self):
        self.members: dict[int, dict] = {}
        self._maps: tuple[dict, dict, dict] = ({}, {}, {})

    def add(self, member_id: int, names: list[str], party: str | None, house: str | None) -> None:
        if not member_id:
            return
        self.members[member_id] = {"party": _party_key(party), "house": house, "name": names[0] if names else ""}
        for name in names:
            for level, key in enumerate(name_keys(name)):
                if key:
                    self._maps[level].setdefault(key, set()).add(member_id)

    def __len__(self) -> int:
        return len(self.members)

    def _pick(self, ids: set[int], party: str) -> int | None:
        if len(ids) == 1:
            return next(iter(ids))
        same = {i for i in ids if party and self.members[i]["party"] == party}
        return next(iter(same)) if len(same) == 1 else None

    def match(self, name: str, party: str | None = None) -> tuple[int | None, str]:
        """(member_id, confidence): high = exact name; medium = without titles/honours; low = fuzzy."""
        pk = _party_key(party)
        full, stripped, _ = name_keys(name)
        # Register "Baroness Smith" also tries the members' short peer form ("Baroness Smith of Malvern").
        for level, key in ((0, full), (1, stripped), (2, stripped)):
            ids = self._maps[level].get(key) if key else None
            if not ids:
                continue
            mid = self._pick(ids, pk)
            if mid is None:
                continue
            agrees = not pk or not self.members[mid]["party"] or self.members[mid]["party"] == pk
            if level == 0:
                return mid, "high" if agrees else "medium"
            return mid, "medium" if agrees else "low"
        return self._fuzzy(stripped, pk)

    def _fuzzy(self, key: str, pk: str) -> tuple[int | None, str]:
        if not key:
            return None, "none"
        try:
            from rapidfuzz import fuzz, process
        except ImportError:  # optional: rapidfuzz is in requirements.txt
            return None, "none"
        choices = list(self._maps[1].keys())
        best = process.extract(key, choices, scorer=fuzz.token_sort_ratio, limit=2)
        if not best or best[0][1] < 90 or (len(best) > 1 and best[1][1] == best[0][1]):
            return None, "none"
        mid = self._pick(self._maps[1][best[0][0]], pk)
        if mid is None or (pk and self.members[mid]["party"] and self.members[mid]["party"] != pk):
            return None, "none"
        return mid, "low"


def build_member_index(fetch: Fetcher) -> MemberIndex:
    """Every current MP and peer from the Members API (20 per page, ~75 calls)."""
    index = MemberIndex()
    for house in (1, 2):
        skip, total = 0, None
        while total is None or skip < total:
            body = fetch.json(MEMBERS_SEARCH_URL, {"House": house, "IsCurrentMember": "true",
                                                   "skip": skip, "take": 20})
            if body is None:
                break
            total = int(body.get("totalResults") or 0)
            items = body.get("items") or []
            for item in items:
                v = (item or {}).get("value") or {}
                names = [n for n in (v.get("nameDisplayAs"), v.get("nameAddressAs"), v.get("nameFullTitle")) if n]
                list_as = v.get("nameListAs") or ""
                if "," in list_as and house == 1:
                    last, first = list_as.split(",", 1)
                    names.append(f"{first.strip()} {last.strip()}")
                index.add(v.get("id"), names, (v.get("latestParty") or {}).get("name"),
                          "Commons" if house == 1 else "Lords")
            skip += 20
            if not items:
                break
    return index


def add_cached_members(index: MemberIndex) -> int:
    """Top up the index from cached_member (names seen by other features)."""
    try:
        from cache_models import CachedMember
        rows = CachedMember.query.all()
    except Exception:
        db.session.rollback()
        return 0
    added = 0
    for m in rows:
        if m.member_id and m.member_id not in index.members and m.name:
            index.add(m.member_id, [m.name], m.party, m.house)
            added += 1
    return added


# ── Upsert ────────────────────────────────────────────────────────────────────

def _content_hash(parsed: dict, officers: list[dict]) -> str:
    return hashlib.sha256(json.dumps([parsed, officers], sort_keys=True, default=str).encode()).hexdigest()


def upsert_appg(slug: str, short_name: str, parsed: dict, index: MemberIndex, edition: date,
                source_url: str, now: datetime) -> tuple[Appg, str, list[dict]]:
    """Write one group and replace its officers. Returns (appg, 'new'|'updated'|'unchanged', officers)."""
    officers = []
    for n, o in enumerate(parsed["officers"]):
        member_id, confidence = index.match(o["name"], o["party"])
        is_chair, is_contact = role_flags(o["role"])
        officers.append({**o, "member_id": member_id, "match_confidence": confidence,
                         "is_chair": is_chair, "is_registered_contact": is_contact, "sort_order": n})
    digest = _content_hash({**parsed, "short_name": short_name}, officers)

    a = Appg.query.filter_by(register_slug=slug).first()
    created = a is None
    if created:
        a = Appg(register_slug=slug, first_seen_edition=edition, ingested_at=now)
        db.session.add(a)
    a.register_edition = edition
    a.is_current = True
    a.source_url = source_url
    a.last_refreshed = now
    if not created and a.content_hash == digest:
        return a, "unchanged", officers

    a.title = parsed["title"] or f"All-Party Parliamentary Group for {short_name}"
    a.short_name = short_name
    a.slug = slug
    a.category = parsed["category"] or None
    a.purpose = parsed["purpose"] or None
    a.secretariat = parsed["secretariat"]
    a.website = parsed["website"]
    a.registered_contact_name = parsed["registered_contact_name"]
    a.agm_date = parsed["agm_date"]
    a.benefits = parsed["benefits"]
    a.topic_slugs = topic_slugs_for(short_name, parsed["purpose"] or "", threshold=2)
    a.content_hash = digest
    a.updated_at = now
    db.session.flush()

    AppgOfficer.query.filter(AppgOfficer.appg_id == a.id).delete(synchronize_session=False)
    for o in officers:
        db.session.add(AppgOfficer(appg_id=a.id, role=o["role"], is_chair=o["is_chair"],
                                   is_registered_contact=o["is_registered_contact"], name=o["name"],
                                   party=o["party"] or None, member_id=o["member_id"],
                                   match_confidence=o["match_confidence"], sort_order=o["sort_order"]))
    return a, "new" if created else "updated", officers


# ── Main loop ─────────────────────────────────────────────────────────────────

def ingest(edition: str | None = None, force: bool = False, limit: int | None = None, dry_run: bool = False,
           fetch: Fetcher | None = None, index: MemberIndex | None = None, now: datetime | None = None) -> dict:
    """Run one ingest. Expects an app context. Returns a summary dict."""
    fetch = fetch or Fetcher()
    now = now or datetime.utcnow()
    t0 = time.time()
    s = {"edition": edition, "skipped": False, "groups_listed": 0, "parsed": 0, "parse_failed": 0,
         "new": 0, "updated": 0, "unchanged": 0, "retired": 0, "officers": 0,
         "matched_high": 0, "matched_medium": 0, "matched_low": 0, "unmatched": 0,
         "unmatched_names": [], "dry_run": dry_run}

    edition = edition or find_latest_edition(fetch)
    s["edition"] = edition
    if not edition:
        print("[appg-ingest] Could not find the latest register edition", flush=True)
        s.update(requests_ok=fetch.ok, requests_failed=max(fetch.failed, 1), elapsed_s=round(time.time() - t0, 1))
        return s
    ed_date = edition_date(edition)
    stored = db.session.query(db.func.max(Appg.register_edition)).scalar()
    print(f"[appg-ingest] latest edition {edition} ({ed_date}); stored {stored or 'none'}"
          f"{' | DRY RUN' if dry_run else ''}", flush=True)
    if stored and stored >= ed_date and not force:
        print("[appg-ingest] No new edition; nothing to do (use --force to re-ingest)", flush=True)
        s["skipped"] = True
        s.update(requests_ok=fetch.ok, requests_failed=fetch.failed, elapsed_s=round(time.time() - t0, 1))
        return s

    base = EDITION_BASE.format(edition=edition)
    contents = fetch.html(base + "contents.htm")
    groups = parse_contents(contents) if contents else []
    s["groups_listed"] = len(groups)
    if limit is not None:
        groups = groups[:limit]

    if index is None:
        index = build_member_index(fetch)
        add_cached_members(index)
    print(f"[appg-ingest] {s['groups_listed']} groups listed; member index {len(index)} members", flush=True)

    seen: set[str] = set()
    for n, (slug, short_name) in enumerate(groups, 1):
        page = fetch.html(base + f"{slug}.htm")
        parsed = parse_group(page) if page else None
        if parsed is None:
            s["parse_failed"] += 1
            continue
        s["parsed"] += 1
        seen.add(slug)
        try:
            if dry_run:
                officers = [{**o, **dict(zip(("member_id", "match_confidence"), index.match(o["name"], o["party"])))}
                            for o in parsed["officers"]]
                exists = Appg.query.filter_by(register_slug=slug).first() is not None
                s["updated" if exists else "new"] += 1
            else:
                _, outcome, officers = upsert_appg(slug, short_name, parsed, index, ed_date,
                                                   base + f"{slug}.htm", now)
                db.session.commit()
                s[outcome] += 1
        except Exception as exc:
            db.session.rollback()
            s["parse_failed"] += 1
            logger.error("APPG %s failed: %s", slug, exc, exc_info=True)
            continue
        for o in officers:
            s["officers"] += 1
            conf = o["match_confidence"]
            s[f"matched_{conf}" if conf != "none" else "unmatched"] += 1
            if conf == "none" and len(s["unmatched_names"]) < 40:
                s["unmatched_names"].append(f"{o['name']} ({o['party']})")
        if n % 100 == 0:
            print(f"[appg-ingest] {n}/{len(groups)} groups", flush=True)

    complete = contents is not None and limit is None and s["parse_failed"] <= max(5, s["groups_listed"] // 20)
    if not dry_run and complete:
        for a in Appg.query.filter(Appg.is_current.is_(True)).all():
            if a.register_slug not in seen:
                a.is_current = False
                a.updated_at = now
                s["retired"] += 1
        db.session.commit()
    s.update(requests_ok=fetch.ok, requests_failed=fetch.failed, elapsed_s=round(time.time() - t0, 1))
    return s


def _print_summary(s: dict) -> None:
    print("\n[appg-ingest] ========== Run complete ==========", flush=True)
    for key, value in s.items():
        if key != "unmatched_names":
            print(f"  {key:<16} {value}", flush=True)
    if s.get("officers"):
        linked = s["matched_high"] + s["matched_medium"]
        print(f"  match rate       {linked / s['officers']:.1%} linked (high+medium), "
              f"{s['matched_low']} low, {s['unmatched']} unmatched", flush=True)
    for name in s.get("unmatched_names") or []:
        print(f"    unmatched: {name}", flush=True)


def mostly_failed(s: dict) -> bool:
    if s.get("skipped"):
        return False
    return s["requests_failed"] > s["requests_ok"] or s["groups_listed"] == 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest the Register of All-Party Parliamentary Groups")
    parser.add_argument("--edition", help="Edition code YYMMDD (default: latest)")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if the edition is already stored")
    parser.add_argument("--limit", type=int, help="First N groups only (no retirements)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch, parse and match; write nothing")
    parser.add_argument("--service-name", default="appg-cron", help="ha_cron_run.service_name")
    args = parser.parse_args()
    if args.edition and not re.fullmatch(r"\d{6}", args.edition):
        parser.error("--edition must look like 261012")

    from flask_app import app
    exit_code = 0
    with app.app_context():
        print(f"[appg-ingest] DB: {db.engine.url.render_as_string(hide_password=True)}", flush=True)
        run = None
        if not args.dry_run:
            run = HaCronRun(service_name=args.service_name, started_at=datetime.utcnow(),
                            days_window=0, status="running")
            db.session.add(run)
            db.session.commit()
        try:
            summary = ingest(edition=args.edition, force=args.force or bool(args.edition),
                             limit=args.limit, dry_run=args.dry_run)
            _print_summary(summary)
            exit_code = 1 if mostly_failed(summary) else 0
            if run:
                run.sessions_ingested = summary["new"] + summary["updated"]
                run.errors = summary["parse_failed"] + summary["requests_failed"]
                run.notes = (f"edition={summary['edition']} skipped={summary['skipped']} "
                             f"groups={summary['groups_listed']} new={summary['new']} "
                             f"updated={summary['updated']} retired={summary['retired']} "
                             f"officers={summary['officers']} unmatched={summary['unmatched']}")
        except Exception as exc:
            db.session.rollback()
            print(f"[appg-ingest] FATAL {type(exc).__name__}: {exc}\n{traceback.format_exc()}", flush=True)
            exit_code = 1
            if run:
                run.notes = f"fatal: {type(exc).__name__}: {exc}"[:2000]
        if run:
            run.finished_at = datetime.utcnow()
            run.status = "failed" if exit_code else "ok"
            db.session.commit()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
