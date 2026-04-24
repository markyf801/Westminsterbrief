# Westminster Brief — Task List

## 🔴 Current Priority — Written Questions

- [ ] **WQ Search cache** — add `CachedWQSearch` model keyed on search params hash
  - Currently individual questions are cached by UIN but search results are never served from cache
  - 2–4hr TTL; would make repeated searches instant for colleagues

- [ ] **WQ pagination** — 1,200 cards in one DOM is slow on DfE machines
  - Server-side paginate (25/50 per page) or client-side virtual scroll

- [ ] **Search-term highlighter** — wrap matched keyword in `<mark>` in question text
  - Transforms scannability when a question is long and term appears once deep in text

- [ ] **WQ heading click-to-filter** — click a heading badge to re-search by that heading

## 🟡 Next — Hansard API Migration (TWFY elimination)

- [ ] **Hansard API Phase 2** — migrate keyword search + session expansion from TWFY to Hansard Parliament API
  - **PoC complete (Apr 2026) — all endpoints verified working:**
    - `/search/debates.json` → keyword search, returns `DebateSectionExtId`, supports date/house/debateType filters
    - `/debates/debate/{ExtId}.json` → session expansion, returns all speeches with Parliament member IDs
    - `/search/contributions.json` → speech-level search (closer to TWFY behaviour — investigate as primary search path)
    - Lords, Commons, Westminster Hall, WMS all covered
    - URLs constructable: `hansard.parliament.uk/{house}/{date}/debates/{ExtId}/{slug}`
    - Note: WMS may live at a separate endpoint (`/search/statements.json`) — confirm before migrating
    - Note: `student loan` works, `student loan repayments` returns 0 — shorter terms needed (AI query expansion handles this)
  - **Phase 1** (minister search) done and deployed
  - **Phase 2a** (session expansion via Hansard API) just shipped — `fetch_full_hansard_session()` deployed, routes by `debate_section_ext_id`; awaiting log confirmation
  - **Phase 2b** (keyword search migration): replace `fetch_twfy_topic()` with `fetch_hansard_contributions()`
  - **Goal: eliminate TWFY API key cost**

  ### Architecture — Opus recommendations (Apr 2026)
  Do these in order; each is a reviewable increment:

  1. **Verify JSON shape first** — curl `/debates/debate/{EXTID}.json` for a known 2026 DfE session; confirm `Items[]` carries speeches, `AttributedTo` has speaker+party, `MemberId` is present, `ChildDebates` works for nested sessions (Oral Questions). Paste `Items[0]` before writing code.
  2. **Scaffold `hansard_client.py`** (thin API client, separate from `debate_scanner.py`):
     - `search_contributions(query, start_date, end_date, house)` → speech-level hits with ExtIds
     - `fetch_debate(ext_id)` → all speeches in a session
     - `parse_attributed_to(raw_string)` → `(name, party_abbrev, role)` with unit tests for: plain MP, peer, minister with role prefix, Mr Speaker (no party), Deputy Speaker, Independent
  3. **Member ID hydration** — use `MemberId` from Items to call Parliament Members API once per unique member; cache aggressively; use `parse_attributed_to` only as fallback when `MemberId` missing (officers, Speaker). This replaces `_normalise_party` and is more robust.
  4. **Date filter on search call** — push `startDate`/`endDate` into API params, never post-hoc
  5. **Keep TWFY live in parallel** under `SEARCH_BACKEND` flag for 1–2 weeks; diff results; delete TWFY only when Hansard matches or beats it for a week
  6. **WMS separate endpoint** — check `/search/statements.json`; may need its own module
  7. **Rate limit strategy** — cache sessions by ExtId (indefinite TTL for sessions > 7 days old, 7-day TTL for recent); parallelise with max 6 workers
  8. **Architectural split** (later, not now): `hansard_client.py` (fetch only) + pipeline layer + route/view; migration is the cheapest moment to do this while the fetch path is being rewritten anyway

## Recently Completed

- [x] **WQ Scanner improvements** (Opus review Apr 2026)
  - HTML stripping (`strip_html()` using regex + `html.unescape`)
  - House filter pushed to Parliament API
  - Multi-subject searches parallelised
  - `is_answered` whitespace fix
  - Answer status filter (All / Unanswered / Answered / Holding / Withdrawn)
  - UIN displayed on each card
  - CSV: UTF-8 BOM, dated filenames, search metadata header, UIN column
  - Word: search metadata block, UIN in question lines, dated filename
  - Count display shows retrieved vs API total separately
- [x] **WQ topic grouping** — group results by Parliament heading, flat/grouped toggle
- [x] **Hansard API Phase 1** — minister search via Hansard API (`fetch_hansard_minister_topic`)
- [x] **Debate Prep page** — new `/debate_prep` route with Lords/Commons toggle
- [x] **Hansard Search UX** — renamed from "Parliamentary Research Tool"
- [x] **Home page** — login/register in hero, cautious language, Smart AI Radar demoted
- [x] **Member Profiles** — Wikipedia caveat + interests timestamp
- [x] **Nav** — login/register links for unauthenticated users, feedback link in footer

## 🟠 Word Document / AI Briefing Quality (External Review Apr 2026)

- [ ] **Cross-party breakdown in AI prompt** — AI prompt already asks for opposition parties by name; verify SNP, LibDem, Crossbench are consistently surfaced in the Word doc's Opposition Position section, not collapsed into a binary govt/opposition view
- [ ] **HMG minute structure** — reformat AI briefing sections to match standard civil service minute: Purpose / Key Facts / Parliamentary Record / Lines to Take / Background
- [ ] **Suggested lines to take (AI draft)** — add a new `lines_to_take` field to the AI prompt: 3–5 draft holding lines clearly labelled as AI draft requiring official clearance; this is the highest-value output civil servants currently have to write manually
- [ ] **Urgency classification surfaced in Word doc** — debate_type classification already exists; surface it more prominently in the Word doc (e.g. "URGENT QUESTION" badge on relevant sections, not just a metadata field)

## 🟠 Reliability & AI

- [ ] **Claude API fallback for Gemini** — when Gemini returns 503/fails, silently retry with `claude-haiku-4-5`; affects briefing generation, `expand_search_query()`, stakeholder briefing; see plan file for implementation detail
  - Reviewer flagged single-provider as a reliability risk for time-pressured users

- [ ] **Inline Hansard citations in AI briefing** — hyperlink every speaker name / quote in the AI briefing output to the source Hansard column; reduces verification burden; briefing currently has links in the speech cards but not in the AI summary text

## 🟢 UX Quick Wins

- [ ] **Demo / sample outputs on homepage** — add screenshots or redacted sample Word exports visible before login; major adoption barrier in departments needing IT approval; no code change, just content
  - Reviewer: "every functional tool redirects to a login screen — no sample outputs to assess quality"

- [ ] **Debate Prep: add Commons mode** — current form only accepts "Peer name" (Lords OPQs); add toggle for Commons oral questions so civil servants can prep for departmental Question Times, Urgent Questions, and Opposition Day debates
  - Reviewer flagged this as a significant gap: Commons oral question prep is the higher-pressure workload

- [ ] **Result count / source transparency** — surface the "N results from X sources" info more prominently in the UI (it's in the debug bar but not visible to users); helps users assess AI briefing confidence

- [ ] **Saved searches / watchlists** — allow users to save a search config (topic + dept + date range) and re-run it in one click; PQ teams run the same searches weekly

- [ ] **Boolean / operator search** — support phrase exclusion and at minimum AND/NOT operators in the Hansard search input; currently any ambiguous keyword generates noisy AI summaries

## 💰 Premium Tier — Features That Justify a Subscription

These are the features Dods Monitoring / DeHavilland charge £10k–£50k/year for. Adding them moves Westminster Brief from "free supplement" to "competitive paid product". Target: departmental procurement rather than individual civil servants.

- [ ] **Real-time PQ & Hansard alerts** — the single biggest gap; departments currently pay Dods/DeHavilland primarily for this; email digest (daily/immediate) when new PQs tabled on keyword or by named MP; needs background scheduler (Railway cron job or Celery)
- [ ] **EDM tracker** — see New Feature Areas below; EDM monitoring is standard in paid services
- [ ] **Select Committee evidence tracker** — oral + written evidence; transcripts; report publication alerts
- [ ] **Bills & legislation tracker** — reading stages, amendment text, Lords ping-pong; alert on new stages
- [ ] **Voting / division records** — voting history for any MP/Lord; searchable by Bill, date, or party line
- [ ] **Saved searches with scheduled re-runs** — "send me this search every Monday morning"; makes the tool sticky and justifies a subscription model

### Pricing model (based on competitive analysis vs Dods/DeHavilland/Vuelio)

| Tier | Price | Target | Key hook |
|---|---|---|---|
| **Civil Servant** | Free (perpetual) | gov.uk verified | Credibility signal + referral pipeline into paid tiers |
| **Individual** | £29–39/month | Freelancers, journalists, academics, SpAds between roles | Under expense-claim threshold; ~£400/year |
| **Team (5 users)** | £149–199/month | Small lobbying firms, trade bodies, small charities | Replaces one consultant day/month; ~£1,800–2,400/year |
| **Enterprise (unlimited)** | £499–799/month | Large firms, corporates, devolved govt, large charities | 40–60% below Dods/DeHavilland at ~£6,000–9,600/year |

**Caveats before Enterprise tier:**
- Sole operator setup limits credibility at £6k+/year — need company, DPA, Cyber Essentials
- TWFY API cost scales with Enterprise volume — review mySociety commercial terms before pricing Enterprise
- Free tier must stay genuinely full-featured — degrading it collapses the credibility advantage

**Realistic near-term target:** Individual + Team tiers. 200 × £35/month = £84k/year — viable at solo scale before Enterprise infrastructure is built.

---

## 🔵 New Feature Areas (Deferred)

- [ ] **Alert / monitoring system** *(critical gap per reviewer)* — email notifications when new PQs are tabled on a keyword or by a specific MP; same-day notification is what PQ handling teams actually need; Parliament API has webhook/poll capability; currently the "Smart AI Radar" placeholder but no implementation
  - Reviewer: "the single most significant gap"

- [ ] **EDM (Early Day Motion) tracking** — EDMs are the primary formal backbench signalling mechanism; knowing who has/hasn't signed an EDM on a topic is standard pre-ministerial-appearance prep; Parliament EDMs API is publicly available

- [ ] **Select Committee coverage** — committee reports, oral evidence transcripts, written evidence submissions; Parliament API available; reviewer: "Select Committees are where the most substantive scrutiny happens"

- [ ] **Voting / division records** — how an MP voted on key legislation; TWFY already exposes this via API; reviewer noted the omission as "surprising" given TWFY is already a data source

- [ ] **Bills tracker** — Bill passage stages (readings, amendments, committee, Lords ping-pong); separate API investigation needed

- [ ] **Bulk / historical WQ export** — batch export all WQs to a dept across a full Parliament for historical analysis or FOI preparation; current CSV export is per-search-result only

- [ ] **User-facing API** — programmatic access to AI synthesis layer for departmental intranet/SharePoint integration; larger departments want this for workflow tools

## 🔭 Long-Term Infrastructure Goal — Own Hansard Data Pipeline

**Goal:** Replace all upstream API dependencies (TWFY, Parliament Hansard API) with a proprietary ingestion pipeline built directly from Parliament's raw Hansard XML. Serve parliamentary data through Westminster Brief's own search index.

**Why this matters:**
- Eliminates TWFY rate limits and mySociety commercial API costs at enterprise scale
- Removes dependency on Parliament API uptime and schema changes
- Enables semantic / vector search over full Hansard corpus — something Dods/DeHavilland don't offer
- Makes the enterprise-tier **user-facing API** (£499–799/month) genuinely proprietary and defensible

**What it involves:**
- Parliament publishes bulk Hansard XML under Open Parliament Licence (verify commercial use terms)
- Daily ingestion pipeline: download XML → parse → store in PostgreSQL → update search index
- Full-text search (PostgreSQL FTS or Elasticsearch) + vector embeddings for semantic search
- Scheduler (Railway cron or Celery) to keep corpus current

**Order of operations:**
1. Phase 2 Hansard API migration first (uses Parliament's API — cheap, fast, no XML parsing)
2. Build own pipeline only when: hitting rate limits at scale, OR enterprise API tier is ready to launch
3. Vector search layer (semantic similarity over speeches) as the premium differentiator

**Realistic timeline:** 2027+ — after Individual/Team tiers are generating revenue to fund the infrastructure.

---

## 🔐 Pre-launch / Compliance

- [ ] **Merge Tracker into WQ Scanner** — add "Due Today" preset button using `dateForAnswer` API param (not tabled date); auto-detects last sitting day to handle weekends/recess; drops AI categorisation (Parliament headings cover it); Tracker page deprecated
- [ ] **Department topic suggestions** — use data.gov.uk dataset catalogue (CKAN API) to surface clickable topic chips when a department is selected in WQ Scanner; pre-populate search box from department's published dataset tags/themes
- [ ] **ICO registration** — required before public launch (~£40/year)
- [ ] **Legal/compliance** — Privacy Policy, T&Cs, GDPR basis, consent checkbox on register
- [ ] **robots.txt / noindex** — currently blocking all crawlers; remove before public launch
- [ ] **Google Search Console** — verify site, submit sitemap
- [ ] **Audit trail** — log what searches were run, what AI outputs generated, what was exported; civil servants are accountable for briefing content; relevant for FOI/parliamentary accountability
- [ ] **Sustainability signals** — replace Gmail contact with a proper domain email; consider Cyber Essentials certification; departmental IT approvals teams will ask about long-term maintenance
