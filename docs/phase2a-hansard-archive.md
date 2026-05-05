# Phase 2A — Hansard Archive

Strategic decisions and locked framing for the Hansard Archive build.
Implementation detail lives in the plan file and in `CLAUDE.md`.

---

## Soft-launch framing (locked 1 May 2026 evening)

Phase 2A is reframed from "discrete launch event" to "soft launch with
ongoing iteration." The tool is publicly accessible at westminsterbrief.co.uk;
features are added as they're built rather than gated to a launch day.

Implications:

1. **Civil service share is still a one-shot moment.** The 32-day approval
   window remains time-fixed; the DD demo and 300-colleague Teams share
   happen when approval lands, regardless of feature state. "Soft launch"
   doesn't mean "share when ready" — it means "iterate after sharing."

2. **Must-haves for share are narrower than feature-complete.** What needs
   to be in place: search working, theme tagging quality acceptable, cron
   services keeping archive current, SEO foundation, page templates
   rendering, no obvious UX issues. Bill Journey, advanced filters, AI
   summaries, etc. are all post-share additions.

3. **Visible gaps are part of the story.** Where features are missing
   (e.g. Bill Journey via Bills API), the tool acknowledges and links to
   authoritative sources rather than hiding the gap. Iteration becomes
   a public narrative of the tool improving.

4. **A "Recent additions" feed on the archive home** auto-generates from
   cron data, signalling freshness without editorial commitment. A heavier
   "What's new" changelog is deferred — decide post-share whether the
   public-building narrative is worth the ongoing time cost.

Revised priority order:
1. Cron services (load-bearing for "feels alive")
2. Bill page polish (sort fix + explanatory line)
3. Theme tagging quality validation
4. Sitemap submission to Search Console
5. DD demo + civil service share when approval lands
6. Phase 2A.5 starts post-share with Bill Journey via Bills API as first build

---

## Linkage mechanism — "Other Stages of This Debate" (confirmed 1 May 2026)

Current implementation: **title-matching only**, no bill_id.

`_related_sessions()` in `hansard_archive/views.py` normalises session titles
(`_normalise_title()` strips "Draft " prefix and "[Lords]" suffix, lowercases),
then does a 365-day window search with an exact normalised-title match.

Works well for the current corpus because Hansard titles for Bill stages are
highly consistent. Known failure modes at scale:

- Titled amendments: "Finance Bill (Ways and Means)" won't match "Finance Bill"
- Prefix ilike cuts at 20 chars — could over-match short bill titles with common prefixes
- Identical-title collision (two bills with similar short names in same window)

**For Phase 2A.5 bill journey display:** build from the Bills API
(`bills.parliament.uk`), not from this function. Bills API gives stable
`billId` and the full stage history as structured data. Title-match panel
is appropriate for navigation; Bills API is required for the progression
timeline (Commons First Reading → ... → Royal Assent).

---

## Phase 2A.5 candidates (post-share, not actioned)

- **Bill Journey display** — Commons First Reading → Commons Second Reading →
  ... → Royal Assent timeline on bill-type session detail pages. Sourced from
  Bills API (`billId`). Factual per Phase 2A architectural rule; appropriate
  Phase 2A.5 scope. First build after civil service share lands.

- **Recent additions feed** — see soft-launch framing above (item 4).

- **MP slug URLs** — Currently MP archive pages use integer member IDs (`/archive/mp/4053`).
  Works correctly and sitemap is consistent, but loses the SEO benefit of keyword-in-URL
  for queries like "Keir Starmer parliament debates". Build: slug-based MP URLs
  (`/archive/mp/keir-starmer`) with 301 redirect from integer to slug as canonical.
  Slug generation logic, route registration, internal link updates, sitemap update,
  redirect handling. Estimated effort: 0.5–1 day.

- **Member Profile / Member Research caching** — `/biography` and `/mp_search` currently
  query Parliament's Members API live on each request. Pre-populating into local DB would
  improve speed and resilience. Three tiers by data stability:
  - Stable (name, party, career history, APPG roles, election results) → weekly refresh
  - Semi-current (registered interests, voting record) → 24–48h refresh
  - Real-time (today's votes) → query live, no cache
  Note: the `/mp_search` Speeches tab already reads from local `ha_contribution` as of
  5 May 2026; this item covers the remaining Members API calls (profile, bio, interests).
  Estimated effort: 3–5 days (schema, ingestor, refresh cron, tool refactors).
  Trigger: post-Phase 2 share, after launch usage data shows speed is a real bottleneck.

- **Wikipedia description caching** — `/biography` fetches Wikipedia summaries live.
  Same pre-population pattern as Members caching. Three safeguards required:
  1. Daily refresh to limit vandalism exposure to <24h windows
  2. Diff monitoring on refresh — flag unusual changes for review
  3. Evaluate whether Parliament's official bio data covers the case sufficiently
     to skip Wikipedia entirely before building
  Attribution requirements: "Source: Wikipedia, accessed [date]", link back to source
  article, CC-BY-SA licence compliance.
  Estimated effort: 2–3 days plus ongoing operational care.
  Trigger: after Members caching is in place, if Wikipedia rendering still adds enough
  value to justify the operational complexity.

- **History of Hansard page** — Static long-form page at `/history-of-hansard`. ~1000-word
  draft exists (see `docs/content-roadmap.md`). Covers pre-Hansard secrecy, Cobbett's
  radical foundation, the Hansard family era, Stockdale v Hansard and 1840 Act, the 1909
  switch to official record, modern Hansard. SEO title, meta description, Article schema,
  internal links to `/archive`. Content is drafted and ready to build. Estimated effort:
  1–2 hours once content is pasted into the spec.
  Trigger: quiet content session; any "SEO content" conversation; post-share when polishing
  the site for a wider audience.

- **Parliamentary Curiosities page** — Single long-form page organised by theme (Ceremony,
  Procedure, Language, Origins). v1 with 8–10 entries, growing over time. Candidates listed
  in `docs/content-roadmap.md`. Builds Westminster Brief's character as a site that
  genuinely understands Parliament rather than just scraping its data. Strong SEO potential
  for procedural/lexical queries. Mark to draft initial entries when there's quiet time.
  Estimated effort: 1–2 days for v1.
  Trigger: content draft exists in content-roadmap.md; any "site character" or "SEO content"
  conversation; post-share when capacity allows.

- **Member Research — contributions tab ignores department filter** — On `/mp_search`,
  the "Filter PQs by department" dropdown correctly filters the Written Questions tab but
  has no effect on the Contributions tab (which always shows the 50 most recent sessions
  regardless). The label is technically accurate ("Filter PQs by...") but looks inconsistent
  at a glance. Fix path: build a department→policy-area mapping (e.g. "Department for
  Education" → "Education and skills"), then filter `ha_contribution` results by matching
  `ha_session_theme.theme` when a department is selected. Uses the Phase 2A policy_area
  tagging infrastructure — no new data work needed. Estimated effort: 1–2 days.

- **Holding/withdrawn filter backfill** — `is_holding` and `is_withdrawn` columns exist in
  `ha_pq` and the ingestor now correctly populates them via the individual endpoint for all
  answered rows on incremental cron runs (from 5 May 2026 onward). Historical rows (90k)
  remain defaulted to false because the bulk WQ API endpoint omits these fields.
  The holding/withdrawn options have been removed from the `/questions` status filter UI
  pending this backfill. To restore them:
  1. Run the individual-endpoint backfill over a quiet weekend (~12 hours): iterate all
     `ha_pq` rows where `is_answered=true`, call `/questions/{api_id}` for each, write
     `is_holding` and `is_withdrawn`. Needs the `api_id` stored — currently not persisted
     (it's a transient `_api_id` field). Schema addition required: `ha_pq.parliament_id INT`.
  2. Once historical data populated and verified non-zero, re-add the filter options to
     `/questions` template.
  Estimated effort: 0.5 day schema + 1 day backfill script + 12h run + 0.5h UI restore.
