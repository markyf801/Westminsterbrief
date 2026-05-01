# Railway Cron Job Setup — Hansard Archive

Three Cron Job services keep the archive current. Each is a separate Railway service.

---

## railway.toml and start commands

`railway.toml` in the repo does **not** set a `startCommand` — it was removed deliberately so each service can set its own command in the Railway UI (Settings → Custom Start Command). The Flask service (`jubilant-intuition`) uses its Procfile as a fallback; cron services set theirs in the UI.

If you see "The value is set in /railway.toml" blocking the Settings field, check that `railway.toml` does not contain `startCommand` under `[deploy]`.

---

## Create each service

In Railway: **New Service → Cron Job → Connect GitHub repo (`markyf801/Westminsterbrief`)**

---

## Service 1 — Morning catch-up

| Setting | Value |
|---|---|
| **Name** | `archive-cron-morning` |
| **Schedule** | `0 8 * * 1-5` |
| **Start command** | `python scripts/archive_cron.py --days 3 --service-name morning-catchup` |
| **Purpose** | Picks up late-Lords content published overnight |

**This is the critical service.** If it fails silently, overnight content won't appear until 11:00.
The script exits non-zero on failure — Railway will mark the run as failed in its dashboard.
An alert email goes to `ADMIN_EMAIL` / `CRON_ALERT_EMAIL` on any error.

---

## Service 2 — Mon-Thu daytime

| Setting | Value |
|---|---|
| **Name** | `archive-cron-daytime-mth` |
| **Schedule** | `0 11-23 * * 1-4` |
| **Start command** | `python scripts/archive_cron.py --days 3 --service-name daytime-mth` |
| **Purpose** | Hourly catch-up during Commons/Lords sitting hours Mon-Thu |

Alert threshold: 3+ errors in a single run (next hourly run will pick up automatically).

---

## Service 3 — Friday daytime

| Setting | Value |
|---|---|
| **Name** | `archive-cron-daytime-fri` |
| **Schedule** | `0 9-19 * * 5` |
| **Start command** | `python scripts/archive_cron.py --days 3 --service-name daytime-fri` |
| **Purpose** | Friday sitting coverage (Lords sometimes sits Fridays) |

Alert threshold: same as Service 2.

---

## Environment variables for each cron service

Add these as Variable References or copies on **each cron service** in Railway:

| Variable | Value | How to set |
|---|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Variable Reference — picks up password rotations automatically |
| `GEMINI_API_KEY` | *(same as Flask service)* | Copy from Flask service or use shared variable |
| `POSTMARK_SERVER_TOKEN` | *(same as Flask service)* | Copy from Flask service |
| `ADMIN_EMAIL` | `markjforde@gmail.com` | Alert recipient — copy from Flask service |
| `CRON_ALERT_EMAIL` | *(optional)* | Override alert recipient if different from ADMIN_EMAIL |
| `EMAIL_FROM_ADDRESS` | `hello@westminsterbrief.co.uk` | Copy from Flask service (has default, only needed to override) |
| `EMAIL_FROM_NAME` | `Westminster Brief` | Copy from Flask service (has default, only needed to override) |

**Critical:** `DATABASE_URL` must use the Variable Reference syntax `${{Postgres.DATABASE_URL}}`, NOT a hardcoded connection string. This ensures cron services automatically pick up any future password rotations — same as the Flask service.

---

## What each run logs

Each run prints to Railway's deployment log:

```
[cron] === START === service=morning-catchup window=2026-04-29→2026-05-01 (3d)
[cron] started_at=2026-05-01T08:00:01Z
[cron] === Commons ===
[cron] Commons done — 23 new sessions, 1 sitting day(s), 0 error(s)
[cron] === Lords ===
[cron] Lords done — 8 new sessions, 1 sitting day(s), 0 error(s)
[cron] Tagging 31 new session(s)…
[cron] Tagging done — 31 session(s) tagged, 0 error(s)
[cron] === END === status=ok ingested=31 tagged=31 errors=0 elapsed=47.2s
[cron] finished_at=2026-05-01T08:00:48Z
```

Run records are also persisted in the `ha_cron_run` database table for 90 days.
These are queryable from `/admin` (planned: add a cron run history panel).

---

## Post-deploy verification checklist

After creating all three services:

- [ ] Manually trigger each service once via **Run Now** in Railway dashboard
- [ ] Check logs show `status=ok` and sensible timings (30–120s typical)
- [ ] Confirm no errors in the log (especially Gemini API key working for tagging)
- [ ] Confirm `ha_cron_run` has three rows in the DB (check via `/admin` or psql)
- [ ] Railway dashboard shows three services as "Scheduled"
- [ ] Wait for the next natural scheduled run and confirm it triggers

**Expected timings:**
- Parliament not sitting: ~5s (both houses return 0 new sessions; tagger finds nothing untagged)
- Normal sitting day: 30–90s depending on volume
- First run after a long recess: up to 5 min if catching up many days

---

## If Railway cron scheduling proves unreliable

Railway cron has historically been reliable but not guaranteed. If runs are consistently
missing or firing at wrong times:

1. Check Railway status page
2. Consider fallback: set each service to always-on with a Python APScheduler loop
3. Or migrate to a dedicated scheduler (e.g. Render Cron Jobs, GitHub Actions scheduled workflows)

Flag to Mark before changing the scheduling approach.
