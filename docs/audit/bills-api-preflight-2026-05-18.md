# Bills API Pre-Flight Report

**Date:** 18 May 2026
**API:** https://bills-api.parliament.uk/api/v1/
**Status:** Green — proceed to schema + build

---

## 1. Authentication

No API key required. All endpoints tested successfully as public unauthenticated requests.
No rate-limit headers observed in responses.

---

## 2. Session IDs for current Parliament

| Session ID | Period | Bill count |
|---|---|---|
| 38 | 2024-25 | 235 |
| 39 | 2025-26 | 389 |

**Total in scope: ~624 bills across both sessions.**

Session 38 = first session of the current Parliament (Labour government from July 2024).
Session 39 = current session.

Both sessions needed for the "full Labour government record" requirement.

---

## 3. Endpoint map — confirmed working

| Endpoint | Purpose | Key fields |
|---|---|---|
| `GET /api/v1/Bills?Session={id}&take={n}&skip={n}` | Paginated list | billId, shortTitle, currentHouse, originatingHouse, lastUpdate, isAct, isDefeated, billWithdrawn, billTypeId, introducedSessionId, currentStage |
| `GET /api/v1/Bills/{billId}` | Single bill detail | All list fields + longTitle, summary (nullable), sponsors[], promoters[] |
| `GET /api/v1/Bills/{billId}/Stages` | Full stage history | Array of {id, stageId, sessionId, description, abbreviation, house, stageSittings[], sortOrder} |
| `GET /api/v1/BillTypes` | Bill type lookup | 10 types, IDs 1–10 |

---

## 4. Schema implications from live data

**Fields that differ from the brief's schema:**
- `summary` is frequently null — many bills have no summary field. Tagging falls back to title + longTitle only.
- `bill_withdrawn` is a value (not a boolean in raw API) — store as nullable DATE or TEXT.
- `is_defeated` exists as a boolean on every bill — add this to `ha_bill`.
- `bill_type_id` is an integer referencing BillTypes — store ID + denormalized name for convenience.
- Sponsors include `member.memberId` (Parliament Members API ID) — direct match to `cached_member.member_id`. `sortOrder = 1` maps to `is_primary = True`.

**Stages:** `stageSittings` can be an empty array. Stage date comes from the first sitting in `stageSittings[0].date` where present.

---

## 5. Bill type reference

| ID | Name |
|---|---|
| 1 | Government Bill |
| 2 | Private Members' Bill (Lords ballot) |
| 3 | Consolidation Bill |
| 4 | Hybrid Bill |
| 5 | Private Members' Bill (Ten Minute Rule) |
| 6 | Private Bill |
| 7 | Private Members' Bill (Ballot) |
| 8 | Private Members' Bill (Presentation) |
| 9 | Draft Bill |
| 10 | Supply and Appropriation Bill |

For the historical record feature, Government Bills (type 1) and Private Members' Bills (types 2, 5, 7, 8) are the primary signals.

---

## 6. Model name correction

The decisions doc references "Gemini 3.1 Pro" — this model does not exist.
Corrected to: **`gemini-2.5-pro`** (the most capable available Gemini model).
The `_detect_model()` pattern in `tagger.py` will be replicated with a `gemini-2.5-pro` preference.

---

## 7. Pagination

`take` and `skip` parameters work as expected. Max `take` not tested but 100 is safe.
At 624 total bills across both sessions, ~7 pages at take=100.

---

## Pre-flight verdict: Green

- No API key needed
- Both target sessions confirmed (38, 39)
- Response shapes confirmed — schema can be finalised
- ~624 bills in scope: well within Railway Postgres capacity
- No blockers. Proceed to Step 2 (schema).
