# Westminster Brief — Project Instructions

> **This repo is public.** Everything in this file (and in `docs/`) is world-readable.
> Never add hostnames with ports, connection strings, passwords, tokens, account
> emails or personal details. Point to the Railway dashboard instead.

---

## Project orientation

Westminster Brief is a UK parliamentary research tool: a free, indexed public
Hansard / Written Questions / statistics archive plus free research tools, with
paid products planned on top. Live at `westminsterbrief.co.uk` on Railway. Built
by Mark Forde as a side project (~24 hours/week).

**Audience:** public affairs and policy professionals, charities and trade
bodies, journalists, and researchers/academics. Civil servants may use the free
tools, but they are not the target or paying audience.

**Monetisation direction (Oct 2026):** first paid product planned is a weekly
plain-English sector digest email (~£8/month), later Individual / Team /
Organisation tiers. The archive stays free and indexed. Older docs that describe
the £49 stakeholder briefing pack as the first paid product
(`docs/phase2-stakeholder-briefing-pack.md`) predate this.

**Current state:** don't rely on this file for it. Read `docs/deploys.md`
(recent pushes, production SQL, half-states) and `docs/orientation.md` (derived
snapshot — regenerate it if stale), plus "Known issues" below.

**Three-role working model:**
- **Mark** — developer and product owner. Makes all decisions.
- **Claude Code** — implementer. Writes code, runs builds, manages files. Proceeds directly on clear tasks.
- **Opus (chat session)** — strategic advisor for architecture, product strategy, stuck debugging. See `docs/working-with-opus.md`.

Briefs for Code from Opus are written in fenced code blocks (copy-button UI when pasting).

---

## How every session should start

1. Read this file — at minimum this section, "Git and pushing", "Destructive operations", and any section relevant to the task
2. Run `git log --oneline -10` and check which branch you're on
3. Check `docs/deploys.md` — last known-good master SHA, outstanding half-states
4. For any module you're about to change, read its design doc (see "Docs map")
5. **Do not assume knowledge from past sessions** — verify by reading docs on disk. Docs in `docs/` are live and may change between interactions; re-read rather than relying on an earlier read.

**Status-check before scoping.** Specs capture locked decisions, not always what's
been built since. Before drafting a plan, ask: "What's actually been built vs what
the doc lists as pending?" The check is cheap; scoping against a stale spec wastes
work and risks regressions.

---

## Branches and workflow

```
feature/xxx  →  beta  →  master
```

- **`master` is production.** Railway auto-deploys the web service on every push to `master`.
- **`beta` is the default working branch** and must always be a superset of `master`. Feature branches merge into `beta`; once validated, merge `beta` → `master`.
- **There is no beta deployment** (the beta Railway service and `beta.westminsterbrief.co.uk` were deleted 2026-10-20). Preview new features on production behind a feature flag in `beta` state (see "Feature flags").
- **All Railway services build from `master`**, including every cron (`pq-cron-afternoon` was switched from `beta` on 2026-10-20). A push to `beta` deploys nothing.
- **A push to `master` redeploys every cron service and kills any run in progress** (2026-10-20: a push stopped the first `bills-cron` run at 275/797). Avoid pushing during cron windows (UTC): 03:00–03:15 backup, 06:30–07:15 bills, 08:30–09:00 / 09:30–10:00 / 14:00–14:30 PQ crons (and Mondays' PQ run), and the first ~5 minutes of each hour 11:00–23:00 Mon–Thu (archive daytime). Batch changes into one push where possible, and check `list-deployments` for a running cron before pushing.
- **Return to beta after operational master work.** If you must commit on `master` directly (e.g. a Railway one-shot tracks `master`), finish that reason, then switch back to `beta`. Don't keep committing on `master` by momentum — including doc commits. The `master → beta` reconciliation merge is an occasional fix, not a routine direction.
- **Exploratory work:** for a substantial new feature/direction, suggest a feature branch (e.g. `experiment/inquiry-tracking`), build a v1 sketch there, review, then merge, iterate or shelve.
- **Plan mode for anything touching >3 files or >1 system** — ask before building, not after.

**Parallel work (two consoles):** prefer a separate `git worktree` per console.
If sharing one checkout: same branch, different files, no uncommitted changes
left floating, no reset/rebase/force-push, and **only one console promotes to
master at a time**. Full rules: `docs/ops/parallel-working.md`.

### Deployment discipline — local-first for major Phase builds only

Applies to major Phase builds (compliance/auth/Stripe foundation, paid products).
Not to general site work, bug fixes or content updates.

- Phase work stays local by default. Don't push Phase-in-progress changes to Railway.
- Mark may ask for specific small unrelated changes to be pushed during Phase work. Push only what he asks for; in-progress Phase work doesn't bundle in.
- When the Phase is complete and reviewed, deployment is a deliberate single step.
- Stripe stays in test mode throughout the Phase build.

Reason: Phase builds touch foundations (compliance, auth, billing, AI) where
half-built state on the live site damages trust.

---

## Git and pushing

**Never push automatically.** Always commit locally and show what changed, then
wait for explicit instruction. Mark will say "push" or "push it". This applies to
small fixes and template changes as much as anything else.

### Never `git add -A` / `git add .` — explicit paths only

The working tree permanently carries untracked clutter (manifesto PDFs in
`docs/`, a large `intelligence.db.backup`, logs under `logs/`, `scripts/_*`
scratch scripts, `*_output.txt` probes, screenshots in `static/`). Stage with
explicit paths only. `.gitignore` is a backstop, but the clutter drifts — name
the files you mean.

### deploys.md — push discipline

`docs/deploys.md` is the append-only ledger of every production-touching
operation and the source of truth for production state.

Before every `git push` to `master`, add a row to its push log **in the same
commit as the work**:

| Field | What to put |
|-------|-------------|
| Date | YYYY-MM-DD |
| SHA | 7-char short SHA of HEAD commit being pushed |
| Summary | One line — same as the commit message subject |
| Operator | Mark (or "Claude Code" if acting on explicit instruction) |

**Never push to master without updating `docs/deploys.md` first.**

**Exception — emergency reverts:** if production needs an immediate hot revert,
push the revert first to restore service, then update `docs/deploys.md` within
the hour. Speed of recovery takes priority over logging.

### Pre-push checklist

1. `python -c "from flask_app import app; print('OK')"` — must return OK. (Caution: see Known issues — with a production `DATABASE_URL` in `.env` this opens a production connection.)
2. `python -m pytest tests/test_routes.py` — route smoke tests + reserved-kwarg lint.
3. `git status` — only intended files staged. No `.env`, debug scripts, or unrelated changes.
4. Confirm you're on a known-good branch (not detached HEAD or an experiment branch).

After Railway deploys, verify:

| Check | URL |
|---|---|
| Health (commit hash + API status) | `https://westminsterbrief.co.uk/health` |
| PQ detail page (old address: must 301 to `/archive/pq/<yyyy-mm-dd>/111792`, which renders) | `https://westminsterbrief.co.uk/archive/pq/111792` |
| Archive search with filter | `https://westminsterbrief.co.uk/archive/search?q=franchising` |
| Sitemap | `https://westminsterbrief.co.uk/sitemap.xml` |
| Archive home | `https://westminsterbrief.co.uk/archive` |

### Session summary protocol

When Mark says "session summary please", produce:

```
## Session summary — YYYY-MM-DD

**Master commits today**
- `<sha>` — <description>

**Beta commits today**
- `<sha>` — <description>

**Production SQL today**
- <statement + context>, or: none

**Outstanding items / half-states**
- <anything left mid-flight>

**Known-good master commit**
`<sha>` — <description>

**Before next session**
- <anything to be aware of>
```

When asked "what's the current state?" / "where are we?": read `docs/deploys.md`
and report the last master push (SHA + date), any recorded half-states, and the
most recent production SQL entry.

---

## Destructive operations — absolutely forbidden

Context: in April 2026 an AI coding agent on Railway infrastructure deleted a
startup's entire production database and backups in one API call by autonomously
"fixing" a credential mismatch. These rules make that failure mode impossible here.

**Infrastructure operations:**
- Never call the Railway API directly with destructive verbs (DELETE on volumes, services, environments, deployments, domains)
- Never run railway CLI commands that delete, destroy, remove, or detach resources
- Never call any cloud provider API (AWS, GCP, Cloudflare, Postmark, Stripe) with destructive operations
- Never use a CLI token, API token, or credential found anywhere in the codebase for a task unrelated to the documented purpose that token was created for

**Database operations:**
- Never run `DROP TABLE`, `DROP DATABASE`, `DROP SCHEMA`, or any other DDL drop statement
- Never run `TRUNCATE` against any table
- Never run `DELETE FROM table` without an explicit `WHERE` clause that targets specific rows
- Never run mass `UPDATE` against entire tables without explicit per-row scoping
- Never run raw SQL on production. Local SQLite is fine; production Postgres on Railway is not Claude Code's territory.

**Git operations:**
- Never run `git push --force` or `git push -f`
- Never delete branches, tags, or remotes
- Never run `git reset --hard` against work that hasn't been committed
- Never run `git clean` with destructive flags

**File operations:**
- Never run `rm -rf` on anything outside `/tmp/` or local virtual environments without explicit instruction
- Never delete files from `migrations/`, `docs/`, `templates/`, `static/`, or any directory that contains canonical project state
- Never delete or modify `.env`, `requirements.txt`, `CLAUDE.md`, `Procfile`, `railway.toml`, or any other infrastructure config file without explicit instruction

### When something seems broken — STOP, don't fix

If Claude Code encounters any of these, STOP and surface to Mark:
- Authentication or credential mismatch
- API token rejected or expired
- Database connection failure with unclear cause
- Production showing unexpected state (rows missing, schema drift, unexpected migrations)
- Any error suggesting production is in a state Claude doesn't understand

The correct response is always: report what's happening, propose options, wait for direction.

### Token hygiene

Credentials are documented per-purpose; their stated purpose IS their permitted
use. If a task needs a credential that isn't already wired up through the
documented mechanism (env vars, config referenced in code), the task stops. Mark
provisions credentials. Claude Code uses them as documented. No improvisation.

---

## Data safety and production

- **No local Python scripts against production (INC-005).** Script writes to production run as Railway one-shot services, not from a local machine.
- **Manual production SQL is Mark's**, via DBeaver (public URL from Railway dashboard → Postgres → Connect, SSL required). Don't rely on the Railway Query tab for anything with `LIMIT` — it errors on some tables.
- **Production SQL log:** any SQL run against production (console, psql, DBeaver, or a script using the production `DATABASE_URL`) is appended to "Production SQL log" in `docs/deploys.md`, dated, full statements, one-line context.
- **DBeaver: verify against DB state, not intent.** Its default runs only the statement under the cursor; a block can look "run" when one statement executed. After any multi-statement operation, SELECT the expected end-state (row counts, status values, timestamps). If it disagrees, the script didn't run as expected — investigate before declaring done.
- **Raw SQL must explicitly set `updated_at = NOW()`** on ORM-managed tables — the `onupdate` hook only fires for ORM writes, and the audit trail relies on it.
- **Data operations at scale: dry-run → spot-check → execute.** Dry-run proves mechanics; spot-check proves values are correct against source; execute only after both, with a decision rule (0 mismatches → proceed; 1–2 → investigate; 3+ → pause). **For upserts, the dry-run must predict the insert/update split against the existing DB**, not just count source rows (INC-008).
- **After fixing a caching bug, clear the cache** at `/admin` — bad data persists until TTL (6h searches, 30 days sessions).
- **One-shot Railway services:** capture the FULL log to a local file and confirm its contents *before* deleting the service. Procedure: `docs/ops/railway-services.md`.
- Bulk single-transaction inserts need ~2–3× final data size in volume headroom — check before `--execute` on 200k+ rows.

---

## Product rules

### Audience and copy

- Default to the broadest reasonable audience. Ask: "would this make sense to a charity policy officer or a journalist?" If it only makes sense inside government, redesign or relocate it.
- Avoid insider workflow framing: "internal deadline", "your department", departmental jargon ("Parliamentary Branch", "Q&A team", "submission deadline"), or assuming users are drafting answers (most are tracking).
- Prefer parliamentary-side facts (what was tabled, what's due, who engaged with whom) and neutral language ("Question", "Answer due", "Department for Education").
- Where audiences genuinely conflict, flag it to Mark — don't silently optimise for one.
- **About page** states "Westminster Brief is run by Mark Forde" (added 2026-10-20).
- **Marketing language must be evidence-based.** "Built for" is fine; "trusted by" requires actual trust. No "thousands of users", "industry-leading", "loved by professionals". Honest framing is a positioning asset with this audience.
- **Cautious language** — nothing should sound definitive. "May help with briefing" not "saves hours". AI outputs are aids, not answers.

### Output rules — free vs paid

**Free toolkit (default): factual or extracted, never authored.** The free tools
(Written Questions Scanner, Today's PQs Tracker, MP Research, Member Profiles,
Hansard Search, Stakeholder Directory, archive, and any future free features)
find and surface evidence. They do not draft positions, lines to take,
recommended responses, or anything that reads as authored professional work
product (minutes, submissions, drafted lines). AI in free features (summary,
classification, extraction) operates on material the tool actually retrieved,
with citations — not on training-data knowledge.

Out of scope for free features: AI-drafted PQ responses, suggested ministerial
statements, auto-generated press lines, draft holding lines, recommended Q&A
briefs, suggested engagement approaches, or AI inference about any individual
parliamentarian's likely position, sympathy or behaviour. "Key ministerial
statements" (verbatim extraction with citations, parser-rejection of unmatched
quotes) is consistent with this rule.

**Free features run on Gemini only. Claude is never invoked from a free-feature code path.**

**Paid products: authored inference permitted, with all six safeguards:**
1. **Source citation on every claim** — Hansard URL, division ID, WQ UIN, committee record. Floating assertions must be rejected by the pipeline.
2. **Honest confidence** — high/medium/low plus one-sentence reasoning. Thin data → "Insufficient parliamentary record to assess", not a low-confidence guess.
3. **Non-removable AI disclosure** — per-page footer "AI-generated analysis. Verify before publishing." Cannot be removed at the standard tier.
4. **Observational, not predictive** — "MP X has voted in favour of similar measures on [dates]", never "MP X is likely to support this bill".
5. **Neutral framing advice, not advocacy guidance** — describe patterns ("contributions have emphasised academic evidence"); never direct lobbying ("lobby this MP via the academic angle").
6. **No official voice** — outputs read as a research deliverable, not a government submission, briefing or line-to-take. No "the recommended approach is", "it is suggested that", "officials may wish to".

**Model by product type:** premium per-request paid briefings use the top Anthropic model (Claude Opus); Sonnet for development testing only, never production. **Exception — the weekly digest** (first paid product) is a bulk output and uses a cheaper model (Gemini Flash / Flash-Lite class or similar), not Opus, with human review: Mark checks each issue by hand for the first couple of months. The six safeguards above still apply to it.

**Hard cost ringfence:** Claude is invoked ONLY for paid product runs — never for
a free feature, regardless of user tier. The `tier` column on users
(public_sector / standard) records eligibility, not entitlement. Tier-to-model
binding must be enforced at the LLM abstraction layer with a code-level guard,
not by convention.

If a feature blurs the line (e.g. a paid "preview" visible to free users),
apply the stricter free rule until the boundary is explicit. Don't expand
authored inference into free features without an explicit decision — stop and
surface to Mark.

### Source attribution

Every piece of content links to its authoritative source, visibly — this is the
trust mechanism, not boilerplate. Patterns: Hansard `session.hansard_url`; WQs
`https://questions-statements.parliament.uk/written-questions/detail/{tabled_date}/{uin}`;
MPs `https://members.parliament.uk/member/{mnis_id}/`;
Bills `https://bills.parliament.uk/bills/{bill_id}`; EDMs
`https://edm.parliament.uk/early-day-motion/{edm_number}`. (May 2026 status
table: `docs/history/decisions.md`.)

### Design

- `docs/design-principles.md` is the authoritative visual and copy guide — read before any UI work. Restrained, content-first, type-led; reference gov.uk, FT, Stripe docs; no gradients, glassmorphism, oversized hero text, generic SaaS copy.
- **Desktop-first** (mobile later). **Information density over whitespace.**
- **Download everything** — every results view must be exportable; Word is the primary format. Non-negotiable.
- **Don't over-build** — accessible, clean, downloadable information. When in doubt, do less but do it well.
- **The Parliamentary Research Tool is the gem** — most important, most complex; give it the most care.

---

## Architecture map

### Stack
- **Backend:** Flask 3.0 with blueprints, gunicorn on Railway (Linux/Docker, Python 3.12)
- **Database:** SQLite locally → PostgreSQL (PG17) on Railway, switched via `DATABASE_URL`. SQLAlchemy **pinned to 2.0.49** (see Known issues).
- **AI:** Google Gemini (`google-genai`; mostly `gemini-2.5-flash-lite`, embeddings `gemini-embedding-001` — check code for current IDs) for free features. Anthropic Claude Opus for premium paid briefings only; the weekly digest uses a cheaper model (see "Output rules").
- **Frontend:** Jinja2 templates + vanilla JS, CSS in `static/style.css`
- **Auth:** Flask-Login with werkzeug password hashing. **Exports:** python-docx.
- **Email:** Postmark. **Backups:** daily encrypted pg_dump to Cloudflare R2.

### Code layout
```
flask_app.py            App config, core models (User etc.), auth, admin, health, sitemap, blueprint registration, startup migrations
hansard.py              Written Questions Scanner (/questions) — queries ha_pq
tracker.py              Today's PQs + AI categorisation (/tracker)
mp_search.py            MP/Peer PQ research (/mp_search)
biography.py            Member Profiles with AI summary (/biography)
debate_scanner.py       Parliamentary Research Tool (/debates)
stats_catalogue.py      Statistical Publications catalogue (/stats, flag-gated)
feature_flags.py        feature_enabled(): hidden / beta / live
hansard_archive/        Public archive: models, ingestion, pq_ingestor, views (/archive, /brief), slugs, tagger, stats discovery
stakeholder_directory/  Stakeholder Directory: models, ingesters, normalisation, scoring, pipeline
migrations/             One-off migration scripts
scripts/                Crons, backfills, backup/restore (archive_cron, ingest_pq_cron, rediscovery_cron, restore_from_backup …)
tests/                  pytest suite (tests/test_routes.py = smoke tests + reserved-kwarg lint)
templates/              Jinja2 — base.html is the master layout
```

### Tool name → URL → file

| Tool name (navbar/home)     | URL          | Backend file        | Template                        |
|-----------------------------|--------------|---------------------|---------------------------------|
| Written Questions Scanner   | `/questions` | `hansard.py`        | `templates/index.html`          |
| Today's PQs Tracker         | `/tracker`   | `tracker.py`        | `templates/tracker.html`        |
| MP PQ Research              | `/mp_search` | `mp_search.py`      | `templates/mp_search.html`      |
| Member Profiles             | `/biography` | `biography.py`      | `templates/biography.html`      |
| Parliamentary Research Tool | `/debates`   | `debate_scanner.py` | `templates/debate_scanner.html` |

"Hansard" appears in both the WQ Scanner (Parliament WQ API / `ha_pq`) and the
Research Tool (debate transcripts). Use the tool names above, not "Hansard tool".

### External APIs

| API | Env var | Used for |
|-----|---------|----------|
| Google Gemini | `GEMINI_API_KEY` | Summaries, embeddings, categorisation, stats discovery (free features) |
| Anthropic Claude | `ANTHROPIC_API_KEY` | Paid product outputs only — never from free features |
| News API | `NEWS_API_KEY` | Media scan in Smart Alerts |
| Bluesky | `BSKY_HANDLE`, `BSKY_PASSWORD` | Stakeholder social monitoring |
| Parliament APIs | none (public) | Hansard, Written Questions, Members, Committees, Bills |

Confirmed parameters, response shapes and gotchas: `docs/api-reference.md`.
Full env var list (names only): `docs/ops/railway-services.md`.

**TheyWorkForYou is not used** (removed Oct 2026: commercial use needs a paid
licence, and the same Hansard data is in our own archive under the Open
Parliament Licence). Don't reintroduce TWFY calls or links without a licence
decision from Mark. The `member_link` and `cached_twfy_search` tables keep their
names; `cached_twfy_search` is the general search cache (`CachedSearch`).

### Feature flags

`feature_enabled(flag)` in `feature_flags.py` reads an env var at call time:
`hidden` (default, fail closed) / `beta` (only signed-in users whose email is in
`BETA_USERS`) / `live`. Used for `FEATURE_AUTH`, `FEATURE_ACCOUNT`.
Plain on/off env vars (not `feature_enabled`): `STATS_CATALOGUE_ENABLED` (any
non-empty value enables `/stats`; read at import, so needs a redeploy).
`SEARCH_BACKEND` is no longer read by any code (the Research Tool always uses
the Hansard / own-archive path); delete it from Railway once the
TheyWorkForYou removal is deployed and confirmed.

### Railway (summary)

- Web service (named `Westminsterbrief` on Railway) auto-deploys from `master`; `jubilant-intuition` is the backup cron's private-network name, not the web service. Crons: Hansard archive (3), Written Questions (4), member cache, stats re-discovery, R2 backup. Schedules, password-rotation redeploy list, one-shot procedure: `docs/ops/railway-services.md`. Per-service setup: `docs/railway-cron-setup.md`.
- Production connection details: Railway dashboard → Postgres → Connect. Never write the hostname/port into the repo.
- Variable References resolve at deploy time — after a Postgres password rotation, redeploy **every** consumer service, then check `/health`.
- DNS is in Cloudflare (domain registered at GoDaddy).

### Docs map

| File | What it is |
|---|---|
| `docs/deploys.md` | Push log + production SQL log — source of truth for production state |
| `docs/orientation.md` | Derived current-state snapshot (regenerate if stale) |
| `docs/ideas-backlog.md` | Active and killed ideas — capture new ideas here |
| `docs/incident-log.md` | Incidents INC-001…, root causes, follow-ups |
| `docs/working-with-opus.md` | How Mark and Opus work; review disciplines |
| `docs/launch-readiness.md` | Gate for flipping `FEATURE_AUTH` / `FEATURE_ACCOUNT` live |
| `docs/pre-launch-checklist.md` | Legal/compliance + SEO checklist |
| `docs/recovery-runbook.md` | Disaster recovery (drill 13 May 2026) |
| `docs/design-principles.md` | Visual + copy guide |
| `docs/api-reference.md` | Parliament / GOV.UK API reference (its TWFY section is historical) |
| `docs/wq-api-constraints.md` | Written Questions API parameters, examples, domain facts |
| `docs/parliamentary-debate-types.md` | Hansard debate type taxonomy |
| `docs/research-tool.md` | Research Tool design notes, canonical test case, testing protocol |
| `docs/stakeholder-directory-design.md` | Stakeholder Directory spec |
| `docs/ops/railway-services.md` | Railway services, cron schedules, one-shots, env var names |
| `docs/ops/parallel-working.md` | Two-console / worktree rules |
| `docs/dev/coding-gotchas.md` | Code examples behind the gotchas below |
| `docs/history/decisions.md` | Historical decisions, incident origins of rules, dated snapshots |
| `docs/phase-1-completion.md`, `docs/phase2-stakeholder-briefing-pack.md` | Phase 1 record; Phase 2 briefing-pack scoping |

**Module design docs are authoritative for their module** and override this file
where they conflict. If they disagree, raise it rather than guessing.

---

## Conventions and gotchas

Worked examples for most of these: `docs/dev/coding-gotchas.md`.

### Local environment (Windows)
- Windows 11, PowerShell, Python 3.13 locally (production is 3.12). Start: `pip install -r requirements.txt` then `python flask_app.py` → `http://127.0.0.1:5000` (`/home` for the dashboard).
- Postgres client tools: `C:\Program Files\PostgreSQL\18\bin\` (on PATH). PG16 server at `C:\Program Files\PostgreSQL\16\bin\` (not on PATH) for local restore testing. `gpg` is not on PATH — use `C:\Program Files\Git\usr\bin\gpg.exe`.
- **Scripts calling external binaries** (`gpg`, `psql`, `pg_dump`) via `subprocess.run()` must use platform-resolved constants, not bare names — follow `_GPG` / `_PSQL` in `scripts/restore_from_backup.py`.
- **Always surface subprocess stderr** regardless of exit code, decoded with `errors="replace"`. Silent "success" with no output is a common failure mode.
- **`stdout=PIPE` + large output = deadlock.** For big outputs (e.g. psql restoring a 460 MB dump) let stdout stream; capture only stderr to a temp file.
- **PGPASSWORD via env, not URL-encoding** — URL-encoded special characters cause auth failures.
- **psql `-f` must come BEFORE the connection URL** on Windows (`psql -f file.sql postgresql://...`). psql stops processing flags after the positional dbname.
- **`--set ON_ERROR_STOP=1` for psql restores** — otherwise psql exits 0 even when statements fail.
- **PG17 dump → PG16 local restore:** the restore script strips `SET transaction_timeout = 0;` (`_PG17_ONLY` filter). Do not remove this filter.
- **Long-running scripts (>~30 min): never in an interactive terminal.** Detach (`Start-Process` / `nohup`) with logs to file, and always implement a checkpoint file so runs resume (reference: `scripts/backfill_pq_questions.py`).
- **Long-running scripts on Railway Postgres: batch-by-ID, not load-all-then-iterate** — `expire_on_commit` + Railway's idle-connection kills crash the latter. Add `OperationalError` handling with `db.engine.dispose()` + retry (reference: `scripts/backfill_pq_answers.py`).

### SQLite vs Postgres — check before writing any DISTINCT / GROUP BY / window query
1. `.distinct()` with ORDER BY → every ORDER BY column must be in SELECT
2. `.group_by()` → no non-aggregated SELECT columns outside GROUP BY
3. Raw SQL booleans → `IS TRUE` / `IS FALSE`, never `= 1` / `= 0`
4. `DISTINCT ON` is Postgres-only — document it if used
5. Naive datetimes (`TIMESTAMP`) throughout — don't mix in `TIMESTAMPTZ`

After a Railway failover, sequences can desync (`duplicate key ... "ha_session_pkey"`) — fix with `setval(...)`; see the gotchas doc. (That's production SQL: Mark runs it.)

### Flask and SQLAlchemy
- **Threads:** every `ThreadPoolExecutor.submit()` that touches the DB or Flask context MUST wrap the function in `copy_current_request_context`. When adding a pool, check every other pool in the file has it.
- **Reserved column names** — never name a model column `query`, `metadata`, `session`, `get`, `filter`, `update`, `delete`, `insert`, `select` (`id` only as PK). Use `search_query`, `result_data`, etc.
- **Reserved Jinja globals** — never pass `session`, `request`, `g`, `config`, `url_for`, `get_flashed_messages`, `current_user` as `render_template()` kwargs; it silently shadows the global and breaks `base.html` later. Rename (`hs_session`) or use a `@app.context_processor`. Enforced by `tests/test_routes.py`.
- **Initialise every `render_template()` variable** with a default before any `if` block that sets it (GET requests otherwise crash with `UnboundLocalError`).
- **Silent exceptions hide bugs** — no `except Exception: pass` / `return []`. Log or return an `_error` marker. Bare `pass` only for genuinely expected no-ops (e.g. "column already exists").
- No migration framework: schema changes are `db.create_all()` plus guarded ALTER TABLE blocks in `flask_app.py` and scripts in `migrations/`.

### External APIs
- **Written Questions API:** use `tabledWhenFrom` / `tabledWhenTo` (not `tabledStartDate`), `answered` (not `isAnswered`), and `answeringBodies` only with a date anchor. Paginate with `take`/`skip`. **UINs are not globally unique across sessions.** Full detail: `docs/wq-api-constraints.md`.
- **Three-strike rule:** if the same call returns the same error or empty result more than twice, stop. Report the exact error and suggest manual intervention.
- **Before any retry**, state what the error was and what is different this time. If nothing, abort and report.

| Error | Action |
|-------|--------|
| 401 / 403 / invalid key | Permanent. Stop; tell Mark to check the env var. Do not retry. |
| 429 | Rate limited. Stop; tell Mark to wait. Do not loop. |
| 500 / 503 / timeout / connection reset | Transient. Retry once after a pause (exponential backoff in production code); if it fails again, report. |
| Empty result (0 rows) | Report clearly; suggest broader term, wider dates, no dept filter. Don't silently retry. |
| `Working outside of application context` | Add `copy_current_request_context`. Don't retry the same code. |
| `AttributeError` on a model | Column name clashes with SQLAlchemy internals — rename it. |
| `UnboundLocalError` | Variable only set in a conditional — add a default first. |

- Before concluding a key is invalid, check it's actually set (locally, and on the Railway **service**), and check `/health`, which tests each API.

### Preserving documented decisions (Chesterton's fence)
- If a previous commit deliberately removed or avoided something with a stated reason, don't reintroduce it without engaging with that reason. Surface the conflict; don't silently override.
- Read the relevant constraint doc before changing constrained code. **Commit messages must accurately describe the diff.** Verify no regressions before declaring done.
- Constraint docs are beliefs, not truth — they can be stale or based on flawed diagnostics. When a constraint and an official spec disagree, trust the spec and test directly.

---

## SEO and URLs (locked — changing indexed URLs costs ranking)

- **Canonical domain:** `westminsterbrief.co.uk` — naked, HTTPS, no www. All canonical tags, `og:url`, internal links and sitemap entries use it. Non-canonical variants (`www.`, `http://`) must 301/308 to it — never 302. www→naked is a Flask `before_request` hook; HTTP→HTTPS is Railway.
- **No trailing slashes**; 301 any trailing-slash variant to the no-slash URL.
- **URL dates:** `27-april-2025` — lowercase, full month, no leading zero.
- **MP slugs:** `firstname-lastname-constituency` (e.g. `keir-starmer-holborn-and-st-pancras`). Stable-first: once assigned, never changes even if the seat changes.
- **Lord slugs:** `firstname-lastname-of-place` per official title; fallback `firstname-lastname-baron`.
- **Constituency normalisation:** lowercase, hyphens for spaces, keep "and", `st` lowercase, strip apostrophes and commas.
- **Theme / policy area / department slugs:** `hansard_archive/slugs.py:slugify_theme()` is the single source of truth (used by `views.py`, the sitemap and the Jinja `slugify` filter). Lowercase → strip chars not in `[a-z0-9\s-]` → whitespace to hyphen → collapse hyphens. So apostrophes are stripped (`childrens-services`), `&` is stripped (`health-social-care`), accents are stripped not transliterated, existing hyphens kept.
- Topic briefs live at `/brief/<slug>` (canonical); `/archive/theme/<slug>` 301s there. They include debates and written questions; a topic with no items returns 404 + `noindex`, and thin topics (<3 items) render with `noindex`.
- The site is indexed (`index, follow`; `static/robots.txt` blocks admin/auth routes, `/archive/search` and SEO-tool crawlers). Before changing robots/noindex policy or enabling auth/accounts publicly, work through `docs/pre-launch-checklist.md` and `docs/launch-readiness.md`.

---

## Module notes

### Stakeholder Directory — committee evidence
- Read `docs/stakeholder-directory-design.md` before touching `stakeholder_directory/`.
- **High-water mark must be per `(committee_id, publication_type)`**, not per committee (`get_incremental_start_dates()` in `stakeholder_directory/ingesters/committee_evidence.py`). If either evidence type is missing for a committee, its start date falls back to `fallback_start` (2024-01-01); only use the MIN high-water mark minus buffer when BOTH types are present.
- If refactoring the ingest loop, interleave per committee (`for committee: for pub_type`) rather than all oral then all written — a container restart mid-run otherwise loses all written evidence.

### Parliamentary Research Tool
Full notes, canonical test case and checklist: `docs/research-tool.md`.
- **Search finds debates → fetch all speeches from each debate → ministers are always present.** Never rely on ministers' responses containing the search keywords.
- Ministerial debates for the selected department come first. Show debates as a unit, not loose speeches.
- **Canonical test:** "student loan repayments", Department for Education — Josh MacAlister and Baroness Smith of Malvern must appear. Verify ministerial roles via GOV.UK/Parliament API before calling anyone a backbencher.
- Lords substitutes: any government peer may cover for the departmental spokesperson and won't be in GOV.UK listings — topic search → full session fetch beats name search.
- Before declaring a fix: clear cache at `/admin`, run the canonical test live, check adjacent sections.

---

## When to stop, plan, or escalate

**Go to plan mode (no code) if ANY of these are true:**
1. The change touches more than one function that share data (search → classify → group → render is one pipeline)
2. The change touches both a backend function and a template
3. The fix changes how rows are structured or what fields they carry
4. Mark mentions a new feature idea mid-session (capture it in `docs/ideas-backlog.md`, don't build it)
5. The same bug has been attempted twice without a confirmed fix
6. The change affects minister search, session expansion, or grouping logic — the most interconnected code
7. The work introduces or modifies AI generation in a paid product code path — paid output rules must be explicitly verified before code is written

**Doom loops.** Never repeat the same tool calls with identical arguments without a
confirmed state change. If the same bug has been attempted more than twice: run
`git diff`, analyse why each attempt failed, find the root cause from the
pattern, then write a new fix. **At 3+ attempts without fixing the underlying
cause, tell Mark: "This has been attempted N times without a stable fix. I
recommend getting an Opus review before continuing."** Don't attempt again without Opus input.

**Escalate to Mark (and suggest Opus) before implementing when:**
- **Architecture:** cross-cutting change, a new pattern others will copy, multi-table schema migration, or the answer depends on product strategy
- **Surprises:** an API not behaving as documented, a regression, a constraint doc conflicting with observed behaviour, unexpected side effects
- **Product/scope:** an ambiguous request, user-facing design that depends on audience, work out of proportion to value
- **Public profile / operations:** scheduled jobs, public-facing accounts, branded social media, paid integrations
- **Cost:** meaningfully more API spend (Opus calls especially) or new paid services
- **Output rules:** a free feature asked to author inference, or a paid path losing a safeguard
- **Risk:** anything destructive, credential/auth failures with unclear cause, any uncertain production anomaly, or a change that's been reverted/re-applied more than once

Escalation = stop, report specifics, recommend Opus if useful, wait for direction. Don't proceed on a guess.

**Capture ideas.** When a new idea comes up that isn't being actioned, add it to
`docs/ideas-backlog.md` (name, one line, revisit trigger) without being asked.

---

## Working with Mark

- Knows Parliament well — which ministers spoke, which debates happened, which questions were tabled. If he says the tool misses something he knows happened, trust his domain knowledge and investigate the tool.
- Solo developer; sustainable pace is ~24 hours/week. Time is constrained.
- Types quickly with typos, thinks out loud in fragments — interpret intent from context.
- "This is fundamental" / "this is the gem" are priority signals. When he says something "should" work a certain way, there's usually a concrete reason grounded in how Parliament operates.
- **Challenge the data retrieval approach** — he values "is this the best way to retrieve this information?" before building. Questions about data structure and search logic are "goldust".
- **Validate architecture out loud** — if an approach is backwards (e.g. speech-first vs debate-first), say so clearly and explain why.

---

## Known issues and recent facts

- **SQLAlchemy pinned to 2.0.49.** 2.1 makes `postgresql://` URLs default to psycopg v3 → boot crash. Don't unpin without changing the driver/URL deliberately.
- **Revert `fb21bcd` casualties.** It silently removed working code: `/brief/<slug>` (restored) and `stats_theme.html` (recreated). If a `/brief` or `/stats` page 404s/500s, check whether it was another casualty.
- **PQ UIN cross-session collision (INC-008).** WQ UINs reset each parliamentary session, so a UIN alone never identifies a question. Identity is `api_id`, then (`tabled_date`, `uin`); `ha_pq.uin` is not unique. Question pages live at `/archive/pq/<yyyy-mm-dd>/<uin>` (lowercase UIN) — build links with `HaPQ.url_path` / `pq_url_path()`, never by hand. The old `/archive/pq/<uin>` 301s to the earliest-stored question with that UIN (what it always showed); each page lists other questions sharing its number. Keep the old address forever; once live, fix forward rather than revert. PQ sitemap: one chunk per tabled month (`/sitemap-pqs-YYYY-MM.xml`). Production run order: `docs/plans/pq-identity-phase3-sql.md`; identity-key plan: `docs/plans/pq-identity-key-plan.md` (Mark's working folder, uncommitted). Related: recent PQ rows lack `heading` (ingested before Parliament assigns it) — see `docs/ideas-backlog.md`.
- **No social share image.** `static/og_default.png` was a saved HTML page and was removed (2026-10-20); there is no default `og:image`. Needs a real 1200×630 PNG.
- **Pre-push import check can connect to production.** `python -c "from flask_app import app"` opens a live Postgres connection when `.env` holds a production `DATABASE_URL`, which conflicts with INC-005. Candidate fix (untested): `DATABASE_URL=sqlite:///local_check.db python -c "from flask_app import app; print('OK')"`. Test before changing the checklist.
- **Committee written evidence likely incomplete** (first ingestion, Apr 2026) — a full re-ingest from `/admin` may still be needed; check current counts first.
- **DSIT GOV.UK discovery query 422s** (Issue A, `docs/ideas-backlog.md`) — expect `errors=1` on re-discovery runs until fixed.
- **`SECRET_KEY` must be set on Railway.** There is no fixed fallback (this repo is public, so one would let anyone forge an admin session); without it each process makes a random key and logins don't survive restarts or cross gunicorn workers. Never add `os.environ.get('SOME_SECRET', 'default')` fallbacks for secrets.
- **Links we build use `SITE_URL`** (default `https://westminsterbrief.co.uk`), never the request host: `ProxyFix` trusts `X-Forwarded-Host`, which a visitor can forge.
- **`/admin` and the direct Railway address.** Cloudflare's IP restriction doesn't cover `*.up.railway.app`. Optional `ADMIN_EDGE_SECRET`: when set, `/admin*` 404s unless the `X-Admin-Edge` header (added by a Cloudflare Transform Rule) matches.
- Root clutter (`railway.backup.toml`, `nixpacks.backup.toml`, one-off scripts) — safe to tidy eventually.

---

## Pre-share checklist

Before sending the site to any new user group, walk each end-to-end with realistic data:

- [ ] Landing page renders cleanly
- [ ] Hansard Archive index + **at least one session detail page** (click through from search results)
- [ ] A PQ detail page — asking MP name and answering minister name both appear
- [ ] An MP archive page (e.g. `/archive/mp/<id>`)
- [ ] Search results with results, and an empty-results state
- [ ] Stakeholder Directory index + at least one organisation detail view
- [ ] Member Research + Member Profiles for one MP
- [ ] Parliamentary Research Tool (both tabs, a real search)
- [ ] No 500s, missing fields or broken layouts on any of the above
- [ ] Google Search Console checked for new error patterns since last push

## Recovery preparedness

Railway stores its snapshots in the same volume as the data, so a volume deletion
erases them too. External discipline:
- Daily encrypted `pg_dump` to Cloudflare R2 (`backup-cron-r2`). Restore tested end-to-end 13 May 2026; repeat every 6 months and after schema migrations.
- Recoverable outside Railway: DNS (Cloudflare), Stripe history, Postmark logs (45 days), code (GitHub). At risk without the R2 backup: user accounts, preferences, saved searches/alerts, anything app-created.
- Procedure: `docs/recovery-runbook.md`.

## Accuracy tester

A separate local project (`wb_tester`, sibling folder — no shared code or DB)
checks what the live site returns against direct TWFY/Parliament API ground
truth and curated fixtures. It has its own `CLAUDE.md`.

---

## Things to avoid (quick list)

- Don't push without Mark saying "push" — and never to master without a `docs/deploys.md` row
- Don't `git add -A` / `git add .`
- Don't hardcode API keys, credentials, hostnames or `.env` paths — and never commit them to this public repo
- Don't use the Flask dev server in production (`debug=True` only via `__main__` locally)
- Don't add a `ThreadPoolExecutor` without `copy_current_request_context` on every `submit()`
- Don't name SQLAlchemy columns `query`, `metadata` or `session`
- Don't leave variables uninitialised before `render_template()`
- Don't invoke Claude (Anthropic API) from any free-feature code path
- Don't remove the AI-disclosure footer from paid product outputs at the standard tier
- Don't run any destructive Railway API call, CLI command, or destructive operation against any cloud provider
- Don't use API tokens or credentials found in files for tasks unrelated to their documented purpose
- Don't autonomously "fix" credential mismatches, auth failures, or unexpected production state — stop and surface to Mark
- Don't run raw SQL against production, or local scripts against the production database
- Don't run `git push --force`, `git reset --hard`, or other destructive git operations without explicit instruction
