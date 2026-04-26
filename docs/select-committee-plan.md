# Plan: Select Committee Evidence Tracker — New Standalone Page

## Context

Civil servants, policy officers, and public affairs professionals regularly need to know what select committees have said about a topic — which committees held evidence sessions, what witnesses argued, what the committee concluded, and how the government responded. Currently this requires manual searching at committees.parliament.uk. The feature brings it into Westminster Brief as a keyword-driven research tool, consistent with the "factual or extracted, never authored" output rule.

**User choices made:**
- Research first (monitoring/alerts deferred to premium tier)
- All four content types: oral evidence, written submissions, reports, witness lists
- New standalone page (not a tab inside the Research Tool)

---

## API: committees-api.parliament.uk

Confirmed live, public, no auth required. Tested: `?SearchTerm=student+loan` returns 37 results.

| Endpoint | Purpose | Key params |
|---|---|---|
| `GET /api/Publications?SearchTerm=...&take=...&skip=...` | Keyword search across all publication types | `SearchTerm`, `take`, `skip`; `CommitteeId` may work (test at runtime) |
| `GET /api/Committees?status=Current&take=200` | All current committees for dropdown | Returns `{id, name, house}` per item |

**Publication result shape:**
```
{
  id, description,
  type: {name: "Oral evidence" | "Written evidence" | "Report" | "Government response" | ...},
  publicationStartDate,
  committee: {id, name, house: {name: "Commons"|"Lords"}},
  businesses: [{id, title, openDate, closeDate}],  // inquiry titles
  documents: [{documentId, fileName, url, fileDataFormat}]
}
```

Response envelope: `{items: [...], totalResults: N, itemsPerPage: N}`

---

## Files to create/modify

| File | Action |
|---|---|
| `committees.py` | **New** — blueprint, API helpers, `/committees` route, `/download_committee_brief` route |
| `templates/committees.html` | **New** — search form + results template |
| `flask_app.py` | Add import + `app.register_blueprint(committees_bp)` |
| `templates/base.html` | Add nav link after Debate Prep |

---

## 1. `committees.py` — structure

### Constants
```python
COMMITTEES_API = "https://committees-api.parliament.uk"
```

### `_fetch_all_committees()` — for dropdown
```python
resp = requests.get(f"{COMMITTEES_API}/api/Committees",
    params={'status': 'Current', 'take': 200}, timeout=10)
# Returns sorted list of {id, name, house_name}
# Cache at module level: (_committees_cache, _committees_cache_ts) — refresh every 24h
```

### `_fetch_publications(search_term, committee_id=None, take=50)`
```python
params = {'SearchTerm': search_term, 'take': take, 'skip': 0}
if committee_id:
    params['CommitteeId'] = committee_id  # test at runtime; client-side fallback if ignored
resp = requests.get(f"{COMMITTEES_API}/api/Publications", params=params, timeout=15)
# Returns (items_list, total_results)
```

### `_group_publications(items)` — normalise + group by type
Groups items by `type.name` into four buckets:
- `reports` — type names containing "Report" or "Government response"
- `oral` — type name "Oral evidence"
- `written` — type name "Written evidence"
- `other` — everything else

Each normalised item:
```python
{
    'id': item['id'],
    'committee_name': item['committee']['name'],
    'committee_house': item['committee']['house']['name'],
    'inquiry_title': item['businesses'][0]['title'] if item.get('businesses') else '',
    'date': (item.get('publicationStartDate') or '')[:10],
    'description': item.get('description', ''),
    'type_name': item['type']['name'],
    'documents': [{'name': d['fileName'], 'url': d['url']} for d in item.get('documents', [])],
}
```

### `_committee_ai_summary(search_term, grouped)` — optional
Short prompt to Gemini/Claude: given publication titles + descriptions retrieved, produce a 2–3 sentence overview of what select committees have examined on this topic. Returns plain text or None on failure. Uses `_claude_fallback` imported from `debate_scanner`. This operates only on retrieved metadata — consistent with the output rule.

### `/committees` route (GET + POST)
```python
committees_bp = Blueprint('committees', __name__)

@committees_bp.route('/committees', methods=['GET', 'POST'])
@login_required
def committees():
    all_committees = _fetch_all_committees()  # cached

    if request.method == 'GET':
        return render_template('committees.html',
            is_post=False, committees=all_committees,
            search_term='', selected_committee='',
            grouped={}, total=0, ai_summary=None, error=None)

    search_term = request.form.get('search_term', '').strip()
    committee_id = request.form.get('committee_id', '') or None

    items, total = _fetch_publications(search_term, committee_id)

    # Client-side committee filter as fallback if API ignored CommitteeId
    if committee_id:
        items = [i for i in items if str(i['committee']['id']) == str(committee_id)]
        total = len(items)

    grouped = _group_publications(items)
    ai_summary = _committee_ai_summary(search_term, grouped)

    return render_template('committees.html',
        is_post=True, committees=all_committees,
        search_term=search_term, selected_committee=committee_id or '',
        grouped=grouped, total=total, ai_summary=ai_summary, error=None)
```

### `/download_committee_brief` route (POST)
Follows `/download_debate_prep_brief` pattern:
- Accepts `results_json`, `search_term`, `ai_summary` hidden fields
- Builds Word doc: Cover block → AI Overview → Reports table → Oral Evidence table → Written Evidence table
- Imports `_add_hyperlink` from `debate_scanner`: `from debate_scanner import _add_hyperlink`
- Returns `send_file(mem_doc, as_attachment=True, download_name=f"Committee Evidence - {safe_term}.docx", ...)`

---

## 2. `templates/committees.html`

Structure follows `debate_prep.html`:

```
[Search form — always visible]
  Topic: [text input, required]
  Committee: [select — "All Committees" + sorted list from API]
  [Search button]

[Results — only when is_post]
  Blue summary bar: "X publications found across Y committees"  (same style as result_counts bar)

  [AI Overview — collapsible, amber AI-warning footnote]

  [📋 Reports & Recommendations (N)]   ← <details> collapsible
    Table: Date | Committee | Inquiry | Description | Documents

  [🗣️ Oral Evidence (N)]               ← <details> collapsible
    Table: Date | Committee | Inquiry | Documents

  [✍️ Written Evidence (N)]            ← <details> collapsible
    Table: Date | Committee | Inquiry | Documents

  [Other (N)]                           ← collapsible, only shown if non-empty

  [⬇ Download Word Brief]
    hidden: results_json, search_term, ai_summary
```

BETA badge on heading. "None found" shown per section — never silently hidden.

---

## 3. `flask_app.py` changes

After existing blueprint imports (~line 54):
```python
from committees import committees_bp
```

After existing blueprint registrations (~line 806):
```python
app.register_blueprint(committees_bp)
```

---

## 4. `templates/base.html` nav link

After the Debate Prep link:
```html
<a href="/committees" {% if request.path.startswith('/committees') %}class="nav-active"{% endif %}>
    Committees <span class="badge-beta">BETA</span>
</a>
```

---

## Reused components (do not rewrite)

| Component | Source |
|---|---|
| `_add_hyperlink(paragraph, url, text)` | `debate_scanner.py` line 21 — import directly |
| `_claude_fallback(prompt, max_tokens)` | `debate_scanner.py` — import for AI summary fallback |
| `copy_current_request_context` | Flask — same pattern as all other routes |
| `.badge-beta` CSS | `static/style.css` — already defined |
| Word doc margin/heading/table pattern | Copy from `debate_scanner.py` `/download_debate_prep_brief` |

---

## Deferred (not in this plan)

- Date range filter — `/api/CommitteeBusiness` supports it but adds complexity; easy follow-up
- Monitoring/alerts — premium tier
- Witness names as a distinct section — not a top-level API field; surface from document metadata if available in a follow-up
- Pagination — first page of 50 results is sufficient for an initial search; add skip/take later

---

## Verification

1. GET `/committees` — form renders, committee dropdown populated (should have ~30–40 committees)
2. Search "student loan repayments", no filter — results appear, grouped correctly by type
3. Search "student loan repayments", filter to Education Committee — results narrow to that committee only
4. Document links open valid `committees.parliament.uk` or Parliament PDF URLs
5. AI summary appears (or page still works if AI unavailable — no crash)
6. Download Word brief — all sections present, hyperlinks functional
7. Search with no results — "None found" in each section, no 500 error
8. Nav "Committees" link highlights when on `/committees`
