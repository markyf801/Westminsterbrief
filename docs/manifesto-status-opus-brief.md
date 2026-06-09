# Manifesto Integration (Party Pages Tier 3) — Status & Brief for Opus

Captured: 6 June 2026 by Claude Code. Planning input for Opus.
Design doc this builds on: `docs/phase-2a5-party-pages-tier3-design.md`.

---

## One-line summary

The feature is ~80% built and the data is ingested, but it is **dark in
production**: a May revert removed the admin review UI, so zero chunks are
approved, so the manifesto section renders on no party page.

---

## Current state (verified against the `beta` tree, 6 Jun 2026; `beta == master` for these files)

**Built and intact:**
- **Schema** — `ManifestoChunk` + `ManifestoChunkTag` in `hansard_archive/models.py`
  (`review_status` gating, `source_page` / `pdf_url` columns). Live migration in
  `flask_app.py` (~L537).
- **Ingestion** — `scripts/ingest_manifestos.py`: pdfplumber extraction →
  Gemini Flash-Lite chunk + tag against the 23-term `POLICY_AREAS` enum
  (schema-enforced) → stores `review_status='pending'`. Run locally
  (~1,998 chunks, 8 parties) and against production.
- **Display** — `_compute_party_policy_positions()` in `hansard_archive/views.py`
  (~L2127) + the "Manifesto commitments" section in
  `templates/hansard_archive/archive_party.html` (~L179). Renders **only**
  `review_status == "approved"` chunks. Sinn Féin abstention note implemented.

**The gap / regression:**
- The admin review UI — route `/admin/manifesto-review` in `flask_app.py` and
  template `admin_manifesto_review.html` — was removed by commit
  `fb21bcd "Revert Merge branch 'beta'"` (18 May 2026) and never rebuilt.
- `templates/admin.html` still linked to it → 404. (Link disabled 6 Jun 2026
  pending rebuild; breadcrumb retained.)
- With no review UI nothing can be approved, so **zero chunks are approved**,
  so the manifesto section renders on **no** party page. Fully dormant in
  production despite ingested data.
- The pre-revert route is recoverable from git: `git show 6d7506b:flask_app.py`
  (route + handler), and `admin_manifesto_review.html` existed pre-revert too.

**Per-party chunk counts (full local run):**

| Party | Local run | Prod log (partial/dedup re-run) |
|---|---|---|
| Labour | 254 | 52 |
| Conservative | 373 | 113 |
| Liberal Democrat | 573 | 105 |
| SNP | 84 | 29 |
| Reform UK | 109 | 5 |
| Green | 82 | 17 |
| Plaid Cymru | 347 | 110 |
| DUP | 176 | 48 |
| **Total** | **~1,998** | **~479 (incomplete — verify)** |

Prod was ingested in ≥2 passes (the prod log shows "duplicates skipped"), so the
true prod pending count needs a direct query before any review begins.

**Not logged:** no manifesto entry in `docs/deploys.md`.

---

## Open questions for Opus to resolve before Code starts

1. **Recover vs rebuild the review UI?** Route + template exist at `6d7506b`.
   Recover-and-adapt is cheaper but predates later `flask_app.py` changes
   (stats, discovery, `SKIP_MIGRATIONS` guard) — risk of drift.
2. **Production data integrity.** Get a verified pending/approved count per party
   in prod Postgres. Is the full local run or the partial prod run the source of
   truth? Re-ingest prod clean, or review what's there?
3. **Review volume — the real launch bottleneck.** ~250–570 chunks/party is a lot
   for one human to review one-by-one (Lib Dems 573, Conservative 373, Plaid 347).
   The design assumed Mark reviews each chunk. Options: bulk approve-by-policy-area,
   tighten the chunker to emit fewer/higher-quality chunks, or a confidence
   threshold.
4. **Chunk quality before review effort is spent.** SNP/Plaid constitutional
   framing and Reform "contract" breadth (design risks 4 & 5) — sample-check
   before committing review hours.
5. **Sequencing vs beta workflow.** Feature is dormant-but-harmless in prod (only
   the now-disabled admin link). Restore review UI on a feature branch → beta →
   master per standard flow.
6. **Under-logged state.** Add a `deploys.md` row when this next touches prod.

---

## Recommended shape (for Opus to confirm or revise)

- **A.** Restore review UI (recover route + template from `6d7506b`, reconcile
  with current `flask_app.py`). Smallest unblock.
- **B.** Verify prod chunk counts; decide clean re-ingest vs review-in-place.
- **C.** Solve the review-volume problem (Q3) — gates launch, not code.
- **D.** Sample-check chunk quality (Q4).
- **E.** Mark review pass → approve → manifesto sections light up → ship
  beta → master, with a `deploys.md` row.

**Ask:** a sequenced plan covering Q1–Q6, and a recommendation on the
review-volume bottleneck (Q3), which is the main thing standing between
"data ingested" and "feature live."
