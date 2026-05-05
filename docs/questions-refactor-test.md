# /questions DB Refactor — Test Protocol

Branch: `feature/questions-db-refactor`

This protocol must be completed before merging to master.
Run each test against both the live API path (master branch) and the DB path (this branch).
Record pass/fail and any discrepancies in the Results column.

---

## What changed

`fetch_wq_pages()` and direct Parliament API calls replaced with `_search_pq_db()`,
which queries `ha_pq` using Postgres FTS on `question_tsv`.

Filters that work identically: keyword, department, date range, house, answered/unanswered.
Filters that return 0 results by design (not tracked in archive): `holding`, `withdrawn`.

---

## Test cases

### T1 — Broad keyword, no other filters

**Inputs:** subject=`student loans`, all other filters default  
**Expected:** 20+ results across Commons and Lords  
**Check:**
- [ ] Results appear (not empty)
- [ ] Results are relevant to student loans / loan repayment
- [ ] Sorted newest-first by tabled date

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T2 — Keyword + date range

**Inputs:** subject=`NHS waiting lists`, from=`2025-09-01`, to=`2025-12-31`  
**Expected:** Results tabled between Sep–Dec 2025 only  
**Check:**
- [ ] No results outside the date range
- [ ] Results contain NHS waiting list / waiting time content

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T3 — Department filter + answered only

**Inputs:** subject=`(blank)`, department=`Department for Education`, status=`Answered`  
**Expected:** Answered DfE questions, recent first  
**Check:**
- [ ] All results show DfE as answering body
- [ ] All results show answered status
- [ ] No unanswered questions in results

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T4 — Narrow keyword + department

**Inputs:** subject=`apprenticeships`, department=`Department for Education`  
**Expected:** DfE questions on apprenticeships  
**Check:**
- [ ] All results are DfE
- [ ] Results are relevant to apprenticeships
- [ ] Member names and party information visible

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T5 — House filter: Lords only

**Inputs:** subject=`climate change`, house=`Lords`  
**Expected:** Lords questions on climate only  
**Check:**
- [ ] All results show Lords as house
- [ ] No Commons results in list
- [ ] Role shows "Life Peer" not "MP for X"

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T6 — Unanswered filter

**Inputs:** subject=`(blank)`, department=`Home Office`, status=`Unanswered`,
  from=`2026-01-01`  
**Expected:** Recent unanswered Home Office questions  
**Check:**
- [ ] All results show unanswered status
- [ ] All results are Home Office
- [ ] Answer text field is empty

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T7 — Multi-subject (comma-separated)

**Inputs:** subject=`solar energy, wind energy`  
**Expected:** Results covering either solar or wind  
**Check:**
- [ ] Results from both topics appear
- [ ] No obviously irrelevant results

**API result count:** ___  
**DB result count:** ___  
**Notes:**

---

### T8 — Word export

**Inputs:** subject=`planning permission`, download format=Word  
**Expected:** .docx downloads without error  
**Check:**
- [ ] File downloads
- [ ] File opens in Word
- [ ] Contains search metadata header
- [ ] Questions and answers formatted correctly
- [ ] UIN and Parliament link included per question

**Pass/Fail:** ___  
**Notes:**

---

### T9 — CSV export

**Inputs:** subject=`benefit cap`, download format=CSV  
**Expected:** .csv downloads with correct columns  
**Check:**
- [ ] File downloads
- [ ] Opens in Excel with correct columns (UIN, Status, Department, Member, Party, Role, etc.)
- [ ] No encoding issues (special characters OK)

**Pass/Fail:** ___  
**Notes:**

---

### T10 — Download selected

**Inputs:** Run any search, tick 3–5 individual questions, download selected  
**Expected:** Selected-only .docx with those questions  
**Check:**
- [ ] Only selected questions appear in export
- [ ] No crash

**Pass/Fail:** ___  
**Notes:**

---

## Known differences (not bugs)

| Behaviour | API path | DB path |
|---|---|---|
| `holding` status filter | Returns holding-answer questions | Returns 0 (not tracked in archive) |
| `withdrawn` status filter | Returns withdrawn questions | Returns 0 (not tracked in archive) |
| `total_available` | Parliament API total (may be >1200) | DB COUNT of matching rows |
| Coverage | Live (includes today's new questions) | Archive (up to last cron run, ~3h lag max) |
| Speed | 5–30s depending on query | <2s |

---

## Pass criteria

All T1–T10 pass. Result count discrepancies of <20% on broad queries are acceptable
(explained by archive lag and holding/withdrawn exclusions). Discrepancies >20% on
a specific query must be investigated before merging.

---

## Sign-off

- [ ] All tests run
- [ ] Discrepancies investigated and explained
- [ ] Approved to merge: _______________  Date: _______________
