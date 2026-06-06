# Westminster Brief — Project Instructions

---

## Project orientation

Westminster Brief is a UK parliamentary research tool — free for civil servants and policy professionals, paid subscription for everyone else. Live at `westminsterbrief.co.uk` on Railway. Built by Mark Forde, a UK civil servant (higher education policy), as a side project at ~24 hours/week.

**Current phase:** Phase 2A (free public Hansard archive live). Next: Phase 2A.5 (PQ answer caching, smoke tests, minor UX). Phase 2 (paid stakeholder briefing pack) follows after.

**Three-role working model:**
- **Mark** — developer and product owner. Makes all decisions.
- **Claude Code** — implementer. Writes code, runs builds, manages files. Proceeds directly on clear tasks.
- **Opus (chat session)** — strategic advisor. Called in for architecture decisions, product strategy, stuck debugging, anything needing fresh judgment before Code acts.

**Briefs for Code from Opus are written in fenced code blocks** so Mark gets a copy-button UI when pasting from the Opus chat response into a Code session.

---

## Working pattern

- **Branch:** `master` (not `main`) — always work here unless explicitly on a feature branch
- **GitHub:** `https://github.com/markyf801/Westminsterbrief` — Railway auto-deploys on push to master
- **Never push automatically** — commit locally, show what changed, wait for Mark to say "push" or "push it"
- **Local environment:** Windows 11, PowerShell, Python 3.13. Postgres client tools at `C:\Program Files\PostgreSQL\18\bin\` (on PATH). PG16 server at `C:\Program Files\PostgreSQL\16\bin\` (not on PATH) for local restore testing.
- **Production environment:** Railway, Linux/Docker, Python 3.12
- **Start local dev server:** `python flask_app.py` — runs at `http://127.0.0.1:5000`
- **Plan mode for anything touching >3 files or >1 system** — ask before building, not after

---

## How future Claude/Code sessions should start

At the beginning of any new working session:

1. Read this CLAUDE.md — at minimum the "Project orientation", "Active priorities", and any section relevant to the current task
2. Check "Active priorities" to understand what's in progress and where the plans live
3. For any module you're about to change, read its design doc (see "Project files reference")
4. Run `git log --oneline -10` to orient to recent commits
5. Check `docs/deploys.md` — recent production pushes, last known-good SHA, any outstanding half-states
6. **Do not assume implicit knowledge from past sessions** — verify by reading canonical docs on disk

The spec captures locked decisions; it does not always capture what's been built since the last edit. Before drafting a plan or scoping work, ask: "What's actually been built vs what the doc lists as pending?"

---

## Active priorities — Phase 2A.5

Current work queue. Each item has a plan or location:

| Priority | What | Plan / location |
|---|---|---|
| High | PQ answer text caching | `docs/phase-2a5-pq-answer-text-plan.md` (6-stage build plan) |
| Medium | Route smoke tests + reserved-kwarg lint guard | Described in "Flask template safety" section below |
| Medium | Backup monitoring (Railway alert + `/health` backup-freshness) | Captured — no doc yet, ~2hr build |
| Later | `pg_dump -Fc` / `pg_restore -j4` migration for faster restores | Defer — current plain-SQL restore works after 13 May fixes |

Phase 2 (paid product: stakeholder briefing pack) is on hold until Phase 2A.5 items are cleared. Plan: `westminster-brief-phase-2-brief.md` (project root).

---

## Project files reference

Key documents a new session should know about:

| File | What it is |
|---|---|
| `docs/launch-readiness.md` | Pre-launch hard blockers and completion status — check before scoping work |
| `docs/recovery-runbook.md` | Disaster recovery procedure — updated after 13 May 2026 restore drill |
| `docs/design-principles.md` | Visual + copy guide — read before any UI redesign |
| `docs/api-reference.md` | Confirmed working parameters for Parliament/GOV.UK APIs |
| `docs/parliamentary-debate-types.md` | Hansard debate type taxonomy — read when touching classification logic |
| `docs/stakeholder-directory-design.md` | Stakeholder Directory spec — read before touching that module |
| `docs/pre-launch-checklist.md` | Legal/compliance + SEO checklist — do not remove noindex without working through this |
| `docs/ideas-backlog.md` | Active ideas and killed ideas — capture new ideas here during sessions |
| `docs/deploys.md` | Append-only ledger of production pushes and SQL — source of truth for recent production state |
| `westminster-brief-phase-2-brief.md` | Phase 2 (paid product) master scoping doc |

---

## Conventions and gotchas

**Windows binary paths — always use platform constants:**
Scripts calling external binaries (`gpg`, `psql`, `pg_dump`) via `subprocess.run()` must use platform-resolved constants, not bare names — these tools are not reliably on PATH on Windows. See `scripts/restore_from_backup.py` for the `_GPG` / `_PSQL` pattern.

**PGPASSWORD — pass via env, not URL-encoding:**
When calling psql in a subprocess, extract the password and pass it as `PGPASSWORD` in the env dict. URL-encoded passwords with special characters cause auth failures.

**psql `-f` flag must come BEFORE the connection URL:**
`psql -f file.sql postgresql://...` works. `psql postgresql://... -f file.sql` does NOT on Windows — psql stops processing flags after the positional dbname argument. Root cause of two days of silent failures, 12–13 May 2026.

**`stdout=PIPE` + large subprocess output = deadlock:**
For subprocesses producing large output (e.g. psql restoring a 460 MB dump), never use `stdout=subprocess.PIPE`. The pipe buffer fills and the process hangs indefinitely. Let stdout stream to terminal; capture only stderr to a temp file if needed.

**Railway Query tab rejects LIMIT on some tables (discovered 2026-05-26):**
Queries containing `LIMIT` against `ha_stat_producer` or `ha_stat_publication` error with
`syntax error at or near "LIMIT"` in Railway's Query tab. Confirmed on Chrome and Firefox,
freshly typed input (not paste corruption). COUNT queries on the same tables work fine.
Root cause unknown. Do not rely on the Railway Query tab for any SQL involving LIMIT.

**Production SQL diagnostics — use DBeaver (confirmed working 2026-05-27):**
DBeaver connects to Railway Postgres via `DATABASE_PUBLIC_URL` with SSL mode = require.
All query types work correctly including LIMIT, DISTINCT, EXPLAIN ANALYZE. This is the
recommended path for any production SQL that the Railway Query tab cannot handle.
Connection details: host `hopper.proxy.rlwy.net`, port `50798`, database `railway`,
SSL required. Credentials from `.env` (`DATABASE_URL` contains all components).

**DBeaver multi-statement execution: verify against DB state, not against intent:**
DBeaver's default execution mode runs the statement under the cursor, not the entire script. A multi-statement block can appear "run" when only one statement actually executed.

After any multi-statement DBeaver operation:
1. SELECT against the expected end-state — not against the queries that were "supposed to run"
2. Confirm row counts, status values, timestamps match the intended change
3. If verification disagrees with intent: the script didn't run as expected; investigate before declaring done

Failure mode: assuming a block ran because no errors appeared. Errors only show for the cursor-position statement. Other statements may have been silently skipped. Refined from several near-misses 28 May 2026.

**Raw SQL must explicitly set `updated_at` on ORM-managed tables:**
SQLAlchemy's `onupdate=datetime.utcnow` hook fires only when the ORM mutates a row. Raw SQL UPDATE statements (via DBeaver, psql, or `connection.execute`) bypass the ORM and will NOT trigger the hook.

```sql
-- WRONG — updated_at stays at previous value
UPDATE ha_stat_producer SET authorisation_status = 'declined' WHERE id = 25;

-- RIGHT — explicitly set updated_at
UPDATE ha_stat_producer
SET authorisation_status = 'declined', updated_at = NOW()
WHERE id = 25;
```

The audit trail relies on `updated_at`; a silent stale value undermines downstream reasoning about when changes happened. Pattern first applied: HESA de-registration 27 May 2026.

**Postgres sequence desync after Railway failover:**
After a Railway Postgres failover or WAL recovery, auto-increment sequences can reset to a low value while data remains intact. Symptom: `duplicate key value violates unique constraint "ha_session_pkey"`. Fix in Railway's Postgres Query console:
```sql
SELECT setval(pg_get_serial_sequence('table_name', 'id'), (SELECT MAX(id) FROM table_name));
```
Run for each affected table (`ha_session`, `ha_contribution`, `ha_pq`, `ha_session_theme`).

**`ON_ERROR_STOP=1` for psql restores:**
Always pass `--set ON_ERROR_STOP=1` when restoring via psql. Without it, psql returns exit code 0 even when individual SQL statements fail.

**PG17 backup → PG16 local restore:**
Railway runs PG17. `SET transaction_timeout = 0;` in the dump is a PG17-only parameter. The restore script strips this line during decompression (`_PG17_ONLY` filter). Do not remove this filter.

**Long-running backfill scripts — never run in an interactive terminal:**
Stage C (May 2026) was interrupted twice because the terminal session was killed overnight. Any script expected to run for more than ~30 minutes must be started with `nohup` or inside `tmux`/`screen` so it survives terminal disconnection. Pattern:
```powershell
# Windows: use Start-Process to detach (logs to file)
Start-Process python -ArgumentList "scripts/my_script.py" `
    -RedirectStandardOutput "scripts/my_script.log" `
    -RedirectStandardError "scripts/my_script_err.log" `
    -WindowStyle Hidden
```
```bash
# Linux/Railway shell: use nohup
nohup python scripts/my_script.py >> scripts/my_script.log 2>&1 &
```
Always implement a checkpoint file so interrupted scripts resume cleanly rather than starting over. See `scripts/backfill_pq_questions.py` for the reference pattern.

**Long-running scripts on Railway Postgres — batch-by-ID, not load-all-then-iterate:**
Railway kills idle Postgres connections after a few hours. Scripts that load a large result set upfront and then iterate slowly will hit this. The failure mode is subtle: SQLAlchemy's default `expire_on_commit=True` expires ALL objects in the session on every commit — not just the committed ones. On the next attribute access of any expired object, SQLAlchemy triggers an autoflush (to flush pending dirty objects), which needs the now-dead connection, and crashes.

**Do not do this:**
```python
rows = db.session.query(Model).filter(...).all()   # loads 68k objects
for row in rows:
    row.field = ...
    if i % 50 == 0:
        db.session.commit()  # expires ALL 68k objects; next attribute access lazy-reloads
```

**Do this instead — batch-by-ID:**
```python
last_id = 0
while True:
    batch = (db.session.query(Model)
             .filter(..., Model.id > last_id)
             .order_by(Model.id).limit(100).all())
    if not batch:
        break
    for row in batch:
        last_id = row.id
        row.field = ...       # row is freshly loaded, not expired
    db.session.commit()       # expires this batch's objects — we never touch them again
                              # next query naturally gets a fresh connection
```

Also add `OperationalError` handling around the batch query and commit to call `db.engine.dispose()` and retry on connection drop. Reference implementation: `scripts/backfill_pq_answers.py` (commit `415382c`, 14 May 2026).

---

## Source attribution principle

Every piece of content on Westminster Brief links to its authoritative source. This is the substantive trust mechanism — not just a compliance requirement.

Westminster Brief is a navigational and analytical layer over Parliament/GOV.UK sources. Every fact must be traceable to where it lives canonically. Source links should be visible and clear, not buried in attribution boilerplate.

**Current state (14 May 2026):**

| Content type | Source URL pattern | Link status |
|---|---|---|
| Hansard debates/sessions | `session.hansard_url` (stored in DB) | ✓ "View on Hansard ↗" in session header |
| Written Questions | `https://questions-statements.parliament.uk/written-questions/detail/{tabled_date}/{uin}` | Present but in small-print — promote to header |
| MP archive pages | `https://members.parliament.uk/member/{mnis_id}/` | ✗ Not linked — MNIS ID not in `cached_member` (uses TWFY ID) |
| | `https://www.theyworkforyou.com/mp/?p={member_id}` | ✗ TWFY ID available — simpler interim option |
| Theme/policy pages | Meta-pages — no single canonical source | N/A |
| Bills | `https://bills.parliament.uk/bills/{bill_id}` | Not yet ingested |
| EDMs | `https://edm.parliament.uk/early-day-motion/{edm_number}` | Not yet ingested |

**Pending work (approved by Mark, 14 May 2026):** Promote PQ source link from small-print to session header (same treatment as Hansard "View on Hansard ↗"). MP pages: add TWFY link as interim; full parliament.uk link requires `mnis_id` added to `cached_member` schema.

---

## Stack
- **Backend:** Flask 3.0 with blueprints, deployed on Railway
- **Database:** SQLite locally → PostgreSQL on Railway (auto-switched via `DATABASE_URL` env var)
- **AI:** Google Gemini API (`google-genai`, model: `gemini-1.5-flash` and `gemini-embedding-001`) for free toolkit features. Anthropic Claude (Opus, via anthropic SDK) for paid product outputs only — see "Output rules" section.
- **Frontend:** Jinja2 templates + vanilla JS, static CSS at `static/style.css`
- **Auth:** Flask-Login with werkzeug password hashing
- **Exports:** python-docx for Word document generation

## Product audience and positioning

Built for UK policy professionals doing parliamentary research. Primary audiences:

- Civil servants writing briefings, submissions, and parliamentary returns
- Charity and trade body policy officers researching engagement
- Public affairs professionals tracking parliamentary activity
- Academic researchers studying policy and Parliament
- Journalists and engaged citizens following specific topics

Civil servants are the most demanding edge case for accuracy, evidence trails, and rigorous citation — meeting their needs raises quality for everyone.

**Pricing model:** free for gov.uk email addresses; paid subscription for everyone else (pricing TBD as product matures).

**Feature design rule:** prefer the version that works for all five audiences to one optimised only for civil servants. Where there's genuine tension, flag it for the user — don't silently optimise for one audience over another.

**Marketing language must be evidence-based.** Claims about adoption, trust, or external validation must be true and verifiable. "Built for" is fine; "trusted by" requires actual trust. Avoid "thousands of users", "industry-leading", "loved by professionals" and similar early-stage overclaim. The absence of overclaim is itself a positioning asset for a tool aimed at policy professionals — they have high BS-detection and respond well to honest framing.

## Audience framing — broader than civil servants

Westminster Brief serves a broader audience than civil servants. The project's primary users include:

- Civil servants writing briefings and managing parliamentary engagement
- Charity and trade body policy officers researching engagement
- Public affairs professionals tracking parliamentary activity
- Academic researchers studying policy and Parliament
- Journalists and engaged citizens following specific topics

When making design decisions, default to the broadest reasonable audience. Features and copy that assume civil-servant-specific context — internal departmental deadlines, departmental workflows, gov.uk-only access, civil-service jargon — are likely to be unhelpful or confusing for other users.

### Examples of audience-specific framing to avoid

- **"Internal deadline"** for parliamentary questions — civil servants know their department's internal deadlines; non-civil-servant users have no such concept and would find this confusing.
- **"Your department"** language — assumes the user belongs to a department.
- **Gov.uk-internal terminology** like "Parliamentary Branch," "Q&A team," "submission deadline" — meaningful to civil servants, opaque to others.
- **Implicit civil service workflow assumptions** in the UI — e.g. assuming users will be drafting answers, when most users are tracking what's happening rather than responding.

### What works for the broader audience

- **Parliamentary-side facts** — what was tabled, what's due to be answered, who has engaged with whom. These work for everyone because they're objective rather than workflow-dependent.
- **Neutral language** — "Question," "Answer due," "Department for Education" rather than internal acronyms.
- **The "built by a civil servant" credibility line** — works as evidence-based context without assuming the reader is also a civil servant.

### When civil-servant-specific features are appropriate

Some features genuinely belong only to civil servants — for example, briefing pack generation tailored to specific internal templates. Those features should be clearly scoped to civil servants and not bleed into the general experience.

### Working principle

When in doubt about a design or copy decision, ask: "would this make sense to a charity policy officer or a journalist?" If the answer is "no, this only makes sense for civil servants," redesign or relocate the feature.

This came up specifically when designing the WQ tracker's deadline display: the initial proposal showed an "internal departmental deadline" alongside the Parliamentary deadline. Civil servants already know their internal deadline; non-civil-servant users have no such concept. The right answer was to show only the Parliamentary deadline (which is meaningful for everyone) and leave the internal deadline implicit.

---

## Output rules — different for free vs paid features

Westminster Brief has two distinct categories of feature, governed by different output rules. The category determines what the tool is allowed to author, what model tier it uses, and what safeguards apply.

### Free toolkit (default rule): factual or extracted, never authored

The free toolkit (Written Questions Scanner, Today's PQs Tracker, MP Research, Member Profiles, Hansard Search, Stakeholder Directory, and any future free features) finds and surfaces evidence — speeches, citations, engagements, statements. It does not draft positions, lines to take, recommended responses, or anything that implies authored content for which a civil servant would normally hold accountability.

Where AI is used in free features (summary, classification, extraction), it operates on factual material the tool has actually retrieved, with citations — not on training-data knowledge. Outputs that look or feel like authored civil service work product (minutes, submissions, drafted lines) are explicitly out of scope for free features.

Specifically out of scope for free features: AI-drafted PQ responses, suggested ministerial statements, auto-generated press lines, draft holding lines, recommended Q&A briefs, suggested approaches for engagement, or AI inference about any individual parliamentarian's likely position, sympathy, or behaviour.

The reframed "Key ministerial statements" feature (verbatim extraction with citations, parser-rejection of unmatched quotes) is consistent with this principle and remains on the roadmap. Anything that requires the tool to author rather than extract does not.

**Free features run on Gemini (Flash-Lite) only. Claude is never invoked from a free-feature code path.**

### Paid products (separate rule): authored inference permitted with strict safeguards

Paid products (the stakeholder briefing pack and any future paid deliverables) are commercial AI services that customers pay for, brand themselves, and are responsible for verifying before use. They are governed by separate, stricter output rules:

**1. Source citation is mandatory on every claim.**

Every authored inference (warmth assessment, suggested approach, themes raised) must be tied to specific cited evidence — Hansard URL, division ID, written question UIN, committee record, etc. Floating assertions ("MP X is sympathetic") without specific cited evidence are not allowed and must be rejected by the generation pipeline.

**2. Confidence must be honest.**

Every authored inference must include an explicit confidence indicator (high, medium, low) plus a one-sentence reasoning. If the underlying data is thin, the output must say "Insufficient parliamentary record to assess" — not produce a low-confidence guess. A briefing that downgrades 60% of stakeholders to "insufficient record" is the correct output for a thin-data issue, not a failure mode to engineer around.

**3. AI disclosure is non-removable.**

All paid outputs carry a per-page footer: "AI-generated analysis. Verify before publishing." This footer cannot be removed at the standard tier under any circumstances. Future agency tier (when built) will have separate rules including stronger ToS commitments around verification.

**4. Language must be observational, not predictive.**

Allowed: "MP X has voted in favour of similar measures on [dates]."
Not allowed: "MP X is likely to support this bill."
Allowed: "MP X has spoken publicly about constituent concerns on [issue]."
Not allowed: "MP X will respond to constituent-led framing."
The output describes what the parliamentary record shows. It does not predict future behaviour.

**5. Suggested approaches must be neutral framing advice, not advocacy guidance.**

Allowed: "MP X's contributions on this issue have emphasised academic evidence; framing around peer-reviewed research may resonate."
Not allowed: "Lobby this MP via the academic angle."
Allowed: "MP X is a member of [APPG]; engaging via [APPG] activity is consistent with their stated interests."
Not allowed: "Use [APPG] to influence MP X's position."
The tool describes patterns; it does not direct the user to take advocacy or lobbying actions.

**6. No civil service voice.**

Paid outputs must read as a research deliverable, not as a civil service submission, briefing, or line-to-take. No phrases like "the recommended approach is" or "it is suggested that" or "officials may wish to." The user is making advocacy decisions; the tool is providing structured intelligence.

**Paid features run on Claude Opus only** (the best Anthropic model available; upgrade conversations triggered when newer models release). Claude Sonnet may be used for development testing only, never in production.

### Hard cost ringfence

Claude (Opus, or any future top-tier Anthropic model) is invoked ONLY for paid product runs. It is never invoked for any free-tier feature, regardless of user tier — including .gov.uk users.

The .gov.uk free tier covers the toolkit only. Public-sector users pay the same as anyone else for stakeholder briefings, topic tracking, and deep research briefs. The tier column on users records eligibility (public_sector / standard), not entitlement.

Tier-to-model binding must be enforced at the LLM abstraction layer with a code-level guard, not by convention.

### Working principle

For any AI generation in the codebase, ask: is this a free feature or a paid product feature?

- Free → factual extraction only, Gemini only. Apply the toolkit rule.
- Paid → authored inference permitted, but only with all six safeguards above; Opus only.

If a feature blurs the line (e.g. a "preview" of a paid product visible to free users), default to the stricter free-toolkit rule until the boundary is explicit.

### When in doubt, escalate

If a proposed feature feels like it might cross from one category to the other — for example, a free tool that starts producing "summaries with implications" — stop and surface to Mark. Don't expand authored inference into free features without explicit decision.

## Design principles

`docs/design-principles.md` is the authoritative visual and copy guide for any redesign work. Read it before implementing any UI or landing page changes. Key summary: restrained, content-first, type-led; reference gov.uk, FT, Stripe docs; avoid gradients, glassmorphism, oversized hero text, generic SaaS copy.

## Documents in /docs/ are live

Documents in `docs/` are the canonical source of truth. They may be updated between Code's interactions with them. When the user references a doc, re-read it from disk rather than relying on what was read earlier in the session.

## Module-level design docs

When working on a specific module, read its design doc first:

- `stakeholder_directory/`: `docs/stakeholder-directory-design.md`
- External APIs: `docs/api-reference.md` — confirmed working parameters, response schemas, gotchas, code examples for all Parliament/GOV.UK APIs
- (others as added)

Module design docs are authoritative for that module — they override general guidance in this file where they conflict. If a module-level doc and CLAUDE.md disagree, raise it rather than guessing.

## Project structure
```
flask_app.py          Main app: config, DB models, auth routes, alerts scanner, blueprint registration
hansard.py            Blueprint: Written Questions search & export (route: /questions)
tracker.py            Blueprint: Today's PQs + AI categorisation (route: /tracker)
mp_search.py          Blueprint: MP/Peer PQ research (route: /mp_search)
biography.py          Blueprint: MP/Lords biography with AI summary (route: /biography)
debate_scanner.py     Blueprint: Debate search, transcript scraping, AI briefing (route: /debates)
templates/            Jinja2 HTML templates — base.html is the master layout
static/style.css      All CSS — no inline styles in base.html or index.html
```

## Tool name → URL → file mapping
Clear mapping to avoid confusion when discussing issues:

| Tool name (navbar/home)         | URL            | Backend file        | Template                      |
|---------------------------------|----------------|---------------------|-------------------------------|
| Written Questions Scanner       | `/questions`   | `hansard.py`        | `templates/index.html`        |
| Today's PQs Tracker             | `/tracker`     | `tracker.py`        | `templates/tracker.html`      |
| MP PQ Research                  | `/mp_search`   | `mp_search.py`      | `templates/mp_search.html`    |
| Member Profiles                 | `/biography`   | `biography.py`      | `templates/biography.html`    |
| Parliamentary Research Tool     | `/debates`     | `debate_scanner.py` | `templates/debate_scanner.html` |

**Note:** "Hansard" as a concept appears in both the Written Questions Scanner (Parliament WQ API) and the Parliamentary Research Tool (TWFY Hansard debate transcripts). When discussing issues, use the tool name above, not "Hansard tool".

## External APIs used
| API | Env var | Used for |
|-----|---------|----------|
| Google Gemini | `GEMINI_API_KEY` | AI summaries, embeddings, categorisation (free toolkit only) |
| Anthropic Claude | `ANTHROPIC_API_KEY` | Paid product outputs only (stakeholder briefing pack, future paid products) — never invoked from free features |
| They Work For You | `TWFY_API_KEY` | Debate transcripts, Hansard search |
| News API | `NEWS_API_KEY` | Media scan in Smart Alerts |
| Bluesky | `BSKY_HANDLE` + `BSKY_PASSWORD` | Stakeholder social monitoring |
| Parliament API | none (public) | Written Questions, MP/member data |

## Database models (flask_app.py)
- `User` — email + hashed password
- `TrackedTopic` — keyword + department, belongs to User
- `TrackedStakeholder` — name + Bluesky handle, belongs to User
- `Alert` — result from AI scan, linked to Topic or Stakeholder

## Deployment (Railway)
- Entry point: `gunicorn flask_app:app` (see `Procfile` and `railway.toml`)
- **Railway project:** `invigorating-joy` — service name: `Westminsterbrief`
- **Production URL:** `westminsterbrief-production.up.railway.app`
- **Custom domain:** `westminsterbrief.co.uk` (domain registered at GoDaddy, nameservers point to Cloudflare — all DNS managed in Cloudflare)
  - `www` CNAME → `5jac57s9.up.railway.app`
  - `_railway-verify` TXT record added for domain verification
  - Root `@` A record managed in Cloudflare
- HTTPS is handled automatically by Railway (Let's Encrypt) once DNS verifies
- Add a **PostgreSQL plugin** in Railway — it sets `DATABASE_URL` automatically
- Set all env vars in Railway dashboard (see API table above)
- Also set `SECRET_KEY` to a long random string in Railway env vars
- GitHub repo: `markyf801/Westminsterbrief` — Railway auto-deploys on push to `master`

### Railway one-shot services — capture logs before teardown

Diagnostic logging in one-shot scripts (e.g. `[SUB_PAGE]` counts, extraction summaries) is lost when the service is deleted. Before tearing down any one-shot Railway service, export or screenshot the full log output — the diagnostic data it contains informs the next piece of work.

**Detailed sequence — do not skip steps 3 and 4:**

1. Deploy and run the service
2. Wait for "Exited" / "Stopped" / "Completed" state in the Railway dashboard
3. Export the FULL log to a local file via Railway's log download
4. Open the file locally and verify it contains the expected content (search for summary blocks, CSV markers, specific log line patterns — don't trust that export succeeded, confirm by inspection)
5. ONLY after local file confirmed: delete the service

This applies to ALL one-shot services regardless of whether logs "look" important at the time. The piece 2b diagnostic re-scan (28 May 2026) was needed precisely because the earlier piece 2 backfill's `[SUB_PAGE]` diagnostic logs were lost on teardown.

If a diagnostic script writes a summary block + CSV section + per-row log lines, the local capture must contain all three before teardown.

### Railway Postgres connections

- **Local connections** (migration scripts, pg_dump, psql) need the **public URL** from Postgres service → Database → Config. **The actual hostname for this project is `hopper.proxy.rlwy.net:50798`** — not the generic `<region>.proxy.rlwy.net` pattern shown in Railway docs. Full URL shape: `postgresql://postgres:<password>@hopper.proxy.rlwy.net:50798/railway`. The internal hostname (`postgres.railway.internal`) only resolves inside Railway's network.
- **Credentials are in `.env`** at the project root (`c:\Users\marky\hansard_app\.env`). The full `DATABASE_URL` is stored there — use it directly for psql, pg_dump, and diagnostic scripts rather than copying from the Railway dashboard. Example: `psql "$env:DATABASE_URL"` in PowerShell after loading the file, or just copy the value directly from `.env`.
- **Production Flask service** uses `DATABASE_URL=${{Postgres.DATABASE_URL}}` as a Variable Reference — not a hardcoded string. This resolves at deploy time.

### Password rotation

Variable References resolve at deploy time, not runtime. The running container on each consumer service has the old password baked in from when it last started — it will fail to authenticate until redeployed.

Sequence after regenerating the Postgres password:
1. Regenerate password in Postgres → Database → Config
2. Manually trigger a redeploy on **every** consumer service:
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
3. Verify Flask picked up the new credential: hit `/health` and confirm `status=ok`

Variable References are not "live" — they resolve at deploy time only.

### Ingestion schedules — all Railway cron services

**Hansard debates, speeches, Written Ministerial Statements** (`scripts/archive_cron.py`):

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `archive-cron-morning` | `0 8 * * 1-5` | 09:00 Mon–Fri | 08:00 Mon–Fri | Morning catch-up, 3-day window |
| `archive-cron-daytime-mth` | `0 11-23 * * 1-4` | 12:00–midnight Mon–Thu | 11:00–23:00 Mon–Thu | Hourly during sittings |
| `archive-cron-daytime-fri` | `0 9-19 * * 5` | 10:00–20:00 Fri | 09:00–19:00 Fri | Hourly during Friday sittings |

WMS are classified within the same Hansard ingestion — no separate cron. Theme tagging runs inline at the end of every Hansard cron run (not a separate service).

**Written Questions** (`scripts/ingest_pq_cron.py`, 7-day rolling window):

| Service | Cron (UTC) | BST window | GMT window | Purpose |
|---|---|---|---|---|
| `pq-cron-early` | `30 8 * * 1-5` | 09:30 Mon–Fri | 08:30 Mon–Fri | First capture after Parliament's 08:00–09:30 publication window |
| `pq-cron-morning` | `30 9 * * 1-5` | 10:30 Mon–Fri | 09:30 Mon–Fri | Safety net for late-published questions |
| `pq-cron-afternoon` | `0 14 * * 1-5` | 15:00 Mon–Fri | 14:00 Mon–Fri | Captures answers published mid-afternoon |
| `pq-cron-monday` | `0 9 * * 1` | 10:00 Monday | 09:00 Monday | Post-weekend backlog catch-up |

**Timing rationale:** The `/questions` tool queries `ha_pq` (local DB), not Parliament's API directly (refactored at commit `57e7273`). The early + morning cron pair restores morning readiness: civil servants checking new PQs at 09:30–10:00 UK will see that day's questions already in the DB. In BST, `pq-cron-early` fires right at Parliament's publication window close. In GMT, `pq-cron-morning` becomes the effective first capture at 09:30 UK — marginally later but within the start-of-day window. Duplicate upserts across the two morning runs are silent (PQ table uses upsert-by-UIN).

**Stakeholder directory**: No automated cron. Ministerial meetings loaded via CSV upload + `stakeholder_directory/pipeline.py` when GOV.UK publishes quarterly transparency data. Committee evidence triggered manually via `/admin` panel. Lobbying register also manual.

**Member cache refresh** (`scripts/refresh_member_cache.py`):

| Service | Cron (UTC) | Purpose |
|---|---|---|
| `member-cache-refresh` | `0 2 * * 0` | Sunday 02:00 UTC — bulk-refresh all current MPs + Lords into `cached_member` |

Run once manually after initial deployment: `python scripts/refresh_member_cache.py`. After that, searches resolve member names/party/constituency entirely from the DB; no per-request Parliament API calls.

**Stats catalogue re-discovery** (`scripts/rediscovery_cron.py`):

| Service | Cron (UTC) | Purpose |
|---|---|---|
| `rediscovery-cron` | `0 6 * * *` | 06:00 UTC daily — re-scan authorised+completed producers for NEW publications (pre-classify URL/dataset-ID dedup, so only genuinely-new pubs hit Gemini) + refresh ONS latest-release dates. Tracked by `last_rediscovered_at` (daily cadence). |

Keeps the stats catalogue current so it doesn't freeze at first-discovery. `--execute` writes; default is dry-run. Headline metric is `SKIPPED_KNOWN` (a healthy run is mostly-skips). A fetch failure now raises `StrategyFetchError` → counted in `errors`, producer NOT stamped (re-attempted next run) — so a broken producer query fails loud rather than silently re-stamping (Issue B, fixed 5 Jun 2026). Respects Decision H — discovery only, no data-file extraction. Known open: DSIT's GOV.UK query 422s (Issue A, `docs/ideas-backlog.md`) — expect `errors=1` every run until fixed.

**Other**:
- `backup-cron-r2` — daily pg_dump to Cloudflare R2

### Bulk migrations and volume size

Single-transaction bulk inserts use working space (WAL + indexes) of roughly 2–3× the final stored data size. For migrations of 200k+ rows, ensure the Postgres volume has sufficient headroom before running `--execute`. The Hobby tier default (500 MB) is insufficient for the Hansard archive; the Phase 2A migration required resizing to several GB. Live resize is supported on all paid tiers with zero downtime.

## Deployment discipline — local-first for major Phase builds only

This rule applies to major Phase builds specifically (currently: Phase 1 compliance/auth/Stripe foundation, and Phase 2 stakeholder briefing pack). It does NOT apply to general site development, bug fixes, content updates, or other unrelated work — Mark continues to work on those in the normal way and pushes when ready.

During a Phase build:

- Phase work stays local by default. Don't push Phase-in-progress changes to Railway.
- Mark may explicitly request specific small unrelated changes be pushed during Phase work (typo fixes, minor copy edits, small bug fixes on existing live tools, content updates, anything not part of the Phase). Those pushes are fine — but only what Mark asks for goes; in-progress Phase work doesn't bundle in alongside.
- When the Phase is complete and Mark has reviewed it, deployment to Railway happens as a deliberate single step.
- Stripe stays in test mode throughout the Phase build.

Outside a Phase build:

- Normal development workflow. Test locally, push when ready, no special discipline required.
- The "never push automatically" rule from the Git section still applies — Mark always says "push" or "push it" first.

**Reason for the Phase-specific rule:** Phase builds touch foundations (compliance, auth, billing, AI integration) where half-built state on the live site damages trust with current users. Local-first during these specific builds keeps mistakes invisible. General site work doesn't carry the same risk.

## Environment variables needed on Railway
```
SECRET_KEY=<long random string>
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
TWFY_API_KEY=
NEWS_API_KEY=
BSKY_HANDLE=
BSKY_PASSWORD=
DATABASE_URL=<set automatically by Railway PostgreSQL plugin>
ADMIN_EMAIL=<your login email — grants access to /admin cache management page>
STRIPE_SECRET_KEY=<paid products only — test keys during dev>
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
```

## Local development

**Local environment: Windows 11.** Shell is PowerShell. Postgres CLI tools at `C:\Program Files\PostgreSQL\18\bin\` (on system PATH). `gpg` is not in PATH — use `C:\Program Files\Git\usr\bin\gpg.exe` directly. PG16 server installed at `C:\Program Files\PostgreSQL\16\bin\` (not on PATH) for local restore testing.

**Windows subprocess pattern:** Any script that invokes an external binary via `subprocess.run(["binary", ...])` must use a platform-resolved constant rather than a hardcoded name, because external tools (`gpg`, `psql`, `pg_dump`) are not reliably on PATH on Windows. Follow the `_GPG` / `_PSQL` pattern in `restore_from_backup.py`: `r"C:\full\path\binary.exe" if platform.system() == "Windows" else "binary"`.

**subprocess.run diagnostic pattern — always apply to new scripts:** When calling an external binary with `stdout=PIPE, stderr=PIPE`, always surface stderr regardless of exit code, and pass `errors="replace"` to decode. Silent success with no tables/output is a common failure mode when psql/gpg run but fail internally. Template:
```python
result = subprocess.run([...], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
stderr_out = result.stderr.decode(errors="replace")
if stderr_out.strip():
    log(f"stderr:\n{stderr_out[:3000]}")
if result.returncode != 0:
    die(f"command failed (exit {result.returncode})")
```

```bash
cd c:\Users\marky\hansard_app
pip install -r requirements.txt
python flask_app.py
```
App runs at http://127.0.0.1:5000 — visit /home for the dashboard.

## Accuracy tester
A standalone testing tool lives at `C:\Users\marky\wb_tester` — **entirely separate from this project** (no shared code, no shared DB).

It verifies what the live site returns against direct TWFY/Parliament API ground truth and manually curated fixtures.

```bash
cd C:\Users\marky\wb_tester
pip install -r requirements.txt
# Copy .env.example to .env and fill in TWFY_API_KEY, WB_TEST_EMAIL, WB_TEST_PASS
python app.py
# Open http://localhost:5001
```

Full instructions and known fragilities are in `C:\Users\marky\wb_tester\CLAUDE.md`.

## SEO conventions for archive pages

These conventions are locked for any URL/slug work that builds public archive pages (debate, MP, theme, department, minister). Apply consistently — once Google indexes URLs, changing them costs ranking.

- **URL date format:** `27-april-2025` — lowercase, full month name, no leading zero on day
- **Trailing slashes:** none. Set Flask routing accordingly. 301 redirect any trailing-slash variant to the canonical no-slash URL
- **MP slugs:** `firstname-lastname-constituency` (e.g. `keir-starmer-holborn-and-st-pancras`). Constituency is always included
- **MP seat changes:** stable-first. Once an MP slug is assigned, it never changes even if they switch constituencies. Page content updates to reflect current role, but the URL is permanent
- **Lord slugs:** `firstname-lastname-of-place` matching their official title where possible. Fallback: `firstname-lastname-baron`
- **Constituency normalisation rules:** lowercase, hyphens for spaces, keep "and" (`holborn-and-st-pancras`), normalise "St" to lowercase `st`, strip apostrophes (`st-albans` not `st-alban's`), strip commas
- **Theme / policy area / department slugs** (locked 7 May 2026): implemented in `hansard_archive/slugs.py:slugify_theme()` — the single source of truth, imported by `views.py`, `flask_app.py` (sitemap), and the Jinja `slugify` filter. Rule: lowercase → strip all chars not in `[a-z0-9\s-]` → whitespace to hyphen → collapse multiple hyphens. Verified edge case behaviour:
  - Apostrophes (straight and curly): stripped, not replaced — `"Children's services"` → `"childrens-services"`
  - Ampersands: stripped; surrounding spaces collapse to one hyphen — `"Health & social care"` → `"health-social-care"` (not `"health-and-social-care"`)
  - Non-ASCII / accented characters: stripped entirely, not transliterated — accented chars don't appear in GOV.UK theme taxonomy so this is theoretical
  - Commas, colons, parentheses: stripped — `"Science, technology"` → `"science-technology"`
  - Existing hyphens: preserved — `"Pre-16 education"` → `"pre-16-education"`

## Canonical domain and URLs (locked 1 May 2026)

- **Canonical domain:** `westminsterbrief.co.uk` — naked domain, no www
- **All canonical tags, `og:url`, internal links, and sitemap entries MUST use the canonical domain consistently.** No www, no http, no trailing slash on the domain itself.
- **All non-canonical variants must 301 (or 308) redirect to the canonical HTTPS naked-domain URL.** This includes: `www.westminsterbrief.co.uk/*`, `http://westminsterbrief.co.uk/*`, `http://www.westminsterbrief.co.uk/*`. Never use 302 for these redirects.
- The www→naked redirect is implemented as a Flask `before_request` hook. HTTP→HTTPS is handled by Railway.
- When adding new pages or templates: check that any hardcoded domain references use `westminsterbrief.co.uk`, not `www.westminsterbrief.co.uk`.

## Committee evidence ingestion — known behaviour and rules

### Written evidence is likely missing — re-run needed
The first full ingestion (Apr 2026) produced only 7 written evidence records vs 4,993 oral evidence records. Root cause: the ingester loops oral evidence for all 153 committees first (~2 hours), then written evidence. If Railway restarts the container mid-run the daemon thread dies silently and written evidence is never fetched.

**To recover:** run a full re-ingestion from the admin panel (Fetch & Ingest, all committees, from 2024-01-01). The `UniqueConstraint(publication_id, raw_organisation_name)` means oral evidence is silently skipped as duplicates — only the missing written evidence gets staged.

**To prevent:** the ingestion should ideally interleave oral and written evidence per committee (i.e., finish one committee completely before moving to the next), not do all oral first then all written. If refactoring the loop, change the order from `for pub_type: for committee` to `for committee: for pub_type`.

### High-water mark must be per (committee_id, publication_type)
`get_incremental_start_dates()` in `stakeholder_directory/ingesters/committee_evidence.py` computes the incremental start date per `(committee_id, publication_type)` — NOT just per committee. This is critical: a committee with oral evidence up to 2026 but no written evidence would otherwise get a high-water mark near 2026, silently skipping all its written evidence.

**Rule:** if either evidence type is missing for a committee, that committee's incremental start date falls back to `fallback_start` (2024-01-01). Only use the MIN high-water mark minus buffer when BOTH types are present.

## Known issues / tech debt
- Written Questions search can be slow — Parliament API latency, no caching yet
- The `SECRET_KEY` in flask_app.py is a placeholder — must be overridden by env var on Railway
- Backup files in root (bckup_flask.py etc.) and backup templates are clutter — safe to delete eventually
- No database migration system — relies on `db.create_all()` which is fine for now
- **Pre-push checklist import check connects to production Postgres** (raised 2026-05-25): step 1 of the Pre-push checklist — `python -c "from flask_app import app; print('OK')"` — opens a live Postgres connection when `.env` contains a production `DATABASE_URL`. Contradicts the local→production prohibition established 2026-05-25. Fix: `DATABASE_URL=sqlite:///local_check.db python -c "from flask_app import app; print('OK')"`. Needs testing before the checklist item is updated.

## Active work in progress

**Phase 2A** — Hansard archive live in production. WQ archive live. Member cache seeding live. PQ detail pages, archive search, and sitemap all live.

**Phase 2A.5** — See "Active priorities" section above for current queue.

**Stakeholder directory module** — see `docs/stakeholder-directory-design.md` for full spec. Foundation phase complete (schema, vocabularies, scoring module). Ministerial meetings ingested. Committee evidence partially ingested (written evidence re-run needed — see "Committee evidence ingestion" above).

**Hansard migration** — substantively complete. The `SEARCH_BACKEND=hansard` flag is the production path for parliamentary search; TWFY is no longer the primary data source.

**Phase 1 (compliance + auth + Stripe foundation)** — see `westminster-brief-phase-1-brief.md`. Local-only build, no Railway deployment until complete.

**Phase 2 (paid product: stakeholder briefing pack)** — see `westminster-brief-phase-2-brief.md`. Offline build, no public surface during development. Paid product runs on Claude Opus, governed by the paid-product output rules above.

## Completed (recent)

The minister-led search via Hansard backend is now in production. The original problem (keyword search missing ministers whose responses didn't contain the search terms) is resolved by the search-finds-debates → fetch-all-speeches architecture documented below.

**Implementation plan:**
1. `get_dept_minister_twfy_ids(dept_name)` — GOV.UK minister list for dept → resolve each name to TWFY person ID via Members API name match
2. `fetch_minister_speeches_on_topic(twfy_person_id, expanded_query, date_range)` — TWFY `getDebates?person=ID&search=EXPANDED_QUERY` — use AI-expanded query for better language matching
3. Run ALL minister fetches in parallel alongside existing keyword search in `debates_topic()`
4. Merge via `deduplicate_by_listurl()` → Phase 1 session expansion → minister flagging → group by debate

This works universally across all departments — pull full ministerial team → search their debates → merge.

## Parliamentary Research Tool — design principles

**Core architectural principle (confirmed by user):**
Search finds debates → fetch all speeches from each debate → ministers are always present.
Do NOT rely on ministers' responses containing search keywords. They rarely do.

**When working on the Research Tool (`debate_scanner.py` / `debate_scanner.html`), always ask:**
1. What is the user's department context? (e.g. DfE) — ministerial debates for that dept come first
2. Are we showing debates as a unit (all speakers) or individual speeches? Always prefer debates as a unit.
3. Does the current approach guarantee the responding minister appears? If not, fix it.

**User context:** Higher education civil servant writing briefings. Knows which debates happened.
If the tool misses Baroness Smith of Malvern or other DfE ministers, something is architecturally wrong.

**The "no central debate database" problem:**
There is no single index of "all debates about topic X". The practical solution is:
find ONE matching speech via TWFY keyword search → extract its debate GID → fetch the full debate session.
This is implemented in `fetch_all_debate_sessions()` in `debate_scanner.py`.

## Working with the user

### Who the user is (Mark, the developer)

Note: this section describes the user *of Claude Code* — i.e. Mark, the developer. The product itself serves a wider audience (see "Product audience and positioning" above).

- UK civil servant working in higher education policy
- Knows Parliament well from the inside — knows which ministers spoke, which debates happened, which questions were tabled
- If Mark says the tool misses something he knows happened, trust his domain knowledge — investigate the tool, don't second-guess his memory
- Builds Westminster Brief as a side project, not a venture-backed product. Solo developer with a day job. Time is constrained — sustainable working pace is around 24 hours per week.
- Civil service propriety has been cleared for the project including paid products. Treat this as settled.

### How they communicate
- Often types quickly with typos — interpret intent, don't get hung up on spelling
- Thinks out loud and in fragments — piece together meaning from context
- Will say things like "this is fundamental" or "this is the gem" — pay attention, these are priority signals
- When they say something "should" work a certain way, they usually have a concrete real-world reason grounded in how Parliament actually operates

### How to collaborate effectively
- **Challenge the data retrieval approach** — the user explicitly values being asked "is this the best way to retrieve this information?" before building. Questions about data structure and search logic are described as "goldust". Ask before assuming.
- **Use plan mode for anything non-trivial** — the user wants to think through design before implementation, especially for the Research Tool
- **Validate architecture out loud** — when an approach is backwards (like speech-first vs debate-first), say so clearly and explain why. The user responds well to direct, logical explanation.
- **Don't over-build** — the user wants accessible, clean, downloadable information. Not feature bloat. When in doubt, do less but do it well.
- **Information must always be downloadable** — this is a hard requirement for every results view. Word export is the primary format.

### When to stop and go to plan mode — trigger rules

Go to plan mode (do not write code) if ANY of the following are true:
1. The change touches more than one function that share data (e.g. search → classify → group → render is one pipeline — changing one stage affects all others)
2. The change touches both a backend function and a template
3. The fix involves changing how rows are structured or what fields they carry
4. The user mentions a new feature idea mid-session (capture it, don't build it)
5. The same bug has been attempted twice without a confirmed fix — stop, plan, diagnose
6. The change affects the minister search, session expansion, or grouping logic — these are the most interconnected parts of the codebase
7. The work introduces or modifies AI generation in a paid product code path — paid product output rules must be respected and explicitly verified before code is written

**Why this matters:** Several bugs in this project were introduced by fixes that looked small but shared data paths with other functions. The dept filter, oral classification, and minister search all interact. Fixing one without planning broke assumptions in another. Plan mode forces the interaction map to be drawn before code is written.

### Design principles the user has established
- **Desktop-first** — this is a web tool for officials at their desks. Mobile is a future consideration, not current.
- **Information density over whitespace** — compact cards, smaller fonts in results sections. Don't waste screen space.
- **Download everything** — every section of results must be exportable to Word. Non-negotiable.
- **Cautious language** — nothing on the site should sound definitive. "May help with briefing purposes" not "saves hours". AI outputs are aids, not answers.
- **The Parliamentary Research Tool is the gem** — it is the most important and most complex tool. Give it the most care.
- **Ministerial debates for the selected department come first** — always. This is the primary use case.

### Canonical test case — always verify this works
**Topic:** "student loan repayments" (or "repayment threshold")
**Department filter:** Department for Education
**Expected results (user confirms these debates exist in 2026):**
- **Josh MacAlister OBE MP** — Parliamentary Under-Secretary of State (Minister for Children and Families), DfE. Confirmed current DfE minister. Has spoken on repayments in 2026. Parliament ID: 5033.
- **Baroness Smith of Malvern** — Minister of State for Skills, DfE (Lords). Has spoken on repayments in 2026 but has been unwell; a substitute Lords peer may have covered some sessions.
- Both should appear in their respective debate sessions with minister-first ordering.

**IMPORTANT:** Do not assume someone is a backbencher without checking GOV.UK/Parliament API first. MacAlister was incorrectly identified as a backbencher in one session — always verify role before drawing conclusions.

If either is missing after a search, something is architecturally wrong — investigate before declaring it fixed.
This is a live, high-stakes policy area (student loan repayments is currently a major issue).

### Questions to ask at the start of a Research Tool session
1. Which department are you testing/using this for?
2. What specific debates or questions are you expecting to see that aren't appearing?
3. Is the issue "not finding the debate at all" or "found the debate but missing speeches from it"?

### Parliamentary structure knowledge to keep in mind
- Ministers rarely use the exact policy keywords in their responses — they use government framing language
- Oral Questions sessions: minister gives prepared answer first, then supplementary questions follow — both must be shown
- Written Questions: Q+A are separate items in the API — always show the answer alongside the question
- Debates are the unit of meaning, not individual speeches
- Lords ministers (e.g. Baroness Smith of Malvern) are easy to miss — name normalisation must handle "Baroness X of Y" patterns

### Parliamentary debate types

Full taxonomy and classification rules for the debate types found in Hansard live in `docs/parliamentary-debate-types.md`. Read this when working on classification logic in the Research Tool.

### Minister substitution — a known real-world complication
Ministers are regularly absent (illness, clashes, recess duties) and are covered by substitutes. This breaks name-based minister detection in several ways:

**In the Lords specifically:**
- Each department has a Lords spokesperson (e.g. Baroness Smith of Malvern for DfE)
- If they are absent or unwell, ANY government peer may cover — a whip, a Lord from a different dept, or a junior minister
- The covering peer will NOT appear in GOV.UK's DfE minister listing
- The Hansard record gives NO indication they were covering — it just shows their name
- Confirmed real case: Baroness Smith of Malvern (DfE, Skills) has been unwell; another Lords peer has covered her education duties

**In the Commons:**
- Secretary of State may answer instead of Minister of State
- A PPS or adjacent minister may cover for a specific session

**Implications for the tool:**
- Do NOT rely solely on GOV.UK department listings to identify ministerial voices in Lords debates
- A government peer (Lord in Waiting, Baroness in Waiting, any government whip) speaking in a DfE-topic Lords debate should be treated as a probable departmental spokesperson
- Name-based minister search ("find all Baroness Smith debates") will miss debates where her substitute spoke
- Topic search → full session fetch is MORE reliable than name search for Lords, precisely because it finds the session first and shows whoever spoke for the government in it
- When displaying Lords results, consider flagging government peers broadly, not just the named departmental minister

## Pre-launch checklist

The site currently has `noindex, nofollow` and crawler-blocked robots.txt. Full pre-launch checklist (legal/compliance and technical/SEO) lives in `docs/pre-launch-checklist.md`. Do not remove the noindex tags or update robots.txt without working through that checklist.

## API error handling rules — prevent looping

These apply whenever working with TWFY, Gemini, Anthropic, Parliament API, or any external service.

### Three-strike rule
If the same API call returns the same error or empty result more than twice in a row, **stop**. Do not retry with identical parameters. Report the exact error to the user and suggest manual intervention (e.g. check the API key, widen the date range, try a different endpoint).

### Pre-flight reflection before retrying
Before retrying a failed API call, explicitly identify:
1. What the error was
2. What is different about this retry that will make it succeed

If the answer to (2) is "nothing", abort and report instead.

### Specific error responses
| Error | Action |
|-------|--------|
| 401 / 403 | Assume key invalid/missing. Stop and tell the user to check the env var. Do not retry. |
| 429 | Rate limited. Stop. Tell the user to wait before retrying. Do not loop. |
| Empty result (0 rows) | Report it clearly. Suggest: broader search term, wider date range, remove department filter. Do not silently retry. |
| `Working outside of application context` | Flask threading issue. Wrap the thread call with `copy_current_request_context`. Do not retry the same code. |
| `AttributeError` on a SQLAlchemy model | Check for column name conflicts with SQLAlchemy internals (e.g. `query`, `metadata`). Rename the column. |
| `UnboundLocalError` | Variable only set inside a conditional block. Add a default value before the block. |

### Loop detection
Never repeat the same sequence of tool calls with identical arguments without a confirmed state change in between. If the same error appears after a fix attempt, re-read the relevant code section before trying again — don't apply the same fix twice.

### Session reset — breaking a doom loop
If the same bug has been attempted more than twice with no progress, stop and perform a session reset:
1. Run `git diff` to see the full history of changes made during this session
2. Analyse the diff as a static object: "Why did each of these attempts fail?"
3. Identify the root cause from the pattern — not from the last error message alone
4. Only then write a new fix

This forces analysis of the failure history rather than continuing to guess. Do not make a fourth attempt without first completing this analysis.

### Escalate to Opus when stuck
If the same feature or bug has been attempted 3+ times without resolving the underlying cause — not just the surface symptom — **stop and tell the user clearly**: "This has been attempted N times without a stable fix. I recommend getting an Opus review before continuing." Do not make another attempt without Opus input.

Opus is particularly valuable for:
- Data pipeline architecture questions (search → filter → rank → AI payload)
- API behaviour that is undocumented or inconsistent (e.g. Parliament WQ API search semantics)
- Issues where the fix keeps oscillating between two failure modes (too strict ↔ too loose)
- Any change that has been reverted or re-applied more than once
- Any situation where Code is considering a destructive operation as a "fix"
- Credential / auth failures with unclear root cause
- Any production-environment anomaly where the right response is uncertain

### Transient vs permanent errors — different responses required
| Error type | Examples | Correct response |
|---|---|---|
| **Permanent** | 401, 403, invalid key, column name clash | Stop immediately. Do not retry. Report to user. |
| **Transient** | 500, 503, network timeout, connection reset | Retry once after a short pause. If it fails again, report. |
| **Rate limit** | 429 | Stop. Tell user to wait. Do not retry in a loop. |
| **Logic error** | Empty result, wrong data | Investigate the query/parameters. Do not retry identical call. |

For transient errors in production code, use exponential backoff — not a fixed sleep or instant retry:
```python
import time
for attempt in range(3):
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 503:
            break
    except requests.exceptions.ConnectionError:
        pass
    time.sleep(2 ** attempt)  # 1s, 2s, 4s
```
Only apply backoff to transient errors. Never apply it to 401/403/400 — these will never self-resolve.

### Environment verification — when an API key stops working
Before concluding a key is invalid, verify it is actually set:
```bash
# Check if the env var is set (local)
echo $TWFY_API_KEY
echo $GEMINI_API_KEY
echo $ANTHROPIC_API_KEY

# Check what the app sees at runtime
python -c "import os; print('TWFY:', bool(os.environ.get('TWFY_API_KEY'))); print('GEMINI:', bool(os.environ.get('GEMINI_API_KEY'))); print('ANTHROPIC:', bool(os.environ.get('ANTHROPIC_API_KEY')))"
```
On Railway: check the Variables tab in the dashboard. The var must be set on the **service**, not just the project.

Also check `/health` on the live site — it explicitly tests each API and reports pass/fail per service.

### Silent exception rule
`except Exception: pass` or `except Exception: return []` hides bugs. Always log or return an `_error` marker so failures surface in the UI or debug panel. Only use bare `pass` for genuinely expected no-ops (e.g. "column already exists" during migration).

## Threading rules — Flask + SQLAlchemy in threads

**Every** `ThreadPoolExecutor.submit()` call that touches the database or Flask context MUST wrap the function with `copy_current_request_context`. This applies to all thread pools in the codebase — the main search pool, the session expansion pool, and any future pools.

```python
# CORRECT
executor.submit(copy_current_request_context(my_function), arg1, arg2)

# WRONG — crashes with "Working outside of application context"
executor.submit(my_function, arg1, arg2)
```

When adding a new thread pool, search the file for other `ThreadPoolExecutor` blocks and confirm all of them already have this wrapper. Missing one is a common cause of silent failures.

## SQLAlchemy column naming — reserved words to avoid

Never name a SQLAlchemy model column any of these — they shadow built-in SQLAlchemy interfaces and cause `AttributeError` at runtime:

`query`, `metadata`, `session`, `get`, `filter`, `update`, `delete`, `insert`, `select`, `id` (safe as PK only)

Use descriptive names: `search_query`, `result_data`, `cached_at`, etc.

## SQLite vs Postgres divergences — queries that pass locally but fail in production

SQLite is permissive about several SQL constructs that Postgres rejects. Because Westminster Brief develops against SQLite locally and runs Postgres in production, these gaps are silent: local tests pass, production 500s.

**Before writing or reviewing any query that uses `.distinct()`, `.group_by()`, `DISTINCT ON`, or window functions, check it against this list.**

### 1. DISTINCT + ORDER BY — columns must appear in SELECT

```python
# WRONG — SQLite silently allows this; Postgres raises InvalidColumnReference
db.session.query(HansardContribution.session_id)
    .join(HansardSession, ...)
    .distinct()
    .order_by(HansardSession.date.desc())

# CORRECT — add the ORDER BY column to SELECT
db.session.query(HansardContribution.session_id, HansardSession.date)
    .join(HansardSession, ...)
    .distinct()
    .order_by(HansardSession.date.desc())
# Then extract only session_id: [r[0] for r in results]
```

**Real incident:** `/archive/mp/<id>` 500'd in production for ~5 days (481 GSC errors, May 2026). Fixed by adding `HansardSession.date` to the SELECT list.

### 2. GROUP BY — non-aggregated columns must be in GROUP BY

```python
# WRONG — SQLite allows selecting non-aggregated columns; Postgres rejects
db.session.query(HansardSession.id, HansardSession.title, func.count(...))
    .group_by(HansardSession.id)  # title not in GROUP BY

# CORRECT — include all non-aggregated SELECTed columns
db.session.query(HansardSession.id, HansardSession.title, func.count(...))
    .group_by(HansardSession.id, HansardSession.title)
```

Exception: if `id` is the primary key, Postgres allows other columns from that table without listing them (functional dependency rule). But explicit is safer.

### 3. Boolean columns — Python bool vs integer

SQLite stores booleans as integers (0/1). Postgres has a native boolean type. SQLAlchemy handles this correctly via `db.Column(db.Boolean)` — but raw SQL comparisons using `= 1` or `= 0` will fail on Postgres. Always use `IS TRUE` / `IS FALSE` in raw SQL, or use SQLAlchemy ORM comparisons (`Model.flag == True`).

### 4. DISTINCT ON — Postgres only

`DISTINCT ON (expr)` is a Postgres extension. SQLite does not support it. If you need "latest row per group" logic, use a subquery or window function that works on both — or accept that this query is Postgres-only and document it clearly.

### 5. Timezone-aware datetimes

SQLite stores datetimes as plain strings. Postgres distinguishes `TIMESTAMP` (naive) from `TIMESTAMPTZ` (timezone-aware). `datetime.utcnow()` produces a naive datetime — this works with Postgres `TIMESTAMP` columns but will produce warnings or errors with `TIMESTAMPTZ`. Westminster Brief models use naive datetimes throughout; keep it consistent and don't mix.

### Pre-push checklist for new queries

Before pushing any new query that uses DISTINCT, GROUP BY, or subqueries:

1. Does `.distinct()` have an ORDER BY? If yes — is every ORDER BY column in the SELECT list?
2. Does `.group_by()` leave any non-aggregated columns outside the GROUP BY clause?
3. Is any raw SQL using `= 1` / `= 0` for booleans? Change to `IS TRUE` / `IS FALSE`.
4. Does the query use `DISTINCT ON`? Note it as Postgres-only.

## Variables used in render_template must always be initialised

Any variable passed to `render_template()` must be initialised with a default value **before** any `if` block that might set it. If the variable is only set inside `if request.method == 'POST':`, a GET request will crash with `UnboundLocalError`.

```python
# CORRECT
total_available = 0
results = []
if request.method == 'POST':
    total_available = ...

# WRONG — crashes on GET
if request.method == 'POST':
    total_available = ...
return render_template('page.html', total_available=total_available)
```

## Flask template safety — reserved Jinja2 globals

Flask injects these names as Jinja2 globals on every request. Passing any of them as a `render_template()` kwarg silently replaces the global with the kwarg value. The collision causes no error at render time — it only fails later when something in a template (often `base.html`) calls a method on the original global object.

**Reserved names — never use as render_template kwargs:**

| Name | What it is |
|---|---|
| `session` | Flask session proxy |
| `request` | Flask request object |
| `g` | Flask app-context global |
| `config` | Flask app config |
| `url_for` | URL builder function |
| `get_flashed_messages` | Flash message accessor |
| `current_user` | Flask-Login user proxy |

**Pattern to avoid:**
```python
ctx = {"session": hansard_session_obj, ...}
return render_template("page.html", **ctx)
```

**Pattern to prefer — rename the kwarg:**
```python
ctx = {"hs_session": hansard_session_obj, ...}
return render_template("page.html", **ctx)
```

If the kwarg name is load-bearing (templates already reference it), inject truly global values via a `@app.context_processor` instead of calling them through the shadowed global in templates.

**Triggered by:** May 2026 — `_session_context()` in `hansard_archive/views.py` passed a `HansardSession` model as `"session"`. Adding `{% if session.get('admin_authenticated') %}` to `base.html` for the admin link called `.get()` on the model, breaking every `/archive/debate/...` page in production. Fix: context processor injecting `admin_authenticated` directly.

**Defensive measures to build (Phase 2A.5):**
- `tests/test_routes.py` — pytest suite hitting key routes via Flask test client, asserts HTTP 200. Catches base.html regressions and template-context collisions at test time.
- Pre-push lint script — greps `render_template()` calls for reserved kwarg names, fails if found.

## TWFY API — known quirks

- **Date range + `person=` param**: TWFY ignores the date range when `person=` is also set. Always apply a Python-level date filter after fetching minister speeches. Never rely on TWFY to enforce the date.
- **Date format**: TWFY expects `YYYYMMDD..YYYYMMDD` in the search string. Python `hdate` fields return `YYYY-MM-DD`. These are different — convert before comparing.
- **Empty result ≠ no data**: TWFY returns `{"rows": []}` (not an error) when it finds nothing. Always check `len(rows) == 0` separately from checking for error keys.
- **`type=` param**: Only valid for the `getDebates` endpoint. Do not pass it to `getWrans` or `getWMS` — it will be silently ignored or cause errors.

## WQ API constraints — read before changing any tracker or WQ-related code

The Parliament Written Questions API at `questions-statements-api.parliament.uk/api/writtenquestions/questions` is documented in its OpenAPI spec at `https://questions-statements-api.parliament.uk/index.html`. That spec is authoritative — refer to it when in doubt. The constraints below have been verified against it and by live testing in April 2026.

### Correction notice — previous constraints were wrong

An earlier version of this section (present until April 2026) documented three constraints that turned out to be false:

- *"tabledStartDate and tabledEndDate are silently ignored"* — **False.** The API does not have these parameters. The correct parameter names are `tabledWhenFrom` and `tabledWhenTo`. We were passing the wrong names; the API was correctly ignoring them.
- *"answeringBodies causes 30s+ timeouts"* — **Conditionally false.** It causes timeouts when used without a date filter (full-table scan across 661k rows). Combined with `tabledWhenFrom`, it responds in ~2 seconds. The root cause was the missing date anchor, not the parameter itself.
- *"isAnswered is silently ignored"* — **False.** The correct parameter name is `answered` (enum: `Any`, `Answered`, `Unanswered`).

The takeaway: the API behaved exactly as the OpenAPI spec describes. Our diagnostics were flawed because we were passing wrong parameter names and drawing causal inferences from the wrong evidence. The `take=500 / max(tabled_dates) / client-side everything` workaround was solving a self-inflicted problem.

### Confirmed working parameters (verified April 2026)

| Parameter | Type | Behaviour |
|---|---|---|
| `tabledWhenFrom` | date string `YYYY-MM-DD` | Filters to questions tabled on or after this date. **Works reliably.** |
| `tabledWhenTo` | date string `YYYY-MM-DD` | Filters to questions tabled on or before this date. **Works reliably.** |
| `answered` | enum: `Any` / `Answered` / `Unanswered` | Server-side answered filter. **Works reliably.** |
| `answeringBodies` | integer (dept ID) | Filters by answering department. **Works reliably when combined with `tabledWhenFrom`.** Do not use without a date anchor — full-table scan will timeout. |
| `house` | `Commons` / `Lords` | Works. |
| `searchTerm` | string | Works (full-text search). |
| `take` / `skip` | integer | Pagination, works. |
| `questionStatus` | `NotAnswered` / `AnsweredOnly` / `AllQuestions` | Alternative to `answered`, also works. |

### Working API call examples

```python
import requests
from datetime import datetime, timedelta

url = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"

# Example 1: All unanswered questions tabled yesterday
yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
resp = requests.get(url, params={
    'tabledWhenFrom': yesterday,
    'tabledWhenTo': yesterday,
    'answered': 'Unanswered',
    'take': 1000,
}, timeout=30)

# Example 2: All unanswered DfE questions tabled in the last 7 days (~2s response)
week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
resp = requests.get(url, params={
    'tabledWhenFrom': week_ago,
    'answeringBodies': 60,   # DfE dept ID
    'answered': 'Unanswered',
    'take': 500,
}, timeout=30)

# Example 3: Paginated fetch for high-volume days
all_results = []
skip = 0
while True:
    resp = requests.get(url, params={
        'tabledWhenFrom': yesterday,
        'tabledWhenTo': yesterday,
        'take': 500,
        'skip': skip,
    }, timeout=30)
    batch = resp.json().get('results') or []
    all_results.extend(batch)
    if len(batch) < 500:
        break
    skip += 500
```

### Implementation pattern for the tracker

The tracker should use server-side filtering, not the client-side workaround:

1. Set `tabledWhenFrom` to yesterday (or walk back up to 7 days if yesterday returns 0 results — handles recess correctly)
2. Set `answeringBodies` to the selected department ID
3. Set `answered=Unanswered`
4. Paginate with `skip` if needed (a busy post-recess day can exceed 1000 questions for a large department)

### Domain facts that constrain the design

- **MPs and Lords cannot table written questions during recess.** The Table Office is closed for tabling. Each WQ has a real `dateTabled` reflecting an actual sitting day.

- **A typical sitting day produces 200–500 WQs across all departments and houses.** Heavy days (post-recess return, end of session, major events) can reach 600–1200. Paginate rather than assuming a single `take=500` covers a full day.

- **Recess detection:** If a lookback of 7 days returns 0 questions, Parliament is likely in recess. Surface a banner — "Parliament not currently sitting — no questions tabled in the last 7 days" — rather than showing a confusingly empty page.

---

## Working principle: status-check before scoping

When picking up a piece of work that may have been progressed in earlier sessions (with this Opus, a previous Opus, or via Code directly), do not assume the spec doc reflects current state. The spec captures locked decisions; it does not always capture what's been built since the last edit.

Before drafting a brief or scoping next steps, ask Code (or Mark) for current state. Two questions usually sufficient:

- "What's been built since [last touchpoint]?"
- "What's actually left vs what the spec doc lists as pending?"

Even when the spec doc has been updated recently, this check is cheap and catches drift. The cost of an unnecessary status check is small. The cost of drafting a brief against a stale spec is larger — it wastes Code's time, risks rebuilding things that exist, and can introduce regressions if Code starts on the redundant work.

---

## When to escalate to Opus / chat session

Most prompts to Claude Code are implementation work where the design is already clear: build this feature, fix this bug, write this test, refactor this function. Code can and should proceed with these directly.

But there's a category of situations where code should pause, surface findings to Mark, and recommend bringing the question to a chat session (Opus) for strategic input *before* implementing. The pattern: code is reliable at doing the work, less reliable at recognising when the work is the wrong work.

### Trigger conditions for escalation

**1. Architectural decisions with cross-cutting impact.**
- A change would affect multiple subsystems
- A new pattern is being introduced that other code might follow
- A schema change would require migration across multiple tables or features
- The right answer depends on product strategy, not just technical correctness

**2. Surprising diagnostic findings.**
- An external API isn't behaving as documented
- A previously-working feature has regressed
- A constraint document conflicts with observed behaviour
- A code change is producing unexpected side effects in unrelated areas

**3. Product or scope questions.**
- The user request is ambiguous between two materially different interpretations
- The change touches user-facing behaviour where the right design depends on audience considerations
- The work feels out of proportion to the value

**4. Civil service or operational considerations.**
- Anything that materially shifts Westminster Brief from "private project" toward "public service"
- Scheduled jobs, public-facing accounts, branded social media, paid integrations

**5. Resource implications.**
- A change would meaningfully increase API costs (LLM calls — Opus invocations are especially cost-sensitive)
- A change would require new paid services

**6. Output rule boundary changes.**
- A free feature being asked to produce authored inference
- A paid product code path losing one of the six safeguards

### What escalation looks like in practice

1. Stop before implementation.
2. Report findings or context to Mark with specifics.
3. Recommend bringing the question to Opus if Mark wants strategic input.
4. Wait for direction. Don't proceed with a guess.

---

## Capture ideas in the backlog

Mark generates ideas at high volume mid-session. When a new idea comes up that isn't being actioned immediately, Claude Code should capture it in `docs/ideas-backlog.md` — name, one-line description, revisit trigger — without being asked. This is Claude's job, not Mark's.

---

## Exploratory work and branches

When Mark proposes a substantial new feature or direction:
1. Engage with the substance briefly to clarify the brief
2. Suggest creating a feature branch (e.g. `experiment/inquiry-tracking`)
3. Build a v1 sketch on the branch, not on master
4. After review, decide whether to merge, iterate, or shelve
5. Master stays clean throughout

---

## Preserving documented architectural decisions

When previous commits deliberately removed or avoided something with a stated reason, do not reintroduce it without engaging with that reason (Chesterton's fence principle).

The tracker regression of April 2026 was caused by reintroducing the `answeringBodies` parameter that two prior commits had removed with a clear stated reason. The reintroducing commit's message claimed to be fixing an indentation bug — the actual diff replaced the working architecture with a previously-rejected approach.

### Working principle

When changing any code that has a documented constraint:

1. **Read the relevant constraint document before making the change.**
2. **If a previous commit's documented decision conflicts, surface it.** Don't silently override.
3. **Commit messages must accurately describe the diff.**
4. **Verify regressions haven't been introduced** before declaring a change complete.

### Verification corollary — constraint documents are beliefs, not truth

Constraint documents record what was believed at the time of writing — they can be wrong, stale, or based on a flawed diagnostic.

**The April 2026 lesson:** three constraints ("date params ignored", "answeringBodies times out", "isAnswered ignored") were all false. They were derived from experiments that used wrong parameter names. The API worked correctly all along.

When a constraint and an official spec disagree, trust the spec and test directly.

---

## After fixing a caching bug — clear the cache

If a bug caused incorrect data to be written to the cache (e.g. wrong date filtering, wrong column), that bad data persists until the TTL expires (6h for searches, 30 days for sessions). After deploying a cache fix, go to `/admin` and clear the relevant cache immediately rather than waiting for TTL.

## Session testing protocol — research tool status tracking

Each coding session that touches the Research Tool must begin by establishing current status and end by confirming it.

### At the start of each session — establish baseline

Before writing any code, ask the user:
1. Which sections are currently working and which are broken?
2. What is the canonical test case you want to verify? (topic, department, expected results)
3. Is the Railway cache clear? (If stale data is possible, clear it at `/admin` before testing)

### Minimum verified checklist — confirm before declaring anything fixed

| Section | What to verify |
|---|---|
| **Oral Questions** | MacAlister or a DfE minister appears; DfE sessions not filtered out |
| **Written Questions** | No duplicate cards; `is_answered` correct; date ordered newest-first |
| **Debates / Westminster Hall** | Sessions grouped correctly; speeches visible |
| **Ministerial Statements** | DfE statements only when dept filter active |
| **Minister-led search** | At least one of MacAlister / Baroness Smith appears |
| **Word download** | Briefing downloads without crash; checkboxes work for all section types |
| **Checkboxes** | Present on Oral, WMS, WQ, Debates sections |

### After deploying a fix — mandatory verification steps

1. Clear Railway cache at `/admin` (stale cache masks bugs)
2. Run the canonical test case live on the deployed app
3. Confirm the specific thing that was broken is now working
4. Note any regressions — do not close a bug without checking adjacent sections

### Feature flags and backends

Currently active flags:
- `SEARCH_BACKEND=hansard` → uses Hansard API for minister search (Phase 1)
- Unset → uses TWFY for all searches (original behaviour)

### Current invariants — must hold

- WQ cards show the question text as the question, not the minister's answer
- WQ deduplication by UIN — no duplicate cards
- `is_answered` set correctly based on answer HTML being non-empty after stripping
- Lords oral questions classified correctly via title pattern + word count threshold
- Minister search uses the AI-expanded query, not the raw topic
- WMS department filter is title-match based, not full-text
- Checkboxes present on Oral, WMS, WQ, and Debates sections
- Minister-led search via Hansard backend works for canonical test case (DfE + student loan repayments)

---

## Beta environment

A private beta Railway service (`wb-beta`) runs alongside production. It shares the production Postgres database via a read-only user, points at the `beta` git branch, and sits behind a simple password gate. It serves as a preview layer for new features before they ship to production.

### Branch workflow

```
feature/xxx  →  beta  →  master
```

- Feature branches merge into `beta` for preview
- Once validated on beta, merge `beta` → `master` (and Railway auto-deploys production)
- `beta` branch must always be a superset of `master` — never merge master back to beta selectively

**Return to beta after operational master work.** Sometimes there's a legitimate reason to be on `master` directly — e.g. a Railway one-shot service tracks `master`, so a script/fix must land there to be deployable. When that happens: finish the operational reason, then **switch back to `beta` for subsequent commits**. Don't keep committing on `master` by momentum — including doc commits. Changes flow UP (`feature → beta → master`); committing on master out of momentum inverts that and leaves beta behind, defeating beta-as-superset (beta is meant to see things *before* master). The `master → beta` reconciliation merge is an occasional fix for when this slips, NOT a routine direction — routine use erodes the point. (Same failure shape as other drifts: a correct reason to deviate, then drifting past the reason. Watch for it.)

### Railway service configuration (set once in Railway dashboard)

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `beta` |
| `BETA_PASSWORD` | *(chosen password — keep in 1Password)* |
| `SKIP_MIGRATIONS` | `1` |
| `DATABASE_URL` | `postgresql://wb_beta:<password>@hopper.proxy.rlwy.net:50798/railway` |
| `SECRET_KEY` | *(separate random string — not the same as production)* |
| All other API keys | Same as production |

**Branch:** set Railway service source to the `beta` branch.

### Read-only DB user

SQL to create the read-only `wb_beta` user lives at `docs/beta-readonly-user.sql`. Run it once in the Railway Postgres console (Query tab). The password goes into the `DATABASE_URL` env var above.

### What `SKIP_MIGRATIONS=1` does

When set, the startup block skips:
- `db.create_all()` — would fail with a read-only user
- MemberLink and User dev seeds — writes that would fail
- `seed_all_minister_links` background thread — writes that would fail

All ALTER TABLE migration blocks already have `try/except`, so they are silently no-ops.

### Beta auth gate

All routes on the beta service redirect to `/beta-login` (a simple password form) unless the visitor has a valid `beta_authenticated` session cookie. No Flask-Login dependency — it's a separate lightweight gate.

### URL

`beta.westminsterbrief.co.uk` — CNAME in Cloudflare DNS pointing to the Railway beta service hostname. Add with DNS-only (grey cloud) initially so Railway can verify the domain and provision SSL; proxy can be enabled after verification.

### BETA banner

When `ENVIRONMENT=beta`, a yellow banner appears at the top of every page (injected via `is_beta` context processor in `flask_app.py`, rendered in `base.html`).

### When NOT to touch beta

Beta is only a preview layer — it does not have its own data, its own users, or its own state. Don't:
- Run write scripts against beta (its DB user is read-only)
- Use beta as a staging environment for schema migrations (apply to production directly)
- Push directly to `beta` branch for production-bound changes — go through `feature/* → beta → master`

## Working in parallel — two consoles, one repo

When two consoles work the same repo at once (e.g. one on stats, one on manifesto/templates), they SHARE one working tree, one HEAD, and one .git. File edits to DIFFERENT files don't collide. The hazards are shared-state operations. Rules:

**1. Both consoles stay on the same branch (normally `beta`). No branch switching while the other is active.**
There is ONE checked-out branch (shared HEAD). A `git checkout`/`switch`/`reset` in one console changes the working tree under the other mid-edit. If a console genuinely needs a different branch, it must coordinate first (tell the other, agree a pause) — never switch unilaterally while the other is mid-flight.

**2. Edit different files. Same-file parallel edits are not allowed without explicit coordination.**
Divide by area (e.g. one console: stats code; other: templates/CSS). If both need the same file, serialise it — one finishes and commits before the other starts. Don't both edit one file simultaneously.

**3. Master-promotion is SERIALISED — only ONE console pushes/merges to master at a time.**
This is the real collision point (it's what required stopping a push mid-flight on 6 Jun). Committing to beta in parallel is fine (commits interleave safely). But two consoles doing beta→master merge+push simultaneously collide. So: before any console promotes to master, confirm the other console is NOT also about to. Live production fixes take master priority — the other console pauses its own master-push until the fix has landed. One master-promotion in flight at a time, full stop.

**4. No uncommitted changes left floating while the other console is active.**
An uncommitted change is INVISIBLE to the other console and can be clobbered by a checkout, swept into the wrong commit, or forgotten (the 6 Jun admin.html situation). If you have a change you're not ready to commit, either commit it (WIP commit is fine, amend later) or `git stash` it — so it's visible and safe, not floating in the shared working tree.

**5. Explicit paths only — NEVER `git add -A`.** (Already a standing rule, doubly critical in parallel: add -A in one console sweeps in the OTHER console's uncommitted changes + untracked clutter.) Stage only the specific files you own in this commit.

**6. No `reset`/`rebase`/branch-delete/force-push while the other console is active.**
These rewrite shared history or state. Coordinate (agree a pause) before any history-altering operation. Never unilaterally.

**7. When the situation changes, re-check stale instructions before running them.**
An instruction written before new information (e.g. before a production bug was found) may be wrong to run now. If something material changed since an instruction was given, pause and re-confirm rather than executing the now-stale plan. (6 Jun: a test-push instruction was correctly stopped because a production 500 was found after it was written, making it the wrong moment to push to master.)

### If parallel work gets frequent: consider `git worktree`

Careful coexistence (the rules above) is enough for occasional parallel work (edit templates while doing stats on beta). If two-console work becomes routine, set up a `git worktree` — each console gets its OWN directory + branch (fully separate working files, shared history). That eliminates the shared-HEAD/working-tree hazard entirely (rules 1, 2, 4 stop mattering; only rule 3 — serialised master-promotion — still applies because they share history). Worth it if parallel becomes the norm; overkill for occasional.

## Git and pushing

**Never push automatically.** Always commit locally and show what changed, then wait for explicit instruction to push. The user will say "push" or "push it" when ready. This applies to small fixes and template changes as much as anything else.

When Mark requests a push of a small unrelated change while in the middle of a Phase build, push only the requested change — do not bundle in-progress Phase work alongside it.

### Never `git add -A` / `git add .` in this repo — explicit paths only

The working tree permanently carries untracked clutter — manifesto PDFs in
`docs/`, a 380 MB `intelligence.db.backup`, run/backfill logs under `logs/`,
underscore-prefixed scratch scripts in `scripts/`, `*_output.txt` probes,
scratch screenshots in `static/`. A blanket `git add -A` (or `git add .`) sweeps
all of it into the commit. This happened 2026-06-05 during the re-discovery
merge: a single `git add -A` staged ~400 MB of junk including the db backup
blob; the commit had to be unwound and rebuilt from explicit paths.

**Rule:** stage with explicit paths only — `git add path/to/file ...`. Never
`git add -A` or `git add .`. The `.gitignore` was hardened the same day (backups,
logs, `scripts/_*`, screenshots, `.claude/`) as a structural backstop, but the
clutter set drifts — do not rely on `.gitignore` alone; name the files you mean.

## Operational logging — deploys.md

`docs/deploys.md` is the append-only ledger of every production-touching
operation. It is the source of truth for current production state.

### Push discipline

Before every `git push` to `master`, add a row to the push log in
`docs/deploys.md`. The update goes in the same commit as the work — not a
separate commit after the push.

| Field | What to put |
|-------|-------------|
| Date | YYYY-MM-DD |
| SHA | 7-char short SHA of HEAD commit being pushed |
| Summary | One line — same as the commit message subject |
| Operator | Mark (or "Claude Code" if acting on explicit instruction) |

**Never push to master without updating `docs/deploys.md` first.**

**Exception — emergency reverts:** If production needs an immediate hot revert
(broken deploy, live incident), push the revert first to restore service. Update
`docs/deploys.md` within the hour once the situation is stable. Speed of
recovery takes priority over logging discipline.

### Production SQL discipline

Any SQL run directly against production Postgres — Railway Query console, psql,
or a script using the production DATABASE_URL — must be appended to the
"Production SQL log" section at the bottom of `docs/deploys.md`, dated and with
the full statement(s) and a one-line context note.

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

### Current state shortcut

When asked "what's the current state?" or "where are we?", read
`docs/deploys.md` and report: last master push (SHA + date), any recorded
half-states, and the most recent production SQL entry.

## Pre-push checklist

Before every `git push`:
1. `python -c "from flask_app import app; print('OK')"` — must return OK. Fix import errors first.
2. `git status` — confirm only intended files are staged. No accidental .env, debug scripts, or unrelated changes.
3. Confirm you are on a known-good branch (not a detached HEAD or experiment branch).

After Railway deploys, verify with these full URLs:

| Check | URL |
|---|---|
| Health (commit hash + API status) | `https://westminsterbrief.co.uk/health` |
| PQ detail page | `https://westminsterbrief.co.uk/archive/pq/111792` |
| Archive search with filter | `https://westminsterbrief.co.uk/archive/search?q=franchising` |
| Sitemap | `https://westminsterbrief.co.uk/sitemap.xml` |
| Archive home | `https://westminsterbrief.co.uk/archive` |

## Pre-share checklist

Run this before sending the site to any new user group. Walk each of these end-to-end with realistic data:

- [ ] Landing page renders cleanly
- [ ] Hansard Archive index + **at least one session detail page** (click through from search results)
- [ ] A PQ detail page — confirm asking MP name and answering minister name both appear
- [ ] An MP archive page (e.g. `/archive/mp/<id>`)
- [ ] Search results page with results, and an empty-results state
- [ ] Stakeholder Directory index + at least one organisation detail view
- [ ] Member Research + Member Profiles for one MP
- [ ] Parliamentary Research Tool (both tabs, run a real search)

Also:
- [ ] No 500 errors, no missing data fields, no broken layouts on any of the above
- [ ] Check Google Search Console for new error patterns since last push

**Triggered by:** May 2026 — two share-blocking bugs (session detail pages returning 500 from a Flask session variable collision; asking MP and answering minister names missing from all PQ detail pages) went undetected during a landing-page polish review and were only found by spot-clicking detail pages.

## Things to avoid
- Don't use port 5432 for Supabase if ever added — use the connection pooler on 6543
- Don't hardcode API keys or .env paths
- Don't use Flask dev server in production (`debug=True` is only active when running locally via `__main__`)
- Don't add a new `ThreadPoolExecutor` without `copy_current_request_context` on every `submit()` call
- Don't name SQLAlchemy columns `query`, `metadata`, or `session`
- Don't leave variables uninitialised before `render_template()` calls
- Don't invoke Claude (Anthropic API) from any free-feature code path
- Don't remove the AI-disclosure footer from paid product outputs at the standard tier
- Don't run any destructive Railway API call, CLI command, or destructive operation against any cloud provider
- Don't use API tokens or credentials found in files for tasks unrelated to their documented purpose
- Don't autonomously "fix" credential mismatches, auth failures, or unexpected production state — stop and surface to Mark
- Don't run raw SQL against production Postgres
- Don't run `git push --force`, `git reset --hard`, or other destructive git operations without explicit instruction

## Destructive operations — absolutely forbidden

Context: in April 2026, an AI coding agent on Railway infrastructure deleted a startup's entire production database and backups in a single API call by autonomously "fixing" a credential mismatch. The rules below exist specifically to make that failure mode impossible for Westminster Brief.

### Things Claude Code must NEVER do

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

If Claude Code encounters any of the following, STOP and surface to Mark:
- Authentication or credential mismatch
- API token rejected or expired
- Database connection failure with unclear cause
- Production environment showing unexpected state (rows missing, schema drift, unexpected migrations)
- Any error suggesting the production system is in a state Claude doesn't understand

The correct response is always: report what's happening, propose options, wait for direction.

### Token hygiene

API tokens, credentials, and secrets are documented per-purpose. Their stated purpose IS their permitted use. If Claude Code's task requires a credential that isn't already wired up through the documented mechanism (env vars, config files referenced in code), the task stops. Mark provisions credentials. Claude Code uses them as documented. No improvisation.

## Recovery preparedness

Westminster Brief stores user data on Railway Postgres. Railway's backup model stores snapshots in the same volume as the source data — meaning a volume deletion erases backups too. We mitigate this with explicit external backup discipline.

### Backup inventory — what is and is NOT recoverable

**Recoverable from external systems even if Railway is wiped:**
- DNS records: Cloudflare DNS dashboard
- Stripe transactions: Stripe dashboard (full history)
- Email logs: Postmark dashboard (last 45 days)
- Code: GitHub repository (full git history)

**Recoverable only from Railway-side state (at-risk surface):**
- User accounts and authentication data
- User preferences (sector, department, policy area, etc.)
- Saved searches, alerts, briefing history (Phase 2)
- Any data created by the application without an external mirror

### External backup

Daily automated `pg_dump` to Cloudflare R2 via `backup-cron-r2`. Restore tested end-to-end 13 May 2026 (see `docs/recovery-runbook.md`). Repeat drill every 6 months and after schema migrations.

### Recovery runbook

`docs/recovery-runbook.md` documents where backups live, how to restore, which env vars to set, DNS records to verify, and smoke tests to run after recovery.
