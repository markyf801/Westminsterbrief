# PQ identity — production run order for the owner (INC-008)

Companion to the PQ identity-key plan. The app never runs any of this: startup
migrations don't create these indexes, and `db.create_all()` never alters an
existing table. Every step is run by Mark.

> **Where to run things.** Use the reusable Railway one-off service
> **`oneoff-tasks`** (Dockerfile.cron, `SKIP_MIGRATIONS=1`, internal
> `DATABASE_URL`, restart policy Never): set its start command to the command
> shown, deploy, wait for it to exit, download the full log and read it before
> the next step (`docs/ops/railway-services.md` → One-shot services).
>
> **Do not use the Railway web query console for writes or bulk checks.** It
> silently appends a 100-row `LIMIT` — on 2026-10-20 a `CREATE TABLE … AS
> SELECT` there copied only 100 rows, and audit counts came back capped at 100.
> Use `psql` from `oneoff-tasks` (commands below) or the scripts.
>
> **Timing.** Nothing here during a cron window (UTC): 03:00–03:15 backup,
> 06:30–07:15 bills, 08:30–09:00 / 09:30–10:00 / 14:00–14:30 PQ crons and
> Mondays' PQ run, the first ~5 minutes of each hour 11:00–23:00 Mon–Thu. Check
> `list-deployments` for a running cron first.
>
> Log every production write (date, full statement or command, one-line
> context) in `docs/deploys.md` → Production SQL log.

`CREATE / DROP INDEX CONCURRENTLY` can't run inside a transaction block — each is
given below as its own `psql -c`, which runs it on its own.

---

## Production facts this order is based on (2026-10-20, exact psql counts)

| Fact | Value |
|---|---|
| Rows in `ha_pq` | 130,934 |
| Questions stored twice (same api_id, same date + uin) | 100 groups, 100 extra rows |
| Rows without `api_id` | 7,373 |
| Rows without `heading` | 40,633 |
| Unique index on `uin` | **none** — `idx_ha_pq_uin` is a plain btree; `ix_ha_pq_api_id` is non-unique |
| Collisions so far (current-session row sharing an old-session UIN) | 0 |
| Old-session rows carrying a current-session api_id (≥ 1,904,079) | 0 — no INC-008 overwrite has happened |
| Current-session numeric UINs ≥ 50,047 | 103 — all in the Commons **9xxxxx series** (below) |
| DB size | 1,544 MB |

What this means:

- With no unique index on `uin`, the **old code (live now) overwrites** an
  old-session row when a new question reuses its UIN; the new code inserts a
  second row instead. There is no "inserts fail safely until an index is
  dropped" phase — the old plan's step "drop the unique `uin` index" is gone.
  The first ordinary collision is expected in early December 2026, so the
  deploy (step 2) is the deadline item.
- Nothing has been damaged, so **no restore is needed**. The snapshot exists for
  undo only.
- The 100 duplicate rows are why the unique indexes don't exist yet. The real
  protection against storing a question twice is the unique index on `api_id`
  (step 4) — build it as soon as the duplicates are merged.

### The 9xxxxx UIN series (e.g. 901603)

Commons UINs from 900001 upwards are a separate numbering series that restarts
every session (900017 on 13 May 2026, 900027 on 17 Jul 2024, 900002 on 27 May
2015 …). Every one checked on the public API is a Commons question with
`isNamedDay = true`, a single answering department per day, tabled about three
sitting days before its named day, and the named day is that department's oral
questions day (e.g. 901544–901549 Defence, tabled 13 Oct, due 19 Oct; 901598–
901603 Health, tabled 14 Oct, due 20 Oct). That is consistent with oral
questions that were unsuccessful in the shuffle being transferred for written
answer (inferred from the API data; Parliament doesn't label them). They are
real questions with their own api_ids, not bad data. A full walk of the public
API for 13 May–20 Oct 2026 (2026-10-20) found 257 of them (900017–901603, all
Commons, all named-day, 210 of the 249 answered on their named day) — but the
archive holds 103. Check whether the other ~150 are missing from `ha_pq` (the
`Commons 9xxxxx` row in the checks output); if so, the headings backfill
(step 6) inserts them. The same walk put the highest ordinary Commons UIN at
36,265 against the old session's lowest archived 50,047: about 13,800 to go at
2,200–2,700 a week, i.e. first ordinary collision late November / early
December.

- Matching is unaffected (api_id, then (date, uin)).
- Collision estimates must treat 9xxxxx as its own series: the checks file
  reports `min_n` / `max_n` per series (`Commons normal`, `Commons 9xxxxx`,
  `Lords HL`); a series collides when the current session's `max_n` reaches
  the old session's `min_n` for the same series. The 9xxxxx series grows far
  more slowly (≈1,600 numbers in five months), and the archive's old-session
  rows start on 6 May 2025 (UIN 50,047), when that session's 9xxxxx series was
  already past 903,300 (public API) — so it should collide well after ordinary
  UINs. Confirm with the checks (`Commons 9xxxxx`, old session, `min_n`). Never compare a 9xxxxx number with ordinary UINs.
  Either way the new code stores the new question as a new row.

---

## Order

| # | Step | Status |
|---|---|---|
| 0 | Snapshot `ha_pq` + `ha_pq_theme` | **Done 2026-10-20** |
| 1 | Read-only checks | **before the deploy**, then after every step |
| 2 | Deploy the code | |
| 3 | Merge duplicates — `dedupe_pq.py` dry run → apply | straight after step 2 |
| 4 | Unique indexes on `api_id` and `(tabled_date, lower(uin))` | straight after step 3 |
| 5 | Fill missing `api_id` — `backfill_pq_api_id.py` dry run → apply | |
| 6 | Answers catch-up, then headings backfill | |
| 7 | Re-tag | |
| 8 | Optional: legacy-UIN table; `api_id NOT NULL` | before December / only if possible |

**Do steps 2 → 3 → 4 in one sitting**, between cron windows, with no Monday
morning in between (or pause `pq-cron-monday` until step 4 is done). Until the
unique api_id index exists, two PQ runs overlapping (e.g. a long Monday run
and the next cron) could each insert the same new question; the code commits
every page to keep that window small, but only the index closes it.

The brief listed the api_id backfill before the indexes. The indexes come first
here because nothing in step 5 depends on them being absent (the backfill
refuses an api_id another row already has), and every day without them is a
day a question can be stored twice.

---

## 0. Snapshot — done

`ha_pq_snap_20261020` (130,934 rows) and `ha_pq_theme_snap_20261020`
(685,358 rows), verified by psql count. Keep them until step 7 has been checked.
The daily R2 backup is the fallback behind them.

**Dropped 23 Oct 2026** (Mark's decision, to free ~300 MB). Later catch-ups had
updated most rows, so undoing from them was no longer practical. If ever needed,
restore them from an R2 backup dated 20–22 Oct 2026 (daily backups are kept 30 days,
weekly ones 12 weeks). The undo steps below assume a restored copy.

Note for any undo: `ha_pq.question_tsv` is a generated column, so copy rows
back by naming the other columns (see step 3's undo), never `SELECT *`.

---

## 1. Read-only checks (before the deploy, and between every step)

Start command:

```
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/sql/pq_identity_checks.sql'
```

The file runs in a `READ ONLY` transaction and rolls back. It prints: indexes
and any INVALID index; `b_dup_uin_groups`, `c_no_api_id`,
`d_dup_api_id_groups`, `d_extra_rows`, `e_dup_date_uin_groups`,
`g_missing_headings`, `answers_to_fetch`, `untagged`; the dates of the api_id
duplicates (last tabled, last answered) and `in_cron_window` — how many the
next cron will meet; collision zone per numbering series; `new_rows_sharing_old_uin`;
`h_old_rows_with_new_api_id`; snapshot row counts.

Expected now: d = 100 groups / 100 extra rows, e = 100, c = 7,373, h = 0.
Run it once **before** deploying and keep the output as the baseline.

## 2. Deploy the code

Merge `fix/pq-identity-phase3` → `beta` → `master` with the `docs/deploys.md`
row in the same commit, outside cron windows. Then:

- `/health` shows the new commit.
- The next PQ cron log has `identity_mismatch=0`, `failed=0`, `page_errors=0`.

Expect cron alerts until step 3 is done if any of the 100 duplicates fall in a
cron's 7-day window (step 1 shows their dates and `in_cron_window`): a question that matches two
rows is counted as `identity_mismatch` and left untouched, and more than 5 in
one pass stops that pass. That is the code protecting the data, not a fault —
run step 3 before the next cron.

The cron now emails on **any** `identity_mismatch`, `failed` or `page_errors`
(one email per run), not only at 3+ errors.

Undo: redeploy the previous master SHA. Safe while there are no cross-session
collisions (the old code overwrites on a collision, so don't stay on it past
late November).

## 3. Merge duplicates

Dry run (default — writes nothing, prints every group: which row is kept, which
fields are filled, how many themes move, which other tables are re-pointed):

```
python scripts/dedupe_pq.py
```

Read the whole log. Expect 100 groups and about 100 rows to delete. A REVIEW
group is never merged: rows that disagree on api_id, one api_id with different
dates/UINs, or rows holding different versions of the question — answer text
beyond a truncated prefix (e.g. holding vs substantive answer), answer date,
holding/withdrawn flag, question text beyond a prefix. Any REVIEW group blocks
step 4 (the unique indexes would fail): bring them to a session, decide which
row is right, and re-run. Then:

```
python scripts/dedupe_pq.py --apply
```

Rules: keep the row with the newest data — latest answer date, then latest
`updated_at`, then the most complete, then the earliest id; fill its empty
fields from the duplicate, never overwrite (a truncated question/answer text
is replaced by the full copy of the same text); move themes without
duplicating; re-point every foreign key to `ha_pq.id` (read from the schema —
today only `ha_pq_theme`); one locked transaction per group. Exit 0 = clean,
3 = groups left for review, 1 = a group failed (rolled back, listed).

Check (step 1): `d_dup_api_id_groups = 0`, `e_dup_date_uin_groups = 0`,
`total_rows` down by the number deleted, `h = 0`.

Undo (deleted and kept ids are in the log; replace `…`):

```sql
BEGIN;
-- 1. drop step 4's indexes first if they exist (they would reject the duplicates)
-- 2. put the deleted rows back
INSERT INTO ha_pq (id, uin, heading, question_text, answer_text, asking_member,
       asking_mnis_id, answering_member, answering_mnis_id, answering_body,
       answering_body_id, tabled_date, answer_date, is_answered, is_holding,
       is_withdrawn, chamber, api_id, ingested_at, updated_at)
SELECT id, uin, heading, question_text, answer_text, asking_member,
       asking_mnis_id, answering_member, answering_mnis_id, answering_body,
       answering_body_id, tabled_date, answer_date, is_answered, is_holding,
       is_withdrawn, chamber, api_id, ingested_at, updated_at
FROM ha_pq_snap_20261020 WHERE id IN (…deleted ids…);
-- 3. kept rows back to their snapshot values
UPDATE ha_pq p SET api_id = s.api_id, heading = s.heading,
       question_text = s.question_text, answer_text = s.answer_text,
       answer_date = s.answer_date, is_answered = s.is_answered,
       is_holding = s.is_holding,
       asking_member = s.asking_member, asking_mnis_id = s.asking_mnis_id,
       answering_member = s.answering_member, answering_mnis_id = s.answering_mnis_id,
       answering_body = s.answering_body, answering_body_id = s.answering_body_id,
       chamber = s.chamber, updated_at = NOW()
FROM ha_pq_snap_20261020 s WHERE s.id = p.id AND p.id IN (…kept ids…);
-- 4. themes for both sets back to the snapshot
DELETE FROM ha_pq_theme WHERE pq_id IN (…kept ids…, …deleted ids…);
INSERT INTO ha_pq_theme SELECT * FROM ha_pq_theme_snap_20261020
WHERE pq_id IN (…kept ids…, …deleted ids…);
COMMIT;
```

(Check the column list against `\d ha_pq` first; add any column production
has that isn't listed, except `question_tsv`.)

## 4. Unique indexes — the protection against storing a question twice

Only when step 1 shows `d_dup_api_id_groups = 0` and
`e_dup_date_uin_groups = 0`. Each its own run, api_id first:

```
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE UNIQUE INDEX CONCURRENTLY uq_ha_pq_api_id ON ha_pq (api_id)"'
```

```
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE UNIQUE INDEX CONCURRENTLY uq_ha_pq_date_uin ON ha_pq (tabled_date, lower(uin))"'
```

`NULL` api_ids are allowed (Postgres treats them as distinct), so this doesn't
wait for step 5. `CONCURRENTLY` doesn't block reads or writes; a few seconds on
130k rows. With these in place a second copy of a question can't be inserted:
the new code counts that as `failed` (cron alert) and moves on.

Check (step 1): both indexes listed, `invalid_index` empty. If a build fails it
leaves an INVALID index — drop it, re-run step 3's dry run, retry:

```
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "DROP INDEX CONCURRENTLY uq_ha_pq_date_uin"'
```

Undo: the same `DROP INDEX CONCURRENTLY` for either index.

## 5. Fill missing api_id

Dry run (≈1 hour for 7,373 rows at 0.5 s/request; `--limit 50` for a quick look):

```
python scripts/backfill_pq_api_id.py
```

Looks each row up on `/questions/<tabled_date>/<uin>` and only writes the
api_id when the API returns the same UIN and date, the question text matches
(first 120 characters after the standard "To ask the Secretary of State for
…," / "To ask His Majesty's Government" opener, which thousands of questions
share), and no other row has that api_id. Every match is logged as
`WRITTEN id=… uin=… tabled=… api_id=…` (keep the log: it is the undo list); a
unique-index violation on one row is counted as `failed` and the run goes on. Never changes uin, date or
text. The log lists NOT_FOUND (e.g. withdrawn questions), TEXT_MISMATCH,
AMBIGUOUS and CONFLICT rows — keep it; those rows need a decision by hand.
Decision rule: TEXT_MISMATCH or AMBIGUOUS 0 → apply; a handful → read them
first; many → stop and review. Then:

```
python scripts/backfill_pq_api_id.py --apply
```

A 429 or 5 API errors in a row stops it (exit 2) and prints the id to resume
from: `python scripts/backfill_pq_api_id.py --apply --after-id <id>`.

Check (step 1): `c_no_api_id` down by the number written; d, e, h still 0.

Undo:

```sql
UPDATE ha_pq p SET api_id = NULL, updated_at = NOW()
FROM ha_pq_snap_20261020 s
WHERE s.id = p.id AND s.api_id IS NULL AND p.api_id IS NOT NULL
  AND p.id IN (…ids from the log…);
```

## 6. Answers catch-up, then headings backfill

Bulk-only passes no longer write answer text (the bulk copy is cut at 258
characters and would look complete); the cron's answered pass and Stage B
re-fetch answers that are missing or look truncated, by api_id.

Answers (writes; replaces only missing/258-character answers with the full one;
`--test` does the first 100):

```
python scripts/backfill_pq_answers.py --stage b --test
python scripts/backfill_pq_answers.py --stage b
```

Headings (dry run, then apply; ~1 s between requests, 14-day chunks):

```
python scripts/backfill_pq_headings.py
python scripts/backfill_pq_headings.py --apply
```

The default window is the current session (from 2026-05-13); if audit G
(`SELECT date_trunc('month', tabled_date), count(*) FILTER (WHERE heading IS NULL)
FROM ha_pq GROUP BY 1 ORDER BY 1;` via psql) shows older months with gaps, run
those months with `--since/--until`. The dry run must predict the
insert/update/mismatch split: expect mostly updates, few inserts, 0 mismatches. If the summary says answered questions
were left without answer text, run `python scripts/backfill_pq_answers.py
--stage b` once more.

Check (step 1): `g_missing_headings` falls (rows Parliament hasn't headed yet
stay NULL), `answers_to_fetch` near 0, d, e, h still 0.

Undo: headings from the snapshot —
`UPDATE ha_pq p SET heading = NULL, updated_at = NOW() FROM ha_pq_snap_20261020 s WHERE s.id = p.id AND s.heading IS NULL AND p.heading IS NOT NULL;`
— rarely wanted (the headings are Parliament's).

## 7. Re-tag

Rows whose question text changed have had their tags cleared; the tagger fills
untagged rows. Either wait for the next PQ cron (it tags all untagged rows) or
run once (needs `GEMINI_API_KEY` on `oneoff-tasks`):

```
python scripts/ingest_pq_cron.py --days 1 --answered-days 0 --service-name pq-retag
```

This tags every untagged row in one go (Gemini Flash-Lite batches): look at
`untagged` in step 1 first. A few hundred is fine; tens of thousands takes
hours and real Gemini spend — raise it at a session first (the tagger takes a
`limit`, but the cron script has no flag for it yet).

Check (step 1): `untagged` falls to about the number of brand-new rows.
Whether to re-tag rows tagged by the old echoed-UIN mapping (sample 200 first,
or all ~14K batches) is still an open decision.

Undo: themes from `ha_pq_theme_snap_20261020` for the affected ids (as in
step 3's undo).

## 8. Optional

**Legacy UIN map for the Phase 4 redirects** — *not needed: Phase 4 redirects
to the earliest-stored row, which is what this table would freeze (see
"Phase 4" below). Kept for reference only.* Only while `b_dup_uin_groups = 0`
(i.e. after step 3 and before the first cross-session collision, early
December); otherwise the primary key fails:

```
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE TABLE ha_pq_legacy_uin AS SELECT lower(uin) AS uin_lc, id AS pq_id FROM ha_pq" -c "ALTER TABLE ha_pq_legacy_uin ADD PRIMARY KEY (uin_lc)"'
```

Undo: `DROP TABLE ha_pq_legacy_uin;`

**`api_id NOT NULL`** — only if `c_no_api_id = 0`, which is unlikely (withdrawn
questions the API no longer returns). A plain `SET NOT NULL` takes an
ACCESS EXCLUSIVE lock and scans the table, blocking every PQ page meanwhile:
off-peak, outside cron windows. Lower-lock version — add the check unvalidated,
validate it (doesn't block reads/writes), then `SET NOT NULL` uses it and skips
the scan:

```
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "ALTER TABLE ha_pq ADD CONSTRAINT ha_pq_api_id_nn CHECK (api_id IS NOT NULL) NOT VALID"'
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "ALTER TABLE ha_pq VALIDATE CONSTRAINT ha_pq_api_id_nn"'
sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "ALTER TABLE ha_pq ALTER COLUMN api_id SET NOT NULL" -c "ALTER TABLE ha_pq DROP CONSTRAINT ha_pq_api_id_nn"'
```

Undo: `ALTER TABLE ha_pq ALTER COLUMN api_id DROP NOT NULL;`

---

## Phase 4 — date + UIN URLs (built 23 Oct 2026, branch `feature/pq-date-uin-urls`)

Must be live **before** `scripts/backfill_pq_parliament_start.py` runs and
before the first cross-session collision (late November / early December):
until then `/archive/pq/<uin>` opens the earliest-stored row with that UIN, so a
new or backfilled question that reuses a UIN would have its page, sitemap
entry and list links open the OLD question.

What it does (no schema change, no migration):

- Question pages: `/archive/pq/<yyyy-mm-dd>/<uin>` (tabled date, lowercase
  UIN — Parliament's own scheme). Looked up by `tabled_date` +
  `lower(uin)`, the expression of `uq_ha_pq_date_uin` (step 4); until that
  index exists the plain `tabled_date` index serves it (a few hundred rows a
  day). Malformed or impossible dates and unknown questions are 404.
- Old `/archive/pq/<uin>`: 301 to the dated page of the earliest-stored row
  with that UIN — exactly the question that URL has always shown. Row ids only
  grow, so later rows sharing the UIN (new session, backfill) never take an
  old link over. This is what the frozen `ha_pq_legacy_uin` map in step 8
  would have recorded, so **step 8's legacy table is not needed**. Instead of
  a separate disambiguation page, every question page lists the other
  questions with the same number (date, heading, asker), so a reader who
  typed a UIN can reach the one they meant.
- Every link, canonical tag, `og:url`, JSON-LD `url`, related panels,
  search results, party/topic pages, consultation/statement related lists and
  the MP research tool use `HaPQ.url_path` / `pq_url_path()`. The WQ page's
  "download selected" now posts the tabled date with the UIN so it exports
  the right question. Word/CSV exports link to Parliament's record (already
  dated), unchanged.
- Sitemap: one chunk per tabled month, `/sitemap-pqs-YYYY-MM.xml`, sorted by
  (tabled_date, id), `lastmod` = latest of tabled/answered/updated. Removes
  the 100K cap. `/sitemap-pqs-1.xml` … `-4.xml` are gone (404).

After deploying: resubmit `https://westminsterbrief.co.uk/sitemap.xml` in
Google Search Console. Expect the old URLs to show as "Page with redirect"
and the dated ones to be discovered and indexed over the following weeks;
the old sitemap chunk URLs may show as fetch errors until Google drops them.

## After everything

- PQ cron logs: `identity_mismatch=0` on every run; no alert emails.
- Step 1: d, e, h = 0; `new_rows_sharing_old_uin` may grow from December —
  that is now expected (two questions, two rows).
- Close INC-008 in `docs/incident-log.md`; update CLAUDE.md "Known issues"
  (it still says `ha_pq.uin` is UNIQUE).
