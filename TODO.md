# Westminster Brief — Task List

## 🔴 Active — Stakeholder Research (Personal Stakeholders)

- [x] **PR 1** — Schema + manual entry + research tab integration (Apr 2026)
  - TrackedStakeholder extended with website, rss_url, description; SSRF protection; 50/user cap
  - "★ My Stakeholders" optgroup in Research tab dropdown; delete button per stakeholder
- [ ] **PR 2** — AI "Look up" button
  - `/stakeholder_lookup` route in `debate_scanner.py` using `_claude_fallback` + `_parse_ai_json`
  - HEAD-validates returned URLs; verified ✓ / unverified ⚠ badges in JS
  - Rate limit: 10 lookups/hour per user (Flask-Limiter)
- [ ] **PR 3** — Website scraping (`fetch_org_website()`)
  - Aggressive RSS autodiscovery across common paths (`/feed`, `/rss`, `/feed.xml` etc.)
  - `trafilatura` for article text extraction when no RSS found
  - Cache per (website_url, topic) for 1 hour

---

## 🔴 Next Priority — AI Briefing Quality

- [ ] **Inline Hansard citations** — hyperlink speaker names/quotes in AI briefing text to source Hansard column; briefing currently has links in speech cards but not in AI summary

---

## 🟡 Written Questions

- [ ] **WQ Search cache** — `CachedWQSearch` model keyed on search params hash; 2–4hr TTL; repeated searches instant for colleagues
- [ ] **WQ pagination** — 1,200 cards in one DOM is slow on DfE machines; server-side paginate (25/50 per page) or client-side virtual scroll
- [ ] **Search-term highlighter** — wrap matched keyword in `<mark>` in question text; improves scannability for long questions
- [ ] **WQ heading click-to-filter** — click a heading badge to re-search by that heading
- [ ] **Merge Tracker into WQ Scanner** — add "Due Today" preset button using `dateForAnswer` API param; auto-detects last sitting day; deprecate Tracker page

---

## 🟠 Word Document / AI Briefing Quality

- [ ] **Cross-party breakdown** — verify SNP, LibDem, Crossbench are consistently surfaced in Opposition Position section, not collapsed into binary govt/opposition
- [ ] **HMG minute structure** — reformat AI briefing sections to match civil service minute: Purpose / Key Facts / Parliamentary Record / Lines to Take / Background
- [ ] **Urgency classification in Word doc** — surface "URGENT QUESTION" badge more prominently in Word doc (classification exists, not visible enough)

---

## 🟢 UX Quick Wins

- [ ] **Demo / sample outputs on homepage** — screenshots or redacted sample Word exports visible before login; major adoption barrier for departments needing IT approval
- [ ] **Debate Prep: Commons mode** — current form only accepts Lords peer names; add toggle for Commons oral questions (departmental QTs, Urgent Questions, Opposition Day)
- [ ] **Result count / source transparency** — surface "N results from X sources" more prominently (currently in debug bar only)
- [ ] **Saved searches / watchlists** — save a search config (topic + dept + date range) and re-run in one click; PQ teams run the same searches weekly
- [ ] **Boolean / operator search** — AND/NOT operators in Hansard search input; reduces noise in AI summaries

---

## 💰 Premium Tier — Features That Justify a Subscription

- [ ] **Real-time PQ & Hansard alerts** — email digest (daily/immediate) when new PQs tabled on keyword or by named MP; needs background scheduler (Railway cron or Celery); single biggest competitive gap vs Dods/DeHavilland
- [ ] **EDM tracker** — Early Day Motions; primary formal backbench signalling mechanism; Parliament EDMs API available
- [ ] **Select Committee evidence tracker** — oral + written evidence; transcripts; report publication alerts
- [ ] **Bills & legislation tracker** — reading stages, amendment text, Lords ping-pong; alert on new stages
- [ ] **Voting / division records** — voting history for any MP/Lord; searchable by Bill, date, or party line
- [ ] **Saved searches with scheduled re-runs** — "send me this search every Monday morning"

### Pricing model (vs Dods/DeHavilland/Vuelio)

| Tier | Price | Target | Key hook |
|---|---|---|---|
| **Civil Servant** | Free (perpetual) | gov.uk verified | Credibility signal + referral pipeline |
| **Individual** | £29–39/month | Freelancers, journalists, academics, SpAds | Under expense-claim threshold |
| **Team (5 users)** | £149–199/month | Small lobbying firms, trade bodies, charities | Replaces one consultant day/month |
| **Enterprise (unlimited)** | £499–799/month | Large firms, corporates, devolved govt | 40–60% below Dods/DeHavilland |

**Realistic near-term target:** Individual + Team tiers. 200 × £35/month = £84k/year.

---

## 🔵 New Feature Areas (Deferred)

- [ ] **Alert / monitoring system** *(critical gap per reviewer)* — same-day email when new PQs tabled on keyword; Parliament API has poll capability; "Smart AI Radar" is a placeholder only
- [ ] **EDM tracking** — who has/hasn't signed an EDM; standard pre-ministerial-appearance prep
- [ ] **Select Committee coverage** — committee reports, oral/written evidence; reviewer: "most substantive scrutiny happens here"
- [ ] **Voting / division records** — TWFY already exposes this via API
- [ ] **Bills tracker** — Bill passage stages; separate API investigation needed
- [ ] **Bulk / historical WQ export** — batch export all WQs to a dept across a full Parliament; current CSV is per-search only
- [ ] **User-facing API** — programmatic access for departmental intranet/SharePoint integration

---

## 🔭 Long-Term — Own Hansard Data Pipeline

Replace all upstream API dependencies with a proprietary ingestion pipeline from Parliament's raw Hansard XML.

- Eliminates TWFY rate limits and API costs at enterprise scale
- Enables semantic/vector search over full Hansard corpus — something Dods/DeHavilland don't offer
- Parliament publishes bulk Hansard XML under Open Parliament Licence (verify commercial terms)
- **Realistic timeline:** 2027+ — after Individual/Team tiers generate revenue to fund infrastructure

---

## 🔐 Pre-launch / Compliance

- [ ] **ICO registration** — required before public launch (~£40/year)
- [ ] **Legal/compliance** — Privacy Policy, T&Cs, GDPR basis, consent checkbox on register, named Data Controller
- [ ] **robots.txt / noindex** — currently blocking all crawlers; remove before public launch
- [ ] **Google Search Console** — verify site, submit sitemap
- [ ] **Department topic suggestions** — data.gov.uk CKAN API to surface clickable topic chips when a dept is selected in WQ Scanner
- [ ] **Audit trail** — log searches run, AI outputs generated, exports; civil servants accountable for briefing content
- [ ] **Sustainability signals** — Cyber Essentials certification; departmental IT approvals teams will ask

---

## ✅ Recently Completed (Apr 2026)

- [x] **Key Ministerial Statements** — `key_ministerial_statements` in AI prompt, `format_briefing_as_text()`, template, and Word doc; verbatim quotes with speaker, role, date, Hansard link; "Suggested lines to take" dropped in favour of this cited approach
- [x] **Access tier gating** — `PAYWALL_ENABLED` / `APPROVED_DOMAINS` / `APPROVED_EMAILS` env vars; gov.uk + approved emails get `civil_servant` tier; all others hit paywall page; bypass with `PAYWALL_ENABLED=false` locally

- [x] **WQ filter consistency** — relevance threshold aligned across WQ Scanner and Hansard Research (2-of-N, not all-words); count message clarified to explain filtered-out questions; "Search all departments" shortcut added
- [x] **BETA badges** — Speech Research and Stakeholder Research tabs labelled as early development
- [x] **WMS migrated to Parliament Questions API** — `fetch_parliament_wms()` replaces TWFY for Written Ministerial Statements under `SEARCH_BACKEND=hansard`; TWFY downgraded to free tier (1,000 calls/month) as fallback
- [x] **Debate cards collapsed by default** — speech lists collapsed on load; minister debates no longer auto-open
- [x] **Hansard API migration** — all phases complete: minister search (Ph1), session expansion (Ph2a), keyword search (Ph2b), secondary features (Ph2c), WMS (Ph2d); all gated on `SEARCH_BACKEND=hansard`; TWFY kept as fallback
- [x] **Claude API fallback** — `_claude_fallback()` wired to briefing generation, query expansion, stakeholder briefing; requires `CLAUDE_API_KEY` env var on Railway
- [x] **Research Tool stability** — MacAlister/Baroness Smith confirmed showing; minister search 403/0-results fixed; Hansard dedup fixed; collapsible speech lists; URL slug fix; WQ timeout banner; Word export IndexError fixed
- [x] **WQ Scanner improvements** — HTML stripping; house filter to API; multi-subject parallel fetch; answer status filter; UIN display; CSV/Word improvements; topic grouping by Parliament heading
- [x] **Debate Prep page** — `/debate_prep` with Lords/Commons toggle; peer profile, media, parliamentary record, Word export
- [x] **Hansard Search UX** — renamed tabs; home page cautious language; login/register in nav; Member Profiles caveat
