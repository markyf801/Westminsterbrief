# Phase 1.8 Scoping — Catalogue classification, policy area alignment, and public launch

**Status:** In progress — policy_area tagging shipped 2026-05-27
**Date:** 23 May 2026
**Prerequisite phases:** Phase 1.6 (producer registry), Phase 1.7 (discovery skill)

---

## Context

Phase 1.7 delivered the discovery skill. As of 23 May 2026, the discovery worker has been validated against three meaningfully different producer types:

- **ONS** — public API, 20 candidates discovered cleanly, all 20 reviewed and authorised
- **HESA / Jisc** — bot-blocked, graceful failure with zero candidates and clear logging
- **DfE** — gov.uk-based, 681 fetched, 627 classified, 182 written, 445 skipped, 54 classifier failures

The pipeline works end-to-end. The system handles success, failure, and scale gracefully.

A CSV review of the first 20 authorised ONS candidates surfaced several design findings that scope Phase 1.8. This document captures those findings as the Phase 1.8 plan.

---

## Phase 1.8 goal

Take the working catalogue from Phase 1.7 and bring it to a public-launch-ready state. The end state is a catalogue civil servants can use to find UK government statistical publications, with classification that supports cross-corpus navigation (publications and Hansard sessions together).

Phase 1.8 is *not* about extracting structured data from publications. That is Phase 1.9 (see below). Phase 1.8 prepares the foundation that Phase 1.9 will build on.

---

## Findings

Findings are organised by the data review that produced them. Each review represents a step in understanding what Phase 1.8 should look like; reading them in order shows how the design picture evolved.

### Part A — From 23 May ONS candidate review

### 1. Subject area vocabulary is uncontrolled

The classifier currently produces free-text subject area labels. Across 20 ONS candidates, this produced inconsistencies:

- "Mortality statistics" used for 3 deaths-related publications, but "Health Statistics" used for suicide statistics (same conceptual domain)
- "Retail sales" (lowercase) and "Retail Sales" (title case) used for sibling publications
- "Economic Statistics" for Quarterly GDP but "Regional GDP" for Annual GDP (same publication family)
- "Economic statistics, Consumer spending" as a comma-separated multi-value (other rows have single values)

By contrast, the cadence field (which uses a controlled vocabulary in the classifier prompt) produced consistent, accurate output across all 20 candidates with zero null values.

**Empirical finding:** the classifier is reliable when constrained, inconsistent when given free rein.

### 2. The existing Hansard taxonomy provides a natural controlled vocabulary

Westminster Brief's Hansard infrastructure tags sessions against 23 policy areas defined in `hansard_archive/policy_areas.py` as `HANSARD_POLICY_NAMES`. Each session gets 1–3 `policy_area` tags (controlled) plus 1–5 `specific` free-text tags (LLM-generated).

This 23-area list is a **curated Westminster Brief vocabulary**, not a direct mirror of GOV.UK's current topic taxonomy. Investigation on 23 May 2026 (see `docs/gov-uk-taxonomy-research.md`) confirmed:

- GOV.UK retired its "policy areas" taxonomy in 2019 in favour of a unified hierarchical topic taxonomy with ~17 top-level taxons going up to 5 levels deep
- The Hansard 23-area list overlaps with GOV.UK's current taxonomy in places (e.g. Business and industry, Crime, justice and law, Education, training and skills) but renames others to suit Westminster usage (e.g. "Employment and labour market" rather than "Work") and promotes some sub-topics to top-level status because they matter to Westminster's audience (Economy, Finance and taxation, Parliament and constitution)
- The list is therefore audience-tuned rather than GOV.UK-mirror — a thoughtful design choice that reflects how civil servants actually navigate policy content

Stat publications currently sit outside this taxonomy. They have a free-text `subject_area` field with no controlled vocabulary.

**Implication:** if stat publications were tagged against the same 23-area Westminster Brief taxonomy as Hansard sessions, the corpus becomes cross-navigable. A user could find "everything on Education, training and skills" — Hansard sessions, ministerial questions, and stat publications — in one query. Manual mapping of the 20 ONS candidates against the 23 areas (23 May review) showed all ONS publications mapped cleanly to one or more existing areas, suggesting the vocabulary works for stat publications without needing extension.

### 3. gov.uk is the natural primary discovery surface

DfE publications discovered via gov.uk include items where the actual data lives on other organisations' sites:

- An "Alternative providers of higher education" publication on gov.uk links to HESA-hosted PDFs
- Many gov.uk publication pages link to `assets.publishing.service.gov.uk` (gov.uk's own asset CDN)
- Other gov.uk pages link to NHS Digital, Ofqual, Office for Students, etc.

This is the systematic pattern in UK statistical publishing: the policy-owning department announces on gov.uk, and the data-publishing body hosts the actual files. gov.uk is therefore the natural aggregation point.

**Implication:** the producer registry can probably be simplified from ~150 individual producers to ~25-30 core gov.uk-hosting departments and major ALBs, with smaller bodies reached transitively through their parent department's gov.uk publications list. This avoids the bot-blocking issue (HESA, etc.) for most cases.

### 4. Publication URL semantics matter

Some publication URLs point to landing pages (gov.uk page describing the publication). Some point to the actual data (a PDF, an API endpoint). These are sometimes the same, sometimes different.

For Westminster Brief's audience (civil servants who want to cite and navigate to data):
- The gov.uk landing page is usually the right citation point (stable, well-structured, includes metadata)
- The actual data location is sometimes useful as a separate field

**Implication:** the data model should probably distinguish "publication URL" (citation/landing) from "data URL" (where the file actually lives). Phase 1.8 should decide this.

### 5. Real-world data has machinery-of-government complications

Example: "Apprenticeships and traineeships data" appears in DfE's gov.uk publications list, but apprenticeships policy moved to DWP. The publication is still attributed to DfE on gov.uk (historically accurate) even though policy ownership has changed.

The discovery skill cannot detect this without external knowledge. Decisions about whether to flag, decline, or re-attribute such publications are editorial, not algorithmic.

**Implication:** Phase 1.8 does *not* need to add re-attribution capability for this. See finding 6 below — tag-based discovery makes producer re-attribution unnecessary. Editorial tools (decline reasons, edit fields) cover the small minority of cases where a publication should be excluded entirely.

### 6. Tag-based discovery makes cross-departmental attribution unnecessary

A natural question raised by finding 5 was: should the system try to re-attribute publications when policy ownership changes between departments (e.g. apprenticeships moving from DfE to DWP)?

The answer is no. Robust tag-based classification handles cross-departmental content discovery without needing producer re-attribution.

**The architecture is:**

- **Discovery skill** captures publications correctly — producer, URL, metadata. Source-of-truth, factual.
- **Classifier** assigns topic tags — 1–3 controlled policy_area tags plus free-text specifics. Analytical, search-enabling.

These are well-separated responsibilities. The discovery skill doesn't need to know anything about topics. The classifier doesn't need to know anything about who published what.

**The result:** when a user searches for "skills" or "apprenticeships," the query returns publications regardless of which department published them:

- BIS publications on skills (historical)
- DfE publications on skills (more recent)
- DWP publications on apprenticeships (current ownership)

All surfaced through tag-based search. Producer attribution remains accurate to publication time (what gov.uk said), and machinery-of-government changes don't require any re-attribution work.

**Why this is the right architectural call:**

1. **Tags reflect content. Producers reflect provenance.** Both are useful for different purposes; neither should try to do the other's job.
2. **Machinery of government changes constantly.** Keeping department attribution synchronised with current policy ownership is a losing battle.
3. **It matches how users actually search.** Civil servants search by topic, not by current department ownership of that topic.
4. **It mirrors how Hansard already works.** Sessions are tagged with policy areas and specifics; corpus search is topic-first, with speaker information available for filtering. Stat publications follow the same model.
5. **It avoids fragile heuristics.** No need to detect "this publication is really about a topic owned by a different department now."

**This finding strengthens findings 1 and 2 (controlled vocabulary + Hansard taxonomy alignment).** The vocabulary isn't just for consistency — it's the architectural mechanism that makes cross-departmental discovery work without depending on perfect provenance tracking. The Westminster Brief 23-area taxonomy is audience-tuned for exactly this kind of cross-corpus navigation.

### 7. Description truncation may be present in source data

The Annual GDP candidate description ends mid-sentence ("...East of England,") while related publications have complete descriptions. Cause unconfirmed — could be ONS API truncation, classifier output limit, or DB column length.

**Implication:** quick investigation needed during Phase 1.8 to determine where truncation originates and whether it's worth fixing.

### 8. URL versioning is a real product decision

ONS API URLs include specific version numbers (`.../versions/45`, `.../versions/130`). These URLs point to historical versions, not "the latest." For weekly publications, the stored URL becomes stale almost immediately.

**Options:**
- Accept versioned URLs as stable historical pointers (current default)
- Store a "latest version" URL alongside (requires understanding each producer's URL pattern)
- Use human-facing landing page URLs instead of API URLs

**Implication:** Phase 1.8 needs to decide URL semantics, and this interacts with finding 4 (publication URL vs data URL).

---

### Part B — From 24 May Hansard tag distribution review

Pre-read data for the dedicated taxonomy conversation. Code ran distribution analysis against `ha_session_theme` to ground taxonomy decisions in actual usage data rather than assumptions.

**Corpus summary:** 7,121 Hansard sessions total; 4,829 tagged (67.8%); 2,292 untagged (32.2%). 15,906 unique specific tags used across 22,777 total specific tag instances.

### 9. The 23-area Hansard taxonomy is empirically validated

All 23 policy areas are used in practice — none is dead. The distribution is coherent:

- **High-volume areas** (10%+ of tagged sessions): Government and public administration (37.9%), Economy (27.1%), Business and industry (21.9%), Crime/justice/law (19.9%), Finance and taxation (19.8%), Welfare and benefits (16.6%), Health and social care (15.9%), Society and culture (15.3%), Education (13.5%), Parliament and constitution (12.4%), Employment and labour market (11.4%), Transport (11.2%), Environment (10.7%), Housing and planning (10.2%), Foreign affairs and diplomacy (10.0%)
- **Mid-volume areas** (5-10%): Children and families, Defence and armed forces, Energy, Local government, Trade, Science and technology
- **Low-volume areas** (under 5%): Immigration and borders (3.9%), International development (0.95%)

The low-volume areas reflect genuinely low content volume on those topics in Hansard, not classification gaps. International development at 0.95% is appropriate — Parliament debates this rarely.

**Implication:** the Westminster Brief 23-area vocabulary works as a controlled list. No areas need removing or merging for Phase 1.8. The vocabulary is good as-is.

### 10. "Government and public administration" functions as a procedural wrapper

At 37.9% of tagged sessions (1,832 of 4,829), this is by far the most-used area — nearly 2× the next category. Co-occurrence data confirms why: its most common specifics are "parliamentary procedure", "statutory instrument approval", "secondary legislation scrutiny", "arrangement of business", "national security."

The pattern is: when Hansard content is procedural in nature (debates about how Parliament does its business, statutory instruments, ministerial responsibilities), "Government and public administration" is the area tag and the substantive subject matter goes in the specifics. The policy area is doing procedural-classification work as well as subject-classification work.

**Implication:** for stat publications, "Government and public administration" needs a clear definition in the classifier prompt. Stat publications don't have a procedural dimension — they're all "publication" in procedural terms. If the area is used for stat publications, it should mean substantive content about how government works (civil service operations, machinery of government, public administration practice), not a procedural wrapper. Without this clarification, it could either be massively underused (no procedural angle to absorb) or become a catch-all for anything Cabinet-Office-adjacent. The Phase 1.8 classifier prompt must be explicit about which interpretation applies.

### 11. The natural tag count is 2-4 per session, not 1-3

The Hansard tagging cap is documented as 1-3, but the empirical distribution shows the natural shape extending higher:

- 1 tag: 6.7% of tagged sessions
- 2 tags: 25.3%
- 3 tags: 40.9% (the mode)
- 4 tags: 19.9%
- 5 tags: 5.6%
- 6+: 1.6%

86% of tagged sessions get 2-4 tags. The 4+ outcomes (27% combined) suggest the 1-3 cap is sometimes overridden, or the cap is documentation rather than enforcement.

**Implication:** for stat publications, the cap should probably be 1-4 (or 1-5) rather than strictly 1-3. The 3-tag mode is the typical case; allowing up to 4 accommodates publications that genuinely span multiple policy areas (e.g. a tax-and-benefits publication touching Welfare, Finance and Economy).

### 12. Free-text specifics produce a very long tail — by design, not failure

The specifics distribution shows:

- 13,196 unique specifics (83%) used only once
- 2,386 (15%) used 2-5 times
- 299 (1.9%) used 6-19 times
- 25 (0.16%) used 20+ times

The 25 high-frequency specifics are recurring Westminster themes: "economic growth", "cost of living", "violence against women", "energy security", "industrial strategy", "NHS waiting lists", "child poverty", "universal credit."

This is the expected shape of a useful free-text classification field. The recurring themes are the genuinely salient cross-cutting topics; the long tail captures the specific concerns of individual sessions.

**Implication:** the controlled-vocabulary-plus-free-text-specifics pattern is empirically validated. For stat publications, retaining the `subject_area` field as free-text alongside the controlled policy areas preserves this useful structure. The free-text field captures nuance that the controlled list cannot.

### 13. Specifics function as cross-cutting tags spanning policy areas

The co-occurrence data shows specifics like "economic growth" appearing across multiple policy areas (Economy, Business and industry, Finance and taxation). "Energy security" appears across Energy, Economy, Business and industry. "National security" appears across Government and public administration, Defence, Crime/justice/law.

This is actually *useful* — a user searching by specific gets cross-area results that the flat policy area structure cannot produce by itself.

**Implication:** specifics aren't redundant with policy areas — they're a complementary cross-cutting dimension. Adding hierarchy to the policy areas would partially overlap with what specifics already do. This argues against rushing into hierarchy for Phase 1.8; the flat-areas-plus-specifics model is doing real navigation work.

### 14. 32% of Hansard sessions are untagged

The untagged rate is worth filing as a known issue but it's not a Phase 1.8 blocker. Possible causes (not investigated): sessions too procedural/short to warrant tagging, classifier failures, pre-tagging-rollout content, sessions that genuinely don't fit the taxonomy.

**Implication:** Phase 1.8 expects stat publication classifier coverage to be near 100% (every candidate goes through classification by design). The Hansard untagged rate is a separate data quality matter, not a Phase 1.8 concern.

---

### Part C — From 24 May DfE skip rate investigation

The DfE discovery run on 23 May produced a 65% skip rate (445 of 681 candidates skipped). Investigation on 24 May identified the cause and surfaced several concrete Phase 1.8 implementation tasks.

**Headline finding:** the skip rate is correct behaviour, not a bug. GOV.UK's search API returns multiple historical releases of the same recurring publication series (e.g. "Apprenticeships and traineeships" appeared 28 times — once per year/release). The classifier correctly normalises each to the same canonical name → same slug → second+ occurrences are correctly skipped as duplicates of the first.

### 15. The publication model is "series, not releases" — and this is correct

The data model assumes one catalogue entry per *publication series*, not per *release of a series*. "Apprenticeships and traineeships data" is one row in `ha_stat_publication`, not 28. This is the right design — civil servants want to find "the apprenticeships statistics," not 28 entries for the same series across different years.

**Implication for Phase 1.8 catalogue volume estimates:**

The number of items in the catalogue reflects the count of publication series across all producers, not the count of releases. Examples from the 23-24 May discovery runs:

- ONS: 20 series (low skip rate — ONS's API returns one entry per series)
- DfE: 182 series from 681 fetched items (445 collisions, 54 LLM rejections)
- DWP: expected to show similar high skip rate (DWP publishes many recurring series — Universal Credit, benefits statistics, employment programmes)

This is a meaningful framing correction. Phase 1.8's catalogue should be described as "X publication series across Y producers" rather than "X publications across Y producers."

### 16. URL capture currently uses first-seen ordering, not recency

The genuine data quality issue underneath the correct skip behaviour: when the same series appears multiple times in the GOV.UK search results, the URL stored is whichever result came first in the API response. That ordering is not recency-based.

**Concrete impact:** "Childcare and early years survey of parents" stored the 2017 URL because that's what came first in the API response. The 2024 release was correctly skipped as a duplicate slug, but its URL was discarded. A civil servant clicking through that catalogue entry would land on a 2017 page when a 2024 release is available.

This pattern likely affects many of the 182 DfE rows. The data model is correct; the URL capture strategy isn't.

**Implication:** Phase 1.8 should add `order=newest` to the GOV.UK search API parameters in the relevant strategy, so the *first* instance of each slug is always the most recent release. Existing rows would need re-discovery to update their URLs.

### 17. Skip logging is too quiet for ongoing operations

The current log line for skipped candidates is `run_discovery: skip existing …` at DEBUG level. Railway's default INFO log level hides this, which is why the 65% skip rate appeared mysterious in the original 23 May logs.

**Implication:** Phase 1.8 should promote skip logging from DEBUG to INFO, and include the canonical name (slug) so future investigations can see exactly which series collided without needing to re-run discovery.

### 18. The `classified` metric in the run summary is misleading

The current calculation is `classified = fetched - failed_classify`, which counts both *written* and *skipped* candidates together. The metric says "didn't fail the LLM step" but reads as "passed all checks."

**Implication:** Phase 1.8 should rename `classified` to `llm_passed` (or similar) to clarify what it actually measures. This is a documentation/observability fix, not a behavioural change.

### 19. The 54 failed_classify items on DfE are not over-aggressive rejection

The investigation confirmed that the classifier's `is_publication` filter is correctly working — most of the 54 failures are legitimate rejections (summary reports, methodology notes, and similar non-publication content) plus some transient Gemini API failures. The simulation found only ~3 items where the classifier might have over-rejected.

**Implication:** the classifier prompt's filtering logic is sound. No Phase 1.8 work needed here. Worth noting for confidence — the classifier is doing the right thing.

---

## Phase 1.8 scope

Based on the findings above, Phase 1.8 covers:

### Schema and data model changes

1. ✅ **Add controlled policy_area tagging to stat publications**, mirroring the Hansard model _(shipped 2026-05-27)_
   - New table `ha_stat_publication_theme` (analogous to `ha_session_theme`)
   - Supports 1–4 `policy_area` tags from the Westminster Brief 23-area controlled vocabulary (`HANSARD_POLICY_NAMES` in `hansard_archive/policy_areas.py`) — the 1-4 range reflects Hansard's empirical 2-4 mode rather than the documented 1-3 cap (see finding 11)
   - Existing `subject_area` field retained as free-text "specifics" equivalent
   - **Distribution baseline (2026-05-27 backfill):** 1,117/1,124 publications tagged; 2,398 tag rows; 2.15 tags/publication average. Tag shape: 17.6% single-tag, 51.4% two-tag, 29.6% three-tag, 1.3% four-tag. Peak area: Welfare and benefits at 20.3% (486/2,398). 21 of 23 areas represented; `International development` and `Parliament and constitution` absent (no seeded producers in scope). Use these figures as a baseline for spotting classifier drift if the prompt is ever changed.

2. **Reconsider URL semantics**
   - Decide between single `url` field (current) and `publication_url` + `data_url` (potential)
   - Driven by what users will actually want when clicking through

3. **Producer registry simplification**
   - Audit existing producers against the gov.uk-as-primary-source insight
   - Identify which producers should be deprecated in favour of transitive discovery via gov.uk
   - Likely reduces registry from ~150 to ~25-30 active producers

### Classifier improvements

4. ✅ **Update Gemini prompt to use the Westminster Brief 23-area controlled vocabulary** _(shipped 2026-05-27)_
   - Include the 23 policy areas in the prompt with examples
   - Require classifier to choose 1–4 from the list (target 3 — the empirical mode)
   - Keep `subject_area` as free-text for nuance
   - **Important:** explicitly clarify how "Government and public administration" should be interpreted for stat publications — only used for publications with substantive content about civil service operations, machinery of government, or public administration practice. NOT used as a procedural wrapper (the Hansard usage pattern doesn't apply because stat publications have no procedural dimension). See finding 10.

5. ✅ **Backfill policy_area tags for existing candidates** _(shipped 2026-05-27)_
   - Re-classified all 1,124 candidate/authorised publications against the new vocabulary
   - 1,117 tagged (7 classify failures — all legitimate non-publication documents)

### Discovery worker improvements

6. **Add `order=newest` to GOV.UK search API calls** (from finding 16)
   - Modifies `GovUkSearchStrategy.fetch_candidates()` to request newest-first ordering
   - Ensures the first instance of each slug is the most recent release
   - Targeted one-line fix
   - Existing DfE/DWP rows will need re-discovery to refresh URLs (one-off backfill)

7. **Promote skip logging from DEBUG to INFO** (from finding 17)
   - Change `run_discovery: skip existing …` log level to INFO
   - Include the canonical name/slug in the log line for traceability
   - Makes future skip rate investigations diagnosable from Railway's default logs

8. **Rename `classified` metric to `llm_passed` in run summary** (from finding 18)
   - Cosmetic but important — current name reads as "passed all checks" when it actually means "didn't fail the LLM step"
   - Affects discovery_worker.py logging, no schema change
   - Optional: also document the metric set in the worker docstring

9. **Address remaining Phase 1.7 operational issues**
   - Rename or redesign `DISCOVERY_DRY_RUN` (current name misleads — suppresses producer state transitions only, not candidate writes)
   - The 54 classifier failures on DfE were investigated and confirmed not over-aggressive (finding 19) — no fix needed to the `is_publication` filter

### Public-facing UI

10. **Build the public catalogue page**
   - List authorised publications with filters by policy area, producer, cadence
   - Search by name, description
   - Each publication has its own detail page
   - This is the user-facing endpoint that makes Phase 1.7 + 1.8 launchable
   - Catalogue scope framing: "X publication series across Y producers" rather than "X publications" (per finding 15)

### Editorial / admin tooling

11. **Improve the review UI based on lessons from 23 May**
   - Bulk actions (decline N at once with shared reason)
   - Better filtering (show only candidates above/below confidence thresholds, if those exist)
   - Edit workflow improvements

---

## Phase 1.8 out of scope (deferred to Phase 1.9+)

- **PDF data extraction.** Phase 1.9 will build an extraction worker that produces structured content from publication PDFs (summary, tables, headline statistics). Phase 1.8 deliberately does not include this — extraction quality is the single biggest determinant of long-term product value, and it deserves dedicated focus on a clean foundation.

- **Cross-publication querying / unified data layer.** Phase 1.9's extraction will be per-publication. A unified queryable stats layer ("retail sales over time, across all publishers") is a much larger ambition, potentially Phase 3+.

- **Briefing pack integration.** The £49 stakeholder briefing pack (separately scoped in `phase-2-scoping.md`) can launch using catalogue-only inputs. When Phase 1.9 extraction is mature, the briefing pack will be enhanced to consume extracted data — but this dependency does not block Phase 2.

---

## Phase 1.9 direction (for context)

Phase 1.9 will build a per-publication extraction worker that produces:

- A text summary of what the publication says (1–2 paragraphs)
- Comprehensive structured extraction of tables within the publication
- Headline statistics and notable figures
- Link to original PDF for charts and full context

Output goes into a new `extracted_content` table (or similar). Public stat pages will then render publication metadata (Phase 1.7 + 1.8) alongside extracted content (Phase 1.9). Briefing packs consume both.

**Pilot scope:** ONS publications only initially. ONS has clean PDFs, structured templates, and well-formed tables. If extraction quality cannot be made reliable on ONS, expansion to other producers won't help.

**Quality bar:** civil servant audience expects ~95%+ accuracy. Below that, trust erodes faster than the catalogue's value compensates for. Phase 1.9 includes building validation / measurement infrastructure to know what extraction accuracy actually is.

**Cost expectations:** multimodal LLM calls on PDFs are not free. Phase 1.9 will include cost modelling to confirm extraction is sustainable at scale before committing to broad rollout.

---

## Launch sequencing

The strategic call from the 23 May review is to **launch the public catalogue before building extraction**. Reasoning:

1. Building extraction without real user behaviour is building on assumptions. Civil servants might use the catalogue in ways that change what extraction should prioritise.
2. The catalogue alone is meaningfully useful — finding government statistics is itself a real pain point.
3. Launching gives early signal on whether the controlled vocabulary, policy area tags, and producer registry simplification are working in practice.
4. Phase 1.9 extraction is a multi-week effort. Phase 1.8 launch happens sooner; extraction work can be informed by what users do with the catalogue.

Sequence:

1. **Phase 1.7 finish line** — clean up DRY_RUN naming, authorise more producers, audit DfE skip behaviour. Days, not weeks.
2. **Phase 1.8** — controlled vocabulary, policy area tagging, producer registry simplification, public catalogue page. ~3-4 weeks.
3. **Public catalogue launch** — make Westminster Brief's stats catalogue accessible to civil servants. Gather usage data via Plausible. Note qualitative feedback.
4. **Phase 1.9** — extraction pilot on ONS, informed by real launch experience. Multi-week.
5. **Phase 2** — briefing pack (catalogue-only inputs initially; consumes extracted data when Phase 1.9 is mature).

---

## Open questions for Code to consider when implementation begins

These are questions Phase 1.8 implementation will need to answer. They are not asks for immediate action.

- **`ha_stat_publication_theme` table design** — should it mirror `ha_session_theme` exactly, or are there reasons to diverge?
- **Classifier prompt structure** — best way to provide the 23 policy areas to Gemini and ensure it picks from the list rather than producing free-text?
- **Flat vs hierarchical model** — the current Westminster Brief 23-area list is flat. GOV.UK's current taxonomy is hierarchical (up to 5 levels). Phase 1.8 keeps flat by default; worth confirming this is the right call given the trade-off (flat is simpler, hierarchical enables drill-down navigation later).
- **Taxonomy drift from GOV.UK** — the Westminster Brief 23-area list is curated, not auto-synced from GOV.UK. If GOV.UK evolves its taxonomy, Westminster Brief's vocabulary won't auto-update. This is intentional (audience-tuned over GOV.UK-mirror) but worth being explicit about. Is there ever a case for tracking GOV.UK's taxonomy as a separate vocabulary?
- **Parliament and constitution as a top-level area** — this is in the Westminster Brief list but doesn't exist as a top-level taxon on GOV.UK. For stat publications specifically, what content (if any) would be tagged here? Worth thinking about during the dedicated taxonomy conversation.
- **URL semantics** — single `url` field with documented meaning, or split into `publication_url` + `data_url`? What does the existing data say about how often these differ?
- **Backfill strategy** — re-classify all existing candidates against the new vocabulary, or only newly-discovered candidates? Cost considerations?
- **Producer registry simplification** — what's the migration path from 150 producers to ~25-30? Soft-delete? Status flag? Manual audit?
- **DRY_RUN redesign** — rename to clarify intent? Change behaviour to actually suppress all writes? Remove entirely?

---

## What this document is and isn't

This is a scoping document. It identifies what Phase 1.8 covers, what it doesn't, and why. It does not specify implementation — that's for individual briefs to be drafted as work begins.

When Phase 1.8 work starts, individual implementation briefs will be drafted for each piece. This document is the reference point for "is this in scope or out?"

It is also the bridge document between today's CSV review insights and the work that follows. Future-Mark reading this in two months should be able to reconstruct why Phase 1.8 took the shape it did, and why Phase 1.9 was deliberately separated.
