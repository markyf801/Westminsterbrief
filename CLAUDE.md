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
```

## Local development
```bash
cd c:\Users\marky\hansard_app
pip install -r requirements.txt
python flask_app.py
```
App runs at http://127.0.0.1:5000 — visit /home for the dashboard.

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

## Things to avoid
- Don't use port 5432 for Supabase if ever added — use the connection pooler on 6543
- Don't hardcode API keys or .env paths
- Don't use Flask dev server in production (`debug=True` is only active when running locally via `__main__`)
