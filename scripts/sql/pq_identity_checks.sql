-- PQ identity (INC-008) — read-only checks. Run between every step of
-- docs/plans/pq-identity-phase3-sql.md, from the oneoff-tasks service:
--   sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/sql/pq_identity_checks.sql'
-- Everything runs inside a READ ONLY transaction that is rolled back, so this
-- file cannot change anything. Not for the Railway web console (it silently
-- adds a 100-row LIMIT).
\pset pager off
BEGIN READ ONLY;

SELECT now() AS checked_at, current_database() AS db,
       pg_size_pretty(pg_database_size(current_database())) AS db_size;

-- A. Indexes on ha_pq, and any INVALID index left by a failed CONCURRENTLY build
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ha_pq' ORDER BY 1;
SELECT c.relname AS invalid_index
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE i.indrelid = 'ha_pq'::regclass AND NOT i.indisvalid;

-- B–E and the backfill targets (exact counts, no LIMIT)
SELECT
  (SELECT count(*) FROM ha_pq)                                        AS total_rows,
  (SELECT count(*) FROM (SELECT 1 FROM ha_pq GROUP BY lower(uin)
                         HAVING count(*) > 1) b)                      AS b_dup_uin_groups,
  (SELECT count(*) FROM ha_pq WHERE api_id IS NULL)                   AS c_no_api_id,
  (SELECT count(*) FROM (SELECT 1 FROM ha_pq WHERE api_id IS NOT NULL
                         GROUP BY api_id HAVING count(*) > 1) d)      AS d_dup_api_id_groups,
  (SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM ha_pq
                         WHERE api_id IS NOT NULL GROUP BY api_id
                         HAVING count(*) > 1) d)                      AS d_extra_rows,
  (SELECT count(*) FROM (SELECT 1 FROM ha_pq GROUP BY tabled_date, lower(uin)
                         HAVING count(*) > 1) e)                      AS e_dup_date_uin_groups,
  (SELECT count(*) FROM ha_pq WHERE heading IS NULL)                  AS g_missing_headings,
  -- missing, or the ingestor's _looks_truncated rule (258 chars, or 248-298 ending "...")
  (SELECT count(*) FROM ha_pq WHERE is_answered IS TRUE
     AND (answer_text IS NULL OR answer_text = ''
          OR length(answer_text) = 258
          OR (length(answer_text) BETWEEN 248 AND 298
              AND rtrim(answer_text) LIKE '%...')))                   AS answers_to_fetch,
  (SELECT count(*) FROM ha_pq p WHERE NOT EXISTS
     (SELECT 1 FROM ha_pq_theme t WHERE t.pq_id = p.id))              AS untagged;

-- Where the api_id duplicates sit. in_cron_window = duplicate rows the PQ cron
-- will meet on its next run (tabled or answered in the last 7 days): each one is
-- an identity_mismatch until dedupe runs, and more than 5 stops that pass.
SELECT min(tabled_date) AS dup_first_tabled, max(tabled_date) AS dup_last_tabled,
       max(answer_date) AS dup_last_answered,
       min(ingested_at) AS dup_first_ingested, max(ingested_at) AS dup_last_ingested,
       count(*) FILTER (WHERE tabled_date >= current_date - 6
                           OR answer_date >= current_date - 6) AS in_cron_window
FROM ha_pq
WHERE api_id IN (SELECT api_id FROM ha_pq WHERE api_id IS NOT NULL
                 GROUP BY api_id HAVING count(*) > 1);

-- F. Collision zone, per numbering series. Commons numbers from 900001 up are
--    a separate per-session series (oral questions transferred to written
--    answer — named day = the department's oral questions day); compare each
--    series only with itself. A series collides once the current session's
--    max_n reaches the old session's min_n.
WITH s AS (
  SELECT tabled_date >= DATE '2026-05-13' AS new_session,
         CASE WHEN uin ~* '^HL[0-9]+$' THEN 'Lords HL'
              WHEN uin ~ '^[0-9]+$' AND uin::bigint >= 900000 THEN 'Commons 9xxxxx'
              WHEN uin ~ '^[0-9]+$' THEN 'Commons normal'
              ELSE 'other' END AS series,
         CASE WHEN uin ~* '^HL[0-9]+$' THEN substr(uin, 3)::bigint
              WHEN uin ~ '^[0-9]+$' THEN uin::bigint END AS n
  FROM ha_pq)
SELECT series, new_session, count(*) AS rows, min(n) AS min_n, max(n) AS max_n
FROM s GROUP BY 1, 2 ORDER BY 1, 2;

-- Collisions so far: current-session rows whose uin also exists in an older session
SELECT count(*) AS new_rows_sharing_old_uin
FROM ha_pq n
WHERE n.tabled_date >= DATE '2026-05-13'
  AND EXISTS (SELECT 1 FROM ha_pq o
              WHERE o.tabled_date < DATE '2026-05-13' AND lower(o.uin) = lower(n.uin));

-- H. INC-008 fingerprint: old-session rows carrying a current-session api_id (must be 0)
SELECT min(api_id) AS new_session_min_api_id FROM ha_pq WHERE tabled_date >= DATE '2026-05-13';
SELECT count(*) AS h_old_rows_with_new_api_id
FROM ha_pq
WHERE tabled_date < DATE '2026-05-13'
  AND api_id >= (SELECT min(api_id) FROM ha_pq WHERE tabled_date >= DATE '2026-05-13');

-- The 2026-10-20 snapshot tables were dropped on 2026-10-23 (restore from an R2
-- backup if needed), so the snapshot row-count check has been removed.

ROLLBACK;
