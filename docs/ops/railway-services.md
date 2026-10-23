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
   - `pq-cron-quickcheck` (once created)
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

Oral ministerial statements are classified within the same Hansard ingestion
(`debate_type='ministerial_statement'`). Written ministerial statements are a
separate source with their own cron — see "Written ministerial statements"
below. Theme tagging runs inline at the end of every Hansard cron run.

### Written Questions (`scripts/ingest_pq_cron.py`, 7-day rolling window)

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `pq-cron-early` | `30 8 * * 1-5` | 09:30 Mon–Fri | 08:30 Mon–Fri | First capture after Parliament's 08:00–09:30 publication window |
| `pq-cron-morning` | `30 9 * * 1-5` | 10:30 Mon–Fri | 09:30 Mon–Fri | Safety net for late-published questions |
| `pq-cron-afternoon` | `0 14 * * 1-5` | 15:00 Mon–Fri | 14:00 Mon–Fri | Captures answers published mid-afternoon |
| `pq-cron-monday` | `0 9 * * 1` | 10:00 Monday | 09:00 Monday | Post-weekend backlog catch-up |
| `pq-cron-quickcheck` *(proposed, not created yet)* | `15,45 6-19 * * 1-5` | 07:15–20:45 | 06:15–19:45 | `--quick-check --service-name pq-quickcheck`: 3 count calls; exits in seconds if nothing new, else ingests only the windows that grew. Its counts feed `scripts/pq_publication_times.py`; narrow the hours once that report has 4+ sitting weeks |

Each full sweep runs three passes: tabled in the last 7 days, answered in the
last 7 days, and answers corrected in the last 7 days (`--corrected-days`,
default same as `--answered-days`; 0 = off). The answered and corrected passes
re-fetch a full record only when the answer is new or changed — including a
holding answer replaced on the same day and a newly corrected answer. The
corrected pass adds one list request per run plus one request per newly
corrected answer. `--dry-run` also works for full sweeps (no writes, no run
record, no tagging, no email; same API requests as a real run).

**`pq-cron-quickcheck` setup (not created yet).** Create it only after the PQ
identity steps 2–4 in `docs/plans/pq-identity-phase3-sql.md` (code deployed,
duplicates merged, unique indexes on `api_id` and `(tabled_date, lower(uin))`):
the indexes are what make an overlap with a full sweep safe. Settings as the
other PQ crons: branch `master`, Dockerfile Path `Dockerfile.cron`, restart
policy Never, `SKIP_MIGRATIONS=1`, `DATABASE_URL` Variable Reference,
`GEMINI_API_KEY` (tagging), `POSTMARK_SERVER_TOKEN` and `ADMIN_EMAIL` /
`CRON_ALERT_EMAIL` (alerts). Start command
`python scripts/ingest_pq_cron.py --quick-check --service-name pq-quickcheck`.
First run: add `--dry-run` (counts and predicted inserts/updates, writes
nothing), press Run Now, read the log, then remove it. Each run makes 3 count
requests (`take=1`); a narrow pass adds 1–4 list pages plus one request per new
question or new answer. It emails on the 2nd failed run in a row only. Skipped
runs (another PQ run in progress) are recorded as `skipped`.

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

- Hansard archive newest session per House; archive and PQ cron runs (`ha_cron_run`)
- Written questions: newest tabled per House, last write, recent answers, missing
  headings (last 14 days, WARN at 5%), all-time missing headings (count only — should
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

- `backup-cron-r2` — daily pg_dump to Cloudflare R2
- **Stakeholder directory:** no automated cron. Ministerial meetings loaded via CSV upload + `stakeholder_directory/pipeline.py` when GOV.UK publishes quarterly transparency data. Committee evidence triggered manually via `/admin`. Lobbying register also manual.

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

## Bulk migrations and volume size

Single-transaction bulk inserts use working space (WAL + indexes) of roughly
2–3× the final stored data size. For migrations of 200k+ rows, check the Postgres
volume has headroom before `--execute`. The Hobby default (500 MB) was too small
for the Hansard archive; Phase 2A needed several GB. Live resize is supported on
paid tiers with zero downtime.

## Environment variables (names only)

Set on the **service** (not just the project) in the Railway dashboard.

- Core: `SECRET_KEY` (long random string), `DATABASE_URL` (Variable Reference), `ADMIN_EMAIL`, `ADMIN_TOKEN`, `TOTP_SECRET`
- AI / data APIs: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (paid products only), `NEWS_API_KEY`, `BSKY_HANDLE`, `BSKY_PASSWORD`
- Email: `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `EMAIL_TEST_MODE`, `CRON_ALERT_EMAIL`
- Billing (paid products; test keys during dev): `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`
- Backups: `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `BACKUP_ENCRYPTION_KEY`
- No longer read by any code (delete from the web service once the TheyWorkForYou removal is deployed and confirmed): `TWFY_API_KEY`, `SEARCH_BACKEND`
- Flags / behaviour: `FEATURE_AUTH`, `FEATURE_ACCOUNT`, `FEATURE_DIVISIONS`, `STATS_CATALOGUE_ENABLED`, `BETA_USERS`, `SKIP_MIGRATIONS`, `RATELIMIT_ENABLED`, `APPROVED_EMAILS`, `APPROVED_DOMAINS`, `DISCOVERY_POLL_INTERVAL`
