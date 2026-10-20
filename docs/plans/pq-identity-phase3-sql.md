# PQ identity — SQL for the owner (Phases 0, 2 and 3)

Companion to the PQ identity-key plan (INC-008). The app never runs any of this:
startup migrations don't create or drop these indexes, and `db.create_all()` never
alters an existing table.

> **Run in the Railway Postgres console (Railway dashboard → Postgres service →
> Data / Query). Read-only first.** Run Section 1 and save the output before
> anything else. Log every write you make in `docs/deploys.md` (production SQL log).
>
> `CREATE/DROP INDEX CONCURRENTLY` can't run inside a transaction block — run
> each of those statements on its own, not wrapped in `BEGIN … COMMIT`.

---

## Order

| Step | When | Section |
|---|---|---|
| 1 | Now, before anything | 1 — read-only audits A–H (save the output) |
| 2 | Before any write | 2 — headroom check + snapshot |
| 3 | When audits C, D, E return 0 (Phase 1 data fixes done) | 3 — additive unique indexes (Phase 2) |
| 4 | **Only after the Phase 3 code is deployed and one cron has run cleanly** | 4 — drop the unique index on `uin` |
| 5 | After step 4 | 5 — checks |

Step 4 is the deadline item: once new-session UINs reach the old-session range
(early December 2026 — confirm with audit F) the cron can only insert new
questions if the unique `uin` index is gone. Until it's dropped, the new code
fails those inserts safely (counted as `failed`, cron alert) instead of
overwriting rows — but the questions are missing from the archive until it is.

---

## 1. Read-only audits

```sql
-- A. Indexes and constraints on ha_pq — note the real name of the unique uin index
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ha_pq';
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conrelid = 'ha_pq'::regclass;

-- B. Duplicate UINs (case-insensitive). 0 rows while the unique index exists.
SELECT lower(uin), count(*) FROM ha_pq GROUP BY 1 HAVING count(*) > 1;

-- C. Rows with no Parliament api_id
SELECT count(*) FROM ha_pq WHERE api_id IS NULL;

-- D. api_id shared by several rows (must be 0 before the unique api_id index)
SELECT api_id, count(*) FROM ha_pq
WHERE api_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;

-- E. (tabled_date, uin) shared by several rows (must be 0 before that unique index)
SELECT tabled_date, lower(uin), count(*) FROM ha_pq
GROUP BY 1, 2 HAVING count(*) > 1;

-- F. Lowest old-session UIN — when new-session numbering reaches this, UINs collide
SELECT min(uin::int) FROM ha_pq
WHERE uin ~ '^[0-9]+$' AND tabled_date < '2026-05-13';
SELECT min(substr(uin, 3)::int) FROM ha_pq
WHERE uin ILIKE 'HL%' AND substr(uin, 3) ~ '^[0-9]+$' AND tabled_date < '2026-05-13';
-- …and where the current session has got to:
SELECT max(uin::int) FROM ha_pq
WHERE uin ~ '^[0-9]+$' AND tabled_date >= '2026-05-13';
SELECT max(substr(uin, 3)::int) FROM ha_pq
WHERE uin ILIKE 'HL%' AND substr(uin, 3) ~ '^[0-9]+$' AND tabled_date >= '2026-05-13';

-- G. Missing headings by month
SELECT date_trunc('month', tabled_date) AS month,
       count(*) FILTER (WHERE heading IS NULL) AS no_heading,
       count(*) AS total
FROM ha_pq GROUP BY 1 ORDER BY 1;

-- H. Old-session rows carrying a new-session api_id (INC-008 fingerprint).
--    Confirm the threshold first: the lowest api_id tabled on/after 2026-05-13.
SELECT min(api_id) FROM ha_pq WHERE tabled_date >= '2026-05-13';
SELECT count(*) FROM ha_pq WHERE tabled_date < '2026-05-13' AND api_id > 1903000;
```

---

## 2. Headroom check and snapshot (first writes — reversible)

```sql
-- Check volume headroom before copying (CLAUDE.md: Railway volume limits)
SELECT pg_size_pretty(pg_total_relation_size('ha_pq'))       AS ha_pq,
       pg_size_pretty(pg_total_relation_size('ha_pq_theme')) AS ha_pq_theme,
       pg_size_pretty(pg_database_size(current_database()))  AS whole_db;

-- Snapshot (replace YYYYMMDD with today's date)
CREATE TABLE ha_pq_snap_YYYYMMDD       AS SELECT * FROM ha_pq;
CREATE TABLE ha_pq_theme_snap_YYYYMMDD AS SELECT * FROM ha_pq_theme;
SELECT (SELECT count(*) FROM ha_pq)       = (SELECT count(*) FROM ha_pq_snap_YYYYMMDD)       AS pq_ok,
       (SELECT count(*) FROM ha_pq_theme) = (SELECT count(*) FROM ha_pq_theme_snap_YYYYMMDD) AS theme_ok;
```

Undo: `DROP TABLE ha_pq_snap_YYYYMMDD; DROP TABLE ha_pq_theme_snap_YYYYMMDD;`
(once you no longer need them).

---

## 3. Phase 2 — additive unique indexes

Only when audits **C, D and E all return 0**. Each statement on its own.

```sql
CREATE UNIQUE INDEX CONCURRENTLY uq_ha_pq_api_id   ON ha_pq (api_id);
CREATE UNIQUE INDEX CONCURRENTLY uq_ha_pq_date_uin ON ha_pq (tabled_date, lower(uin));
ALTER TABLE ha_pq ALTER COLUMN api_id SET NOT NULL;

-- Freeze today's UIN → row mapping for the Phase 4 redirects
CREATE TABLE ha_pq_legacy_uin AS SELECT lower(uin) AS uin_lc, id AS pq_id FROM ha_pq;
ALTER TABLE ha_pq_legacy_uin ADD PRIMARY KEY (uin_lc);
```

If a `CONCURRENTLY` build fails it leaves an INVALID index — drop it
(`DROP INDEX CONCURRENTLY uq_…;`), fix the duplicates, retry.

Undo: `DROP INDEX CONCURRENTLY uq_ha_pq_api_id;`,
`DROP INDEX CONCURRENTLY uq_ha_pq_date_uin;`,
`ALTER TABLE ha_pq ALTER COLUMN api_id DROP NOT NULL;`, `DROP TABLE ha_pq_legacy_uin;`.

---

## 4. Phase 3 — drop the unique index on `uin`

**Only after the Phase 3 code (api_id / (date, uin) matching) is deployed** and a
PQ cron has logged `identity_mismatch=0`. Dropping it earlier gains nothing (the
old code matches by UIN alone, so it overwrites rather than inserts) and removes
the easy way back if the new code has to be reverted.

Until Phase 4 ships, `/archive/pq/<uin>` shows the earliest-ingested row when a
UIN exists in two sessions (the same row `ha_pq_legacy_uin` freezes), so the
newer question has no detail page of its own (lists link to the UIN URL too).
Phase 4 should follow soon after.

Use the real name from audit A. With SQLAlchemy's default naming it is
`ix_ha_pq_uin` (a unique index). Build the non-unique replacement first so
lookups by UIN are never without an index:

```sql
CREATE INDEX CONCURRENTLY ix_ha_pq_uin_nonuniq ON ha_pq (uin);
DROP INDEX CONCURRENTLY ix_ha_pq_uin;            -- the UNIQUE one, name from audit A
ALTER INDEX ix_ha_pq_uin_nonuniq RENAME TO ix_ha_pq_uin;
```

If audit A shows a unique **constraint** instead (e.g. `ha_pq_uin_key`), replace
the `DROP INDEX` line with:

```sql
ALTER TABLE ha_pq DROP CONSTRAINT ha_pq_uin_key;
```

The code already declares `uin` as non-unique (`hansard_archive/models.py`), so a
fresh database built by `create_all()` matches.

Undo: only while audit B still returns 0 (no cross-session duplicate inserted yet):
`DROP INDEX CONCURRENTLY ix_ha_pq_uin; CREATE UNIQUE INDEX CONCURRENTLY ix_ha_pq_uin ON ha_pq (uin);`.
After the first duplicate UIN is inserted there is no undo — forward-fix only.

---

## 5. Checks

```sql
-- Audits B–E and H again. After step 4, B may return rows (expected: the same
-- UIN in two sessions); C, D, E and H must stay 0.

-- Recent rows still missing a heading (should fall after the heading backfill)
SELECT count(*) FROM ha_pq WHERE heading IS NULL AND tabled_date >= current_date - 60;

-- Asking-member names no longer blanked by the cron
SELECT count(*) FROM ha_pq WHERE asking_member IS NULL AND asking_mnis_id IS NOT NULL;

-- Untagged rows (question_text changes clear tags so the tagger redoes them)
SELECT count(*) FROM ha_pq p
WHERE NOT EXISTS (SELECT 1 FROM ha_pq_theme t WHERE t.pq_id = p.id);
```

In the PQ cron logs: `identity_mismatch=0` on every run.
