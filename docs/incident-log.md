# Westminster Brief — Incident Log

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
