"""
Backup Westminster Brief Postgres database to Cloudflare R2.

Run by the Railway cron service daily at 03:00 UTC.
Exits non-zero on any failure so Railway marks the cron run as failed.
Sends a Postmark failure alert email on any fatal error.

Required env vars:
  DATABASE_URL           — set automatically by Railway when DB is attached
  BACKUP_ENCRYPTION_KEY  — long random passphrase (set manually in Railway)
  R2_ACCESS_KEY_ID       — Cloudflare R2 token (Object Read & Write on bucket only)
  R2_SECRET_ACCESS_KEY
  R2_ENDPOINT_URL        — https://<account-id>.r2.cloudflarestorage.com
  R2_BUCKET_NAME         — defaults to westminsterbrief-backups
  POSTMARK_SERVER_TOKEN  — for failure alert emails (optional: logs if unset)
  ADMIN_EMAIL            — alert recipient (optional: logs if unset)
"""

import gzip
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import boto3
import psycopg2


def log(msg):
    print(f"[backup] {msg}", flush=True)


def _send_failure_alert(error_msg: str) -> None:
    """Send a Postmark email on backup failure. Logs to stdout if sending fails."""
    token = os.environ.get("POSTMARK_SERVER_TOKEN", "")
    to_addr = os.environ.get("ADMIN_EMAIL", "")
    from_addr = os.environ.get("EMAIL_FROM_ADDRESS", "hello@westminsterbrief.co.uk")

    if not token or not to_addr:
        log(f"ALERT: backup failed — no email sent (POSTMARK_SERVER_TOKEN or ADMIN_EMAIL not set). Error: {error_msg}")
        return

    subject = f"[Westminster Brief] Backup failed — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    body = (
        f"The daily Westminster Brief database backup failed.\n\n"
        f"Error: {error_msg}\n\n"
        f"Check Railway deployment logs for backup-cron-r2 for full details.\n"
        f"The /health endpoint will report backup as STALE within 26 hours if not resolved."
    )

    try:
        from postmarker.core import PostmarkClient
        client = PostmarkClient(server_token=token)
        client.emails.send(
            From=f"Westminster Brief <{from_addr}>",
            To=to_addr,
            Subject=subject,
            TextBody=body,
            MessageStream="outbound",
        )
        log(f"Failure alert sent to {to_addr}")
    except Exception as exc:
        # Alert send failed — log clearly so Railway deployment logs capture it
        log(f"ALERT: backup failed AND failure email could not be sent ({exc}). Error was: {error_msg}")


def die(msg):
    print(f"[backup] ERROR: {msg}", file=sys.stderr, flush=True)
    _send_failure_alert(msg)
    sys.exit(1)


def main():
    database_url    = os.environ.get("DATABASE_URL")
    encryption_key  = os.environ.get("BACKUP_ENCRYPTION_KEY")
    r2_key_id       = os.environ.get("R2_ACCESS_KEY_ID")
    r2_secret       = os.environ.get("R2_SECRET_ACCESS_KEY")
    r2_endpoint     = os.environ.get("R2_ENDPOINT_URL")
    r2_bucket       = os.environ.get("R2_BUCKET_NAME", "westminsterbrief-backups")

    for name, val in [
        ("DATABASE_URL",          database_url),
        ("BACKUP_ENCRYPTION_KEY", encryption_key),
        ("R2_ACCESS_KEY_ID",      r2_key_id),
        ("R2_SECRET_ACCESS_KEY",  r2_secret),
        ("R2_ENDPOINT_URL",       r2_endpoint),
    ]:
        if not val:
            die(f"{name} is not set")

    date_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    object_key = f"daily/{date_str}.sql.gz.gpg"
    log(f"Starting backup → {r2_bucket}/{object_key}")

    with tempfile.TemporaryDirectory() as tmpdir:
        dump_path = os.path.join(tmpdir, "dump.sql")
        gz_path   = os.path.join(tmpdir, "dump.sql.gz")
        enc_path  = os.path.join(tmpdir, "dump.sql.gz.gpg")

        # 1. pg_dump
        log("Running pg_dump...")
        with open(dump_path, "wb") as dump_file:
            result = subprocess.run(
                ["pg_dump", "--no-owner", "--clean", "--if-exists", database_url],
                stdout=dump_file,
                stderr=subprocess.PIPE,
            )
        if result.returncode != 0:
            die(f"pg_dump failed (exit {result.returncode}): {result.stderr.decode()[:500]}")
        log(f"pg_dump complete ({os.path.getsize(dump_path):,} bytes uncompressed)")

        # 2. gzip
        log("Compressing...")
        with open(dump_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            while chunk := f_in.read(1024 * 1024):
                f_out.write(chunk)
        log(f"Compressed ({os.path.getsize(gz_path):,} bytes)")

        # 3. GPG symmetric encryption (AES-256)
        log("Encrypting...")
        result = subprocess.run(
            [
                "gpg", "--batch", "--yes",
                "--passphrase-fd", "0",
                "--symmetric",
                "--cipher-algo", "AES256",
                "--output", enc_path,
                gz_path,
            ],
            input=encryption_key.encode(),
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            die(f"GPG encryption failed: {result.stderr.decode()[:500]}")
        log(f"Encrypted ({os.path.getsize(enc_path):,} bytes)")

        # 4. Upload to R2
        log("Uploading to R2...")
        s3 = boto3.client(
            "s3",
            endpoint_url=r2_endpoint,
            aws_access_key_id=r2_key_id,
            aws_secret_access_key=r2_secret,
            region_name="auto",
        )
        s3.upload_file(enc_path, r2_bucket, object_key)

    log(f"Backup complete: s3://{r2_bucket}/{object_key}")

    # Record successful run in ha_cron_run so /health can check backup freshness
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.execute(
                """
                INSERT INTO ha_cron_run
                    (service_name, started_at, finished_at, days_window, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                ("backup-r2", now, now, 1, "ok"),
            )
        conn.close()
        log("Recorded run in ha_cron_run")
    except Exception as exc:
        log(f"WARNING: could not record run in ha_cron_run: {exc}")


if __name__ == "__main__":
    main()
