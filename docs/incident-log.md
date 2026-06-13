# Westminster Brief — Incident Log

---

## INC-008 — Cross-session UIN collision corrupted 690 PQ rows during pre-window backfill test

**Date found:** 13 June 2026 (caught at the gated `--test` step of the pre-window PQ backfill)
**Severity:** Medium — 690 recent (May-2026) PQ rows had content fields overwritten with July-2024 data; corruption window ~2h; caught by the insert/update-split tripwire before any full run; repaired same day
**Status:** Resolved — 689 rows fixed by re-ingest, 1 (UIN 444) restored from backup, 9 stray rows deleted; full-table corruption fingerprint = 0. Backfill blocked pending identity-key decision.

### What happened

The Phase 2A.5 pre-window PQ backfill (`scripts/backfill_pq_pre_window.py`) ingests
Written Questions tabled 9 Jul 2024 → the current floor, reusing `ingest_pq_date_range`
(upsert by UIN). Its gated `--test` chunk (re-ingest 2024-07-09..17) reported
**inserted=9, updated=690** instead of the expected ~699 inserts. The 690 "updated"
rows were existing **May-2026** questions: their `question_text`, `answer_text`, members,
`answer_date`, `is_answered`/`is_holding`/`is_withdrawn`, and `api_id` were overwritten
with **July-2024** content. `tabled_date`/`uin`/`heading`/`chamber` were left unchanged
(the update path doesn't set them), producing mismatched rows (May-2026 dates, July-2024
content).

### Root cause

**WQ UINs reset per parliamentary session and are NOT globally unique.** A new session
began ~13 May 2026, resetting UINs to low numbers (1, 10, 444, HL50, …); July 2024 (start
of the 2024-25 session) carries those same low UINs. `ha_pq.uin` has a `UNIQUE` constraint,
so the ingestor's upsert-by-UIN matched the 690 July-2024 questions to the already-present
May-2026 rows and **overwrote them** (collide-and-overwrite) instead of inserting.
Confirmed: 699 July source UINs = 690 collisions with May-2026 rows + 9 genuinely new
inserts.

### Resolution

1. **Stopped at the gated test** — the insert/update split (`updated=690`) was the tripwire;
   no full run was attempted.
2. **Confirmed pre-incident backup** (`daily/2026-06-13.sql.gz.gpg`, R2, 03:02 UTC, ~15h
   before the incident) as fallback — `backup-r2` cron run recorded `ok`; object confirmed
   in the R2 dashboard (147 MB).
3. **Bulk repair** — re-ingested 2026-05-13..14 via `ingest_pq_date_range` (each UIN
   re-fetched from the individual endpoint → correct current content). Result
   `inserted=12, updated=1815, errors=0` (1827 = all WQs tabled those 2 days; the 12 inserts
   were genuine May-2026 questions previously missing from the DB). Fixed 689 of the 690.
4. **Residue: 1 row (UIN 444)** escaped the bulk repair because its real May-2026 question
   had been **withdrawn at source** and no longer appears in the live API (confirmed: not
   returned for any May-2026 window, even `answered=Any`). The bad test had also overwritten
   its `api_id` to the July id (1721729), so the individual endpoint returned July data too.
   Restored from the pre-incident backup: extracted the correct row from the decrypted dump
   and ran a targeted `UPDATE … WHERE uin='444' AND tabled_date='2026-05-13'`. The real
   question is a withdrawn Home Office/Greenpeace PQ (`is_withdrawn=true`, `answer_date=NULL`,
   `api_id=1904612`) — which is exactly why it had vanished from the live API.
5. **Removed the 9 stray rows** — the 9 genuinely-new July-2024 inserts (UINs 900027–900039,
   tabled 2024-07-17) that didn't collide: `DELETE FROM ha_pq WHERE tabled_date < '2025-05-06'`
   (9 rows; FK-safe, no theme tags).
6. **Verified clean** — full-table corruption fingerprints all 0 (`answer_date < tabled_date`;
   2024-answer-on-2026-row; test-window `updated_at` residue); strays 0; `MIN(tabled_date)`
   back to 2025-05-06; total 100,527.

### Follow-ups

- **`scripts/backfill_pq_pre_window.py` is blocked** pending an identity-key decision — the
  backfill cannot key on UIN across a session boundary. Options: `api_id` (globally unique)
  as identity, or a composite key (uin + session/tabled_date). Cross-cutting (schema +
  `/archive/pq/{uin}` routing/SEO + `HaPQTheme` FK) → separate scoping pass.
- **CLAUDE.md known-issue added** (UIN non-uniqueness; dry-runs must predict insert/update split).
- **Pre-existing, separate:** recent-session PQ rows lack `heading` (cron ingests before
  Parliament assigns it; update path never refreshes it) — logged in `ideas-backlog.md`,
  noted as kin to the identity-key problem (both: ingest-once-never-re-pull).

### Lessons

- A dry-run that counts **source rows** is insufficient for an upsert ingestion — it must
  predict the **insert/update split against the existing DB**. The source-count dry-run
  looked healthy (58,129, within estimate); the collision was only visible as the
  insert/update split at the gated test.
- The gated `--test` (first chunk, with the split tripwire) is what contained the blast
  radius to 690 rows instead of the full ~58k range.

---

## INC-007 — Discovery worker wrote last-updated date instead of publication date

**Date found:** 2 June 2026 (pre-launch spot-check)
**Severity:** Low — caught before /stats launched; production /stats flag-gated OFF throughout; no user impact
**Status:** Resolved (root cause fixed; data verified clean)

### What happened

Phase 1.9 added a `first_published_at` column to `ha_stat_publication` so the
stats catalogue could order newest-first by source publication date (a launch
criterion). A pre-launch spot-check of a real page (MCS domestic battery
statistics, DESNZ) showed the catalogue's "Published" date as 28 May 2026,
while the GOV.UK source page showed "Published 29 May 2025 / Last updated
28 May 2026". The catalogue was showing the LAST-UPDATED date, not the
publication date — the exact misleading-order failure the feature exists to
prevent.

### Root cause

Two code paths populated the column using different GOV.UK APIs:

- A3 backfill (existing rows) used the GOV.UK **Content API**, which exposes
  `first_published_at` correctly.
- A2 discovery worker (new rows) used the GOV.UK **Search API**, which does
  NOT expose `first_published_at` at all — only `public_timestamp`
  (last-updated). An earlier "alignment" change requested `first_published_at`
  from the Search API, silently got nothing back, and fell through to
  `public_timestamp`. So the discovery worker wrote the updated date.

Verified directly against both APIs: the Search API result objects contain no
`first_published_at` field; the Content API returns it.

### Resolution

1. **Shared helper (root-cause fix):** `hansard_archive/discovery/pub_dates.py`
   is now the single source of truth — GOV.UK Content API `first_published_at`,
   ONS datasets API `release_date`, no fallback to any other field. Both the
   discovery worker (A2) and the backfill/compare script (A3) import it, so the
   two paths cannot diverge again. Commit `c0456e4`.
2. **A2 fixed:** discovery worker resolves the date via the Content API at
   new-row write time (cheap — new rows only), not from Search API hints. The
   `_parse_date_hint` path was deleted; `public_timestamp` is no longer a date
   source anywhere (it survives only as free-text classifier context).
3. **Read-only compare pass** over all 1,124 rows confirmed the existing data
   was already clean: `match: 1104, differ: 0, null_result: 20 (all ONS),
   errors: 0`. No corrective write was needed — the only wrong values had
   already resolved (GOV.UK metadata was in flux around the 28 May republish;
   stored and live now agree).

### Known follow-up (not a regression)

20 ONS publications have NULL `first_published_at` — the ONS datasets API does
not return a usable `release_date` for them via the current path. They sort
last under NULLS-LAST. Tracked as a Phase 1.9 follow-up (investigate ONS date
capture vs accept NULL); not blocking.

### Lessons learned

- **Silent fallbacks hide field-name mistakes.** Requesting a non-existent API
  field and falling through to a different one produced plausible-but-wrong
  data with no error. The fix removes the fallback entirely: absent date → None.
- **Two code paths populating one column must share the fetch logic.** The
  shared helper makes divergence structurally impossible — the same principle
  as a single source of truth for any cross-path constant.
- **Spot-checking real pages against source pre-launch is the process working.**
  The dry-run mechanics looked fine; only eyeballing a real page against GOV.UK
  surfaced the semantic error. This is why the launch criterion required it.

---

## INC-006 — DISCOVERY_DRY_RUN ambiguity caused unexpected Gemini API spend

**Date:** 27 May 2026
**Severity:** Low — no data integrity impact, contained cost overrun
**Status:** Resolved (same day)

### What happened

A producer-discovery script was run with the intent of validating producer
classification logic without spending Gemini API tokens. The `DISCOVERY_DRY_RUN`
env var was set, but the script's interpretation of "dry run" did not include
"skip Gemini calls" — only "skip DB writes." Result: Gemini classification was
invoked for ~150 candidate publications during what was assumed to be a zero-cost
validation pass.

### Root cause

The `DISCOVERY_DRY_RUN` flag's behaviour was ambiguous. The flag suppressed
database writes (its original purpose) but the producer-discovery flow had been
extended to call Gemini for classification BEFORE the write step. The flag was
never extended to also suppress the LLM call.

The script's docstring said "dry run mode" without specifying which side effects
were suppressed. The operator assumed broader suppression than was implemented.

### Cost impact

~150 Gemini Flash-Lite classification calls at standard rates. Total overrun
under £1. Not financially material; flagged because the operational discipline
matters more than the cost.

### Resolution

1. `DISCOVERY_DRY_RUN` flag retired entirely (commit `b719891`, 27 May 2026)
2. Producer-discovery script now requires explicit `--execute` flag to run any
   side-effecting operation, matching the pattern of all other scripts in the
   codebase (`extract_pub_data_files.py`, backfill scripts)
3. No "dry run" partial-suppression modes — either fully dry (no DB, no LLM,
   no external API calls) or fully executing

### Commits

- `b719891` — Chore: remove DISCOVERY_DRY_RUN flag from discovery worker

### Lessons learned

Partial-suppression flags accumulate semantic drift as scripts grow. A flag that
meant "no DB writes" in v1 doesn't reliably mean "no side effects" in v3 when v2
added an LLM call. Either fully-dry or fully-executing is unambiguous; partial
modes invite the operator to assume more suppression than the code implements.

The correct pattern (required for all scripts going forward): any script that
touches the DB, calls an external API, or spends money requires an explicit
`--execute` flag. Without it, the script logs what it would do and exits cleanly.

---

## INC-005 — Local-to-production connection prohibition

*(Captured as CLAUDE.md discipline rule, not a discrete incident.)*

---

## INC-004 — Railway log buffering masked sub-page fetch

*(Captured as CLAUDE.md one-shot service rule, not a discrete incident.)*

---

## INC-003 — Mobile search timeout
**Date:** 2026-05-22
**Duration:** Unknown — observed approximately 16:00 UTC; resolved by next-day check on 23 May
**Severity:** Medium — affected mobile users specifically; full functionality continued on desktop
**Status:** Resolved (apparent — not actively investigated)

### What happened

Mark experienced search functionality timing out on the Westminster Brief site when accessing from mobile. Approximate timing: 16:00 UTC on 22 May 2026. The same searches worked normally on desktop in the same time window.

By the time of the next-day check on 23 May, searches were working normally on mobile.

### What we know

- Mobile searches timed out around 16:00 UTC on 22 May
- Desktop searches in the same window worked normally
- By 23 May morning, mobile searches were working again
- No automated alerting fired; discovery was by personal observation

### What we don't know

- Exact timing — when the issue started, when it resolved
- Whether it was specifically search functionality or any complex query
- Whether it was related to the ongoing aftermath of INC-002 (cron lock contention had been resolved by 09:45 UTC, but residual effects or recurrence are possible)
- Whether it would have affected other mobile users or was specific to Mark's mobile network
- Whether the recovery was due to underlying conditions changing, or due to something done as part of INC-002 recovery work later in the day

### Lessons learned

- **Desktop checks are not a substitute for mobile checks.** Today's pattern — "fine on PC, timing out on mobile" — is a real failure mode that isn't visible from a developer's normal workflow. Without mobile network checks, this kind of degradation can affect users without being noticed.

- **Mobile networks have shorter effective timeouts.** A response that takes 8-10 seconds appears fine on desktop fibre but times out on 4G/5G. The slow-but-responding pattern from INC-002 Phase 3 (30-60s responses) would have been catastrophic for mobile users.

- **Without external monitoring from mobile network types, degradation can go undetected for the user segment most affected.**

### Pending follow-up actions

- [ ] External uptime monitoring should include mobile network simulation if possible (UptimeRobot's free tier may have limitations here; investigate paid tier options if mobile audience grows)
- [ ] When investigating future incidents, check mobile experience as a distinct verification step
- [ ] If incident recurs and is reproducible, investigate whether it's bot-related, query-complexity-related, or mobile-network-specific

### Connection to INC-002

INC-003 occurred in the same 24-hour window as INC-002 and may be a symptom of the same root causes (cron lock contention, slow query responses) manifesting differently for mobile users. If recurrence happens, investigating the connection becomes important. Currently filed separately because the symptom (search-specific mobile timeout) is distinct from INC-002's symptoms (whole-site hang, then NameErrors, then degraded response times).

---

## INC-002 — Production outage: worker timeouts + /archive/mp 500 errors + cron lock contention
**Date:** 2026-05-22
**Duration:** ~4 hours across three distinct phases (approx 05:30–09:45 UTC)
**Severity:** High — site fully unavailable for ~2 hours, then degraded for further ~2 hours
**Status:** Resolved

### What happened

The production site (westminsterbrief.co.uk) became unavailable from approximately 05:30 UTC and was not fully stable until approximately 09:45 UTC. The incident unfolded across three distinct phases, with each phase resolution exposing the next.

### Phase 1: Startup hang from Postgres connection slot saturation (05:30–07:58 UTC)

Cloudflare reported 524 errors (origin timeout). Gunicorn workers were being cycled every 120 seconds — each worker started, attempted to run startup migration blocks, hung for exactly 120s, was SIGKILL'd, and the next worker repeated.

**Root cause:** External scanner bots hammering Railway's public Postgres proxy (hopper.proxy.rlwy.net:50798) with hundreds of FATAL authentication failures per second saturated Postgres connection slots. Flask startup ALTER TABLE migration blocks hung indefinitely waiting for a connection slot.

**Resolution:** Set `SKIP_MIGRATIONS=1` env var on Westminsterbrief Railway service. Bypassed startup DB inspection blocks. Site recovered at ~07:58 UTC.

### Phase 2: NameErrors on /archive/mp/ pages exposed by recovery (~08:00–09:00 UTC)

Once the site recovered, all `/archive/mp/<id>` pages returned HTTP 500.

**Root cause:** Two separate pre-existing bugs in `archive_mp()` in `hansard_archive/views.py`:
- `policy_filter` variable used in `render_template()` but never initialised
- `analytics` variable passed as a kwarg to `render_template()` but never assigned (dead kwarg from incomplete earlier work)

Both were latent — they only manifested when the site started serving traffic again, because bots crawling MP pages exposed them immediately.

**Resolution:**
- `e88162c` — initialised `policy_filter = request.args.get("policy", "")` in archive_mp
- `4163711` — removed the dead `analytics=analytics` kwarg from the render_template call

Both pushed to master, deployed.

### Phase 3: Cron lock contention (~09:00–09:45 UTC)

After Phase 2 fixes deployed, a new degraded-performance pattern emerged: requests taking 30-60 seconds each rather than failing outright. Workers were responding but unable to complete queries quickly.

**Root cause:** `archive-cron-daytime-fri` fired at 09:00 UTC, imported `flask_app.py`, and ran `_run_startup_migrations()` — because `SKIP_MIGRATIONS=1` had only been set on the Westminsterbrief web service, not on any of the 9 cron services. The cron's own long-running archive ingestion SELECT on `ha_bill` held a ShareLock. The startup ALTER TABLE in the cron's app boot tried to acquire an ExclusiveLock and queued behind it. All subsequent queries on `ha_bill` and `ha_pq` (including homepage and PQ pages) queued behind the ALTER TABLE.

The startup-hang vulnerability we'd fixed on the web service had been replicated across every cron service because they all import `flask_app.py` at startup.

**Resolution:**
- Set `SKIP_MIGRATIONS=1` on all 9 cron services + discovery-worker (10+ services total)
- Killed 4 stuck PIDs (90661, 90873, 91074, 90447) via `pg_terminate_backend`
- Cron services confirmed not interfering with web traffic when verified manually at ~10:00 UTC

### Critical finding — Railway start command edits silently lose env vars

During Phase 3 investigation, it emerged that the `--workers 2` change made via Railway UI earlier in the day had silently dropped `SKIP_MIGRATIONS=1` from the Westminsterbrief service variables. The env var that we believed was set was not in fact applied. This is platform behaviour worth documenting.

### Commits

- `e88162c` — Fix: NameError policy_filter undefined on /archive/mp/<id> pages
- `4163711` — Fix: remove undefined analytics variable from archive_mp render_template
- `b957084` — Merge beta into master: chore/railway-toml-remove-global-healthcheck (separate work earlier in the day)

### How it was detected

Discovery happened by user-reported degradation rather than monitoring. Phase 1 was noticed when Mark hit the site himself. Phase 2 was noticed when MP profile pages returned 500. Phase 3 was identified by the slow response time pattern in Railway logs. No automated alerting fired at any point during the incident.

### Lessons learned

- **Workarounds tested on beta may not prove behaviour under production load.** The `SKIP_MIGRATIONS=1` approach worked when tested but its effectiveness depended on the env var being present, which it wasn't reliably.

- **Railway start command edits silently affect env vars.** Editing the start command via UI is not just a string change — it can affect adjacent configuration. Future config changes should explicitly verify env vars remain set after.

- **Pre-existing bugs in low-traffic code paths surface immediately under bot crawling.** `/archive/mp/` pages had two latent bugs that hadn't been seen because the function wasn't being hit often. Bots crawling systematically expose every latent bug in every page.

- **Cron services importing `flask_app.py` is an architectural choice with consequences.** Today's incident showed that startup behaviour scales differently across 10+ services than across 1. The pattern needs reconsidering before more services are added.

- **Detection happens by chance, not by design.** Three phases of the same incident were each discovered through Mark's own checking rather than automated alerting. External uptime monitoring would have caught Phase 1 within minutes rather than tens of minutes.

### Pending follow-up actions

- [ ] Remove `SKIP_MIGRATIONS=1` once startup blocks have proper connection timeouts added — currently it's a 10+ service workaround dependency, not a permanent fix
- [ ] Add `connect_timeout` to startup DB inspection blocks so future Postgres connection delays self-resolve rather than hanging indefinitely
- [ ] Investigate whether Railway Postgres proxy can be restricted to known IP ranges to reduce bot authentication noise
- [ ] Add smoke test for `/archive/mp/<id>` route to catch render_template variable errors before production
- [ ] Document Railway config behaviour: changes to start commands may affect env vars; verification needed after such changes
- [ ] Architectural decision needed: should cron scripts import `flask_app.py` at all, or should they use a lightweight context that doesn't include migration blocks? (See Opus brief on Gap 2 from 22 May.)
- [x] **External uptime monitoring set up** (2026-05-22 evening) — three monitors via UptimeRobot covering homepage, /archive, and /health endpoint
- [ ] Migration strategy needs architectural decision: keep SKIP_MIGRATIONS with explicit runner, add lock_timeout to ALTERs, move to Alembic, or restrict migration runs to the web service only. (See Opus brief on Gap 1 from 22 May.)

---

## INC-001 — Data restoration event triggered outside this conversation
**Date:** 2026-05-21
**Duration:** Approximately 1 hour from discovery to verified recovery (the underlying restore in the parallel conversation happened earlier in the day; exact timing not known)
**Severity:** High — production data was modified by a restore from R2 backup; no permanent data loss but full state of the database was uncertain for the duration of the incident
**Status:** Resolved

### What happened

During a planning session in this conversation, a check of production database state revealed unexpectedly low row counts — approximately 102 hansard sessions instead of the expected ~5,000, and parliamentary questions stopping at 20 March 2026 (current data was missing). This appeared at first to be data loss.

Investigation revealed that a data restore from the daily R2 backup had been authorised and executed by Claude Code in a separate, parallel conversation earlier the same day. The data observed was the restored state, not a "loss."

**The original trigger for the restore remains unresolved.** What initially caused Code to authorise a restore in the parallel conversation is not recoverable from this conversation's context, and was not documented elsewhere at the time. This is itself a finding — substantial production work was done without an artifact that would allow future reconstruction.

Additionally, a credential string was surfaced in a screenshot shared during the parallel Code conversation. The credentials needed to be rotated immediately to prevent any exposure.

### Root causes

**1. Lack of cross-conversation context for production-affecting actions**

Code conducted a substantial production data operation (a backup restore) in one conversation without Mark having complete recall of that conversation when working in a different conversation later. This created the perception of data loss when in fact a recovery operation had succeeded earlier in the day.

The system allows Code to be productive across parallel conversations, but there's no mechanism by which the assistants in different conversations know what each other has done. Both Code and the planning assistant operated in good faith but with different views of the actual production state.

**2. Sensitive credential surfaced in a shared screenshot**

A screenshot shared during the parallel conversation contained a visible production credential string. This had to be assumed compromised the moment it was visible to any system that could capture image data outside the immediate conversation context.

**3. Schema state inconsistency between database and application code**

The restore did not include 21 rows from `StatPolicyArea` that had been added since the most recent backup. These rows needed to be backfilled to restore full functionality (the Phase 1 stats infrastructure depends on the policy area mappings).

### Resolution

| Step | Action | Result |
|------|--------|--------|
| 1 | Verified the unexpected row counts represented a deliberate restore, not data loss, by tracing back through Code's parallel conversation history | Confirmed restore had succeeded; data was as expected post-restore |
| 2 | Deduplicated 108 `ha_pq` IDs that had been double-restored | Restored normal PQ table state |
| 3 | Rotated the surfaced production credential via Railway's UI | Credential rotation completed cleanly; exposure window closed |
| 4 | Ran `scripts/backfill_stat_policy_areas.py` to add 21 missing `StatPolicyArea` rows | Restored Phase 1 stats infrastructure to working state |
| 5 | Verified backup chain: confirmed Cloudflare R2 contained 28+ daily encrypted backups, restore had succeeded, restore tested per runbook on 13 May per documentation | Confirmed the backup system itself was sound |

### How it was detected

Discovery happened during a routine check of production database state at the start of a planning session, not via monitoring or alerting. There was no automated alert that flagged the restore had happened, nor that flagged row counts as unexpected. Detection was incidental to other work.

### Lessons learned

- **Production-modifying operations across parallel conversations need explicit tracking.** When Claude Code performs substantial production data work (restores, schema changes, data migrations), there's currently no way for that to be reflected in any other conversation Mark might have. The pattern "Mark works with Code in one conversation, Code does production work, Mark works with another assistant later" needs more structure to be safe at scale.

- **Screenshots containing production data need pre-share review.** Anything captured by screenshot might include credentials, database state, or other sensitive content. The "share a screenshot" pattern is genuinely useful for diagnosis but the cost of accidental exposure is high.

- **Detection of state changes happened by chance.** If the planning session hadn't included a state check, the restore might have remained unknown for longer. This is the same monitoring gap that INC-002 surfaced from a different angle: external visibility into the system's state is largely manual.

### Pending follow-up actions

- [ ] Establish a convention for Code conversations: when production data is modified, Code writes a record to a known location (e.g. `docs/operations-log.md` or similar) before the conversation ends, so subsequent sessions can pick up context
- [ ] Document credential rotation procedure in a runbook (currently it's "remember to do it")
- [ ] Add automated alerting for unusual production state changes (e.g. sudden drop in row counts on key tables would have detected this immediately)
- [ ] Review what's the right level of caution for screenshots — possibly establishing a pre-share visual check habit, or scrubbing tools, depending on what's practical

### Connection to INC-002

INC-001 and INC-002 share one underlying theme: Mark discovered both incidents through manual checks rather than automated alerting. Both were resolved within hours but the discovery latency could have been minutes if external monitoring was in place. The "Set up external uptime and health monitoring" action from INC-002's follow-up list applies equally here.

---

## Log format reference

Each entry should include:
- **INC-NNN** — sequential ID
- **Date** — UTC date
- **Duration** — time between first impact and full resolution
- **Severity** — Low / Medium / High / Critical
- **Status** — Investigating / Mitigating / Resolved
- **What happened** — plain English narrative
- **Root causes** — specific technical causes (not symptoms)
- **Resolution** — ordered steps taken
- **Commits** — any code changes deployed
- **Pending follow-up** — open items that remain after resolution
