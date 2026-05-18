# Phase 2 Restructure Brief: Navigation, Hansard Merge, Stats Section, WQs/Tracker Merge

## Scope

Four restructures, shipped together to the Beta environment:

1. Rename and merge: "Hansard Archive" + "Hansard Search" → single nav item "Hansard" with default fast search and a deeper-archive button on results.
2. New top-level "Stats" section: `/stats` index + `/stats/<slug>` per-theme pages, replacing planned headline-stat placement on `/brief/<slug>`.
3. Merge "Tracker" into "Written Questions": two tabs at the top of `/written-questions` — "Search" (default) and "Today's PQs". Tracker URL `/tracker` 301s to `/written-questions/today`.
4. `/brief/<slug>` page cleanup: remove the upcoming-releases panel; add a one-line stats teaser linking to `/stats/<slug>`.

**Environment scope: Beta only.** All four restructures deploy to the Beta environment first. Promotion to live `westminsterbrief.co.uk` follows separately, gated on Beta validation. Live URLs are not changed by this build.

## Final navigation

Beta nav becomes six items:

`Hansard | Written Questions | Research | Stats | Members | Directory`

Stats sits between Research and Members. Tracker and Archive are removed as standalone items (Archive was already redundant post `/brief/` migration; Tracker is now a tab under WQs).

## Restructure 1: Hansard merge

### Renames

- "Hansard Archive" → renamed and merged into "Hansard".
- "Hansard Search" → merged into "Hansard".
- Nav item: "Hansard" (single word, no qualifier).

### Implementation (Implementation B — fast default, escalation button)

URL: `/hansard` for search input; `/hansard?q=<query>` or `/hansard/search?q=<query>` for results — Code picks whichever fits existing routing conventions, but lock the pattern before building.

Default search:
- Hits the existing DB-backed fast search (formerly "Hansard Archive").
- Returns results fast.
- Results page shows results plus, below them, an escalation block:

```
Looking for older debates? Search the full Hansard archive →
```

Clicking the escalation link triggers the deeper search (formerly "Hansard Search"). Renders as a separate action — new results section appended below, or new page. Code's call based on existing patterns; whichever feels less surprising for users.

### Existing URLs

- Pre-existing Hansard Archive URLs and Hansard Search URLs need 301s consolidating onto the new `/hansard` pattern, **but only when this ships to live**. On Beta, the existing URLs may resolve to their existing locations alongside the new `/hansard` route — Code to confirm Beta routing strategy with Mark before implementing.

### Wording check

The escalation copy ("Search the full Hansard archive") is the only place "archive" survives. It correctly applies to the slower comprehensive search — accurate to what it does.

### Out of scope for this restructure

- Merging the two underlying search backends. They remain separate; only the user-facing presentation merges.
- Result-set merging or de-duplication between fast and deep searches. Each search renders its own result set, clearly delineated.

## Restructure 2: Stats section

### URLs

- `/stats` — index page listing all 23 themes with current headline figure for each.
- `/stats/<slug>` — per-theme page. Slug taxonomy mirrors `/brief/<slug>` exactly (`/stats/education`, `/stats/health-and-social-care`, etc.).
- `/about/statistics` — unchanged methodology page, linked from every stats card.

Slug parity between `/stats/<slug>` and `/brief/<slug>` is non-negotiable. Both routes pull slugs from the same single source of truth (existing theme taxonomy list). No divergence.

### `/stats/<slug>` page structure (top to bottom)

1. Page heading — theme name (e.g. "Education statistics").
2. Headline stat card — figure, plain-English summary, source wording, source attribution, "report an issue" link, AI-generated disclosure. Per the main Phase 2 brief's per-card layout (no changes from the original spec — only the mount point changes).
3. Upcoming releases panel — moved from `/brief/<slug>`. Component unchanged; only its location moves.
4. Small line: "How we source and rewrite statistics — see methodology →" linking to `/about/statistics`.
5. Small line at bottom: "See parliamentary activity on this topic →" linking to `/brief/<slug>`.

### `/stats` index page structure

Heading: "Cross-government statistics".

Body: grid or table listing all 23 themes. Per row/cell:
- Theme name (linked to `/stats/<slug>`)
- Current headline figure (e.g. "Unemployment rate: 4.9%")
- Source attribution (e.g. "ONS, March 2026")
- "Provisional" or "may be outdated" tags where applicable

For themes with no headline stat available (the ~18 govuk_bulletin themes that may have `# NO SOURCE:` entries): show theme name and link, with "No headline figure currently tracked" as the entry. Don't omit themes — completeness signals coverage.

Mark's call on grid vs table layout; design constraint is FT/gov.uk reference aesthetic, content-first.

### Build implications for the main Phase 2 brief

The headline stat display component (originally scoped for `/brief/<slug>` in main brief steps 16–17) targets `/stats/<slug>` instead. **Pause the planned headline-stat work on `/brief/<slug>`. Do not delete dormant code; leave it in place until `/stats/<slug>` is stable.**

## Restructure 3: WQs/Tracker merge

### URLs

- `/written-questions` — default landing page. "Search" tab active by default.
- `/written-questions/today` — "Today's PQs" tab.
- `/tracker` — 301 redirect to `/written-questions/today`.

Existing `/tracker` inbound links continue to resolve. The 301 preserves any link equity.

### Page structure

`/written-questions` has two tabs at the top of the page, equally prominent:

```
[Search] [Today's PQs]
```

"Search" is the default tab; the URL `/written-questions` renders Search content. Clicking "Today's PQs" navigates to `/written-questions/today`.

Both tabs share the same page chrome (heading "Written Questions", same nav, same footer). Only the body changes between tabs.

Tab styling consistent with the rest of the site — no special treatment.

### Asymmetry with Hansard noted

Hansard uses a fast-default + escalation-button pattern (sequential). WQs uses two parallel tabs (parallel choice). This asymmetry reflects the genuinely different relationship between the two surfaces within each section. Mark has flagged this for review later; for now, ship asymmetric and revisit in Beta if it feels wrong.

## Restructure 4: `/brief/<slug>` page cleanup

### Removed

- Upcoming releases panel (now lives on `/stats/<slug>`).

### Added

One-line stats teaser, positioned above the session list:

```
Next official release: [release title] ([organisation], [date]) → See all [theme] statistics
```

Linked to `/stats/<slug>`. If no upcoming release is available for the theme: hide the teaser entirely. Do not show "No upcoming releases" — keep the brief page focused on debate browsing.

No icon, no card styling. Plain text, single line, one link. Consistent with the FT/gov.uk reference aesthetic.

### Not added

- Headline stat card. That work moves to `/stats/<slug>` (see Restructure 2).

## Build order

1. **Decision lock** — Mark confirms Beta routing strategy for existing Hansard URLs (do they keep resolving on Beta alongside `/hansard`, or do redirects ship to Beta too?).
2. Rename and route work for Hansard: `/hansard` route + results template + escalation block. Existing Archive/Search routes remain functional on Beta.
3. Build `/stats` index route + template.
4. Build `/stats/<slug>` route + template. Mount the existing headline stat card component (currently dormant for `/brief/<slug>`) and the existing upcoming releases panel component onto this route.
5. Remove upcoming releases panel from `/brief/<slug>` template.
6. Add one-line stats teaser to `/brief/<slug>` template. Hide if no upcoming release exists.
7. Build `/written-questions` two-tab layout. `/written-questions/today` route.
8. Add `/tracker` → `/written-questions/today` 301 redirect.
9. Update Beta nav: remove Archive and Tracker; add Stats. Final order: Hansard | Written Questions | Research | Stats | Members | Directory.
10. Update Beta sitemap.xml to include `/stats`, `/stats/<slug>`, `/written-questions/today`, `/hansard` URLs.

## Acceptance criteria

Build-time (all on Beta):

- `/hansard` renders search input; running a search returns fast DB-backed results; escalation link visible below results and triggers deeper Hansard search when clicked.
- `/stats` index renders, lists all 23 themes with headline figures or "no headline figure currently tracked" placeholder, all linked to `/stats/<slug>`.
- `/stats/<slug>` renders for all 23 themes with headline stat card + upcoming releases panel + methodology link + cross-link to `/brief/<slug>`.
- Slug parity between `/stats/<slug>` and `/brief/<slug>` verified across all 23 themes.
- `/brief/<slug>` no longer shows upcoming releases panel; shows one-line stats teaser when an upcoming release exists.
- `/written-questions` renders Search tab by default; `/written-questions/today` renders Today's PQs tab.
- `/tracker` returns 301 redirect to `/written-questions/today`.
- Beta nav shows six items in the agreed order.
- Beta sitemap.xml includes new routes.

## Out of scope

- Promotion to live `westminsterbrief.co.uk`. Separate release, gated on Beta validation. Mark's call on timing.
- Underlying backend merge of Hansard Archive and Hansard Search search engines.
- Result-set merging or de-duplication between fast and deep Hansard searches.
- Headline stat display work on `/brief/<slug>` — paused; do not delete dormant code, leave in place.
- Member Profile integration of stats. Phase 3+.
- Stats downloads, sub-stats pages, per-stat permalinks. Phase 3+.
- Symmetry refactor between Hansard (button) and WQs (tabs). Revisit in Beta if it feels wrong.

## Risks and mitigations

- **User confusion at default fast Hansard search returning "incomplete" results compared to old Search behaviour** — escalation block must be unmissable. Test wording with Mark before shipping.
- **Slug divergence between `/brief/<slug>` and `/stats/<slug>`** — broken teaser links, split indexing. Mitigated by single source of truth for theme slugs across both routes.
- **`/brief/<slug>` feels emptier after the panel removal** — possible. Teaser keeps the stats connection visible; the brief page returns to doing one thing well rather than two half-well. Revisit in Beta if user feedback signals it's too thin.
- **`/tracker` external backlinks may exist** — 301 preserves equity. Audit Search Console for known inbound links and update what's in your control.
- **Beta-only deploy creates drift between Beta and live** — Mark to schedule promotion to live within a defined window (suggest 2–4 weeks of Beta validation) to avoid Beta and live diverging long-term.

## Pre-flight decisions needed from Mark before Code starts

1. **Beta routing for existing Hansard URLs** — do they stay reachable alongside `/hansard` on Beta, or do 301s ship to Beta too? Recommendation: 301s ship to Beta. Closer simulation of the eventual live behaviour; cleaner Beta validation.
2. **`/stats` index layout — grid or table?** Mark's call based on design language.
3. **Confirm one-line teaser exact wording on `/brief/<slug>`.** Suggested copy in this brief is a starting point; revise if it doesn't match site voice.
