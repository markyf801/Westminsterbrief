# Data Sources Integration Roadmap

Westminster Brief's long-term value proposition is cross-source intelligence: parliamentary activity alongside the statistical evidence base that underpins policy. This document captures the integration roadmap for external data sources beyond Parliament's own APIs.

Phase 1 is ONS. Subsequent phases follow the same architectural pattern once Phase 1 is established.

---

## Phase 1 — ONS Statistics API

**Brief:** Integrate the ONS Statistics API as the first cross-source statistical data layer and establish the architectural pattern for all subsequent integrations.

**Why ONS first:** Covers the widest policy surface (economic, population, labour market, regional, health, education). Open licence, no key required. Well-documented beta API. Most debates and WQs in the archive have a directly relevant ONS series.

### Reconnaissance tasks (start here)
1. Review `developer.beta.ons.gov.uk` — endpoint structure, pagination, authentication
2. Confirm: expected open/no key (verify before assuming)
3. Sample 5–10 relevant datasets across policy areas: economic indicators, population, labour market, regional data, health statistics
4. Document rate limits and OGL v3.0 attribution requirements
5. Identify dataset discovery mechanism (search by keyword? by topic taxonomy? by geography?)

### Data model (design before build)
- `ons_dataset` — dataset-level metadata (id, title, description, topic, release_date, url)
- `ons_observation` — individual data points (dataset_id, time_period, geography, value, unit, confidence_interval)
- Linking strategy: by topic taxonomy alignment? by geographic area? by keyword matching to `ha_pq_theme`/`ha_session_theme`? Design decision before schema is written.
- Versioning: when ONS revises data, keep historical snapshot — don't overwrite silently

### Ingestion pipeline
- Weekly refresh job (Railway cron)
- Phase 1 scope: ~50 datasets, ~10–50 MB total storage
- Single statistical series: ~50–500 KB
- Re-run safe (upsert by dataset_id + time_period + geography)

### Surfacing
- Theme pages (`/archive/theme/<slug>`) — optional "Related statistics" panel
  - Example: "Health & social care" debates page alongside NHS performance data from ONS
- Attribution: "Source: ONS — [dataset title]" with link, licensed under OGL v3.0
- Phase 1: display only. Pattern detection (correlation, gap analysis) is Phase 2.

### Attribution requirement
OGL v3.0: "Contains public sector information licensed under the Open Government Licence v3.0."

### Estimated effort
1–2 weeks of Code work. Not blocking Phase 2A.5 priorities. Build when convenient after Phase 2A.5 verification is complete.

---

## Phase 2 — DfE Education Evidence Service (EES)

Statistical releases from the Department for Education: school performance, GCSE and A-level results, higher education participation, apprenticeships, NEET data.

**Why:** DfE is a high-traffic department in Westminster Brief's primary user base (civil servants, HE policy). EES data gives direct context for education WQs and debates.

**API:** `explore-education-statistics.service.gov.uk/api` — public, documented, no key required.

*Prerequisite: ONS Phase 1 architectural pattern established.*

---

## Phase 3 — DWP Stat-Xplore

Benefits, employment support, Universal Credit, disability statistics.

**Why:** Welfare and labour market data is the evidence base behind a large share of WQs tabled by opposition MPs. High value for policy officers tracking DWP activity.

**API:** `stat-xplore.dwp.gov.uk` — requires free registration but open to researchers.

*Prerequisite: Phase 1 pattern.*

---

## Phase 4 — Police.uk / Home Office crime data

Crime outcomes, police workforce statistics, stop and search data — by force area and LSOA.

**Why:** Strong geographic linking potential (MP constituencies). Home Affairs is a high-WQ-volume department.

**API:** `data.police.uk/api` — open, no key.

*Prerequisite: Phase 1 pattern.*

---

## Phase 5 — MoJ Criminal Justice Statistics

Court outcomes, prison population, probation. Covers Home Secretary and Justice Secretary accountability.

**Why:** Justice WQs are high-volume and data-heavy. MoJ publishes machine-readable stats through GOV.UK.

**Source:** GOV.UK statistical bulletins via the Statistics API or direct CSV. Less well-structured than ONS — may require more custom parsing.

*Prerequisite: Phase 1 pattern established and at least one subsequent integration (Phase 2 or 3) validated.*

---

## Architectural principles (apply from Phase 1)

1. **Separate storage from surfacing.** Ingestion jobs write to `ons_*` (or equivalent) tables. Display logic reads from those tables. No inline API calls in templates.
2. **Attribution at the data layer.** Store the attribution string and source URL alongside the data in the DB. Never display data without its provenance.
3. **OGL/OPL compliance.** All government statistical data is OGL v3.0. Carry the licence term through to the display layer.
4. **Upsert-safe ingestion.** All ingestors must be idempotent — re-running does not create duplicates.
5. **Versioning.** When a statistical series is revised, keep the prior snapshot. Don't silently overwrite.
6. **Phase 1 is integration only.** Pattern detection, correlation analysis, gap identification — all Phase 2 analytical work. Phase 1 just gets data in and displayed.

---

*Roadmap created 13 May 2026. Phase 1 ready to build — not blocking current Phase 2A.5 work.*
