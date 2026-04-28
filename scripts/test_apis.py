#!/usr/bin/env python3
"""Westminster Brief — API Smoke Tests

Run: python scripts/test_apis.py
Requires: TWFY_API_KEY in environment or .env file in project root.
All Parliament and GOV.UK APIs are unauthenticated.

Each test hits a real endpoint with a minimal query and validates
the response has the expected structure and non-empty data.
"""

import os
import sys
from datetime import datetime, timedelta

import requests

# Load .env from project root if dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

TWFY_API_KEY = os.environ.get("TWFY_API_KEY", "")
TWO_WEEKS_AGO = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
YYYYMMDD_TWO_WEEKS_AGO = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
TODAY_YYYYMMDD = datetime.now().strftime("%Y%m%d")

# Accumulated results: list of (name, passed, detail)
_results: list[tuple[str, bool, str]] = []

# Shared state across tests
_hansard_ext_id: str = ""


def _run(name: str, fn) -> None:
    try:
        passed, detail = fn()
        _results.append((name, passed, detail))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


# ── Hansard API ───────────────────────────────────────────────────────────────

def test_hansard_debates_search():
    global _hansard_ext_id
    resp = requests.get(
        "https://hansard-api.parliament.uk/search/debates.json",
        params={
            "queryParameters.house": "Commons",
            "queryParameters.searchTerm": "education",
            "queryParameters.startDate": TWO_WEEKS_AGO,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    results = data.get("Results", [])
    if results:
        _hansard_ext_id = results[0].get("DebateSectionExtId", "")
    total = data.get("TotalResultCount", "?")
    return bool(results), f"{len(results)} results (total={total}), ext_id={_hansard_ext_id!r}"


def test_hansard_speech_search():
    global _hansard_ext_id
    resp = requests.get(
        "https://hansard-api.parliament.uk/search.json",
        params={
            "queryParameters.house": "Commons",
            "queryParameters.searchTerm": "education",
            "queryParameters.startDate": TWO_WEEKS_AGO,
        },
        timeout=25,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    contribs = data.get("Contributions", [])
    if contribs and not _hansard_ext_id:
        _hansard_ext_id = contribs[0].get("DebateSectionExtId", "")
    total = data.get("TotalContributions", "?")
    return bool(contribs), f"{len(contribs)} contributions (total={total})"


def test_hansard_minister_search():
    # Josh MacAlister — DfE Parliamentary Under-Secretary, Parliament ID 5033
    resp = requests.get(
        "https://hansard-api.parliament.uk/search.json",
        params={
            "queryParameters.memberId": 5033,
            "queryParameters.house": "Commons",
            "queryParameters.searchTerm": "education",
            "queryParameters.startDate": "2024-07-01",
            "take": 10,
        },
        timeout=25,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    contribs = data.get("Contributions", [])
    names = list({c.get("MemberName", "") for c in contribs if c.get("MemberName")})
    return bool(contribs), f"{len(contribs)} contributions, speakers={names[:3]}"


def test_hansard_session_fetch():
    if not _hansard_ext_id:
        return False, "Skipped — no ext_id captured from debates/speech search"
    resp = requests.get(
        f"https://hansard-api.parliament.uk/debates/debate/{_hansard_ext_id}.json",
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    items = []

    def collect(node):
        items.extend(node.get("Items", []))
        for child in node.get("ChildDebates", []):
            collect(child)

    collect(data)
    overview = data.get("Overview", {})
    title = overview.get("Title", "?")
    return bool(items), f"{len(items)} items in '{title}'"


# ── TWFY API ──────────────────────────────────────────────────────────────────

def test_twfy_debates():
    if not TWFY_API_KEY:
        return False, "TWFY_API_KEY not set"
    resp = requests.get(
        "https://www.theyworkforyou.com/api/getDebates",
        params={
            "key": TWFY_API_KEY,
            "search": f"education {YYYYMMDD_TWO_WEEKS_AGO}..{TODAY_YYYYMMDD}",
            "type": "commons",
            "num": 5,
            "order": "d",
            "output": "json",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        return False, f"API error: {data['error']}"
    rows = data.get("rows", [])
    return bool(rows), f"{len(rows)} rows"


def test_twfy_wrans():
    if not TWFY_API_KEY:
        return False, "TWFY_API_KEY not set"
    resp = requests.get(
        "https://www.theyworkforyou.com/api/getWrans",
        params={
            "key": TWFY_API_KEY,
            "search": f"education {YYYYMMDD_TWO_WEEKS_AGO}..{TODAY_YYYYMMDD}",
            "num": 5,
            "output": "json",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        return False, f"API error: {data['error']}"
    rows = data.get("rows", [])
    return bool(rows), f"{len(rows)} rows (no type= param used)"


def test_twfy_wms():
    if not TWFY_API_KEY:
        return False, "TWFY_API_KEY not set"
    resp = requests.get(
        "https://www.theyworkforyou.com/api/getWMS",
        params={
            "key": TWFY_API_KEY,
            "search": f"education {YYYYMMDD_TWO_WEEKS_AGO}..{TODAY_YYYYMMDD}",
            "num": 5,
            "output": "json",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        return False, f"API error: {data['error']}"
    rows = data.get("rows", [])
    return True, f"{len(rows)} rows (0 is valid if no WMS in this period)"


# ── Parliament WQ API ─────────────────────────────────────────────────────────

def test_wq_api():
    resp = requests.get(
        "https://questions-statements-api.parliament.uk/api/writtenquestions/questions",
        params={
            "tabledWhenFrom": TWO_WEEKS_AGO,
            "answered": "Unanswered",
            "take": 5,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    results = data.get("results", [])
    return bool(results), f"{len(results)} questions"


def test_wq_api_with_dept():
    resp = requests.get(
        "https://questions-statements-api.parliament.uk/api/writtenquestions/questions",
        params={
            "tabledWhenFrom": TWO_WEEKS_AGO,
            "answeringBodies": 60,   # DfE
            "answered": "Unanswered",
            "take": 5,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    results = data.get("results", [])
    return bool(results), f"{len(results)} DfE questions (tabledWhenFrom+answeringBodies)"


# ── Parliament WMS API ────────────────────────────────────────────────────────

def test_wms_api():
    resp = requests.get(
        "https://questions-statements-api.parliament.uk/api/writtenstatements/statements",
        params={
            "madeWhenFrom": TWO_WEEKS_AGO,
            "take": 5,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    results = data.get("results", [])
    return True, f"{len(results)} statements (0 valid during recess)"


# ── Parliament Members API ────────────────────────────────────────────────────

def test_members_search():
    resp = requests.get(
        "https://members-api.parliament.uk/api/Members/Search",
        params={"Name": "Smith", "IsCurrentMember": "true", "take": 5},
        timeout=10,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    items = data.get("items", [])
    return bool(items), f"{len(items)} members matching 'Smith'"


def test_members_by_id():
    # Josh MacAlister — Parliament ID 5033
    resp = requests.get(
        "https://members-api.parliament.uk/api/Members/5033",
        timeout=10,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    name = data.get("value", {}).get("nameDisplayAs", "")
    passed = "MacAlister" in name
    return passed, f"nameDisplayAs={name!r}"


def test_members_biography():
    resp = requests.get(
        "https://members-api.parliament.uk/api/Members/5033/Biography",
        timeout=10,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    posts = data.get("value", {}).get("governmentPosts", [])
    current = [p for p in posts if p.get("endDate") is None]
    return True, f"{len(posts)} total posts, {len(current)} current"


# ── GOV.UK Content API ────────────────────────────────────────────────────────

def test_govuk_ministers():
    resp = requests.get(
        "https://www.gov.uk/api/content/government/ministers",
        timeout=10,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    links = data.get("links", {})
    cabinet = links.get("ordered_cabinet_ministers", [])
    depts = links.get("ordered_ministerial_departments", [])
    return bool(cabinet), f"{len(cabinet)} cabinet ministers, {len(depts)} departments"


# ── Committees API ────────────────────────────────────────────────────────────

def test_committees_api():
    resp = requests.get(
        "https://committees-api.parliament.uk/api/Committees",
        params={"status": "Current", "take": 5},
        timeout=15,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json()
    items = data.get("items", [])
    total = data.get("totalResults", "?")
    return bool(items), f"{len(items)} returned (total={total})"


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    ("Hansard /search/debates.json", test_hansard_debates_search),
    ("Hansard /search.json (speech)", test_hansard_speech_search),
    ("Hansard /search.json (minister memberId=5033)", test_hansard_minister_search),
    ("Hansard /debates/debate/{ext_id}.json", test_hansard_session_fetch),
    ("TWFY getDebates (type=commons)", test_twfy_debates),
    ("TWFY getWrans (no type=)", test_twfy_wrans),
    ("TWFY getWMS (no type=)", test_twfy_wms),
    ("Parliament WQ API (unanswered)", test_wq_api),
    ("Parliament WQ API (answeringBodies=60 DfE)", test_wq_api_with_dept),
    ("Parliament WMS API", test_wms_api),
    ("Members /Members/Search", test_members_search),
    ("Members /Members/5033 (MacAlister)", test_members_by_id),
    ("Members /Members/5033/Biography", test_members_biography),
    ("GOV.UK /government/ministers", test_govuk_ministers),
    ("Committees /Committees (status=Current)", test_committees_api),
]


def main():
    print("Westminster Brief - API Smoke Tests")
    print(f"Date window: {TWO_WEEKS_AGO} to {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 60)

    if not TWFY_API_KEY:
        print("WARNING: TWFY_API_KEY not set — TWFY tests will fail\n")

    for name, fn in TESTS:
        _run(name, fn)

    print()
    col_w = max(len(name) for name, _, _ in _results) + 2
    passed_count = 0
    for name, passed, detail in _results:
        mark = "PASS" if passed else "FAIL"
        print(f"  {mark}  {name:<{col_w}} {detail}")
        if passed:
            passed_count += 1

    total = len(_results)
    print()
    print(f"{passed_count}/{total} passed")

    if passed_count < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
