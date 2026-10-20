# Decisions, lessons and history (moved out of CLAUDE.md)

> Moved from `CLAUDE.md` on 2026-10-20 during a trim. These are the stories and
> dated snapshots behind rules that still live (in short form) in `CLAUDE.md`.
> Read them when you need the "why"; the rules themselves are in `CLAUDE.md`.
> Dated status tables here are snapshots — they are not kept up to date.

---

## Phase 2A.5 work queue (snapshot, May 2026)

| Priority | What | Plan / location |
|---|---|---|
| High | PQ answer text caching | `docs/phase-2a5-pq-answer-text-plan.md` (6-stage build plan) |
| Medium | Route smoke tests + reserved-kwarg lint guard | Since built: `tests/test_routes.py` |
| Medium | Backup monitoring (Railway alert + `/health` backup-freshness) | Captured — no doc yet, ~2hr build |
| Later | `pg_dump -Fc` / `pg_restore -j4` migration for faster restores | Defer — current plain-SQL restore works after 13 May fixes |

By June 2026 `docs/orientation.md` recorded PQ answer caching as a backlog
slice-to-test idea rather than in flight.

## "Active work in progress" (snapshot, mid-2026)

- **Phase 2A** — Hansard archive live in production. WQ archive live. Member cache seeding live. PQ detail pages, archive search, and sitemap all live.
- **Stakeholder directory module** — foundation phase complete (schema, vocabularies, scoring module). Ministerial meetings ingested. Committee evidence partially ingested (written evidence re-run needed).
- **Hansard migration** — substantively complete. `SEARCH_BACKEND=hansard` is the production path for parliamentary search; TWFY is no longer the primary data source.
- **Phase 1 (compliance + auth + Stripe foundation)** — local-only build (see `docs/phase-1-completion.md`).
- **Phase 2 (paid product: stakeholder briefing pack)** — offline build, no public surface during development (see `docs/phase2-stakeholder-briefing-pack.md`).

## Minister-led search via Hansard backend (completed)

The original problem (keyword search missing ministers whose responses didn't
contain the search terms) was resolved by the search-finds-debates →
fetch-all-speeches architecture. Implementation plan as built:

1. `get_dept_minister_twfy_ids(dept_name)` — GOV.UK minister list for dept → resolve each name to TWFY person ID via Members API name match
2. `fetch_minister_speeches_on_topic(twfy_person_id, expanded_query, date_range)` — TWFY `getDebates?person=ID&search=EXPANDED_QUERY` — use AI-expanded query for better language matching
3. Run ALL minister fetches in parallel alongside existing keyword search in `debates_topic()`
4. Merge via `deduplicate_by_listurl()` → Phase 1 session expansion → minister flagging → group by debate

This works universally across all departments — pull full ministerial team → search their debates → merge.

## Source attribution — status table (snapshot, 14 May 2026)

| Content type | Source URL pattern | Link status (14 May 2026) |
|---|---|---|
| Hansard debates/sessions | `session.hansard_url` (stored in DB) | "View on Hansard ↗" in session header |
| Written Questions | `https://questions-statements.parliament.uk/written-questions/detail/{tabled_date}/{uin}` | Present but in small-print — promote to header |
| MP archive pages | `https://members.parliament.uk/member/{mnis_id}/` | Not linked — MNIS ID not in `cached_member` (uses TWFY ID) |
| | `https://www.theyworkforyou.com/mp/?p={member_id}` | TWFY ID available — simpler interim option |
| Theme/policy pages | Meta-pages — no single canonical source | N/A |
| Bills | `https://bills.parliament.uk/bills/{bill_id}` | Not yet ingested |
| EDMs | `https://edm.parliament.uk/early-day-motion/{edm_number}` | Not yet ingested |

Pending work approved 14 May 2026: promote PQ source link from small-print to
session header; MP pages: add TWFY link as interim; full parliament.uk link
requires `mnis_id` added to `cached_member`.

## WQ tracker deadline display (audience framing lesson)

The initial proposal for the WQ tracker showed an "internal departmental
deadline" alongside the Parliamentary deadline. Most users have no such concept.
The right answer was to show only the Parliamentary deadline (meaningful for
everyone) and leave internal deadlines out.

## WQ API — April 2026 correction notice and tracker regression

An earlier version of the WQ constraints section (present until April 2026)
documented three constraints that turned out to be false:

- *"tabledStartDate and tabledEndDate are silently ignored"* — **False.** The API does not have these parameters. The correct names are `tabledWhenFrom` and `tabledWhenTo`. We were passing the wrong names; the API was correctly ignoring them.
- *"answeringBodies causes 30s+ timeouts"* — **Conditionally false.** It times out without a date filter (full-table scan across 661k rows). Combined with `tabledWhenFrom`, it responds in ~2 seconds.
- *"isAnswered is silently ignored"* — **False.** The correct parameter is `answered` (enum: `Any`, `Answered`, `Unanswered`).

The API behaved exactly as the OpenAPI spec describes. Our diagnostics were
flawed because we passed wrong parameter names and drew causal inferences from
the wrong evidence. The `take=500 / max(tabled_dates) / client-side everything`
workaround was solving a self-inflicted problem.

**Tracker regression (April 2026):** caused by reintroducing the
`answeringBodies` parameter that two prior commits had removed with a clear
stated reason. The reintroducing commit's message claimed to be fixing an
indentation bug — the actual diff replaced the working architecture with a
previously-rejected approach. This is the origin of the "Chesterton's fence" and
"commit messages must describe the diff" rules.

## Incident-driven rules — where they came from

- **psql `-f` before the URL** — root cause of two days of silent restore failures, 12–13 May 2026.
- **Long-running scripts detached** — Stage C backfill (May 2026) was interrupted twice because the terminal session was killed overnight.
- **Batch-by-ID on Railway Postgres** — reference implementation `scripts/backfill_pq_answers.py` (commit `415382c`, 14 May 2026), after idle-connection kills crashed a load-all-then-iterate backfill.
- **Railway Query tab rejects LIMIT** (2026-05-26) — queries with `LIMIT` against `ha_stat_producer` / `ha_stat_publication` errored with `syntax error at or near "LIMIT"` in Chrome and Firefox, freshly typed. COUNT worked. Root cause unknown. DBeaver confirmed working for all query types on 2026-05-27.
- **DBeaver multi-statement verification** — refined from several near-misses, 28 May 2026.
- **Raw SQL must set `updated_at`** — first applied in the HESA de-registration, 27 May 2026.
- **One-shot services: capture logs before teardown** — the piece 2b diagnostic re-scan (28 May 2026) was needed because piece 2's `[SUB_PAGE]` diagnostic logs were lost when the service was deleted.
- **DISTINCT + ORDER BY on Postgres** — `/archive/mp/<id>` 500'd in production for ~5 days (481 GSC errors, May 2026). Fixed by adding `HansardSession.date` to the SELECT list.
- **Reserved Jinja globals** — May 2026: `_session_context()` in `hansard_archive/views.py` passed a `HansardSession` model as `"session"`. Adding `{% if session.get('admin_authenticated') %}` to `base.html` called `.get()` on the model and broke every `/archive/debate/...` page in production. Fix: a context processor injecting `admin_authenticated` directly. Smoke tests + reserved-kwarg lint now live in `tests/test_routes.py`.
- **Pre-share checklist** — May 2026: two share-blocking bugs (session detail pages 500 from the session variable collision; asking MP and answering minister names missing from all PQ detail pages) went undetected during a landing-page polish review and were only found by spot-clicking detail pages.
- **Never `git add -A`** — 2026-06-05, during the re-discovery merge, a single `git add -A` staged ~400 MB of junk including a 380 MB db backup; the commit had to be unwound and rebuilt from explicit paths. `.gitignore` was hardened the same day.
- **Parallel consoles** — 6 Jun 2026: a master push had to be stopped mid-flight; an uncommitted `admin.html` change was nearly lost; a test-push instruction was correctly halted because a production 500 was found after it was written.
- **Re-discovery fails loud** — Issue B, fixed 5 Jun 2026: a fetch failure now raises `StrategyFetchError` and the producer is not stamped.
- **Destructive-operations rules** — April 2026: an AI coding agent on Railway infrastructure (at another company) deleted a startup's entire production database and backups in one API call by autonomously "fixing" a credential mismatch.

## Committee evidence — first full ingestion (April 2026)

The first full ingestion produced only 7 written evidence records vs 4,993 oral
evidence records. Root cause: the ingester loops oral evidence for all 153
committees first (~2 hours), then written evidence. If Railway restarts the
container mid-run the daemon thread dies silently and written evidence is never
fetched. Recovery: run a full re-ingestion from the admin panel (Fetch & Ingest,
all committees, from 2024-01-01); the `UniqueConstraint(publication_id,
raw_organisation_name)` means oral evidence is skipped as duplicates and only the
missing written evidence is staged.

## Beta Railway service (deleted 2026-10-20)

A private beta Railway service (`wb-beta`) used to run alongside production at
`beta.westminsterbrief.co.uk`. It shared the production Postgres via a read-only
role (`wb_beta`), deployed from the `beta` branch, set `ENVIRONMENT=beta` and
`SKIP_MIGRATIONS=1`, and sat behind a simple password gate with a yellow BETA
banner. The service and its domain were deleted on 2026-10-20. The `beta`
*branch* and the `feature → beta → master` workflow remain. The beta gate /
banner code is no longer present in the codebase.
