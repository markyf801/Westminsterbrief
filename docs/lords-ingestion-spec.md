# Lords Hansard Ingestion — Design Spec

**Status:** Design-locked, build deferred to Phase 2A Week 3  
**Investigated:** 30 April 2026  
**Decision:** Lords in Phase 2A scope; pipeline adaptation starts in Week 3 after Commons public pages are built

---

## Summary

The Lords Hansard API is structurally identical to Commons. Chain-walking, item flattening, and the session model are all fully reusable. The main work is classifier updates (Lords HRSTag taxonomy is flatter than Commons), two new container/anchor patterns, and one session-chain edge case (Grand Committee).

Realistic build estimate: **8–13 hours** across pipeline adaptation, 12-month backfill, theme tagging, and validation.

---

## API Structure — Confirmed Identical to Commons

| Aspect | Commons | Lords | Notes |
|---|---|---|---|
| Search endpoint | `/search/debates.json?house=Commons` | `/search/debates.json?house=Lords` | Same endpoint, different `house` param |
| Session fetch | `/debates/debate/{ext_id}.json` | `/debates/debate/{ext_id}.json` | Identical |
| Overview fields | Title, Date, Location, HRSTag, Next/PrevDebateExtId | **Identical field names** | 100% structural match |
| Chain-walking | Next/PreviousDebateExtId BFS | **Identical** | Same algorithm works |
| Items structure | Items[] + ChildDebates[] recursion | **Identical** | `_flatten_items()` reusable without changes |
| 25-result search cap | Confirmed | Confirmed | Same cap applies |
| URL pattern | `hansard.parliament.uk/Commons/...` | `hansard.parliament.uk/Lords/...` | `_build_hansard_url()` already handles Lords |

The `house` parameter already flows through the entire Commons pipeline (`ingest_date()`, `ingest_date_range()`, `_build_hansard_url()`). Switching to Lords is mostly a matter of passing `house="Lords"`.

---

## Reuse Assessment

| Component | Reusable? | Notes |
|---|---|---|
| `_flatten_items()` | ✅ 100% | Identical Items/ChildDebates structure |
| `_fetch_session_full()` | ✅ 100% | Same endpoint, same schema |
| `_collect_all_sessions_for_date()` | ✅ 100% | Same BFS algorithm |
| `_write_contributions()` | ✅ 100% | House-agnostic |
| `HansardSession` / `HansardContribution` models | ✅ 100% | `house` column already exists |
| `scripts/backfill_hansard.py` | ✅ with `--house` flag | Add `--house` param, default `Commons` |
| `scripts/run_tagging.py` + `tagger.py` | ✅ 100% | House-agnostic; tags by session content |
| `ingest_date()` / `ingest_date_range()` | ✅ with minor changes | See below |
| `_classify_from_overview()` | ⚠️ needs Lords branch | See classifier section |
| `_get_seeds_for_date()` | ⚠️ needs Lords anchor search | See Grand Committee section |
| `_CONTAINER_HRS_TAGS` | ⚠️ add hs_venue | See container section |

---

## Lords HRSTag Taxonomy — Much Flatter Than Commons

Commons has ~15 distinct HRSTags that drive precise classification. Lords uses primarily:

| HRSTag | Observed on | Notes |
|---|---|---|
| `NewDebate` | Almost everything | Generic Lords tag — bills, motions, oral questions, statements, debates. No fine-grained equivalent of hs_8Question, hs_2BillTitle, etc. |
| `hs_Venue` | Opening ceremony | Prayers session — chain head, minimal content (see below) |
| `null` | "Lords Chamber" aggregate | Container — same pattern as Commons "Commons Chamber" null-tag |

**Implication:** the Commons classifier's HRSTag branch will largely fall through to the title fallback for Lords sessions. This is acceptable — Lords sessions have descriptive titles ("Crime and Policing Bill", "Oral Questions", "Written Statements") that the title fallback handles correctly. The "other" proportion may be slightly higher for Lords than Commons because edge-case titles won't have a matching HRSTag branch.

**Unknown:** What other Lords HRSTags exist beyond `NewDebate` and `hs_Venue`? Only two sitting days were probed. There may be Lords equivalents of `hs_2cStatement` (written ministerial statements), `hs_8Question` (oral questions), `hs_2BillTitle` (bill debates). The classifier should handle these if they appear (the Commons branches already match these tags). A wider HRSTag survey across 10+ Lords sitting days during container validation would catch any surprises.

---

## Container and Anchor Patterns — Lords-Specific

### 1. `hs_Venue` — Opening Ceremony (Anchor)

**Confirmed by direct API probe (2026-04-22):**

- ExtId: `26042254000163` (numeric format — distinct from GUID-format content sessions)
- HRSTag: `hs_Venue`
- Title: `House of Lords`
- Location: `Lords Chamber`
- **PreviousDebateExtId: `""` (empty) — chain head**
- **NextDebateExtId: `B21CEB72-...` — points to first real debate ("Low-carbon Heat Networks")**
- Items: 4 items — column number, date marker, timestamp, prayers text ("Prayers—read by the Lord Bishop of Chichester.")

This is a chain-head anchor. It has minimal content (prayers + metadata) rather than zero content, but is not substantive parliamentary debate. `_flatten_items()` would pick up the prayers text as a "contribution" — not useful.

**Handling:** Add `"hs_venue"` to `_CONTAINER_HRS_TAGS`. Detection is clean and reliable.

**Note on numeric vs GUID ExtIds:** hs_Venue sessions consistently use numeric-style ExtIds (e.g. `26042254000163`). Content sessions use GUIDs. This pattern may be useful as a secondary detection signal but should not be relied on alone.

### 2. Null-tag "Lords Chamber" — Aggregate Container

Same pattern as Commons null-tag "Commons Chamber" — aggregate session that duplicates child contributions. Our existing code already handles this:

```python
or (not hrs_tag and title.lower() in {"commons chamber", "westminster hall"})
```

**Change needed:** Add `"lords chamber"` to this set.

```python
or (not hrs_tag and title.lower() in {"commons chamber", "westminster hall", "lords chamber"})
```

### 3. "Introductions" — New Peer Ceremonies (Probable Anchor)

Lords introduction ceremonies (new peers taking their seats) appear as debate sessions in the Hansard record. Based on the session type, they likely contain only ceremonial text (oaths, signatures) rather than substantive speech.

**Status: probable anchor, not yet confirmed by direct probe.**  
Action before build: fetch one "Introductions" session and check Items content and word count. If ceremonial-only, add `"introductions"` to `_ANCHOR_TITLES`.

### 4. No Lords Equivalent of hs_6bDepartment / hs_3OralAnswers

Lords has no departmental oral questions structure. Each Lords sitting day has a single "Oral Questions" block (30 minutes, typically 4 topical questions from any Lord). There is no nested department-header container pattern. The four Commons duplicate-content containers (hs_6bDepartment, hs_3MainHdg, hs_3OralAnswers, hs_6bPetitions) have no Lords equivalents.

---

## Grand Committee Chain — Key Finding

**Confirmed by direct API probe (2026-04-27):**

Grand Committee is NOT a completely separate linked-list chain like Westminster Hall. Key differences:

| | Westminster Hall (Commons) | Grand Committee (Lords) |
|---|---|---|
| In plain-date search results? | **No** — crowded out by 25+ CC sessions | **Yes** — appears in general search when total ≤ 25 |
| Internal chain? | Yes — its own chain | Yes — its own internal chain |
| Cross-links to main chamber? | No | No |
| Secondary search needed? | Always (WH always crowded out) | Sometimes (only when total sessions > 25) |
| Wrapper session chain links? | N/A | Empty (wrapper has no Next/Prev links) |

**The Grand Committee wrapper session** (ExtId: `973e6fbd-c73f-4100-ab21-9601d2b72f55`) has **empty** `NextDebateExtId` and `PreviousDebateExtId`. It is isolated from the chain-walking BFS. The substantive Grand Committee sessions (child sessions, GUID ExtIds) form their own internal chain and DO have chain links to each other.

**On 2026-04-27:** plain-date search returned 25 results (16 Lords Chamber + 5 Grand Committee + 4 other). Grand Committee sessions were discoverable because total count was at the cap. On a busy day with 20+ Lords Chamber sessions, Grand Committee sessions could be crowded out.

**Recommended handling:** Add secondary `"Grand Committee"` anchor search to `_get_seeds_for_date()` for Lords, equivalent to the Westminster Hall anchor fix for Commons. This costs one extra API call per Lords sitting day and eliminates the edge-case crowding risk.

**One unresolved question before building the anchor search:** When a secondary search for `"Grand Committee"` is run, does it return the wrapper session (empty chain links — useless for BFS) or individual Grand Committee child sessions (have chain links — useful)? This needs one targeted API call on a day with Grand Committee to confirm. If it returns the wrapper, the anchor search strategy needs adjustment — either extract child ExtIds from the wrapper's `ChildDebates` array, or search with a more specific term that returns individual sessions.

---

## Classifier Changes Needed

```python
def _classify_from_overview(title: str, location: str, hrs_tag: str, house: str = "Commons") -> str:
```

Add a `house` parameter (default `"Commons"` for backwards compatibility). Add Lords-specific location check:

```python
# Lords Grand Committee — committee stage in Moses Room
if "grand committee" in loc:
    return DEBATE_TYPE_COMMITTEE_STAGE
```

**No other classifier changes needed for initial Lords build.** The existing title fallback handles Lords session types well:
- "Oral Questions" → `oral_questions` (via title fallback keyword match)
- "Crime and Policing Bill" → `debate` (via `"bill"` keyword)
- "Written Statements" → `ministerial_statement` (via title fallback)
- "Arrangement of Business" → `other` (correct — procedural)
- "Statutory Instrument" / "Regulations" → `statutory_instrument` (via title fallback)

Lords has no PMQs equivalent. The PMQs detection branch (`t == "engagements"`) will never fire for Lords — harmless.

---

## New container/anchor code changes summary

```python
# _CONTAINER_HRS_TAGS — add:
"hs_venue",        # Lords opening ceremony (Prayers) — chain head, ceremonial content only

# null-tag container titles — extend:
or (not hrs_tag and title.lower() in {"commons chamber", "westminster hall", "lords chamber"})

# _ANCHOR_TITLES — add after confirming content:
"introductions",   # Lords peer introduction ceremonies — ceremonial, no substantive speech
```

---

## Debate Type Vocabulary — Lords Mapping

| Lords session type | Frequency | Mapped type | Detection method |
|---|---|---|---|
| Oral Questions | Daily | `oral_questions` | Title "Oral Questions" (fallback) |
| Questions for Short Debate | Several/week | `debate` | Title contains "Question" (fallback) |
| Bill debates (2nd reading, report, 3rd reading) | Variable | `debate` | Title contains "Bill" (fallback) |
| Grand Committee | Several/week | `committee_stage` | Location "Grand Committee" (new branch) |
| Written Statements | Variable | `ministerial_statement` | Title / `hs_2cstatement` tag |
| Business of the House | Daily | `other` | Title fallback → other |
| Introductions | Occasional | excluded (anchor) | `_ANCHOR_TITLES` |
| Opening Prayers | Daily | excluded (anchor) | `hs_Venue` tag |
| Lords Chamber aggregate | Daily | excluded (container) | null-tag + "lords chamber" title |

No Lords equivalent of: `pmqs`, `westminster_hall`, `petition`.

---

## Time Estimates

| Phase | Estimate | Variance driver |
|---|---|---|
| Pipeline adaptation (classifier + container + anchor + Grand Committee anchor search) | 2–4 hours | Grand Committee wrapper question (see above) |
| Container sweep (validate all Lords hrs_tag/location combos post-backfill) | 1–2 hours | Could be 0 surprises or 2 new patterns |
| 12-month Lords backfill (~150 sitting days × 12–18 sessions) | 2–3 hours wallclock | Background job — no manual time |
| Theme tagging (~1,200–1,800 taggable Lords sessions) | 1.5–2 hours wallclock | Background job — no manual time |
| Validation (sample-check 50 tagged sessions, confirm container exclusions) | 2–3 hours | May require prompt tuning |
| **Total active dev time** | **5–9 hours** | |
| **Total wallclock (inc. background jobs)** | **8–13 hours** | |

---

## Pre-Build Checklist (do before writing any code)

- [ ] **Grand Committee anchor search behaviour** — run one targeted API call: search for "Grand Committee" on a Lords date where GC was sitting. Does the search return the wrapper session (GUID `973e6fbd-...`) or individual child sessions? Determines whether the secondary anchor search is viable or needs a different approach (wrapper ChildDebates extraction).

- [ ] **"Introductions" content check** — fetch one "Introductions" session and examine Items content. Confirm whether it's anchor-appropriate (ceremonial only) or has substantive speech.

- [ ] **Lords HRSTag survey** — run container sweep diagnostic on a 2-week Lords sample. Group by (hrs_tag, location), order by avg contribution count, check top combinations for container patterns. Takes ~30 minutes, eliminates surprises during the main build.

---

## Risks

| Risk | Severity | Notes |
|---|---|---|
| Flat HRSTag taxonomy | Low | Falls to title matching — Lords titles are descriptive, works for 9-type vocab |
| Grand Committee crowding (> 25 total sessions) | Low-medium | Secondary anchor search mitigates; unresolved question about wrapper vs child seeding |
| Unknown container patterns | Low | Same diagnostic approach as Commons worked well; Lords is simpler structurally |
| "Introductions" content | Low | Unconfirmed but easily handled once checked |
| Lords member name normalisation | Low for ingestion | `member_name` stored raw — normalisation is a future display concern, not ingestion |
| Peer title matching in future MP-page work | Medium | Deferred — not a Phase 2A blocker |

---

## Post-Build Findings — Lords Pipeline (30 April 2026)

### Corpus stats after 12-month Lords backfill + reclassification pass

| Category | Count |
|---|---|
| Total Lords sessions ingested | 1,901 (incl. containers) |
| Non-container sessions (taggable) | 1,898 |
| Containers (excluded from tagging/public pages) | 3 |
| Errors during backfill | 0 |
| Sitting days (12 months) | 164 |

**Post-reclassification debate_type distribution (non-container):**

| debate_type | Count | Notes |
|---|---|---|
| other | 1,099 | 57.9% — dominated by unclassifiable OQ/MS (see gap below) |
| debate | 426 | Bills, Questions for Short Debate, general debates |
| committee_stage | 229 | Grand Committee (Moses Room) |
| statutory_instrument | 144 | After reclassification pass (was 2) |

### Reclassification pass — 216 sessions corrected

Three fixes applied via SQL UPDATE pass after the 12-month backfill (30 April 2026):

| Fix | From | To | Count | Detection |
|---|---|---|---|---|
| Fix 1 | `other` | `statutory_instrument` | 67 | `_MADE_SI_RE` on titles in `other` bucket |
| Fix 2 | `debate` | `statutory_instrument` | 75 | `_MADE_SI_RE` before `"amendment"` keyword — SIs with "(Amendment)" in title were misclassified |
| Fix 3 | `committee_stage` | `other` | 74 | Procedural title override (`_PROCEDURAL_TITLE_STARTS`) for AoB sessions landing in GC via location check |

`ingestion.py` updated with both `_MADE_SI_RE` and `_PROCEDURAL_TITLE_STARTS` before the reclassification pass, so future ingestion produces correct classifications without needing a SQL pass.

---

## Known Classification Gap — Lords Oral Questions and Ministerial Statements

**Status: documented gap, not a blocker for Phase 2A launch. Phase 2A.5 task.**

Lords oral questions and ministerial statements are not separately classifiable from current Hansard API signals at launch. They remain searchable via full-text and theme tagging but do not appear under their respective `debate_type` filters (`oral_questions`, `ministerial_statement`). These sessions are stored with `debate_type='other'`.

**Root cause:** The Lords HRSTag taxonomy is almost entirely `NewDebate` — there are no Lords equivalents of `hs_8Question` (oral questions) or `hs_2cStatement` (written ministerial statements) found in the 12-month corpus. Session titles are the only available signal, and `"Oral Questions"` as a title is not reliably distinct from other question-type sessions (Questions for Short Debate, Private Notice Questions, Urgent Questions) without additional context.

**Rejected proxy:** Contribution-count bucketing (sessions with 16–30 contributions as an OQ proxy) matches ~91% of expected OQ sessions but rejects the principle that parliamentary procedure classification should come from parliamentary signals, not statistical correlations. This proxy was considered and explicitly rejected (Mark, 30 April 2026): "contribution-count buckets aren't a parliamentary procedure signal — they're a correlation."

**Impact at launch:**
- Lords oral questions are searchable by keyword and theme but will not appear under an "Oral Questions" filter
- Lords ministerial statements are searchable by keyword and theme but will not appear under a "Ministerial Statements" filter
- Both session types are displayed correctly on public pages; the gap is filter-only

**Phase 2A.5 task — investigate after launch, not before:**

Approaches to evaluate when revisited:
- Chain position — do OQs cluster at chain heads of each sitting day?
- Speaker turn structure — distinctive Q->A->Q->A pattern with named Lords
- Hansard API time-of-day metadata if available
- Lords Order Paper cross-reference if accessible

Time budget: 4 hours of investigation. If no reliable signal found in 4 hours, accept the gap permanently and document why.

Reasoning for deferral: better to investigate post-launch with real usage data, a larger corpus, and possible API evolution. Speculative investigation now risks wasted effort with no usage signal to validate against.
