# Ideas Backlog

Capture point for ideas that are worth keeping but not currently being built. Active means "consider when the time is right"; Killed means "decided against, with reason."

When Claude Code encounters a new idea mid-session that isn't being actioned immediately, it should add it here rather than letting it drift.

---

## Active

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

### Backup Monitoring — Railway Alert + /health Freshness Check

Silent backup failures went undetected for 13 days (29 April – 10 May 2026) because the cron service exited non-zero but nothing surfaced the failure. Two small items to prevent a repeat:

1. **Railway cron alert on non-zero exit** — investigate Railway's notification options (dashboard alerts or webhook). Objective: if `backup-cron-r2` exits non-zero, Mark gets notified same day rather than discovering it weeks later.
2. **`/health` backup-freshness check** — add a check to the `/health` endpoint that queries R2 (or a local timestamp written by the backup script) and flags `backup_fresh: false` if the most recent backup is older than 25 hours. Makes freshness visible without needing to open Railway or R2.

**Effort:** ~30–45 min combined. No schema change, no new infrastructure.

**Revisit trigger:** Any session touching the backup pipeline or `/health`; before Phase 2 launch; any infrastructure review session.

*Captured 12 May 2026 — from backup gap diagnosis (29 Apr–10 May 2026 gap hidden by silent cron failures).*

---

### Sitemap Caching for PQ Archive Scale

Phase 2A.5 candidate. PQ archive adds ~90k URLs to the sitemap; per-request generation would take ~25s and risk Google's fetcher timing out. PQ URLs stripped from sitemap before Phase 2 deploy as interim fix.

**Plan:** `ha_sitemap_cache` table with `name`, `content` (text), `generated_at`. `_build_sitemap_xml()` serves from cache if `< 24h` old, regenerates synchronously on cache miss. Archive cron pre-warms nightly. Once stable, add PQ URLs back and resubmit to Search Console. Sitemap index pattern (`/sitemap-sessions.xml`, `/sitemap-pqs.xml`) as a follow-up if URL count grows past 100k.

**Effort:** ~2–3h. No new infrastructure — DB-only.

**Revisit trigger:** Immediately after Phase 2 deploy is stable; before submitting sitemap to Search Console.

*Captured 3 May 2026.*

---

### Stakeholder Directory — Organisation Enrichment (Description, URL, Leadership)

The 12,058 orgs in `sd_organisation` have `canonical_url` and `description` columns but both are empty for every org. Adding this data would make the directory pages meaningful rather than just engagement lists.

**Description + URL (lower effort, high confidence):**
Gemini Flash can generate a 2-sentence "who is this organisation" summary and canonical website URL for most UK policy orgs from training data. Batched overnight script, ~2–4 hours of API calls, a few pounds in API cost. Risk: URLs need verification (Gemini may hallucinate). Mitigation: store `url_verified = false` flag and surface unverified URLs as "unconfirmed" in the UI until spot-checked.

**Chair and CEO (harder, schema change needed):**
No `chair` or `ceo` fields in the schema yet. Leadership changes frequently (quarterly for some orgs), so stale data is a real risk. Better data sources than website scraping:
- Charity Commission API (free) — trustee data for ~170k UK charities
- Companies House API (free) — directors for registered companies
- Gemini with web search — covers remainder but accuracy is variable and goes stale

Recommended approach: Charity Commission + Companies House for registered entities; Gemini as fallback for trade bodies, universities, and think tanks not in either registry.

**Effort:** Description + URL: ~1 day. Chair/CEO with registry APIs: 3–5 days including schema migration, ingestor, staleness handling.

**Revisit trigger:** Directory is live and used; any "make org pages richer" session; Mark is thinking about what the org detail page should show.

*Captured 5 May 2026.*

---

## Killed

*(Nothing formally killed yet — this section is for ideas explicitly decided against, with reason recorded so they don't keep resurfacing.)*

---

## How to use this file

**Adding an idea:** When a new idea comes up in conversation that isn't being actioned, Claude Code should add it here in one go — name, one-line description, revisit trigger. Don't wait to be asked.

**Surfacing ideas:** When a relevant trigger condition applies, surface the idea from this list to Mark for a decision. Don't act on it; surface it.

**Killing an idea:** If Mark explicitly decides against something, move it to Killed with a one-line reason. This prevents it from being re-raised.

**Graduating an idea:** When Mark decides to build something, move it to TODO.md (for current-session work) or a dedicated design doc. Remove from this backlog.
