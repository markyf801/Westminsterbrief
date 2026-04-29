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

```bash
export BACKUP_ENCRYPTION_KEY="<passphrase from password manager>"
export R2_ACCESS_KEY_ID="<from Railway or Mark's notes>"
export R2_SECRET_ACCESS_KEY="<from Railway or Mark's notes>"
export R2_ENDPOINT_URL="https://<account-id>.r2.cloudflarestorage.com"
export R2_BUCKET_NAME="westminsterbrief-backups"
```

---

### Step 4 — Run the restore script

```bash
python scripts/restore_from_backup.py daily/2026-04-29.sql.gz.gpg "postgresql://user:pass@host:5432/railway"
```

Replace the backup key and the target DB URL with the actual values from Steps 1 and 2.

The script will:
1. Ask you to confirm before proceeding (Ctrl-C to abort)
2. Download the backup from R2
3. Decrypt it (GPG, AES-256)
4. Decompress it
5. Run `psql -f` to restore into the target database

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

## Contacts

- **Mark Forde** — mark@westminsterbrief.co.uk — original developer
- **Railway project:** `invigorating-joy` — service: `Westminsterbrief`
- **R2 bucket:** `westminsterbrief-backups` — Cloudflare account owned by Mark
