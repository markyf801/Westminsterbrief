# Party Pages Tier 3 — Manifesto Integration: Design Document

Captured: 18 May 2026. Design-only phase — no implementation until post-Teams-share.

---

## Context

Tier 1 party pages are live on beta as of 17 May 2026: activity statistics, top voices, debate type breakdown, policy area distribution, recent contributions. Useful as quantitative content but does not deliver on the core party-page promise.

The promoted vision (Mark, 18 May 2026): what matters is what each party has said about each policy area, taken from their manifesto, linked to key parliamentary debates. Activity stats become supporting content, not headline.

Editorial principle locked: Westminster Brief surfaces evidence — manifesto positions, parliamentary contributions. It does not characterise parties. Users draw their own conclusions.

The 9 parties currently on the site: Labour, Conservative, Liberal Democrats, SNP, Reform UK, Green, Plaid Cymru, DUP, Sinn Féin.

The 23 policy areas (GOV.UK taxonomy, locked in `hansard_archive/tagger.py`):
Business and industry · Children and families · Crime, justice and law · Defence and armed forces · Economy · Education, training and skills · Employment and labour market · Energy · Environment · Finance and taxation · Foreign affairs and diplomacy · Government and public administration · Health and social care · Housing and planning · Immigration and borders · International development · Local government · Parliament and constitution · Science and technology · Society and culture · Trade · Transport · Welfare and benefits

---

## Design Question 1: Manifesto Sourcing

**Decision: excerpt + attribution + link, no full-text reproduction.**

All major party manifestos are publicly distributed political documents. The excerpt approach (2–5 sentences per policy area, linked to source) is consistent with fair dealing for the purpose of reporting and public interest commentary. This mirrors the existing treatment of Hansard content and committee evidence. Attribution must be visible — source, year, and link on every excerpt. No full-text reproduction.

**Manifesto audit — all 9 parties:**

| Party | Document | Year | Format | Status |
|---|---|---|---|---|
| Labour | "Change: Labour Party Manifesto 2024" | 2024 | PDF + HTML (labour.org.uk/change/) | Available |
| Conservative | "Clear Plan, Bold Action, Secure Future" | 2024 | PDF (conservatives.com) | Available |
| Liberal Democrats | "For a Fair Deal" | 2024 | PDF + HTML (libdems.org.uk/manifesto) | Available |
| SNP | "Energy, Opportunity, Independence" | 2024 | PDF (snp.org/manifesto) | Available |
| Reform UK | "Our Contract with the People" | 2024 | PDF (reformparty.uk) | Available — note: branded as "contract", not manifesto |
| Green | "Real Hope. Real Change." | 2024 | PDF + HTML (greenparty.org.uk) | Available |
| Plaid Cymru | 2024 Westminster election manifesto | 2024 | PDF (plaid.cymru) | Available — Wales constituencies only |
| DUP | 2024 Westminster election manifesto | 2024 | PDF (mydup.com) | Available — Northern Ireland focus |
| Sinn Féin | No Westminster manifesto | — | — | See edge case below |

**Preferred source format:** HTML where available (cleaner extraction than PDF). PDF as fallback. For PDF-only sources, use `pdfplumber` or similar for extraction; may need manual cleanup for multi-column layouts.

**Ingestion timing:** Manifestos are 2024 documents — content won't change. One-time ingestion per party. Label clearly: "Source: [Party] General Election Manifesto, July 2024."

---

## Design Question 2: Manifesto Structure and Internal Organisation

Each party organises their manifesto differently:

- **Labour**: 6 "missions" (Growth, NHS, Safe Streets, Clean Energy, Opportunity, Public Services), each broken into sub-areas
- **Conservative**: 5 chapters (Economy, NHS, Crime, Opportunity, Safer World) — chapters broadly thematic
- **Lib Dems**: Chapter-per-topic, closely aligned to traditional policy areas
- **SNP**: Chapters + a strong constitutional/independence thread running through all areas
- **Reform UK**: "Contract" structured around headline commitments — less depth per area than traditional manifestos
- **Green**: Chapter-per-theme, often combining social and environmental angles
- **Plaid Cymru**: Wales-focused, some GOV.UK policy areas are thin or absent (Defence, Foreign affairs)
- **DUP**: NI-focused, strong on constitutional issues, thinner on UK-wide domestic policy
- **Sinn Féin**: No Westminster manifesto — see edge cases

**Proposed storage model:** discrete manifesto chunks, each tagged to one or more policy areas.

Each chunk is a coherent passage of 2–5 sentences from the manifesto that substantively addresses a policy area. A single manifesto section may yield multiple chunks if it covers multiple policy areas. Chunks carry their source section title and a URL to the relevant section of the party's website.

---

## Design Question 3: Mapping to GOV.UK Policy Taxonomy

**Challenge:** Manifesto chapter titles rarely match GOV.UK taxonomy terms. Labour's "Mission 1: Growth" spans Economy, Business and industry, Finance and taxation, and Employment. Conservative "Safer Streets" spans Crime, justice and law and Government and public administration.

**Proposed approach: AI-assisted chunking and tagging with human review.**

1. Extract full manifesto text (HTML preferred, PDF fallback)
2. Send sections to Gemini Flash-Lite with the 23-term POLICY_AREAS enum (reuse the existing tagger enum from `tagger.py`)
3. Gemini breaks each section into 2–5 sentence chunks and tags each to 1–3 policy areas
4. Schema enforcement: same pattern as session tagger — response_schema constrains policy_area values to the POLICY_AREAS enum
5. Mark reviews the output for accuracy before storing
6. Store chunks + tags

**Many-to-many tagging:** one chunk can be tagged to multiple policy areas. On the party page, for each policy area, surface the most relevantly tagged chunk (primary tag = first in the list). If a policy area appears in multiple chunks, surface the one where it is the primary tag.

**Expectation:** not every party will have coverage of all 23 policy areas. Labour and Conservatives likely cover 20+; Reform UK probably 12–15; SNP/Plaid/DUP 10–16 with regional skew; Green 18–20.

---

## Manifestos as Fixed Reference Documents

Party manifestos are published only at general elections. The 2024 manifestos are the canonical commitments for the current Parliament. No party will publish a new manifesto until the next general election (likely 2028–2029).

**Implications:**
- The page surfaces "manifesto commitments from the 2024 general election" — not "current positions." Framing matters: governments diverge from manifestos; oppositions refine theirs. The manifesto is a fixed point-in-time document, not a live policy statement.
- No refresh cycle for manifesto content. Ingest once per party; the data does not change during this Parliament.
- **Editorial discipline on tagging is critical.** Incorrect chunk tags stay incorrect — there is no future re-ingestion to correct errors. This is why `review_status = 'pending'` until Mark approves every chunk before it goes live.
- The interesting analytical layer — comparing manifesto commitments to what parties have actually said and done since July 2024 — is deferred to Tier 4. The data infrastructure being built now (chunks + activity links) will support that comparison when the time comes.

**Framing locked for all party pages:**
- Section heading: "Manifesto commitments — 2024 general election"
- Every excerpt carries explicit date attribution: "2024 manifesto"
- Footer note: "Manifesto commitments reflect each party's 2024 general election manifesto. Manifestos are published at general elections and do not update during a Parliament."

---

## Design Question 4: Content Depth Per Policy Area

**Per (party × policy area) pair, display:**
1. One manifesto excerpt: 2–5 sentences, the most relevantly tagged chunk
2. Source attribution: "[Party] General Election Manifesto 2024 — [Section title] ↗" linked to the party's website
3. Recent parliamentary activity: 2–3 debate sessions, newest first (see Question 5)
4. For Labour only: recent ministerial statements on this area (see Question 6)

**What NOT to display:**
- AI-generated summaries or characterisations of the manifesto position
- The tool's own interpretation of what the party "believes" or "stands for"
- Any language that editorialises beyond the manifesto text itself
- Any implication that the manifesto excerpt represents current party thinking (it is a 2024 commitment, not a live position)

The manifesto excerpt speaks for itself. The tool's job is to surface it with proper attribution, not to interpret it.

**Empty states:** if a party has no manifesto chunk tagged to a policy area AND no parliamentary activity in that area, the policy area section does not render. Conditional rendering throughout — no placeholder cards.

---

## Design Question 5: Parliamentary Activity Linking

**For each (party × policy area), surface 2–3 recent substantive debate sessions.**

Query:
- `ha_session_theme.theme = policy_area AND theme_type = 'policy_area'`
- `ha_contribution.party IN (party contrib_codes)`
- `ha_session.is_container = False`
- Order by `ha_session.date DESC`
- Limit 3

**"Substantive" definition:** session must have at least one contribution from a party member over 30 words (filters out procedural interventions). The existing Tier 1 party page already queries contribution counts per session — extend this.

**Infrastructure reuse:** `_compute_party_data()` already joins sessions to contributions by party code. The extension is: group sessions by policy area tag, then surface the top 3 per area.

**Display:** session title, date, contribution count from this party, link to full debate transcript (`/archive/debate/<date>/<slug>`).

---

## Design Question 6: Ministerial Statements

**Only renders for parties currently in government** — Labour currently; no other party in coalition or supply-and-confidence arrangement at present.

For Labour, for each policy area:
- Query `ha_session WHERE debate_type = 'wms'` (Written Ministerial Statements) already in DB
- Filter by policy area tag
- Surface 1–2 most recent, linked to full transcript

This data already exists in the archive — WMS are ingested and tagged by the existing cron. No new data source required.

For all other parties: this section does not render. No empty state — just absent.

**Condition check:** add a `is_government` boolean to `_PARTY_SLUG_MAP` config. Currently `True` only for Labour. Update when government changes.

---

## Design Question 7: Edge Cases

**Reform UK — "contract" framing:**
Reform called their 2024 document "Our Contract with the People." Treat it identically to a manifesto for ingestion purposes. No special UI treatment needed — the source attribution will naturally reflect the document's own title. Coverage of GOV.UK policy areas will be narrower than Labour/Conservative; expect 12–15 areas with substantive content.

**SNP — Scottish manifesto scope:**
SNP's 2024 manifesto is a UK general election manifesto standing candidates in Scottish constituencies. It addresses UK-wide policy areas where Westminster has jurisdiction (Defence, Foreign affairs, Economy, Employment) as well as devolution-related issues. Coverage of all 23 GOV.UK areas is plausible, but the constitutional independence thread runs through most sections — will need care in chunking to isolate policy substance from constitutional framing.

**Plaid Cymru — Welsh scope:**
Plaid's manifesto focuses on Wales. UK-wide policy areas that are largely devolved (Health, Education) will have thin Westminster content; areas where Westminster has direct competence (Defence, Foreign affairs, Economy, Employment) will have more. Expect 12–18 areas with meaningful content. Be honest with thin coverage.

**DUP — Northern Ireland focus:**
DUP's manifesto is NI-focused. Constitutional (Union, Northern Ireland Protocol/Windsor Framework) content is dominant. UK domestic policy areas will be thinner than GB parties. Expect 10–15 areas. NI-specific issues map partially to GOV.UK areas: constitutional = "Parliament and constitution", policing = "Crime, justice and law".

**Sinn Féin — no Westminster manifesto:**
Sinn Féin contests NI Westminster seats but abstains from taking them. They have no Westminster manifesto in any conventional sense. They have policy documents, but these are for the Irish/NI political context, not Westminster governance.

*Decision:* Display a factual note on the Sinn Féin party page's policy section:

> "Sinn Féin abstains from Westminster and does not publish a Westminster manifesto. No policy positions are recorded in this section. Parliamentary contributions from Sinn Féin members are not included in the Westminster Brief archive as the party does not participate in Westminster proceedings."

This is factually accurate, editorially neutral, and consistent with the existing Sinn Féin note in the Tier 1 implementation. No placeholder cards. No attempt to source alternative policy documents.

**Green Party — England and Wales only:**
Green's 2024 manifesto covers England and Wales. Scotland (Scottish Greens) and Northern Ireland have their own parties. Parliamentary activity in the DB is GB-wide. No special handling needed — note "Green Party of England and Wales" in the source attribution.

---

## Design Question 8: Refresh Strategy

| Content type | Change frequency | Strategy |
|---|---|---|
| Manifesto chunks | Never (2024, point-in-time) | Ingest once; no refresh cron |
| Manifesto tags (policy area mapping) | Occasional (if tagging errors found) | Manual correction via admin or direct DB |
| Parliamentary activity links | Daily | Query at render time; 1-hour cache (existing party page TTL) |
| Ministerial statements | Daily | Query at render time; 1-hour cache |
| "Activity last updated" indicator | Daily | Display `MAX(session.date)` for that (party × policy area) pair |

**No staleness problem with manifesto content:** it's explicitly attributed to the 2024 general election manifesto. Users know it's a point-in-time document. Display "July 2024 manifesto" in every source attribution — the date is the attribution.

---

## Design Question 9: Cross-Cutting UI Decisions

**Which policy areas to show?** Conditional rendering: only show areas where the party has at least one manifesto chunk or parliamentary activity. For a party like Labour this will be most of the 23; for DUP it may be 10–12.

**Page ordering:** show policy areas alphabetically within the section. Alternative (by activity volume) introduces a ranking decision that might look like editorial priority — alphabetical is neutral.

**Show/hide:** if a party has 15+ policy areas, consider defaulting to showing the 6 most active (by recent session count) with a "Show all policy areas" toggle. For parties with 8 or fewer, show all. Prevents overwhelming the first-load experience.

**Visual hierarchy:**
- Policy positions section: full-width, prominent, positioned above fold
- Activity overview (Tier 1 stats): below the policy section, or in a collapsible panel
- Methodology: footer-level, small text

---

## Design Question 10: SEO

- URL structure unchanged: `/archive/party/labour` — canonical, do not change
- Anchor links: `/archive/party/labour#health-and-social-care` — slugify policy area names (reuse `slugify_theme()` from `hansard_archive/slugs.py`)
- Content depth: Labour page goes from ~500 words of stats to ~3,000–5,000 words of attributed manifesto content + activity links. Meaningful SEO uplift for queries like "Labour housing policy" or "Conservative NHS manifesto 2024"
- Schema.org: consider `PoliticalParty` type (not in standard schema.org but `Organization` with `additionalType`) with `description` per policy area. Low priority for initial implementation.

---

## Proposed Data Model (schema sketch — not a migration)

```sql
-- One row per manifesto chunk (2-5 sentences)
CREATE TABLE manifesto_chunk (
    id              SERIAL PRIMARY KEY,
    party_slug      TEXT NOT NULL,          -- 'labour', 'conservative', etc.
    manifesto_year  INTEGER NOT NULL,       -- 2024
    source_section  TEXT,                   -- original section title in the manifesto
    source_url      TEXT,                   -- URL to the relevant section on party website
    chunk_text      TEXT NOT NULL,          -- the excerpt (2-5 sentences)
    ingested_at     TIMESTAMP NOT NULL,
    review_status   TEXT DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected'
    UNIQUE (party_slug, manifesto_year, source_section, chunk_text)
);

-- Many-to-many: chunk → policy area tags
CREATE TABLE manifesto_chunk_tag (
    chunk_id        INTEGER REFERENCES manifesto_chunk(id) ON DELETE CASCADE,
    policy_area     TEXT NOT NULL,          -- must be in POLICY_AREAS enum
    is_primary      BOOLEAN DEFAULT FALSE,  -- True if this is the best-fit area for this chunk
    PRIMARY KEY (chunk_id, policy_area)
);
```

**Notes:**
- `review_status` allows Mark to approve/reject AI-tagged chunks before they go live. Only `approved` chunks render on party pages.
- `is_primary` allows display logic to pick the single best chunk when multiple chunks are tagged to the same policy area.
- No `cached_member` dependency — manifesto content is party-level, not individual.
- No cron needed — ingestion is a one-time admin operation per party.

**_PARTY_SLUG_MAP addition:**
Add `"is_government": True/False` to each party config. Used to gate the Ministerial Statements sub-section.

---

## Reimagined Party Page Structure

```
/archive/party/labour

┌─ HEADER ─────────────────────────────────────────────────────┐
│ Labour                                                        │
│ UK political party, currently in government. Founded 1900.    │
│ 412 MPs · 180 Lords  |  labour.org.uk ↗                     │
└───────────────────────────────────────────────────────────────┘

┌─ POLICY POSITIONS ───────────────────────────────────────────┐  ← HEADLINE
│ Manifesto commitments — 2024 general election                │
│                                                              │
│ ▾ Health and social care                                     │
│   "[Manifesto excerpt, 2–5 sentences from the manifesto.]"  │
│   Source: Labour Manifesto 2024 — NHS chapter ↗             │
│                                                              │
│   Recent parliamentary activity                              │
│   · NHS funding reform — 14 May 2026 · 23 contributions →  │
│   · Social care workforce — 2 May 2026 · 11 contributions → │
│   · Mental health waiting times — 28 Apr 2026 · 8 contr. → │
│   Ministerial statements: 4 in last 3 months ↗              │
│                                                              │
│ ▾ Economy                                                    │
│   "[Excerpt]"                                                │
│   Source: Labour Manifesto 2024 — Growth chapter ↗          │
│   ...                                                        │
│                                                              │
│ [Show all 21 policy areas ↓]                                │
└───────────────────────────────────────────────────────────────┘

┌─ ACTIVITY OVERVIEW ──────────────────────────────────────────┐  ← SUPPORTING
│ 18,420 total contributions (12 months)                       │  (collapsed by
│ [Debate type breakdown] [Monthly chart]                      │  default or
│ Top voices: [list]  Recent contributions: [list]             │  below fold)
└───────────────────────────────────────────────────────────────┘

┌─ SOURCES AND METHODOLOGY ────────────────────────────────────┐  ← FOOTER
│ Manifesto commitments reflect each party's 2024 general      │
│ election manifesto. Manifestos are published at general      │
│ elections and do not update during a Parliament.             │
│ Parliamentary activity: Hansard, tagged by policy area using │
│ AI. Activity current to [date].                              │
└───────────────────────────────────────────────────────────────┘
```

**Sinn Féin party page — policy section:**
```
┌─ POLICY POSITIONS ───────────────────────────────────────────┐
│ Sinn Féin abstains from Westminster and does not publish a   │
│ Westminster manifesto. No policy positions are recorded in   │
│ this section.                                                │
└───────────────────────────────────────────────────────────────┘
```

---

## Effort Estimate — Post-Teams-Share Implementation

| Phase | Scope | Estimated sessions |
|---|---|---|
| 1. Schema + ingestion pipeline | DB tables, Gemini chunking/tagging script, PDF/HTML extraction for 9 parties | 2–3 |
| 2. Mark review pass | Review AI-tagged chunks, approve/reject, correct tags | 1 (Mark) + 0.5 (Code support) |
| 3. Backend query layer | `_compute_party_policy_positions()` in views.py, WMS query for Labour | 1 |
| 4. Template redesign | Restructure `archive_party.html`, conditional rendering, anchor links | 1 |
| 5. CSS + UX | Policy section styling, show/hide toggle, empty states | 0.5 |
| **Total** | | **5–6 sessions** |

---

## Identified Risks and Open Questions

**Risk 1: PDF extraction quality.**
Some manifesto PDFs (particularly Plaid Cymru, DUP) may have poor text extraction — multi-column layouts, headers embedded as images, etc. Mitigation: prefer HTML sources where available; flag poor-quality extractions for manual cleanup during Mark's review pass.

**Risk 2: AI tagging accuracy.**
Gemini may tag chunks too broadly (assigning "Economy" to sections that are more specifically "Employment" or "Finance"). Mitigation: Mark's review pass before any chunks go live; `review_status = 'pending'` until approved.

**Risk 3: Manifesto age.**
By post-share implementation (June 2026+), the 2024 manifestos will be ~2 years old. Party positions may have shifted (e.g. Labour in government has moved on some manifesto commitments). Mitigation: clear date attribution ("July 2024 manifesto") on every excerpt; add a page-level note "Policy positions reflect each party's 2024 general election manifesto."

**Risk 4: Reform UK coverage breadth.**
Reform's "Contract" is significantly shorter than a traditional party manifesto. Many GOV.UK policy areas may have no substantive content. Mitigation: conditional rendering handles this naturally — their page simply shows fewer policy areas than Labour or Conservative.

**Risk 5: SNP/Plaid constitutional framing.**
For SNP and Plaid, independence/devolution framing runs through almost every policy section. Extracting policy substance without including constitutional arguments requires careful chunking. Mitigation: chunking prompt instructs Gemini to focus on the policy substance of each section; Mark review to catch any constitutional-heavy excerpts that don't represent substantive policy positions.

**Open question: does Tier 1 activity section stay visible or collapse?**
On Tier 3 launch, the activity statistics (Tier 1) become supporting content. Decision needed: hidden by default (expandable), shown below fold, or removed. Defer to implementation phase — depends on how much real estate the Tier 3 policy section takes on the actual page.

**Open question: anchor link UX.**
Anchor links to specific policy areas (#health-and-social-care) would be useful for sharing ("here is Labour's housing policy") and for SEO. Implementation straightforward but needs template support. Recommend including from the start.

---

*Implementation begins post-Teams-share (target week of 18–22 May 2026). Drawing on this document as the brief.*
