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
| 2026-05-25 | TBD       | Chore: operational logging — deploys.md + CLAUDE.md discipline (this commit)               | Mark     |

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
