# Westminster Brief — Content Roadmap

This document captures editorial content ideas, drafts, and assignments 
for non-tool pages on the Westminster Brief site. It complements the 
product spec doc (which covers tool features and data architecture) by 
focusing specifically on:

- Static informational pages (history, curiosities, explainers)
- Content that builds Westminster Brief's editorial character
- SEO-rankable long-form content
- Educational material for civil service / policy / engaged-citizen audiences

The product spec doc remains the single source of truth for tools, 
features, and data architecture. This doc is purely for *what's written* 
on those static pages.

## How to use this doc

**For Mark**: capture content ideas at the bottom under "Backlog." When 
ready to commission a page, draft the content under "Drafted/Ready" 
with target route, intended audience, and any specific notes for Code.

**For Code**: build pages from the "Drafted/Ready" section in priority 
order. Each entry includes proposed route, page structure, and content. 
Treat content as the authoritative draft — preserve facts and tone 
unless flagged otherwise.

**For Opus / future me**: when content ideas surface during conversations, 
this is where they go. Don't stuff them into the product spec doc.

---

## Drafted/Ready

### 1. The History of Hansard

**Status**: Content drafted (3 May 2026)
**Proposed route**: `/about/history-of-hansard` or `/history-of-hansard`
**Proposed nav location**: footer link initially; consider header nav 
if usage is high
**Estimated build effort**: 1-2 hours
**Priority**: Medium — Phase 2A.5+ enhancement

**Audience**: Civil service colleagues, journalists, researchers, engaged 
citizens. Treats reader as informed but not necessarily expert in 
parliamentary history.

**Tone**: factual, slightly dry, respectful of institutional substance. 
Avoid whimsy; aim for the voice of an authoritative reference source.

**Structure**:
- H1: The History of Hansard
- H2 sections covering: pre-Hansard secrecy, Cobbett's radical foundation, 
  the Hansard family era, Stockdale v Hansard and 1840 Act, the 1909 
  switch to official record, the 1893 verbatim definition, modern Hansard
- Closing paragraph linking to Westminster Brief's use of Hansard
- Sources/further reading list at bottom

**SEO considerations**:
- Unique title tag: "The History of Hansard | Westminster Brief"
- Meta description: ~150 chars summarising the page
- Canonical URL set
- Schema markup for Article type if applicable
- Internal links to relevant /archive pages

**Full content**: [paste in the 1000-word draft from session 3 May 2026 
when ready to build]

---

### 2. [Next ready content goes here]

---

## Backlog (ideas — not yet drafted)

### A. Parliamentary Curiosities

**Concept**: A single long page (or eventually a hub of articles) 
collecting interesting procedural, ceremonial, lexical, and origin-story 
elements of UK Parliament. Builds Westminster Brief's character as a 
site that genuinely understands Parliament rather than just scraping its 
data.

**Target audience**: Civil service colleagues, public affairs 
professionals, journalists, engaged citizens. Same audience as the main 
product but with a slightly broader curiosity radius.

**Proposed structure**: Single page with H2 sections by category. v1 with 
8-10 entries; grow organically over time.

**Categories and candidate entries**:

*Ceremony and physical tradition*
- Black Rod and the door-slamming at State Opening
- The Mace and what happens if it's not present
- Acts of Parliament on vellum (1849-2017)
- The two red lines on the Commons floor (sword's-length apart)
- The Speaker's chair and procedural artifacts

*Procedural quirks*
- "I refer the Honourable Member to my answer of [date]"
- Points of order — what they actually do
- Praying against statutory instruments
- Crossing the floor (literal and political)
- Why MPs nod and shake heads instead of clapping

*Lexical and linguistic curiosities*
- "Honourable Members" / "Right Honourable" — Privy Counsellor distinction
- Hansard stock phrases and their meaning ("in due course," "considering," 
  "actively considering," "keeping under review")
- Constituency naming during debates
- "Take note" motions vs substantive motions
- The Norman French formula phrases ("La Reyne le veult," etc.)

*Origins and history*
- "Big Ben" — why it's the bell, not the clock or tower
- Why we have two Houses
- The 1605 Gunpowder Plot legacy (cellar searches before State Opening)
- The 1834 fire and what was lost
- Why the Commons chamber is too small (Churchill's argument)

*Modern Whitehall folklore*
- Stock-phrase drift in ministerial responses
- "We are committed to" vs "We will" vs "We intend to"
- "Welcoming" vs "noting" stakeholder contributions
- White Paper vs Green Paper
- What APPGs actually do

**Risks to manage**:
1. Don't be twee — informative, slightly dry, respectful of substance
2. Verify everything — fact-check via authoritative sources, cite them
3. Politically neutral — about the institution, not partisan content

**Estimated build effort**: 1-2 days for v1 page with 8-10 entries

---

### B. About Westminster Brief / Methodology

**Concept**: A clear page explaining what Westminster Brief is, how it 
works, who built it, and what its business model is. Likely needed when 
colleagues ask "what's the catch?" — better to have a clear answer 
published than be surprised.

**Topics to cover**:
- Who built this and why (Mark, civil service, personal capacity)
- Free during beta, future paid features may exist but core archive 
  remains free
- Data sources used (Hansard, Parliament APIs, etc.)
- Limitations clearly stated (12-month archive window, current scope)
- AI usage and verification approach
- Privacy and propriety stance (no contact details, no devolved coverage 
  yet, etc.)

**Estimated build effort**: 0.5 day

---

### C. Glossary of UK Parliamentary terms

**Concept**: Plain-English glossary of terms colleagues might encounter — 
particularly useful for newer civil servants or non-specialists. Could be 
linked from anywhere in the site as a hover or on-page reference.

**Examples**:
- PMQs, MQs, oral questions, written questions
- Whips, three-line whips
- Money Bill, Hybrid Bill
- Order Paper, Order of Business
- Standing Order 24, urgent question
- All-Party Parliamentary Group (APPG)
- Select Committee vs Public Bill Committee
- Statutory Instrument, Order in Council

**Estimated build effort**: 1 day

---

### D. How to engage with Parliament — practical guides

**Concept**: Short practical guides aimed at organisations new to 
parliamentary engagement. Charities, small think tanks, advocacy groups, 
or industry bodies who'd benefit from "how to" content.

**Candidate guides**:
- How to respond to a parliamentary consultation
- How to brief an MP for a debate
- How to prepare for a select committee evidence session
- How to use Westminster Brief for engagement preparation
- What APPGs are and how to engage with them
- The difference between writing to your MP vs writing to a minister

**Tone**: practical, plain-language, civil-service-appropriate. Not 
political — process-focused.

**Estimated build effort**: 1-2 days for initial set of 4-5 guides

---

### E. [Future ideas live here]

---

## Notes for editorial discipline

When drafting content for this doc, three principles:

1. **Verify before publishing.** All facts double-sourced from authoritative 
   sources (parliament.uk, House of Commons Library, Britannica, established 
   reference works). Citation discipline matters even on static pages.

2. **Preserve neutral tone.** No partisan content. Westminster Brief is 
   politically neutral as a tool; its editorial content should reflect that.

3. **Audience-aware.** Civil service colleagues won't tolerate fluff. 
   Write to the level of someone informed but maybe new to the specific 
   topic. Slightly dry is better than slightly twee.
