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
