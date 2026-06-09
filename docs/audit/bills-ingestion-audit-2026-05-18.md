# Bills Ingestion Audit — Westminster Brief

**Date:** 18 May 2026  
**Codebase:** westminsterbrief.co.uk (Phase 2A)  
**Auditor:** Claude Code  

---

## Executive Summary

Bills ingestion in Westminster Brief exists **as a planned Phase 2A.5 feature** with architectural decisions locked but **no implementation yet built in production**. The Hansard Archive (Phase 2A) is live and fully ingesting parliamentary debates; bills-related functionality is explicitly deferred to Phase 2A.5 as a post-launch enhancement.

**Current state:** Factual bill status (phase progression) is not yet in the database. The only bill-related functionality live is session-to-bill **title-matching** on the session detail page, which links related debate stages via normalized title comparison (e.g., "Finance Bill" stage 1 to "Finance Bill" stage 2).

---

## Finding 1: Bills Ingestion Status

### Does bills ingestion exist in any form?

**No—not yet built.** Bills functionality is documented and scoped but deferred.

**Evidence:**
- CLAUDE.md explicitly marks Bills as "Not yet ingested" in the external linking table
- docs/phase2a-hansard-archive.md confirms bill journey display is a Phase 2A.5 candidate, not Phase 2A scope
- No Bills class in database schema (hansard_archive/models.py); only Hansard, PQ, and theme tables exist
- Database query confirms zero tables matching bill% or legislation% across public schema
- No API keys in .env for bills.parliament.uk

**What exists today (title-matching):**
- hansard_archive/views.py contains _related_sessions() function that normalizes session titles and finds related debate stages
- Works for current corpus because Hansard debate titles are highly consistent
- Documented failure modes: titled amendments, prefix collisions on short titles
- This is a workaround, not a bill integration—no Bills API connection

### Strategic intent (locked decisions)

From docs/phase2a-hansard-archive.md:

Phase 2A.5 planned build:
- Integrate Parliament Bills API (bills.parliament.uk)
- Add bill_id and stage_type columns to ha_session schema
- Backfill existing bill-related sessions with bill_id by matching against Bills API
- Update bill-type session pages with full journey display
- Estimated effort: 5–7 days

---

## Finding 2: Data Population Check

### Is data in the database? (row count, date range, freshness)

**No bills data exists.** Only Hansard, Written Questions, and Parliamentary Debate metadata.

Database schema summary (as of 18 May 2026):

| Table | Purpose | Row Count |
|---|---|---|
| ha_session | Hansard debate sessions | 4,414 |
| ha_contribution | Individual speeches | 120,000+ |
| ha_session_theme | AI-generated theme tags | 25,000+ |
| ha_pq | Written Parliamentary Questions | 90,000+ |
| Bills tables | NONE | 0 |

**Data freshness (Hansard):**
- 12-month backfill complete: 4,414 sessions (30 April 2026)
- Cron ingestion active: rolling daily ingest via scripts/archive_cron.py
- As of 18 May 2026: actively ingesting today's debates

**Data freshness (Bills):**
- Does not exist

---

## Finding 3: Tagging Coverage

### Are bills tagged to policy areas?

**No.** Bills are not in the system at all.

**What IS tagged:**
- Hansard sessions: 3,282/3,687 non-container sessions tagged (90.3% coverage)
- Each session has 1–3 policy area tags + 1–5 specific topic phrases
- All 23 GOV.UK controlled vocabulary terms in use
- Tagging model: Gemini Flash-Lite
- Tagger: hansard_archive/tagger.py with batch runner scripts/run_tagging.py

Bills do not appear in any tagging scope because they are not ingested.

---

## Finding 4: Ingestion Job Status

### Is an ingest job scheduled?

**No bills ingest job exists.**

**Cron jobs currently scheduled:**
- scripts/archive_cron.py — Hansard debate ingestion (daily rolling)
- scripts/ingest_pq_cron.py — Written Questions ingestion
- scripts/committee_evidence_cron.py — Committee evidence ingestion
- No bills_cron.py or equivalent

Cron services are managed per-service in Railway UI (not in railway.toml). Bills cron is not configured.

---

## Finding 5: Recommendation

### Case matching: Phase 2A.5 candidate — design locked, build not started

This matches Case 3 with a forward path: Schema and architectural decisions are locked, Bills API integration is documented and planned as Phase 2A.5 (first post-launch priority), but no production code has been written.

**Recommendation:**

Build bills ingestion as Phase 2A.5 (5–7 days estimated), immediately post-launch. The pathway is clear and the value is high:

**Why now (vs later):**
- Bills API is Parliament's most stable/reliable API
- Phase 2A title-matching workaround is increasingly insufficient as archive scales
- Bill-related sessions are already being ingested via Hansard API
- Phase 2B (alert subscription) will benefit from bill stage progression data
- SEO value: bill stage pages will rank for "Finance Bill second reading" queries

**Build sequence (5–7 days):**
1. Day 1: Spike on Bills API (endpoints, rate limits, bill_id discovery, stage type enum, title-match reliability)
2. Day 2–3: Add bill_id and stage_type columns to ha_session; write backfill script to match existing sessions
3. Day 4: Add Bills API call to ingestion pipeline so new sessions populate bill_id automatically
4. Day 5–6: Update session detail template to display full bill journey with stage progression
5. Day 7: Testing, backfill verification, cron integration

**Success metrics:**
- All bill-type sessions have bill_id populated
- New bill-type sessions automatically enrich on ingest
- Session detail pages show bill journey when available
- Title-match panel replaced with bill_id-based linkage
- Performance impact: <50ms per Bills API call

**Risks & mitigations:**
- Risk: Bills API title-matching unreliable. Mitigation: Spike day 1 tests on sample of 20–30 real bills.
- Risk: Bills API rate limits/availability. Mitigation: Hansard API is primary; bills is enrichment-only.
- Risk: Scope creep into amendment tracking. Mitigation: Phase 2A.5 is factual only (stage names, dates, status).

---

## Summary Table

| Aspect | Status | Notes |
|---|---|---|
| Bills table in schema | No | Planned as Phase 2A.5 |
| Bills data in database | 0 rows | No ingestion built |
| Bills API integration | Not built | Spike planned; design complete |
| Title-matching fallback | Working | Functional but will be replaced |
| Bills cron job | Not scheduled | Will be added in Phase 2A.5 |
| Bills tagged to policy areas | N/A | Bills not in system |
| Architectural clarity | High | Locked decisions in docs |
| Path forward | Clear | Phase 2A.5 first priority post-launch |
| Effort estimate | 5–7 days | Clear scope |

---

## Conclusion

Westminster Brief's bills functionality is a **well-scoped Phase 2A.5 feature with architectural decisions locked and a clear build path**, not a gap or silent failure. The free Hansard archive (Phase 2A) is live and actively ingesting; bills enrichment is deferred to the first post-launch priority because Phase 2A's soft-launch framing makes bill journey display a "nice-to-have" rather than blocking.

**Recommendation:** Proceed with Phase 2A.5 build immediately post-launch. No blockers exist.

---

**Audit conducted:** 18 May 2026  
**Next review:** Recommended after Phase 2A.5 build completion
