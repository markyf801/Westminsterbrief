"""
Prune old backups from Cloudflare R2.

Retention policy:
  - Daily backups: keep for 30 days
  - Weekly backups: one per ISO week (Monday's backup), keep for 12 weeks (84 days)
  - Promotion: when a daily ages out, if it's a Monday it is copied to weekly/ first

Run after a successful backup:
  python scripts/backup_to_r2.py && python scripts/prune_old_backups.py

Required env vars: same R2_* vars as backup_to_r2.py (no DATABASE_URL needed here).

Safety: logs every planned deletion before executing; aborts if deletion count
looks unexpectedly large (> 60 dailies or > 15 weeklies) rather than proceeding.
"""

import os
import sys
from datetime import datetime, date, timedelta, timezone

import boto3


def log(msg):
    print(f"[prune] {msg}", flush=True)


def die(msg):
    print(f"[prune] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def get_s3():
    for var in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL"):
        if not os.environ.get(var):
            die(f"{var} is not set")
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def list_keys(s3, bucket, prefix):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def parse_daily_date(key):
    """'daily/2026-04-29.sql.gz.gpg' → date(2026, 4, 29) or None."""
    try:
        stem = key.split("/")[-1].split(".")[0]   # '2026-04-29'
        return datetime.strptime(stem, "%Y-%m-%d").date()
    except (ValueError, IndexError):
        return None


def week_key(d):
    """Return weekly/ object key for the ISO week containing date d."""
    iso = d.isocalendar()
    return f"weekly/{iso[0]}-W{iso[1]:02d}.sql.gz.gpg"


def main():
    bucket  = os.environ.get("R2_BUCKET_NAME", "westminsterbrief-backups")
    s3      = get_s3()
    today   = datetime.now(timezone.utc).date()
    cutoff_daily  = today - timedelta(days=30)
    cutoff_weekly = today - timedelta(days=84)   # 12 weeks

    # ------------------------------------------------------------------ dailies
    daily_keys = list_keys(s3, bucket, "daily/")
    log(f"Found {len(daily_keys)} daily backup(s)")

    to_delete_daily    = []
    to_promote         = {}   # weekly_key -> daily_key (first Monday seen per week)

    for key in sorted(daily_keys):
        d = parse_daily_date(key)
        if d is None:
            log(f"  Skipping unrecognised key: {key}")
            continue
        if d >= cutoff_daily:
            log(f"  Keeping  (within 30d):   {key}")
            continue
        # Older than 30 days — candidate for promotion or deletion
        if d.weekday() == 0:   # Monday
            wk = week_key(d)
            if wk not in to_promote:
                to_promote[wk] = key
                log(f"  Promote  (Monday→weekly): {key}  →  {wk}")
            else:
                log(f"  Delete   (dup Monday):    {key}")
                to_delete_daily.append(key)
        else:
            log(f"  Delete   (non-Mon >30d):  {key}")
            to_delete_daily.append(key)

    # Safety guard
    if len(to_delete_daily) > 60:
        die(
            f"About to delete {len(to_delete_daily)} daily files — exceeds safety "
            "threshold of 60. Investigate manually before running again."
        )

    # Copy promoted dailies to weekly/ (skip if weekly already exists)
    existing_weeklies = set(list_keys(s3, bucket, "weekly/"))
    for wk, src in to_promote.items():
        if wk in existing_weeklies:
            log(f"  Weekly already exists, skipping copy: {wk}")
        else:
            log(f"  Copying {src} → {wk}")
            s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": src},
                Key=wk,
            )
        # Daily is no longer needed regardless
        to_delete_daily.append(src)

    # Delete old dailies
    if to_delete_daily:
        log(f"Deleting {len(to_delete_daily)} old daily object(s)...")
        for key in to_delete_daily:
            log(f"  DELETE {key}")
            s3.delete_object(Bucket=bucket, Key=key)
    else:
        log("No old daily backups to delete.")

    # ----------------------------------------------------------------- weeklies
    weekly_keys = list_keys(s3, bucket, "weekly/")
    log(f"Found {len(weekly_keys)} weekly backup(s)")

    to_delete_weekly = []
    for key in weekly_keys:
        try:
            stem  = key.split("/")[-1].split(".")[0]   # '2026-W17'
            year, week = stem.split("-W")
            monday = datetime.fromisocalendar(int(year), int(week), 1).date()
            if monday < cutoff_weekly:
                log(f"  Delete   (>12 weeks): {key}")
                to_delete_weekly.append(key)
            else:
                log(f"  Keeping  (within 12w): {key}")
        except (ValueError, IndexError, AttributeError):
            log(f"  Skipping unrecognised weekly key: {key}")

    if len(to_delete_weekly) > 15:
        die(
            f"About to delete {len(to_delete_weekly)} weekly files — exceeds safety "
            "threshold of 15. Investigate manually before running again."
        )

    if to_delete_weekly:
        log(f"Deleting {len(to_delete_weekly)} old weekly backup(s)...")
        for key in to_delete_weekly:
            log(f"  DELETE {key}")
            s3.delete_object(Bucket=bucket, Key=key)
    else:
        log("No old weekly backups to delete.")

    log("Pruning complete.")


if __name__ == "__main__":
    main()
