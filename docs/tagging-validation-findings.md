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
