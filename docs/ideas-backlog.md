# Ideas Backlog

Capture point for ideas that are worth keeping but not currently being built. Active means "consider when the time is right"; Killed means "decided against, with reason."

When Claude Code encounters a new idea mid-session that isn't being actioned immediately, it should add it here rather than letting it drift.

---

## Active

### Post-launch: revisit the 4 paused stats producers (Ofcom, OBR, OfS, Ofgem)
Paused pre-launch (8 Jun 2026) because discovery returned 0 publications — but they are **not** dead surfaces. Root cause diagnosed: all 4 are `producer_type` regulator/ndpb (or have `gov.uk` in the URL), so `select_strategy()` routes them to `GovUkSearchStrategy`, which queries the GOV.UK Search API for their org slug filtered to *statistics document types* (`official_statistics` / `statistical_data_set` / `statistics_announcement`). Those return **0** for all 4 — yet each org has a real gov.uk presence (Ofcom 384 docs, Ofgem 136, OBR 60, OfS 40), just none tagged as statistics doc-types. Their actual statistics live on their **own domains** (ofcom.org.uk, obr.uk, officeforstudents.org.uk, ofgem.gov.uk). `DirectPageParserStrategy` (own-domain) never gets a chance because `GovUkSearchStrategy.can_handle()` claims them first via `producer_type`.

**Per-body decision when revisited (fix-or-deregister, per the strategic call):**
- **Parser-fixable** → the body has a real own-domain publication surface the GovUkSearchStrategy isn't reaching. Fix = route to an own-domain/bespoke parser (Wave-2 risk class), verify it extracts, repopulate, un-pause → it gets a page.
- **Transitively covered** → its key stats already surface via a parent dept's GOV.UK pubs (Ofgem→DESNZ? OfS→DfE? OBR→HMT? Ofcom→DCMS?). If so, **decline** (HESA precedent — no standalone row needed).
- **No accessible surface** → decline.

The thin-content gate keeps this clean: each either repopulates (real page) or stays out (no empty page). Note: `GovUkSearchStrategy.can_handle()` short-circuiting own-domain regulators is the structural bug — worth a broader look (does any other regulator/ndpb get mis-routed?).

### PQ Answers as Attributed Government Statistics
Mine the PQ answer text already held in the database (~90k PQs, policy-area tagged) for government-stated figures, and surface them as attributed statistics — "this figure was stated by [department] on [date], in answer to PQ [UIN]: '250,000 students received loans in 2024-25'." Faithful, attributed, dated, with permanent reference — the same lift-and-attribute model as stated key-findings (NOT synthesis; stays on the right side of the free-archive no-synthesis line). Source is ALREADY SOLVED — the PQ answers are in the DB (no Hansard search needed; it's extraction from text already held). Best home is the PQ PAGES (the validated, used feature), NOT the unlaunched stats catalogue — enriches the gem using data already held.

**Two empirical unknowns to test with a SLICE before building (same discipline as the key-findings slice):**
- YIELD: what fraction of PQ answers actually contain a clean, liftable figure? Likely a minority — many PQ answers are non-answers ("information not held centrally", "I refer the member to...", referrals, deflections). Test on a real sample across policy areas.
- EXTRACTION ACCURACY (the real risk): extracting figures from prose can introduce errors WORSE than no feature — a wrong government figure presented as fact gets quoted and relied on, on a tool civil servants use. Caveats matter ("approximately 250,000, excludes part-time, subject to revision"). Must be conservative extraction (only unambiguous caveat-free figures) AND always shown IN CONTEXT (surrounding sentence + source PQ link), never a bare number divorced from its qualifiers.

**Guardrail:** figures must always carry their context/caveats and link to the source PQ. Never a bare extracted number. Conservative extraction over high yield — a missed figure is fine (falls back to no-figure-shown); a mangled figure is a credibility-killer.

**Trial evidence (6 June 2026) — two trials, both pointing to PQ-answers-ONLY:**

*Trial 1 — extraction on a real debate* (Lords, "AI: Impact on Human Relationships and Society", 5 Jun): a 13-speaker debate yielded ~12 liftable figures (high yield). But almost all were peers *citing third parties* (Barclays, OBR, Male Allies UK, Milburn, Durham) — i.e. "a peer quoted this stat", not "the government stated it." Weaker provenance than a PQ answer.

*Trial 2 — source verification of those debate figures* (traced each to its origin): **of ~11 cited figures, only 4 verified cleanly.** Three failure tiers:
- **CLEAN (4):** OBR 40% UK occupations exposed to AI, Durham VAWG report, Evo 2 (9.3T nucleotides), DfE AI tutors end-2027 — figure + source confirm. Safe.
- **DRIFTED (the insidious tier):** Male Allies UK "26% preferred AI companion to real human connection" — but the source's 26% was "*liked the attention*"; the "*preferred to real-life*" figure was **36%**. The peer FUSED two stats into a claim neither supports. The *number* (26%) is real and verifiable — so a naive ✓-check ("does 26% appear in the source?") would PASS it and propagate the misquote as verified fact. **This is why automated ✓/⚠ verification is dangerous: drift passes the check while being false.**
- **UNTRACEABLE:** Barclays 70–90% (source real, specific figure unconfirmed in any public summary), Milburn "7% of 14–16s unfit for work" (not found), and unnamed-source figures (3–27% hallucination, ⅓ of teens, >50% use generative AI) — can't be confirmed; live only in the spoken claim.

**The decisive structural finding: PQ answers are VERIFIED-BY-CONSTRUCTION; debate citations are not.** A government figure in a PQ answer IS its own primary source — the department stating it is the authority; the figure and its source are the same object. No tracing, no drift (it's not paraphrasing a third party — it's stating its own data), no fusion risk. **The verification problem you'd have to build for debate-stats is the problem PQ-stats DOESN'T HAVE.** Debate citations are NEVER primary — a peer's spoken recollection of someone else's figure, two hops from authority, mediated by memory/paraphrase; >half fail to trace or are mangled.

**DECISION (confirmed by both trials): build from PQ ANSWERS only. Debate-citation extraction REJECTED** — it would require a hard verification layer that *still* fails the majority of the time (the drift trap passes misquotes), whereas PQ figures need no verification. The verification idea (✓/⚠ stamps), once tested, became another argument FOR PQ-only: the source you'd verify (debates) is the one to avoid; the source to use (PQs) is self-verifying.

**Still slice-to-test before build** — even verified-by-construction, extraction must preserve the PQ answer's OWN caveats faithfully (a department's figure can carry "data not held in this format" / "provisional" / "excludes X" qualifiers — same fidelity discipline, just no third-party tracing needed). Test PQ-answer yield + extraction accuracy on a real sample first.

**Revisit trigger:** post-launch; any "how do we add stats to pages" or "enrich PQ pages" conversation; capacity for an extraction slice to test yield + accuracy. Test with a slice (yield + extraction accuracy on a real sample) before any build. Homed on PQ pages.

*Captured 6 June 2026 — Mark's idea. Two same-day trials (debate extraction + source verification) settled the source question: PQ answers only; debate-citation extraction rejected (third-party, untraceable/drifted >half the time). Open questions now narrowed to PQ-answer yield + caveat-faithful extraction. Slice-to-test, not build.*

---

### Party pages need restructuring + contain some incorrect information

Production `/archive/party/<slug>` pages now render manifesto content (after the 6 June 2026 party-page 500 fix — `a286b1d`, restored ManifestoChunk import). They're live, but the site is pre-launch (`noindex` + robots-blocked), so they won't appear in search — only reachable by direct URL. Low exposure, not urgent. Mark's read (6 June): "not the end of the world if found; they need work — some information is incorrect, just needs restructuring."

The pages need: (a) correction of factually-wrong content, and (b) layout/structure rework. Connected to the manifesto workstream (ManifestoChunk content + the manifesto-review admin flow currently disabled in `templates/admin.html`, awaiting rebuild — see `docs/manifesto-status-opus-brief.md`).

Action when revisited: walk each party page, list what's incorrect (specifics not yet captured — gather when actioned), then restructure. **Must be done before removing `noindex` / any launch / advertising** — incorrect content is fine hidden, not fine indexed.

**Revisit trigger:** before the pre-launch checklist removes `noindex`; when the manifesto workstream resumes; before advertising the site.

*Captured 6 June 2026 — flagged by Mark on seeing the party pages live post-500-fix.*

---

### Sinn Féin party config — mojibake in `_PARTY_SLUG_MAP` (data-quality bug)

`hansard_archive/views.py` `_PARTY_SLUG_MAP["sinn-fein"]` has UTF-8-read-as-Latin-1
corruption: `full_name` = `"Sinn FÃ©in"` and `cached_names` = `{"Sinn FÃ©in"}`
([views.py:2094](../hansard_archive/views.py#L2094)); also `member_summary` joiner
`" Â· "` ([views.py:2531](../hansard_archive/views.py#L2531)). **Real consequence:**
the corrupted `cached_names` won't match the correctly-encoded `"Sinn Féin"` in
`cached_member`, so Sinn Féin's MP/Lord counts likely render as zero. The display
name is also garbled. Pre-existing; **not** the cause of the party-page 500 (that
was the missing `ManifestoChunk` import, fixed in `ca00919` / `3cc992f`).
Deliberately kept out of that hotfix to keep its blast radius minimal.

**Revisit trigger:** during the manifesto/Tier 3 review pass, or any Sinn Féin
party-page work. Fix = correct the three strings to proper UTF-8; verify member
counts populate. Check the source file for other mojibake while there.

### Triage the 15 pre-existing full-suite test failures — don't normalise a red suite

As of 5 June 2026 the full `pytest tests/` run shows **15 failures, 425 passed** — all in `test_producer_authorisation.py` (13: `TestStatProducerAuthLogModel`, `TestAdminAuthGuard`, `TestAuthoriseRoute`, `TestDeclineRoute`) and `test_stats_registry.py::TestSeedScript` (2: `test_build_seed_data_returns_29_entries`, `test_seed_inserts_all_producers`). Confirmed pre-existing (identical with the Issue-B-fix changes stashed), so not a regression — but a chronically-red suite is how a real regression hides as "failure #16, unnoticed."

Two specific concerns:
- **Producer authorisation routes (13 failures):** producer authorisation is a launch gate (batch-authorising ~21 producers via `/admin` before the stats catalogue goes public). These failures are *probably* test drift (admin-session fixture, route signature), but it must be confirmed the `/admin` authorise + decline routes actually work before relying on them for the batch.
- **Seed-count 29 (2 failures):** likely just HESA de-registration drift (28 producers now, not 29) — update the expected count, or confirm the seed list is intentionally shorter.

Action: triage each cluster as known-benign (and fix/mark accordingly) vs unknown (investigate). Goal is a green suite so the next genuine regression is visible. The earlier `project_preexisting_test_fixes` set (`test_llm_fallback`, tracker-badge) is no longer in the failing set, so the red-suite membership has already drifted once — another reason to get it to zero and keep it there.

**Revisit trigger:** before the producer batch-authorisation launch step; or as a standalone chore branch. Not blocking the Issue B fix.

*Captured 5 June 2026 — surfaced while verifying the Issue B fix caused no regressions.*

---

### GOV.UK discovery 422 — `order=newest` invalid (Issue A — FIXED; was NOT DSIT-specific)

The 5 June 2026 re-discovery execute run surfaced a 422 via DSIT. Investigation (6 Jun) proved the cause was **not DSIT-specific and not the org slug** — it was an invalid sort parameter affecting **every GOV.UK producer**.

**Issue A — `order=newest` is an invalid GOV.UK Search API value → HTTP 422. FIXED 6 Jun 2026.** Empirical peeling of the params (org-only → add each back): `order=newest` 422s on its own, for *any* org (DfE 422s identically to DSIT). The GOV.UK Search API expects a field-based sort with a leading `-` for descending; the literal `"newest"` is not valid. **Root cause:** `order=newest` was added 24 May 2026 (deploys `0389784`) — it has 422'd ever since, but the swallow-to-`[]` bug (Issue B) hid it, so **GOV.UK producer discovery + re-discovery silently found nothing for ~2 weeks (24 May–6 Jun)** for *all* central departments, not just DSIT. The Issue B raise-on-failure fix is what surfaced it. **Fix:** `order=newest` → `order=-public_timestamp` in `GovUkSearchStrategy` (verified: HTTP 200, newest-first; DSIT returns 36 results incl. 2026-06-05). Test `test_sends_newest_first_order_param` updated to assert `-public_timestamp` and guard against regressing to `newest` (the old test asserted `== "newest"`, locking the bug — it was mocked so the 422 never showed).

**Catch-up implication (not data loss):** because GOV.UK discovery returned nothing 24 May–6 Jun, the catalogue missed any *new* GOV.UK statistics publications in that window. Once the fix is on production, the next re-discovery run will find them as `new` and classify+insert — self-healing. Expect the first post-fix run to show `new > 0` for GOV.UK producers (a backlog catch-up, NOT a dedup failure). To force it sooner, clear `last_rediscovered_at` for the affected producers (as was done for DSIT) so they're due immediately.

**Issue B — fetch-failure was swallowed to `[]` and still stamped. FIXED 5 Jun 2026 (commit on master).** `GovUkSearchStrategy.fetch_candidates` caught the 422 and returned `[]` (the `except: return []` anti-pattern), and `run_rediscovery` stamped `last_rediscovered_at` unconditionally — so a failed fetch looked identical to "nothing new" and the producer was marked re-discovered while blind, with `errors=0` hiding it. Fix: all three HTTP strategies (`OnsApiStrategy`, `GovUkSearchStrategy`, `DirectPageParserStrategy`) now raise `StrategyFetchError` on a hard fetch failure (genuine HTTP-200-empty still returns `[]`). This honours the existing `run_discovery` contract ("raises on unrecoverable fetch failure — caller must catch and record"). The raise propagates to: the discovery worker → marks producer `failed` (not `completed`); the re-discovery cron → counts `errors`, rolls back, and `run_rediscovery` aborts *before* its stamp → **no stamp on failure**. Tests: `test_fetch_failure_propagates_and_does_not_stamp` (re-discovery), updated `test_raises_on_request_failure` (strategy). Protects *all* producers, not just DSIT.

**Remaining follow-up (Issue A):** the code fix must reach **production** (the `rediscovery-cron` runs off `master`) for the cron to actually work again. Then a catch-up run picks up the ~2-week GOV.UK backlog. Optional: clear `last_rediscovered_at` for the central departments so they're due on the next run rather than waiting out the 24h cadence.

*Captured 5 June 2026 — surfaced by the re-discovery dry-run, sharpened by the execute run's 422; root-caused and fixed 6 June 2026 (`order=newest` → `-public_timestamp`).*

---

### Landing page: lead with the job-to-be-done (design observation, not committed)

Observation (28 May 2026): the landing page currently describes what WB *is* (parliamentary research tools) and *contains* (the tool cards), but doesn't clearly answer "what is this site for?" for a cold visitor. The job-to-be-done — understand what Parliament is doing on your topics, across otherwise-scattered sources, in one place, kept current — is the value, and it's the same thing as the data-corpus moat.

Thoughts to weigh when/if revisited (none committed):
- Lead with the job, not the tool list
- 2–3 concrete worked examples ("see every time apprenticeship funding came up in Parliament in the last 6 months — debates, answers, bills, tagged, in one place") to make the value tangible
- Surface the scale numbers (5k debates, 90k WQs, 621 bills, 12k orgs, daily) — currently buried in grey, but strong credibility signal for a cautious professional audience
- Whitespace/density: this audience (GOV.UK, Hansard, Commons Library readers) may read density as credibility more than airy SaaS layouts — weigh against
- Constraint: keep the propriety/accuracy hedging ("verify before use" etc.) — the challenge is clarity within cautious framing, not punchy marketing copy. Vagueness is the gap, not caution.

Matters most because the landing page converts the widening audience shares (Teams group → parly → private office) from click into understanding. Revisit deliberately, not squeezed between build tasks.

**Revisit trigger:** ahead of any deliberate push to a new audience segment; or when conversion from landing page to tool use is noticeably low.

*Captured 28 May 2026 — Mark's design thoughts, explicitly not committed.*

---

### Hansard Archive on Homepage + Naming Disambiguation

Add a 7th card to the homepage tool grid for the Hansard Archive. Suggested card: title "Hansard Archive [BETA]", description covering 12 months of debates and WQs organised by policy area, MP, and theme with AI tagging, CTA "Open Hansard Archive →", link `/archive`. Grid treatment: 4-per-row or a featured first card rather than forcing 7 into a 3-column layout.

Also resolve the naming confusion between "Hansard Search" (existing card — live API) and "Hansard Archive" (new — local DB). Preferred option: rename the existing card to "Live Hansard Search" and add a one-line differentiator to each ("Searches the live Hansard API" vs "Browse 12 months of AI-tagged debates"). Retiring Hansard Search outright is an option once Archive coverage is confident.

**Revisit trigger:** Hansard Archive is share-stable (backfill complete, cron running clean, no known data gaps); any homepage or onboarding session; any "what should new users see first?" conversation.

*Captured 5 May 2026 — deferred until Archive is share-stable.*

---

### Government Consultations
Add a parallel data layer to the directory covering government consultations. Three states (open / closed-awaiting-response / closed-responded), plus anticipated/forthcoming consultations. Data source: gov.uk consultations and department publications. Cross-references nicely with existing committee evidence — same organisations often engage with both. Probably 30–50 hours of work for v1. See `docs/consultations-design-note.md` for fuller thinking.

**Revisit trigger:** Public beta running 4+ weeks with tester feedback; users ask about consultation tracking; capacity for substantial new feature work; any "directory expansion" conversation.

*Captured 26 April 2026.*

---

### Key Speakers on a Topic — Three Product Surfaces

Who's actually leading the parliamentary conversation on a given issue, not just who's spoken once. Three distinct surfaces with different build costs:

1. **Theme page enhancement** (`/archive/theme/<slug>`) — "Top contributors" panel: count-based, no analytical work, indexable for SEO. Build cost: ~1–2 days. Free feature.
2. **Paid briefing pack** (Phase 2B) — "Key MPs to engage": full analytical treatment, position analysis, advocate identification, Whitehall-aware framing. Already scoped as part of the £49 pack design.
3. **Subscription alert** (Phase 2B reframed) — "New contributions on theme X by MPs you follow": recurring product, different shape from the briefing pack.

**Revisit trigger:** 4+ weeks post-share with GSC data on theme-page traffic. If theme pages are getting organic visits, Option 1 is high-leverage and low-cost. If theme pages get no traffic, Option 1 doesn't earn its build cost. Usage patterns inform which surface is worth building first.

*Captured 11 May 2026 — sparked by looking at a session detail page and noticing speaker info is buried in the contribution list. Right surface depends on how users actually traverse the archive.*

---

### MP Engagement Scoring
Rank MPs by observable parliamentary engagement per topic — WQs tabled, debate contributions, EDM signatures, committee questions, adjournment debates — as a composite score. Output: "MPs most engaged with [topic X]." Useful for stakeholder mapping by charity policy officers, public affairs professionals, and researchers. Potentially a flagship paid-tier feature.

**Revisit trigger:** Public beta running 4+ weeks with active users; users raise stakeholder mapping as a need; Mark is deciding paid-tier feature shape; "what comes next after launch" conversation.

**Phase 1 option:** Single-signal WQ report (WQs by MP for a keyword) — 4–6 hours, validates the concept before committing to the full build.

---

### Active Inquiries Filter — Stakeholder Directory
Surface a filter on the directory to show only organisations currently under active committee scrutiny — i.e. where an inquiry is open. The data is already in the schema (`inquiry_status`). A checkbox or badge filter on the directory results page would surface it without new data work.

**Revisit trigger:** Directory has meaningful coverage (>500 orgs with inquiry data); users are actively using the directory; any session touching directory UX.

---

### Inquiry Tracking Surface
A dedicated view or alert for open committee inquiries relevant to a user's watched policy areas — "new inquiry opened on [topic]." Design doc exists at `docs/select-committee-plan.md` (the committee evidence tracker). This is the monitoring/alerting tier of that feature, deferred from the initial research build.

**Revisit trigger:** Select Committee research page is live and used; Mark is building the dashboard Phase C feed; premium alerting tier conversation.

---

### EDM Digest — Content Marketing
Early Day Motions digest as a content marketing vehicle: a weekly public summary of EDMs tabled on key policy topics, published on Westminster Brief, optimised for search. Serves SEO and positions Westminster Brief as a go-to reference for people searching parliamentary activity. Secondary benefit: validates the EDM tracker feature before building the full product version.

**Revisit trigger:** Public beta is live; Mark is thinking about SEO and content strategy; any "how do we get organic traffic" conversation.

---

### PQ Backfill — Chunked Date-Range Mode

Large backfills (`--days 365`) hit Parliament WQ API 500 errors at high pagination depths (skip=61000+) because the API struggles with offset queries over large result sets. Fix: add an optional `--chunk-days` flag to `ingest_pq_cron.py` that splits the date range into smaller windows (e.g. 30 days at a time), each paginated independently. Keeps skip values low and avoids the deep-offset failure mode.

**Revisit trigger:** Any future full-year backfill run; any session adding `--skip-answer-fetch` or similar backfill flags; before the next schema migration that requires re-ingesting all rows.

*Captured 5 May 2026 — observed during is_holding/is_withdrawn backfill (500 errors at skip=61000+).*

---

### Periodic API Audit Ritual
Formal check (quarterly or when adding a new feature) of all external API dependencies: are documented behaviours still accurate? Any new endpoints available? Any deprecated params still in use? Surfaces the kind of drift that caused the tracker regression (answeringBodies re-introduced despite documented constraint).

**Revisit trigger:** Quarterly if no other trigger; before any session that touches TWFY, Parliament WQ API, or Hansard API; after an unexplained regression.

**What it involves:** Re-read constraint docs, spot-check key endpoints, update CLAUDE.md if anything has changed. Probably 1–2 hours per audit.

---

### Saved Searches / Watchlists
Save a search configuration (topic + dept + date range) and re-run in one click. PQ teams run the same searches weekly. Dashboard Phase B feature — see `docs/dashboard-roadmap.md`.

**Revisit trigger:** Dashboard Phase B work begins; user feedback mentions repetitive searching.

---

### Boolean / Operator Search
AND/NOT operators in the Hansard search input to reduce noise in AI summaries. E.g. "student loans NOT postgraduate" to narrow results.

**Revisit trigger:** Users complain about noise in results; any session improving search quality.

---

### Demo / Sample Outputs on Homepage
Screenshots or redacted sample Word exports visible before login. Major adoption barrier for departments needing IT approval — approvers want to see what the tool produces before granting access.

**Revisit trigger:** Pre-launch checklist work; any session on the homepage or landing page; user onboarding conversation.

---

### Debate Prep: Commons Mode
Current Debate Prep page accepts Lords peer names only. Add a Commons toggle for departmental oral questions, urgent questions, and opposition day debates.

**Revisit trigger:** Users mention oral questions prep; any session touching the Debate Prep page.

---

### Progressive Profiling — Richer User Context After First Value
Non-government users are asked only for Sector at signup. Once they've experienced the tool, richer profiling can be collected organically:
- After saving a search → "Want similar topics flagged when they come up in Parliament?"
- After repeatedly using a tool on one topic → "Tell us more about your interest in [topic]"
- On /my_preferences → an optional expandable "Tell us more about your work" section (policy area, subject, organisation name)

This data is more useful when given voluntarily after experiencing value, not demanded at signup.

**Revisit trigger:** Public beta has been running 4+ weeks with active non-government users; usage analytics show repeated searches by the same users; any session touching onboarding or /my_preferences; "what do we know about our users" conversation.

*Captured 28 April 2026 — explicitly deferred from onboarding rework brief.*

---

### Hansard Archive — Triage the "other" Bucket (Week 3)
Some sessions classified as `debate_type='other'` are substantive. The taxonomy survey flagged "Business of the House" (105 contributions), several `hs_2cGenericHdg` sessions (e.g. "School Minibus Safety" 14 contributions, "NHS Dentists" 19 contributions), and Supplementary Estimates debates. The initial theme-tagging run excludes all `other` sessions to avoid procedural noise, but the substantive ones should be tagged eventually.

**What it involves:** Query `other` sessions with contribution count > threshold (suggest: 20+). Manually triage the list — identify which are genuine policy debates vs procedural interruptions (Points of Order, Call Lists). Add `reclassify_candidate = True` flag or directly reclassify the clear cases (e.g. "Business of the House" → `debate`, substantive emergency debates → `debate`). Then include in the tagging run.

**Revisit trigger:** Phase 2A Week 3 page template work; any session asking "what's in the other bucket"; user reports missing a debate they know happened.

*Captured 29 April 2026 — explicitly deferred from Week 2 tagging build per Mark's instruction.*

---

### Bulk Migration — Chunked-Commit Mode
For any future bulk data load into Railway Postgres (not needed for the 30–50 session incremental cron runs), add an optional `--chunked` flag to the migration script that commits in batches (e.g. 5,000 rows per commit) rather than a single transaction. Trades all-or-nothing atomicity for lower peak working space — avoids the ~1.5GB WAL peak that caused the disk-full error during the initial 203k-row migration. The script already has `ON CONFLICT DO NOTHING` so interrupted chunked runs are safely re-runnable.

**Revisit trigger:** Any future bulk historical backfill (e.g. extending the archive back to 2020); any migration touching a volume nearing its disk allocation; any session adding data to the `ha_*` tables at scale.

*Captured 1 May 2026 — from disk-full error during initial SQLite→Railway migration.*

---

### Backup Secrets — Runtime Injection vs Build-time ARG/ENV
Railway's Nixpacks build passes env vars (including `BACKUP_ENCRYPTION_KEY`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`) as Docker ARG/ENV during the image build. This means secrets could leak via Docker image layer inspection. Investigate whether Railway supports runtime-only env injection for the cron service so these credentials never appear in the build layer.

**Revisit trigger:** Any security review; before making the backup service or R2 credentials more widely accessible; any session touching Railway infrastructure or the cron service config.

*Captured 29 April 2026 — spotted in cron service build logs during backup pipeline setup.*

---

### Backup Monitoring — Railway Alert

~~Silent backup failures went undetected for 13 days (29 April – 10 May 2026) because the cron service exited non-zero but nothing surfaced the failure. Two small items to prevent a repeat:~~

1. **Railway cron alert on non-zero exit** — investigate Railway's notification options (dashboard alerts or webhook). Objective: if `backup-cron-r2` exits non-zero, Mark gets notified same day rather than discovering it weeks later.
2. ~~**`/health` backup-freshness check**~~ — ✓ Done 15 May 2026. `/health` now reports `backup: ok/STALE`; `backup_to_r2.py` records to `ha_cron_run` after each successful run.

**Remaining:** Railway notification config only (item 1).

**Revisit trigger:** Any session touching the backup pipeline or Railway notification settings; before Phase 2 launch.

*Captured 12 May 2026. Partially complete 15 May 2026.*

---

### Universal Source-Link Audit — Complete and Implement

Westminster Brief links every piece of content to its canonical Parliament/GOV.UK source. The audit (14 May 2026) identified:
- PQ detail: link exists but buried in small-print → promote to header
- MP archive pages: no parliament.uk link (TWFY ID available; MNIS ID not stored → interim: add TWFY link, longer term: add `mnis_id` to `cached_member`)
- Session detail: ✓ already has "View on Hansard ↗"
- Bills, EDMs: not yet ingested (no action needed now)

URL patterns documented in CLAUDE.md "Source attribution principle" section.

**Revisit trigger:** Any session touching PQ detail pages, MP archive pages, or new content type detail pages. Implement PQ link promotion + MP TWFY link as next template-touch session.

*Captured 14 May 2026 — audit done, implementation pending Mark review.*

---

### Sitemap Caching for PQ Archive Scale

Phase 2A.5 candidate. PQ archive adds ~90k URLs to the sitemap; per-request generation would take ~25s and risk Google's fetcher timing out. PQ URLs stripped from sitemap before Phase 2 deploy as interim fix.

**Plan:** `ha_sitemap_cache` table with `name`, `content` (text), `generated_at`. `_build_sitemap_xml()` serves from cache if `< 24h` old, regenerates synchronously on cache miss. Archive cron pre-warms nightly. Once stable, add PQ URLs back and resubmit to Search Console. Sitemap index pattern (`/sitemap-sessions.xml`, `/sitemap-pqs.xml`) as a follow-up if URL count grows past 100k.

**Effort:** ~2–3h. No new infrastructure — DB-only.

**Revisit trigger:** Immediately after Phase 2 deploy is stable; before submitting sitemap to Search Console.

*Captured 3 May 2026.*

---

### Stakeholder Directory — Organisation Enrichment (Conditional)

**Design decision (16 May 2026):** conditional enrichment, not promised enrichment. Each org page renders what data is available — no placeholders for missing data, sections simply don't render when empty. Users see consistent, honest pages: rich where data exists, lean where it doesn't.

**Data sources (priority order):**
1. Wikipedia — descriptions + structured infobox data, CC-BY-SA with attribution
2. Charity Commission — charity facts (registered charities only), free structured data
3. Companies House — company facts (registered companies only), free structured data
4. AI-generated — descriptions only, clearly labelled, for orgs not in above sources

**What renders only if data exists:** description, key facts panel (founded/type/HQ/size), CEO/Chief Executive (registry sources only), website link (verified or AI+validation flag), source attribution + last-refreshed date.

**What always renders:** organisation name, activity records (PQs, evidence, meetings).

**Build approach:** enrich incrementally starting with most-engaged organisations. Coverage grows over time without user-visible rollout.

**Effort:** Wikipedia + Charity Commission + Companies House ingestors: ~3–4 days. AI fallback: ~1 day additional. Schema: `source_type`, `source_refreshed_at` fields on description/facts.

**Revisit trigger:** Post-Teams-share; Stage C polish complete; first major Phase 2A.5 enhancement candidate.

*Captured 5 May 2026. Revised 16 May 2026 — conditional enrichment model confirmed.*

---

### Parliamentary Speaker Analysis + Predictive Engagement View

Multi-phase build. Three pieces, two phases.

**Piece 1 — Speaker analysis (Phase 2A.5, ~1 week)**
Structured analytical view of each MP's parliamentary engagement patterns — contribution history, themes, frequency, debate types. Publishes to users when complete.

**Piece 2 — What's on at Parliament (Phase 2A.5, ~1–2 weeks)**
Live page showing upcoming parliamentary business. Publishes to users when complete.

**Piece 3 — Predictive engagement analysis (multi-phase, ~9–12 months total)**

*Stage A: Build prediction system (~4–6 weeks)*
Pattern recognition for likely speakers and positions on upcoming debates. AI-driven, drawing on Pieces 1 + 2 plus historical contribution data.

*Stage B: Silent run for accuracy measurement (~3–6 months)*
System generates predictions but does not publish them. After each debate, compare predictions against actual outcomes. Measure: did predicted speakers speak? Did unpredicted speakers speak? Did positions match? Accuracy by debate type, theme, party, MP type.

*Stage C: Decide based on data*
If accuracy is genuinely useful: publish predictions with calibrated confidence levels based on measured accuracy. If weaker: reframe as "patterns that may inform" rather than predictions, or continue iterating.

**Why silent-run-first:** generates credibility evidence before any public claim; calibrates confidence framing empirically; catches systematic biases before they're public; teaches which signals actually work.

**Strategic significance:** distinguishes Westminster Brief from data-only parliamentary tools; creates moat (months of accuracy data competitors can't quickly match); provides Substack content (analytical posts about findings).

**Risks acknowledged:** public predictions can be wrong (measurement is mitigation); politically sensitive surface (methodology must be transparent and evenhanded); multi-month timeline.

**Build sequence:**
- Pieces 1, 2: post-Teams-share, Phase 2A.5
- Piece 3 Stage A: when Pieces 1+2 are stable
- Piece 3 Stage B: runs silently in background, no user-facing change
- Piece 3 Stage C: only after empirical accuracy data justifies publishing

**Revisit trigger:** Pieces 1+2 complete and stable; any "what's next after directory enrichment" session; Substack content planning conversation.

*Captured 16 May 2026 — substantive analytical capability, methodology refined.*

---

### £5 Paid Mailout — Cheaper Recurring Entry Product

A cheaper recurring entry-point product alongside the eventual £49 briefing pack. Email-shaped, low-commitment, designed to build a paid email list as an asset distinct from website traffic and from the free Substack plan.

**What it is:**
- £5 per issue or per month, recurring rather than one-off
- Email format (not PDF), drawing on the same WB data corpus that backs the £49 pack
- Curated regular slice of the aggregated data rather than on-demand comprehensive synthesis

**Distinct from the existing free Substack plan** (Layer A editorial + Layer B automated digest, both free as marketing infrastructure per 13 May 2026 decision). The £5 mailout is paid, recurring, lower commitment than £49 pack, and builds a separate paid email list asset.

**Strategic positioning — not yet decided:**
- *Bridge product:* lives between free archive and £49 pack as a first-paid step that converts a subset of subscribers to £49 buyers
- *Standalone complement:* serves users who want ongoing low-effort touch rather than deep on-demand synthesis — different customer relationship from £49

Probably both at different times; bridge first, standalone product as it matures.

**Why this works given the moat framing:**
The £49 pack's defensibility is the WB data corpus Claude can't otherwise access. Same logic supports a £5 mailout — a regular email digest drawing on the structured Hansard/PQ/stats aggregation is something a Claude Pro user can't reproduce on their own. Different product shape, same underlying moat.

**Open design questions:**
- Cadence: weekly, monthly, event-triggered, mixed?
- Topic scope: themed by policy area (subscriber-selected), general Westminster digest, or editor's choice?
- Length: short (300-500 words), medium (1000-1500), or long-form?
- Production: manual editorial under Mark's name, Code-generated and reviewed, or fully automated?
- Platform: Substack paid tier (low lock-in concern, native handling) or separate (ConvertKit/Buttondown, more control)?
- Lifecycle: monthly cancellable, pre-paid blocks, lifetime early-subscriber tier?

**Cost economics (rough):**
AI cost is pennies per issue at Flash-Lite/Haiku scale. Email infrastructure roughly £15-30/mo at low volume, or 10% to Substack. At £5/subscriber/month, 100 subscribers = £6,000/year gross with healthy margins.

**Revisit trigger:** Free archive has stable returning audience; Phase 2B £49 pack is close to launch or recently launched; users explicitly asking for ongoing updates rather than on-demand reports; "how do we build recurring relationships with readers" or "what's the cheaper product" conversation.

*Captured 13 May 2026 (initial stub) — expanded 27 May 2026 alongside £49 pack moat clarification.*

---

### "Useful Tool Nobody's Built" — Competitive Landscape

Mark wants to explore the question of what parliamentary intelligence tools exist, what gaps there are, and where Westminster Brief has genuine competitive differentiation. Separate session.

**Revisit trigger:** Mark explicitly flags "landscape" or "competitive" session; any product strategy or positioning conversation; before writing the landing page or marketing copy.

*Captured 13 May 2026 — deferred by Mark mid-session.*

---

### Reusable Concurrent Fetcher Utility

Bounded worker pool with per-API rate-limit config and 429/503 backoff — intended as shared infrastructure before Hansard and WQ backfills start. Single-threaded ingestion is fine for the 621-bill run (nearly done) but would make 70–80k Hansard and 30k WQ records into multi-day jobs.

**Design intent:** one utility, configurable per API (Parliament Bills API, Hansard API, WQ API each have different tolerance). Replaces the per-script `_get()` + `time.sleep()` pattern with a proper worker pool. Bills ingestion can be left as-is — retrofit is not worth the risk on a completed run.

**Revisit trigger:** Bills display work (Steps 8+) is complete; Hansard backfill brief is about to be drafted. Build this *before* drafting that brief, not after.

*Captured 18 May 2026 — Mark's explicit sequence: bills work → concurrent fetcher → Hansard backfill.*

---

### Bill detail pages — gov.uk factsheets section

Gov.uk publishes factsheets for major bills at predictable URLs under `/government/publications/{bill-slug}-factsheets/`. Example: `https://www.gov.uk/government/publications/crime-and-policing-bill-2025-factsheets/crime-and-policing-bill-child-sexual-abuse-material-factsheet`. These are authoritative plain-English explanations of what individual clauses do — high-value for policy professionals.

**Design options:**
1. **Manual link** — store a `govuk_publications_url` field on `ha_bill` and populate for major bills only. Simple, accurate, no scraping.
2. **Derived link** — attempt to derive the slug from the bill title + session year. Brittle; will break on renamed bills.
3. **GOV.UK search link** — link to `https://www.gov.uk/search/all?keywords={title}&content_store_document_type=guidance` as a fallback. Lower fidelity but always works.

Option 1 is cleanest. Add `govuk_publications_url TEXT` to `ha_bill` schema; populate manually or via a small enrichment script for the 89 Government Bills. PMBs rarely have factsheets so null is fine.

**Revisit trigger:** Bill detail pages (Step 8) are being built; any session touching the bill schema.

*Captured 19 May 2026 — Mark identified from Parliament's own bills site.*

---

### Research Tool → Local DB Migration (Replace External API with ha_* tables)

> **"Debate Contributions" tab promoted to approved scope — see `docs/research-tool-db-migration-scoping.md`** (5 June 2026). A live bug (Baroness Smith of Malvern fails TWFY name lookup — she's a 2024 peer TWFY can't resolve by name) traced to this tab still using TWFY. Crux data-check PASSED: Lords contributions 98.1% ID-keyed; Baroness Smith joins to 1,472 contributions by `member_id` → clean ID-keyed swap, not migration-plus-linking. Approved, plan-mode queued behind the stats launch-decision. The scoping doc holds the locked scope/decisions; plan-mode it from there at its slot.

**Context:** The Parliamentary Research Tool (`/debates`, `debate_scanner.py`) currently fetches live from the Hansard Parliament API (or TWFY fallback) on every search. The local `ha_*` DB now contains essentially the same data — the migration would replace the fetch layer with DB queries.

**Audit result (20 May 2026):** Local DB covers all fields the Research Tool needs:

| Research Tool needs | Local DB field | Notes |
|---|---|---|
| Speech text | `ha_contribution.speech_text` | Full text in ORM + FTS via `speech_tsv` |
| Speaker name | `ha_contribution.member_name` | ✓ |
| Speaker party | `ha_contribution.party` | ✓ |
| Parliament member ID | `ha_contribution.member_id` | Same ID used by Hansard minister search |
| Session date | `ha_session.date` | ✓ |
| Session title | `ha_session.title` | ✓ |
| House (Commons/Lords) | `ha_session.house` | ✓ |
| Debate type | `ha_session.debate_type` | ✓ — controlled vocab already in use |
| Hansard URL | `ha_session.hansard_url` | ✓ — already used by archive tool |
| Department (for OQ filter) | `ha_session.department` | ✓ |
| WQs | `ha_pq` table | Full text, asking/answering member, FTS via `question_tsv` |
| WMS | `ha_session` filtered by `debate_type='ministerial_statement'` | ✓ |
| Policy area tags | `ha_session_theme` | Bonus — richer than API |

**What's not available (minor):**
- TWFY `relevance` float — replace with `ts_rank` from FTS. Equivalent for ordering.
- TWFY `listurl` links — replace with `ha_session.hansard_url` or `/archive/debate/...` internal links.

**Scope of work (for Opus to scope properly):**
1. New fetch layer: `_fetch_from_db(topic, source_type, date_range, member_id=None)` querying `ha_contribution` FTS and joining `ha_session`. Drop-in replacement for `fetch_hansard_topic()` and `fetch_hansard_minister_topic()`.
2. WQ fetch: replace Parliament WQ API call with `ha_pq` FTS query.
3. WMS fetch: replace WMS API call with `ha_session` filter on `debate_type`.
4. URL generation: switch from TWFY-format URLs to `ha_session.hansard_url` or internal archive links.
5. AI payload assembly, grouping, deduplication, Word export: **unchanged**.
6. Remove `SEARCH_BACKEND` env var (no longer needed); remove TWFY dependencies for search.

**Benefits:** 10–100× faster responses (DB vs live API), no TWFY API key dependency, no external outage risk, results directly linkable to archive detail pages.

**Risk:** `speech_text` is not in the SQLAlchemy ORM model definition (added via ALTER TABLE migration) — needs either raw SQL or adding the column to the ORM class before the migration.

**Coverage note:** DB covers last 12 months (deliberate product scope). No regression vs current product intent.

**Revisit trigger:** Any session scoping Phase 2A.5 work after Teams share; any "Research Tool performance" conversation; any "TWFY dependency" conversation; Opus architecture review session.

*Captured 20 May 2026 — audit done by Code, scoping to be done by Opus.*

---

### /about/legislation — "How Parliament Makes Laws" Explainer Page

Scaffold page explaining the UK bill procedure for lay readers, linked from the timeline display on `/bill/<id>` pages. URL to confirm: `/about/legislation` or `/how-parliament-makes-laws`. Content drafted by Mark separately; Code's job is the page scaffold (template, route, nav link) and the inline link near the timeline ("How does this work? →") on the bill detail page.

**Scope:** Page scaffold + cross-link only. No content authoring by Code. Stage descriptors in `hansard_archive/bill_stages.py` may be reused or adapted for the explainer.

**Timing:** After bill detail pages are built (Step 8). Mark drafts content first; page is assembled around that content.

**Revisit trigger:** Step 8 (bill detail pages) is complete; Mark has drafted the explainer content; any session touching bill display templates.

*Captured 18 May 2026 — raised post-stage-descriptor build.*

---

### Bill Committee Evidence on Bill Detail Pages

Should committee evidence (oral/written) relating to a bill appear on the bill's detail page (`/bill/<id>`)? The stakeholder directory already ingests committee evidence into `sd_staging_committee_evidence` / `sd_engagement`, but there's no link between those records and `ha_bill`. Answering this requires two decisions:

1. **Product:** does a bill detail page surface "who gave evidence on this bill?" — useful for policy professionals who want to see the scrutiny picture alongside the legislative timeline.
2. **Data model:** how is the link established? Options include: (a) a FK `bill_id` on `sd_engagement` (requires matching committee evidence to bills — nontrivial), (b) a join table `ha_bill_committee_evidence`, or (c) a derived link via committee inquiry name pattern-matching against bill titles.

Neither the ingester nor the schema currently supports this link.

**Revisit trigger:** Bill detail pages (Step 8) are being built; any session touching the bill schema or the committee evidence ingester; any "what does a bill page show?" design conversation.

*Captured 22 May 2026.*

---

### Steady-state data file extraction cron (data_url piece 4)

> **Related — see `docs/re-discovery-scoping.md`** (added 3 June 2026): piece 4 is the
> downstream maintenance half of a two-mechanism freshness design. The upstream half —
> new-publication re-discovery — is a committed pre-launch gate with design substantially
> worked out (incl. the ONS dataset-ID normalisation that unifies new-pub discovery with
> ONS date-refresh). Read that doc before building piece 4; its two-mechanism split and
> Decision-H reasoning shape where each job lives.

The data_url backfill (piece 2) is a one-shot covering existing publications. New publications created by the discovery worker get `data_files_status = 'pending'` by default but nothing currently triggers extraction on them. Steady-state coverage needs a scheduled job.

**Decision (made during piece 2 scoping, 28 May 2026): daily cron, option 2.**

A lightweight Railway cron runs `extract_pub_data_files.py --execute` daily, picking up any `pending` (and `fetch_failed`) publications. Same script as the backfill, deployed as a scheduled service rather than a one-shot. New publications get extracted within 24h of discovery.

Ruled out:
- Inline in discovery worker — violates failure isolation (decision H): an extraction fetch timeout must not block/slow a discovery run. Different workloads, different failure modes.
- Manual one-shot only — fragile; new pending rows arrive continuously via the discovery cron, so a growing unextracted tail builds between manual runs.

**To nail down when scoped:**
- Selection query: `pending` AND `fetch_failed` (failed rows must be retry-eligible, not stranded)
- Timing: after the discovery cron completes + buffer (extraction is downstream of discovery). Derive from discovery's actual schedule, not an isolated hour.
- Memory: cron runs then exits, no persistent worker (matches policy_area pattern)
- Re-extraction of already-`extracted` rows: separate question from daily mop-up — whether/how often to re-fetch to catch new editions (NHS-style accumulating pages, quarterly releases). Decision I from scoping flagged ~monthly with a skip for annual/biennial < 60 days old. Scope alongside or defer.
- Piece 2b interaction: once sub-page following ships, the cron should also re-process `no_files_found` sub-page-only rows (re-queue or include in selection).
- **ONS date refresh (added 3 June 2026):** piece 4's selection logic must cover a SECOND job beyond data-files — re-fetching ONS `first_published_at` (latest-version release_date) and updating-where-changed. ONS dates go stale because the discovery worker only INSERTs and never updates existing rows, so each new weekly/quarterly ONS version leaves the stored "Latest release" date behind. GOV.UK first-published is immune (write-once). One cron, two jobs — don't build a separate mechanism. Full context in `docs/phase-1-9-scoping.md` (ONS publication date section). Needs to be live within the first few weeks of /stats going public, before drift misleads.

**Constraint added 28 May 2026 — do NOT re-attempt the 47 Phase 1.9 HTML-content publications.**

Piece 2b dry-run (28 May 2026) confirmed that 47 `no_files_found` GOV.UK publications
serve their statistical data as HTML content on sub-pages — no `gem-c-attachment`
downloadable files exist on the parent page OR the sub-pages. Sub-page following has
been attempted and yielded zero files. Recovering these is Phase 1.9's job, not
piece 4's.

The selection query must NOT re-attempt these 47 IDs:
```
664, 686, 687, 695, 705, 720, 733, 743, 750, 751, 775, 814, 828, 835, 838,
847, 848, 859, 862, 1124, 1128, 1133, 1135, 1164, 1166, 1167, 1193, 1198,
1202, 1205, 1207, 1246, 1272, 1280, 1296, 1309, 1400, 1438, 1454, 1460,
1494, 1504, 1505, 1541, 1549, 1601, 1602
```

Implementation options to pick from at design time:
- Explicit ID exclusion in the selection query
- New status value `html_content_only` distinguishing these from other `no_files_found` rows
- Boolean flag on `ha_stat_publication` marking "Phase 1.9 candidate" (most extensible)

Full context in `docs/phase-1-9-scoping.md` (section: "Empirical finding from Phase 1.8 piece 2b").

**Build timing:** after piece 2 backfill verified, piece 3 (ONS), and piece 2b (sub-page following) — or whenever steady-state coverage starts mattering. Not urgent: the backfill covers all current pubs, and new ones sit safely at `pending` until the cron exists.

**Revisit trigger:** piece 2 backfill complete + verified; piece 3 (ONS) scoped; or any session where the pending/unextracted tail is visibly growing.

*Captured 28 May 2026.*

---

### ~~Rename DISCOVERY_DRY_RUN to something less misleading~~

**Done — flag removed entirely (2026-05-27, commit `60b5c7a`).** Resolved more decisively than a rename: the flag suppressed producer state-machine transitions only (not candidate writes), making the name actively misleading. Removing it is cleaner than renaming. Railway env var `DISCOVERY_DRY_RUN` removed from `discovery-worker` service after deploy.

*Captured 2026-05-23.*

---

## Killed

*(Nothing formally killed yet — this section is for ideas explicitly decided against, with reason recorded so they don't keep resurfacing.)*

---

## How to use this file

**Adding an idea:** When a new idea comes up in conversation that isn't being actioned, Claude Code should add it here in one go — name, one-line description, revisit trigger. Don't wait to be asked.

**Surfacing ideas:** When a relevant trigger condition applies, surface the idea from this list to Mark for a decision. Don't act on it; surface it.

**Killing an idea:** If Mark explicitly decides against something, move it to Killed with a one-line reason. This prevents it from being re-raised.

**Graduating an idea:** When Mark decides to build something, move it to TODO.md (for current-session work) or a dedicated design doc. Remove from this backlog.
