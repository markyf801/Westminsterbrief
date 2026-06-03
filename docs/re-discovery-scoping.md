# Stats catalogue re-discovery — scoping inputs

**Status:** Committed pre-launch gate for `STATS_CATALOGUE_ENABLED`. Design
substantially worked out; build not started. Sequences BEFORE the /stats flag flip.
**Recorded:** 3 June 2026 (at the analysis-done / build-not-started seam, so the
scoping pass starts warm rather than re-deriving these).

This is the freshness machinery for the stats catalogue. It is NOT a parked idea
— it's a committed gate with the design substantially settled. The inputs below
were expensive to derive (compare pass → ONS staleness → freshness analysis →
classify-skip investigation); capturing them here keeps that chain from being
re-walked or, worse, the load-bearing ONS insight being missed.

---

## The problem

The discovery worker (`scripts/discovery_worker.py`) is a one-shot-per-producer
daemon: it processes each producer once (`pending → completed`) and never
re-scans a `completed` producer. So **new publications from already-discovered
producers are never found** — the catalogue freezes at build-time content per
producer. For a tool whose value proposition is "find current statistics", a
silently-frozen catalogue destroys credibility on day one (a civil servant
returning in 3 weeks sees weeks-old "latest" stats and concludes the tool is
abandoned).

This is **face (a)** of the freshness problem and it is the **pre-launch gate**.
Faces (b) ONS date-drift and (c) data-file extraction are few-weeks-after, not
launch gates — do not let the gate expand to "all freshness machinery".

---

## THE KEY STRUCTURAL POINT — ONS normalisation unifies faces a + b

This is load-bearing, not incidental. For ONS, normalising the URL to its
**dataset-ID** (not the versioned URL) doubles as the face-(b) date-refresh
trigger:

- **"New version of a known dataset-ID"** → the dataset already exists as a row →
  **UPDATE** its latest-release date (face b — the ONS date-drift fix).
- **"Unknown dataset-ID"** → genuinely new → **INSERT** a new row (face a — new-pub
  discovery).

So one detection step (normalise ONS URL → dataset-ID → known/unknown) solves
**both** ONS freshness faces at once. The re-discovery build should treat ONS
normalisation as solving two freshness faces, not one. (GOV.UK has no equivalent:
its `first_published_at` is write-once, so GOV.UK is face-(a)-only.)

---

## Design inputs (settled)

### Pre-classify dedup key
- **GOV.UK:** raw source URL (`cand.url`). At discovery the stored `pub.url`
  equals `cand.url`, so a pre-classify `filter_by(url=cand.url)` skip works.
- **ONS:** **normalised dataset-ID**, NOT the versioned URL. At discovery
  `cand.url` is the version href (`api.beta.ons.gov.uk/.../versions/N`), which
  differs from the stored canonical `www.ons.gov.uk/datasets/{id}` (the 28 May
  URL fix). So ONS dedup must extract and match the dataset-ID.

Needed because the current dedup key is **LLM-derived** (the slug comes from
`result["name"]`, the Gemini-normalised name), so dedup can only happen
post-classify. A raw-data key must be introduced to dedup before the Gemini call.

### Skip-BEFORE-classify is required
Current worker skips AFTER classify (`discovery/__init__.py:91` classify →
`:96` slug-from-name → `:102` existing-slug check). Re-running discovery over a
completed producer therefore re-classifies the **full** candidate list every
cycle (DfE returns 681 candidates = 681 Gemini calls per run). The re-discovery
build MUST move the skip before the Gemini call, keyed on the raw dedup key
above, so cost ≈ only genuinely-new pubs.

### Separate re-discovery path — NOT reset-to-pending
Do **not** reset `completed → pending`. That overloads one flag with two meanings
(never-discovered vs checking-for-new) and is dangerous next to the INC-007
`first_published_at` correctness. Instead: a separate re-discovery path that
leaves `pending`/`completed` untouched and tracks its own cadence via a
`last_rediscovered_at` timestamp on the producer, selecting by timestamp rather
than by flag-flip. Initial-discovery and re-discovery stay distinct concerns with
distinct state.

### Cadence: daily, flat, all producers
NOT per-producer. Per-producer cadence's only justification is saving wasteful
re-classification, but the skip-before-classify guard already eliminates that
cost. Daily-flat matches the archive's established "current, not breaking-news"
cadence and means at most one day behind the source. Special-case a producer
later only if one misbehaves; don't build per-producer scheduling speculatively.

### INSERT-only-for-genuinely-new
Re-discovery INSERTs only for genuinely-new slugs/IDs. It must **never UPDATE an
existing GOV.UK `first_published_at`** (that field is write-once; the whole of
INC-007 was about getting it right). All date resolution uses the shared helper
`hansard_archive/discovery/pub_dates.py` as the single source of truth. The one
permitted UPDATE is the ONS latest-release date on a known dataset-ID (face b,
above) — and that too goes through the shared helper.

### Two-mechanism split (respects Decision H)
- **Re-discovery cron** — the new, load-bearing piece. Re-scans completed
  producers for new publications (and, for ONS, refreshes latest-release dates via
  the unified detection). Daily.
- **Downstream maintenance cron** — ONS date-refresh (if not folded into
  re-discovery via the a+b unification) + data-file extraction (piece 4).

This keeps **Decision H** intact: data-file extraction stays SEPARATE from
discovery so an extraction fetch timeout can't block/slow a discovery run. A
single cron doing discover → refresh → extract would revive exactly the coupling
H rejected.

Note: the ONS a+b unification means ONS date-refresh naturally belongs in the
**re-discovery** mechanism (it falls out of the same dataset-ID detection), not the
downstream maintenance cron. The maintenance cron is then primarily data-files
(piece 4). Confirm this placement during the scoping pass.

---

## Sequencing

Re-discovery (face a) is a **committed pre-launch gate** — it must be live before
`STATS_CATALOGUE_ENABLED` flips on production. Faces (b) and (c) are few-weeks-
after and do not gate the flip (though the ONS a+b unification means face b may
land with re-discovery anyway).

Not bolted onto Phase 1.9's display work (done) — this is distinct freshness
machinery. Call it 1.9.5 or fold into the piece-4 work area; the label matters
less than the sequencing (before the flag flip).

---

## Cross-references
- `docs/phase-1-9-scoping.md` — ONS date semantic, the staleness finding, INC-007.
- `docs/ideas-backlog.md` — piece-4 steady-state extraction cron (the downstream
  maintenance mechanism; cross-referenced from there to here).
- `docs/incident-log.md` — INC-007 (the `first_published_at` correctness this must
  preserve).
