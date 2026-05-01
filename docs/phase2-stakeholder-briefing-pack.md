CopyPhase 2 Scoping — Stakeholder Briefing Pack
Captured: 29 April 2026 (significantly updated later same day)
This document captures the scoping decisions made during focused sessions on Phase 2 of Westminster Brief. It supplements the locked decisions already in Mark's Westminster Brief planning and is intended to drive the Phase 2 build.
Important: strategic pivot locked 29 April 2026 (afternoon)
After thorough scoping of the £49 briefing pack product (captured below), a go-to-market conversation surfaced a meaningful strategic shift. The decision is now to build a free public Hansard archive first, then the £49 briefing pack second, rather than the briefing pack first.
The pivot in one paragraph
Phase 2A is now a free public Hansard intelligence archive (Hansard data ingestion + AI theme tagging + searchable, indexable public pages). Phase 2B is the £49 briefing pack as previously scoped, built on top of the data infrastructure created in Phase 2A. Phase 2C remains the subscription intelligence tier, deferred until after the briefing pack launches.
Why the pivot
Five reasons, in rough order of weight:
1. Mark has a known launch audience for a free product. A 300-colleague Teams group of HE policy civil servants exists. Mark has shared an earlier iteration before (Python Anywhere version) with his DD's encouragement. The 300-colleague launch is a real thing he can do, not a hypothetical. This is a meaningful competitive advantage most solo developers lack.
2. Civil service system approval is in progress and time-fixed. westminsterbrief.co.uk is going through the standard 32-day approval to be accessible from civil service systems. It will land in approximately 4 weeks. This creates a hard launch window that the free archive can hit; the briefing pack cannot.
3. The propriety landscape is genuinely cleaner for free first. "I built a useful free tool" is unproblematic and consistent with how Mark has shared things previously. Sharing a paid product with civil service colleagues is much more sensitive even if technically permissible. Building the free product first establishes Mark as someone who builds useful things, not someone monetising public data; the paid product later sits on top of established trust.
4. SEO compounds during the time the paid product is being built. If Phase 2A launches as a free archive in 4 weeks, by the time Phase 2B (the briefing pack) launches another 4-5 weeks later, the site has potentially had 8-9 weeks of SEO compounding. Search engines index parliamentary intelligence pages well — TheyWorkForYou pages routinely rank in top results. The free archive becomes top-of-funnel for the eventually-launched paid product.
5. Audience validation precedes monetisation validation, in this order. Mark has explicitly said audience matters more than first paying customer at this stage. Knowing colleagues use the tool is more valuable than knowing one stranger paid £49 once. The free launch validates audience; the paid product later validates willingness-to-pay among that audience.
Phasing under the pivot
Phase 2A — Free public Hansard archive (next ~4 weeks)
Goal: launch a free, useful, SEO-indexable Hansard intelligence layer in time for civil service system approval and the 300-colleague share.
Scope locked at: Hansard data ingestion + AI theme tagging at launch. Conservative deliberately — better to launch a polished smaller thing than a sprawling unfinished thing.
Out of Phase 2A scope (deferred to Phase 2A.5 post-launch): "did the minister answer" classification on debates, AI-generated weekly digests, email alerts, advanced filtering, the briefing pack itself.
Detailed scope and build sequence in the "Phase 2A: Free archive build plan" section below.
Phase 2B — £49 briefing pack (~4-5 weeks after Phase 2A launches)
Goal: launch the comprehensive on-demand briefing pack product as scoped in detail throughout the rest of this document.
Scope: as previously locked (the entire "Locked product spec for v1" section below remains accurate for Phase 2B).
The briefing pack now sits on top of the data infrastructure built in Phase 2A — Hansard ingestion, theme tagging, structured Q&A. This means the briefing pack build benefits from work already done rather than starting from scratch.
Phase 2C — Subscription intelligence tier (revisit 6 months after Phase 2B launches)
Same as previously decided. Triggers for bringing this forward, decision criteria, and reasoning all unchanged from the existing "Relationship to the Hansard intelligence strategy" section below.
Launch sequence for Phase 2A
A specific operational sequence to manage relationships well:

Build progresses while civil service approval clock runs in parallel — these are simultaneous, not sequential
Civil service approval lands (~4 weeks from 29 April 2026)
Casual demo with DD before wider sharing — courtesy of letting her see it before her colleagues do
HE Teams group share (~300 civil servants) once DD has seen it
Parly colleagues share alongside or shortly after
Wider organic spread — civil service Twitter, adjacent networks, search engines indexing the archive

Important: do NOT share with the HE Teams group before the DD has seen it. Sequencing matters; she should be a prepared advocate, not a surprised observer.
What this means for the rest of this document
The bulk of this document (everything below this section) describes the £49 briefing pack product in detail. All of that content remains accurate and locked for Phase 2B. The pivot doesn't change what the briefing pack is; it changes when it launches and what comes before it.
So when reading this document going forward:

Sections describing the briefing pack apply to Phase 2B
Sections describing data sources, propriety exclusions, structural principles, position evolution, data accuracy safeguards, media analysis, etc. all apply to Phase 2B
The Phase 2A build plan is captured in its own section below
The Hansard intelligence strategy section (originally framing a parallel product) is now even more closely related — Phase 2A is essentially the first concrete step toward what that strategy described

Phase 2A: Free archive build plan
Detailed plan for the next ~4 weeks of focused build, ending with civil service launch.

## Soft-launch framing (locked 1 May 2026 evening)

Phase 2A is reframed from "discrete launch event" to "soft launch with
ongoing iteration." The tool is publicly accessible at westminsterbrief.co.uk;
features are added as they're built rather than gated to a launch day.

Implications:

1. Civil service share is still a one-shot moment. The 32-day approval
   window remains time-fixed; the DD demo and 300-colleague Teams share
   happen when approval lands, regardless of feature state. "Soft launch"
   doesn't mean "share when ready" — it means "iterate after sharing."

2. Must-haves for share are narrower than feature-complete. What needs
   to be in place: search working, theme tagging quality acceptable, cron
   services keeping archive current, SEO foundation, page templates
   rendering, no obvious UX issues. Bill Journey, advanced filters, AI
   summaries, etc. are all post-share additions.

3. Visible gaps are part of the story. Where features are missing
   (e.g. Bill Journey via Bills API), the tool acknowledges and links to
   authoritative sources rather than hiding the gap. Iteration becomes
   a public narrative of the tool improving.

4. A "Recent additions" feed on the archive home auto-generates from
   cron data, signalling freshness without editorial commitment. A heavier
   "What's new" changelog is deferred — decide post-share whether the
   public-building narrative is worth the ongoing time cost.

Revised priority order:
1. Cron services (load-bearing for "feels alive")
2. Bill page polish (sort fix + explanatory line)
3. Theme tagging quality validation
4. Recent additions feed on archive home
5. Sitemap submission to Search Console
6. DD demo + civil service share when approval lands
7. Phase 2A.5 starts post-share with Bill Journey via Bills API as first build

### Bill Journey display — Phase 2A.5 candidate (deferred from Phase 2A)

Bill stage progression display ("Commons First Reading → Commons Second
Reading → Royal Assent") is appropriate Phase 2A scope per the architectural
distinction (bill status is factual, not analytical). Build was not
started before the Phase 2A scope was reframed to soft-launch. Bill-related
sessions in v1 are linked via title-matching, which works for current
Hansard data but doesn't surface stage labels or progression structure.

Phase 2A v1 mitigation: bill-type session pages show "Other Stages of This
Debate" via title-matching, with an explanatory line linking to
bills.parliament.uk for full bill progression.

Phase 2A.5 build (first post-share build):

- Integrate Parliament Bills API (bills.parliament.uk)
- Add bill_id and stage_type columns to ha_session schema
- Backfill existing bill-related sessions with bill_id by matching against
  the Bills API
- Update bill-type session pages with full journey display: chronological
  stage progression, contribution counts per stage, current stage indicator,
  pending stages flagged
- Replace title-match linkage with bill_id linkage on related-sessions
  display

Estimated effort: 5-7 days.

Phase 2A vs Phase 2B — the governing principle (locked 30 April 2026)
The line between Phase 2A (free archive) and Phase 2B (paid briefing pack) is not which data sources are used. It is what the feature does with the data.

Phase 2A: factual context — states what is publicly known, unchanged from the source. Examples: "currently at Lords Committee Stage, next sitting 12 May"; "this bill was introduced on X, received Royal Assent on Y"; "27 April 2026 — Lords Oral Questions". No synthesis, no interpretation, no inference about intent or direction.

Phase 2B: analytical synthesis — interprets patterns, traces evolution, produces authored narrative. Examples: "government position has shifted on Clauses 14-17"; "lexical drift: 'shortly' → 'in due course' signals internal delay"; "parliamentary activity on this topic has increased markedly since the budget". This is what Claude Opus earns its compute cost for.

This means any data source — Bills API, Hansard, GOV.UK content API, ONS — can supply Phase 2A features, as long as the feature states facts rather than synthesises them. Phase 2B uses the same sources but adds analysis on top.

This distinction also matters for the free toolkit rule in CLAUDE.md: free features (Phase 2A) retrieve and display factual data; they do not author interpretation. Both must hold.

Locked scope for Phase 2A
In scope:

Hansard rolling ingestion pipeline
Database schema for sessions → questions → analysis (per the 27 April Hansard strategy data model)
Backfill of last 3-6 months of debates (deeper backfill running in background after launch)
AI theme tagging using Gemini Flash-Lite (per the 27 April strategy economics — under £1 for full backfill)
Public-facing browsing pages: search, by MP, by department, by theme
SEO basics: proper title tags, structured data (JSON-LD), sitemap.xml, robots.txt, semantic HTML
Performance — pages must load fast enough for civil service systems
Browser compatibility — Edge and Chrome at minimum (likely civil service browsers)
Existing six tools continue to work (no regression)
Bills API factual context — map sessions to bills they discuss (title matching), display current bill status on session detail pages (introduced / current stage / concluded / withdrawn), link to Parliament's official bill page. States facts only; no amendment tracking, vote analysis, or analytical synthesis (those are Phase 2B).

Out of scope (deferred to Phase 2A.5):

"Did the minister answer" directness classification on general debates
AI-generated weekly digests / summaries
Email alerts on themes
Advanced filtering / faceted search
User accounts (the archive is fully public, no login needed)
Subscription tier infrastructure

Build sequence (suggested)
Week 1: Hansard ingestion and storage — COMPLETE (29 April 2026)

What was built:
Chain-walking ingestion pipeline via NextDebateExtId/PreviousDebateExtId links (full BFS traversal — not just the Hansard search index, which misses up to 30% of sessions)
Westminster Hall anchor fix: secondary "Westminster Hall" search guarantees WH chain seeding when Commons Chamber sessions crowd it out of the search results
90-day backfill complete: 1,224 sessions, 39,598 contributions in the database
9-type debate vocabulary locked: oral_questions, pmqs, westminster_hall, debate, ministerial_statement, statutory_instrument, committee_stage, petition, other
hrs_tag-first classifier (full rewrite): location → hrs_tag → title fallback; eliminates silent SI misclassification and PMQs-as-oral_questions bugs from prior heuristic-only approach
is_container flag: 175 structural header sessions flagged across 4 container types (hs_6bDepartment, hs_3MainHdg, hs_3OralAnswers, hs_6bPetitions + null-tag "Westminster Hall" aggregate sessions) and excluded from tagging and public pages. Full container sweep completed 29 April 2026 — see "Container handling" in decision points.
Merged to master.

Week 2: AI theme tagging — COMPLETE (29 April 2026)

What was built:
hansard_archive/tagger.py: two-level theme tagging using Gemini Flash-Lite with JSON schema enum enforcement (hard constraint on policy_area values)
scripts/run_tagging.py: batch runner with --limit, --id, --include-other flags; safe to re-run (already-tagged sessions skipped)
Two-level vocabulary: policy_area (23-term GOV.UK taxonomy, controlled via response schema enum) + specific (1–5 free-text topic phrases per session)
50-session sample run result: 50/50 tagged, 405 theme rows, 0 failures, 0 off-list policy_area values, 0 JSON parse errors

Status: full run COMPLETE (29 April 2026 evening).
902/912 sessions tagged (98.9%) — 10 failures, all sessions with no speech text ("Backbench Business" and "Business without Debate" procedural sessions)
7,397 total theme rows (387 pre-existing + 7,010 added in full run)
0 off-list policy_area values, 0 JSON parse errors

Corpus state as of 29 April 2026 (post-run, 90-day corpus):
1,224 sessions ingested (39,598 contributions)
175 is_container sessions excluded from tagging
960 sessions tagged (7,397 theme rows)
89 debate_type='other' sessions untagged at launch (full-text searchable; Week 3 backlog item to triage substantive ones)

Year backfill ingestion complete (29 April 2026 evening):
365 days checked (2025-04-29 to 2026-04-28), 130 sitting days found
3,190 new sessions ingested, 0 errors
DB total: 4,414 sessions (727 containers, 3,687 non-container)

Backbench Business anchor fix (30 April 2026):
53 zero-content structural anchor sessions patched to is_container=True
ingestion.py updated to detect anchor pattern going forward (see Container handling decision point)
Untagged taggable sessions after patch: 2,356

Year backfill tagging (30 April 2026 — COMPLETE):
Gov+PA diagnostic (spot-check gate): only 4 sessions tagged exclusively with Gov+PA, avg 3.11 policy_area tags per session including it — confirmed working correctly, not a fallback
2,356 eligible sessions; 2,332 tagged (99.0%), 24 failures (no response), 0 errors; 18,172 theme rows added
Full Commons corpus: 3,634 taggable sessions; 3,282 tagged (90.3%); avg 3.04 policy_area tags per tagged session; all 23 policy_area terms in use; no runaway catch-all, no dead terms

Lords pipeline — COMPLETE (30 April 2026):
Decision made: Lords in Phase 2A scope, building now (not deferred to Week 3).
ingestion.py updated: hs_venue container, "lords chamber" and "grand committee" null-tag containers, Grand Committee classifier branch (location → committee_stage), ChildDebates BFS extraction (handles isolated GC sub-chain without secondary anchor search). Also: _PROCEDURAL_TITLE_STARTS override (AoB always → other regardless of venue) and _MADE_SI_RE check placed before "amendment" keyword (fixes Amendment SIs miscategorised as debate).
backfill_hansard.py: --house Lords flag added; ingest_date_range() now accepts house= param.
spec doc: docs/lords-ingestion-spec.md written (container/anchor patterns, taxonomy, GC chain design, time estimates). Updated with post-build findings and documented OQ/MS classification gap.

1-day Lords test (2026-04-29): 6 sessions, 0 errors — passed.
7-day Lords test (2026-04-23 to 2026-04-29): 68 sessions across 4 sitting days, 0 errors. Grand Committee confirmed on 2026-04-27 (25 sessions incl. GC). Classification distribution healthy. Passed.
12-month Lords backfill (COMPLETE): 365 days, 164 sitting days, 2,332 new sessions, 0 errors.
Lords taxonomy survey and reclassification pass (30 April 2026):
  Fix 1: 67 other → statutory_instrument (made SIs correctly classified)
  Fix 2: 75 debate → statutory_instrument (Amendment SIs misclassified due to keyword order — fixed in ingestion.py)
  Fix 3: 74 committee_stage → other (AoB sessions inside Grand Committee)
  Total: 216 sessions reclassified
Lords theme tagging (2 passes, 30 April 2026 — COMPLETE):
  Pass 1: 806/830 tagged (97.1%), 5,366 theme rows
  Pass 2 (post-reclassification): 62/91 tagged, 819 theme rows (29 failures = SIs with no debate text, expected)
  Coverage: 799/799 taggable Lords sessions (100%); 873 sessions with theme tags total

Documented gap (Lords oral questions and ministerial statements):
Not separately classifiable from current Hansard API signals. Lords HRSTag taxonomy is almost entirely NewDebate — no Lords equivalent of hs_8Question or hs_2cStatement. Sessions stored as other, remain full-text searchable and theme-tagged. Phase 2A.5 task. See docs/lords-ingestion-spec.md for full gap documentation.

Week 3: Public-facing pages — UPCOMING

Search/browse interface
MP-level page templates (every Q from this MP, themed)
Department-level page templates
Theme-level page templates (every Q on tuition fees, etc.)
SEO basics applied across all templates

Week 4: Polish and launch prep — UPCOMING

Performance optimisation (page load speed, database queries)
Civil service system browser testing
Bug fixes from real-data trials
Verify existing six tools haven't regressed
DD demo prep

Decision points already made for Phase 2A

Model for theme tagging: Gemini Flash-Lite (cheap, batchable, sufficient quality for tagging). Confirmed correct after 50-session sample run; Gemini 2.5 Flash-Lite auto-detected and used if available.
Ingestion approach: chain-walking via NextDebateExtId/PreviousDebateExtId rather than search-index-only; Westminster Hall anchor search guarantees WH chain seeding.
Data model: sessions → contributions → themes (ha_session, ha_contribution, ha_session_theme tables). Implemented and in production.
Backfill depth: 90 days at launch (1,224 sessions); year backfill complete 29 April 2026 (4,414 sessions total, 130 sitting days, 2025-04-29 to 2026-04-28). Full year corpus theme-tagged 30 April 2026. Same pipeline supports deeper historical backfill — just extend the date range.
Debate type vocabulary: 9 types locked. The hrs_tag field is the authoritative classification signal, not title heuristics. Vocabulary will not change mid-build.
Container handling: is_container boolean flag (not deletion). Excluded from tagging and public pages; raw contribution data preserved. Two structurally distinct patterns both use this flag:

(1) Duplicate-content containers: structural headers whose _flatten_items() recursion captures contributions from all child sessions, producing duplicate counts. Four types: hs_6bDepartment (dept oral questions header), hs_3MainHdg (chain-head header), hs_3OralAnswers (full-day oral answers header), hs_6bPetitions (petitions header). Plus null-tag "Westminster Hall" and "Commons Chamber" aggregate sessions. Comprehensive sweep completed 29 April 2026 — all (hrs_tag, location) combinations verified by contribution-count pattern matching. Non-containers confirmed: hs_2BillTitle, hs_2GenericHdg/hs_2cGenericHdg, null-tag Public Bill Committee sittings.

(2) Zero-content anchors: section-header sessions that announce a parliamentary slot but carry no speech text. The actual debates within the slot are ingested separately under their own titles. Always 0 contributions. "Backbench Business" identified as the confirmed anchor type (53 sessions, 30 April 2026 — patch applied to existing rows; ingestion.py updated via _ANCHOR_TITLES set to detect going forward).
Theme vocabulary: two-level (policy_area controlled 23-term GOV.UK taxonomy + specific free-text 1–5 phrases). policy_area values are hard-constrained via Gemini JSON schema enum; no off-list values possible.
Skip types for initial tagging: debate_type='other' excluded at launch (procedural/mixed sessions). other sessions remain full-text searchable; a Week 3 backlog item will triage substantive ones for reclassification.
No paywall in Phase 2A: everything is free and indexed.
No user accounts in Phase 2A: fully public access.

Phase 2A.5 tasks (post-launch, before Phase 2B)

Lords oral questions classifier — COMPLETE (30 April 2026).
Resolved via constitutionally mandated opening-phrase signal: every Lords oral question begins "To ask His Majesty's Government..." — 100% precision, no false positives possible. 640 sessions reclassified from other/committee_stage/debate to oral_questions via backfill script. The combined classifier (word-count gates, contribution-count gates) considered and rejected: the phrase alone achieves ~100% precision, additional gates only create false negatives. Archive now correctly surfaces Lords OQs when filtering by dtype=oral_questions.

Bills API integration (Phase 2A scope — factual context only).
Map sessions to bills by title matching. Display current stage on session detail pages. Link to Parliament's official bill page. No analytical synthesis in Phase 2A — that is Phase 2B scope. Investigation first: endpoints, rate limits, title-match reliability, effort estimate, update frequency. Then build.

Scoring / ranking for /archive search results.
Approved formula (30 April 2026): 40% text relevance + 35% recency (45-day half-life) + 15% engagement (log-scaled contribution count) + 10% cross-cutting (policy area breadth). Additive. No-query browse stays pure date DESC. Short single-word queries that match as substrings of common words (e.g. "AI" matching "retail", "rail") need word-boundary fix before scoring is applied to them. Implementation pending.

OQ grouping on /archive.
When dtype=oral_questions or dtype=pmqs and no search query, group results by date/department rather than flat list. Commons OQs: group by department-on-date. Lords OQs: group by date only. PMQs: group by date. Implementation in progress (30 April 2026).

---

Decision points still open for Phase 2A

Lords debates at launch — DECIDED (30 April 2026). Lords included in Phase 2A. Pipeline code complete; 1-day Lords test pending. 12-month backfill and container sweep follow test. Week 3 page templates must support both houses (house field on ha_session already present). See docs/lords-ingestion-spec.md for full Lords pipeline spec.
Specific URL structure for theme pages. Slug conventions need locking before Week 3 — Mark has SEO slug conventions for MP/constituency pages but theme slugs are new. Suggest locking alongside the Week 3 page template work.
Search backend: Postgres full-text search (simple, no new dependencies) vs dedicated search service (more capable, more setup). Decide at start of Week 3 as it affects the search page architecture.

Success metrics for Phase 2A
Not revenue. The Phase 2A success criteria:

Adoption signal: are HE colleagues actually using it after the share? (server logs)
Spread signal: does it get shared beyond the original 300? (referrer logs)
SEO signal: is Google indexing the pages? Are any of them ranking? (Search Console)
Quality signal: are users coming back? (return visits)
Validation signal: any unsolicited feedback ("this is useful", "could it also do X")

A reasonable success threshold for Phase 2A's first month: 100+ unique users, indexed pages climbing, at least some unsolicited positive feedback. If those happen, the path to Phase 2B is validated.
Risks for Phase 2A
Risk 1: 4 weeks isn't enough to do this well. Hansard ingestion done properly is genuinely complex. Backfilling 3 months of debates, running AI tagging on each, building public-facing pages, getting SEO right — that's a lot for 4 weeks of solo work alongside a day job.
Mitigation: ruthless scope discipline. The "out of scope" list above is non-negotiable for Phase 2A. If something is taking longer than expected, defer not stretch.
Risk 2: Theme tagging quality is variable. Gemini Flash-Lite is cheap but not infallible. Bad tags would undermine trust in the archive on first use.
Mitigation: validate by hand on a sample of 50-100 questions before launch. If tagging quality is poor, spend prompt-engineering time before exposing to colleagues.
Risk 3: Civil service systems may have unexpected restrictions. A site that works perfectly on consumer browsers might have issues on locked-down civil service systems (browser version, security policies, etc.).
Mitigation: test from a civil service-equivalent environment before approval lands. Mark may be able to test from a colleague's machine, or use a virtual machine simulating the constraints.
Risk 4: DD reaction differs from expectation. Mark has assessed she'll be supportive based on past behaviour. Most likely true, but worth not banking on completely.
Mitigation: have a simpler "internal-only / quiet" fallback share if the conversation goes unexpectedly. Keep the launch flexible until after the DD demo.
Strategic position
Mark has explicitly chosen breadth and substance over speed-to-launch. The product should be genuinely comprehensive at v1 rather than a thin MVP. Reasoning:

Mark has a stable day job and isn't pressured for early revenue
Westminster Brief is sustainable at 24 hrs/week
First impressions matter — a thin product that disappoints loses the user; a substantial one builds reputation
Incumbents (Dods, Vuelio, DeHavilland) are comprehensive and charge thousands; a £49 alternative needs to be credibly comprehensive, not just cheaper

This means a longer build before launch is the right trade-off for this product.
Locked product spec for v1
The following decisions were made during the 29 April 2026 scoping session and are locked in for v1 unless explicitly revisited.
Geographic scope
UK Parliament and UK Government policy only. England-as-default. Devolved administrations (Scotland, Wales, Northern Ireland) are NOT covered in v1.
The methodology page must be explicit: "This pack covers UK Parliament and UK Government policy. Devolved administration policy (Scotland, Wales, Northern Ireland) is not included. For policy areas that are wholly or partly devolved, additional research from devolved sources will be necessary."
Pricing and lifecycle

Single price: £49 one-off, no subscription, no tiers, no free trial
User owns the file indefinitely after purchase
No DRM, no link expiry, no nonsense — pack is fully self-contained
Optional cheap-regeneration flow (e.g. £29 for refreshed pack on same topic) is a future revenue stream possibility, not v1

Branding
Standard tier only at v1. Configuration:

User replaces cover with own logo
"Prepared for [user's organisation]" line on cover
Non-removable per-page footer: "AI-generated analysis. Verify before publishing"
Westminster Brief attribution on back methodology page only

Agency/white-label tier (no WB attribution, full white-label) is deferred to v2 as a higher-priced product (likely £149-249) targeted at consultancies reselling the pack. The data model already supports a branding_tier field for this future expansion — no refactoring needed when added.
User input flow
The input form has three required fields and one optional, in this order:
1. Topic (required, free text)
Open free-text with example pairs showing what good topic-framing looks like:

"Be specific. Examples:
• Apprenticeship funding for level 4 and 5 (good)
• Education (too broad)
• Section 17 of HE&R Act (too narrow)
• AI safety regulation (good)
• Technology (too broad)
• The General Product Safety Regulations 2005 (too narrow)"

2. Pre-flight topic check (optional checkbox, default-on)
Wording: "Run a quick topic review before generating — we'll suggest improvements if needed."
When ticked (which is the default), the system runs a Sonnet check that:

Classifies the topic as well-pitched / too broad / too narrow / not a Westminster policy topic
If well-pitched, confirms and reassures
If problematic, offers 2-4 specific reframings as clickable buttons that auto-fill the topic field
User retains agency to proceed with original wording even if the AI flags it

After one round of refinement (user clicks a suggested reframing), the check auto-unchecks for that session. User can re-check manually if they want.
The check should not be paternalistic — broad topics are sometimes legitimate (think tank doing a sector overview, charity doing a strategy review). Wording should signal trade-offs, not enforce narrowing.
3. Context (optional free text)
Prompt: "What's your role and what are you trying to do with this pack?" Example: "I work for a sector body representing FE colleges; we're preparing a response to the upcoming spending review."
This shapes Claude Opus's synthesis tone and emphasis without changing the data gathered. Without it, synthesis is more generic.
4. Lens picker (required, choose 1 of 3)
Three lenses, presented as use cases rather than user identities:

Engagement-led — "I'm engaging with Parliament on this — show me who's active and where to engage." Foregrounds: MPs, committees, EDMs, consultations, APPGs.
Analysis-led — "I'm publishing or briefing on this — show me the landscape and the narrative." Foregrounds: position evolution, commentary, think tank activity, statistics.
Orientation-led — "I'm new to this and need to understand the landscape." Foregrounds: background, key players, basic structure. Assumes less prior knowledge; sections are slightly more explanatory.

All three lenses use the same underlying data. They differ in what gets foregrounded vs summarised. The user's free-text topic input remains the primary signal; the lens is a tiebreaker that shapes priority order.
Why use-case-led rather than identity-led
Asking the user "are you a charity / think tank / public affairs professional?" forces them to self-classify, which they often resist or get wrong (a charity doing think-tank-style publication work that month doesn't fit either box cleanly). Asking "what are you trying to do?" maps to use-case rather than identity, which is more durable.
Generation flow
Hybrid: live browser status page during generation, plus email delivery when complete (always, regardless of browser state).
When the user pays and generation starts:

Browser shows live status page — "Generating your pack. Estimated completion: 5 minutes." With progress indicators for each phase (fetching parliamentary data → analysing position evolution → generating PDF → finalising).
Generation runs server-side — completely independent of browser state.
When complete:

If browser tab still open, download button appears for immediate download
Pack is also emailed to user's email address (always, not conditionally)
Email contains both: a download link (primary CTA) AND the PDF as attachment (backup if link breaks or attachment is too large for user's email filter)



This belt-and-braces approach handles all failure modes: tab crashes, wifi drops, user navigates away, email goes to spam, attachment size limits, etc.
Email infrastructure required:

Transactional email service (Postmark, SendGrid, Resend, or AWS SES)
Sender domain (probably delivery@westminsterbrief.co.uk)
DKIM/SPF properly configured to avoid spam filters
Polished email template

Excluded from v1 (and the reasoning)

MP/staff direct contact details — propriety-driven (Mark's civil service position). Tool surfaces who to engage with, users find contacts on parliament.uk themselves
Devolved administration policy — scope discipline; v2 candidate
Social media monitoring — too unreliable, deletion risk, attribution complexity
Paywalled news content — unbalanced paywall geography would skew political balance; ToS/copyright concerns
News reportage as content (vs commentary) — downstream of primary sources already captured
White-label/agency tier branding — v2 candidate at higher price point
Subscription pricing — explicit choice for one-off model at launch
Free trial — explicit choice; users should commit before generation

Market positioning
There is a genuine gap in the market. The current landscape:
Cheap/one-offSubscription/expensiveWestminster-specific, AI-synthesisedGAPVuelio, Dods, DeHavilland (£3-30K/year)Generic templatesFree templates online, Mural £20/moSimply Stakeholders, Quorum
No one is producing AI-synthesised, Westminster-specific stakeholder briefing packs as one-off £49 deliverables. Free Commons Library briefings cover topics, not stakeholder mapping for your engagement. Platforms cost thousands and need expertise to use. Generic tools don't know Westminster.
The format should look like a POSTnote / Commons Library briefing — recognised, "official" feel. Differentiator is personalisation + AI synthesis on a specific organisation's needs. Competitive moat is the structured data corpus (six existing tools + new integrations).
Relationship to the Hansard intelligence strategy (27 April 2026)
A separate strategic conversation on 27 April 2026 sketched a parallel product: an ongoing Hansard intelligence layer with subscription tiers, targeting public affairs / lobbyist users in the £39-179/month range as the primary wedge. Currently this is design intent only, not yet built in production.
What the 27 April strategy described

Near-live Hansard analysis (2-5 minutes behind rolling Hansard publication, not full live video)
Q&A extraction from debates with structured pairing of questions to ministerial responses
Theme tagging across debates
12-month archive with theme-based aggregations, MP-level views, department-level views
Multiple audience views off the same engine (civil servants, journalists, researchers, charities, public affairs/lobbyists, engaged public)
Pricing model: free tier / £39-49/month solo / £129-179/month team / agency tier
Gemini Flash-Lite for batch classification (cheap)

How this relates to the £49 briefing pack
These are two distinct products with different value propositions:

£49 one-off briefing pack = comprehensive synthesis of one specific topic on demand, deliverable artefact
Subscription intelligence tier = ongoing monitoring and alerts across multiple topics with weekly briefings, recurring service

They are complementary, not competing. They share the same underlying data corpus and infrastructure but serve different customer needs (depth on a topic vs breadth and recency across many topics).
Strategic decision (29 April 2026)
The £49 briefing pack proceeds as the immediate Phase 2 focus. The subscription intelligence tier is parked until after the pack launches and generates real usage data. Reasons:

Get one product working end-to-end before scoping the second
The briefing pack reveals which topics generate repeat purchases — informs subscription tier topic priorities
Briefing pack pricing data informs subscription tier pricing
Building both in parallel risks neither being polished

The Phase 2 build will, however, create infrastructure the subscription tier will later use:

Hansard data ingestion for the briefing pack's position-evolution analysis is the foundation for the subscription tier's near-live extraction
Theme tagging built for briefing packs becomes the basis of the subscription tier's archive
The structured Q&A data model from the 27 April strategy informs how briefing pack data is stored

So Phase 2 is best seen as building the data infrastructure that both products will use, with the briefing pack being the first product surface on top of it.
The SEO vs paywall tension
The Hansard intelligence layer described in the 27 April strategy has significant SEO potential — every extracted Q&A, every theme aggregation, every MP-level page is potentially indexable content that ranks for niche policy queries. This is genuinely valuable: TheyWorkForYou is the closest competitor but doesn't have AI-extracted themes or structured Q&A pairing, and Hansard's own search isn't topic-organised.
But putting everything behind a paywall destroys the SEO value entirely — search engines can't rank what they can't crawl.
The resolution is a layered model where different content is gated differently:
All extracted Q&A and basic themes: free, fully indexed, generates SEO traffic and trust
Deep historical archive (e.g. older than 30 days): free, indexed, depth value
Recent activity views (last 30 days, theme-tracked, alerted): paid — this is what lobbyists and PA pros pay for
AI analysis, dashboards, alerts, exports, weekly digests: paid — the substantive ongoing value
£49 one-off comprehensive briefing pack: paid — premium synthesis on demand
This means:

The base archive content is fully indexed (full SEO benefit)
The substantive AI-generated and time-sensitive value is paid (revenue protected)
Casual users (researchers, journalists doing one-off lookups, engaged citizens) get genuine free value (generates trust, traffic, word-of-mouth)
Three distinct revenue streams (subscription tiers, briefing packs, possible future white-label) from one data corpus

This is a well-established freemium pattern — most successful SaaS products in adjacent spaces (newsletter platforms, research tools, monitoring services) use variants of it.
What this means for Phase 2 build sequence
The Phase 2 build should now factor in:

The Hansard data infrastructure built for the briefing pack should be designed for both products. Don't build a one-off pipeline that has to be rebuilt for the subscription tier later. Use the data model sketched in the 27 April strategy (session → questions → analysis) so the briefing pack's data lives in tables that the subscription tier can later expose to subscribers and to crawlers.
Index policy decisions matter even at Phase 2. Even though the subscription tier isn't being built yet, decisions about what gets stored, how content is structured, and what's exposed publicly affect future SEO. Better to design the data model knowing the SEO/paywall layering exists than to retrofit it later.
Hansard ingestion is now a Phase 2 dependency, not just a briefing pack feature. The briefing pack's position-evolution analysis needs structured Hansard data; that data is also the foundation for the subscription tier. Worth scoping the ingestion as a first-class infrastructure piece.

Revisit timing for the subscription tier
Earliest sensible re-evaluation: 6 months after the briefing pack launches, with at least 3 months of usage data. Triggers to bring forward:

Multiple users buying multiple briefing packs (signals appetite for ongoing monitoring)
Repeated requests for "I want this updated weekly"
Explicit feedback that the briefing pack is good but the user wants ongoing rather than one-off

Data sources for the briefing pack
Already available via Westminster Brief tools:

Hansard (debates, statements)
Written Questions (PQs)
Today's PQs Tracker
Member Research
Member Profiles
Stakeholder Directory (3,400+ orgs)

Parliament APIs to integrate:

Bills API — in Phase 2B this is used for analytical synthesis: amendment tracking, vote analysis, position evolution across bill stages, government majority/risk analysis, committee membership analysis. (Factual bill status display — current stage, next sitting, link to bill page — is Phase 2A scope. Phase 2B builds on that to add interpretation and narrative.)
Early Day Motions (via Oral Questions & Motions API) — backbench opinion-tabling, signature patterns, cross-party support

GOV.UK Content API to integrate:

Ministerial speeches
Policy papers
Consultation outcomes (closed consultations and government responses)
Open consultations in progress with closing dates
Impact assessments
Corporate reports

Other government sources to consider:

ONS API (api.beta.ons.gov.uk) — context statistics where relevant
IfG Ministers Database — historical record of who held which role when (useful for "position evolution" sections)

Authoritative reference layer:

Commons Library research briefings — link to canonical impartial source rather than reproducing
POSTnotes / POSTbriefs where relevant
Lords Library briefings where relevant

Explicit exclusions
MP and parliamentary staff direct contact details are NOT included in the briefing pack. This is a propriety-driven decision, not data-availability driven.
Rationale: although technically public, including curated contact lists in a paid product crosses a line Mark prefers not to cross given his civil service position. The pack identifies who to engage with and what they've said; users find direct contact details on parliament.uk themselves.
This decision should be retained even if other features are added later. Marketing language to use: "Westminster Brief identifies the right MPs and committees to engage with, based on their parliamentary activity. Direct contact details are available on parliament.uk."
This positions Westminster Brief as an intelligence tool, not a contact directory — clearer category, fewer propriety questions, arguably more honest about the actual value (synthesis, not directory).
Structural principles for the pack
1. Conditional sections — appear only when signal warrants
If there are no open consultations, no relevant bills, no recent EDMs, etc., the corresponding section is omitted entirely. Do not include "we found nothing" placeholders. Absence of a section is itself information (e.g. "no EDMs section" implies "this isn't a backbench priority right now").
Prompt design needs to instruct Claude Opus to evaluate threshold and relevance, not just summarise everything.
2. Pack length varies by topic activity
A high-activity topic (e.g. AI policy in 2026) might produce 30-50 pages. A focused or quiet topic might produce 12 pages. Both are correct outputs. Communicate this expectation on the product page so users don't expect fixed length.
3. Pack as curated entry point, not replacement
Always link to authoritative sources (Commons Library briefings, GOV.UK pages, ONS data) rather than reproducing them. The link stays current; reproduced summaries don't. This handles staleness honestly.
Pattern: "For the authoritative impartial overview of this policy area, refer to the House of Commons Library briefing: [Title and link, last updated date]. The summary below reflects the briefing as of [pack generation date]; check the linked source for any updates."
4. Time-sensitivity made visible

Pack is dated as generated (footer on every page already required by branding decisions)
Time-sensitive items (open consultations, upcoming bill stages) show their dates prominently
Generated on demand, never pre-built

5. Style: curated entry point, not punchy answer
Style A would be "Pack as the answer — here's what you need to know." Style B is "Pack as a curated entry point — here's what's happening, here are your sources, here's how to keep current."
Style B is more honest, longer-lived, and defensively better. AI synthesis adds value by connecting and contextualising public sources, not by replacing them.
The position evolution section
This is the most distinctive design idea from today's session and likely the strongest differentiator from competitors.
What it does: traces how government and opposition stated positions on a topic have evolved over time. Not just "where things stand" but "where they were 18 months ago, what shifted, when, and what that signals."
Why it matters: Vuelio and Dods don't do narrative analysis at this depth because their target user is a public affairs professional with an analyst on staff. The target user for Westminster Brief (small charity, think tank, in-house policy team without analyst support) needs the synthesis done for them. This is where Claude Opus earns its compute cost over Sonnet.
Data sources for this section:

Hansard (full text of speeches, going back years)
Written ministerial statements
Manifesto pledges (dated, comparable)
Government policy papers (often have version histories on GOV.UK)
Opposition front-bench responses in Hansard

Practical considerations:

Define a sensible time window (current Parliament, or last 2-3 years, whichever is longer)
Distinguish "official position changed" from "different person said something different" (changes of minister vs changes of position)
Honest treatment of ambiguity: if positions haven't changed, say so. Don't manufacture a narrative of evolution where the source shows continuity.

The lexical drift insight (Mark's contribution from inside Whitehall)
Civil Service writing has stock phrases that encode meaning beyond their surface text:

"Shortly" vs "in due course" — confidence in timeline
"Considering" vs "actively considering" vs "keeping under review" — degrees of seriousness
"We will" vs "we intend to" vs "the Government will consider" — degree of commitment
"Welcome the contribution" vs "note the contribution" — degree of agreement

The post-16 skills white paper is the archetypal example: government statements drifted from "publishing shortly" → "publishing in due course" → "publishing shortly" again. Each statement looks fine on its own; the sequence tells the actual story (internal blocker emerged, then resolved).
The position-evolution analysis should be specifically instructed to watch for stock-phrase drift, not just substantive policy changes. This is the kind of analytical lens that comes from years inside Whitehall and is genuinely hard to specify well from outside. It's part of what differentiates a Westminster Brief pack from a generic AI summary.
Tradecraft to capture as a separate doc
Mark should keep a separate docs/analytical-tradecraft.md document accumulating Whitehall-aware patterns as they occur to him. Eventually this becomes source material for the Claude Opus prompt design. Examples to start with:

Stock-phrase drift — see post-16 skills white paper as archetype
Manifesto pledges that disappear from departmental priorities lists are usually about to be quietly dropped
PQs answered with "I refer the Honourable Member to my answer of [date]" often signal frustration with repeat probing — worth noting who's asking what repeatedly
APPG reports that get a written ministerial statement reply are being taken seriously; ones that get a press release reply are being managed
Consultation outcomes that don't address particular submitter concerns
Positions that get vague rather than change ("keeping options under review")

This isn't urgent. It's a slow-accumulation document Mark adds to as he thinks of patterns.
Proposed pack structure (working draft)

Executive summary — 1-2 pages, key takeaways the user can read before a meeting
Position evolution: where things stand and how we got here — 3-5 pages, the narrative arc with stock-phrase drift flagged
Current parliamentary activity — variable length, only sections with signal:

Bills in progress (if relevant)
Recent debates and statements
EDMs (if recent activity)
PQs (if recent activity)


Engagement opportunities — variable, time-sensitive:

Open consultations with deadlines
Upcoming bill stages
Forthcoming committee inquiries


Stakeholder ecosystem — relevant orgs from the directory plus any think tanks / academic centres flagged in the synthesis
Statistical context — ONS or other where genuinely relevant
Authoritative further reading — Commons Library briefings, POSTnotes, etc., with last-updated dates
Methodology and sources — single page, WB attribution per locked branding decision

Reading order is: story → current state → engagement → landscape → deeper sources → transparency. Coherent for someone trying to understand a topic before acting on it.
Open questions / deferred decisions
Several questions originally listed here were answered in the 29 April 2026 session and moved to the "Locked product spec for v1" section near the top of this document. The remaining genuinely open questions are below.
1. Length and structure
Mark has explicitly deferred this — "review later." Will become clearer once a draft pack exists with realistic data. Pack length will vary by topic activity per the locked structural principle (sections appear only when signal warrants).
2. PDF generation approach
Not yet scoped. Options to evaluate:

python-docx (existing dependency) → convert to PDF
WeasyPrint from HTML
Direct PDF generation with reportlab

Decision criteria: branding control (must support locked branding tier), layout flexibility, generation speed (within ~5 min budget), file size (must work as email attachment for delivery flow).
3. Stripe product setup
Single product, single price (£49 one-off) per locked decisions. Webhook configuration for delivery on payment success, test mode workflow before going live. Mechanics not yet scoped.
4. Email infrastructure choice
The hybrid generation flow requires transactional email. Options:

Postmark (good deliverability, simple API, ~$15/month for low volume)
SendGrid (cheaper, more enterprise-focused, slightly less reliable)
Resend (modern, developer-friendly, newer)
AWS SES (cheapest at scale, more setup work)

Decision criteria: deliverability rate (most important), simplicity of integration, monthly cost at expected volumes.
5. MVP scope decision
What's the smallest viable v1 that's still genuinely comprehensive? Mark wants comprehensive at launch. Worth defining what minimum comprehensive looks like so the build has a clear target rather than scope-creeping indefinitely.
Working definition (to be refined): v1 includes all data sources listed in the data sources section, all six core sections of the proposed pack structure, the position-evolution analysis, the data accuracy safeguards, the user input flow as locked, and the hybrid generation flow as locked. v1 does NOT include the items listed in "Excluded from v1" in the locked spec.
6. Pre-flight check prompt design
The Sonnet pre-flight topic check needs careful prompt engineering to:

Reliably classify topics as well-pitched / too broad / too narrow / not Westminster
Generate useful reframings rather than generic "make it more specific"
Respect that broad topics are sometimes legitimate
Not be paternalistic in tone

This is a few hours of prompt iteration with worked examples, ideally tested against real likely user inputs (which we don't have yet — could be informed by Mark's DfE knowledge of how policy areas typically get framed).
Data accuracy and statistical safeguards
A briefing pack that contains hallucinated, stale, or methodologically misrepresented statistics is reputation-damaging — possibly worse, if used to inform a charity's submission to a consultation or a think tank's published response. This is a critical concern given Phase 2's price tier and target users.
The three failure modes to guard against
1. Hallucinated numbers. Claude Opus could generate a paragraph saying "according to ONS data, X% of higher education students are mature learners" with the figure plausibly wrong but not actually traceable to any specific dataset. Most dangerous failure mode because users can't easily verify it.
2. Stale data with confident framing. Even if the number is real, it might be from 2019 while the pack reads as if it's current. ONS data has different release cycles for different series (quarterly, annual, decennial). A pack that says "current statistics show…" when the underlying data is three years old creates the same trust problem.
3. Misinterpreted methodology. ONS publishes data with specific definitions (e.g. "higher education student" includes/excludes certain categories; "NEET rate" can be measured by different age bands). AI summaries can drop these caveats and present headline figures as if methodology is obvious. Statisticians spot this immediately and lose trust in the whole document.
The architectural safeguard: retrieve, don't generate
The strongest protection is a design choice: AI never generates statistics, only retrieves them from named sources and presents the response.
The pattern:

Claude Opus identifies what statistical context is relevant (e.g. "headline HE student numbers, regional breakdown of NEET rates")
Code makes a deterministic API call to ONS for those specific datasets
The actual API response — with its own metadata: release date, methodology link, units — is what gets displayed
Claude Opus is given the retrieved data and asked to contextualise it, not invent it

Significantly more reliable than "Claude reads about HE policy and writes a section including stats." Slightly more complex to build. Worth it.
Citation requirements for every statistic
Every statistic in the pack carries:

The actual figure
The unit (£ million, percentage, count)
The reference period (e.g. "Q4 2025", "2024 calendar year")
The release date (when the source published this)
The dataset name and link to source page

Pattern:

"There were 2.86 million higher education students in the UK in 2023/24.
Source: HESA Higher Education Student Statistics, released January 2025. [Link to dataset.]
Most recent available data at pack generation."

That triple — figure, when measured, when published — handles staleness honestly. The user can see at a glance "this is March 2024 data, published Jan 2025, accurate as of pack generation date." If they need newer, they know to check the source.
Methodology caveats preserved, not stripped
ONS data comes with methodology notes that statisticians care about. A naive AI summary drops these. A good design preserves the most important ones, even as a footnote:

"This figure uses the ONS definition of [X], which differs from the [Y] definition used in DfE statistics."

This is the kind of caveat that protects the product when a careful reader spots a number that looks "off" — they can see the methodological choice has been flagged, not just the headline figure.
Three operating principles for prompt design
1. Never let Claude Opus generate a statistic without source data in its context. If the data isn't in the prompt, the prompt should not invite Claude to provide a number. Phrasing the prompt as "describe the policy context" rather than "describe the policy context including relevant statistics" matters. The latter invites hallucination; the former doesn't.
2. Prefer exact citations over rounded figures. "2.86 million students" with a date is harder to question than "around 3 million students" without one. Specificity invites verification, which is what you want — verification confirms accuracy.
3. Make the staleness window visible. Every statistical claim should carry a "data as of" date. If that date is more than 18 months old, it should be flagged ("Note: this is the most recent ONS data available; next release expected [date]"). Handles the situation where ONS hasn't updated a dataset in a while.
This applies beyond ONS
The same safeguards apply to all factual data in the pack:

Bills API data — bill stages update daily; pack shouldn't claim a bill is "currently at second reading" if it's moved on
Hansard quotes — direct quotes should be exact, with date and Hansard column reference; paraphrases should be clearly marked as such
EDM data — signature counts change; pack should say "as of [date]" and link to the live EDM page
Consultation deadlines — should be the actual deadline, not approximated

The general rule: anything that's a fact should be retrieved and cited. Anything that's synthesis should be clearly framed as the analyst's interpretation. The two should be visually distinct in the pack. This is the journalist's distinction between reporting (verified facts with sources) and analysis (clearly labelled interpretation). A briefing pack that consistently distinguishes these feels far more trustworthy than one that blends them.
Implication for the methodology page
The locked branding decision puts WB attribution on the back-page methodology section. This is the natural place to be explicit about how the statistical safeguards work. Something like:

"All statistics in this report are retrieved directly from named sources (ONS, HESA, Parliament APIs) at the date of pack generation. Each statistic shows its source, reference period, and last-updated date. Westminster Brief does not generate numerical estimates — only sources them.
AI synthesis (provided by Claude Opus) is used for narrative analysis, position-tracking, and contextual framing. AI-generated text is clearly distinguished from sourced data in this pack.
For the most current figures, follow the source links provided. Statistics in this pack are accurate as of [generation date]."

Honest, defensible, addresses the AI-accuracy question head-on rather than hoping users won't ask.
How this reinforces the curated-entry-point principle
This data-accuracy approach reinforces the structural principle that the pack is a curated entry point with sources, not a replacement for the sources themselves. If the pack tries to replace the source, every figure has to be defended forever. If the pack curates and points, staleness is handled honestly and the pack ages gracefully.
Media and commentary analysis (light touch)
A pack that captures only formal parliamentary and government activity misses something users genuinely value: the shape of public debate. MPs read the same papers and listen to the same broadcasts as everyone else; how an issue is being argued in commentary shapes how it gets framed in Parliament. Charities and think tanks want to know "is this issue cutting through?" and "what narratives are gaining ground?" — questions Hansard alone doesn't answer.
The decision is to include this lightly — not as a news monitoring service, but as a complementary signal layer.
Why not full news monitoring
Three reasons explicit news monitoring is excluded from v1:
1. Paywall geography is unbalanced. The Guardian and BBC are free; Telegraph, Times, FT are paywalled. A tool that only reads free mainstream news produces a left-of-centre and centrist establishment view with no right-of-centre counterweight. Reputationally bad for a tool serving organisations across the political spectrum.
2. Terms-of-service and copyright concerns are real. Most news sites' terms prohibit large-scale automated ingestion even of free content. AI summarisation of journalism is a contested legal space (the NYT-OpenAI case being the touchstone). A commercial product that systematically summarises journalism is in grey territory.
3. News is downstream of primary sources we already capture. Hansard, ministerial statements, policy papers, and EDMs are what news journalism reports on. The pack already has the primary sources. News monitoring would add summary of summary, with extra risk.
What's included instead: Option A + Option B combined
Option A: User-supplied URLs
The pack input form includes an optional field: "Paste up to 5 article URLs you'd like analysed alongside the parliamentary intelligence."
The user chooses which articles. The system fetches the public versions of those URLs (or title + description metadata if behind paywall), includes them in the prompt, and has Claude analyse them as part of the synthesis.
Legal posture: user is choosing what to include; the system processes user-supplied content rather than operating a news ingestion service. Same intelligence value, dramatically lower risk.
Option B: Curated commentary sources
Free, opinion-focused, politically diverse sources where AI synthesis is more defensible:

ConservativeHome (right-of-centre policy debate)
LabourList (left-of-centre policy debate)
Think tank publications (covered separately in main scope)
The Conversation UK (academic-led commentary)
LSE / IfG / Resolution Foundation blogs (expert analysis)
New Statesman free articles
Selected free Substack newsletters from UK political analysts

This is commentary and analysis rather than journalism. Different category, easier to use defensibly, arguably more useful — opinion is what shifts the Overton window, not news coverage.
Output framing: discourse analysis, not news summary
The section explicitly frames itself as analysis of the shape of public debate rather than summary of news content. Useful framings the AI synthesis can produce:

"Recent commentary has clustered around three main arguments: [...]"
"ConservativeHome contributors have argued [position]; LabourList has emphasised [position]; cross-party think tank commentary has focused on [position]"
"An emerging argument that [X] has appeared across [sources] in the past 30 days"
"Coverage of this topic in commentary has [increased / decreased] over the past quarter"

This is meta-commentary about discourse, not reproduction of journalism content.
Operating principles
1. No caching of article content. Process, summarise, discard. Article text isn't stored beyond pack generation. Reduces both legal exposure and storage complexity.
2. Source diversity required. The prompt explicitly instructs Claude not to draw only from one political tendency. If both ConservativeHome and LabourList have written on a topic, both are surfaced. If only one tendency has commented, that fact is itself flagged ("commentary on this topic has come predominantly from [tendency]; we found no significant counterposing arguments in [tendency] sources during the period reviewed").
3. Recency window: 30-60 days. Older commentary becomes stale. Longer arcs are handled by the parliamentary intelligence sections, not by this one.
4. Attribution and linking required for every claim. Same principle as the data accuracy safeguards: the pack surfaces and links, doesn't replace.
Propriety consistency
This approach is consistent with the propriety stance taken on MP contact details. Westminster Brief aggregates publicly available commentary, with attribution, for users to inform their own analysis. The tool does not endorse, recommend, or rank sources. The synthesis presents what others are arguing, not the tool's own opinion.
The methodology page should include a line:

"Westminster Brief surfaces commentary from a range of free sources to capture the shape of public debate. We do not endorse, recommend, or rank these sources. Source diversity and attribution are prioritised; the synthesis presents arguments, not the tool's own opinion."

What's explicitly excluded

Paywalled news content (Times, Telegraph, FT, etc.)
Social media monitoring (X, LinkedIn, etc. — too unreliable, deletion risk, attribution complexity)
News reportage (left to mainstream news services that users already subscribe to or follow elsewhere)
Tabloid commentary (signal-to-noise ratio too low for this product's audience)

Revised timeline implication
The original 3-month plan had Phase 2 build in weeks 4-7. Given the expanded scope:

Data integration work alone (Bills API, EDMs, GOV.UK Content API, ONS) = ~1-2 weeks
Pack template and structure design = ~1 week
Position evolution analysis prompt design and tuning = ~1-2 weeks (the hardest part)
PDF generation + branding implementation = ~1 week
Stripe wiring + delivery flow = ~3-5 days
Testing and refinement = ~1 week

Realistic Phase 2 build: 6-8 weeks rather than 4. Worth re-baselining the plan.
This still allows toolkit soft launch in weeks 8-10 if Phase 1 launches first, with Phase 2 following shortly after.
Next session entry points
User input flow and lens picker are now locked (see "Locked product spec for v1" near the top of this document). Suggested order when picking up Phase 2 build work:

Data integration spike — Bills API first — well-documented Parliament API, immediately useful, validates the integration pattern. End-to-end: fetch bills relevant to a topic, parse stage data, integrate into a sample pack section. ~3-5 days.
Draft a sample pack manually using whatever data you can pull from the Bills API spike plus your existing tools' data. This becomes the visual and structural reference for everything else. Don't aim for production quality — aim for "enough to evaluate the shape." ~3-5 days.
PDF generation approach decision — evaluate python-docx vs WeasyPrint vs reportlab against the sample pack. Pick the one that handles your branding requirements and the document complexity. ~2-3 days including a small spike on each option.
Build the rest of the data integrations — EDMs, GOV.UK Content API (consultations, speeches, policy papers), ONS API. Each is a few days. ~1-2 weeks total.
Position evolution prompt design — the hardest analytical work. Starts with worked examples (Mark's post-16 skills white paper case being the archetype) and iterates. ~1-2 weeks.
Pre-flight topic check prompt design — Sonnet prompt with worked examples for well-pitched / too broad / too narrow / not Westminster classifications. Tests against real likely user inputs. ~3-5 days.
User input form, generation flow, and email delivery — the hybrid status-page-plus-email infrastructure. Includes Stripe wiring (single product, £49). ~1 week.
Polish, testing, real-data trial runs. Generate test packs on different topics across the three lenses; refine accordingly. ~1 week.

Total: realistically 6-8 weeks of focused work, consistent with the revised timeline above.
The sample pack approach is important: it's much easier to design the system once you have a target output than to design the system speculatively. Build something rough first, then refine.