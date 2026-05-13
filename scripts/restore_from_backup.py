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
import platform
import subprocess
import sys
import tempfile

import boto3
from dotenv import load_dotenv

load_dotenv()

# On Windows, gpg is bundled with Git but not always in PATH.
_GPG = (
    r"C:\Program Files\Git\usr\bin\gpg.exe"
    if platform.system() == "Windows"
    else "gpg"
)

# On Windows, psql is not always in PATH even when PG client tools are installed.
_PSQL = (
    r"C:\Program Files\PostgreSQL\16\bin\psql.exe"
    if platform.system() == "Windows"
    else "psql"
)


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
                _GPG, "--batch", "--yes",
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
        # PG17-only SET statements that fail on older local servers — strip them.
        # These are session-level no-ops; removing them has no effect on data.
        _PG17_ONLY = [b"transaction_timeout"]

        log("Decompressing...")
        with gzip.open(gz_path, "rb") as f_in, open(sql_path, "wb") as f_out:
            for line in f_in:
                if any(pat in line for pat in _PG17_ONLY):
                    continue
                f_out.write(line)
        log(f"Decompressed ({os.path.getsize(sql_path):,} bytes)")

        # 4. Restore via psql
        # Prefer PGPASSWORD env var directly (avoids URL-encoding issues with
        # special characters in passwords). Fall back to extracting from URL.
        import urllib.parse as _urlparse
        _parsed = _urlparse.urlparse(target_db_url)
        _password = os.environ.get("PGPASSWORD") or _urlparse.unquote(_parsed.password or "")
        _pg_env = {**os.environ, "PGPASSWORD": _password}
        log("Running psql restore (this may take a moment)...")
        stderr_log = os.path.join(tmpdir, "psql_stderr.txt")
        with open(stderr_log, "w", encoding="utf-8") as _stderr_f:
            result = subprocess.run(
                [_PSQL, "-f", sql_path,
                 "--echo-errors", "--set", "ON_ERROR_STOP=1",
                 target_db_url],
                stderr=_stderr_f,
                env=_pg_env,
            )
        with open(stderr_log, encoding="utf-8", errors="replace") as _f:
            stderr_out = _f.read()
        if stderr_out.strip():
            log(f"psql stderr (first 3000 chars):\n{stderr_out[:3000]}")
        if result.returncode != 0:
            die(f"psql restore failed (exit {result.returncode})")

        log("Restore complete.")
        log("Next: run the smoke test against the restored database to verify.")
        log("See docs/recovery-runbook.md — Step 6.")


if __name__ == "__main__":
    main()
