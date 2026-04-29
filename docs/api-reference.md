# Westminster Brief — Parliament API Reference

Last updated: 2026-04-29

This document records confirmed working parameters, response schemas, and known gotchas for every external API the app calls. Its purpose is to prevent re-investigating the same questions across sessions — the WQ API section in CLAUDE.md shows the cost of not having this.

**For the Parliament Written Questions API**, see the comprehensive section in `CLAUDE.md` — it is not duplicated here.

---

## 1. Hansard API

**Base URL:** `https://hansard-api.parliament.uk`  
**Auth:** None required  
**Last verified:** 2026-04-27

### Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/search/debates.json` | GET | Find debate sessions by title/keyword — returns session metadata |
| `/search.json` | GET | Find individual speeches by keyword or by minister (memberId) |
| `/debates/debate/{ext_id}.json` | GET | Fetch all speeches in a single debate session |

---

### `/search/debates.json` — Session title search

Finds debate sessions whose **title** matches the search term. Returns session-level metadata (no speech text). Use this to discover `DebateSectionExtId` values for follow-up session fetches.

**Hard cap of 25 results per call — pagination does not work on this endpoint.** The `skip`, `take`, and `orderBy` parameters are accepted but silently ignored; all calls return the same 25 items. `TotalResultCount` may exceed 25 (e.g. 41 for 2026-04-22).

**Critical: Commons Chamber and Westminster Hall are on SEPARATE LINKED LISTS.** Each sitting day has multiple independent chains of sessions. The search endpoint may return sessions from only one chain (e.g. Commons Chamber), missing the Westminster Hall chain entirely. Verified 2026-04-22: search returned 25 sessions all from Commons Chamber; Westminster Hall's 6 sessions were on a separate chain not in the search results.

**Solution: chain-walk via `NextDebateExtId` / `PreviousDebateExtId`.** The `Overview` of each full session response (see third endpoint below) contains links to adjacent sessions in the same chain. Starting from the search seeds and following these links in both directions gives complete coverage for the day. The Hansard Archive ingestion pipeline uses this approach — see `hansard_archive/ingestion.py`. Tested 2026-04-29: chain-walk found 30 sessions (24 Commons Chamber + 6 Westminster Hall) vs 25 from search alone.

**No searchTerm required for date-range queries.** Omitting `queryParameters.searchTerm` returns all sessions for the date range (up to the 25-item cap). This is how the archive ingestion pipeline uses this endpoint. A searchTerm is only needed when doing keyword-based session discovery.

**Confirmed working parameters:**

| Parameter | Type | Notes |
|-----------|------|-------|
| `queryParameters.searchTerm` | string | Title keyword(s). Phrase matching works. Boolean OR may be accepted here (unlike `/search.json`) |
| `queryParameters.house` | string | `Commons` or `Lords` |
| `queryParameters.startDate` | string | ISO format `YYYY-MM-DD` |
| `queryParameters.endDate` | string | ISO format `YYYY-MM-DD` |

**Note:** Date format is ISO (`YYYY-MM-DD`), **not** the TWFY `YYYYMMDD` format.

**Response top-level fields:**

| Field | Type | Notes |
|-------|------|-------|
| `Results` | array | Session metadata rows |
| `TotalResultCount` | int | Total matching sessions (not all may be returned) |

**Per-result fields:**

| Field | Notes |
|-------|-------|
| `DebateSectionExtId` | The ID used to fetch the full session — this is the key output |
| `Title` | Debate title |
| `SittingDate` | ISO datetime string — truncate to `[:10]` for date |
| `House` | `Commons` or `Lords` |
| `DebateSection` | Subsection name (e.g. `Westminster Hall`) — use to distinguish Westminster Hall from main chamber |
| `Rank` | Relevance score |

**Working example:**
```python
import requests

resp = requests.get(
    "https://hansard-api.parliament.uk/search/debates.json",
    params={
        "queryParameters.house": "Commons",
        "queryParameters.searchTerm": "student loans",
        "queryParameters.startDate": "2026-01-01",
        "queryParameters.endDate": "2026-04-27",
    },
    timeout=15,
)
data = resp.json()
for r in data.get("Results", []):
    print(r["DebateSectionExtId"], r["Title"], r["SittingDate"][:10])
```

---

### `/search.json` — Speech-level search

Finds individual **speech contributions** matching a keyword or by a specific minister. Use this when you need speech text (not just session titles), or when searching by minister ID.

**Confirmed working parameters:**

| Parameter | Type | Notes |
|-----------|------|-------|
| `queryParameters.searchTerm` | string | Keyword. **Boolean OR syntax `("a" OR "b")` is rejected — use a single phrase** |
| `queryParameters.house` | string | `Commons` or `Lords` |
| `queryParameters.startDate` | string | ISO format `YYYY-MM-DD` |
| `queryParameters.endDate` | string | ISO format `YYYY-MM-DD` |
| `queryParameters.memberId` | int | Parliament member ID — use for minister-specific searches |
| `take` | int | Page size (default unknown; safe to request up to 50) |
| `skip` | int | Pagination offset |

**Known gotcha:** Boolean OR syntax (`("a" OR "b")`) causes errors on this endpoint. If you have an AI-expanded query in OR form, extract the first quoted phrase and use that as the search term.

**Response top-level fields:**

| Field | Type | Notes |
|-------|------|-------|
| `Contributions` | array | Individual speech rows |
| `TotalContributions` | int | Total matching speeches |

**Per-contribution fields:**

| Field | Notes |
|-------|-------|
| `ContributionTextFull` | Full speech text (HTML) |
| `ContributionText` | Truncated speech text — prefer `ContributionTextFull` |
| `MemberName` | Speaker's display name |
| `AttributedTo` | Raw attribution string — used to parse party affiliation |
| `House` | `Commons` or `Lords` |
| `Section` | Section name (e.g. `Westminster Hall`) |
| `SittingDate` | ISO datetime — truncate to `[:10]` |
| `DebateSectionExtId` | Session ID — use to fetch the full session via the third endpoint |
| `DebateSection` | Debate title |
| `Rank` | Relevance score |

**Working example (minister search):**
```python
import requests

resp = requests.get(
    "https://hansard-api.parliament.uk/search.json",
    params={
        "queryParameters.memberId": 5033,          # Josh MacAlister — DfE minister
        "queryParameters.house": "Commons",
        "queryParameters.searchTerm": "student loans",
        "queryParameters.startDate": "2026-01-01",
        "take": 50,
    },
    timeout=25,
)
data = resp.json()
for c in data.get("Contributions", []):
    print(c["SittingDate"][:10], c["MemberName"], c["DebateSectionExtId"])
```

---

### `/debates/debate/{ext_id}.json` — Full session fetch

Fetches all speeches from a single debate section, identified by its `DebateSectionExtId`. This is the core mechanism for the "search finds debates → fetch all speeches" architecture. **No query parameters** — ext_id goes in the URL path.

**Response structure:**

| Field | Type | Notes |
|-------|------|-------|
| `Overview` | dict | Session metadata: `Title`, `Date`, `House` |
| `Items` | array | Direct speech items in this section |
| `ChildDebates` | array | Subsections — each has its own `Items` and `ChildDebates` |

To get all speeches, you must flatten `Items` recursively through `ChildDebates`.

**Overview fields (authoritative session metadata):**

| Field | Notes |
|-------|-------|
| `Overview.Title` | Session title |
| `Overview.Date` | ISO datetime |
| `Overview.House` | `Commons` or `Lords` |
| `Overview.Location` | **"Commons Chamber" or "Westminster Hall"** — authoritative location, more reliable than search results |
| `Overview.HRSTag` | **Hansard classification tag** — use for accurate debate_type. Key values: `hs_8Question` (oral questions), `hs_2BillTitle` / `hs_2BillHd` (bill debates), `hs_8Petition` (petitions), `hs_2BusinessWODebate` (formal business). Verified 2026-04-29. |
| `Overview.NextDebateExtId` | **ext_id of the next session in this day's chain** — NULL at end of chain or when crossing to the next day. Use for chain-walking. |
| `Overview.PreviousDebateExtId` | **ext_id of the previous session in this day's chain** — NULL at start of chain. Use for chain-walking. |
| `Overview.DebateTypeId` | Numeric type (1=Debate, others TBD) — less informative than HRSTag |
| `Overview.SectionType` | Numeric section type — less informative than HRSTag |

**Per-item fields:**

| Field | Notes |
|-------|-------|
| `Value` | Speech text (HTML) |
| `MemberName` | Speaker name (clean) — prefer this over `AttributedTo` |
| `AttributedTo` | Raw attribution — use for party parsing when `MemberName` is missing |
| `MemberId` | Parliament member ID (may be absent for procedural items) |
| `SittingDate` | ISO datetime |

**Working example:**
```python
import requests

ext_id = "your-ext-id-here"
resp = requests.get(
    f"https://hansard-api.parliament.uk/debates/debate/{ext_id}.json",
    timeout=15,
)
data = resp.json()

# Flatten Items recursively
items = []
def collect(node):
    items.extend(node.get("Items", []))
    for child in node.get("ChildDebates", []):
        collect(child)
collect(data)

overview = data.get("Overview", {})
print(f"{overview.get('Title')} — {len(items)} items")
```

**Cache:** App uses 720-hour (30-day) TTL for session fetches — published debate transcripts don't change.

---

### Hansard URL construction

To build a browseable Hansard link from API data:
```
https://hansard.parliament.uk/{Commons|Lords}/{YYYY-MM-DD}/debates/{DebateSectionExtId}/{TitleSlug}
```
Where `TitleSlug` is PascalCase with no spaces or special characters (e.g. `StudentLoans`).

---

## 2. TheyWorkForYou (TWFY) API

**Base URL:** `https://www.theyworkforyou.com/api/`  
**Auth:** `key=TWFY_API_KEY` — required on every call  
**Last verified:** 2026-04-27  
**Additional reference:** CLAUDE.md "TWFY API — known quirks" section

### Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `getDebates` | GET | Hansard Commons, Lords, Westminster Hall debates |
| `getWrans` | GET | Written Answers |
| `getWMS` | GET | Written Ministerial Statements |

**Base URLs:**
- Debates: `https://www.theyworkforyou.com/api/getDebates`
- Wrans: `https://www.theyworkforyou.com/api/getWrans`
- WMS: `https://www.theyworkforyou.com/api/getWMS`

---

### Confirmed working parameters

| Parameter | Valid on | Type | Notes |
|-----------|----------|------|-------|
| `key` | all | string | API key — required |
| `output` | all | string | **Must be `json`** — omitting returns XML |
| `search` | all | string | Keyword search. Date range embedded as `search=term 20260101..20260427` |
| `num` | all | int | Results per call — max 1000; default is ~10 |
| `order` | all | string | `r` (relevance), `d` (date desc) |
| `type` | `getDebates` only | string | `commons`, `lords`, `westminsterhall` — **do NOT pass to getWrans or getWMS** |
| `gid` | `getDebates`, `getWMS` | string | Fetch a specific session by group ID |
| `person` | `getDebates` | int | TWFY person ID — fetch all debates for a specific person |

### Date range syntax

TWFY embeds the date range **inside the search string** (not as separate params):
```
search=student loans 20260101..20260427
```
Format: `YYYYMMDD..YYYYMMDD` — note no dashes, not ISO format.

### Known gotchas

- **`person=` ignores date range.** When you use `person=ID`, TWFY ignores any date embedded in the search string. Always apply a Python-level date filter on the returned `hdate` field after fetching.
- **`type=` is getDebates-only.** Passing `type=` to `getWrans` or `getWMS` is silently ignored or causes errors.
- **`output=json` is required.** Omitting it gives XML (not JSON).
- **Boolean OR in search string.** TWFY cannot parse `("a" OR "b") 20260101..20260427`. Strip outer parentheses before appending the date range.
- **`hdate` format.** TWFY returns dates as `YYYY-MM-DD` strings in the `hdate` field — not `YYYYMMDD`. Convert before comparing with date range parameters.
- **Empty result format.** `{"rows": []}` (not an error key) when nothing is found.

### Response structure

```python
{
    "rows": [
        {
            "gid": "2026-04-01.123.0",       # debate group ID
            "listurl": "/debates/?id=...",    # relative — prepend https://www.theyworkforyou.com
            "body": "<p>Speech text...</p>", # HTML
            "hdate": "2026-04-01",
            "speaker": {"name": "Josh MacAlister", "party": "Labour"},
            "parent": {"body": "<p>Oral Answers to Questions</p>"},
            "relevance": 95,
        }
    ]
}
```

### Working example

```python
import requests

TWFY_API_KEY = "your-key"

resp = requests.get(
    "https://www.theyworkforyou.com/api/getDebates",
    params={
        "key": TWFY_API_KEY,
        "search": "student loans 20260101..20260427",
        "type": "commons",
        "num": 50,
        "order": "d",
        "output": "json",
    },
    timeout=15,
)
data = resp.json()
for row in data.get("rows", []):
    print(row["hdate"], row["speaker"]["name"], row["listurl"])
```

---

## 3. Parliament Written Questions API

See the comprehensive **"WQ API constraints"** section in `CLAUDE.md`. Not duplicated here.

**Short summary:** Use `tabledWhenFrom` / `tabledWhenTo` (not `tabledStartDate`). Use `answeringBodies` (integer dept ID) with a date anchor. Use `answered` (not `isAnswered`). Paginate with `take` / `skip`.

---

## 4. Parliament Written Ministerial Statements API

**Base URL:** `https://questions-statements-api.parliament.uk/api/writtenstatements/statements`  
**Auth:** None required  
**Last verified:** 2026-04-27

### Confirmed working parameters

| Parameter | Type | Notes |
|-----------|------|-------|
| `take` | int | Results per call |
| `madeWhenFrom` | string | ISO `YYYY-MM-DD` — statements made on or after this date |
| `madeWhenTo` | string | ISO `YYYY-MM-DD` — statements made on or before this date |
| `answeringBodies` | int | Department ID (same IDs as WQ API) — filters by making department |
| `searchTerm` | string | Keyword filter — OR-word matching, not phrase matching |

**Department IDs** (same as WQ API): DfE=60, DHSC=17, Treasury=14, Home Office=1, MoD=11, MoJ=54, DSIT=216, Cabinet Office=53

### Response structure

```python
{
    "results": [
        {
            "value": {
                "title": "Students: Loans",
                "text": "<p>The Secretary of State...</p>",  # HTML
                "dateMade": "2026-04-01T00:00:00",
                "house": "Commons",
                "uin": "WMS-2026-04-01-...",
                "memberId": 1234,
            }
        }
    ]
}
```

### Known gotchas

- `searchTerm` does OR-word matching, not phrase matching. "student loans" matches any statement containing either "student" or "loans". Post-filter by keyword stems client-side for precision.
- When `answeringBodies` is set, omit `searchTerm` and post-filter instead — the dept filter alone narrows the set enough.
- `memberId` identifies the making minister — use the Members API to resolve to a name.

### Hansard URL for WMS items

```
https://hansard.parliament.uk/{Commons|Lords}/{YYYY-MM-DD}/writtenstatements/{uin}
```

### Working example

```python
import requests
from datetime import datetime, timedelta

week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

resp = requests.get(
    "https://questions-statements-api.parliament.uk/api/writtenstatements/statements",
    params={
        "madeWhenFrom": week_ago,
        "answeringBodies": 60,   # DfE
        "take": 50,
    },
    timeout=15,
)
for item in resp.json().get("results", []):
    v = item["value"]
    print(v["dateMade"][:10], v["title"])
```

---

## 5. Parliament Members API

**Base URL:** `https://members-api.parliament.uk`  
**Auth:** None required  
**Last verified:** 2026-04-27

### Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/Members/{id}` | GET | Fetch a member by Parliament ID |
| `/api/Members/Search` | GET | Search members by name |
| `/api/Members/{id}/Biography` | GET | Fetch biography with government posts |
| `/api/Members/{id}/RegisteredInterests` | GET | Fetch registered financial interests |
| `/api/Posts/GovernmentPosts` | GET | Fetch all current government posts (Cabinet only) |

---

### `/api/Members/{id}` — Member by ID

**Response:**
```python
{
    "value": {
        "id": 5033,
        "nameDisplayAs": "Josh MacAlister",
        "latestParty": {"abbreviation": "Lab", "name": "Labour"},
        "thumbnailUrl": "https://...",
        "latestHouseMembership": {"house": 1}   # 1=Commons, 2=Lords
    }
}
```

**Canonical test IDs:**
- `5033` — Josh MacAlister (DfE, Commons)
- `269` — Baroness Smith of Malvern (DfE, Lords) — Parliament ID, not TWFY person ID

---

### `/api/Members/Search` — Name search

| Parameter | Type | Notes |
|-----------|------|-------|
| `Name` | string | Member name — partial matching works |
| `IsCurrentMember` | string | `true` to restrict to sitting members |
| `take` | int | Results per call |

**Response:**
```python
{
    "items": [
        {"value": {"id": 5033, "nameDisplayAs": "Josh MacAlister", ...}}
    ]
}
```

**Known gotcha:** Lords names with titles (`Baroness Smith of Malvern`) can fail to match if the search string doesn't include the title. Try with just the surname as a fallback.

---

### `/api/Members/{id}/Biography` — Government posts

Use this to verify whether someone currently holds a government role.

**Response includes:**
```python
{
    "value": {
        "governmentPosts": [
            {
                "name": "Parliamentary Under Secretary of State...",
                "startDate": "2024-07-05T00:00:00",
                "endDate": None   # None means currently held
            }
        ]
    }
}
```

A post with `endDate == None` is currently held.

---

### `/api/Posts/GovernmentPosts` — Cabinet posts

Returns all current government posts with holders. Cabinet only — junior ministers not included. Use the GOV.UK API (section 7) for a complete minister list.

**Response:** Array of post objects with current holder name and Parliament ID.

---

## 6. Parliament Committees API

**Base URL:** `https://committees-api.parliament.uk`  
**Auth:** None required  
**Last verified:** 2026-04-27

### Endpoint

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/Committees` | GET | List committees |

**Confirmed parameters:**

| Parameter | Type | Notes |
|-----------|------|-------|
| `status` | string | `Current`, `Closed`, `Any` |
| `take` | int | Results per call (use 300 to get all current committees) |

**Response:**
```python
{
    "items": [
        {
            "value": {
                "id": 123,
                "name": "Education Committee",
                "house": "Commons",
                "commonsAppointedDate": "2024-07-01T00:00:00"
            }
        }
    ],
    "totalResults": 153
}
```

### Working example

```python
import requests

resp = requests.get(
    "https://committees-api.parliament.uk/api/Committees",
    params={"status": "Current", "take": 300},
    timeout=15,
)
data = resp.json()
for item in data.get("items", []):
    v = item["value"]
    print(v["id"], v["name"], v.get("house"))
```

---

## 7. GOV.UK Content API (Minister Lists)

**Base URL:** `https://www.gov.uk/api/content`  
**Auth:** None required  
**Last verified:** 2026-04-27

Used to fetch the current list of government ministers — more complete than the Parliament GovernmentPosts endpoint (includes junior ministers, not just Cabinet).

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/government/ministers` | Top-level ministers page — cabinet list + dept links |
| `{dept_base_path}/ministers` | All ministers for a specific department |

### Top-level ministers page

```python
resp = requests.get("https://www.gov.uk/api/content/government/ministers", timeout=10)
links = resp.json()["links"]
# Cabinet ministers
cabinet = links.get("ordered_cabinet_ministers", [])
# Department links (each has a base_path for the dept ministers endpoint)
depts = links.get("ordered_ministerial_departments", [])
```

Each dept in `ordered_ministerial_departments` has:
- `base_path` — e.g. `/government/organisations/department-for-education`
- `title` — dept display name

### Department ministers page

```python
dept_base_path = "/government/organisations/department-for-education"
resp = requests.get(f"https://www.gov.uk/api/content{dept_base_path}/ministers", timeout=10)
ministers = resp.json()["details"]["ordered_ministers"]
# Each entry: {"title": "Baroness Smith of Malvern", "role": "Minister of State for Skills"}
```

### Known gotchas

- `details.ordered_ministers` uses the `title` key for the minister's name (confusingly, not `name`).
- The `role` field gives their full title.
- GOV.UK minister names need normalisation before name-matching against Hansard/TWFY data — remove titles, normalise unicode.
- **File-cache for 30 days** (in `minister_cache.json`) — reshuffles are infrequent.

---

## 8. Department ID Reference

Department IDs used across WQ API, WMS API, and Members API:

| Department | ID |
|---|---|
| Home Office | 1 |
| Ministry of Defence | 11 |
| HM Treasury | 14 |
| Department of Health and Social Care | 17 |
| Department for Transport | 21 |
| Department for Work and Pensions | 29 |
| Ministry of Housing, Communities and Local Government | 7 |
| Ministry of Justice | 54 |
| Department for Education | 60 |
| Department for Culture, Media and Sport | 47 |
| Department for Environment, Food and Rural Affairs | 13 |
| Cabinet Office | 53 |
| Department for Science, Innovation and Technology | 216 |
| Department for Energy Security and Net Zero | 202 |
| Foreign, Commonwealth and Development Office | 208 |

---

## 9. Canonical Test Cases

Use these for smoke testing — the data is known to exist:

| Test | API | Query | Expected |
|------|-----|-------|----------|
| DfE minister speech | Hansard `/search.json` | `memberId=5033 searchTerm=student` | Contributions from Josh MacAlister |
| Debate session | Hansard `/debates/debate/{ext_id}.json` | Any ext_id from search results | Items list non-empty |
| WQ unanswered | WQ API | `tabledWhenFrom=2 weeks ago answeringBodies=60 answered=Unanswered` | Questions from DfE |
| WMS statements | WMS API | `madeWhenFrom=2 weeks ago answeringBodies=60` | DfE statements |
| MP lookup | Members `/Members/5033` | — | `nameDisplayAs=Josh MacAlister` |
| Committee list | Committees `/Committees` | `status=Current take=5` | items non-empty |
