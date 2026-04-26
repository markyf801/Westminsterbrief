# Stakeholder Directory — Design Document and Build Plan

**Status:** Foundation phase complete. Schema and models built; vocabulary sync migration and runtime guards in place.
**Owner:** Mark
**Last updated:** 24 April 2026

This document is the single source of truth for the stakeholder directory project. Every build prompt references it. Do not implement anything that contradicts the principles or schema below. If the document appears to conflict with a prompt, stop and raise the conflict rather than guessing.

---

## 1. Purpose

A directory of UK organisations that engage with government and Parliament on policy, built from source evidence: consultations, committee evidence, ministerial meetings, APPG data, lobbying register, parliamentary citations. The directory captures existence and engagement; it does not assign importance. Users apply their own precedence at query time via filters and (for power users) tunable scoring weights.

The directory is an internal data layer for Westminster Brief. It is not a public API product.

---

## 2. Design Principles

These are load-bearing. Every subsequent decision should be traceable back to them.

**2.1 Evidence, not editorial judgement.**
The tool captures engagement as fact. It does not rank organisations by "importance," "significance," or "tier." Relevance is computed at query time from engagement data and user-supplied query parameters.

**2.2 Generous inclusion for organisations.**
Any organisation with at least one recorded engagement is in the directory. Registration status is a filterable flag, not an inclusion gate. Unincorporated bodies, coalitions, and informal campaign groups are included.

**2.3 Gated inclusion for individuals.**
Individuals are included only if they hold a formal role: named author/chair of a government-commissioned review, APPG officer role, named expert witness, advisory group member, or expert panel member. One-off individual consultation responses are not captured.

**2.4 Historical preservation with visibility control.**
Closed or merged organisations remain in the database with `status = closed_or_merged`. Hidden from default views; users can toggle `include_historical = true` at query time. Closure is a filter, not a deletion.

**2.5 Stable type weights, decaying engagement records.**
Engagement types have fixed weights in config. These are the same for every organisation. Individual engagement records decay with recency. Organisations rise and fall in rankings as their engagement record evolves.

**2.6 Controlled vocabularies, configuration-driven.**
All enum-like fields (source types, org types, flag types, departments, policy areas) validate against YAML config files. Adding a value is a config change, not a schema migration.

**2.7 No AI confidence as threshold for destructive operations.**
Destructive operations (merging organisation records) use mechanical rules: exact-match after normalisation, or string similarity combined with matching external identifiers. AI self-reported confidence does not gate merges.

**2.8 Umbrella submissions captured at umbrella level only.**
When a membership body submits on behalf of members, the engagement records against the umbrella. Members' implicit participation is not recorded as separate engagements. Known limitation.

**2.9 External stakeholders only.**
Ministerial meetings with core government departments are excluded. Arm's-length bodies (Ofsted, OfS, Research England) are included as stakeholders. Core department name variants listed in `config/internal_government.yaml`.

**2.10 Every URL is validated.**
Every ingester must call the shared `validate_url_or_flag` utility on any URL it records. Hard rule.

---

## 3. Scope

**Departments supported at launch:** All 24 UK ministerial departments (vocabulary-wide). Ingesters built department-by-department, starting with DfE as pilot.

**Policy areas:** Cross-government taxonomy, defined upfront in `config/policy_areas.yaml`. *[TBD — to be populated before ingester code is written. Schema supports it from day one.]*

**Time window:** Engagement records from 2019 onwards (five most recent complete years, rolling). Earlier records captured only if material.

---

## 4. Schema

All tables live in the SQLAlchemy module `stakeholder_directory/`. Uses the existing `db` object from the main Flask app.

### 4.1 `organisation`

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `canonical_name` | string(300), indexed, not null | |
| `canonical_url` | string(500), nullable | |
| `description` | text, nullable | One-sentence description from enrichment |
| `type` | string(50), not null | From `org_types` vocab. One primary type |
| `scope` | string(30), not null | From `scope` vocab |
| `status` | string(30), not null, default `'active'` | From `status` vocab |
| `registration_status` | string(50), nullable | From `registration_status` vocab |
| `registration_number` | string(50), nullable | Charity Commission or Companies House number |
| `last_verified` | date, nullable | |
| `created_at` | datetime, not null | |
| `updated_at` | datetime, not null | |

### 4.2 `alias`

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `organisation_id` | fk → `organisation.id`, indexed, not null | |
| `alias_name` | string(300), not null | |
| `source` | string(100), not null | Which ingester or manual entry added this alias |

Unique constraint on (`organisation_id`, `alias_name`).

### 4.3 `engagement`

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `organisation_id` | fk → `organisation.id`, indexed, not null | |
| `source_type` | string(50), not null | From `source_types` vocab |
| `source_url` | string(500), not null | |
| `department` | string(50), nullable | From `departments` vocab (guarded at ORM level until vocab populated) |
| `policy_area` | string(100), nullable | From `policy_areas` vocab (guarded at ORM level until vocab populated). The area of this specific engagement event |
| `engagement_date` | date, indexed, not null | |
| `evidence_url` | string(500), nullable | Link to specific evidence document |
| `inquiry_id` | string(200), nullable | Links written + oral evidence records for the same committee inquiry |
| `cited_in_outcome` | boolean, not null, default false | Set during citation-extraction pass |
| `engagement_depth` | string(50), nullable | Reserved for future scoring refinements |
| `ingested_at` | datetime, not null | |
| `ingester_source` | string(100), not null | Which ingester created this record |

### 4.4 `policy_area_tag`

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `organisation_id` | fk → `organisation.id`, indexed, not null | |
| `area` | string(100), not null | From `policy_areas` vocab |

Additional columns (`derived_from_sources`, `source_evidence`, `confidence`, `first_derived_at`, `last_updated`) are added during the enrichment phase. See Section 10 for full schema and derivation logic.

Distinct from `engagement.policy_area`: this is the organisation's overall footprint; engagement.policy_area is the area of a specific event.

The foundation phase (Prompts 1–5) creates the table with the three columns above only. Enrichment columns are added via migration when the enrichment prompt is built.

### 4.5 `flag`

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `organisation_id` | fk → `organisation.id`, indexed, not null | |
| `engagement_id` | fk → `engagement.id`, nullable | Some flags attach to orgs, some to engagements. **Relationship accessor is `flag.engagement_ref`, not `flag.engagement`** due to FK-column/relationship naming collision |
| `flag_type` | string(50), not null | From `flag_types` vocab |
| `detail` | text, not null | Human-readable explanation |
| `raised_at` | datetime, not null | |
| `raised_by` | string(100), not null | Which pipeline step raised it |
| `resolved` | boolean, not null, default false | |
| `resolved_at` | datetime, nullable | |

### 4.6 Hard constraints

- **No `importance`, `tier`, `priority`, `weight`, or `rank` fields on `organisation`.** These are query-time computations, never stored.
- **No free-text enum fields.** Every populated enum validates against the corresponding YAML config via DB-level CHECK constraint.
- **Deferred-vocab columns** (`department`, `policy_area`, `area`) use ORM-level `@validates` guard that raises `VocabularyNotReadyError` until YAML is populated. Ingesters must use the ORM, not raw SQL, to ensure the guard fires.
- **Do not modify** existing `StakeholderOrg`, `TrackedStakeholder`, `User`, or any other existing table.
- **No caching, Celery tasks, or background workers** in the foundation phase.

---

## 5. Controlled Vocabularies

### 5.1 `org_types` (15 values)

| Value | Description |
|---|---|
| `membership_body` | Represents member organisations (UUK, Russell Group) |
| `professional_body` | Represents individual professionals (RCGP, BMA) |
| `trade_association` | Represents commercial firms in an industry |
| `union` | Trade union |
| `think_tank_or_research` | Policy research, with or without advocacy posture |
| `academic_body` | University, college, research council |
| `learned_society` | Academic or professional society |
| `charity` | Charitable organisation not primarily a think tank or campaign group |
| `campaign_group` | Advocacy-led, may or may not be incorporated |
| `public_body` | Arm's-length body, NDPB (Ofsted, OfS) |
| `regulator` | Specifically regulatory body |
| `government_review` | Commissioned review (Augar, Timpson, Casey) — distinct from standing bodies |
| `private_company` | Commercial firm engaging as itself |
| `consultancy` | Professional services firm, often representing clients |
| `individual_expert` | Named individual with a formal role (see principle 2.3) |

**Tiebreaker rule (soft guideline):** Where an organisation genuinely fits two types, choose the one describing its primary functional role over its legal or structural form. Example: IFS is `think_tank_or_research` (functional) rather than `charity` (structural). Document contested cases.

### 5.2 `source_types` (11 values)

| Value | Description | Default weight |
|---|---|---|
| `oral_evidence_committee` | Gave oral evidence to a select committee | 4.0 |
| `government_review_role` | Chair, panel member, or secretariat of a commissioned review | 3.5 |
| `ministerial_meeting` | Met a minister (from transparency data) | 3.0 |
| `appg_secretariat` | Serves as secretariat to an APPG | 2.5 |
| `advisory_group_member` | Member of a departmental advisory group | 2.5 |
| `expert_panel_member` | Member of a formal expert panel | 2.5 |
| `appg_officer` | Holds an officer role on an APPG | 2.0 |
| `cited_in_parliamentary_record` | Named in a parliamentary debate or committee report | 1.5 |
| `consultation_response` | Responded to a government consultation | 1.0 |
| `written_evidence_committee` | Submitted written evidence to a select committee | 1.0 |
| `lobbying_register` | Appears on register of consultant lobbyists | 0.5 |

### 5.3 `scope` (4 values)

`national`, `local`, `international`, `consultancy`

### 5.4 `status` (3 values)

`active`, `possibly_dormant`, `closed_or_merged`

### 5.5 `registration_status` (6 values)

`registered_charity`, `registered_company`, `public_body`, `unincorporated`, `government_review`, `unknown`

### 5.6 `flag_types` (10 values)

| Value | Description |
|---|---|
| `url_dead` | URL returns 404 or connection error |
| `url_parked` | URL resolves but is a parked domain |
| `engagement_stale` | No engagements recorded in >3 years |
| `possible_duplicate` | Name or URL similarity with another record, below auto-merge threshold |
| `status_dissolved` | Companies House shows dissolved status |
| `status_merged` | Confirmed merged with another entity |
| `name_ambiguous` | AI normalisation could not confidently resolve canonical form |
| `description_unverified` | Description derived from AI training data, not source fetch |
| `individual_possibly_institutional` | Individual appears to be acting in institutional capacity; review suggested |
| `potentially_same_as_other_org` | Specific duplicate candidate flagged with target org id in detail |

### 5.7 `departments`

All UK ministerial departments (24). Exact list in `config/departments.yaml`. *[To be populated from gov.uk ministerial departments list before first ingester.]*

### 5.8 `policy_areas`

*[TBD — cross-government taxonomy to be drafted separately before ingester code is written. Will be defined in `config/policy_areas.yaml` with one-line scope notes per area. Foundation phase does not require this to be populated; schema supports the column via deferred-vocab guard.]*

---

## 6. Deduplication Rules

Applied in the name-normalisation pass after ingestion. Not required in foundation phase.

**Tier 1 — auto-merge (mechanical).** Exact match after normalisation (strip punctuation, standardise case/whitespace, strip legal suffixes, expand known aliases from `config/aliases.yaml`).

**Tier 2 — auto-merge (corroborated similarity).** Similarity > 90% AND at least one matching corroborator: same canonical URL, same Charity Commission number, or same Companies House number.

**Tier 3 — flag for human review.** Similarity ≥ 90% without matching identifier. Raises `possible_duplicate` flag with rationale in `detail`.

*Note: threshold raised from 70% → 85% after first real-data run (DfE ministerial meetings, 2025): education-sector org names sharing tokens ("Association", "National", "Schools") produced a 52% false-positive flag rate at 70%.*

*Raised further from 85% → 90% after second real-data run. At 85%, 208 flags were produced from 708 staging rows, with notable false positives (NAHT scoring 0.93 against NASUWT; Sixth Form Colleges Association scoring 0.93 against Association of Colleges). At 90%, only similarity ≥ 0.90 without corroborator triggers a flag — the same boundary as Tier 2, so the tiers are now: sim ≥ 0.90 + corroborator → merge; sim ≥ 0.90, no corroborator → flag; sim < 0.90 → new org. May revisit further if the queue still has a high false-positive rate.*

**Tier 4 — treat as distinct.** Similarity < 90% and no matching identifiers.

*Bug fix (May 2026) — in-batch Tier 3 indexing:* Tier 3 orgs were appended to the `orgs` list but not to the `norm_to_org` dict. A second occurrence of the same raw name within the same normalisation batch missed the Tier 1 exact match, scored 1.0 similarity against the Tier 3 org already created, and created a duplicate `canonical_name` record. Fix: `_do_tier3` now immediately indexes the new org in `norm_to_org` by normalised name so all subsequent appearances in the same batch hit Tier 1. Manifestation: 23 duplicate canonical_names found by `audit.check_count_invariants` after a 4-quarter DfE run; resolved to 0 after the fix.*

**Distinct-org pairs:** Some pairs of organisations have textual similarity above the Tier 3 threshold but are genuinely distinct (e.g. NAHT vs NASUWT — both teaching unions, similar names, different orgs). These pairs are listed in `config/distinct_orgs.yaml` and excluded from the dedup process entirely: when the best similarity candidate is found to be a member of a known-distinct pair with the staging name, the row is treated as Tier 4 (new organisation) rather than Tier 3. Entries in the YAML can use canonical names or known aliases (e.g. "NASUWT" expands via `config/aliases.yaml`). Adding to this file is a manual operation, typically done during human review of the flag queue when a reviewer confirms two orgs are distinct.

**Safety rule:** Never auto-merge if both candidates have independent engagement records on different dates with different source URLs, even if similarity and identifiers match.

---

## 7. URL Validation

Every ingester must call `validate_url_or_flag(url, org_id, engagement_id)` on any URL it records. The module `stakeholder_directory/url_validator.py` is scaffolded as a stub during the foundation phase; full implementation follows when ingesters are built.

Validator behaviour (for full implementation):
- HEAD request, 5-second timeout
- Shared worker pool, 10 concurrent max
- Per-host rate limit: 5 requests/second max to a single host
- On 404, timeout, SSL error, or connection refused: raise `url_dead` flag, still create the record
- On parked-domain signatures: raise `url_parked` flag
- Log validation result to `url_validation_log` table (reserved for future revalidation passes)

**Source URL validation (to be added to full implementation).** The validator currently targets `canonical_url` (organisation website) and `evidence_url` fields. When the full implementation is built, it must also be invoked on `source_url` values at ingestion time. `source_url` is the audit trail link — it must resolve to the actual source publication (a GOV.UK page, Parliament committee page, or ORCL register). A non-200 response on `source_url` should raise a `url_dead` flag on the engagement record in the same way as for other URLs. This was not caught during the DfE pilot ingest because the stub always returns `reachable=True`: the committee evidence ingester stored `/oralevidence/{id}/` (missing `/html/` suffix) and the ministerial meetings ingester was passed placeholder collection URLs — both silently stored without validation. The real implementation must catch these at ingestion time rather than requiring manual post-hoc discovery.

---

## 8. Scoring

Not required in foundation phase. Specified here for completeness because the engagement table shape must support it.

### 8.1 Default weights (`config/weights.yaml`)

```yaml
source_type_weights:
  oral_evidence_committee: 4.0
  government_review_role: 3.5
  ministerial_meeting: 3.0
  appg_secretariat: 2.5
  advisory_group_member: 2.5
  expert_panel_member: 2.5
  appg_officer: 2.0
  cited_in_parliamentary_record: 1.5
  written_evidence_committee: 1.0
  consultation_response: 1.0
  lobbying_register: 0.5

cited_in_outcome_bonus: 1.5
recency_half_life_days: 1825   # 60 months
policy_area_match_multiplier: 2.0
department_match_multiplier: 1.5
```

### 8.2 Computation

For a given query `(policy_area, department, recency_window)`, an organisation's score is the sum over its engagement records of:

```
engagement_score =
    source_type_weight
  × recency_decay(engagement_date, half_life_days)
  × (1 + cited_in_outcome_bonus if engagement.cited_in_outcome else 1)
  × (policy_area_match_multiplier if engagement.policy_area == query.policy_area else 1)
  × (department_match_multiplier if engagement.department == query.department else 1)
```

`recency_decay` is exponential: `0.5 ** (days_since_engagement / half_life_days)`.

### 8.3 User tuning

Weights and half-life tunable in a hidden admin panel (power-user mode). Tuned queries labelled clearly. Default-user UI controls: date range, policy area, department, engagement type filter, show/hide historical, show/hide flagged. Filters do not modify weights; they filter which engagements are in scope.

---

## 9. Known Limitations

1. Umbrella submissions are captured at umbrella level only.
2. Individuals included only if holding formal roles; one-off individual responses not captured.
3. Policy area taxonomy is cross-government but may require revision as new departments are ingested.
4. Cited-in-outcome extraction depends on AI processing; accuracy subject to human review.
5. 60-month recency half-life privileges sustained historical engagement; if users report stale stakeholders ranking too high, shorten the half-life first.
6. Deferred-vocab columns (`department`, `policy_area`, `area`) are ORM-guarded, not DB-constrained. Raw-SQL inserts bypass validation. Ingesters must use the ORM.
7. `vocab.py` loads vocabulary values at module import time; reloading `vocab.py` without reloading `models.py` in tests will produce stale behaviour. Test vocab changes in fresh processes.
8. On SQLite, `--sync-vocab` rebuilds the entire table for any single constraint drift. Becomes expensive at 100k+ rows. Not an issue on Postgres (production).
9. **Individual-researcher inflation (FairGo CIC pattern):** Individual researchers who use a CIC, consultancy, or sole-trader name as a vehicle for personal policy engagement (e.g. "FairGo CIC" — a single individual making frequent written submissions across PAC inquiries on unrelated topics) appear in the directory as high-engagement organisations. This is correct ingestion of the source data: the API reports the org name, not the individual. The enrichment pass should detect this pattern (single author across many topics, low institutional signal) and reclassify as `individual_expert` or downweight in scoring. Until then, expect 5–15 such "inflated" entries in the directory at any time. They are visible in audit queries: look for organisations where all engagements share the same `attendee_role` value and span many unrelated `inquiry_title` values. Tier 3 flag review (Section 6) is a periodic task — not blocking on each ingester prompt. 442 flags after two ingesters is expected; review every 3–6 months or before any public-facing ranking feature is launched.
10. **Concurrent normaliser runs corrupt SQLite databases.** The normaliser builds an in-memory org index (`norm_to_org`) from the database at the start of each call. If two normaliser processes run simultaneously against the same SQLite file, each reads a different snapshot of the database and independently creates `Organisation` rows with the same `canonical_name`, producing duplicate organisations and orphaned `Engagement` records with no corresponding committed staging row. The `check_count_invariants` audit catches both symptoms (`duplicate_canonical_names > 0` and `committed_staging != engagement_count`). Fix by deleting the affected database and re-running the full pipeline sequentially. This is a SQLite-only concern: PostgreSQL (production) serialises concurrent writers via row-level locking and will fail fast rather than silently corrupting data. **Operational rule: never run two normaliser instances against the same database simultaneously.**

11. **Ministerial meeting `source_url` slug pattern (DfE).** Ministerial meeting source URLs follow the gov.uk slug pattern `https://www.gov.uk/government/publications/dfe-ministerial-overseas-travel-and-meetings-{period}-{year}` where `{period}` is `january-to-march`, `april-to-june`, `july-to-september`, or `october-to-december`. The DfE Q1–Q4 2025 records have been migrated to use these correct publication URLs. Future ingestions must pass the resolved publication URL (not a collection or placeholder URL) when calling `ingest_ministerial_meetings()`; `verify_normalisation.py` has been updated with `QUARTERLY_URLS` to this pattern. **Slug conventions vary across government** — verify each department's URL pattern before ingesting; do not assume the DfE slug applies elsewhere. Note: one record (id=225) carries a malformed engagement_date of `2029-05-01` — a CSV parse error from the source data; it has been assigned the Q2 2025 URL since it originated from that quarterly file.

12. **Lobbying register `source_url` values point at a generic landing page.** All 1,876 lobbying register records share the URL `https://www.lobbying.co.uk/search-the-register`, which resolves (200) but lands on a generic search page rather than the specific quarterly return. The ORCL register has quarter-specific pages at `https://registrarofconsultantlobbyists.org.uk/public-search/{quarter_code}/` (e.g. `QR20221` for Q4 2022). Future enhancement: derive `source_url` from the `quarter` field on each staging row using this pattern rather than storing a fixed landing page. Not blocking — users can navigate from the landing page — but imprecise for an audit trail.

---

## 10. Policy Area Tag Derivation

The directory's primary user-facing query is filter-by-interest: "show me organisations working on [policy area]". This is answered from the `policy_area_tag` table, which is populated during the enrichment pass from multiple sources.

### Sources

Tags accumulate from up to five sources per organisation:

1. **website_self_description** — derived from website content via AI classification against the `policy_areas` vocabulary. Confidence varies based on AI confidence and amount of content fetched.

2. **minister_portfolio** — derived from the ministers an organisation has met. Each `ministerial_meeting` engagement looks up the minister's portfolio at the date of the meeting and adds the implied policy areas as tags. Requires the `sd_minister_portfolio` reference table.

3. **committee_topic** — derived from select committee evidence engagements. Each committee has an implied topic scope; an org that gave evidence to the Education Committee gets education-related tags. Requires a committee→policy_area mapping (added in the committee evidence ingester prompt).

4. **appg_topic** — derived from APPG roles (secretariat, officer). APPG titles encode topics directly. Requires an APPG→policy_area mapping (added in the APPG ingester prompt).

5. **consultation_topic** — derived from consultation response engagements. Each consultation has an explicit policy topic captured at ingest time.

### Schema

`policy_area_tag` table (extending the foundation schema in Section 4.4):

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `organisation_id` | fk → `organisation.id`, indexed, not null | |
| `area` | string(100), not null | From `policy_areas` vocab |
| `derived_from_sources` | JSON | List of source names that have supported this tag |
| `source_evidence` | JSON | List of `{source, detail}` for audit (e.g. minister name + meeting date, or website URL) |
| `confidence` | string(20) | `high` / `medium` / `low` — derived from source confidence and number of supporting sources |
| `first_derived_at` | datetime | |
| `last_updated` | datetime | |

The same `(org, area)` pair appears at most once per organisation. When a new source supports an existing tag, the source name is appended to `derived_from_sources` and `source_evidence` rather than creating a new row. Confidence is recomputed when sources are added.

### Minister portfolio reference table

New table `sd_minister_portfolio`:

| Column | Type | Notes |
|---|---|---|
| `id` | integer, pk | |
| `member_id` | integer, indexed | Parliament member ID |
| `name` | string(200) | Display name |
| `department` | string(50) | From `departments` vocab |
| `role` | string(200) | E.g. "Minister of State for Skills" |
| `portfolio_areas` | JSON | List of `policy_areas` keys this role covers |
| `coverage_breadth` | string(20) | `specific` (named portfolio only) / `departmental` (covers whole dept e.g. Lords spokespersons) / `all_department` (SoS-level) |
| `start_date` | date | |
| `end_date` | date, nullable | Null for current incumbent |
| `source_url` | string(500) | Reference URL on gov.uk |

Time-aware to handle reshuffles. Meetings reference the portfolio active on the meeting date by looking up records where `start_date <= meeting_date AND (end_date IS NULL OR end_date >= meeting_date)`.

Populated manually for v1 (probably 30–50 records covering current and recent ministers across pilot departments). A small ingester from gov.uk ministerial responsibility pages may follow if maintenance becomes burdensome.

### Confidence and edge cases

- **SoS portfolios** use `coverage_breadth='all_department'`. Meetings with the SoS don't narrow policy area — they only confirm departmental relevance. Inference produces broad tags (every active policy area in that department) with `confidence='low'`.

- **Lords coverage**. Lords ministers often cover broader department in Lords debates than their formal portfolio. Flagged via `coverage_breadth='departmental'`. Inference produces departmental tags with `confidence='medium'` rather than `confidence='high'`.

- **Substitute ministers** (covering for absence). If the attending minister at a meeting has no portfolio match to the meeting's department, the inference layer skips that meeting rather than producing wrong tags. Better to under-tag than to mis-tag.

- **Multi-portfolio ministers**. Some ministers carry portfolios across multiple departments (rare but happens — e.g. Cabinet Office roles spanning constitutional and machinery-of-government work). The schema supports this via the `portfolio_areas` JSON field; inference uses the relevant subset based on the meeting's department.

---

## 11. Future: Automated Source Data Updates

### Motivation

Ministerial meetings transparency data is published quarterly by each department on GOV.UK. Manual download and re-run works for prototyping but is not sustainable for a growing directory covering multiple departments and source types. This section describes the automation path.

### Source data characteristics

| Property | Value |
|---|---|
| Cadence | Quarterly per department (~90–120 days per cycle) |
| Publication lag | Typically 2–3 months after the quarter ends |
| URL pattern | Consistent per department but changes each quarter (e.g. `/q2-2025`) |
| Format | CSV, Excel (.xlsx), or HTML table depending on department; not standardised |
| Discovery | GOV.UK `/government/collections/{dept}-ministers-transparency-data` collection pages |

### Automation architecture (v1)

A lightweight scheduler, run as a Railway cron job or periodic task, checks each tracked department's GOV.UK collection page and downloads any quarterly files not already in the database.

```
scheduler.py  (or a management command)
  for each (dept, collection_url) in tracked_departments:
      new_files = discover_new_quarterly_files(collection_url, already_seen=source_files_log)
      for file_url in new_files:
          local_path = download_csv(file_url, downloads/dept_name/)
          run_pipeline([(local_path, file_url)], dept, app)
```

The `sd_ingestion_run.source_files` JSON column already records which URLs have been processed, making idempotency checks straightforward.

### Discovery strategy

GOV.UK collection pages list attached documents with structured HTML. A simple `requests` + `BeautifulSoup` scraper can extract CSV/Excel download links. The collection page URL and the expected URL slug pattern per department are stored in configuration.

A new config section in `config/departments.yaml` (or a separate `config/tracked_sources.yaml`) records:
- `collection_url` — GOV.UK collection page for ministerial meetings
- `file_pattern` — regex or glob to match meeting CSV attachments
- `download_dir` — local subdirectory under `downloads/`

### Download handling

- GOV.UK files are stable once published — no re-download needed if the URL has been seen before
- Excel files (`.xlsx`) require `openpyxl`; the ingester's `_read_csv` helper should be extended to handle them, writing normalised rows to a temp CSV before staging
- PDF files are out of scope for v1; flagged in `sd_ingestion_run.errors` if encountered

### Scheduling

Railway supports cron-style triggers via the `railway run` or a separate Railway service configured as a worker. The recommended cadence is **weekly** — this catches new quarterly publications promptly without hammering GOV.UK.

The scheduler must be idempotent: re-running against already-ingested files produces no new rows (guaranteed by the existing `_write_to_staging` duplicate-key check and normaliser idempotency confirmed in `test_end_to_end.py`).

### Out of scope for v1

- Automatic Excel-to-CSV conversion (use pandas or openpyxl; defer until first dept publishes only in Excel)
- Change detection within a quarterly file after initial publication (GOV.UK files are static once published)
- Non-ministerial-meetings source types (consultations, committee evidence) — handled by future ingesters with their own discovery logic

---

**End of design document.**

Further prompts will be issued one at a time. Each references this document. Do not implement anything that contradicts it without raising the conflict first.
