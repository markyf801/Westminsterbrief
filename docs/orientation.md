# Orientation — current project state

> **Generated 2026-06-10** (regenerated) from: `docs/deploys.md` (master + beta tails),
> `docs/ideas-backlog.md`, `docs/manifesto-status-opus-brief.md`,
> `docs/phase-2a5-party-pages-tier3-design.md`, `docs/incident-log.md`, and direct
> git topology recon of `master` vs `beta`.
> Derived snapshot, not a maintained narrative — if it looks stale, regenerate it from
> the sources above before relying on it. Items that could not be verified from the repo
> or docs (e.g. anything requiring a production-Postgres query) are marked **UNVERIFIED**.
> How Mark & Opus work: `docs/working-with-opus.md`. Hard rules & constraints: `CLAUDE.md`.

---

## 1. Current phase & what's live in production

**Phase:** Phase 2A.5 housekeeping, with the cross-government **Statistics Catalogue**
(Phase 1.8 → 1.9 lineage) as the active launch piece.

**Live in production (`master`):**
- Free Hansard archive, WQ archive, PQ detail pages, archive search, sitemap, member cache.
- Research Tool Debate-Contributions tab migrated off TheyWorkForYou (member_id-keyed).
- Minister field preloads all ministers (usable without choosing a department).
- Re-discovery cron (keeps stats catalogue current; pre-classify dedup).
- **Stats Catalogue promoted to production 2026-06-09** (commits `22c652d`, `5f7ebff`,
  `d2e8949`, `a171844`): per-producer pages `/stats/producer/<slug>`, catalogue
  Producer column + "Browse by producer" cross-links, source/licence disclaimer,
  sitemap producer URLs, landing card + nav link. **Shipped DARK** — gated on
  `STATS_CATALOGUE_ENABLED`. The promotion was deliberately *file-scoped* (via worktree):
  `views.py` and the diverging party-page templates were **not** promoted, to keep the
  beta-only manifesto work out of production.
- GOV.UK discovery `order` param fix (`newest` → `-public_timestamp`, Issue A) shipped to
  master 2026-06-06 (`b4212af`). This had silently broken GOV.UK discovery 24 May–6 Jun;
  now fixed in production.

**UNVERIFIED:** whether `STATS_CATALOGUE_ENABLED` has actually been flipped on in
production yet (deploys log says "flag flipped on after live verify"; cannot check prod
state from here). The site remains `noindex` + robots-blocked pre-launch.

---

## 2. In-flight workstreams (branch + status)

| Workstream | Branch | Status |
|---|---|---|
| **Stats Catalogue launch** | promoted `beta` → `master` (file-scoped) | Shipped DARK to production 9 Jun. Remaining: live-verify, flip `STATS_CATALOGUE_ENABLED`. |
| **Party pages / Manifesto Tier 3** | `beta` only (not on master) | ~80% built, **dark**. Schema + display path intact; admin review UI was removed by the 18 May `fb21bcd` revert and never rebuilt → no approval path. See §3 and the manifesto note below. |
| **Manifesto status / next steps** | n/a (planning) | Captured in `docs/manifesto-status-opus-brief.md` — awaiting Opus-sequenced plan (recover vs rebuild review UI; verify prod chunk counts; solve review-volume bottleneck). |
| **Stats key-findings slice** | `feature/stats-key-findings-slice` | Merged to beta then **reverted** (`0aa6b47`); deferred, not abandoned (`adc1be9`). |
| **PQ answer-text caching** | n/a | Backlog slice-to-test idea (`docs/phase-2a5-pq-answer-text-plan.md`), not in flight. |

### Manifesto / party-pages detail (Task 2 recon, 10 Jun)
- **Models** `ManifestoChunk` + `ManifestoChunkTag` exist on **both** master and beta
  (`hansard_archive/models.py` ~L285). `review_status` gates display (`pending`/`approved`/`rejected`).
- **Display path** `_compute_party_policy_positions()` (`hansard_archive/views.py` ~L2172)
  filters `review_status == "approved"`; rendered in `templates/hansard_archive/archive_party.html`.
- **Design doc EXISTS** (correcting the prior belief that none existed):
  `docs/phase-2a5-party-pages-tier3-design.md` (18 May) is the canonical design doc, and
  `docs/manifesto-status-opus-brief.md` (6 Jun) is the status brief. **Both live on `beta` only —
  neither is on `master`.**
- **Ingestion script NOT in repo (UNVERIFIED):** the status brief references
  `scripts/ingest_manifestos.py`, but that file exists on **no branch and in no git history**.
  The ingestion code appears to have been run locally and never committed — i.e. not version-controlled.
- **Admin review UI gone:** route `/admin/manifesto-review` is **not** in `flask_app.py` on
  beta, and `templates/admin_manifesto_review.html` does **not** exist. Yet `templates/admin.html`
  (L307) still has a **live, active link** to `/admin/manifesto-review` — so it 404s. The status
  brief claims this link was "disabled 6 Jun"; on the beta tree it is still active. **Flagged, not fixed.**
- **Data-state contradiction (UNVERIFIED):** the `3cc992f` deploys row says "Labour renders
  775 manifesto excerpts across 24 policy areas," and ideas-backlog says party pages "now
  render manifesto content." But the manifesto status brief says **zero** chunks are approved,
  so the manifesto section should render on no page. Because display filters
  `review_status == "approved"`, these cannot both be true. Resolving this needs a direct
  production-Postgres query (see §3).

---

## 3. Pending data operations — require Mark's PC / DBeaver / prod access

Listed explicitly so nothing silently drops. **None of these were done this session** (recon-only,
production Postgres off-limits).

- **Verify production ManifestoChunk counts** per party (approved vs pending), and resolve the
  "775 excerpts vs zero approved" contradiction above. Status brief Q2.
- **Decide manifesto re-ingest vs review-in-place** once prod counts are known; the ingestion
  script is not in the repo, so re-ingest would need that script recovered/rebuilt first.
- **INC-007 follow-up (non-blocking):** ~20 ONS publications have NULL `first_published_at` —
  backfill.
- **4 paused stats producers** (Ofcom / OBR / OfS / Ofgem) — paused 8 Jun, 0 discovery;
  post-launch fix-or-deregister decision (production SQL).
- **Flip `STATS_CATALOGUE_ENABLED` on production** after live-verify (Railway env var, not DBeaver).
- **Production SQL discipline:** any of the above that touch prod must be logged in
  `docs/deploys.md` Production SQL log.

---

## 4. Git topology status

**Is `beta` currently a superset of `master`? NO.**

- Merge-base: `ca00919` (6 Jun). Both sides have diverged since.
- **5 commits on `master` not in `beta`** (confirmed non-equivalent by `git cherry` / patch-id):
  the 9 Jun Stats Catalogue "ships dark" launch (`a27f42e`, `7427c21`, `e7b760e`, `e805b5e`)
  + the Issue A discovery-order promote (`14f0099`). These were *file-scoped* promotions, so the
  equivalent work on beta exists as different (flag-gated) commits — genuine commit-graph divergence.
- **~34 commits on `beta` not in `master`** — flag-gated stats catalogue, party-page Option B
  restructure, manifesto docs, `orientation.md`, and several production-SQL log entries.
- **`deploys.md` itself has diverged:** master's push-log has rows for 6–9 Jun (`b4212af`,
  `22c652d`, `5f7ebff`, `d2e8949`, `a171844`) that are **not** on beta. Master's deploys.md is
  ahead here; beta's is behind.
- **Reconciliation merge: did NOT happen.** The last `master → beta` reconciliation was `201a0de`
  (merged `ca00919`). The 6–9 Jun master work has not been merged back. A reconciliation is
  outstanding — **do not action without Mark** (CLAUDE.md: changes flow `feature → beta → master`;
  `master → beta` reconciliation is an occasional fix, not routine).
- **Real bidirectional template divergence to flag (not resolve):** in
  `session_detail.html` and `archive_pq_detail.html`, topic tags link to
  `/archive/theme/<slug>` on **master** but to `/archive/search?q=...` on **beta**
  (beta commit `4869539` deliberately changed this). Each side has a fix the other lacks.

### Branch hygiene (Task 1d)
- **Fully merged into both** (safe-to-delete candidates): `feat/discovery-worker-resilience`,
  `feature/minister-field-preload`, `feature/phase-1-8-data-url-piece-2b`,
  `feature/phase-1-9-spike`, `feature/research-tool-minister-db-migration`,
  `fix/discovery-null-cadence`.
- **Merged to beta, reverted, deferred:** `feature/stats-key-findings-slice`.
- **Stale / unmerged to either beta or master — worth Mark's review** (large divergence, some
  security-titled; confirm whether their content landed via other routes before deleting):
  `chore/post-phase-1-5-cleanup` (last 20 May, +611), `feature/questions-db-refactor`
  (5 May, +394), `feature/security-hardening` (27 Apr, +327), `debates-rework` (3 Apr, +98).

---

## 5. Known issues

- **No open incidents** — INC-001 to INC-007 all Resolved.
- **INC-003** (mobile search timeout) — soft-closed, not root-caused.
- **INC-007 follow-up** — ~20 ONS pubs with NULL `first_published_at` (non-blocking; §3).
- **Party pages contain incorrect content** — live but `noindex`; must be corrected/restructured
  before `noindex` removal (ideas-backlog).
- **Sinn Féin `_PARTY_SLUG_MAP` mojibake** — member counts may render zero; fix during any
  Sinn Féin party-page work (ideas-backlog).
- **Dead admin link** — `templates/admin.html` links to the removed `/admin/manifesto-review`
  route (404). See §2.
- **Manifesto ingestion script not version-controlled** — see §2.

---

## 6. Snapshot note

Generated 2026-06-10 by Claude Code (cloud session, recon + docs only — no code, no schema, no
data operations, no production access). This file is a derived snapshot of the sources named at
the top; treat those sources as authoritative where they disagree with this summary, and
regenerate rather than hand-editing if it drifts.
