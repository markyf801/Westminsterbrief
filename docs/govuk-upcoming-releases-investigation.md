# GOV.UK Upcoming Releases — API Investigation

**Conducted:** 18 May 2026  
**Conclusion: Build can proceed as written.** Confirmed/provisional distinction is clean in the Content API. No authentication required. Daily caching is the right refresh cadence.

---

## 1. Authentication

None required. Both the HTML search page and the GOV.UK Content API are public endpoints with no API key or OAuth.

## 2. Rate limits

No documented rate limits for the GOV.UK search or Content APIs. GOV.UK infrastructure handles high public traffic. A daily refresh calling ~10–20 Content API endpoints per organisation is well within acceptable use. Being polite (0.25s delay between calls) is sufficient.

## 3. Confirmed/provisional distinction — CLEAN ✓

This was the critical gate. **The distinction is clean and machine-readable.**

The GOV.UK Content API exposes a `details.state` field with three values:

| Value | Meaning | Display date format |
|---|---|---|
| `"confirmed"` | Confirmed release date | "21 May 2026 9:30am" |
| `"provisional"` | Provisional date, subject to change | "June 2026" |
| `"cancelled"` | Cancelled — must be excluded from display | N/A |

**Verified example — confirmed:**
```
GET /api/content/government/statistics/announcements/serious-incident-notifications-2025-to-2026
details.state:            "confirmed"
details.release_timestamp: "2026-05-21T09:30:00.000+01:00"
details.display_date:     "21 May 2026 9:30am"
content_id:               "404c8e73-81bc-4d08-8906-a7dbf62c3d2f"
```

**Verified example — provisional:**
```
GET /api/content/government/statistics/announcements/leo-graduates-and-postgraduates-outcomes-2023-to-2024
details.state:            "provisional"
details.release_timestamp: "2026-06-25T09:30:00.000+01:00"
details.display_date:     "June 2026"
content_id:               "24036936-0839-4a1a-a6d1-e6c42955184b"
```

Note: `release_timestamp` is populated even for provisional items (an internal GOV.UK estimate), but `display_date` only shows month+year for provisional. Use `display_date` for display; use `release_timestamp` for sorting and filtering.

## 4. Data access approach

### What doesn't work

- **GOV.UK Search API (`/api/search.json`)**: Returns 0 results for `filter_content_store_document_type=upcoming_statistics` and `filter_document_type=statistics_announcement`. The statistics announcement document type is not indexed via the standard search API filters. Multiple filter syntaxes tested; all returned empty.
- **Atom feed** (`/search/research-and-statistics.atom`): Contains `<updated>` (when the listing was edited) but no `release_date` or confirmed/provisional field. Unusable for our purpose.
- **JSON suffix** (`/search/statistics-announcements.json`): 404.

### What works — recommended approach

**Two-step per organisation per daily refresh:**

**Step 1 — HTML page to get slug list:**

```
GET /search/statistics-announcements?organisations[]={org-slug}&release_date_after={today}
```

This returns an HTML page listing all upcoming statistics for that organisation, filtered to future dates. Parse the result links to extract `base_path` slugs (e.g. `/government/statistics/announcements/serious-incident-notifications-2025-to-2026`).

**Step 2 — Content API per slug:**

```
GET /api/content/government/statistics/announcements/{slug}
```

Returns full structured JSON with all fields needed. Only call this for slugs not already in the DB (or where the item may have changed).

This keeps Content API calls proportional to genuinely new or changed announcements — typically a handful per organisation per day — not the full 87-item DfE backlog on every refresh.

### Why HTML scraping for Step 1

The HTML search page is the only way to get a list of upcoming statistics filtered by organisation. It is stable GOV.UK infrastructure (the finder-frontend) and the URL pattern has been unchanged for years. The scraping target is the `href` attributes of result links — a very stable HTML structure. If GOV.UK changes this structure, the refresh job will return an empty list (fail-safe), not garbage data.

## 5. Full field inventory — Content API response

Fields from `GET /api/content/government/statistics/announcements/{slug}`:

| Field | Type | Description |
|---|---|---|
| `content_id` | UUID string | Stable unique identifier — use as primary dedup key |
| `base_path` | string | URL path, e.g. `/government/statistics/announcements/...` |
| `title` | string | Publication title |
| `description` | string | One-paragraph summary |
| `details.state` | string | `"confirmed"` / `"provisional"` / `"cancelled"` |
| `details.release_timestamp` | ISO 8601 | Machine-readable release datetime (populated even for provisional) |
| `details.display_date` | string | Human-readable date ("21 May 2026 9:30am" or "June 2026") |
| `details.document_type_label` | string | "Official Statistics", "National Statistics", "Experimental Statistics" |
| `document_type` | string | Always `"official_statistics_announcement"` for these items |
| `links.organisations` | array | Org objects with `title` and `base_path` (slug derivable from base_path) |
| `public_updated_at` | ISO 8601 | When this content was last updated on GOV.UK — useful for change detection |
| `first_published_at` | ISO 8601 | When first announced |

Fields to ignore: `analytics_identifier`, `locale`, `phase`, `publishing_app`, `rendering_app`, `schema_name`, `withdrawn_notice`.

## 6. Organisation slug coverage

Confirmed working (tested):

| Org | Slug | Test result |
|---|---|---|
| Department for Education | `department-for-education` | 87 upcoming items ✓ |
| Dept of Health & Social Care | `department-of-health-and-social-care` | Results confirmed ✓ |
| Ministry of Housing, Communities and Local Govt | `ministry-of-housing-communities-local-government` | 56 items ✓ (note: no "and" in slug) |

**Important slug gotcha:** MHCLG's slug is `ministry-of-housing-communities-local-government` — the "and" is dropped. Verify all slugs against `https://www.gov.uk/government/organisations/{slug}` before seeding the mapping. Wrong slugs return 0 results silently (not an error).

Full mapping in `docs/theme-to-organisation-mapping.md`.

## 7. Cancelled state — must filter

The HTML search showed "cancelled" items in the DHSC results. The `details.state = "cancelled"` value must be excluded from display and from the DB. The refresh job should skip any item where `state == "cancelled"`.

## 8. Rate limits — practical observation

No timeouts or 429s observed during investigation. Fetching ~10 Content API endpoints in rapid succession worked without issue. Add a 0.25s sleep between calls as a courtesy; no exponential backoff needed for this use case.

---

## Decision gate outcome

| Gate | Result |
|---|---|
| Clean confirmed/provisional distinction in JSON | ✓ Yes — `details.state` field |
| No authentication required | ✓ Confirmed |
| Rate limits compatible with daily refresh | ✓ Confirmed |
| Organisation slug coverage for all themes | ✓ Mapping drafted in separate doc |

**Build can proceed as specified in the addendum.** No redesign required.

**One additional finding not in the addendum:** handle `state = "cancelled"` — skip these in the refresh job and delete any already in the DB.
