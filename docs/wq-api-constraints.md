# Parliament Written Questions API — constraints and examples

> Moved from `CLAUDE.md` on 2026-10-20 (`docs/api-reference.md` §3 points here
> via CLAUDE.md). Read before changing any tracker or WQ-related code.
> The history of the April 2026 wrong constraints is in `docs/history/decisions.md`.

The API at `questions-statements-api.parliament.uk/api/writtenquestions/questions`
is documented in its OpenAPI spec at
`https://questions-statements-api.parliament.uk/index.html`. That spec is
authoritative. The constraints below were verified against it and by live
testing in April 2026.

## Confirmed working parameters (verified April 2026)

| Parameter | Type | Behaviour |
|---|---|---|
| `tabledWhenFrom` | date `YYYY-MM-DD` | Questions tabled on or after this date. Works reliably. |
| `tabledWhenTo` | date `YYYY-MM-DD` | Questions tabled on or before this date. Works reliably. |
| `answered` | `Any` / `Answered` / `Unanswered` | Server-side answered filter. Works reliably. |
| `answeringBodies` | integer (dept ID) | Works reliably **with** `tabledWhenFrom`. Without a date anchor it full-table scans and times out. |
| `house` | `Commons` / `Lords` | Works. |
| `searchTerm` | string | Full-text search. Works. |
| `take` / `skip` | integer | Pagination. Works. |
| `questionStatus` | `NotAnswered` / `AnsweredOnly` / `AllQuestions` | Alternative to `answered`. Works. |

| `answeredWhenFrom` / `answeredWhenTo` | date | Questions whose `dateAnswered` is in the range. Verified Oct 2026. |
| `correctedWhenFrom` / `correctedWhenTo` | date | Questions whose answer was corrected in the range (`dateAnswerCorrected`). Verified 22 Oct 2026: ~9 corrections in 11 days. |

Wrong names that the API (correctly) ignores: `tabledStartDate`, `tabledEndDate`, `isAnswered`.

## Holding answers and corrections (verified live, 22 Oct 2026)

- **Holding answer:** `answerIsHolding=true`, `dateAnswered` = `dateHoldingAnswer`.
  When the real answer arrives, `answerIsHolding` becomes false,
  `dateHoldingAnswer` keeps the holding date and `dateAnswered` becomes the
  real answer's date — **the same date when both land on the same day**, so a
  date comparison alone misses the change.
- **Correction:** `answerIsCorrection=true`, `dateAnswerCorrected` set,
  `correctingMemberId` set, `answerText` is the corrected text,
  `originalAnswerText` the old one. **`dateAnswered` keeps the original date**,
  so an "answered in the last N days" window never finds a correction to an
  older answer — use `correctedWhenFrom`/`correctedWhenTo`.
- The bulk list endpoint includes all these fields (text cut at 258 chars).

## Examples

```python
import requests
from datetime import datetime, timedelta

url = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"

# All unanswered questions tabled yesterday
yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
resp = requests.get(url, params={
    'tabledWhenFrom': yesterday,
    'tabledWhenTo': yesterday,
    'answered': 'Unanswered',
    'take': 1000,
}, timeout=30)

# All unanswered DfE questions tabled in the last 7 days (~2s response)
week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
resp = requests.get(url, params={
    'tabledWhenFrom': week_ago,
    'answeringBodies': 60,   # DfE dept ID
    'answered': 'Unanswered',
    'take': 500,
}, timeout=30)

# Paginated fetch for high-volume days
all_results = []
skip = 0
while True:
    resp = requests.get(url, params={
        'tabledWhenFrom': yesterday,
        'tabledWhenTo': yesterday,
        'take': 500,
        'skip': skip,
    }, timeout=30)
    batch = resp.json().get('results') or []
    all_results.extend(batch)
    if len(batch) < 500:
        break
    skip += 500
```

## Tracker implementation pattern

Use server-side filtering, not a client-side workaround:

1. `tabledWhenFrom` = yesterday (walk back up to 7 days if 0 results — handles recess)
2. `answeringBodies` = selected department ID
3. `answered=Unanswered`
4. Paginate with `skip` (a busy post-recess day can exceed 1000 questions for a large department)

## Domain facts

- MPs and Lords cannot table written questions during recess. Each WQ has a real `dateTabled` on a sitting day.
- A typical sitting day produces 200–500 WQs across all departments and houses; heavy days 600–1200. Paginate rather than assuming one `take=500` covers a day.
- Recess detection: if a 7-day lookback returns 0 questions, show a banner — "Parliament not currently sitting — no questions tabled in the last 7 days" — rather than an empty page.
- **UINs reset each parliamentary session and are not globally unique** (see INC-008 in `docs/incident-log.md`). Do not treat UIN alone as an identity across sessions.
