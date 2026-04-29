"""
Restore a Westminster Brief backup from Cloudflare R2.

This is a manual disaster-recovery tool, not a cron job.
See docs/recovery-runbook.md for full recovery procedure.

Usage:
  python scripts/restore_from_backup.py

Required env vars:
  RESTORE_BACKUP_KEY     R2 object key, e.g. daily/2026-04-29.sql.gz.gpg
  RESTORE_TARGET_DB_URL  Postgres connection string for the target database
  BACKUP_ENCRYPTION_KEY  Must match the passphrase used during backup
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_ENDPOINT_URL
  R2_BUCKET_NAME         Defaults to westminsterbrief-backups

Safety:
  When running non-interactively (e.g. on Railway), also set:
  RESTORE_ALLOWED=true   Explicit confirmation that target is not production
"""

import gzip
import os
import subprocess
import sys
import tempfile

import boto3


def log(msg):
    print(f"[restore] {msg}", flush=True)


def die(msg):
    print(f"[restore] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def masked_url(url):
    """Return DB URL with password hidden for safe logging."""
    try:
        at = url.rindex("@")
        scheme_end = url.index("://") + 3
        return url[:scheme_end] + "***:***@" + url[at + 1:]
    except ValueError:
        return "***"


def main():
    if len(sys.argv) != 1:
        print(__doc__)
        sys.exit(1)

    backup_key    = os.environ.get("RESTORE_BACKUP_KEY")
    target_db_url = os.environ.get("RESTORE_TARGET_DB_URL")
    encryption_key = os.environ.get("BACKUP_ENCRYPTION_KEY")
    r2_key_id      = os.environ.get("R2_ACCESS_KEY_ID")
    r2_secret      = os.environ.get("R2_SECRET_ACCESS_KEY")
    r2_endpoint    = os.environ.get("R2_ENDPOINT_URL")
    r2_bucket      = os.environ.get("R2_BUCKET_NAME", "westminsterbrief-backups")

    for name, val in [
        ("RESTORE_BACKUP_KEY",     backup_key),
        ("RESTORE_TARGET_DB_URL",  target_db_url),
        ("BACKUP_ENCRYPTION_KEY",  encryption_key),
        ("R2_ACCESS_KEY_ID",       r2_key_id),
        ("R2_SECRET_ACCESS_KEY",   r2_secret),
        ("R2_ENDPOINT_URL",        r2_endpoint),
    ]:
        if not val:
            die(f"{name} is not set")

    log(f"Source:  s3://{r2_bucket}/{backup_key}")
    log(f"Target:  {masked_url(target_db_url)}")
    log("=" * 60)
    log("WARNING: This will OVERWRITE ALL DATA in the target database.")
    log("=" * 60)

    is_interactive = sys.stdin.isatty()
    if is_interactive:
        input("[restore] Press Enter to continue, or Ctrl-C to abort...")
    else:
        restore_allowed = os.environ.get("RESTORE_ALLOWED", "").strip().lower()
        if restore_allowed != "true":
            die(
                "Running non-interactively (no tty). Set RESTORE_ALLOWED=true "
                "in this service's env vars to confirm the target is not production."
            )
        log("Non-interactive mode — proceeding because RESTORE_ALLOWED=true is set.")

    with tempfile.TemporaryDirectory() as tmpdir:
        enc_path = os.path.join(tmpdir, "backup.sql.gz.gpg")
        gz_path  = os.path.join(tmpdir, "backup.sql.gz")
        sql_path = os.path.join(tmpdir, "backup.sql")

        # 1. Download from R2
        log("Downloading from R2...")
        s3 = boto3.client(
            "s3",
            endpoint_url=r2_endpoint,
            aws_access_key_id=r2_key_id,
            aws_secret_access_key=r2_secret,
            region_name="auto",
        )
        s3.download_file(r2_bucket, backup_key, enc_path)
        log(f"Downloaded ({os.path.getsize(enc_path):,} bytes)")

        # 2. Decrypt
        log("Decrypting...")
        result = subprocess.run(
            [
                "gpg", "--batch", "--yes",
                "--passphrase-fd", "0",
                "--decrypt",
                "--output", gz_path,
                enc_path,
            ],
            input=encryption_key.encode(),
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            die(f"GPG decryption failed: {result.stderr.decode()[:500]}")
        log(f"Decrypted ({os.path.getsize(gz_path):,} bytes)")

        # 3. Decompress
        log("Decompressing...")
        with gzip.open(gz_path, "rb") as f_in, open(sql_path, "wb") as f_out:
            while chunk := f_in.read(1024 * 1024):
                f_out.write(chunk)
        log(f"Decompressed ({os.path.getsize(sql_path):,} bytes)")

        # 4. Restore via psql
        log("Running psql restore (this may take a moment)...")
        result = subprocess.run(
            ["psql", target_db_url, "-f", sql_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            die(f"psql restore failed:\n{result.stderr.decode()[:1000]}")

        log("Restore complete.")
        log("Next: run the smoke test against the restored database to verify.")
        log("See docs/recovery-runbook.md — Step 6.")


if __name__ == "__main__":
    main()
