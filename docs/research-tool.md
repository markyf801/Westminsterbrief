# Parliamentary Research Tool — design notes and testing protocol

> Moved from `CLAUDE.md` on 2026-10-20. Read before working on
> `debate_scanner.py` / `templates/debate_scanner.html`. The core rules are
> summarised in `CLAUDE.md`; this file has the detail.
> Debate type taxonomy: `docs/parliamentary-debate-types.md`.

---

## Core architecture

Search finds debates → fetch all speeches from each debate → ministers are always present.
Do NOT rely on ministers' responses containing search keywords. They rarely do.

**The "no central debate database" problem:** there is no single index of "all
debates about topic X". The practical solution: find ONE matching speech via
keyword search → extract its debate GID → fetch the full debate session.
Implemented in `fetch_all_debate_sessions()` in `debate_scanner.py`.

When working on the Research Tool, always ask:
1. What is the user's department context? (e.g. DfE) — ministerial debates for that dept come first
2. Are we showing debates as a unit (all speakers) or individual speeches? Always prefer debates as a unit.
3. Does the current approach guarantee the responding minister appears? If not, fix it.

If the tool misses Baroness Smith of Malvern or other DfE ministers on a DfE
topic, something is architecturally wrong.

## Canonical test case — always verify this works

**Topic:** "student loan repayments" (or "repayment threshold")
**Department filter:** Department for Education
**Expected results (confirmed debates in 2026):**
- **Josh MacAlister OBE MP** — Parliamentary Under-Secretary of State (Minister for Children and Families), DfE. Has spoken on repayments in 2026. Parliament ID: 5033.
- **Baroness Smith of Malvern** — Minister of State for Skills, DfE (Lords). Has spoken on repayments in 2026; a substitute Lords peer may have covered some sessions.
- Both should appear in their respective debate sessions with minister-first ordering.

**Do not assume someone is a backbencher without checking GOV.UK/Parliament API first.**
MacAlister was once incorrectly identified as a backbencher — always verify role
before drawing conclusions. If either is missing after a search, investigate
before declaring a fix.

## Questions to ask at the start of a Research Tool session

1. Which department are you testing/using this for?
2. What specific debates or questions are you expecting to see that aren't appearing?
3. Is the issue "not finding the debate at all" or "found the debate but missing speeches from it"?
4. Which sections are currently working and which are broken?
5. Is the cache clear? (If stale data is possible, clear it at `/admin` before testing.)

## Minimum verified checklist — before declaring anything fixed

| Section | What to verify |
|---|---|
| **Oral Questions** | MacAlister or a DfE minister appears; DfE sessions not filtered out |
| **Written Questions** | No duplicate cards; `is_answered` correct; newest-first |
| **Debates / Westminster Hall** | Sessions grouped correctly; speeches visible |
| **Ministerial Statements** | DfE statements only when dept filter active |
| **Minister-led search** | At least one of MacAlister / Baroness Smith appears |
| **Word download** | Briefing downloads without crash; checkboxes work for all section types |
| **Checkboxes** | Present on Oral, WMS, WQ, Debates sections |

After deploying a fix: clear cache at `/admin` → run the canonical test case live
→ confirm the specific broken thing works → check adjacent sections for regressions.

## Current invariants — must hold

- WQ cards show the question text as the question, not the minister's answer
- WQ deduplication by UIN — no duplicate cards
- `is_answered` set correctly based on answer HTML being non-empty after stripping
- Lords oral questions classified correctly via title pattern + word count threshold
- Minister search uses the AI-expanded query, not the raw topic
- WMS department filter is title-match based, not full-text
- Checkboxes present on Oral, WMS, WQ, and Debates sections
- Minister-led search via Hansard backend works for the canonical test case

## Feature flag

- `SEARCH_BACKEND=hansard` → Hansard API for minister search (production path)
- Unset → TWFY for all searches (original behaviour)

## Parliamentary structure knowledge

- Ministers rarely use the exact policy keywords — they use government framing language
- Oral Questions: minister gives prepared answer first, then supplementaries — show both
- Written Questions: Q and A are separate items in the API — always show the answer alongside the question
- Debates are the unit of meaning, not individual speeches
- Lords ministers are easy to miss — name normalisation must handle "Baroness X of Y" patterns

## Minister substitution — a real-world complication

Ministers are regularly absent (illness, clashes, recess duties) and covered by
substitutes. This breaks name-based minister detection.

**Lords:**
- Each department has a Lords spokesperson (e.g. Baroness Smith of Malvern for DfE)
- If they are absent, ANY government peer may cover — a whip, a Lord from another dept, or a junior minister
- The covering peer will NOT appear in GOV.UK's department minister listing
- Hansard gives NO indication they were covering — it just shows their name

**Commons:**
- Secretary of State may answer instead of Minister of State
- A PPS or adjacent minister may cover a session

**Implications:**
- Do NOT rely solely on GOV.UK department listings to identify ministerial voices in Lords debates
- A government peer (Lord/Baroness in Waiting, any government whip) speaking in a department-topic Lords debate should be treated as a probable departmental spokesperson
- Name-based minister search misses debates where a substitute spoke
- Topic search → full session fetch is MORE reliable than name search for Lords
- Consider flagging government peers broadly in Lords results, not just the named minister
