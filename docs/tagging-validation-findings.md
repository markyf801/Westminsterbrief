---

# Tagging Validation & Bug Findings

Started: 2 May 2026
Purpose: Capture issues surfaced during real use of the archive during the
soft-launch phase. Distinguishes UX bugs from tagging quality from
architecture/feature gaps.

## How to read this

Three categories of finding:

- **BUG** — something is broken or behaves unexpectedly. Fixable by Code.
- **TAGGING** — theme assignments on specific sessions look wrong or
  inconsistent. Inputs into prompt iteration or backfill re-tagging.
- **ARCHITECTURE** — system can't do what users need. Phase 2A.5+ candidate
  features.

Severity: Low / Medium / High based on impact on share-readiness and user
experience.

Status: Open / In progress / Fixed / Deferred / Won't fix

---

## Findings

### Finding 1 — Search + filter don't compose

**Date**: 2 May 2026
**Category**: BUG
**Severity**: Medium (affects share-readiness — primary user workflow)
**Status**: Fixed (2 May 2026)

**Observation**: When searching for a term (e.g. "franchising") and then
trying to apply a policy area filter (e.g. "Education and skills"), the
filter doesn't combine cleanly with the search. Search context appears to
be lost or filter doesn't apply on results page.

**Root cause**: `archive_search` route had no `policy` parameter — the
filter UI existed on archive_home but didn't carry through to the search
page. `_fts_search` and `_pq_fts_search` both had no policy filter arg.

**Fix**: Extended `_fts_search` and `_pq_fts_search` with `policy_filter`
param (inner JOIN on `ha_session_theme` / `ha_pq_theme`). Added policy
dropdown to search form. Preserved filter in pagination and in
`archive_home` → `archive_search` redirect.

**Expected behaviour**: Search + filter now compose. "franchising" +
"Education and skills" filter = HE-related franchising results only.

---

### Finding 2 — Mixed-context search results without clear differentiation

**Date**: 2 May 2026
**Category**: TAGGING (probable, pending verification)
**Severity**: Medium
**Status**: Open — needs verification

**Observation**: Search for "franchising" returns 101 results spanning bus
franchising, HE franchising, corporate franchising. User cannot easily
distinguish which results belong to their context of interest (HE).

**Verification needed**:
- Pick a known HE franchising session, check its assigned themes
- Pick a known bus franchising session, check its assigned themes
- If themes correctly distinguish (HE = "Education and skills", Bus =
  "Transport"), the issue is purely UX (now resolved by Finding 1 fix)
- If themes overlap or are inaccurate, this is a tagging quality issue
  requiring prompt iteration

**Action**: Verify after tagging completes, before share.

---

### Finding 3 — Search ranking doesn't reflect title relevance

**Date**: 2 May 2026
**Category**: ARCHITECTURE (ranking tuning, not a bug)
**Severity**: Medium
**Status**: Partially addressed — Open for further tuning

**Observation**: Search returns sessions in unclear order. Sessions where
the search term appears in the title (genuinely about the topic) should
rank above sessions where it appears once in passing.

**Investigation (2 May 2026)**: Title weighting IS already in place. The
FTS implementation applies a ×3 multiplier to `title_rank` at query time:

```sql
COALESCE(tm.title_rank, 0.0) * 3 + COALESCE(bc.body_rank, 0.0) AS final_rank
```

The `title_tsv` and `speech_tsv` columns are plain `to_tsvector()` without
`setweight()`, but the query-time multiplier is functionally equivalent for
ranking purposes.

**Remaining concern**: A session with extensive body matches (e.g. a long
debate with dozens of hits) could still outscore a session with a weak
title match, even at ×3. This is expected `ts_rank` behaviour — not wrong,
but may not match user expectation.

**Potential improvement (Phase 2A.5)**: Apply `setweight('A')` to title and
`setweight('B')` or `setweight('C')` to body when building combined
tsvectors, which gives Postgres more granular IDF weighting rather than a
manual multiplier. Would require ALTER TABLE + backfill on `ha_session`.

**Action**: Defer further tuning to Phase 2A.5. Current ×3 multiplier is
reasonable for launch. Monitor user feedback post-share for specific
mis-ranking examples.

---

### Finding 4 — No faceted results by policy area

**Date**: 2 May 2026
**Category**: ARCHITECTURE
**Severity**: Medium
**Status**: Deferred — Phase 2A.5 candidate

**Observation**: Search returns all matching sessions in a flat list. For
terms that span multiple policy areas (e.g. "franchising" appearing in
Transport, Education, Business), users can't easily see the distribution
or narrow without applying a filter.

**Suggestion**: Show counts by policy area at the top of search results
("47 in Transport, 28 in Business, 12 in Education and skills"),
clickable to filter.

**Action**: Defer to Phase 2A.5. Adds significant value but isn't a
launch blocker. The search + filter compose fix (Finding 1) provides
manual narrowing in the interim.

---

### Finding 5 — No recency boost in search

**Date**: 2 May 2026
**Category**: ARCHITECTURE
**Severity**: Low
**Status**: Deferred — Phase 2A.5 candidate

**Observation**: Recent debates and older debates ranked equally for the
same FTS score. Users searching active topics (e.g. current bills) likely
want recent content prioritised.

**Suggestion**: Combine `ts_rank` with a recency factor, or apply a
secondary sort by date for equal-rank results.

**Implementation note**: Secondary date sort is a one-line change and has
no schema impact. A weighted recency factor (e.g. decay function on
`now() - date`) is more complex and would need evaluation.

**Action**: Defer to Phase 2A.5. Secondary date sort for equal-rank
results could be a quick win — flag for the next search tuning pass.

---

## Tagging quality validation log

Sessions checked by hand for tag accuracy. Aim for 25-30 across different
policy areas, debate types, and time periods.

| # | Session title/date | Assigned themes | Assessment | Notes |
|---|---|---|---|---|
| | | | | |

Severity codes:
- ✓ Tags clearly correct
- ~ Tags borderline (defensible but could be better)
- ✗ Tags wrong or misleading

Target: ≥90% of sessions in ✓ category before share readiness.
