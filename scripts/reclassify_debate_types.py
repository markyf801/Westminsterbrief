"""
Re-classify HansardSession.debate_type with the current classifier.

Why: Lords sessions all have HRSTag "NewDebate", and the old Lords oral-question
override read a line that ingestion drops, so most Lords questions, statements,
urgent-question repeats, take-note debates and Questions for Short Debate were
stored as 'other' ("Proceedings") -- and 'other' sessions are never topic-tagged.
Commons urgent questions were 'other' too (misspelt HRSTag). The classifier now
reads the official hs_DebateType label Hansard prints above each Lords item.

Dry run by default: prints how many rows would change, by house and old -> new
type, with sample titles. Nothing is written without --apply.

Lords rows need the label, which isn't stored, so each Lords session's JSON is
fetched from the Hansard API (about 0.3s each). Commons rows are re-classified
from the stored title / location / hrs_tag with no API call.

Usage (Railway one-off against production, dry run first):
    python scripts/reclassify_debate_types.py                     # dry run, all rows
    python scripts/reclassify_debate_types.py --house Lords --since 2026-06-01 --limit 200
    python scripts/reclassify_debate_types.py --apply             # write changes

After --apply, sessions that left 'other' have no topic tags yet; tag them with
    python scripts/run_tagging.py
(it tags every untagged non-container session that isn't 'other').
"""

import argparse
import collections
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

_API = "https://hansard-api.parliament.uk/debates/debate/{}.json"
_DELAY = 0.3
_BATCH = 200


def _fetch_label(ext_id: str) -> tuple[bool, str]:
    """(ok, official hs_DebateType label) for one session; ok=False on any fetch error."""
    from hansard_archive.ingestion import _official_debate_type_label
    try:
        r = requests.get(_API.format(ext_id), timeout=30)
        r.raise_for_status()
        return True, _official_debate_type_label(r.json())
    except (requests.RequestException, ValueError):
        return False, ""


def _first_speech(db, HansardContribution, session_id: int) -> str:
    row = (
        db.session.query(HansardContribution.speech_text)
        .filter(HansardContribution.session_id == session_id)
        .order_by(HansardContribution.speech_order)
        .first()
    )
    return (row[0] if row else "") or ""


def run(args) -> None:
    from flask_app import app
    from extensions import db
    from hansard_archive.ingestion import classify_session
    from hansard_archive.models import HansardContribution, HansardSession

    changes: collections.Counter = collections.Counter()
    samples: dict = collections.defaultdict(list)
    seen = fetch_errors = written = 0

    with app.app_context():
        q = db.session.query(HansardSession.id).filter(HansardSession.is_container.is_(False))
        if args.house:
            q = q.filter(HansardSession.house == args.house)
        if args.since:
            q = q.filter(HansardSession.date >= date.fromisoformat(args.since))
        if args.until:
            q = q.filter(HansardSession.date <= date.fromisoformat(args.until))
        ids = [r[0] for r in q.order_by(HansardSession.id).all()]
        if args.limit:
            ids = ids[: args.limit]
        print(f"[reclassify] {len(ids)} sessions to check ({'APPLY' if args.apply else 'dry run'})", flush=True)

        for start in range(0, len(ids), _BATCH):
            batch_ids = ids[start:start + _BATCH]
            for attempt in range(3):
                try:
                    sessions = HansardSession.query.filter(HansardSession.id.in_(batch_ids)).all()
                    break
                except OperationalError as exc:
                    print(f"[reclassify] DB error, retrying: {exc}", flush=True)
                    db.session.rollback()
                    db.engine.dispose()
                    time.sleep(5)
            else:
                raise SystemExit("[reclassify] DB unavailable — stopping (no partial batch written)")

            pending = []
            for s in sessions:
                seen += 1
                label = ""
                if s.house == "Lords":
                    ok, label = _fetch_label(s.ext_id)
                    time.sleep(_DELAY)
                    if not ok:
                        fetch_errors += 1
                        continue
                new = classify_session(
                    s.title or "", s.location or "", s.hrs_tag or "", s.house or "",
                    debate_type_label=label,
                    first_speech=_first_speech(db, HansardContribution, s.id),
                )
                old = s.debate_type or "None"
                if new != old:
                    key = (s.house, old, new)
                    changes[key] += 1
                    if len(samples[key]) < args.samples:
                        samples[key].append(f"{s.date} {(s.title or '')[:70]!r} [{label or s.hrs_tag}]")
                    pending.append((s, new))

            if args.apply and pending:
                for s, new in pending:
                    s.debate_type = new
                db.session.commit()
                written += len(pending)
            else:
                db.session.rollback()
            print(f"[reclassify] checked {seen}/{len(ids)} — changes so far {sum(changes.values())}", flush=True)

    print("\n[reclassify] Changes by house, old -> new:")
    for (house, old, new), n in sorted(changes.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {house:8s} {old} -> {new}")
        for line in samples[(house, old, new)]:
            print(f"            e.g. {line}")
    leaving_other = sum(n for (h, old, new), n in changes.items() if old == "other")
    print(f"\n[reclassify] checked={seen} would_change={sum(changes.values())} "
          f"leaving_other(untagged, need run_tagging)={leaving_other} fetch_errors={fetch_errors} "
          f"written={written}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--house", choices=("Commons", "Lords"))
    p.add_argument("--since", help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", help="YYYY-MM-DD, inclusive")
    p.add_argument("--limit", type=int, help="check at most N sessions (by id)")
    p.add_argument("--samples", type=int, default=3, help="example titles per change type")
    run(p.parse_args())


if __name__ == "__main__":
    main()
