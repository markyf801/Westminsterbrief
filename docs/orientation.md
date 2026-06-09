# Orientation — current project state

> Generated 2026-06-08 from: docs/deploys.md (recent tail), docs/ideas-backlog.md (Active), docs/per-producer-pages-plan.md, docs/incident-log.md.
> Derived snapshot, not a maintained narrative — if it looks stale, ask Code to regenerate it from the sources above before relying on it.
> How Mark & Opus work: docs/working-with-opus.md. Hard rules & constraints: CLAUDE.md.

## Where we are
Mid stats-catalogue launch. Per-producer pages (`/stats/producer/<slug>`) are built on **beta**, not yet on production; the live task is working the launch sequence to promote them. Party pages (manifesto Tier 3) are in production but noindexed pending content-accuracy fixes.

## Active priorities (in flight / near-term)
- **Stats catalogue launch** — per-producer pages + catalogue cross-links + source/licence disclaimer built on beta (steps 4–6). Remaining: verify → beta→master promotion → flip `STATS_CATALOGUE_ENABLED`. ~21 indexable producer pages from data already held (SEO play).
- **Gate before producer batch-authorisation:** triage 15 pre-existing test failures (test_producer_authorisation + seed-count).
- **GOV.UK discovery 422 fix** is in code but must reach production — cron is down until it ships, with a ~2-week discovery backlog to catch up.
- **Party pages before noindex removal:** fix restructuring + incorrect content; Sinn Féin mojibake in `_PARTY_SLUG_MAP` (member counts may render zero).

## Recently shipped to production (last ~2 weeks, master)
- 6 Jun — restored `ManifestoChunk` import; fixed every `/archive/party/<slug>` 500 (last deploy).
- 5–6 Jun — Research Tool Debate-Contributions migrated off TWFY; minister field preloads all ministers; re-discovery dedup + cron; repo-hygiene rules.
- 29 May–3 Jun — Phase 1.9 stat detail pages, catalogue date UI, per-producer date labels, ONS release_date/staleness fixes (INC-007).
- 8 Jun (production SQL, not pushes) — producer licence cleanup: declined 3 devolved + NISRA enrich; OfS/Ofcom licence cleared; UCAS decline; DCMS/MHCLG slug fix; paused 4 own-domain regulators.

## Open threads / caveats
- No open incidents (INC-001–007 all Resolved).
- INC-003 (mobile search timeout) — soft-closed, not root-caused.
- INC-007 follow-up (non-blocking): 20 ONS publications have NULL `first_published_at`.
- 4 stats producers (Ofcom/OBR/OfS/Ofgem) paused 8 Jun, 0 discovery — post-launch fix-or-deregister.

## Not the live phase (avoid confusion)
- `phase-1-9-scoping.md` = forward vision (local content extraction / per-publication pages), not being built.
- `phase-2a5-pq-answer-text-plan.md` = PQ answer caching; currently a backlog slice-to-test idea, not in flight.

## Where the detail lives
- Pushes + production SQL → `docs/deploys.md`
- Parked ideas + revisit triggers → `docs/ideas-backlog.md`
- Current build → `docs/per-producer-pages-plan.md`
- Incidents / regressions → `docs/incident-log.md`
- Hard rules & constraints → `CLAUDE.md`
- How Mark & Opus work → `docs/working-with-opus.md`
