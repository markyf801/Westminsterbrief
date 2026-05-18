# Westminster Brief — Theme to GOV.UK Organisation Mapping

**Purpose:** Maps each Westminster Brief theme slug to the GOV.UK organisation slug(s) used to query the upcoming statistics release calendar (`/search/statistics-announcements?organisations[]={slug}`).

**Last reviewed:** 18 May 2026  
**Review trigger:** Departmental reshuffle, machinery of government change, or wrong-slug symptoms (0 results for an active department).

---

## Verification status key

- ✓ **Verified** — slug tested against `/search/statistics-announcements`, confirmed returns results
- ~ **Assumed** — derived from GOV.UK URL pattern, not live-tested
- ? **Needs check** — uncertain mapping, requires manual verification before use

---

## Mapping table

| Theme slug | Primary org(s) | Slug(s) | Status |
|---|---|---|---|
| `economy` | HM Treasury | `hm-treasury` | ~ |
| `employment-and-labour-market` | Dept for Work & Pensions | `department-for-work-pensions` | ~ |
| `finance-and-taxation` | HMRC | `hm-revenue-customs` | ~ |
| `government-and-public-administration` | Cabinet Office | `cabinet-office` | ~ |
| `business-and-industry` | Dept for Business & Trade | `department-for-business-and-trade` | ~ |
| `education` | Dept for Education | `department-for-education` | ✓ |
| `health-and-social-care` | Dept of Health & Social Care | `department-of-health-and-social-care` | ✓ |
| `housing-and-planning` | MHCLG | `ministry-of-housing-communities-local-government` | ✓ |
| `transport` | Dept for Transport | `department-for-transport` | ~ |
| `crime-justice-and-law` | Home Office, MoJ | `home-office`, `ministry-of-justice` | ~ |
| `welfare-and-social-security` | Dept for Work & Pensions | `department-for-work-pensions` | ~ |
| `immigration-and-asylum` | Home Office | `home-office` | ~ |
| `environment-and-climate-change` | Defra | `department-for-environment-food-rural-affairs` | ~ |
| `defence-and-national-security` | Ministry of Defence | `ministry-of-defence` | ~ |
| `international-affairs` | FCDO | `foreign-commonwealth-development-office` | ~ |
| `science-technology-and-innovation` | DSIT | `department-for-science-innovation-and-technology` | ~ |
| `energy-and-utilities` | DESNZ | `department-for-energy-security-and-net-zero` | ~ |
| `work-and-pensions` | Dept for Work & Pensions | `department-for-work-pensions` | ~ |
| `agriculture-environment-and-rural-affairs` | Defra | `department-for-environment-food-rural-affairs` | ~ |
| `culture-media-and-sport` | DCMS | `department-for-culture-media-and-sport` | ~ |
| `constitutional-affairs` | Cabinet Office | `cabinet-office` | ~ |
| `foreign-affairs` | FCDO | `foreign-commonwealth-development-office` | ~ |
| `parliamentary-affairs` | — | *(no org — Parliament's own statistics are not on GOV.UK release calendar)* | ? |

---

## Notes

### Why ONS is excluded from this mapping

ONS publishes statistics that cut across almost every economic and social theme. Adding `office-for-national-statistics` to economy, employment, finance, housing, crime, etc. would flood every brief page with ONS announcements, most of which are irrelevant to the specific theme. ONS timeseries data is already surfaced via the headline stats feature. ONS upcoming releases are deliberately excluded here.

### Themes sharing an organisation

Several themes share a primary organisation. This is handled correctly by the data model: `UNIQUE (govuk_content_id, theme_slug)` means one release can appear on multiple brief pages if both its themes are mapped to the same org. The refresh job processes each theme slug independently.

- `welfare-and-social-security` and `work-and-pensions` both map to DWP
- `environment-and-climate-change` and `agriculture-environment-and-rural-affairs` both map to Defra
- `international-affairs` and `foreign-affairs` both map to FCDO
- `government-and-public-administration` and `constitutional-affairs` both map to Cabinet Office
- `crime-justice-and-law` maps to two orgs — query each separately and deduplicate by `content_id`

### Multi-org themes

For `crime-justice-and-law`, query both `home-office` and `ministry-of-justice`, then deduplicate results by `content_id` before inserting. The UNIQUE constraint on `(govuk_content_id, theme_slug)` prevents duplication at the DB level anyway.

### MHCLG slug gotcha

MHCLG's slug drops "and": `ministry-of-housing-communities-local-government` — NOT `ministry-of-housing-communities-and-local-government`. Verified live. Using the "and" version silently returns 0 results.

### Parliamentary affairs

Parliament's own statistical publications (e.g. member statistics, voting records, sittings data) are not listed on the GOV.UK release calendar — they are published directly by Parliament at `parliament.uk`. This theme has no useful mapping and should be skipped in the refresh job.

---

## Verifying a slug

To test any slug not marked ✓:

```
https://www.gov.uk/search/statistics-announcements?organisations[]={slug}&release_date_after=2026-05-01
```

Expected: results list. If "0 results", the slug is wrong — check at:

```
https://www.gov.uk/government/organisations/{slug}
```

If the page loads, the slug is valid; check whether the org publishes statistics. If 404, the slug is wrong.
