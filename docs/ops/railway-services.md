# Railway services, crons and connections

> Moved from `CLAUDE.md` on 2026-10-20. This repo is public: never put
> hostnames with ports, passwords, connection strings or account emails in
> this file. For live values use the Railway dashboard.
> See also `docs/railway-cron-setup.md` (per-service setup detail) and
> `docs/recovery-runbook.md`.

---

## Project and web service

- Entry point: `gunicorn flask_app:app` (see `Procfile` and `railway.toml`)
- Railway project: `invigorating-joy` — web service name: `Westminsterbrief` (`jubilant-intuition` is the backup cron's private-network name, not the web service)
- Railway auto-deploys the web service on push to `master`
- Custom domain `westminsterbrief.co.uk`: registered at GoDaddy, nameservers point to Cloudflare — all DNS is managed in Cloudflare (`www` CNAME to the Railway hostname, `_railway-verify` TXT, root record). HTTPS is handled by Railway (Let's Encrypt) once DNS verifies.
- Postgres is a Railway plugin; it sets `DATABASE_URL`. The web service uses `DATABASE_URL=${{Postgres.DATABASE_URL}}` as a Variable Reference (resolves at deploy time).
- **Beta service:** deleted 2026-10-20 (see `docs/history/decisions.md`). There is no beta deployment.

## Postgres connections

- **Production connection details:** Railway dashboard → Postgres → Connect. The *public* URL is the one local tools (DBeaver, pg_dump for restore drills) need; the internal hostname (`postgres.railway.internal`) only resolves inside Railway.
- DBeaver: use the public URL with SSL mode = require.
- No local Python scripts against production (INC-005): script writes run as Railway one-shot services. Manual production SQL is Mark's, via DBeaver. See `CLAUDE.md` → Data safety.

## Password rotation

Variable References resolve at deploy time, not runtime. The running container on
each consumer service keeps the old password until redeployed.

1. Regenerate password in Postgres → Database → Config
2. Manually redeploy **every** consumer service:
   - `Westminsterbrief` (Flask app)
   - `archive-cron-morning`
   - `archive-cron-daytime-mth`
   - `archive-cron-daytime-fri`
   - `pq-cron-early`
   - `pq-cron-morning`
   - `pq-cron-afternoon`
   - `pq-cron-monday`
   - `pq-cron-quickcheck`
   - `pq-cron-weekly` (once created)
   - `backup-cron-r2`
   - `member-cache-refresh`
   - `minister-refresh` (once created)
   - `rediscovery-cron`
   - `health-report` (once set up)
3. Verify Flask picked up the new credential: hit `/health` and confirm `status=ok`

## Cron services

**Note:** all cron services build from `master` (`pq-cron-afternoon` was switched
from `beta` on 2026-10-20).

### Hansard debates, speeches, oral statements (`scripts/archive_cron.py`)

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `archive-cron-morning` | `0 8 * * 1-5` | 09:00 Mon–Fri | 08:00 Mon–Fri | Morning catch-up, 3-day window |
| `archive-cron-daytime-mth` | `0 11-23 * * 1-4` | 12:00–midnight Mon–Thu | 11:00–23:00 Mon–Thu | Hourly during sittings |
| `archive-cron-daytime-fri` | `0 9-19 * * 5` | 10:00–20:00 Fri | 09:00–19:00 Fri | Hourly during Friday sittings |

Run-record names (`--service-name`, see `docs/railway-cron-setup.md`):
`morning-catchup`, `daytime-mth`, `daytime-fri`.

Oral ministerial statements are classified within the same Hansard ingestion
(`debate_type='ministerial_statement'`). Written ministerial statements are a
separate source with their own cron — see "Written ministerial statements"
below. Theme tagging runs inline at the end of every Hansard cron run.

Each run also refreshes debates it already holds when Hansard's copy has
changed (a debate stored while still going on, or before Hansard finished
publishing it): contributions are updated in place, keeping the session's id,
slug and tags, and `REFRESHED` / `HOLD` lines appear in the log, with counts in
the run summary and `ha_cron_run.notes`. A debate that loses more than 3
speeches and more than 10% is held (`HOLD`), not refreshed — and stays held on
every run while it is in the window, because the cron never applies such a
drop. To clear one: compare our page with Hansard by hand; if Hansard really
shortened it, run `python scripts/refresh_partial_debates.py --ext-id <ExtId>`
(dry run first, then `--execute`), which applies drops down to half of our
speeches. Refreshed debates that were already tagged are re-tagged (at most
`ARCHIVE_RETAG_CAP`, default 30, per run; old tags kept if the new ones don't
verify); any left over are carried to the next archive run via a
`retag_pending: <id>:<times carried>,…` line at the top of `ha_cron_run.notes`,
for at most 3 runs (a session whose Gemini call failed with a final error is
not carried; the log says how many were dropped — re-tag those with
`scripts/retag.py --ids`). See `hansard_archive/refresh.py`. Older debates stored cut
short: `scripts/refresh_partial_debates.py` (one-off, dry run by default).
Duplicate and swallowing debates (provisional copies Hansard later split or
re-keyed): the cron logs `DUPLICATE?` / `SWALLOWED?` and counts them in
`ha_cron_run.notes` (`swallowed=… duplicates=…`; a parent holding its own
child debate is logged as `(parent/child)` and not counted) but never deletes.
Clean-up, all one-shots, dry run first: `scripts/find_duplicate_debates.py`
(read-only; CSV in the log) → review → `migrations/session_redirect_table.py
--execute` → `scripts/delete_duplicate_debates.py` (only dead ids with a live
twin; undo tables; old URLs 302 to the kept copy until `ARCHIVE_REDIRECT_CODE=301`
is set on the web service) → `scripts/refresh_partial_debates.py --accept-shrink
--ext-id …` (add `--allow-empty` only for reviewed headings Hansard now has
empty) for the reviewed swallowers → `scripts/retag.py --ids …` as printed.
Rehearse on a Postgres copy first; the delete script's docstring lists the
before/after SQL checks, `curl -I`, the Cloudflare purge, and why the affected
days must not be re-ingested afterwards. `ARCHIVE_REDIRECT_CODE` and the
check that the redirect tables exist are per web worker (the check repeats at
most every 10 minutes): after the migration, wait 10 minutes or restart the
web service before testing redirects. Do not `--allow-empty` a final-id page
with the debate's real title (e.g. 15 Oct C5400E9A) while its speeches live
only under a provisional heading id; leave it for review until Hansard
finalises the day.

### Written Questions (`scripts/ingest_pq_cron.py`, 7-day rolling window)

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `pq-cron-early` | `30 8 * * 1-5` | 09:30 Mon–Fri | 08:30 Mon–Fri | First capture after Parliament's 08:00–09:30 publication window |
| `pq-cron-morning` | `30 9 * * 1-5` | 10:30 Mon–Fri | 09:30 Mon–Fri | Safety net for late-published questions |
| `pq-cron-afternoon` | `0 14 * * 1-5` | 15:00 Mon–Fri | 14:00 Mon–Fri | Captures answers published mid-afternoon |
| `pq-cron-monday` | `0 9 * * 1` | 10:00 Monday | 09:00 Monday | Post-weekend backlog catch-up; with `--full-refresh` (see "API trouble") the weekly full-record refresh |
| `pq-cron-quickcheck` | `15,45 6-19 * * 1-5` | 07:15–20:45 | 06:15–19:45 | `--quick-check --service-name pq-quickcheck`: 3 count calls; exits in seconds if nothing new, else ingests only the windows that grew. Its counts feed `scripts/pq_publication_times.py`; narrow the hours once that report has 4+ sitting weeks |
| `pq-cron-weekly` *(proposed, not created yet)* | `0 6 * * 6` | 07:00 Saturday | 06:00 Saturday | Headings safety net: re-reads questions tabled in the last 30 days from the bulk list only (see below) |

**Run-record names.** Each run writes a row in `ha_cron_run` named by its
`--service-name`, which the daily health report and the "Archive updated" line
on `/questions` read. For the four sweeps it is the Railway name
(`pq-cron-early`, `pq-cron-morning`, `pq-cron-afternoon`, `pq-cron-monday`);
the quick-check records `pq-quickcheck` and the weekly refresh
`pq-cron-weekly`. Runs started by hand use their own name (e.g. `pq-catchup`
for a one-off catch-up, `pq-retag` for a one-off tagging run); the health report lists those for information only once
they are in `CONFIG["pq_crons"]["one_off"]`, and warns about any other `pq-*` name.
A new scheduled PQ service needs its name added to `CONFIG["pq_crons"]["known"]`
in `hansard_archive/health_checks.py` and to `PQ_SCHEDULED_RUNS` in
`hansard_archive/models.py` (the "updated" times on `/questions` and `/written-questions/today`).

**Headings.** Parliament adds a question's heading 2–3 working days after it
is tabled (checked 23 Oct 2026: questions tabled the day before had none, three
days before about 80%, a week before all). The sweeps refresh `heading` on every
question in their 7-day tabled window (and on any question answered in the
last 7 days), so headings fill in as long as sweeps finish. When they don't
for several days (deploys, Railway outages, a slow Parliament API), questions
age out of the window without one. `pq-cron-weekly` closes that gap cheaply. It does a different job from
`pq-cron-monday` with `--full-refresh` (on fix/pq-resilience): the weekly run re-reads 30 days of list
records only, for headings; `--full-refresh` fetches full records for the 7-day window.

| Setting | Value |
|---|---|
| Service name | `pq-cron-weekly` |
| Source | this repo, branch `master` |
| Dockerfile Path | `Dockerfile.cron` |
| Start command | `python scripts/ingest_pq_cron.py --days 30 --answered-days 0 --skip-answer-fetch --no-tag --service-name pq-cron-weekly` |
| Cron schedule | `0 6 * * 6` (Saturday 06:00 UTC; no other PQ run then) |
| Restart policy | Never |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `SKIP_MIGRATIONS` | `1` |
| `POSTMARK_SERVER_TOKEN`, `ADMIN_EMAIL` / `CRON_ALERT_EMAIL` | as the other PQ crons (failure alerts) |

Bulk list pages only (`--skip-answer-fetch`: no per-question requests, stored
text never replaced, answer text never written), so about 20–30 requests of
500 questions: a few minutes normally, up to ~20 minutes when the API is slow.
`--no-tag`: the weekday sweeps tag anything it inserts. First run: add
`--dry-run`, press Run Now, check the log shows mostly `updated` (headings) and
0 identity mismatches, then remove it. The health report skips this service
until its first run, then expects a run every Saturday.

For a gap that is already there, run `scripts/backfill_pq_headings.py` once as
a one-shot (same bulk-only rules; dry run by default, `--apply` writes), e.g.
`python scripts/backfill_pq_headings.py --since 2026-10-01` then the same with
`--apply`. Its default `--since` is the start of the session (2026-05-13, ~42,000
questions, ~95 requests at 1 s spacing: about 20–45 minutes, longer when the API is
slow). Run it outside the quick-check hours (after 20:00 UTC or at the weekend).

Each full sweep runs three passes: tabled in the last 7 days, answered in the
last 7 days, and answers corrected in the last 7 days (`--corrected-days`,
default same as `--answered-days`; 0 = off). The answered and corrected passes
re-fetch a full record only when the answer is new or changed — including a
holding answer replaced on the same day and a newly corrected answer. The
corrected pass adds one list request per run plus one request per newly
corrected answer. `--dry-run` also works for full sweeps (no writes, no run
record, no tagging, no email; same API requests as a real run).

**`pq-cron-quickcheck` setup.** Create it only after the PQ
identity steps 2–4 in `docs/plans/pq-identity-phase3-sql.md` (code deployed,
duplicates merged, unique indexes on `api_id` and `(tabled_date, lower(uin))`):
the indexes are what make an overlap with a full sweep safe. Settings as the
other PQ crons: branch `master`, Dockerfile Path `Dockerfile.cron`, restart
policy Never, `SKIP_MIGRATIONS=1`, `DATABASE_URL` Variable Reference,
`GEMINI_API_KEY` (tagging; plus `ANTHROPIC_API_KEY` and `TAGGER_MODEL`, see "Topic tagging model"), `POSTMARK_SERVER_TOKEN` and `ADMIN_EMAIL` /
`CRON_ALERT_EMAIL` (alerts). Start command
`python scripts/ingest_pq_cron.py --quick-check --service-name pq-quickcheck`.
First run: add `--dry-run` (counts and predicted inserts/updates, writes
nothing), press Run Now, read the log, then remove it. Each run makes 3 count
requests (`take=1`); a narrow pass adds 1–4 list pages plus one request per new
question or new answer. It emails on the 2nd failed run in a row only. Skipped
runs (another PQ run in progress) are recorded as `skipped`.

**API trouble.** A list page that fails with a 5xx or a timeout is tried up to
5 times (pauses of 15, 30, 60 and 120 s plus jitter), then as pages of 100. A
429 is never retried: the run stops. Three pages failing in a row stop a pass
(the API is down); the rows already stored are still tagged and the run is
reported as failed, with an email. A sweep or quick-check also stops starting
new pages or full-record requests after 50 minutes (`--max-minutes`, env
`PQ_RUN_BUDGET_MINUTES`; 0 = no limit), so a hanging API can't make 08:30 run
into 09:30; it then tags what it stored and is reported failed. Chunked runs
have no budget. The run notes list, per pass, rows written and full records
fetched (`passes: tabled ins=… upd=… full_fetched=… fetch_failed=…`).

Every pass fetches a question's full record only when it is new, its stored
question text may be cut (the list endpoint cuts it at 255 characters with no
marker; a list text of 245+ characters must be a strict prefix of what is
stored) or its answer is new or changed (answer date, holding flag, correction
date, or the stored answer missing or cut). Until 2026-10-23 the tabled pass
fetched every question in its 7-day window on every run (~3,500 requests).
Compared with that, three things are no longer seen at once: an edit to a
question after its first 255 characters; an edit to an answer that changes
neither its date nor its holding/correction flags; and a holding answer
replaced the same day when the list record leaves out `answerIsHolding`.
Safety net: **`pq-cron-monday` should run with `--full-refresh`**, which
fetches every question in the tabled window as before, once a week (about 25
minutes at today's speed, inside the budget). Chosen over "also fetch answers
from the last 2 days" because it covers all three cases, costs one run a week
instead of a few hundred requests on every run, and Monday has the fewest new
answers. It covers only questions **tabled in the last 7 days**: an answer
edited later (no new date or flag) on an older question is still missed, as
it always was by the answered pass. `--full-refresh` is refused with
`--quick-check` or a chunked run. Tagging is capped at 500 PQs a run (env
`PQ_TAG_LIMIT`), 200 after an ingest that stopped early; the next run tags
the rest. Individual-record 404s are counted as `missing=` in the notes (ten
failed or missing in a row stop the pass, so a broken endpoint can't pass as
"ok").

Deploy steps for this change (after merging to `master`):
1. `pq-cron-monday` → Settings → Start command: add ` --full-refresh` to the
   end of its current command. If it is the plain form, the result is
   `python scripts/ingest_pq_cron.py --service-name pq-cron-monday --full-refresh`.
   Leave the other PQ crons as they are.
2. Nothing else: no migrations or new variables (`PQ_RUN_BUDGET_MINUTES` and
   `PQ_TAG_LIMIT` are optional; a bad value falls back to the default).

**Long catch-ups: chunked mode (one-shot).** A long `--answered-days` window
pages deep into the API, which returns HTTP 500 on deep pages and drops rows
as pages shift; a 541-day catch-up on 2026-10-22/23 aborted three times
(skip 7,000 / 33,500 / 39,500). Use date chunks instead:
`--answered-from` / `--answered-to` (or `--tabled-from` / `--tabled-to`) with
`--chunk-days` (default 7). Chunks are walked newest first, so if the run
stops the most-read questions are already done. Progress and a heartbeat are
saved in the run's `ha_cron_run.notes` after every page; **running the same
command again carries on** from where an unfinished run stopped and retries
the chunks that failed. A deploy to `master` kills a running one-shot and
leaves its record "running": for 2 hours after its last heartbeat a rerun is
recorded as `skipped` (no email); once Railway shows the old container
stopped, rerun with `--take-over` instead of waiting (step 3 below). A failed chunk is retried at the end and doesn't stop the run;
3 failed chunks in a row, a 429, more than 5 identity mismatches in a chunk or
more than 20 in the run do. A chunk where full-record requests failed counts
as failed too, so its stale answers are retried. If a chunk's API total is above the unique questions seen, the chunk is
walked once more (listed as `short=` if still short; not a failure). While it
runs, quick-checks are skipped (soft lock, kept alive by the heartbeat; a
crashed run stops blocking after 2 hours without progress). The scheduled
sweeps carry on as normal.

Finish the stale-answer catch-up (answered 2025-05-01 → 2026-10-23, 78
chunks) as a one-shot service (see "One-shot services" below): branch
`master`, Dockerfile Path `Dockerfile.cron`, restart policy Never, no
schedule, `SKIP_MIGRATIONS=1`, `DATABASE_URL=${{Postgres.DATABASE_URL}}`,
`POSTMARK_SERVER_TOKEN` and `ADMIN_EMAIL` / `CRON_ALERT_EMAIL` as on the other
PQ crons (alerts). No `GEMINI_API_KEY` needed: `--no-tag`, and the next
scheduled sweep tags any untagged rows.

1. Dry run of one week from the middle of the backlog (writes nothing, no run
   record; makes the same requests a real run would for that week, so allow
   up to ~15 minutes):
   `python scripts/ingest_pq_cron.py --answered-from 2025-10-13 --answered-to 2025-10-19 --chunk-days 7 --service-name pq-catchup --no-tag --dry-run`
   Read the log: `Chunk ... done — inserted=… updated=…` (updated = stale
   answers it would fix), `page_errors=0`.
2. The real run (start command):
   `python scripts/ingest_pq_cron.py --answered-from 2025-05-01 --answered-to 2026-10-23 --chunk-days 7 --service-name pq-catchup --no-tag`
3. Progress (DBeaver, read-only):
   `SELECT id, status, started_at, finished_at, notes FROM ha_cron_run WHERE service_name = 'pq-catchup' ORDER BY id DESC LIMIT 3;`
   — `done=N/78`, `next=` (next chunk), `failed=` (to retry), `beat=` (last
   progress, UTC). Finished when `next=end | done=78/78 | failed=-`. If it
   stopped (status `failed`), run the same command again. Keep the dates
   exactly the same, or it starts a new plan. If the container was killed
   (a deploy to `master`), its record still says `running` for up to 2 hours
   after its last `beat=`; a rerun in that time is recorded as `skipped`
   (no email) and does nothing. Either wait, or, once Railway shows the old
   container stopped, run the same command with `--take-over` added (never
   while the old one may still be running: two would walk at once).
4. Capture the log, then delete the service (see "One-shot services").

Estimated duration: about 110k answered questions in 78 chunks, about 225
list pages. A page whose rows are already up to date costs one list request
(20–50 s while the API is slow) plus a DB lookup per row; each stale answer
costs one extra request (~0.4 s; a fully stale page of 500 takes ~3.5 min).
So roughly 2–3 h of list requests plus 2.3 h per 20,000 stale answers: about
4–5 h if 20k are still stale, 8 h if 50k, up to ~15 h if nearly all are. API
retries add to that on a bad day.

**Timing rationale:** `/questions` queries `ha_pq` (local DB), not Parliament's
API directly (refactored at commit `57e7273`). The early + morning pair means
users checking new PQs at 09:30–10:00 UK see that day's questions already in the
DB. In BST, `pq-cron-early` fires right at Parliament's publication window close;
in GMT, `pq-cron-morning` is the effective first capture. Duplicate upserts
across the two morning runs are silent (upsert-by-UIN — but see the UIN
collision known issue in `CLAUDE.md`).

### Member cache refresh (`scripts/refresh_member_cache.py`)

| Service | Cron (UTC) | Purpose |
|---|---|---|
| `member-cache-refresh` | `0 2 * * 0` | Sunday 02:00 UTC — bulk-refresh all current MPs + Lords into `cached_member` |

Run once manually after initial deployment. After that, searches resolve member
names/party/constituency from the DB; no per-request Parliament API calls.

### Member profiles + AI biographies (`scripts/refresh_members.py`) — to be created

Feeds `member_profile` / `member_biography`, which `/members/<id>/<slug>`,
`/mp_search` and `/biography` read instead of calling Parliament on page view.
Needs `migrations/members_profile_tables.py --execute` run once first (one-off,
`SKIP_MIGRATIONS=1`). Both services: Dockerfile.cron, `SKIP_MIGRATIONS=1`,
`DATABASE_URL` reference; the bio job also needs `GEMINI_API_KEY`.

| Service | Cron (UTC) | Start command | Purpose |
|---|---|---|---|
| `member-profile-refresh` | `0 3 * * 0` | `python scripts/refresh_members.py` | Weekly — identity, posts, committees, contact, interests, archive activity counts (~4,400 polite requests, ~30–40 min) |
| `member-bio-refresh` | `0 4 2 * *` | `python scripts/refresh_members.py --audit-names --regenerate-bios` | Monthly — same refresh, re-checks every personal name (Wikidata P10428, Parliament fallback) with an audit report in `member_name_audit`, then Gemini Flash-Lite bios only where inputs changed |

Bios: generated with `MEMBER_BIO_MODEL` (default `gemini-2.5-flash`), then verified — filler and any
sentence naming an institution or year not in that section's source are dropped, plus a
`MEMBER_BIO_VERIFY_MODEL` check (default `gemini-2.5-flash-lite`; `off` disables it). The model judges
whether the sources, taken together, support each claim (not whether the wording matches); Parliament
data wins over Wikipedia. Source dates are written as "June 2023" so drafts and sources match. Regression
set: `tests/test_member_bio_verifier.py`. The report is in
`member_biography.verification` (added by re-running the migration with `--execute`). If a draft
loses more than 40% of its claims it is not published (`last_error` starts `needs_review`, the
`[review]` log lines list the dropped claims) — that usually means the verifier, not the bio, is wrong. Background comes
from Wikipedia; Wikidata education/occupation only when there is no article. Before a backfill, run
`python scripts/refresh_members.py --sample 12` and read the printed bios + dropped claims.

Names: `member_name` holds personal names (e.g. Baroness Smith of Malvern → Jacqui Smith); set
`source='manual'` on a row to lock an owner correction. Peer slugs follow
`firstname-lastname-of-place` once a name is known; old slugs 301.

After a reshuffle, run `python scripts/refresh_members.py --regenerate-bios --member-id <id>`
(repeatable) as a one-off rather than waiting for the schedule. `--dry-run`
predicts the insert/update/unchanged split and bio count without writing.

### Stats catalogue re-discovery (`scripts/rediscovery_cron.py`)

| Service | Cron (UTC) | Purpose |
|---|---|---|
| `rediscovery-cron` | `0 6 * * *` | Daily — re-scan authorised+completed producers for NEW publications + refresh ONS latest-release dates. Tracked by `last_rediscovered_at`. |

`--execute` writes; default is dry-run. Pre-classify URL/dataset-ID dedup means
only genuinely-new pubs hit Gemini; the headline metric is `SKIPPED_KNOWN` (a
healthy run is mostly skips). A fetch failure raises `StrategyFetchError` →
counted in `errors`, producer NOT stamped (re-attempted next run). Respects
Decision H — discovery only, no data-file extraction. Known open: DSIT's GOV.UK
query 422s (Issue A, `docs/ideas-backlog.md`).

### Early Day Motions (`scripts/ingest_edms.py`) — live since 2026-10-20

Service `edm-cron`, schedule `30 4 * * *` (daily 04:30 UTC), start
command `python scripts/ingest_edms.py`, `SKIP_MIGRATIONS=1`. Lists every EDM
in the current session and re-fetches only new or changed ones plus those
tabled in the last 90 days. Logs a row in `ha_cron_run` (`service_name=edm-cron`);
exits 1 if most API requests fail. Needs `migrations/edm_tables.py --execute`
and a one-shot `--backfill` first (both done 2026-10-20: 4,230 EDMs). `FEATURE_EDMS=live`
is set on the web service.

### Written ministerial statements (`scripts/ingest_written_statements.py`) — not yet set up

Feeds `ha_written_statement`, which `/written-statements` and Debate Prep read
(Debate Prep falls back to Parliament's live API only while the table is
missing or empty). Proposed service:

| Setting | Value |
|---|---|
| Service name | `wms-cron` |
| Source | this repo, branch `master` |
| Dockerfile Path | `Dockerfile.cron` |
| Start command | `python scripts/ingest_written_statements.py` |
| Cron schedule | `15 19 * * 1-5` (UTC: 20:15 BST / 19:15 GMT, Mon–Fri) |
| Restart policy | Never |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `SKIP_MIGRATIONS` | `1` |

No API keys (Parliament's API is public; policy areas are mapped without AI).
Statements are published through the sitting day, so an evening run picks up
the whole day; each run lists the last 7 days and re-fetches only new or
changed statements plus those made in the last 2 days (~2–3 minutes). It
sits clear of the archive cron's on-the-hour runs and the push-free windows.
Logs a row in `ha_cron_run` (`service_name=wms-cron`); exits 1 if most API
requests fail or Parliament returns 429 (the run stops at the first 429).

Order: `python migrations/written_statement_tables.py` (dry run) →
`--execute` as a one-shot → one-shot backfill
`python scripts/ingest_written_statements.py --since 2024-07-04 --service-name wms-backfill`
(~3,900 statements: ~40 list pages + one detail request each, about 75
minutes; re-running resumes, since unchanged statements are skipped; add
`--dry-run` first to see the predicted split) → enable `wms-cron` → set
`FEATURE_WRITTEN_STATEMENTS=beta`, then `live`, on the web service.

### Divisions / votes (`scripts/ingest_divisions.py`) — not yet set up

| Service | Cron (UTC) | Start command | Purpose |
|---|---|---|---|
| `divisions-cron` | `15 5 * * *` | `python scripts/ingest_divisions.py` | Daily — Commons and Lords divisions dated in the last 14 days, plus Hansard debate links |

Dockerfile.cron, restart policy Never, `SKIP_MIGRATIONS=1`, `DATABASE_URL` as a
Variable Reference to Postgres. No API keys (Commons Votes, Lords Votes and
Hansard APIs are public). 05:15 sits between `edm-cron` (04:30) and
upcoming-refresh (05:45); by then the previous evening's votes are published
and the hourly archive cron has normally stored the debates they link to.
A normal run makes about 5–30 requests and takes under a minute. Logs a row
in `ha_cron_run` (`service_name=divisions-cron`); exits 1 if most requests fail
or Parliament returns 429 (the run stops at once on a 429; re-run later).

What a run does: lists Commons divisions (detail fetched only for new ones or
when Parliament's `PublicationUpdated` changes), lists Lords divisions (the
list already carries the member lists), rewrites votes only when they differ,
then matches each division to Hansard (`/search/divisions.json`, by House +
date + division number) to link `ha_session` and store the question put and
the result line. Divisions whose debate isn't in the archive yet are linked on
a later run.

Order:
1. One-shot `python migrations/division_tables.py` (dry run: prints the SQL and
   the database size), then `python migrations/division_tables.py --execute`.
2. One-shot backfill for this Parliament: first
   `python scripts/ingest_divisions.py --since 2024-07-04 --dry-run`, then
   `python scripts/ingest_divisions.py --since 2024-07-04 --service-name divisions-backfill`.
   About 620 Commons and 440 Lords divisions (~700 requests, roughly 20–40 min);
   re-running resumes (stored Commons divisions are not re-fetched). Expect
   ~435,000 vote rows, ~75–100 MB with indexes (growth ~40–50 MB a year).
3. Create `divisions-cron` above; first run with `--dry-run` via Run Now.
4. `FEATURE_DIVISIONS=beta` on the web service (owner preview), then `live`.
   `live` also adds `/divisions` and `/sitemap-divisions.xml` to the sitemap.

### Ministers (`scripts/refresh_ministers.py`) — to be created

Feeds `gov_minister`, which the Research Tool's department/minister pickers,
its minister flags and the PQ pages' answering-minister roles read (via
`ministers.py`) instead of the old `minister_cache.json` (ephemeral disk,
refetched from GOV.UK after every deploy). Titles next to an answer or speech
are the post held on that date: `ministers.post_on_date()` reads the dated
post history in `member_profile.government_posts` (weekly
`member-profile-refresh`) plus `gov_minister` (daily, so a new appointment
shows before the weekly history catches up; for a member with a history, only
`gov_minister` posts that started at most 7 days before the history was
fetched, or any time after, are added, so a stale table can't bring back an ended post). When
several posts are held on one day, the most senior one in the department wins
(Secretary of State, then Minister of State, then Under-Secretary; Women and
Equalities, spokesperson and whip posts last). With neither, no title is shown;
`cached_member.ministerial_role` is no longer read. About 30 polite requests: Parliament
Members API government posts per department (member ids, post names, start
dates) merged with GOV.UK's ministers page (portfolio titles, and anyone
Parliament is missing). Any failed request, or a result that is implausibly
small (under 60 people / 15 departments, or under 70% of the people already
stored), writes nothing and exits 1 — the previous list stays. Logs a row in
`ha_cron_run` (`service_name=minister-refresh`). Until the table has rows the
site keeps using the old file-cached fetch, so the code can deploy first.

| Setting | Value |
|---|---|
| Service name | `minister-refresh` |
| Source | this repo, branch `master` |
| Dockerfile Path | `Dockerfile.cron` |
| Start command | `python scripts/refresh_ministers.py` |
| Cron schedule | `15 6 * * *` (daily 06:15 UTC; runs in under a minute, before bills at 06:30) |
| Restart policy | Never |
| `SKIP_MIGRATIONS` | `1` |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |

Order: one-shot `python migrations/gov_minister_table.py` (dry run) then
`--execute` → one-shot `python scripts/refresh_ministers.py --dry-run` (check
the people/department counts and the `[warn] no Parliament member id` lines) →
run once without `--dry-run` → create the cron. After a reshuffle, run it once
by hand rather than waiting for 06:15 (GOV.UK updates the same day; Parliament's
post list can lag a few days — GOV.UK-only appointments are added meanwhile).
`/admin` shows the table's age and counts.

### Daily health report (`scripts/daily_health_report.py`) — not yet set up

| Service | Cron (UTC) | Purpose |
|---|---|---|
| `health-report` | `30 7 * * *` | Read-only freshness checks (archive, PQs, bills, EDMs, members, stats, crons, R2 backup, `/ping` + `/health`). Emails `CRON_ALERT_EMAIL`/`ADMIN_EMAIL` when anything is WARN/FAIL; all-OK days send nothing except a Monday summary. |

07:30 sits after the edm (04:30), upcoming-refresh (05:45) and bills (06:30–07:15)
windows and before `archive-cron-morning` (08:00).

**Railway setup (new cron service):**

| Setting | Value |
|---|---|
| Service name | `health-report` |
| Source | this repo, branch `master` |
| Dockerfile Path | `Dockerfile.cron` |
| Start command | `python scripts/daily_health_report.py` |
| Cron schedule | `30 7 * * *` (UTC) |
| Restart policy | Never |
| `DATABASE_URL` | Variable Reference to the Postgres service (the script opens it read-only) |
| `POSTMARK_SERVER_TOKEN` | same value as the web service |
| `ADMIN_EMAIL` or `CRON_ALERT_EMAIL` | where the report goes (`CRON_ALERT_EMAIL` wins if both are set) |
| `SKIP_MIGRATIONS` | `1` |
| `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | optional; without them the R2 backup check shows SKIPPED (a read-only R2 token is enough) |

First run: set the start command to `python scripts/daily_health_report.py --dry-run`,
press Run Now, read the log, then put the normal start command back. Exits 0 even
when it finds problems; non-zero only if the checker crashes or the email can't be
sent.

"No new data" checks for Hansard and written questions use Parliament's sitting
calendar (What's On API `calendar/proceduraldates/{House}/sittingdates.json`, both
Houses, fetched once per run, read-only, no key). A sitting day's data is overdue
once the following working day has passed. If the calendar can't be reached they
fall back to a weekday rule and the report line says so. `--no-calendar` forces
the fallback.

What it checks (thresholds in `CONFIG` at the top of
`hansard_archive/health_checks.py`; `RECESS_PERIODS` there is a manual override,
normally empty):

- Hansard archive newest session per House; archive and PQ cron runs (`ha_cron_run`,
  by the run-record names above). A run with no finish time after 3 hours was stopped
  by a deploy or restart (or is hung): the line says so. For the four PQ sweeps another
  sweep finishing after it covers it (OK) while the unfinished run is under 26 Mon–Fri
  hours old (Railway skips a cron's next run while one is still running, so a hung run
  must not stay covered); after that, three unfinished runs in a row, or no sweep
  finishing since, the service is graded by the age of its last ok run (WARN, then
  FAIL). One-off runs (`pq-catchup`) are listed for information only; any other
  `pq-*` name not in the config is a WARN. Skipped quick-check runs are counted
- Bank holidays: not known to the report; "working days" below count them
- Written questions: newest tabled per House, last write, recent answers, missing
  headings (tabled in the last 14 days except the last 3 working days, which
  Parliament hasn't headed yet; WARN at 5%), all-time missing headings (count only — should
  fall after the heading backfill), and integrity: duplicate groups by `api_id` and by
  `(tabled_date, lower(uin))` must be 0 (FAIL otherwise), and `uq_ha_pq_api_id` /
  `uq_ha_pq_date_uin` must exist and be valid (WARN otherwise)
- Bills `last_refreshed` (WARN > 2 days), EDMs `last_refreshed` (WARN > 2 days) plus the
  `edm-cron` run record (WARN > 26 h)
- Members: `member_profile.summary_refreshed_at` (WARN > 8 days, with profile counts),
  `member_biography` (WARN > 35 days, dated by the newest bio attempt or monthly name
  audit, since unchanged bios aren't regenerated), `cached_member`
- Stats: upcoming releases (WARN > 24 h, FAIL > 72 h), producer rediscovery, headline
  stats — the stats-refresh job is intentionally not scheduled yet, so a stale
  headline table shows SKIPPED with its age; set `CONFIG["headline_stats"]["scheduled"]
  = True` once that service exists
- Search locale: `SELECT to_tsvector('english', 'Starmer’s')::text` must contain
  `'starmer'` (FAIL otherwise). Archive search relies on Postgres treating the curly
  apostrophe as punctuation, which depends on the database locale (production is
  `en_US.utf8`); a restore into a "C"-locale cluster would silently stop most
  possessives matching. The line shows the database `LC_CTYPE`. SKIPPED on SQLite
- Committee evidence cron, backup cron record, newest R2 object, live `/ping` + `/health`

### Committees and APPGs (`scripts/ingest_committees.py`, `scripts/ingest_appgs.py`) — not yet set up

Proposed services (both Dockerfile.cron, `SKIP_MIGRATIONS=1`):

| Service | Cron (UTC) | Start command | Purpose |
|---|---|---|---|
| `committees-cron` | `30 1 * * *` | `python scripts/ingest_committees.py` | Daily — current committees, members/roles, open inquiries + calls for evidence, recent reports (~40–60 min; the API is slow) |
| `appg-cron` | `30 2 * * 2` | `python scripts/ingest_appgs.py` | Weekly — no-op unless a new Register of APPGs edition is out (~10 min when it is) |

Order: `migrations/committees_appgs_tables.py --execute` → one-shot
`python scripts/ingest_committees.py` (first run ~1–1.5 h: scope text for
~300 inquiries) and `python scripts/ingest_appgs.py` → enable the crons → set
`FEATURE_COMMITTEES=beta`, then `live`, on the web service. Both log a row in
`ha_cron_run` and exit 1 if most requests fail; `--dry-run` writes nothing.

APPG edition discovery: www.parliament.uk (the register's index page) answers
Railway with HTTP 403 (Cloudflare bot protection; it works from a home connection).
When that happens the script probes `publications.parliament.uk/pa/cm/cmallparty/<YYMMDD>/contents.htm`
for weekdays after the stored edition (Mondays first, ≤ 30 requests, 0.5 s apart,
stops on 403/429). `--edition YYMMDD` skips discovery. If only the edition check
fails and the stored edition is under 12 weeks old, the run is logged as `failed` in
`ha_cron_run` but exits 0 (no weekly Railway failure email); otherwise it exits 1.

### Other

- `backup-cron-r2` — daily pg_dump to Cloudflare R2; records `backup-r2` in `ha_cron_run`
- `committee-evidence-cron` (`scripts/committee_evidence_cron.py`, script header says Mondays 06:00 UTC) —
  records `committee-evidence-cron`; the health report and `/health` expect a run every 8 days.
  Not otherwise documented here: confirm in the Railway dashboard that the service exists
- **Stakeholder directory:** no automated cron. Ministerial meetings loaded via CSV upload + `stakeholder_directory/pipeline.py` when GOV.UK publishes quarterly transparency data. Committee evidence triggered manually via `/admin`. Lobbying register also manual.

## Topic tagging model (Gemini / Claude Haiku 5.5)

Mark's decision, 24 Oct 2026: tag debates and Written Questions with Claude
Haiku 5.5 instead of Gemini 2.5 Flash-Lite (CLAUDE.md "Output rules"; eval in
`docs/plans/tagging-model-test-2026-10-24.md`, Mark's plans folder). The code
reads two variables, at the start of each run:

| Variable | Value | Effect |
|---|---|---|
| `TAGGER_MODEL` | `claude-haiku-5-5` | Tagging uses Claude. Unset: Gemini Flash-Lite with `GEMINI_API_KEY`, as before. Any other `claude-*` value stops tagging (only `LIVE_CLAUDE_MODELS` in `hansard_archive/tagger.py` may tag live). |
| `ANTHROPIC_API_KEY` | shared variable (reference it, don't paste the key) | The key Claude tagging uses. |

Set both on every service that tags: `archive-cron-morning`,
`archive-cron-daytime-mth`, `archive-cron-daytime-fri`, `pq-cron-early`,
`pq-cron-morning`, `pq-cron-afternoon`, `pq-cron-quickcheck`,
`pq-cron-monday`, `oneoff-tasks` (one-offs: `scripts/retag.py`, the backfills,
`scripts/run_tagging.py`). Set `TAGGER_MODEL` on all of them, so debates and
questions are not tagged by different models. Keep `GEMINI_API_KEY` on them
(rollback, and the Gemini default).

- **Deploy the code first.** Builds before this change ignore a Claude
  `TAGGER_MODEL` (they log an error and keep using Gemini), so setting the
  variable early is harmless but does nothing.
- **No fallback.** With `TAGGER_MODEL` set, a missing or rejected
  `ANTHROPIC_API_KEY` (401/403), a bad request, or repeated API failures stop
  that run's tagging: the items stay untagged and the next run picks them up.
  It is visible as `ABORTED: …` in the run's `ha_cron_run.notes` tagging line,
  the cron alert email, and, if it lasts, the daily health report's "Tagging:
  recent … tagged" checks. Gemini is never used instead.
- The tagging line in `ha_cron_run.notes` ends `model=<model id>`; stored tags
  record it in `model_used`.
- **Rollback:** delete `TAGGER_MODEL` from the services; the next run tags with
  Gemini. Tags already written keep their `model_used`.

## One-shot services

Settings: restart policy Never, `SKIP_MIGRATIONS=1`, internal `DATABASE_URL`.

`SKIP_MIGRATIONS=1` makes startup skip `db.create_all()` and the dev seeds.
(ALTER TABLE migration blocks have `try/except` and are no-ops.)

**Capture logs before teardown — do not skip steps 3 and 4:**

1. Deploy and run the service
2. Wait for "Exited" / "Stopped" / "Completed" in the dashboard
3. Export the FULL log to a local file via Railway's log download
4. Open the file locally and verify it contains the expected content (summary blocks, CSV markers, specific log patterns — confirm by inspection)
5. ONLY after the local file is confirmed: delete the service (don't just disable — they hold memory)

If a diagnostic script writes a summary block + CSV section + per-row log lines,
the local capture must contain all three before teardown.

### Duplicate topic tags clean-up (one-off, Oct 2026)

`ha_session_theme` holds policy-area tags stored twice (the tagger before
22 Oct 2026 kept repeats in Gemini's list). In this order:

1. The tagger fix (`write_session_tags`: each tag once, per-session lock) is
   deployed to the web service **and every cron service**. A push to master
   redeploys them all; check none is still running an older build.
2. `python scripts/dedupe_theme_rows.py` (dry run: groups per theme_type,
   one-write vs separate-write groups, the other tag tables). Review the log.
3. `python scripts/dedupe_theme_rows.py --execute`, outside a cron window. It
   copies the deleted rows to `ha_theme_dedupe_bak_<timestamp>` and prints the
   undo SQL. Log it in the production SQL log.
4. Optional: `python migrations/theme_unique_indexes.py` (dry run: must show 0
   duplicate groups).
5. `python migrations/theme_unique_indexes.py --execute`. It must end with
   "exists and is valid"; exit 1 means missing or INVALID (see the script's
   docstring).

If step 2 lists separate-write sessions, it prints a `scripts/retag.py --ids`
line to give them one clean tag set after step 3. Use the undo SQL only before
any re-tag of those sessions.

### Re-tag the archive with Claude Haiku (one-off, before the credit expires 21 Nov 2026)

`scripts/retag.py --batch` sends the same requests as live tagging through
Claude's Message Batches API (half price) and writes results through the same
verification and write path. It never submits anything twice: batch ids are
saved in `ha_cron_run` (service `retag-<sessions|pqs>-batch-<hash>`) as soon
as they are created, and the same command picks them up again. Batch API
limits and behaviour: the script's docstring. Run on `oneoff-tasks`
(Dockerfile.cron, restart policy Never, `SKIP_MIGRATIONS=1`, `DATABASE_URL`,
`ANTHROPIC_API_KEY`); start it outside cron windows (weekdays after 23:30 UTC,
or weekends): results are only written outside them, and a run that meets one
stops with exit 4 and resumes. Use exactly the same flags at each re-run of a
step (the checkpoint is keyed on them). In order:

1. **Dry run** (no API call, no writes): counts and the estimated cost at Batch
   prices per kind.
   `python scripts/retag.py --batch --model claude-haiku-5-5`
   Eval projection: about $16 for 7,000 debates + 131,000 questions (×1.4 with
   the July 2024 backfill); the script prints the figure for what is selected.
2. **Sample** (submits 50 debates + 50 questions, waits, prints before/after,
   writes no tags; costs cents):
   `python scripts/retag.py --batch --model claude-haiku-5-5 --sample 50`
   Spot-check the "after" tags against the source pages before going on
   (CLAUDE.md "Data safety": 0 wrong → proceed; 1–2 → investigate; 3+ → stop).
3. **Full run:**
   `python scripts/retag.py --batch --model claude-haiku-5-5 --apply --backup`
   Before its first write it copies each theme table to
   `ha_session_theme_bak_<UTC timestamp>` / `ha_pq_theme_bak_<…>` (names in the
   log); add them to `docs/deploys.md`. Most batches end within an hour, all
   within 24 h. Exit 3 (API failures, e.g. batch queue full), 4 (cron window)
   or 5 (still processing after `--max-wait-hours`, default 25): run the same
   command again — it polls and applies what was already submitted. Capture
   the full log (one-shot procedure above).
4. **Verify:**
   - the end-of-run summary per kind: `retagged`, `kept_old` (nothing
     verified: old tags kept), `to_retry` (questions sent again singly), and
     errored / expired / changed items (old tags kept), with a ready-made
     `--ids … --batch --apply --backup` command to re-run those;
   - Mark, in DBeaver: `SELECT model_used, COUNT(DISTINCT session_id) FROM
     ha_session_theme GROUP BY model_used;` and `SELECT model_used,
     COUNT(DISTINCT pq_id) FROM ha_pq_theme GROUP BY model_used;` — nearly all
     `claude-haiku-5-5`; Gemini rows remain only on kept_old / errored items;
   - the daily health report's tagging checks, and a few `/archive` and
     `/brief/<topic>` pages by hand.
5. **Undo (if needed):** the full run prints, per kind, undo SQL that restores
   the backed-up tags of every item now tagged by `claude-haiku-5-5` since the
   run started (items that had no tags before keep their new ones). Mark runs
   it in DBeaver in one transaction, checks the `items_to_restore` count before
   `COMMIT`, and logs it in the Production SQL log. It also reverts any debate
   the archive cron re-tagged with Claude after the run started (a refreshed
   debate), which then shows its older tags until its text next changes. Use it
   before any other re-tag; drop the backup tables later (Mark's call).

After the re-tag, the question backfill (`scripts/backfill_pq_parliament_start.py`)
tags with Claude too once `TAGGER_MODEL` is set on `oneoff-tasks` (live calls,
full price; the backfill checks the key at start and probes Claude, not Gemini,
when deciding whether the API is down).

## Bulk migrations and volume size

Single-transaction bulk inserts use working space (WAL + indexes) of roughly
2–3× the final stored data size. For migrations of 200k+ rows, check the Postgres
volume has headroom before `--execute`. The Hobby default (500 MB) was too small
for the Hansard archive; Phase 2A needed several GB. Live resize is supported on
paid tiers with zero downtime.

## Environment variables (names only)

Set on the **service** (not just the project) in the Railway dashboard.

- Core: `SECRET_KEY` (long random string), `DATABASE_URL` (Variable Reference), `ADMIN_EMAIL`, `ADMIN_TOKEN`, `TOTP_SECRET`
- AI / data APIs: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (paid products; topic tagging on the tagging services), `TAGGER_MODEL` (tagging services: `claude-haiku-5-5`, see "Topic tagging model"), `NEWS_API_KEY`, `BSKY_HANDLE`, `BSKY_PASSWORD`
- Email: `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `EMAIL_TEST_MODE`, `CRON_ALERT_EMAIL`
- Billing (paid products; test keys during dev): `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`
- Backups: `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `BACKUP_ENCRYPTION_KEY`
- No longer read by any code (delete from the web service once the TheyWorkForYou removal is deployed and confirmed): `TWFY_API_KEY`, `SEARCH_BACKEND`
- Flags / behaviour: `FEATURE_AUTH`, `FEATURE_ACCOUNT`, `FEATURE_DIVISIONS`, `STATS_CATALOGUE_ENABLED`, `BETA_USERS`, `SKIP_MIGRATIONS`, `RATELIMIT_ENABLED`, `APPROVED_EMAILS`, `APPROVED_DOMAINS`, `DISCOVERY_POLL_INTERVAL`
