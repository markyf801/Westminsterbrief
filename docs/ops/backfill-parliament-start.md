# Backfill to the start of the current Parliament (9 July 2024)

Two one-off scripts take the archive back from May 2025 to the first sitting of
the current Parliament (Tue 9 July 2024):

- `scripts/backfill_parliament_start.py` — debates, statements, Westminster Hall,
  contributions (9 Jul 2024 → 29 Apr 2025), with inline theme tagging.
- `scripts/backfill_pq_parliament_start.py` — Written Questions (9 Jul 2024 →
  5 May 2025). Insert-only: a stored question is never changed.

Items the tagger cannot tag never block a run: they are counted (in the
checkpoint, and in `ha_tag_attempt` by the tagger) and skipped after a few
attempts; items the crons already gave up on are skipped too. Only a Gemini
failure stops tagging.

Both walk newest-first (the archive stays contiguous), keep their checkpoint in
`ha_cron_run` (re-running carries on where the last run stopped), stop cleanly
before production cron windows (`hansard_archive/cron_windows.py`), and refuse to run at the same time as each other.

## Estimates (sampled October 2026)

| | Debates | Written Questions |
|---|---|---|
| Volume | ~125 Commons + ~120 Lords sitting days; ~5,400 sessions, ~180k contributions, ~213 MB speech text | 57,630 questions |
| Runtime | ~6 h (ingest ~2.7 h, tagging ~3,800 sessions) | ~19 h ingest + ~25 h tagging (one Gemini call per PQ) |
| DB growth | ~0.5 GB | ~0.3 GB |

## Service

One-shot service as in `railway-services.md` → "One-shot services": build from
`master` with `Dockerfile.cron`, restart policy **Never**, `SKIP_MIGRATIONS=1`,
`DATABASE_URL` (Variable Reference), `GEMINI_API_KEY`. Change the start command
for each step and redeploy. Pushing to master stops a run; just run it again.

## Before every run

1. **Volume free space.** The Postgres volume is 5 GB (2.1 GB used on 23 Oct
   2026, after that day's release). The two backfills add roughly 0.85 GB
   (debates ~0.65 GB, questions ~0.2 GB), so with index bloat the database ends
   at about 3.1–3.3 GB — 62–66% of 5 GB, and past the 70% line within a few
   months of normal growth. **Grow the volume to 10 GB before the questions
   backfill** (live resize, no downtime on paid plans; see
   `railway-services.md` → "Bulk migrations and volume size"). Check usage in
   the Railway dashboard before every run; stop and resize if it passes 70% —
   vacuum, index builds and backup/restore need headroom.
2. **The 03:00 backup still finishes in time.** After ~1 GB of growth the nightly
   `pg_dump` takes longer. Check the latest `backup-cron-r2` log: status `ok`, the
   dump size, and that it finished before 03:30 UTC (the backfill keeps clear of
   02:55–03:30). If it runs later, widen the backup window in `_CRON_WINDOWS`
   before the next run.
3. A backup from the same day exists (R2, `daily/`).

Size check (read-only, Postgres query tab):

```sql
SELECT pg_size_pretty(pg_database_size(current_database()));
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 8;
SELECT MIN(date) FROM ha_session;  SELECT MIN(tabled_date) FROM ha_pq;
```

## Order

1. Merge the classification/tagging fixes first, so backfilled sessions get the
   final debate types and theme hygiene. If they land later, re-run
   `scripts/reclassify_debate_types.py` over the backfilled range.
2. `python scripts/backfill_parliament_start.py --dry-run --max-days 14`
3. `python scripts/backfill_parliament_start.py --max-days 7 --max-tag 300`
   → spot-check a few July 2024 sessions (type, tags, links) and the size growth.
4. `python scripts/backfill_parliament_start.py --max-tag 5000`
   — repeat until the log says `range complete`. Always pass a large
   `--max-tag`: the default (600) stops the whole run, ingest included, once
   600 sessions are tagged (about 3,800 to tag in all).
5. `python scripts/backfill_parliament_start.py --tag-only --max-tag 1000`
   — repeat until `still_untagged=0` (sessions that cannot be tagged are counted
   as `skipped`).
6. Check the PQ unique indexes exist (`uq_ha_pq_api_id`, `uq_ha_pq_date_uin`;
   the daily health report's "Written questions: duplicates + unique indexes"
   line is OK, or
   `docs/plans/pq-identity-phase3-sql.md` step 4). They stop a cron and the
   backfill storing the same question twice; if a cron stores one first, the
   backfill keeps the cron's row. Then
   `python scripts/backfill_pq_parliament_start.py --dry-run` — expect about
   57,600 to insert and exit 0. Exit 5 means an identity mismatch: stop and look
   at the listed rows. Records the API returns with no UIN, question text or
   tabled date are skipped and listed as `unusable` (the crons can't store
   them either).
7. `python scripts/backfill_pq_parliament_start.py --max-days 3 --max-tag 1000`
8. `python scripts/backfill_pq_parliament_start.py --max-tag 60000`
   — repeat until `range complete`. Always pass a large `--max-tag`: the
   default (3000) stops the run once 3,000 questions are tagged (57,630 to
   tag in all).
9. Copy changes ("from July 2024") and the bill-page archive start date
   (`hansard_archive/bill_session_match.py` `_DEFAULT_ARCHIVE_START`).

When runs are allowed: the shared schedule in `hansard_archive/cron_windows.py`
(20-minute lookahead) blocks weekday daytime whole (07:55–23:30 Mon–Thu,
07:55–20:00 Fri, which also covers the proposed PQ quick-check's last run at
19:45), 02:55–03:30 and 04:25–07:45 every day, plus Sunday
01:55–02:20 / 02:55–03:50 and 03:55–05:00 on the 2nd of the month. In practice:
weekday nights (Mon–Thu 23:30–02:55, then 03:30–04:25), Friday evening onwards
and weekends (Sat/Sun 07:45 until the next early-morning window), about 60 hours
a week. Start runs on a Friday evening or at the weekend; a run started inside a
window exits 0 at once with `cron window: …`, and a running one stops cleanly
before the next window. Capture the full log of each run before deleting the
service.

## Exit codes

0 done or stopped cleanly (cap, time, cron window) · 1 a day failed after
retries, or a database error — re-run · 2 API refused (401/403/429) or
`GEMINI_API_KEY` missing — wait / fix, do not retry at once · 3 tagging stopped:
Gemini rejected the request (bad key / model / schema), Gemini failing (health
check failed), too many untaggable items, or the tagger is missing a helper the
script calls · 4 the other
backfill is running · 5 (PQs) identity mismatch — investigate.
