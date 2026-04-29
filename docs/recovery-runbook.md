# Westminster Brief — Disaster Recovery Runbook

This document covers full database recovery from a Cloudflare R2 backup. It is written for someone who may not have been involved in the original setup.

---

## Where backups live

- **Provider:** Cloudflare R2 (independent of Railway — survives Railway account or volume deletion)
- **Bucket:** `westminsterbrief-backups`
- **Structure:**
  - `daily/YYYY-MM-DD.sql.gz.gpg` — kept for 30 days
  - `weekly/YYYY-W##.sql.gz.gpg` — kept for 12 weeks; one per ISO week (Monday's backup)
- **Encryption:** AES-256 GPG symmetric encryption. The passphrase is `BACKUP_ENCRYPTION_KEY` — stored in Railway Variables and in Mark's password manager.
- **Backup schedule:** 03:00 UTC daily, run by the `westminsterbrief-backup` Railway cron service.

---

## What you'll need

- Python 3.10+ with `boto3` installed (`pip install boto3`)
- `gpg` command-line tool (standard on macOS/Linux; Windows: install Gpg4win)
- `psql` command-line tool (PostgreSQL client)
- The `BACKUP_ENCRYPTION_KEY` passphrase (from Mark's password manager)
- R2 credentials: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL`
- Access to create a new Railway Postgres database (or another Postgres target)

Clone the repo if you don't have it:
```bash
git clone https://github.com/markyf801/Westminsterbrief.git
cd Westminsterbrief
pip install boto3
```

---

## Recovery steps

### Step 1 — Choose which backup to restore

List available backups manually via the R2 dashboard at dash.cloudflare.com, or run:

```bash
python - <<'EOF'
import boto3, os
s3 = boto3.client('s3',
    endpoint_url=os.environ['R2_ENDPOINT_URL'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto')
for prefix in ['daily/', 'weekly/']:
    resp = s3.list_objects_v2(Bucket='westminsterbrief-backups', Prefix=prefix)
    for obj in resp.get('Contents', []):
        print(obj['Key'], f"  {obj['Size']:,} bytes")
EOF
```

Pick the most recent daily backup unless you need to go back further (e.g. `daily/2026-04-29.sql.gz.gpg`).

---

### Step 2 — Create a new Railway Postgres database

If the original database is gone:

1. Go to [railway.app](https://railway.app) → your project (`invigorating-joy`)
2. Click **New** → **Database** → **PostgreSQL**
3. Wait for it to provision (~30 seconds)
4. Click the new database → **Variables** tab → copy `DATABASE_URL`

If the original database still exists (e.g. you're restoring to fix data corruption), use its `DATABASE_URL` directly — but be aware this **overwrites all current data**.

---

### Step 3 — Set env vars for the restore script

All parameters are passed via environment variables — there are no command-line arguments.

> ⚠️ **`RESTORE_TARGET_DB_URL` must point to the NEW or throwaway Postgres, never the production database.** Restoring overwrites all data in the target. There is no undo.

| Variable | Value |
|---|---|
| `RESTORE_BACKUP_KEY` | The R2 object key chosen in Step 1, e.g. `daily/2026-04-29.sql.gz.gpg` |
| `RESTORE_TARGET_DB_URL` | **NON-PRODUCTION** Postgres connection string — the new database from Step 2 |
| `BACKUP_ENCRYPTION_KEY` | From Mark's password manager |
| `R2_ACCESS_KEY_ID` | From Cloudflare R2 token |
| `R2_SECRET_ACCESS_KEY` | Same token |
| `R2_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET_NAME` | `westminsterbrief-backups` |
| `RESTORE_ALLOWED` | **`true` — required when running non-interactively (e.g. on Railway).** This is a deliberate safety gate: the script refuses to proceed without it when there is no interactive terminal. It forces the operator to make a conscious decision before anything is overwritten. If missing in a non-interactive environment, the script exits immediately with an error before touching any data. |

If running locally (interactive terminal), `RESTORE_ALLOWED` is not required — the script will prompt for confirmation instead.

For local execution, export the vars:
```bash
export RESTORE_BACKUP_KEY="daily/2026-04-29.sql.gz.gpg"
export RESTORE_TARGET_DB_URL="postgresql://user:pass@host:5432/railway"
export BACKUP_ENCRYPTION_KEY="<passphrase from password manager>"
export R2_ACCESS_KEY_ID="<from Mark's notes>"
export R2_SECRET_ACCESS_KEY="<from Mark's notes>"
export R2_ENDPOINT_URL="https://<account-id>.r2.cloudflarestorage.com"
export R2_BUCKET_NAME="westminsterbrief-backups"
```

---

### Step 4 — Run the restore script

```bash
python scripts/restore_from_backup.py
```

The script will:
1. Validate all env vars are set
2. In interactive mode: prompt for confirmation (Ctrl-C to abort). In non-interactive mode: check `RESTORE_ALLOWED=true`, then proceed.
3. Download the backup from R2
4. Decrypt it (GPG, AES-256)
5. Decompress it
6. Run `psql -f` to restore into the target database

If any step fails, the script exits non-zero with a clear error message.

---

### Step 5 — Point the Flask app at the new database

If you created a new Railway Postgres service:

1. Go to the `Westminsterbrief` Railway service → **Variables**
2. Update `DATABASE_URL` to the new database's connection string
3. Railway will redeploy automatically

If the original database still exists and you restored in-place, no change needed.

---

### Step 6 — Verify recovery

Run the smoke test against the live URL:

```bash
python scripts/smoke_test_phase1.py
```

Or manually check:
- `/home` loads
- `/health` returns 200
- `/questions` returns results
- Log in with a known account and verify preferences are intact

If the smoke test passes, recovery is complete.

---

## If you need an older backup

Weekly backups go back 12 weeks. Use the same restore process but with a weekly key:

```bash
python scripts/restore_from_backup.py weekly/2026-W15.sql.gz.gpg "postgresql://..."
```

---

## Recreating the cron service

If the `westminsterbrief-backup` Railway cron service ever needs to be recreated from scratch:

- **Schedule:** `0 3 * * *` (03:00 UTC daily)
- **Command:** `python scripts/backup_to_r2.py && python scripts/prune_old_backups.py`
- **Config File Path** (in Railway service Settings → Build): `railway.backup.toml`
- **Restart policy:** Never (set in `railway.backup.toml`)

### Environment variables for the cron service

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Use Railway reference `${{Postgres.DATABASE_URL}}` — do not hardcode |
| `BACKUP_ENCRYPTION_KEY` | From Mark's password manager |
| `R2_ACCESS_KEY_ID` | From Cloudflare R2 API token (Object Read & Write on bucket only) |
| `R2_SECRET_ACCESS_KEY` | Same token |
| `R2_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET_NAME` | `westminsterbrief-backups` |

**Note — `pg_dump`, `psql`, and `gpg` are installed via `nixpacks.backup.toml`, not via `NIXPACKS_PKGS`.** The config file adds the official PostgreSQL PGDG apt repository and installs `postgresql-client-18` and `gnupg` during the container build. Do not set `NIXPACKS_PKGS` on this service — it was tried during initial setup and caused Nixpacks to resolve the wrong package output (`postgresql_16.dev`, headers only). The `nixpacks.backup.toml` approach is what's actually deployed.

**Note — the restore script (`scripts/restore_from_backup.py`) is a separate manual tool, not run by the cron service.** Its env vars (`RESTORE_BACKUP_KEY`, `RESTORE_TARGET_DB_URL`, `RESTORE_ALLOWED`) are set on a dedicated short-lived restore service, not here. See Steps 3–4 above.

---

## Contacts

- **Mark Forde** — mark@westminsterbrief.co.uk — original developer
- **Railway project:** `invigorating-joy` — service: `Westminsterbrief`
- **R2 bucket:** `westminsterbrief-backups` — Cloudflare account owned by Mark
