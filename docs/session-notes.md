# Westminster Brief — Session Notes

Running log of what was built, what decisions were made, and what was deferred.
Most recent session first.

---

## Session: 5 May 2026 — Phase 2A completion + post-deploy polish

**Status at close:** Phase 2A complete and share-ready. Civil service approval pending (~2 weeks out).

### What was built

- **PQ Archive** — 90,280 Written Questions ingested into Postgres (`ha_pq`). Ingestor, cron scripts (pq-morning, pq-afternoon, pq-monday), theme tagging, FTS index, and detail pages all live.
- **`/archive/pq/<uin>`** — PQ detail pages rendering correctly in production.
- **Unified archive search** — `/archive/search` returns both Hansard sessions and PQ results.
- **PQ sitemap** — all `ha_pq` UINs included in sitemap.xml.
- **Form layout fixes** — `/questions` and `/tracker` form rows compacted to match Hansard Archive style (`align-items: flex-end` + plain `<div>` for button containers).
- **Select2 font fix** — biography and mp_search dropdowns now inherit Inter font.
- **Theme page 404 fix** — removed HAVING COUNT threshold from archive theme lookup; thin-tag pages now render friendly empty-state instead of 404.
- **Homepage grid update** — added Hansard Archive [BETA] card; renamed "Hansard Search" to "Live Hansard Search"; removed "Free during beta" tag and civil servant credibility line.
- **"Unknown" name fix** — `/questions` results now correctly fall back to Members API name when `asking_member` is null in DB.
- **is_holding / is_withdrawn** — bulk WQ API endpoint omits these fields; confirmed via verification SQL (both 0 after full backfill). Filters hidden from `/questions` UI pending full historical backfill via individual endpoint. Incremental cron now calls individual endpoint for all answered rows in the 7-day window. Full historical backfill deferred to Phase 2A.5+ (needs `parliament_id` column, ~12h individual-endpoint run).
- **`--skip-answer-fetch` flag** — added to `ingest_pq_cron.py` to bypass per-row individual endpoint calls; cut backfill from ~25 hours to ~92 minutes.
- **History of Hansard page** — built at `/history-of-hansard`; Article schema, footer link, internal link to `/archive`.
- **Content roadmap doc** (`docs/content-roadmap.md`) — created; History of Hansard draft content saved; Acts of Parliament on vellum entry added to Curiosities backlog.

### Key decisions made

- **is_holding / is_withdrawn hidden until data is correct.** Parliament's bulk WQ API endpoint doesn't return these fields. Showing 0/0 would mislead users. Option B: hide the UI filters, fix the data path incrementally, restore when data is verified non-zero.
- **History of Hansard** — footer-linked, not header nav. Reference content, not a primary nav item. Route `/history-of-hansard` (not `/about/history-of-hansard`).
- **Civil servant credibility line removed from homepage.** Site serves a broader audience; the line was too narrowly civil-service-framed.
- **"Free during beta" removed from hero.** No longer accurate framing for a soft-launched tool.

### Phase 2A.5+ backlog (18 candidates)

Captured in `docs/phase2a-hansard-archive.md`. Top priorities on return:
1. Full is_holding / is_withdrawn backfill (needs `parliament_id` column, weekend run)
2. Bill Journey display via Bills API (`billId`)
3. MP slug URLs (`/archive/mp/keir-starmer`) with 301 redirect
4. Member Research contributions tab dept filter (policy area mapping)
5. Parliamentary Curiosities page (Mark to draft entries)

### Infrastructure state at close

- Railway cron services: `archive-cron-morning`, `archive-cron-daytime-mth`, `archive-cron-daytime-fri`, plus 3 new PQ crons (`pq-cron-morning`, `pq-cron-afternoon`, `pq-cron-monday`)
- Backup cron: `backup-cron-r2` — daily pg_dump to Cloudflare R2
- Sitemap submitted to Google Search Console
- Tagging quality: 90.3% of sessions tagged (validated)
- `noindex, nofollow` removed — site is publicly indexed

### Content state at close

- `docs/content-roadmap.md` — editorial content doc created; History of Hansard live; Acts of Parliament on vellum drafted for Curiosities page
- History of Hansard page live at `/history-of-hansard`

---

## Session: 5 May 2026 (late evening) — Phase 2B strategic decisions

Two decisions captured and committed to `docs/phase2-stakeholder-briefing-pack.md`.

**Decision 1: Phase 2B reframed as alert subscription product.**
On-trigger email alerts to subscribers on chosen topics (GOV.UK policy
area taxonomy + free-text keywords). Replaces the £49 one-off briefing
pack as Phase 2B. Briefing pack moves to Phase 2C candidate or possibly
dropped pending real-use signal from Phase 2B.

**Decision 2: Two-tier pricing.**
- £5/month starter — single topic, weekly digest, low-friction onramp
- £15/month main — multiple topics (suggest 5), triggered alerts,
  priority flagging, richer monthly summary

Rationale for £15 anchor (not £3-5 flat): value-vs-price mismatch at
low end, margin trap, and starvation pricing selects for price-sensitive
customers. Anchoring at £15 preserves room for later discount tiers
(charity rate, civil service rate) without the reverse problem.

Pricing open to revision from real-use feedback. Strategic commitment
is "tiered with starter wedge," not "£5/£15 specifically."

**Build gate:** not before 2-4 weeks of Phase 2A real-user signal
post-share.

---

## Session: 1 May 2026 — Phase 2A soft-launch framing + cron architecture

**Key decisions:**
- Soft launch framing locked: iterate publicly after sharing rather than gating to a launch day.
- Civil service share is still time-fixed to the 32-day approval window — "soft launch" doesn't change the one-shot share moment.
- Cron schedule: hourly during sittings (not daily-overnight as spec originally said).
- Must-haves for share: search, theme tagging quality, cron services, SEO foundation, no obvious UX issues.
- Bill Journey, advanced filters, AI summaries deferred to Phase 2A.5+.

---
