# Research Tool — "Debate Contributions" tab: migrate off TheyWorkForYou

**Status:** Approved direction; crux data-check passed; build queued. Gets its
proper plan-mode session when it reaches its slot (next proper piece **after**
the stats catalogue reaches its launch-decision point).
**Recorded:** 5 June 2026 (at the crux-passed / build-not-started seam, so the
slot-time plan-mode starts warm).

This is the Research Tool DB migration audited 20 May 2026, now confirmed clean
for the minister-by-name tab. NOT a parked idea — an approved migration with the
load-bearing data risk already cleared. Lives on the scoping shelf.

---

## The bug this fixes

On `/debates`, the **"Debate Contributions"** tab (search a minister by name →
find every debate they've spoken in → generate Q&A transcript + style analysis)
fails for **Baroness Smith of Malvern** with "Could not find … on TheyWorkForYou.
Try their full name." She's a DfE Skills minister and a canonical CLAUDE.md test
minister.

**Root cause:** this tab was never migrated off TWFY. The Hansard migration retired
TWFY for the *topic* tab only. `/debates_minister` still calls `lookup_twfy_person`
(TWFY `getLords`/`getMPs` name search) then `fetch_minister_debates` (TWFY). TWFY's
name search fails for **newer peers (created ~2023+)** — Baroness Smith (Jacqui
Smith, peer 2024) is exactly that case. Our member cache knows her; TWFY doesn't →
the failure. This is a whole **class** of new-peer failures, not one name.

---

## Crux data-check — PASSED (5 June 2026)

The migration only works if local contributions are keyed by `member_id`, not name
(else we'd move the Lords-name fragility from TWFY into our own DB). Confirmed:

- Lords contributions are **98.1% ID-keyed** — 84,538 with `member_id` vs 1,672 null.
- **Baroness Smith of Malvern specifically joins through to 1,472 contributions** by
  `member_id`.

So the exact minister who broke TWFY is fully reachable locally by ID. Remove TWFY,
query by `member_id`, bug gone for her and the whole new-peer class. **Clean swap,
not migration-plus-linking.**

---

## Approved migration scope (this tab only — `/debates_minister` + `/debates_minister_analyze`)

Key everything on `member_id`. No names in the resolution or query path.

1. **Member resolution:** dropdown selection → submit the Parliament/member ID →
   resolve via `cached_member`. **Require dropdown selection — no raw free-text
   fallback** (free-text is what reintroduces name fragility).
2. **"Every debate they spoke in":** query `ha_contribution` by `member_id` → group
   by `session_id` → `ha_session`. Replaces `fetch_minister_debates` (TWFY).
3. **Filters (topic / date / house):** apply as local query filters
   (`ha_session_theme` for topic, `ha_session.date`, `ha_session.house`).
4. **Transcript for Q&A analysis:** assemble from `ha_contribution.speech_text` for
   the selected sessions, instead of fetching transcripts from TWFY.
5. **Gemini style-analysis/briefing step UNCHANGED** — operates on the assembled
   transcript (free-toolkit factual/extractive use, Gemini only).

Functions/routes that lose their TWFY dependency: `lookup_twfy_person`,
`fetch_minister_debates`, `get_twfy_date_range` (for this tab),
`/debates_minister`, `/debates_minister_analyze`.

---

## Design decisions (locked at scoping)

- **Dropdown selection required** — no free-text minister name (kills name fragility).
- **Preserve existing session-grouping + minister-first ordering EXACTLY.** Do NOT
  realign with how the topic tab groups — that's a separate optional item, not part
  of this migration.
- **12-month rolling window honesty:** the local archive is a 12-month rolling window
  (locked decision). TWFY went back further. **Label results as a recent-window view**
  ("contributions in the last 12 months") — do NOT imply a complete career record.
- **Free-toolkit rule:** extraction/analysis stays **Gemini only**; no Claude in this
  path.

---

## Known limitation (not a blocker)

The **1,672 null-`member_id` Lords contributions (1.9%)** won't surface via the
ID-keyed query — no ID to match. Acceptable (98.1% clean-ID coverage beats fragile
name-matching). **Before build, run the quick-look query below** to see WHAT they are:
- Procedural / junk → ignore.
- A systematic gap (e.g. an early import batch that didn't link IDs, or a date range)
  → note as a small **later** backfill candidate, not part of this migration.
Just don't let 1.9% silently vanish unexamined.

```sql
-- Characterise the null-member_id Lords contributions (run in DBeaver, read-only)
SELECT date_trunc('month', s.date) AS month,
       count(*) AS null_id_contribs,
       count(DISTINCT c.member_name) AS distinct_speaker_strings
FROM ha_contribution c
JOIN ha_session s ON s.id = c.session_id
WHERE c.member_id IS NULL
  AND s.house = 'Lords'   -- adjust if house lives elsewhere
GROUP BY 1 ORDER BY 1;

-- And a sample of the actual rows:
SELECT c.member_name, s.date, s.title, left(c.speech_text, 120) AS speech_start
FROM ha_contribution c
JOIN ha_session s ON s.id = c.session_id
WHERE c.member_id IS NULL AND s.house = 'Lords'
ORDER BY s.date DESC LIMIT 40;
```

---

## Plan-mode discipline (CLAUDE.md — this is the gem + minister search)

- **Plan-mode before code, no quick patch.** Show Mark the plan/diff before the build
  runs. This touches minister search — historically where interaction bugs got
  introduced.
- **Verify after build (canonical case):** DfE + "student loan repayments" →
  **MacAlister (Parliament ID 5033) AND Baroness Smith of Malvern both appear,
  minister-first.** Plus confirm new-peer resolution works generally.

---

## Sequencing

Next proper piece **after** the stats catalogue reaches its launch-decision point.
A real visible bug on a flagship minister, but it's the gem — plan-mode it properly
at its slot, don't squeeze it in.

---

## Cross-references
- `docs/ideas-backlog.md` — Research Tool DB migration entry (the 20 May audit +
  full field table).
- CLAUDE.md — Parliamentary Research Tool design principles, canonical test case,
  Lords minister-substitution / name-normalisation notes.
