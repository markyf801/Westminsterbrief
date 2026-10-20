"""
Re-walk the Parliament Written Questions bulk API to fill in headings (and
asking/answering member names) on PQs ingested before Parliament assigned them.

Uses the ingestor's own upsert (hansard_archive.pq_ingestor) — same identity
rules (api_id, then (tabled_date, uin); never uin alone), same tripwire, same
"never blank a stored value" rules. Bulk records only: stored question/answer
text is never replaced (bulk text is truncated), only filled if missing, so
this pass does not clear theme tags.

DRY RUN BY DEFAULT — prints the predicted insert/update/unchanged/mismatch
split and sample diffs, and writes nothing. --apply writes.

Usage:
  python scripts/backfill_pq_headings.py                       # dry run, 2026-05-13 → today
  python scripts/backfill_pq_headings.py --since 2026-09-01 --until 2026-09-30
  python scripts/backfill_pq_headings.py --apply               # write

Railway one-off: safe with SKIP_MIGRATIONS=1 (set by default below, so no
startup migrations or seeding run). Don't run within 30 minutes of a PQ cron.
Review the dry run before --apply; --apply aborts once identity mismatches
exceed --max-mismatches across the whole run.
"""

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import date, timedelta

os.environ.setdefault("SKIP_MIGRATIONS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pq-headings] %(message)s",
                    datefmt="%H:%M:%S")
_log = logging.getLogger("pq-headings")

DEFAULT_SINCE = date(2026, 5, 13)   # start of the current session
CHUNK_DAYS = 14
MIN_DELAY = 1.0                     # seconds between API requests (floor)
SAMPLE_LIMIT = 10


def _short(val, n=70):
    s = "None" if val is None else str(val)
    return s if len(s) <= n else s[:n] + "…"


class Tally:
    def __init__(self):
        self.counts = Counter()
        self.field_changes = Counter()
        self.update_samples: list[str] = []
        self.mismatch_samples: list[str] = []
        self.insert_samples: list[str] = []

    def on_outcome(self, fields, outcome):
        if outcome.action == "updated":
            self.field_changes.update(outcome.changes.keys())
            if len(self.update_samples) < SAMPLE_LIMIT:
                diff = "; ".join(f"{k}: {_short(o)} → {_short(n)}"
                                 for k, (o, n) in outcome.changes.items())
                self.update_samples.append(
                    f"id={outcome.pq_id} {fields['tabled_date']} {fields['uin']}: {diff}")
        elif outcome.action == "identity_mismatch":
            if len(self.mismatch_samples) < 50:
                self.mismatch_samples.append(outcome.detail)
        elif outcome.action == "inserted" and len(self.insert_samples) < SAMPLE_LIMIT:
            self.insert_samples.append(
                f"api_id={fields.get('api_id')} {fields['tabled_date']} {fields['uin']} "
                f"{_short(fields.get('heading'))}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", type=date.fromisoformat, default=DEFAULT_SINCE,
                        help="First tabled date (default 2026-05-13)")
    parser.add_argument("--until", type=date.fromisoformat, default=None,
                        help="Last tabled date (default today)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Predict only, no writes (default)")
    mode.add_argument("--apply", action="store_true", help="Write changes")
    parser.add_argument("--delay", type=float, default=MIN_DELAY,
                        help=f"Seconds between API requests (minimum {MIN_DELAY})")
    parser.add_argument("--max-mismatches", type=int, default=5,
                        help="--apply aborts when identity mismatches exceed this (default 5)")
    args = parser.parse_args(argv)

    until = args.until or date.today()
    if args.since > until:
        parser.error("--since is after --until")
    apply = args.apply
    delay = max(MIN_DELAY, args.delay)

    from flask_app import app
    from hansard_archive.pq_ingestor import PQIdentityMismatchError, ingest_pq_date_range

    _log.info("=== %s === tabled %s → %s, %ss between requests",
              "APPLY" if apply else "DRY RUN (no writes)", args.since, until, delay)

    tally = Tally()
    aborted = None
    with app.app_context():
        chunk_start = args.since
        while chunk_start <= until:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), until)
            # Dry run: count every mismatch so the prediction is complete.
            allowance = (args.max_mismatches - tally.counts["identity_mismatch"]
                         if apply else 10 ** 9)
            try:
                res = ingest_pq_date_range(
                    chunk_start, chunk_end, verbose=False,
                    skip_answer_fetch=True, dry_run=not apply,
                    max_identity_mismatches=allowance, request_delay=delay,
                    on_outcome=tally.on_outcome,
                )
            except PQIdentityMismatchError as exc:
                res, aborted = exc.counts, str(exc)
            for key in ("inserted", "updated", "unchanged", "identity_mismatch",
                        "failed", "page_errors"):
                tally.counts[key] += res.get(key, 0)
            _log.info("%s..%s ins=%d upd=%d same=%d mismatch=%d failed=%d page_err=%d",
                      chunk_start, chunk_end, res.get("inserted", 0), res.get("updated", 0),
                      res.get("unchanged", 0), res.get("identity_mismatch", 0),
                      res.get("failed", 0), res.get("page_errors", 0))
            if aborted:
                break
            chunk_start = chunk_end + timedelta(days=1)

    c = tally.counts
    verb = "" if apply else "predicted "
    _log.info("=== SUMMARY (%s) ===", "applied" if apply else "dry run — nothing written")
    _log.info("%sinserted=%d updated=%d unchanged=%d identity_mismatch=%d failed=%d page_errors=%d",
              verb, c["inserted"], c["updated"], c["unchanged"], c["identity_mismatch"],
              c["failed"], c["page_errors"])
    if tally.field_changes:
        _log.info("fields changed: %s", ", ".join(
            f"{k}={v}" for k, v in tally.field_changes.most_common()))
    for title, rows in (("sample updates", tally.update_samples),
                        ("sample inserts", tally.insert_samples),
                        ("identity mismatches", tally.mismatch_samples)):
        if rows:
            _log.info("%s:", title)
            for r in rows:
                _log.info("  %s", r)
    if c["inserted"]:
        _log.info("NOTE: inserts are questions missing from the archive — expected to be "
                  "few for an already-ingested window. Many inserts = investigate first.")
    if aborted:
        _log.error("ABORTED: %s", aborted)
        return 2
    if c["identity_mismatch"] and not apply:
        _log.warning("Dry run found %d identity mismatches — resolve before --apply "
                     "(--apply stops above %d).", c["identity_mismatch"], args.max_mismatches)
    return 1 if (c["failed"] or c["page_errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
