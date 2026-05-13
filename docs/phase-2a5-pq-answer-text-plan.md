# Phase 2A.5 — PQ Answer Text: Full-Text Backfill

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
5. ⏳ Stage A running locally (bulk api_id re-pass, ~30 min)
6. ⬜ Stage B test run (100 rows) — pending Stage A completion
7. ⬜ Full Stage B overnight run (~10.4 hours at 0.5s delay)
8. ⬜ Spot-check 20 random PQs post-backfill

**No FTS migration needed**: `question_tsv` is a generated Postgres column; it auto-updates when `answer_text` changes.

**Deployment note**: Do not push until Stage B verification complete. Bundle with other outstanding changes.

---

## Stage 2 (post-Stage 1) — Template and attribution

- Add OPL attribution line to PQ detail template: *"Contains Parliamentary information licensed under the Open Parliament Licence v3.0."* with link
- Confirm answer display handles multi-paragraph full answers cleanly (no runaway whitespace from stripped HTML)
- Update methodology/about page if one exists

*Stage 0 completed 13 May 2026. Stage 1 in progress.*
