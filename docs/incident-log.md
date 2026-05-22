# Westminster Brief — Incident Log

---

## INC-002 — Production outage: worker timeouts + /archive/mp 500 errors
**Date:** 2026-05-22
**Duration:** ~2–3 hours (approx 05:30–08:00 UTC)
**Severity:** High — site fully unavailable to users; all /archive/mp pages 500-ing after recovery
**Status:** Resolved

### What happened

The production site (westminsterbrief.co.uk) became unavailable. Cloudflare reported 524 errors (origin timeout). Gunicorn workers were being cycled every 120 seconds — each worker started, received a request, hung for exactly 120s, was SIGKILL'd, and the next worker repeated the same cycle.

A secondary issue emerged after initial recovery: all `/archive/mp/<id>` pages returned HTTP 500 due to a pre-existing code bug exposed when the site came back up.

### Root causes

**1. External scanner bot flood saturating Postgres connection slots**

Railway's Postgres proxy (hopper.proxy.rlwy.net:50798) was receiving hundreds of FATAL authentication failures per second from external scanner bots hammering the public endpoint with wrong credentials. This saturated Postgres connection slots. Flask startup inspection blocks — which run `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` queries for schema migrations — hung indefinitely waiting for a connection slot to become free. Each gunicorn worker hit this hang on boot, was killed at 120s timeout, and the next worker repeated the cycle.

**2. Single gunicorn worker monopolised by crawler traffic**

Even after initial recovery, the site was not reliably serving real user requests to the landing page. With `--workers 1`, legitimate requests from real users were queuing behind automated crawler/bot requests (~1 req/sec on PQ pages, ~1s each). The single worker had no capacity to spare.

**3. NameError: policy_filter undefined in archive_mp**

All `/archive/mp/<id>` pages returned 500 with `NameError: name 'policy_filter' is not defined`. The variable was used in the `render_template()` call at line 1212 of `hansard_archive/views.py` but was never initialised in the `archive_mp` function. The same variable is correctly initialised in `archive_index` and `archive_search` but had been missed in `archive_mp`.

### Resolution

| Step | Action | Result |
|------|--------|--------|
| 1 | Set `SKIP_MIGRATIONS=1` env var on Westminsterbrief Railway service | Bypassed startup DB inspection blocks; workers stopped hanging; site recovered at ~07:58 UTC |
| 2 | Changed gunicorn `--workers 1` to `--workers 2` in Railway start command | Second worker handles crawler traffic; real user requests no longer queued behind bots |
| 3 | Added `policy_filter = request.args.get("policy", "")` to `archive_mp` in `hansard_archive/views.py` | All `/archive/mp/` pages stopped 500-ing |
| 4 | Pushed master to Railway | Deployed fix; confirmed site serving normally |

### Commits

- `e88162c` — Fix: NameError policy_filter undefined on /archive/mp/<id> pages
- `b957084` — Merge beta into master: chore/railway-toml-remove-global-healthcheck (removed global healthcheckPath that was causing unnecessary restarts)

### Pending follow-up actions

- [ ] Remove `SKIP_MIGRATIONS=1` once startup blocks have connection timeouts added — currently it's a workaround, not a permanent fix
- [ ] Add `connect_timeout` to startup DB inspection blocks so future Postgres connection delays self-resolve rather than hanging indefinitely
- [ ] Investigate whether Railway Postgres proxy can be restricted to known IP ranges to reduce bot authentication noise
- [ ] Add smoke test for `/archive/mp/<id>` route to catch render_template variable errors before production

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
