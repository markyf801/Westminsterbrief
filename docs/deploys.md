# Production Deploy Log

Append-only ledger of production-touching operations.

**Push entries** — every `git push` to `master`. Railway auto-deploys on every
push; each entry here is a production deployment.

**SQL entries** — every statement run directly against production Postgres,
whether via Railway Query console, psql, or a script using the production
DATABASE_URL. Full statements are in the "Production SQL log" section below.

Update this file as part of every commit that touches production.
**Never push to master without adding an entry** (see exception for emergency
reverts in the operational logging section of CLAUDE.md).

Beta-only pushes (branch `beta`, not yet merged to master) are not logged here.

> **Retroactive entries (2026-05-19 to 2026-05-25):** Push boundaries are
> approximate — exact git push sessions were not captured at the time. SHAs
> are the last commit in each logical group. Going-forward entries are exact.

---

## Push log

| Date       | SHA       | Summary                                                                                      | Operator |
|------------|-----------|----------------------------------------------------------------------------------------------|----------|
| 2026-05-19 | `9a205b5` | Merge from beta: bills module, related panel, front-door restructure _(retroactive)_         | Mark     |
| 2026-05-19 | `31915d6` | Follow-up fixes: browse cards, WQ tab strip, tile labels _(~5 commits, boundary approx.)_   | Mark     |
| 2026-05-20 | `283896b` | Phase 1 stats schema + ingestion, Research Tool DB migration, MP contribution nav, slug refactors _(many commits, boundaries approx.)_ | Mark |
| 2026-05-21 | `32c86f5` | Merge from beta: post-Phase-1.5 cleanup + stats state checker _(retroactive)_               | Mark     |
| 2026-05-21 | `85a9bc6` | Chore: trigger redeploy after INC-001 DB restore — recreate stats tables _(retroactive)_    | Mark     |
| 2026-05-21 | `be4bff5` | Merge Phase 1.6 to master: source registry schema, seed, SKIP_MIGRATIONS guard _(retroactive)_ | Mark  |
| 2026-05-21 | `76b0f5a` | Merge: backup_to_r2 NOT NULL tracking fix _(retroactive)_                                   | Mark     |
| 2026-05-21 | `cda4251` | Merge: DB-level DEFAULT 0 on ha_cron_run integer columns _(retroactive)_                    | Mark     |
| 2026-05-21 | `00c88c7` | Merge: fix 12 pre-existing test failures _(retroactive)_                                    | Mark     |
| 2026-05-21 | `ab9945e` | Chore: trigger redeploy to clear stuck committee evidence thread _(retroactive)_            | Mark     |
| 2026-05-22 | `b957084` | Merge: remove global healthcheckPath from railway.toml _(retroactive)_                      | Mark     |
| 2026-05-22 | `93a43db` | Fix: NameError policy_filter on /archive/mp pages (first attempt — had a bug) _(retroactive)_ | Mark  |
| 2026-05-22 | `e88162c` | Fix: NameError policy_filter on /archive/mp pages (corrected) _(retroactive)_              | Mark     |
| 2026-05-22 | `4163711` | Fix: remove undefined analytics variable from archive_mp _(retroactive)_                    | Mark     |
| 2026-05-23 | `08d561c` | Merge: incident log INC-001/002/003 full writeups _(retroactive)_                           | Mark     |
| 2026-05-23 | `0d5dc49` | Merge: null cadence fix + DRY_RUN docs _(retroactive)_                                      | Mark     |
| 2026-05-23 | `7f3a4dd` | Merge: discovery worker resilience — NullPool + batch resume _(retroactive)_                | Mark     |
| 2026-05-23 | `3a1a328` | Merge: Phase 1.8 scoping docs + OBR seed _(retroactive)_                                    | Mark     |
| 2026-05-24 | `0078a30` | Merge: fix discovery worker cadence crash (validate_cadence + outer rollback) _(retroactive)_ | Mark  |
| 2026-05-24 | `2cbf3cb` | Merge: rename discovery metric `classified` → `llm_passed` _(retroactive)_                 | Mark     |
| 2026-05-24 | `aa2be8c` | Merge: promote skip log from DEBUG to INFO, add canonical name _(retroactive)_              | Mark     |
| 2026-05-24 | `0389784` | Merge: add order=newest to GOV.UK search API calls _(retroactive)_                          | Mark     |
| 2026-05-24 | `4b00e58` | Feat: stats catalogue public page (beta-gated; coming-soon on production) _(retroactive)_   | Mark     |
| 2026-05-25 | `3349773` | Docs: note that DATABASE_URL is stored in .env _(retroactive)_                              | Mark     |
| 2026-05-25 | `00dc7b7` | Chore: .gitignore patterns for ad-hoc diagnostic scripts _(retroactive)_                    | Mark     |
| 2026-05-25 | `3e4f884` | Perf: eliminate wasted COUNT + cap ts_headline calls in PQ related-content _(retroactive)_  | Mark     |
| 2026-05-25 | `3ffe57d` | Chore: operational logging — deploys.md + CLAUDE.md discipline                             | Mark     |
| 2026-05-27 | `f947ed8` | Docs: DBeaver operational note + /stats diagnostic findings update                         | Mark     |
| 2026-05-27 | `9289347` | Fix: stats catalogue route precedence + pool_pre_ping stale connection fix                 | Mark     |
| 2026-05-27 | `2371c20` | Feat: Phase 1.8 — StatPublicationTheme model + policy_area classifier + discovery write path | Mark   |
| 2026-05-27 | `2feda73` | Feat: Phase 1.8 — backfill script for existing stat publication policy_area tags            | Mark     |
| 2026-05-27 | `1622d4c` | Fix: backfill written counter increments in dry-run mode too (pushed before execute run)    | Mark     |
| 2026-05-27 | `b921208` | Docs: expand £5 Paid Mailout entry in ideas backlog                                         | Mark     |
| 2026-05-27 | `6e08efa` | Chore: strip [STATS_DIAG] timing instrumentation from stats_catalogue.py                    | Mark     |
| 2026-05-27 | `b719891` | Chore: remove DISCOVERY_DRY_RUN flag from discovery worker                                  | Mark     |
| 2026-05-27 | `d06658c` | Docs: close out HESA producer de-registration (deploys.md + phase-1-8-scoping.md)          | Mark     |
| 2026-05-28 | `0e16be6` | Feat: Phase 1.8 data_url schema — StatPublication columns + StatPublicationDataFile model   | Mark     |
| 2026-05-28 | `9c2f657` | Feat: Phase 1.8 data_url piece 2 — GOV.UK extractor, orchestration script, 61 tests         | Mark     |
| 2026-05-28 | `3dff5ee` | Fix: dedup duplicate URLs in gem-c-attachment containers (UniqueViolation on FE data library) | Mark   |
| 2026-05-28 | `443b8f8` | Feat: Phase 1.8 data_url piece 3 — OnsApiExtractor + dispatch + 78 tests                    | Mark     |
| 2026-05-28 | `bd57cdb` | Fix: remove GOV.UK-only producer filter from extract_pub_data_files                         | Mark     |
| 2026-05-28 | `659d828` | Docs: log ONS piece 3 execute run + producer filter fix in deploys.md                       | Mark     |
| 2026-05-28 | `f3e42ec` | Docs: capture 28 May forward design thinking (Phase 1.9, hub pages, landing page)           | Mark     |
| 2026-05-28 | `a62ea96` | Feat: Phase 1.8 data_url piece 2b — sub-page prevalence diagnostic script                   | Mark     |
| 2026-05-28 | `e2f5b6f` | Merge feature/phase-1-8-data-url-piece-2b: piece 2b sub-page following + Phase 1.9 docs     | Mark     |
| 2026-05-28 | `edb7ccb` | Docs: week-of-26-May housekeeping (CLAUDE.md discipline, INC-006, deploys.md reconciliation) | Mark     |
| 2026-05-29 | `dc05450` | Phase 1.9: spike (stat detail pages), A1/A2/A3 first_published_at, related pubs section      | Mark     |
| 2026-06-02 | `36ad8a0` | Phase 1.9: B1-B4 catalogue date UI; pub-date bug fix (shared Content-API helper, discovery worker) | Mark |
| 2026-06-03 | `be4706a` | INC-007 writeup + pub_dates tests; ONS release_date fix (follow latest_version)              | Mark     |
| 2026-06-03 | `94e0936` | Per-producer date label (Published vs Latest release) + ONS semantic note; working-with-opus update | Mark |
| 2026-06-03 | `d0c9b93` | Docs: ONS date-staleness finding (piece-4 task) + CLAUDE.md branch-flow rule (return to beta after operational master work) | Mark |

---

## Production SQL log

All statements run directly against production Postgres. Append-only — never
edit past entries.

---

### 2026-05-21 — INC-001 data restoration

**Context:** Production data loss event. Volume recovered via R2 backup replay.
Full account in `docs/incident-log.md` (INC-001).

> **Uncertain — exact SQL not captured at the time.** Known operations included
> pg_restore from R2 backup and sequence resyncs on ha_session, ha_contribution,
> ha_pq, ha_session_theme. See INC-001 writeup for full account.

---

### 2026-05-25 — GIN full-text search indexes

**Context:** Performance improvement — enable Bitmap Index Scan on ha_pq and
ha_contribution for full-text search queries.

**Run via:** `scripts/create_gin_indexes.py` (SQLAlchemy AUTOCOMMIT mode against
production DATABASE_URL). _Note: executed before the local→production connection
prohibition was established on 2026-05-25. Future index work must go through
Railway Query console._

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ha_session_title_tsv
    ON ha_session USING GIN (to_tsvector('english', title));

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ha_pq_uin
    ON ha_pq USING GIN (to_tsvector('english', uin));

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ha_pq_question_tsv
    ON ha_pq USING GIN (to_tsvector('english', coalesce(question_text, '')));

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ha_contribution_speech_tsv
    ON ha_contribution USING GIN (to_tsvector('english', coalesce(speech_text, '')));
```

**Verified:** EXPLAIN ANALYZE confirmed Bitmap Index Scan — ha_pq 4.265 ms,
ha_contribution 0.873 ms.

---

### 2026-05-27 — Phase 1.8 ha_stat_publication_theme table

**Context:** `db.create_all()` was not running on production (SKIP_MIGRATIONS=1 was
accidentally set). Table created manually via DBeaver. SKIP_MIGRATIONS=1 retained
on production service going forward — all future schema changes go via DBeaver.

**Run via:** DBeaver (hopper.proxy.rlwy.net:50798)

```sql
CREATE TABLE ha_stat_publication_theme (
    id SERIAL PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES ha_stat_publication(id) ON DELETE CASCADE,
    theme VARCHAR(200) NOT NULL,
    theme_type VARCHAR(20) NOT NULL DEFAULT 'specific',
    confidence FLOAT,
    tagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    model_used VARCHAR(100),
    CONSTRAINT uq_stat_pub_theme_publication_theme_type
        UNIQUE (publication_id, theme, theme_type)
);

CREATE INDEX idx_stat_pub_theme_pub ON ha_stat_publication_theme(publication_id);
CREATE INDEX idx_stat_pub_theme_type ON ha_stat_publication_theme(theme_type);
```

### 2026-05-27 — HESA producer de-registration

**Context:** HESA / Jisc (producer id 25, slug `hesa-jisc`) de-registered as an active producer. Site blocks all automated access (403 across all paths — root, `/data-and-analysis`, `/api` — confirmed 27 May 2026). No GOV.UK organisation registration exists for HESA. HESA data surfaces correctly via DfE GOV.UK publications (file URLs on DfE landing pages link directly to hesa.ac.uk). Discovery had previously run on 2026-05-23 and completed cleanly with zero candidates — the 403 was caught and swallowed by `DirectPageParserStrategy` (logged as a warning; not surfaced as a failure to the state machine). Producer was `authorisation_status = 'authorised'`, `discovery_status = 'completed'` before this update. Zero `ha_stat_publication` rows attributed to HESA.

**Run via:** DBeaver (hopper.proxy.rlwy.net:50798), 27 May 2026 ~21:22–21:25 BST

```sql
UPDATE ha_stat_producer
SET authorisation_status     = 'declined',
    authorisation_reason     = 'Site blocks all automated access (403 across all paths). HESA data surfaces transitively via DfE GOV.UK publications — no standalone discovery strategy is viable. De-registered as active producer 2026-05-27.',
    discovery_status         = 'failed',
    discovery_failure_reason = 'Site blocks all automated access (403 across all paths). HESA data surfaces transitively via DfE GOV.UK publications — no standalone discovery strategy is viable. De-registered as active producer 2026-05-27.',
    updated_at               = CURRENT_TIMESTAMP
WHERE slug = 'hesa-jisc';

INSERT INTO ha_stat_producer_auth_log
    (producer_id, changed_at, old_status, new_status, change_reason, recorded_by)
VALUES
    (25, CURRENT_TIMESTAMP, 'authorised', 'declined',
     'Site blocks all automated access (403 across all paths). HESA data surfaces transitively via DfE GOV.UK publications — no standalone discovery strategy is viable. De-registered as active producer 2026-05-27.',
     'Mark Forde (DBeaver, manual de-registration)');
```

**Result:** 1 row updated (`ha_stat_producer` id 25), 1 row inserted (`ha_stat_producer_auth_log` id 10). Zero data rows affected.

**Note:** `updated_at` set explicitly because `onupdate=datetime.utcnow` is an ORM hook — raw SQL bypasses it. This is the required pattern for all raw SQL updates on ORM-managed tables.

---

### 2026-05-28 — Phase 1.8 data_url schema: ha_stat_publication columns + ha_stat_publication_data_file table

**Context:** Phase 1.8 data_url piece 1 — schema changes to support per-publication data file extraction. All statements run individually via DBeaver Console (cursor-position execution), one at a time.

**Run via:** DBeaver (hopper.proxy.rlwy.net:50798), 2026-05-28 ~09:37–09:39 BST

```sql
-- Step 1: Add three columns to ha_stat_publication
ALTER TABLE ha_stat_publication
    ADD COLUMN IF NOT EXISTS data_files_status       VARCHAR(30) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS data_files_extracted_at TIMESTAMP   NULL,
    ADD COLUMN IF NOT EXISTS ees_url                 TEXT        NULL;

-- Step 2: Add CHECK constraint on data_files_status
ALTER TABLE ha_stat_publication
    ADD CONSTRAINT ck_stat_pub_data_files_status
    CHECK (data_files_status IN (
        'pending', 'extracted', 'fetch_failed', 'no_files_found', 'not_extractable'
    ));

-- Step 3: Index on data_files_status
CREATE INDEX IF NOT EXISTS idx_stat_pub_data_files_status
    ON ha_stat_publication (data_files_status);

-- Step 4: Index on data_files_extracted_at
CREATE INDEX IF NOT EXISTS idx_stat_pub_data_files_extracted_at
    ON ha_stat_publication (data_files_extracted_at);

-- Step 5: New table ha_stat_publication_data_file
CREATE TABLE IF NOT EXISTS ha_stat_publication_data_file (
    id              SERIAL PRIMARY KEY,
    publication_id  INTEGER NOT NULL
                        REFERENCES ha_stat_publication(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    file_type       VARCHAR(10),
    title           TEXT,
    file_size_bytes INTEGER,
    classification  VARCHAR(20)
                        CHECK (classification IS NULL OR classification IN (
                            'main_release', 'supporting_tables', 'technical_docs', 'other'
                        )),
    display_order   INTEGER NOT NULL DEFAULT 0,
    extracted_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_pub_data_file_url UNIQUE (publication_id, url)
);

-- Step 6: Index on publication_id for data file lookups
CREATE INDEX IF NOT EXISTS idx_pub_data_file_pub
    ON ha_stat_publication_data_file (publication_id);
```

**Verified:** All 4 post-DDL checks passed — columns present with correct types/nullability/defaults; `ha_stat_publication_data_file` structure correct; all constraints registered; 1,124 existing `ha_stat_publication` rows defaulted to `data_files_status = 'pending'`.

---

### 2026-05-28 — Phase 1.8 piece 3: ONS data file extraction execute run

**Context:** Phase 1.8 data_url piece 3 — OnsApiExtractor execute run against the 20 ONS pending publications. Dry-run validated on Railway first (processed: 20, dry_run: 20). Execute run at 14:29 BST.

**Run via:** Local script (`scripts/extract_pub_data_files.py --execute`) connecting to production DATABASE_URL. _(Note: violates INC-005 local-to-production discipline — should have been a Railway one-shot service. Recorded for audit completeness.)_

**Result:** processed: 20, extracted: 19, no_files_found: 1 (pub=17 `trade` — ONS API returned no downloads for the latest version of that dataset). Zero pending ONS rows remain.

---

### 2026-05-28 — Phase 1.8 piece 3: ONS URL fix — api.beta.ons.gov.uk → www.ons.gov.uk/datasets

**Context:** Phase 1.8 data_url piece 3, step 1 — 20 ha_stat_publication rows stored api.beta.ons.gov.uk API endpoint URLs rather than human-facing ONS dataset landing page URLs. Dataset ID is deterministically extractable from the path. Updated to canonical www.ons.gov.uk/datasets/{id} form.

**Run via:** DBeaver (hopper.proxy.rlwy.net:50798), 2026-05-28 ~14:10 BST

```sql
UPDATE ha_stat_publication
SET url        = 'https://www.ons.gov.uk/datasets/' ||
                     split_part(replace(url, 'https://api.beta.ons.gov.uk/v1/datasets/', ''), '/', 1),
    updated_at = CURRENT_TIMESTAMP
WHERE url LIKE 'https://api.beta.ons.gov.uk/%';
```

**Result:** 20 rows updated. All now have canonical `https://www.ons.gov.uk/datasets/{dataset-id}` URLs. Zero api.beta.ons.gov.uk URLs remain in ha_stat_publication.

---

### 2026-05-29 — Phase 1.9: add first_published_at column to ha_stat_publication

**Context:** Phase 1.9 publication date work (A1) — add source publication date column.
Discovery worker and backfill script will populate it going forward.

**Run via:** DBeaver (hopper.proxy.rlwy.net:50798), 2026-05-29 ~18:20 BST

```sql
ALTER TABLE ha_stat_publication
ADD COLUMN IF NOT EXISTS first_published_at DATE NULL;
```

**Verified:**
```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'ha_stat_publication'
  AND column_name = 'first_published_at';
-- Result: first_published_at | date | YES
```

---

### 2026-05-29 — Phase 1.9: first_published_at backfill execute run

**Context:** Phase 1.9 A3 — backfill `first_published_at` on existing
`ha_stat_publication` rows. GOV.UK rows via GOV.UK Content API
(`first_published_at`); ONS rows via ONS datasets API (`release_date`).

**Run via:** Railway one-shot service `scripts/backfill_pub_dates.py --execute`
(internal DATABASE_URL, SKIP_MIGRATIONS=1, Restart: Never). Dry-run validated
first (1104 populated / 20 null / 0 errors); 6-row spot-check against source
pages confirmed dates correct before execute. Not raw SQL — writes via
SQLAlchemy per-row commit, `updated_at` set explicitly.

**Result:** 1,104 of 1,124 rows populated; 20 NULL (source provides no date);
0 errors. Date range 2008-03-01 to 2026-05-28. NULLs handled by NULLS LAST
ordering in the catalogue (no separate UX treatment needed at this count).

---

## Railway infrastructure log

One-off infrastructure changes (service additions, deletions, env var changes) that
don't correspond to a git push or SQL operation.

---

### 2026-05-28 — Phase 1.8 piece 2b: sub-page prevalence diagnostic + dry-run

**Piece 2b diagnostic re-scan (one-shot service):**
One-shot Railway service over 108 `no_files_found` GOV.UK publications. 47 of 108
(44%) found to have sub-pages. Zero fetch errors. DWP accounts for 25 of 47.
Log captured locally before teardown. 47 pub IDs recorded in
`docs/phase-1-9-scoping.md` as Phase 1.9 HTML-extraction targets.

**Piece 2b dry-run (one-shot service, no --execute):**
Dry-run over 6 representative publications (IDs 1167, 1198, 1193, 664, 1454, 1272)
spanning DSIT, DBT, DWP, DfT, Defra. Mechanics validated: sub-pages followed
correctly; pre-release-access exclusion works for DBT prefix variant and DSIT
suffix variant; scenario B (sub-pages fetched, no files, stays `no_files_found`)
confirmed. Zero data-file yield — sub-pages contain HTML statistical content, not
`gem-c-attachment` downloads. Full execute on all 47 deferred; 47 IDs documented
as Phase 1.9 targets.

**Final extraction state across all 1,124 publications (confirmed 28 May 2026):**
`extracted: 962 | no_files_found: 109 | not_extractable: 53 | pending: 0`

---

### 2026-05-28 — Phase 1.8 piece 2: GOV.UK extractor backfill execute run

**Context:** Phase 1.8 data_url piece 2 — `GovUKGenericExtractor` + `FallbackExtractor`
+ `select_extractor` dispatch. Backfill run as Railway one-shot service over all
GOV.UK-producer pending publications (7 producers). Mid-run bug: `UniqueViolation`
on `uq_pub_data_file_url` — dedup fix committed (`3dff5ee`) and one-shot
re-deployed cleanly. Final result: all GOV.UK publications processed. ONS
publications (non-GOV.UK domain) correctly skipped, staying `pending` for piece 3.

---

### 2026-05-27 — Service teardown: backfill-stat-policy-areas

**Action:** Deleted Railway service `backfill-stat-policy-areas`  
**Reason:** One-shot policy_area backfill complete — 1,117 publications tagged and verified. Service no longer needed.  
**Script retained:** `scripts/backfill_pub_policy_areas.py` — kept in repo for future re-runs if needed.  
**Expected effect:** Reduce monthly Railway memory cost.

**Verified:** `SELECT COUNT(*) FROM ha_stat_publication_theme` → 0 (empty, as expected).

---

### 2026-05-27 — Phase 1.8 policy_area backfill execute run

**Context:** Phase 1.8 backfill of controlled policy_area tags onto existing
`ha_stat_publication` rows. Run via Railway one-shot service
`backfill-stat-policy-areas` using `scripts/backfill_pub_policy_areas.py --execute`.
Not raw SQL — writes via SQLAlchemy using production `DATABASE_URL`.

**Result:** 2,398 theme rows written across 1,117 publications (7 classify
failures — all non-publication documents correctly rejected). 0 pubs with no
tags returned.

**Phase 1.8 policy_area tagging complete.** Steady-state tagging now handled
inline by `run_discovery()` for new publications. Backfill service left dormant
(restart policy: Never) in case re-run is needed for future producer additions.
