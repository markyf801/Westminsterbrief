# Coding gotchas — worked examples

> Moved from `CLAUDE.md` on 2026-10-20. `CLAUDE.md` keeps the one-line rules;
> this file holds the code examples behind them.

---

## SQLite vs Postgres — queries that pass locally but fail in production

Local dev runs SQLite; production runs Postgres. These gaps are silent: local
tests pass, production 500s.

### 1. DISTINCT + ORDER BY — ORDER BY columns must appear in SELECT

```python
# WRONG — SQLite allows this; Postgres raises InvalidColumnReference
db.session.query(HansardContribution.session_id)
    .join(HansardSession, ...)
    .distinct()
    .order_by(HansardSession.date.desc())

# CORRECT — add the ORDER BY column to SELECT
db.session.query(HansardContribution.session_id, HansardSession.date)
    .join(HansardSession, ...)
    .distinct()
    .order_by(HansardSession.date.desc())
# Then extract only session_id: [r[0] for r in results]
```

### 2. GROUP BY — non-aggregated columns must be in GROUP BY

```python
# WRONG — Postgres rejects (title not in GROUP BY)
db.session.query(HansardSession.id, HansardSession.title, func.count(...))
    .group_by(HansardSession.id)

# CORRECT
db.session.query(HansardSession.id, HansardSession.title, func.count(...))
    .group_by(HansardSession.id, HansardSession.title)
```

Postgres allows other columns of a table when its primary key is grouped
(functional dependency), but explicit is safer.

### 3. Booleans

SQLite stores booleans as 0/1; Postgres has a native boolean. Raw SQL `= 1` /
`= 0` fails on Postgres. Use `IS TRUE` / `IS FALSE`, or ORM comparisons.

### 4. DISTINCT ON — Postgres only

Use a subquery or window function that works on both, or document the query as
Postgres-only.

### 5. Timezone-aware datetimes

Models use naive datetimes (`datetime.utcnow()`) with `TIMESTAMP` columns
throughout. Keep it consistent; don't mix in `TIMESTAMPTZ`.

---

## Raw SQL and `updated_at`

```sql
-- WRONG — updated_at stays at previous value
UPDATE ha_stat_producer SET authorisation_status = 'declined' WHERE id = 25;

-- RIGHT
UPDATE ha_stat_producer
SET authorisation_status = 'declined', updated_at = NOW()
WHERE id = 25;
```

## Postgres sequence desync after a Railway failover

Symptom: `duplicate key value violates unique constraint "ha_session_pkey"`.
Fix (Mark runs this, logged in `docs/deploys.md`):

```sql
SELECT setval(pg_get_serial_sequence('table_name', 'id'), (SELECT MAX(id) FROM table_name));
```

Run for each affected table (`ha_session`, `ha_contribution`, `ha_pq`, `ha_session_theme`).

---

## Long-running scripts on Railway Postgres — batch-by-ID

Railway kills idle Postgres connections after a few hours. SQLAlchemy's default
`expire_on_commit=True` expires ALL objects in the session on every commit. The
next attribute access triggers an autoflush on the dead connection and crashes.

```python
# DON'T
rows = db.session.query(Model).filter(...).all()   # loads 68k objects
for row in rows:
    row.field = ...
    if i % 50 == 0:
        db.session.commit()  # expires ALL 68k objects

# DO — batch-by-ID
last_id = 0
while True:
    batch = (db.session.query(Model)
             .filter(..., Model.id > last_id)
             .order_by(Model.id).limit(100).all())
    if not batch:
        break
    for row in batch:
        last_id = row.id
        row.field = ...
    db.session.commit()
```

Wrap the batch query and commit in `OperationalError` handling that calls
`db.engine.dispose()` and retries. Reference: `scripts/backfill_pq_answers.py`.

## Detaching long-running scripts

```powershell
# Windows
Start-Process python -ArgumentList "scripts/my_script.py" `
    -RedirectStandardOutput "scripts/my_script.log" `
    -RedirectStandardError "scripts/my_script_err.log" `
    -WindowStyle Hidden
```
```bash
# Linux / Railway shell
nohup python scripts/my_script.py >> scripts/my_script.log 2>&1 &
```

Always use a checkpoint file so interrupted runs resume. Reference:
`scripts/backfill_pq_questions.py`.

---

## Windows subprocess patterns

Platform-resolved binary constant (see `_GPG` / `_PSQL` in `scripts/restore_from_backup.py`):

```python
_PSQL = r"C:\Program Files\PostgreSQL\18\bin\psql.exe" if platform.system() == "Windows" else "psql"
```

Always surface stderr:

```python
result = subprocess.run([...], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
stderr_out = result.stderr.decode(errors="replace")
if stderr_out.strip():
    log(f"stderr:\n{stderr_out[:3000]}")
if result.returncode != 0:
    die(f"command failed (exit {result.returncode})")
```

(For very large output — e.g. a psql restore — do not PIPE stdout; see CLAUDE.md.)

---

## Flask patterns

### Initialise render_template variables before any `if`

```python
# CORRECT
total_available = 0
results = []
if request.method == 'POST':
    total_available = ...

# WRONG — crashes on GET with UnboundLocalError
if request.method == 'POST':
    total_available = ...
return render_template('page.html', total_available=total_available)
```

### Reserved Jinja globals — rename the kwarg

```python
# AVOID
ctx = {"session": hansard_session_obj, ...}
# PREFER
ctx = {"hs_session": hansard_session_obj, ...}
return render_template("page.html", **ctx)
```

If the kwarg name is load-bearing, inject truly global values via
`@app.context_processor` instead.

### Threads

```python
# CORRECT
executor.submit(copy_current_request_context(my_function), arg1, arg2)
# WRONG — "Working outside of application context"
executor.submit(my_function, arg1, arg2)
```

---

## External API retries — exponential backoff (transient errors only)

```python
import time
for attempt in range(3):
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 503:
            break
    except requests.exceptions.ConnectionError:
        pass
    time.sleep(2 ** attempt)  # 1s, 2s, 4s
```

Never back off on 401/403/400 — they will not self-resolve.

## Checking API keys are actually set

```bash
python -c "import os; print('TWFY:', bool(os.environ.get('TWFY_API_KEY'))); print('GEMINI:', bool(os.environ.get('GEMINI_API_KEY'))); print('ANTHROPIC:', bool(os.environ.get('ANTHROPIC_API_KEY')))"
```

On Railway: Variables tab — the var must be set on the **service**. `/health`
on the live site tests each API and reports pass/fail.
