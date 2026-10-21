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
   - `backup-cron-r2`
   - `member-cache-refresh`
   - `rediscovery-cron`
   - `health-report` (once set up)
3. Verify Flask picked up the new credential: hit `/health` and confirm `status=ok`

## Cron services

**Note:** all cron services build from `master` (`pq-cron-afternoon` was switched
from `beta` on 2026-10-20).

### Hansard debates, speeches, WMS (`scripts/archive_cron.py`)

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `archive-cron-morning` | `0 8 * * 1-5` | 09:00 Mon–Fri | 08:00 Mon–Fri | Morning catch-up, 3-day window |
| `archive-cron-daytime-mth` | `0 11-23 * * 1-4` | 12:00–midnight Mon–Thu | 11:00–23:00 Mon–Thu | Hourly during sittings |
| `archive-cron-daytime-fri` | `0 9-19 * * 5` | 10:00–20:00 Fri | 09:00–19:00 Fri | Hourly during Friday sittings |

WMS are classified within the same Hansard ingestion — no separate cron. Theme
tagging runs inline at the end of every Hansard cron run.

### Written Questions (`scripts/ingest_pq_cron.py`, 7-day rolling window)

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `pq-cron-early` | `30 8 * * 1-5` | 09:30 Mon–Fri | 08:30 Mon–Fri | First capture after Parliament's 08:00–09:30 publication window |
| `pq-cron-morning` | `30 9 * * 1-5` | 10:30 Mon–Fri | 09:30 Mon–Fri | Safety net for late-published questions |
| `pq-cron-afternoon` | `0 14 * * 1-5` | 15:00 Mon–Fri | 14:00 Mon–Fri | Captures answers published mid-afternoon |
| `pq-cron-monday` | `0 9 * * 1` | 10:00 Monday | 09:00 Monday | Post-weekend backlog catch-up |

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
`MEMBER_BIO_VERIFY_MODEL` check (default `gemini-2.5-flash-lite`; `off` disables it). The report is in
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

### Early Day Motions (`scripts/ingest_edms.py`) — not yet set up

Proposed service `edm-cron`, schedule `30 6 * * *` (daily 06:30 UTC), start
command `python scripts/ingest_edms.py`, `SKIP_MIGRATIONS=1`. Lists every EDM
in the current session and re-fetches only new or changed ones plus those
tabled in the last 90 days. Logs a row in `ha_cron_run` (`service_name=edm-cron`);
exits 1 if most API requests fail. Needs `migrations/edm_tables.py --execute`
and a one-shot `--backfill` first. Pages are hidden until `FEATURE_EDMS=live`
on the web service.

### Daily health report (`scripts/daily_health_report.py`) — not yet set up

| Service | Cron (UTC) | Purpose |
|---|---|---|
| `health-report` | `30 7 * * *` | Read-only freshness checks (archive, PQs, bills, EDMs, members, stats, crons, R2 backup, `/ping` + `/health`). Emails `CRON_ALERT_EMAIL`/`ADMIN_EMAIL` when anything is WARN/FAIL; all-OK days send nothing except a Monday summary. |

07:30 sits after the bills window (06:30–07:15) and before `archive-cron-morning`
(08:00). Setup: Dockerfile Path `Dockerfile.cron`, start command
`python scripts/daily_health_report.py`, restart policy Never, `SKIP_MIGRATIONS=1`.
Env: `DATABASE_URL` (Variable Reference; the script opens it read-only),
`POSTMARK_SERVER_TOKEN`, `ADMIN_EMAIL` (or `CRON_ALERT_EMAIL`), and optionally
`R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`
(without them the R2 check shows SKIPPED; a read-only R2 token is enough).
Exits 0 even when it finds problems; non-zero only if the checker crashes or the
email can't be sent. Try it first with `--dry-run` via Run Now. "No new data"
checks for Hansard and written questions use Parliament's sitting calendar
(What's On API `calendar/proceduraldates/{House}/sittingdates.json`, fetched once
per run, read-only); if it can't be reached they fall back to a weekday rule and
the report line says so. Thresholds and the manual `RECESS_PERIODS` override:
`CONFIG` / `RECESS_PERIODS` at the top of `hansard_archive/health_checks.py`.

### Other

- `backup-cron-r2` — daily pg_dump to Cloudflare R2
- **Stakeholder directory:** no automated cron. Ministerial meetings loaded via CSV upload + `stakeholder_directory/pipeline.py` when GOV.UK publishes quarterly transparency data. Committee evidence triggered manually via `/admin`. Lobbying register also manual.

## One-shot services

Settings: restart policy Never, `SKIP_MIGRATIONS=1`, internal `DATABASE_URL`.

`SKIP_MIGRATIONS=1` makes startup skip `db.create_all()`, the dev seeds and the
`seed_all_minister_links` background thread. (ALTER TABLE migration blocks have
`try/except` and are no-ops.)

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
- AI / data APIs: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (paid products only), `TWFY_API_KEY`, `NEWS_API_KEY`, `BSKY_HANDLE`, `BSKY_PASSWORD`
- Email: `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `EMAIL_TEST_MODE`, `CRON_ALERT_EMAIL`
- Billing (paid products; test keys during dev): `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`
- Backups: `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `BACKUP_ENCRYPTION_KEY`
- Flags / behaviour: `SEARCH_BACKEND`, `FEATURE_AUTH`, `FEATURE_ACCOUNT`, `STATS_CATALOGUE_ENABLED`, `BETA_USERS`, `SKIP_MIGRATIONS`, `RATELIMIT_ENABLED`, `APPROVED_EMAILS`, `APPROVED_DOMAINS`, `DISCOVERY_POLL_INTERVAL`
