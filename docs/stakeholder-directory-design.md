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
| `area` | string(100), not null | From `policy_areas` vocab (guarded at ORM level until vocab populated). Organisation's claimed policy footprint |

Unique constraint on (`organisation_id`, `area`).

Distinct from `engagement.policy_area`: this is the organisation's overall footprint; engagement.policy_area is the area of a specific event.

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

**Tier 3 — flag for human review.** Similarity 70–90%, OR similarity > 90% without matching identifier. Raises `possible_duplicate` flag with AI-generated rationale in `detail`.

**Tier 4 — treat as distinct.** Similarity < 70% and no matching identifiers.

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

---

**End of design document.**

Further prompts will be issued one at a time. Each references this document. Do not implement anything that contradicts it without raising the conflict first.
