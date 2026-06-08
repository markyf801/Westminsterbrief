# Per-producer stats pages — approved implementation plan

**Status:** approved, build-ready. Build slots in at **step 4** of the stats-catalogue
launch sequence (after producers are authorised + populated + verified). Captured
8 Jun 2026 so the locked decisions survive the multi-step launch sequence.

SEO play: a dedicated, indexable page per producer (~21 new individually-rankable
pages targeting "[Producer] statistics" searches) from data already held. The main
`/stats` catalogue is unchanged — these are additive.

---

## Locked decisions

- **URL:** `/stats/producer/<slug>` — the `producer/` prefix deliberately avoids the
  existing `/stats/<theme_slug>` policy-area route (which lives in `views.py`, the
  Manifesto console's file). This keeps the build entirely out of `views.py`.
- **Slug source:** `ha_stat_producer.slug` — already clean, readable, unique
  (`uq_stat_producer_slug`), stable, and already embedded in indexed URLs via the
  detail route `/stats/<producer_slug>/<pub_slug>`. No migration.
- **Timing:** fold into launch (catalogue + producer pages + cross-links + sitemap
  all live together in one complete crawl).
- **Thin-content gate (structural, not vigilance):** a producer page exists only if
  the producer has ≥1 visible publication. The same has-publications gate guards all
  three surfaces, so an empty page is impossible by construction and self-heals when
  a producer later populates:
  1. **Route:** `total == 0 → abort(404)` — no page for an unpopulated producer.
  2. **Cross-link:** the catalogue's existing `producers` query already lists only
     producers-with-publications — no link to an empty page.
  3. **Sitemap:** has-publications gate — no sitemap entry for an empty page.

---

## Implementation (5 files)

### File 1 — `stats_catalogue.py` (new route) — *Stats-lane*
`@stats_catalogue_bp.route("/stats/producer/<slug>")`. Mirrors `catalogue_list`
scoped to one producer. The blueprint's existing `before_request` gate
(`_check_enabled`) auto-applies → flag-gated by `STATS_CATALOGUE_ENABLED` for free.
- Resolve producer: `StatProducer.query.filter_by(slug=slug, authorisation_status="authorised").first_or_404()`.
- Publications: filter `producer_id` + `StatPublication.authorisation_status.in_(["candidate","authorised"])` — the **same visibility filter** as the catalogue.
- `total = count`; **`if total == 0: abort(404)`** (thin-content gate).
- Sort: `first_published_at.desc().nullslast(), id.desc()` (identical to catalogue). Paginate at `_PER_PAGE` (25).
- Compute `date_min, date_max` via `func.min/max(first_published_at)` for the fallback context (these ignore nulls).

### File 2 — `templates/stats_producer.html` (new) — *Stats-lane*
Reuse the catalogue's publication-card markup: **extract the card block from
`stats_catalogue.html` into a shared partial `_stats_pub_card.html`** and
`{% include %}` it from both (single-sourced rendering). New template adds only the
producer header:
- `<title>`: `{{ producer.name }} — Statistical Publications | Westminster Brief`
- meta description + canonical `/stats/producer/{{ producer.slug }}` (no trailing slash).
- H1 = `{{ producer.name }}`.
- **Context paragraph:** `producer.description` if set, else the templated fallback (below).
- Breadcrumb: `Hansard › Statistics › {{ producer.name }}`, Statistics crumb → `/stats`.
- Server-side Jinja → content on load, indexable by construction.

### File 3 — `templates/stats_catalogue.html` (cross-link) — *Stats-lane*
Add a **"Browse by producer"** section using the `producers` list already passed to
the template (already filtered to producers-with-publications), each linking to
`/stats/producer/<slug>`. Otherwise the catalogue is untouched (additive only).

### File 4 — `flask_app.py` `_build_sitemap_core_xml` (sitemap) — ⚠️ SHARED FILE
Add ~21 producer URLs, gated twice:
```python
if os.environ.get("STATS_CATALOGUE_ENABLED"):
    for p in _producers_with_publications():   # reuse the catalogue's query
        add_url(f"{BASE}/stats/producer/{p.slug}", changefreq="weekly")
```
Flag gate → out of the sitemap pre-launch; has-publications gate → never an empty
page. **Coordination:** `flask_app.py` is shared with the Manifesto console — flag +
confirm they're not mid-edit before touching. The sitemap is cached (`_serve_xml`
TTL) — see flag-flip note below.

### File 5 — `tests/test_stats_producer_pages.py` (new) — *Stats-lane*
Uses the `:memory:` conftest, flag forced on:
- Populated producer → 200; name in `<title>` and `<h1>`; ≥1 card.
- Producer with 0 visible pubs → 404 (thin-content gate).
- Unknown / declined / unauthorised slug → 404.
- Flag off → coming-soon (`before_request`).
- `/stats` contains the "Browse by producer" links.
- Two distinct producers → distinct `<title>`/`<h1>`.
- **Regression: main `/stats` catalogue renders identically after the card-partial
  extraction** (same publications, order, controls) — guards the validated artefact.

---

## Content (locked)

### `producer_type` → human label (article baked in; sentence reads "{name} is {label} that…")
| `producer_type` | Label |
|---|---|
| `central_department` | a central government department |
| `executive_agency` | an executive agency |
| `ndpb` | a non-departmental public body |
| `regulator` | a regulator |
| `gss_producer` | **a national statistics producer** |
| `other_public_body` | a public body |
| `devolved_administration` | a devolved administration *(declined — won't render)* |
| `devolved_body` | a devolved public body *(declined — won't render)* |

### Fallback context paragraph (fires only when `description` is null)
> **{{ producer.name }} is {{ label }} that publishes UK official statistics.
> Westminster Brief tracks {{ total }} statistical publication{{ 's' if total != 1 }}
> from {{ producer.short_name or producer.name }}{{ date_clause }}.**

`date_clause` (three cases; `func.min/max` ignore nulls):
- both dates, equal → `, published on {{ date_max | display_date }}`
- both dates, differ → `, published between {{ date_min | display_date }} and {{ date_max | display_date }}`
- no dates → omit the clause entirely

Verb is "tracks" (conveys the living/current nature — re-discovery keeps it current).
Singular/plural via `{{ 's' if total != 1 }}` (gate is ≥1, never "0").

### Loose-fit producers — seed a `description` so the generic "official statistics" verb doesn't fire
The generic verb is inaccurate for bodies publishing analysis/forecasts/charity data
rather than accredited official statistics. **Two in-scope cases: OBR and UCAS.**
- **OBR** (OGL/gov, authorised) — seed always:
  *"The Office for Budget Responsibility provides independent analysis of the UK's
  public finances, including the official economic and fiscal forecasts that inform
  the Budget."*
- **UCAS** (charity, custom non-OGL licence) — seed **only if** the licence check
  (below) clears it for authorisation:
  *"UCAS operates the centralised admissions service for UK higher education and
  publishes statistics on applications, offers and acceptances."*

Apply via scoped DBeaver `UPDATE` (`description = …, updated_at = NOW()`, HESA-pattern)
**and** add to `build_seed_data()` for permanence. Surface the exact SQL at step 4.

---

## The one open judgment — UCAS licence (decided AT Wave 2 authorisation, before any page)
UCAS has a **custom non-OGL licence** — a surfacing-rights question, same shape as
HESA. Everything else is OGL (which is *why* we can freely list/link). Before
authorising UCAS: read its licence and confirm it permits WB to **list publication
metadata (titles, dates) AND link to source** (WB only lists+links, never reproduces
the underlying data — so the question is metadata-listing + linking, not reproduction;
many non-OGL licences permit that while restricting reproduction).
- Permits list+link → authorise UCAS, seed its description, it gets a page.
- Restricts even that, or unclear → **decline UCAS** (`authorisation_status='declined'`,
  HESA precedent). No page, no rights problem, wording moot.

Rationale: if UCAS is authorised → page built → launched → and the licence later turns
out not to permit surfacing, that's published-without-the-right on a public tool. Check
first; it's free and either clears or declines cleanly.

---

## Launch sequence (build is step 4)
1. **Decline** ScotGov / WelshGov / NISRA (devolved exclusion — "no devolved coverage yet").
2. **Wave 1** — authorise GOV.UK-org producers (Ofsted, Ofgem, UKHSA, OBR…) → initial discovery → verify populated.
3. **Wave 2** — authorise own-domain producers (NHSE, OfS, Ofcom, UCAS*) → initial discovery → verify populated. *UCAS gated on the licence check above.
4. **← BUILD the per-producer pages** (producers now populated; the gate covers any that didn't).
5. **Verify** producer pages render WITH content (spot-check, esp. own-domain parsers); sitemap lists only populated producers; cross-links resolve; **main `/stats` catalogue renders unchanged** after the partial extraction.
6. `/stats` disclaimer.
7. **Flip `STATS_CATALOGUE_ENABLED`** → full surface live, one complete crawl.
   - **Flag-flip atomicity check:** all four surfaces read the same env var, but the
     sitemap is cached (`_serve_xml` TTL). At flip, confirm the sitemap cache is
     cold / force-regenerate so it doesn't lag the live pages by up to the TTL.

---

## Coordination
- **Stats-lane:** Files 1, 2, 3, 5 (`stats_catalogue.py`, two templates, tests).
- **One shared touch:** File 4 (`flask_app.py` sitemap) — flag + confirm Manifesto
  isn't mid-edit before editing.
- **`views.py` untouched** — guaranteed by the `producer/` prefix.

**Effort:** ~half-day. No new data, no `views.py`, no migration.

---

## Sibling: per-policy-area stats pages (`/stats/<theme_slug>`) — build at step 4

Same feature from the other axis. Per-producer = "who published it"; per-policy =
"what it's about" — *"all Health and social care statistics, across every producer"*.
Arguably the more valuable axis for a policy person (their topic across all sources).
Build it **together** with the per-producer pages at step 4 so both genuinely share
the `_stats_pub_card.html` partial (created once, used by both) rather than building
one ahead of the other and retrofitting.

**The 500 is already cleared** (commit `b57a14a`, 8 Jun): `stats_theme.html` was
recreated as the original (headline-stat + upcoming releases), template-only, dead
links dropped. So `/stats/<theme_slug>` renders 200 today. **This section is the
ENHANCEMENT only** — adding the cross-source publication list. Net-new: the original
template never had a publication list.

**Scope of the enhancement (net-new vs the current restored page):**
- A publication list on each policy-area page: all publications tagged to that policy
  area, across all producers, using the shared `_stats_pub_card.html` partial.
- Same quality bar as per-producer: distinct `<title>`/H1/context, on-load server-side
  render (indexable), breadcrumb, thin-content gate.
- **Thin-content gate:** 0 visible publications → 404 (consistency). Data is healthy —
  2,301/2,308 publications tagged; the 22 populated areas range 539 (Employment) down
  to 3 (International development) — so the gate rarely if ever fires, but apply it.

**This one DOES touch `views.py`** — unlike the per-producer pages (which the
`producer/` prefix keeps out of `views.py`). The `stats_theme` handler
([`views.py`](../hansard_archive/views.py) ~L2556) must gain:
- A `StatPublicationTheme` query (theme_type='policy_area') joined to `StatPublication`,
  resolved via the **brief-slug → Hansard-name bridge** `_pa.hansard_names_for_slug()`
  (the handler already uses `_pa`). This matters: the `/stats/` brief-slug ≠ the
  `ha_session_theme`/`StatPublicationTheme` policy name in **17 cases** (e.g.
  `/stats/education` → *"Education, training and skills"* **and** *"Children and
  families"* — one slug, two areas). Naive slugify will not match.
- The thin-content gate.
- → **Manifesto-console coordination + serialised master-promotion** required at build
  time (shared `views.py`). This is the one difference from the per-producer lane's
  "`views.py` untouched" guarantee.

**Namespace:** `/stats/<theme_slug>` (1-seg, this) coexists with `/stats/producer/<slug>`
(per-producer) — Werkzeug ranks the literal `producer` segment above the `<theme_slug>`
converter, so no collision. Edge case to verify when per-producer lands: a bare
`/stats/producer` (no second segment) falls through to the theme route → `producer`
isn't a valid policy slug → `abort(404)`, harmless.
