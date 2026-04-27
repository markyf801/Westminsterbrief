# Westminster Brief — Session Handover

Last updated: 25 April 2026

This document captures current state of the project so a fresh Claude session can pick up with full context. Paste a short summary into a new chat, then point to this file (or paste the relevant sections) for detail.

---

## Quick context paste (for new sessions)

> I'm Mark, building Westminster Brief — a parliamentary research and stakeholder intelligence tool for UK policy professionals. I'm a civil servant doing this in my own time. The project is at westminsterbrief.co.uk, deployed on Railway, running Flask with SQLite locally and Postgres in production.
>
> Current state: directory build is complete (3 ingesters, 3,400+ orgs, working search UI). Strategic pivot is to polish the existing tools and launch a public beta rather than keep building features. Several next steps are documented in `docs/`.
>
> The full handover is in `docs/session-handover.md`. Authoritative design docs are in `docs/design-principles.md`, `docs/dashboard-roadmap.md`, `docs/inquiry-tracking-design.md`, and `docs/stakeholder-directory-design.md`. CLAUDE.md has the project's working principles.
>
> What I'd like to work on now: [whatever the actual task is]

---

## Where the project actually is

### Built and working

- **Westminster Brief tool surfaces**: Hansard search, Written Questions scanner, MP/peer profiles, Debate Prep, Stakeholder Research (personal stakeholders)
- **Stakeholder Directory** — auto-populated from public sources:
  - Three ingesters complete: ministerial meetings, committee evidence, lobbying register
  - 3,400+ organisations, 5,500+ engagements, 116 distinct inquiries tracked
  - Searchable via `/directory` with subject context, inquiry status, clickable inquiry titles
  - Audit functions verifying data integrity
  - Idempotent re-ingestion
- **Hansard migration** fully complete — TWFY no longer used for any user-facing feature. Primary search, WMS, session expansion, and MP speeches tab all run through Parliament's official Hansard API. TWFY fallback code paths remain in `debate_scanner.py` but are never reached when `SEARCH_BACKEND=hansard`.

### Pending decisions / not yet built

- **Public beta launch** — strategic pivot agreed but not yet executed. Includes:
  - Visual redesign of landing page (planned via Claude Design + Claude Code)
  - Removing civil-service-only gating
  - Setting up Plausible analytics
  - Privacy policy, T&Cs, ICO registration
  - Robots.txt + noindex tag removal
- **Dashboard rebuild** — paused, full plan in `docs/dashboard-roadmap.md`
- **Inquiry tracking feature** — paused, full plan in `docs/inquiry-tracking-design.md`
- **Enrichment pass** — directory has raw data but no website-derived descriptions, policy area tags, or minister-portfolio inference yet
- **Tier 3 flag review** — 442 flags accumulated across the directory, need a focused review session to clear

### Documents to refer to

| File | Purpose |
|---|---|
| `CLAUDE.md` | Working principles for Claude Code: who Mark is, what the project is, output rules, positioning principles, audience |
| `docs/stakeholder-directory-design.md` | Full design spec for the directory module |
| `docs/dashboard-roadmap.md` | Phased plan for dashboard rebuild |
| `docs/inquiry-tracking-design.md` | Plan for inquiry tracking feature (deferred) |
| `docs/design-principles.md` | Visual and copy guidance for redesign work |
| `docs/parliamentary-debate-types.md` | Reference: Hansard taxonomy |
| `docs/pre-launch-checklist.md` | Compliance and technical pre-launch items |

---

## Strategic frame

**What Westminster Brief is:** parliamentary research and stakeholder intelligence tool for UK policy professionals. Built by a civil servant. Free during beta.

**Audience:** civil servants, charity policy officers, public affairs professionals, academic researchers, journalists. Primary user is policy professionals doing recurring research.

**What it isn't:** a managed service, a Dods/DeHavilland competitor, a CRM, an enterprise tool, an AI tool that drafts content for civil servants.

**Output rule:** factual or extracted, never authored. Tool surfaces evidence; doesn't write content civil servants would be accountable for.

**Pricing model (post-beta):** free for gov.uk users, £29–39/month Individual, £149–199/month Team. 50% charity/academic discount. No Enterprise tier.

**Realistic revenue target:** £30–150k/year. Achievable but requires distribution work, not just building. 6–12 months to first meaningful revenue.

---

## Outstanding non-engineering tasks

These are the things that aren't code prompts but matter for the project's future:

1. **Civil service declaration** — proactive disclosure to line manager / DD that I'm building Westminster Brief in my own time. Frame as declaration, not a question. Process: verbal heads-up → written summary → formal disclosure form via HR. Standard departmental process. Should happen before any public-facing launch activity (Bluesky brand account, LinkedIn role, public beta).

2. **Three tester conversations** — book 15-20 minutes each with three DfE testers. Watch them use the directory. Take notes immediately afterwards. Listen for "can it do X?" questions. Schedule rather than letting drift.

3. **Domain-specific reading** — keep up with what's happening in adjacent UK policy tools and parliamentary tech space. Quiet ongoing distribution work.

4. **Plausible Analytics setup** — £9/month, GDPR-friendly, no cookie banner needed. Set up before public beta launch.

---

## Where to pick up

When you come back, the most defensible next moves in priority order:

1. **Book tester conversations** (one hour of organising, three short conversations next week)
2. **Draft / start the manager declaration conversation** (15 minutes of preparation, the conversation itself takes 10–15 minutes)
3. **Visual redesign of landing page via Claude Design + Claude Code** (using `docs/design-principles.md` as authoritative brief)
4. **Public beta launch readiness work**: privacy policy, T&Cs, ICO registration, Plausible setup, remove noindex
5. **Tier 3 flag review session** when you've got an evening for unglamorous data hygiene
6. **Continue building features** (inquiry tracking, dashboard, enrichment) — but only after tester feedback informs which feature matters most

The trap to avoid is "build more before validating less." Most solo SaaS dies of feature accumulation without user input.

---

## Recently completed (this session)

For continuity if returning soon:

- Built and verified Prompts 1–8 (directory schema, scoring, ministerial meetings ingester, normalisation with dedup tiers, audit + end-to-end script, committee evidence ingester, lobbying register ingester, search/UI prototype)
- Fixed broken Parliament 404 URLs (committee evidence) and gov.uk URLs (ministerial meetings)
- Added engagement subject context (inquiry title / meeting purpose / lobbying subject) to org detail pages
- Added inquiry status visibility (open/closed/reported) and clickable inquiry titles linking to Parliament committee pages
- Created design documents: dashboard roadmap, inquiry tracking design, design principles
- Established strategic pivot: polish + public beta rather than keep building features

---

## Open considerations to track

These aren't blocking but are worth holding in mind:

- **Concurrent normaliser runs against SQLite produce duplicates.** Always run sequentially. Resolved when production migrates to Postgres on Railway.
- **Source data corrections.** If gov.uk republishes corrected data for an earlier quarter, current unique constraints would silently reject the correction. Not a current issue, but worth fixing before quarterly re-ingestion runs at scale.
- **FairGo CIC pattern.** Individual researchers using a CIC name as a vehicle for personal submissions get inflated engagement counts. Enrichment pass should detect and reclassify as `individual_expert`.
- **442 Tier 3 flags accumulated.** Review session needed before more sources land on top.
- **Some lobbying register subject_matters vague or empty.** Authentic to source, not a bug, but worth knowing testers may comment.
