# Government Consultations — Design Note

**Status:** Future feature. Backlog item. Not currently being built.

---

## Concept

Add a parallel data layer to the directory covering government consultations alongside the existing committee evidence coverage. Government consultations are the executive-led equivalent of select committee inquiries — same audience cares about both, similar engagement patterns, different data sources.

---

## Categories worth distinguishing

- **Open consultations** — currently accepting responses. Highest immediate value for users.
- **Closed, awaiting response** — submission window closed, government hasn't yet published formal response. Submissions made, waiting on outcome.
- **Closed and responded** — full lifecycle complete. Historical record.
- **Anticipated/forthcoming** — signalled in policy papers, ministerial statements, white papers. Intelligence for planning.

---

## Data sources to investigate

- gov.uk publications API (consultations are a publication type on gov.uk)
- Individual department websites where consultations may be listed before gov.uk indexing
- Cabinet Office Grid (forthcoming announcements, not always public)
- Ministerial statements signalling forthcoming consultation

Verify current API access, rate limits, and structured data availability before committing to ingester design.

---

## Why this fits Westminster Brief

Same audience as directory. Adds meaningful dimension to stakeholder engagement coverage. Cross-references with existing committee evidence — an organisation that responds to a DfE consultation on SEND probably also gave evidence to the Education Committee inquiry on the same topic. Together, richer picture than either alone.

Potentially flagship feature for paid-tier subscriptions. Public affairs teams and policy charities pay for similar coverage from incumbents.

---

## Build sequencing

**Phase 1 — Open consultations only.**
Ingest currently open consultations. Single page listing them, filterable by department or topic. Simplest version of the value.

**Phase 2 — Lifecycle states.**
Add closed-awaiting-response and closed-responded states. Lifecycle visualisation per consultation.

**Phase 3 — Cross-reference with directory.**
Show consultation respondents in the directory, alongside committee evidence and lobbying register. Treats consultation response as another engagement type.

**Phase 4 — Anticipated consultations.**
Harder data source — signals scattered across multiple government publications. Worth building only if the previous phases prove valuable.

---

## Open questions

- Is there a clean structured-data API for consultations, or is it gov.uk publication metadata with consultations identified by type?
- How are joint consultations (multiple departments) handled in the data?
- For anticipated consultations, what's the most reliable signal source?
- Should consultation response surface as a new engagement type in the directory schema, or as a separate parallel data structure?

---

## When to build

Subject to:
- Public beta running with tester feedback
- Confirmation that consultation tracking is a sought feature
- Capacity for substantial new feature work (30–50 hours)

Likely 2027 not 2026.
