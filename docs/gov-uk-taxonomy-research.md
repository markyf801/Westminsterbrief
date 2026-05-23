# GOV.UK Topic Taxonomy Research

**Status:** Stub — to be expanded during Phase 1.8 taxonomy design conversation
**Date created:** 23 May 2026
**Relevant to:** Phase 1.8 controlled vocabulary decision (stat publication policy_area tagging)

---

## Summary findings (23 May 2026)

GOV.UK retired its "policy areas" taxonomy in 2019. The current taxonomy is a unified hierarchical topic structure with approximately 17 top-level taxons going up to 5 levels deep.

The Westminster Brief 23-area vocabulary (`HANSARD_POLICY_NAMES` in `hansard_archive/policy_areas.py`) is **not** a mirror of GOV.UK's current taxonomy. It is an audience-tuned vocabulary that:

- Overlaps with GOV.UK in some areas (e.g. Business and industry, Crime, justice and law, Education, training and skills)
- Renames some areas to suit Westminster usage (e.g. "Employment and labour market" rather than GOV.UK's "Work")
- Promotes certain sub-topics to top-level status because they matter to Westminster's audience (Economy, Finance and taxation, Parliament and constitution)

This is a deliberate design choice, not a gap.

---

## GOV.UK current taxonomy (top-level taxons)

*To be filled in during Phase 1.8 taxonomy conversation*

---

## Westminster Brief 23-area vocabulary

See `hansard_archive/policy_areas.py` → `HANSARD_POLICY_NAMES` for the authoritative list.

Manual mapping of 20 ONS candidates (23 May 2026 review): all 20 mapped cleanly to one or more of the 23 areas. No new areas required.

---

## Decision: flat vs hierarchical

Current Westminster Brief vocabulary is **flat** (23 areas, no hierarchy). GOV.UK is hierarchical (up to 5 levels).

Phase 1.8 decision: keep flat. Reasoning:
- Flat is simpler to implement and query
- The 23 areas are already curated for audience relevance
- Hierarchical drill-down navigation is a future enhancement, not a Phase 1.8 requirement
- Stat publications and Hansard sessions both use the same flat vocabulary → cross-corpus navigation works

---

## Open question: tracking GOV.UK taxonomy drift

The Westminster Brief vocabulary is curated, not auto-synced from GOV.UK. If GOV.UK evolves its taxonomy, Westminster Brief's vocabulary won't update automatically. This is intentional (audience-tuned > GOV.UK-mirror) but worth being explicit about.

*To be decided during Phase 1.8 taxonomy conversation:* Is there ever a case for tracking GOV.UK's taxonomy as a separate vocabulary alongside the Westminster Brief one?

---

## References

- GOV.UK taxonomy: `https://www.gov.uk/topic` (top-level browse)
- Westminster Brief vocabulary: `hansard_archive/policy_areas.py`
- Phase 1.8 scoping: `docs/phase-1-8-scoping.md` (findings 1, 2, 6)
