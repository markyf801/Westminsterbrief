# Phase 2 Addendum: Upcoming Releases Panel on Topic Brief Pages

## Scope

Add a small panel to `/brief/{slug}` pages showing upcoming official statistics releases relevant to that theme, pulled from the GOV.UK search API. One feature, contained scope, slots into the existing Phase 2 build between build order steps 17 and 18.

**Not in scope for this addendum:** refresh-timing optimisation for the headline stats job; predictive alerts on Smart Alerts. Both documented as Phase 3 backlog.

## Pre-build investigation (do first, before any schema or code)

Pull the GOV.UK search API JSON endpoint and confirm the data structure. Likely endpoint:

```
https://www.gov.uk/api/search.json?filter_content_store_document_type=upcoming_statistics&filter_organisations=department-for-education&order=-release_timestamp&count=20
```

Confirm by inspection:

1. **Authentication** — does the endpoint require an API key? Public search likely no, but verify.
2. **Rate limits** — find documented limits or test gently. Determines whether per-render query is acceptable or daily caching is required.
3. **Date field structure** — the HTML page shows confirmed dates, provisional dates, and "to be confirmed" entries. Identify which field(s) in the JSON expose this distinction. Critical: a provisional date displayed as confirmed will erode trust. If the JSON doesn't make this distinction cleanly, the build is more complex than it looks — pause and flag.
4. **Organisation slug coverage** — confirm slugs for all departments Westminster Brief maps themes to. Build a mapping table (theme_slug → list of relevant organisation slugs). Some themes map to multiple orgs (Health → DHSC + NHS England). Save the mapping to `/docs/theme-to-organisation-mapping.md`.
5. **Field inventory** — list every field returned per release. Identify: title, release date, release date status (confirmed/provisional), summary, link to publication, organisation name, document type, any uniqueness identifier.

Save findings to `/docs/govuk-upcoming-releases-investigation.md` before writing schema or code.

## Decision gate after investigation

If investigation reveals:
- **Clean confirmed/provisional distinction in JSON** → proceed with full build below.
- **No clean distinction** → either drop the feature for Phase 2 or display all dates with a blanket "dates may be provisional" caveat. Mark to decide before continuing.
- **Rate limits incompatible with daily refresh** → revise refresh cadence and document.
- **Endpoint requires authentication** → pause; reassess whether feature is worth the auth complexity.

## Data model

Single cached table, refreshed daily:

```sql
CREATE TABLE upcoming_release (
    id                  SERIAL PRIMARY KEY,
    govuk_content_id    TEXT NOT NULL,        -- stable identifier from GOV.UK API
    theme_slug          TEXT NOT NULL,        -- mapped from organisation_slug via theme-to-organisation-mapping
    organisation_slug   TEXT NOT NULL,        -- raw from API, e.g. 'department-for-education'
    organisation_name   TEXT NOT NULL,        -- display name, e.g. 'Department for Education'
    title               TEXT NOT NULL,
    summary             TEXT,
    release_date        DATE NOT NULL,
    release_date_confirmed BOOLEAN NOT NULL,  -- true=confirmed, false=provisional
    publication_url     TEXT NOT NULL,        -- link to GOV.UK page for the upcoming release
    document_type       TEXT,                 -- 'official_statistics', 'national_statistics', etc.
    last_refreshed      TIMESTAMPTZ NOT NULL,
    UNIQUE (govuk_content_id, theme_slug)
);
CREATE INDEX idx_upcoming_release_theme_date ON upcoming_release(theme_slug, release_date);
```

`govuk_content_id` + `theme_slug` is the unique key because one release can map to multiple themes if its organisation maps to multiple themes (rare but possible).

Daily refresh truncates and re-seeds — releases get cancelled, postponed, or merged on GOV.UK side, and the cleanest way to stay accurate is to take the live state as source of truth each day.

## Refresh job

Flask CLI command `flask upcoming refresh-all`. Daily Railway cron, 05:00 UTC (before the 06:00 stats refresh on Mondays).

For each organisation slug in the theme-to-organisation mapping:
1. Query `https://www.gov.uk/api/search.json` with appropriate filters (upcoming_statistics document type, organisation filter, order by release_timestamp ascending, count limit).
2. Parse response, map each release to one or more theme_slugs via the mapping.
3. Insert rows. Use `TRUNCATE upcoming_release; INSERT …` pattern, or `DELETE WHERE last_refreshed < NOW() - INTERVAL '2 hours'` after insert to clear stale rows.
4. Log per-organisation counts and any API errors.

SWR: if the API call fails for a given organisation, leave existing rows for that organisation in place and log the failure. Don't blank user-facing pages because GOV.UK had a hiccup.

Add `flask upcoming refresh-organisation {slug}` for single-org manual runs during dev.

## Display

New panel on `/brief/{slug}` pages, positioned **between** the statistics section and the parliamentary activity section. Heading: "Upcoming official statistics".

Show up to 5 releases, ordered by `release_date` ascending. For each:

```
[Date — formatted "11 June 2026"]
[Title, linked to publication_url]
[Organisation name]
[If release_date_confirmed = false: "Provisional date" tag]
```

If no upcoming releases for the theme: hide the panel entirely. Do not show "No upcoming releases" — that's noise.

Below the list: a small link "View full GOV.UK release calendar →" pointing to the relevant filtered GOV.UK search URL for that theme's organisations.

**Not on Research Tool result pages.** Topic brief pages only.

## Schema.org

Skip. Upcoming releases are pre-publication metadata, not authoritative published data. Don't add JSON-LD; could confuse Google about the nature of the page content.

## Methodology page update

Add an 8th section to `/about/statistics`:

> **Upcoming releases.** We display upcoming official statistics from GOV.UK's release calendar. Dates may be provisional and can change without notice. Confirmed dates are shown without a tag; provisional dates are labelled. Source: GOV.UK release calendar, refreshed daily.

## Build order — insertion into main brief

Add after step 17 (topic brief page) and before step 18 (Research Tool integration):

- **Step 17a.** GOV.UK upcoming releases investigation. Save findings to `/docs/`. Mark reviews and signs off on the theme-to-organisation mapping and the confirmed/provisional date handling before proceeding.
- **Step 17b.** `upcoming_release` schema.
- **Step 17c.** Refresh job (Flask CLI + Railway daily cron).
- **Step 17d.** Display panel on `/brief/{slug}` pages.
- **Step 17e.** Methodology page section 8.

Estimated ~3 days of work assuming clean investigation findings.

## Acceptance criteria

Build-time:
- Investigation document complete and Mark-reviewed.
- Theme-to-organisation mapping saved to `/docs/`.
- `upcoming_release` table populated with at least one row per active mapped organisation (assuming GOV.UK has upcoming entries for them, which it will for the major departments).
- Panel renders on `/brief/{slug}` pages for themes with upcoming releases; panel hidden cleanly for themes without.
- Provisional dates visibly tagged; confirmed dates untagged.
- Methodology page section 8 live.

Calendar-time:
- Daily cron runs successfully for 7 consecutive days without manual intervention.
- Simulated GOV.UK API outage leaves existing rows visible (SWR working).

## Risks and mitigations

- **GOV.UK API changes** — endpoint is public and stable but not contractual. SWR keeps panel showing last-known releases. Log all 4xx/5xx responses for review.
- **Provisional dates displayed as confirmed** — biggest accuracy risk. Mitigated by the investigation step gating the build; if the JSON doesn't expose the distinction cleanly, the feature pauses for redesign rather than ships half-broken.
- **Theme-to-organisation mapping drift** — departmental reshuffles change organisation slugs. Mitigated by storing the mapping in `/docs/` (not hardcoded), and an annual review reminder.
- **Cron timing race with stats refresh** — upcoming releases refresh runs at 05:00, stats refresh at 06:00 Mondays. No shared table, no race condition. If both fail, two independent failure modes to debug.

## Out of scope (for this addendum)

Use 1 (stats refresh timing optimisation) — Phase 3 backlog. Worth doing once stats refresh has been live long enough to have observable wasted-fetch patterns; until then, premature.

Use 3 (predictive Smart Alerts) — Phase 3+ backlog. Smart Alerts as a shipped feature is a separate build; predictive layer sits on top of it. Don't build the layer before the base.

Surfacing upcoming releases on Research Tool result pages or Member Profiles — topic brief pages only in this addendum.

Calendar export (.ics feed of upcoming releases) — possibly useful, definitely Phase 3.

## Dependencies on main Phase 2 brief

- `/brief/{slug}` routes must exist (built in main brief steps 2 and 17).
- Theme taxonomy must be settled (already is — 23 themes locked).
- `/about/statistics` methodology page must exist (built in main brief step 20).

No blocking dependencies — this addendum can run in parallel with the late stages of the main brief, or after main brief ships.
