# Westminster Brief — Project Instructions

## What this project is

Westminster Brief is a parliamentary research and stakeholder intelligence tool for UK policy professionals — built by a civil servant, free for gov.uk users.

Lets users search Hansard, track Written Questions, analyse debates, research stakeholders, and generate Word briefings. Deployed on Railway at `westminsterbrief.co.uk`.

## Stack
- **Backend:** Flask 3.0 with blueprints, deployed on Railway
- **Database:** SQLite locally → PostgreSQL on Railway (auto-switched via `DATABASE_URL` env var)
- **AI:** Google Gemini API (`google-genai`, model: `gemini-1.5-flash` and `gemini-embedding-001`)
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

**Output rule: factual or extracted, never authored.** The tool finds and surfaces evidence — speeches, citations, engagements, statements. It does not draft positions, lines to take, recommended responses, or anything that implies authored content for which a civil servant would normally hold accountability. Where AI is used (summary, classification, extraction), it operates on factual material the tool has actually retrieved, with citations — not on training-data knowledge. Outputs that look or feel like authored civil service work product (minutes, submissions, drafted lines) are explicitly out of scope.

This rules out, for example: AI-drafted PQ responses, suggested ministerial statements, auto-generated press lines, draft holding lines, recommended Q&A briefs. The reframed "Key ministerial statements" feature (verbatim extraction with citations, parser-rejection of unmatched quotes) is consistent with this principle and remains on the roadmap. Anything that requires the tool to author rather than extract does not.

## Design principles

`docs/design-principles.md` is the authoritative visual and copy guide for any redesign work. Read it before implementing any UI or landing page changes. Key summary: restrained, content-first, type-led; reference gov.uk, FT, Stripe docs; avoid gradients, glassmorphism, oversized hero text, generic SaaS copy.

## Module-level design docs

When working on a specific module, read its design doc first:

- `stakeholder_directory/`: `docs/stakeholder-directory-design.md`
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
| Google Gemini | `GEMINI_API_KEY` | AI summaries, embeddings, categorisation |
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
- **Custom domain:** `westminsterbrief.co.uk` (GoDaddy DNS → Railway)
  - `www` CNAME → `5jac57s9.up.railway.app`
  - `_railway-verify` TXT record added for domain verification
  - Root `@` A record: update or forward to www once GoDaddy allows
- HTTPS is handled automatically by Railway (Let's Encrypt) once DNS verifies
- Add a **PostgreSQL plugin** in Railway — it sets `DATABASE_URL` automatically
- Set all env vars in Railway dashboard (see API table above)
- Also set `SECRET_KEY` to a long random string in Railway env vars
- GitHub repo: `markyf801/Westminsterbrief` — Railway auto-deploys on push to `master`

## Environment variables needed on Railway
```
SECRET_KEY=<long random string>
GEMINI_API_KEY=
TWFY_API_KEY=
NEWS_API_KEY=
BSKY_HANDLE=
BSKY_PASSWORD=
DATABASE_URL=<set automatically by Railway PostgreSQL plugin>
ADMIN_EMAIL=<your login email — grants access to /admin cache management page>
```

## Local development
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

## Active work in progress

**Stakeholder directory module** — see `docs/stakeholder-directory-design.md` for full spec. Foundation phase complete (schema, vocabularies, scoring module). Next: ingester for ministerial meetings (Prompt 3 in the build plan). Module is parallel to existing tables — does not modify `StakeholderOrg`, `TrackedStakeholder`, or any existing data layer.

**Hansard migration** — substantively complete. The `SEARCH_BACKEND=hansard` flag is the production path for parliamentary search; TWFY is no longer the primary data source.

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
- Builds Westminster Brief as a side project, not a venture-backed product. Solo developer with a day job and family. Time is genuinely constrained.

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

These apply whenever working with TWFY, Gemini, Parliament API, or any external service.

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

# Check what the app sees at runtime
python -c "import os; print('TWFY:', bool(os.environ.get('TWFY_API_KEY'))); print('GEMINI:', bool(os.environ.get('GEMINI_API_KEY')))"
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

## When to escalate to Opus / chat session

Most prompts to Claude Code are implementation work where the design is already clear: build this feature, fix this bug, write this test, refactor this function. Code can and should proceed with these directly.

But there's a category of situations where code should pause, surface findings to Mark, and recommend bringing the question to a chat session (Opus) for strategic input *before* implementing. The pattern: code is reliable at doing the work, less reliable at recognising when the work is the wrong work.

### Trigger conditions for escalation

Code should pause and recommend escalation when any of the following are true:

**1. Architectural decisions with cross-cutting impact.**
- A change would affect multiple subsystems (e.g. directory + tracker + WQ scanner all use the same API helper)
- A new pattern is being introduced that other code might follow (e.g. caching strategy, retry logic, error handling shape)
- A schema change would require migration across multiple tables or features
- The right answer depends on product strategy, not just technical correctness

**2. Surprising diagnostic findings.**
- An external API isn't behaving as documented
- A previously-working feature has regressed
- A constraint document conflicts with observed behaviour
- A code change is producing unexpected side effects in unrelated areas

When code's diagnosis surfaces something surprising, the right next step is usually to verify the diagnosis with Opus rather than act on it. Today's WQ API parameter discovery (where the documented constraint was wrong because we'd been using wrong parameter names) is a paradigm case.

**3. Product or scope questions.**
- The user request is ambiguous between two materially different interpretations
- Implementing what's literally asked would produce a worse outcome than implementing what's likely meant
- The change touches user-facing behaviour where the right design depends on audience considerations
- The work feels out of proportion to the value (much smaller, or much larger, than expected)

**4. Civil service or operational considerations.**
- Anything that materially shifts Westminster Brief from "private project" toward "public service"
- Scheduled jobs, public-facing accounts, branded social media, paid integrations
- Anything that would normally trigger a disclosure conversation

**5. Resource implications.**
- A change would meaningfully increase API costs (LLM calls in particular)
- A change would require new paid services
- A change would significantly increase storage, bandwidth, or compute usage

### What escalation looks like in practice

When a trigger fires, code should:

1. **Stop before implementation.**
2. **Report findings or context to Mark with specifics.** Not "this is complex, want help?" but "I've found X, the implications are Y, the options are A, B, or C."
3. **Recommend bringing the question to Opus** if Mark wants strategic input before proceeding.
4. **Wait for direction.** Don't proceed with a guess.

### What is *not* a trigger

Code should *not* escalate for:

- Routine implementation work where the brief is clear
- Bug fixes with obvious causes
- Test additions
- Documentation updates
- Refactors within a single function or file
- Anything where the right answer is clear from CLAUDE.md or the design docs

The escalation pattern is for situations where strategic judgement adds value, not for every uncertain moment.

### Examples from recent sessions

- **Should escalate:** "The tracker fetches 500 questions and assumes the API returns UIN-descending. I've discovered the API actually has working date filters under different parameter names. Should we keep the workaround or switch?"
- **Should not escalate:** "I've added the question type derivation logic and three tests. All pass."
- **Should escalate:** "Mark asked for inquiry tracking. Implementing it would add a new schema, a new ingester, and ~15 hours of work. Worth confirming before starting."
- **Should not escalate:** "Mark asked for badges on engagement rows. I've implemented them and the tests pass."

---

## Capture ideas in the backlog

Mark generates ideas at high volume mid-session. When a new idea comes up that isn't being actioned immediately, Claude Code should capture it in `docs/ideas-backlog.md` — name, one-line description, revisit trigger — without being asked. This is Claude's job, not Mark's.

The format is: add to the Active section, note what conditions would make it worth revisiting, and move on. Don't let ideas drift into conversation history where they'll be lost.

When a revisit trigger condition applies in a later session, surface the idea to Mark for a decision. Don't act on it; surface it. If Mark explicitly kills an idea, move it to the Killed section with a one-line reason so it doesn't keep resurfacing.

---

## Exploratory work and branches

Mark generates ideas at high volume. Many are good and worth exploring; sketching them in service of evaluating them is valuable. The discipline isn't "don't have ideas" — it's "don't sketch them on top of in-flight work or on the main branch."

When Mark proposes a substantial new feature or direction:
1. Engage with the substance briefly to clarify the brief
2. Suggest creating a feature branch for the sketch (e.g. `experiment/inquiry-tracking`)
3. Build a v1 sketch on the branch, not on main
4. After review, decide whether to merge, iterate, or shelve
5. Main stays clean throughout

This preserves Mark's generative working style while protecting the production branch from half-finished experiments.

---

## Preserving documented architectural decisions

When previous commits deliberately removed or avoided something with a stated reason, do not reintroduce it without engaging with that reason. This is the standard "Chesterton's fence" principle: there was a fence; before removing it, find out why it was put there.

This has bitten the project before. The tracker regression of April 2026 was caused by reintroducing the `answeringBodies` parameter that two prior commits had removed deliberately with a clear stated reason ("causes 30s+ timeouts"). The reintroducing commit's message claimed to be fixing an indentation bug — the actual diff replaced the working architecture with a previously-rejected approach.

### Working principle

When changing any code in this codebase that has a documented constraint or a deliberate architectural pattern:

1. **Read the relevant constraint document before making the change.** For WQ-related code, read the WQ API constraints section above. For directory-related code, read `docs/stakeholder-directory-design.md`. For dashboard-related code, `docs/dashboard-roadmap.md`. For design changes, `docs/design-principles.md`.

2. **If a previous commit's documented decision conflicts with the change being made, surface it.** Don't silently override. Either: refute the original reasoning explicitly with new evidence, or design around the constraint. Don't pretend the constraint isn't there.

3. **Commit messages must accurately describe the diff.** A commit that replaces a working architecture should not be described as "fix indentation bug" even if there's an indentation issue elsewhere in the changed lines. Future debugging depends on commit messages matching reality.

4. **Verify regressions haven't been introduced.** Before declaring a change complete, test that the previous working behaviour still holds. The tracker regression existed undetected for over a week because the change wasn't tested against its actual user-facing function.

### When asking Claude (Code or otherwise) to change WQ-related, directory-related, or other constraint-bearing code

Begin the request with: "before making changes, read [the relevant constraint document] and confirm the constraints you'll be working within." This forces explicit acknowledgement of prior decisions and reduces accidental regression risk.

If asked to "refactor" or "improve" code that's working, the right starting question is "what constraints does this code currently respect, and which (if any) is the refactor relaxing?" Refactors that quietly relax documented constraints are how regressions land.

### Verification corollary — constraint documents are beliefs, not truth

The Chesterton's fence principle applies to constraints too: before treating a documented constraint as a hard rule, verify that it is still correct. Constraint documents record what was believed at the time of writing — they can be wrong, stale, or based on a flawed diagnostic.

**Working principle:** verify documented constraints against authoritative sources (OpenAPI specs, official documentation, primary API tests) when introducing them, and periodically thereafter. If a constraint was derived from experimentation rather than official documentation, say so — and note what the authoritative source actually says.

**The April 2026 lesson:** three constraints in this file ("date params ignored", "answeringBodies times out", "isAnswered ignored") were all false. They were derived from experiments that used wrong parameter names. The API worked correctly all along. The constraints were our misreading, not the API's behaviour. We spent weeks building workarounds for a problem that didn't exist.

When a constraint and an official spec disagree, trust the spec and test directly. Don't trust prior-Claude's documented belief over a live API response.

---

## After fixing a caching bug — clear the cache

If a bug caused incorrect data to be written to the cache (e.g. wrong date filtering, wrong column), that bad data persists until the TTL expires (6h for searches, 30 days for sessions). After deploying a cache fix, go to `/admin` and clear the relevant cache immediately rather than waiting for TTL.

## Session testing protocol — research tool status tracking

Each coding session that touches the Research Tool must begin by establishing current status and end by confirming it. This prevents the loop where something appears fixed but regresses silently.

### At the start of each session — establish baseline

Before writing any code, ask the user:
1. Which sections are currently working and which are broken?
2. What is the canonical test case you want to verify? (topic, department, expected results)
3. Is the Railway cache clear? (If stale data is possible, clear it at `/admin` before testing)

Record the baseline in the session. Do not assume prior session state carries over.

### Minimum verified checklist — confirm before declaring anything fixed

Run the canonical test case (student loan repayments + DfE, 2026) and verify each section:

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

When testing after a backend change, always state which backend is active so results are interpretable.

### Current invariants — must hold

These behaviours have been verified working and must continue to work. If any regress, check git log for the original fix and ensure recent changes haven't broken the assumption.

- WQ cards show the question text as the question, not the minister's answer
- WQ deduplication by UIN — no duplicate cards
- `is_answered` set correctly based on answer HTML being non-empty after stripping
- Lords oral questions classified correctly via title pattern + word count threshold
- Minister search uses the AI-expanded query, not the raw topic
- WMS department filter is title-match based, not full-text
- Checkboxes present on Oral, WMS, WQ, and Debates sections
- Minister-led search via Hansard backend works for canonical test case (DfE + student loan repayments)

---

## Pre-push checklist

Before every `git push`, run:
```bash
python -c "from flask_app import app; print('OK')"
```
If this fails, do not push. Fix the import error first.

After Railway deploys, verify at `/health` — all services should show `"ok"`.

## Things to avoid
- Don't use port 5432 for Supabase if ever added — use the connection pooler on 6543
- Don't hardcode API keys or .env paths
- Don't use Flask dev server in production (`debug=True` is only active when running locally via `__main__`)
- Don't add a new `ThreadPoolExecutor` without `copy_current_request_context` on every `submit()` call
- Don't name SQLAlchemy columns `query`, `metadata`, or `session`
- Don't leave variables uninitialised before `render_template()` calls
