# Phase 1.9 — Local content extraction + per-publication pages

**Status:** Vision recorded — not yet built, not yet fully scoped
**Recorded:** 28 May 2026
**Prerequisite:** Phase 1.8 (data_url) — can't extract file contents until file URLs are captured

Cross-reference: Phase 1.8 direction document is `docs/phase-1-8-scoping.md`.
The Phase 1.9 direction section there (lines ~352–386) captures the original
scope outline. This document is the expanded vision, recorded after the
data_url design was finalised.

---

## The thesis

For each statistical publication, extract the substantive content (summary text,
key findings, headline figures) from the source files (PDF, XLSX, ODS, CSV) and
render it on a per-publication page on the site. The page is comprehensive on
the publication's substance, BUT:

- Tables and graphs are NOT reproduced. They stay at GOV.UK, reached via a
  prominent link. Reliable table/graph extraction (inconsistent markup, merged
  cells, charts-as-images with no underlying data) is a technical tar pit
  disproportionate to the value; GOV.UK renders them better; and reproductions
  go stale while links stay current.
- The substance (what the publication says, the headline numbers) IS extracted,
  stored locally, and rendered as semantic HTML.

The store/link boundary: store extractable text and numerical substance; link out
for full rendered tables, graphs, and methodology.

---

## Why store locally rather than link out (the SEO rationale)

Pure link-out generates zero SEO value — Google already indexes GOV.UK's version,
and a thin page that only links onward won't rank. A per-publication page that
renders extracted summary + key statistics + policy_area tags + structured
metadata as proper semantic HTML is an indexable asset that CAN rank for niche
policy/statistics queries. This is the TheyWorkForYou model — they rank well
precisely because they restructure and render content as indexable pages rather
than linking to source. The stats pages become another wing of the same indexable
estate built in Phase 2A (taxonomy navigation, structured data, archive-as-
rankable-content). Local storage is the mechanism that turns the stats catalogue
from a thin link directory into a knowledge graph that compounds search authority.

---

## Why this is licensingly defensible

ONS and gov.uk departmental statistics are Open Government Licence (OGL). OGL
explicitly permits copying, publishing, adapting, and commercial use with
attribution. Storing and rendering this content locally is PERMITTED in a way
think-tank content is not. This is also why the OGL-only producer scope (locked
27 May 2026) matters beyond technical tractability — it's what makes local
storage-and-render defensible. We reproduce only content we're explicitly
licensed to reproduce, with attribution.

---

## Constraints that carry forward into Phase 1.9 scoping

**1. Staleness ownership shifts.**
Link-out = GOV.UK owns correctness. Store-and-render = WB owns correctness until
next re-extraction. Mitigation: every rendered figure shows "data as of [date],
source: [link]" prominently (the data-accuracy-safeguard pattern from the
briefing pack scoping applies directly). Turns staleness into honest, dated
information rather than a credibility risk. Note: GOV.UK corrections to existing
figures won't show until next re-extraction (up to ~30 days) — the dated
provenance handles this honestly.

**2. Accuracy bar is higher once rendered under WB's banner.**
A wrong figure on a WB page is a WB credibility hit, especially for the civil
servant audience. The 95% accuracy target exists for this reason.
Retrieve-don't-generate discipline: extractor pulls exact figures from source
files, never generates or rounds; every figure carries provenance.

**3. The store-vs-link boundary needs precise definition before build.**
"Comprehensive bar tables and graphs" is the right principle but the extractor
needs a precise target: which content is extracted and rendered (summary/
abstract? headline figures? key-findings? the publication's own summary section?)
vs what stays a link. Needs the same evidence-based scoping the data_url work
got — examine real publications across producers before locking.

---

## Relationship to Phase 1.8 (data_url)

Phase 1.8 is the prerequisite: can't extract file contents until file URLs are
captured. The two phases are distinct in effort — data_url captures URLs (small);
Phase 1.9 extracts and renders content (multi-week, 95% accuracy bar).

Phase 1.8 piece 2's extraction behaviour (data files only, HTML excluded, link to
source) is the correct foundation regardless of how Phase 1.9 shapes up.

---

## Consistency with existing principles

- **Curated-entry-point principle** (briefing pack scoping): surface and point,
  with attribution, link to authoritative source. Here: store substance, link
  tables/graphs.
- **Retrieve-don't-generate / cite-don't-reproduce** (data-accuracy safeguards):
  extract exact figures with provenance, never generate.
- **Propriety stance:** public OGL data, attributed, pointing to canonical source.

---

## What needs to be scoped before building

This document records the vision. Full Phase 1.9 scoping needs:

1. **Examination of real publications across producers** — what content is
   extractable from PDF, XLSX, ODS across ONS, DfE, DWP, DHSC? Where are the
   tar pits (scanned PDFs, charts-as-images, inconsistent structure)?
2. **Precise store/link boundary definition** — what exactly is extracted
   (summary paragraph? headline figures? key findings bullet list?) vs what
   stays a link.
3. **Accuracy measurement infrastructure** — how do we know extraction is 95%+
   before rolling out? Needs a validation set and a measurement run.
4. **Cost modelling** — multimodal LLM calls on PDFs are not free. Unit cost ×
   publication count × re-extraction frequency = sustainable or not?
5. **Per-publication page design** — what does the rendered page look like? What
   metadata surfaces alongside the extracted content?

Phase 1.9 scoping starts after Phase 1.8 data_url is complete and the first
backfill has run — real data about file types and page structures will inform
scoping more than speculation from here.

---

## Free-archive stats presentation — design thinking (28 May 2026, NOT yet decided)

This refines how extracted stat content is presented on the free archive. It is
generative thinking, not a locked decision — the real decision needs a dedicated
session with spike data in hand. Recorded so the options and the lines drawn
aren't re-derived later.

### The target output Mark wants

Curated stat bullets — e.g. "2.86 million HE students in 2023/24" — the 3–5
figures a human analyst would actually quote. Comprehensive WHERE that detail
exists in the source, but only where it genuinely exists.

### The core problem identified

The headline figures Mark wants as bullets often aren't in a cleanly extractable
place on GOV.UK pages. They're in prose narrative, or one cell among hundreds in
a spreadsheet, or in chart-images with no underlying data, or split across main
release / supplementary tables / technical annex. Extracting "the data" is one
problem; extracting "the HEADLINE figures" is a different, harder problem —
because "which 4 numbers matter" is a salience JUDGMENT, not a parseable field.
A spreadsheet has 400 numbers; the curated bullet wants the 4 that matter, and
nothing in the file marks which 4.

### The lift-vs-manufacture distinction (the crux)

- **LIFT existing curation (safe):** where a publication has a statistician-authored
  "key findings / main points / headline" section, extract THOSE figures. The
  salience judgment was already made, correctly, with authority, by the source's
  own statisticians. Present with attribution + date. This is NOT AI summarisation
  — it's lifting curation that already exists. Unambiguously fine.
- **MANUFACTURE curation (risky):** where there's no headline section, asking an LLM
  to select/summarise headline figures from raw tables is the model manufacturing
  salience judgment. This is error-prone (wrong figures, mislabelled, wrong
  period) and a wrong/mislabelled figure is worse than none for the civil-service
  audience. This hits the "no AI summaries in free archive" gate locked in
  Phase 2A scoping.

### The resolved default (Mark agreed 28 May)

Where a publication LACKS curated headlines, the default is to surface, WITHOUT
synthesis:
- The publication's own abstract/description (authored by the source, lifted with
  attribution)
- The list of data files with their titles
- The policy-area tags
- A prominent link to the full source

This is "comprehensive about what's there" without manufacturing curation. Clears
the licensing gate (source's own abstract, not a derivative), the AI-summary gate
(no synthesis), and the accuracy gate (no asserted figures). Ages gracefully —
nothing rendered can drift out of correctness. It's the curated-entry-point
principle applied: surface and point, don't synthesise.

### The spectrum for no-headline publications (lowest to highest risk)

1. Metadata + file list + abstract + link (no synthesis — the agreed default)
2. AI summary WITH full data-accuracy safeguards (dated, every figure
   source-cited, "AI-generated — verify against source" label, figures
   retrieved-not-generated) — opens the gate; only ever a DELIBERATE, eyes-open
   decision for a known subset, never the default, never slid into by drift
3. AI summary without safeguards — don't

### Why Phase 1.9 is now de-risked

The product works regardless of how extraction quality turns out. If curated
headlines are common → rich version (lift them). If rare → fall back to the
abstract+link default, which Mark has confirmed is acceptable. Headline
extraction is UPSIDE, not a dependency. This dissolves the earlier "what if
extraction doesn't meet quality" anxiety — the floor (abstract+link) is good
enough, so quality extraction is a bonus not a requirement.

### The missing input — the spike question

Everything depends on a number not yet known: what FRACTION of GOV.UK statistical
releases carry a statistician-curated headline/key-findings section?
- High fraction → curated-bullet vision largely achievable by lifting; AI-summary
  is a small edge case, possibly skippable
- Low fraction → forced toward the AI-summary gate for a large chunk; decision
  gets weightier

Checkable on real pages in an afternoon. The Phase 1.9 spike should test the ACTUAL
target: "can we reliably produce 3–5 correct, correctly-contextualised headline
bullets" — AND "how often does the source give us liftable curation vs how often
would we have to manufacture it." That ratio is the real input.

### Mark's stated approach (28 May)

"Expose what we have and see what we end up with, take it from there." The
abstract+file-list+tags+link default is buildable from data ALREADY captured
(file lists from data_url pieces 1–3; abstracts and tags exist). Surfacing that
minimal version on stat pages lets Mark see the real corpus rendered as a user
would — which becomes the empirical input to the bigger decision, rather than
reasoning about hypothetical pages. The exposing IS the spike, in effect.

### Status: needs a bigger conversation

The genuine decisions (does the free archive ever carry AI summaries; curated-
headline prevalence; how rich the stat pages get) need a dedicated session with
spike/exposure data. NOT resolved here. This note records the options and the
lines drawn so that conversation starts from the right place.

---

## Taxonomy hub pages + topic context — design thinking (28 May 2026, NOT decided)

### Hub-and-spoke / pillar-cluster model — ENDORSED (it's already the plan)

Policy-area pages as hubs, individual PQs/sessions as spokes, programmatic
cross-linking via shared taxonomy tags, breadcrumbs reflecting hierarchy, URL
structure mirroring the taxonomy. This is what the Phase 2A taxonomy-navigation
work was already heading toward — the taxonomy as "core navigation infrastructure,
not just metadata." Confirmation, not new direction.

Cheap high-value addition endorsed: auto-generated "Read More — 5 most recent from
the same taxonomic bucket" at the bottom of each PQ/session page. Fits the model,
reduces bounce, pure structured cross-linking, no synthesis.

### Factual data tables on hub pages — ENDORSED IF sourced correctly

Quick-reference tables (e.g. "current thresholds," "figures by region") are sticky
and useful — BUT only if figures are retrieved-and-cited, not AI-generated. Same
data-accuracy safeguard: retrieve, don't generate; dated and sourced. A sourced
table is a better bounce-killer than AI prose anyway (cautious audience trusts a
sourced table over an AI paragraph).

### Topic explainers ("what is Plan 5", "how does UKRI allocate funding") — OPEN

The key distinction (determines whether licensing even applies):
- **Summary OF THE BRIEFING** (condensing Commons Library briefing content) =
  derivative work of parliamentary material → falls back to OPEN PARLIAMENT
  LICENCE check (NOT OGL — different licence, parliamentary copyright applies; the
  stats OGL argument does NOT transfer). Also the weaker product. Probably don't.
- **Summary OF THE TOPIC** (explaining the subject from primary facts, + link to the
  briefing as further reading) = not a derivative; clears licensing. But still an
  AI-generated explainer if an agent writes it → hits the AI-summary gate, AND
  carries accuracy/staleness risk (policy facts like thresholds change; "evergreen"
  is the WRONG word for policy content — it's fast-decaying).

Recommended shape if explainer content happens at all: own-topic factual framing
(dated, sourced to primary GOV.UK) + prominent link to canonical source. Safe
fallback: just the link + minimal factual framing, no explainer prose.

### Canonical source references in DB — ENDORSED (references only)

Store METADATA about authoritative sources (Commons Library briefings, POSTnotes
etc.): title, URL, policy-area tag, last-updated date. Surface "relevant briefings
for this policy area" on hub pages. Pointers + metadata, linking out — clean,
consistent with locked "authoritative further reading" principle, good for SEO
(topical authority + quality outbound links; TheyWorkForYou ranks well without
reproducing briefings).

DO NOT ingest briefing CONTENT into the DB until: (1) Open Parliament Licence
terms on reproduction/adaptation actually checked, AND (2) staleness-vs-canonical
resolved (the Library maintains its briefings; a stored copy goes stale — the
link-out-with-date approach exists precisely to avoid this). Even if licence
permits, link-out is probably better than storing content.

Note: Commons Library briefings are maintained but UNEVENLY — updated when there's
reason to, not continuously. Many are written for a specific event then untouched.
The dated-link pattern handles this (user sees last-updated date, judges freshness).

### AI-generated hub PROSE (auto-drafted pillar overviews) — GATED OUT for now

The original "agent drafts evergreen pillar content summarising themes across PQs"
idea runs into the locked "no AI summaries in free archive" decision AND is the
highest-risk version (synthesising parliamentary activity, where exact ministerial
wording — the lexical-drift signal — is the whole analytical premise; paraphrasing
destroys it). Hub pages should be built from structured/factual/non-synthesised
elements (PQ lists, counts, sub-theme clouds, breadcrumbs, sourced tables) — which
still does the SEO and bounce job without crossing the gate.

### Caveat on the bounce-rate framing

The original idea cited "killing a 75% bounce rate." We have NO measured bounce
figure — Plausible data so far is small and dominated by Mark + the Teams share.
Build hub pages on their own merits (SEO, navigation, audience-widening shares),
not anchored to an unconfirmed bounce number.

### Status: down the road, needs the bigger Phase 1.9 conversation

All of this is part of the same unresolved question — what the free archive
surfaces about a topic beyond raw parliamentary data. The four points on the
spectrum (store references / factual framing / AI explainers / reproduced content)
want deciding together, with the Open Parliament Licence check as a specific
prerequisite for anything involving briefing content.

---

## Empirical finding from Phase 1.8 piece 2b — 47 known HTML-content targets (28 May 2026)

**Recorded from:** piece 2b dry-run diagnostic (feature/phase-1-8-data-url-piece-2b, commit ad2c72a)

### What was found

The Phase 1.8 piece 2b diagnostic identified 47 GOV.UK statistical publications
(out of 108 `no_files_found` GOV.UK publications) whose landing pages link to
HTML sub-pages rather than directly to downloadable data files. The sub-page
follower correctly returned `no_files_found` for all of these — there are no
`gem-c-attachment` download components on the sub-pages.

However, the sub-pages **do contain the actual statistical data**, presented as
HTML content on the page: figures, tables, narrative with embedded statistics.
The data is there; it is just not in a downloadable file format.

**Representative example (pub=664, DWP):**

Landing page: `https://www.gov.uk/government/statistics/move-to-universal-credit-july-2022-to-end-march-2026`

Sub-page: `https://www.gov.uk/government/statistics/move-to-universal-credit-july-2022-to-end-march-2026/completing-the-move-to-universal-credit-statistics-related-to-the-move-of-households-claiming-tax-credits-and-dwp-benefits-to-universal-credit-data`

The sub-page has statistics on it — figures, data — but no file download buttons.
"Print this page" is the only export avenue. The data lives in the HTML.

### Composition of the 47

DWP dominates (25 of 47 pubs). Remaining are DfT, DBT, DSIT, Defra and others.
Full ID list:
```
664, 686, 687, 695, 705, 720, 733, 743, 750, 751, 775, 814, 828, 835, 838,
847, 848, 859, 862, 1124, 1128, 1133, 1135, 1164, 1166, 1167, 1193, 1198,
1202, 1205, 1207, 1246, 1272, 1280, 1296, 1309, 1400, 1438, 1454, 1460,
1494, 1504, 1505, 1541, 1549, 1601, 1602
```

### Why this matters for Phase 1.9

These 47 publications are **confirmed Phase 1.9 targets** with known sub-page
URLs. The data is:
- Accessible (OGL, publicly available HTML)
- Already located (sub-page URLs captured by piece 2b via `sub_page_urls` in
  `ExtractionResult` and logged during the dry-run)
- Known to contain statistics (verified by manual browser check, 28 May 2026)

Phase 1.9 HTML extraction has a concrete starting cohort rather than an
abstract target. The sub-page URLs for these 47 publications can be retrieved
from the `ha_stat_publication` table (via `sub_page_urls` if stored, or by
re-running the piece 2b extractor in read-only mode).

### What Phase 1.9 needs to handle for this cohort

These pages appear to use GOV.UK's `govspeak` HTML rendering (standard
gov.uk statistics presentation). The content likely includes:
- A headline summary / "main points" section (potentially liftable curation —
  see "lift-vs-manufacture distinction" section above)
- Data presented in `<table>` elements within `div.govspeak`
- Narrative paragraphs with inline figures

Phase 1.9's HTML extraction path should target `div.govspeak` content on these
sub-pages, not the landing page (which only contains sub-page links).

### Current DB state for these 47 publications

All 47 remain at `data_files_status = 'no_files_found'`. Piece 2b correctly
declined to set them to `extracted` (no downloadable files found). If a future
Phase 1.9 build ingests HTML content from sub-pages, these rows would likely
need a new status value (e.g. `html_content_only`) to distinguish "no files,
but sub-page HTML content available" from "no files and no content either".
