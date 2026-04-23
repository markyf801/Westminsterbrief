# Westminster Brief — Project Instructions

## What this project is
Westminster Brief (`westminsterbrief.co.uk`) is an AI-powered parliamentary research tool built for UK government officials. It lets users search Hansard, track Written Questions, analyse debates, and generate Word briefings. Deployed on Railway.

## Stack
- **Backend:** Flask 3.0 with blueprints, deployed on Railway
- **Database:** SQLite locally → PostgreSQL on Railway (auto-switched via `DATABASE_URL` env var)
- **AI:** Google Gemini API (`google-genai`, model: `gemini-1.5-flash` and `gemini-embedding-001`)
- **Frontend:** Jinja2 templates + vanilla JS, static CSS at `static/style.css`
- **Auth:** Flask-Login with werkzeug password hashing
- **Exports:** python-docx for Word document generation

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

## Known issues / tech debt
- Written Questions search can be slow — Parliament API latency, no caching yet
- The `SECRET_KEY` in flask_app.py is a placeholder — must be overridden by env var on Railway
- Backup files in root (bckup_flask.py etc.) and backup templates are clutter — safe to delete eventually
- No database migration system — relies on `db.create_all()` which is fine for now

## Next priority: minister-led search for selected department

**The problem (affects ALL departments, not just DfE):**
Keyword search misses ministers whose speeches don't contain the search terms. Phase 1 (full session fetch) helps but only when the session was already found. If no speech in a session contains the keywords, the session is invisible.

**The proper fix:**
When a department is selected, pull the ENTIRE ministerial team from GOV.UK, search each minister's debates via TWFY person search, merge with keyword search.

**Critical insight — portfolio doesn't matter:**
In Oral Questions, whoever is at the dispatch box answers — regardless of their specific portfolio.
MacAlister is "Minister for Children and Families" but answers ANY DfE question when present.
Baroness Smith covers ALL DfE business in the Lords, not just Skills.
DO NOT filter minister search by portfolio area. Search ALL ministers in the selected department.

This is universal across departments:
- Treasury: Reeves + all junior ministers may answer any Treasury question
- Home Office: Cooper + juniors may answer any HO question
- DfE: Phillipson, MacAlister, McKinnell, Morgan, Daby, Baroness Smith — any may answer repayments

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

### Who the user is
- UK civil servant working in higher education policy, writing parliamentary briefings professionally
- Knows Parliament well from the inside — knows which ministers spoke, which debates happened, which questions were tabled
- If the tool misses something they know happened, the tool is wrong — trust their domain knowledge
- Building this as a tool they and colleagues across government would use; thinks about it from a practitioner's perspective

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

### Parliamentary debate types — full reference

This is the complete taxonomy used for classifying TWFY results. Classification is two-tier: `get_debate_type()` assigns a display label; `_classify_group()` assigns a section bucket for rendering.

#### TWFY source types (API-level)
| Source code | What it is | Endpoint |
|---|---|---|
| `commons` | House of Commons chamber debates | `getDebates` |
| `westminsterhall` | Westminster Hall debates | `getDebates` |
| `lords` | House of Lords chamber debates | `getDebates` |
| `wrans` | Written Answers to Questions | `getWrans` |
| `wms` | Written Ministerial Statements | `getWMS` |

#### Debate type classification — structural signatures

Each type has a predictable structure in TWFY data. Use these to tune both detection and display:

**🗣️ Oral Questions (Commons or Lords)**
- Title pattern: `"Oral Answers to Questions — [Department]"` or `"[Department] Questions"`
- Structure: Short question (~50 words) → minister answer (~150 words) → supplementaries (~50–100 words each)
- Word count heuristic: max speech in group < 300 words → likely Oral Questions
- PMQs title pattern: `"Prime Minister — Questions"` or `"Oral Answers to the Prime Minister"`
- Lords oral questions: shorter, less structured, title often `"[Topic] — Oral Questions"`
- Detection rule: word-count heuristic applies **Commons only** — Lords oral questions are short too but structured differently

**❗ Urgent Questions**
- Title pattern: `"[Topic] — Urgent Question"` or `"Urgent Question — [Minister name]"`
- Structure: Short question statement (~100 words) → minister statement (~500 words) → rapid supplementaries
- Rarer — speaker's discretion, maximum 20 mins, granted without notice
- Often follows a news event — high relevance for policy monitoring

**📜 Ministerial Statement**
- Source `wms` OR title contains `"statement"`
- Structure: Single long minister speech (~800–1500 words) → supplementaries from MPs/peers
- Commons statements usually follow PMQs or urgent questions on same sitting day
- Lords statements are separate sessions, titled `"[Topic] — Statement"`
- Key difference from debates: minister controls the floor for the opening statement

**💬 General Debate / Backbench Business**
- Title patterns: `"[Topic]"` (bare), `"[Topic] — Motion"`, `"Backbench Business — [Topic]"`
- Structure: Multiple speeches 5–20 mins each (~600–2500 words), minister responds at the end
- End-of-day adjournment debates: one backbencher raises a topic, minister responds, ~30 mins total
- Usually lower relevance for policy monitoring unless minister's closing speech is captured

**🏛️ Westminster Hall**
- All debates from this source are Westminster Hall
- Structure: Backbencher opens (~15 mins), other MPs speak, minister responds (~10 mins)
- Adjournment debates: single MP raises constituency/policy issue, minister responds — very short sessions
- Title often: `"[Topic] — Westminster Hall"` or just `"[Topic]"`
- Important for monitoring: Westminster Hall debates frequently cover niche policy areas not debated in the chamber

**⚖️ Statutory Instrument / Delegated Legislation**
- Title patterns: `"draft [X] regulations"`, `"[X] order [year]"`, `"affirmative resolution"`, `"delegated legislation"`, `"statutory instrument"`
- Structure: Short minister opening (~300 words) → brief contributions → division or formal approval
- Lords often has more substantive SI debates than Commons
- Detection is title-only — word counts are unreliable (some SIs are hotly contested, some are nodded through)

**⚖️ Legislation (Bills)**
- Title patterns: `"[Bill name] — [reading]"`, `"second reading"`, `"committee stage"`, `"report stage"`, `"third reading"`, `"Lords amendments"`
- Structure varies enormously by stage — Second Reading is set-piece speeches; Committee is clause-by-clause
- High word counts, many speakers, long sessions

**✍️ Written Answer (via Lords TWFY)**
- Source `wrans` OR title contains `"Written Answers"` (common in Lords records)
- Structure: Single question → single minister answer, no supplementaries
- Lords written answers come through the `lords` TWFY source with title `"Written Answers — [Dept]: [Topic]"` — this is NOT an oral debate
- Critical: these must NOT be classified as Oral Questions even though they appear in the lords source

**📝 Motion**
- Title contains `"motion"` — e.g. `"Opposition Day Motion"`, `"Take Note Motion"` (Lords), `"humble address"`
- Lords Take Note motions: government introduces a topic for discussion without a vote — common for policy areas
- Often high-quality debate content — multiple long speeches from experienced peers

#### Word count guide for classification
| Speech length | Likely type |
|---|---|
| < 100 words | Supplementary oral question or brief intervention |
| 100–300 words | Oral PQ minister answer, or brief Lords oral answer |
| 300–800 words | Urgent Question response, short ministerial statement, Westminster Hall contribution |
| 800–1500 words | Full ministerial statement, main debate speech |
| 1500+ words | Major debate speech, Second Reading, Budget statement |

#### Title patterns to add if detection improves in future
These debate types currently fall through to `💬 General Debate` but could be classified more precisely:
- `"take note"` → Lords debate (low urgency)
- `"adjournment"` → End-of-day adjournment debate (one MP + one minister)
- `"opposition day"` → Opposition-led debate
- `"backbench business"` → Backbench Business Committee debate
- `"estimates day"` → Estimates debate (spending scrutiny)
- `"ten minute rule"` → Ten-minute rule bill introduction
- `"private member"` → Private Member's Bill

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

## ⚠️ Pre-launch checklist — complete BEFORE promoting on Google / wider marketing

The site currently has `noindex, nofollow` in base.html meta tags and robots.txt blocks crawlers. Before removing these and going public, the following MUST be completed:

### Legal / compliance
- [ ] **ICO registration** — collecting email addresses from UK users requires ICO registration (~£40/year, ~10 mins at ico.org.uk). Not yet done. Must be done before public launch.
- [ ] **Named Data Controller** — Privacy Policy and Terms must name a real person or company as the Data Controller, not just "Westminster Brief and its operators". User to supply company name + contact email.
- [ ] **Legal basis for processing** — Privacy Policy must state Article 6 basis (likely "performance of a contract")
- [ ] **Right to complain to ICO** — must be added to Privacy Policy (UK GDPR requirement)
- [ ] **Register consent checkbox** — registration form needs explicit T&C + Privacy Policy acceptance checkbox
- [ ] **ICO registration number** — add to Privacy Policy footer once registered
- [ ] **Governing law clause** — add "governed by laws of England and Wales" to Terms

### Technical / SEO
- [ ] **robots.txt** — currently blocks all crawlers. Update to allow Googlebot when ready.
- [ ] **Remove noindex** — `<meta name="robots" content="noindex, nofollow">` in base.html needs removing or conditional logic
- [ ] **Google Search Console** — verify site and submit sitemap
- [ ] **sitemap.xml** — create and register with Google

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

### Known good baseline (as of 2026-04)
The fixes applied in the April 2026 sessions resolved:
- WQ cards showing minister answer as the question (TWFY wrans removed)
- Duplicate WQ cards (UIN deduplication added)
- `is_answered` incorrectly false (answer HTML stripping fixed)
- Lords oral questions not classified correctly (title pattern + word count threshold)
- Minister search using wrong query (now passes `expanded` not raw `topic`)
- WMS dept filter too aggressive (now title-match based)
- Checkboxes missing from Oral/WMS/WQ sections (added)

If any of these regress, check the git log for the relevant commit.

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
