# Phase 2A.5 — PQ Answer Text: Full-Text Backfill

## Current status (11 June 2026) — read first

**The PQ answer-caching build largely ran 13–14 May 2026.** Stages 0–2 below are
complete; the ingestor (`hansard_archive/pq_ingestor.py`) now fetches the
**individual endpoint by default** for every PQ (full answer + question text,
`is_holding`, `is_withdrawn`), and the daily PQ cron has been storing full
answers since. The schema (`api_id` column), OPL attribution, and the two-stage
backfill script (`scripts/backfill_pq_answers.py`) all exist.

**The May 2026 answer backfill is confirmed complete / green-lit** (Mark, 11 Jun
2026 — "that is when we did it"). Full answers are cached for every PQ that was
in the DB as of 14 May (the then-current ~12-month window). The only loose end
is the formal post-Stage-B verification sign-off (checklist items 8 / 1–5 below),
never logged in this doc or `deploys.md` — worth a one-off confirmation pass but
not blocking.

**Outstanding work = the dates *before* the May backfill.** The DB's PQ coverage
starts at whatever the 12-month backfill's floor was (~May 2025); under the new
retention posture we want full answers back to **9 July 2024**. Those pre-floor
PQs were never ingested, so they need ingesting *with* full answers — a single
`ingest_pq_date_range()` pass (individual-endpoint answer fetch is already the
default), not a separate answer-only backfill. Implementation brief written for a
Fable session 11 Jun 2026.

**Retention re-scope (locked 11 June 2026):** the Stage 0 figures below
(90,280 total PQs / 74,511 truncated) reflect the **then-current ~12-month
rolling window**, *not* the new "current Parliament from 9 July 2024" posture.
Extending the PQ archive back to 9 July 2024 is part of the **Hansard backfill**
(queued behind this, do not start) — not the answer-text caching of PQs already
in the DB. Any PQs tabled 9 Jul 2024 → ~May 2025 that were never ingested will
need their *full answers fetched at ingest time* by the same individual-endpoint
path the ingestor already uses — no separate answer-backfill design needed, but
the count grows once those rows land. Get a fresh production count before sizing.

Two answer-backfill scripts exist in `scripts/`: `backfill_pq_answers.py` (the
two-stage version referenced here — the live one) and `backfill_pq_full_answers.py`
(an earlier single-pass draft — superseded; candidate for cleanup).

---

## Stage 0 Findings (13 May 2026)

### Current DB state

| Metric | Count |
|---|---|
| Total PQs | 90,280 |
| `is_answered = True` | 89,979 |
| Has `answer_text` | 89,979 |
| `len(answer_text) ≤ 258` (all answered rows) | 89,979 |
| `len(answer_text) = 258` exactly (truncated) | 74,511 |
| `len(answer_text) < 258` (possibly complete) | 15,468 |
| `is_holding = True` | 0 |
| `is_withdrawn = True` | 0 |

**All answered rows have `answer_text` but every single one is ≤ 258 chars.** Zero rows exceed 258 chars. This is statistically impossible for genuine full answers — it confirms the bulk endpoint truncates at 258 characters and the individual-endpoint backfill pass was not completed.

### Root cause

The Parliament WQ API bulk endpoint (`/api/writtenquestions/questions`) truncates `answerText` at ~258 characters. The individual endpoint (`/api/writtenquestions/questions/{id}`) returns the full untruncated text.

Confirmed by direct API comparison (Feb 2026 data):

| UIN | Bulk len | Individual len | Ratio |
|---|---|---|---|
| 113295 | 258 | 286 | 1.1× |
| 112157 | 258 | 1,033 | 4.0× |
| 112156 | 258 | 1,033 | 4.0× |

The ingestor (`hansard_archive/pq_ingestor.py`) has logic to detect truncation and fetch the individual endpoint, but the `--skip-answer-fetch` flag appears to have been used (or failed silently) during the 12-month backfill — because a full individual-fetch pass on ~90k answered PQs would take ~7.5 hours, making it impractical in the original 30-minute backfill window.

The `is_holding` and `is_withdrawn` fields are also 0 across the board, which corroborates that individual endpoint data was never fetched (these fields only come from the individual endpoint).

### API behaviour

- **Bulk endpoint truncates** `answerText` at 258 characters, mid-sentence if necessary
- **Individual endpoint** (`/questions/{id}`) returns full text; `id` is present in bulk responses
- **`answerIsHolding`** and **`isWithdrawn`** are only available on the individual endpoint
- **`originalAnswerText`** and **`comparableAnswerText`** are present but empty on tested samples
- **Rate limit**: none documented; 0.3s inter-request delay is safe
- **Auth**: not required (Open Parliament Licence — public data)

### OPL compatibility

The Parliament Written Questions API is covered by the **Open Parliament Licence v3.0**. Key points:
- Commercial use is permitted
- Caching and database storage is permitted (standard "use, adapt, and distribute" rights)
- Attribution required: "Contains Parliamentary information licensed under the Open Parliament Licence v3.0"
- No restrictions specific to Written Questions or answer text

Attribution is already present in the Westminster Brief footer. No new legal barrier to storing full answer text.

### Backfill scope

| Item | Estimate |
|---|---|
| Rows needing individual fetch | 74,511 (exactly 258 chars) |
| Rows to skip (short, likely complete) | 15,468 (< 258 chars) |
| Individual API calls required | ~74,511 |
| Time at 0.3s delay | ~6.2 hours |
| Additional storage | ~55 MB (est. avg 750 chars/row × 74k rows) |

A targeted query: `WHERE is_answered IS TRUE AND length(answer_text) = 258` identifies all truncated rows. The backfill can run locally overnight — upserts are idempotent by UIN.

The `is_holding` and `is_withdrawn` fields should be updated during the same pass (they also come from the individual endpoint).

### Edge cases

| Case | Notes |
|---|---|
| **Holding answers** | Genuinely short ("It has not proved possible...") — will appear in the `< 258` bucket. No backfill needed. |
| **Withdrawn questions** | `isWithdrawn` only on individual endpoint — currently 0 in DB because individual fetch was skipped. Backfill will correct this. |
| **Grouped questions** | API returns `groupedQuestions` list; grouped siblings share the same answer text. No DB schema change needed — each UIN gets its own copy. |
| **HTML in answer text** | Both bulk and individual endpoints return HTML. The ingestor already strips with `re.sub(r"<[^>]+>", " ", html)`. Full answers may contain links (e.g., gov.uk archive URLs) that become bare text after stripping — acceptable. |
| **Answers corrected after tabling** | `answerIsCorrection`, `originalAnswerText`, `correctingMember` fields exist. `originalAnswerText` was empty on all tested samples. Not a concern for backfill. |
| **Lords PQs** | Same endpoint, same truncation behaviour. No difference in backfill approach. |

### Clarifications (pre-Stage 1, 13 May 2026)

**Q1 — Individual endpoint by default**: Confirmed. After Stage 1, the ingestor always fetches the individual endpoint for answered PQs (same logic as before, now also permanently storing `api_id`). New daily cron PQs get full text on first ingest. The `_answer_truncated` dead-code path has been removed.

**Q2 — Template baseline**: [archive_pq_detail.html](../templates/hansard_archive/archive_pq_detail.html) line 90 renders `{{ pq.answer_text }}` directly. Current baseline is 258-char truncated text (mid-sentence). SEO/UX improvement is truncated → full content, not no-content → content.

**Q3 — OPL attribution**: Exact required wording: *"Contains Parliamentary information licensed under the Open Parliament Licence v3.0."* with hyperlink to the licence. Currently absent from PQ detail template — add in Stage 2 template work.

**Q4 — Rate limit**: No documented rate limit. Backfill script uses 0.5s delay (2 req/sec). At 74,511 rows ≈ 10.4 hours overnight. Can increase to 0.3s (3.3 req/sec) if no 429s observed in first 1,000 rows.

**Q5 — Short-answer breakdown**:
- 301 unanswered (answer_text IS NULL) — no backfill needed
- 15,468 is_answered=True, len < 258 — genuinely complete short answers:
  - 409 very short (< 50 chars): references to previous answers
  - 4,121 (50–100 chars): holding answers ("It has not proved possible to respond...")
  - 10,938 (100–258 chars): brief factual responses
- Post-backfill UI: these rows display as-is (they are complete)

---

## Stage 1 (Option B approved) — Implementation status

**Completed 13 May 2026:**

1. ✅ `ALTER TABLE ha_pq ADD COLUMN IF NOT EXISTS api_id INTEGER` — `flask_app.py` startup block
2. ✅ `api_id = db.Column(db.Integer, index=True)` added to `HaPQ` model
3. ✅ Ingestor updated: `api_id` stored from bulk response on insert and update; `_answer_truncated` dead code removed; `_fetch_full_answer()` removed (redundant)
4. ✅ `scripts/backfill_pq_answers.py` written: Stage A (bulk re-pass for api_id), Stage B (individual fetch for truncated rows), `--test` flag (100 rows), 429 retry, progress logs every 200 rows, naturally resumable
5. ✅ Stage A complete (13 May 2026, 57 min). 82,907/90,280 rows got api_id. 7,373 still null (mostly pre-window rows; 1 500-error from Parliament API on Nov 2025 skip=2000 missed ~500 rows)
6. ✅ Stage B test (100 rows, 13 May 2026). 100/100 updated, 0 errors. Answers expanded 258 → 751-963 chars.
7. ⏳ Full Stage B running overnight (~68,267 rows, ETA ~05:30 UTC 14 May 2026)
8. ⬜ Final verification post-Stage B (see below)

**No FTS migration needed**: `question_tsv` is a generated Postgres column; it auto-updates when `answer_text` changes.

**Pushed**: commits `073c393` (schema + ingestor + backfill script) + `273995d` (OPL attribution) deployed to Railway 13 May 2026.

---

## Stage 2 (completed 13 May 2026)

- ✅ OPL attribution added to `archive_pq_detail.html` — *"Contains Parliamentary information licensed under the Open Parliament Licence v3.0."* with link. Committed `273995d`.
- ✅ HTML residue check: 5 long-answer samples, 0 HTML tags found. Stripping is clean.

---

## Post-Stage B verification checklist (14 May 2026)

Run after Stage B completes (~05:30 UTC):

1. **Final population stats**
   - `python -m scripts._pop_stats` (re-run after recreating the script, or inline query)
   - Capture: updated count, still-truncated count, median/max answer length, is_holding count, is_withdrawn count
   - If errors > 0 in Stage B output: capture UIN, error class, count before proceeding

2. **Long-answer rendering** — pick 3–5 PQs from top of length distribution (>5k chars)
   - Visit `/archive/pq/{uin}` for each
   - Check: no runaway whitespace, text wraps correctly, no truncation in browser

3. **VACUUM ANALYZE** (run in Railway Query console):
   ```sql
   VACUUM ANALYZE ha_pq;
   ```
   Updates query planner stats for full-text search. Do after backfill, not before.

4. **Holding and withdrawn counts** — confirm is_holding and is_withdrawn now > 0 after Stage B updates these fields from the individual endpoint.

5. **Referral pattern sanity** — "I refer the hon" answers render as plain one-line text (correct — no special processing needed, the referral IS the full answer).

---

## Optional follow-ups (not Phase 2A.5 scope)

- **Inline referral resolution**: "I refer the Honourable Member to my answer of [date]" → display referenced answer inline. Complex, lower priority.
- **Theme tagging on answer text** (Stage 4 in original plan): re-tag using heading + question + answer[:400] instead of question only. Would improve theme specificity for answers that diverge from question heading.

*Stage 0 completed 13 May 2026. Stage 1 code complete, backfill running. Stage 2 complete. Verification pending.*
