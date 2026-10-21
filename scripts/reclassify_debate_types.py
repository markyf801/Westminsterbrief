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

Rows that earlier one-off scripts set by hand are skipped unless
--include-manual-fixes, and listed as their own group in the output:
  - scripts/apply_lords_classify_fixes.py: Lords SI titles -> statutory_instrument,
    procedural titles in Grand Committee -> other
  - scripts/backfill_lords_oq_classify.py: Lords sessions opening "To ask His
    Majesty's Government..." -> oral_questions
Review that group in the dry run before deciding to include it (for example,
Questions for Short Debate open the same way and were made oral_questions).

Usage (Railway one-off against production, dry run first):
    python scripts/reclassify_debate_types.py                     # dry run, all rows
    python scripts/reclassify_debate_types.py --house Lords --since 2026-06-01 --limit 200
    python scripts/reclassify_debate_types.py --apply             # write changes
    python scripts/reclassify_debate_types.py --apply --include-manual-fixes

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


# Same title list scripts/apply_lords_classify_fixes.py used for its Fix 3.
_MANUAL_PROCEDURAL_STARTS = (
    "arrangement of business", "business of the house", "oaths and affirmations",
    "retirement of a member", "retirements of members", "lord speaker's statement",
    "standing orders", "clerk of the parliaments", "leave of absence",
    "deaths of members", "message from the king", "royal assent",
)


def _first_attributed_speech(db, HansardContribution, session_id: int) -> str:
    """First speech with a member name — what backfill_lords_oq_classify.py matched on."""
    row = (
        db.session.query(HansardContribution.speech_text)
        .filter(HansardContribution.session_id == session_id,
                HansardContribution.member_name.isnot(None))
        .order_by(HansardContribution.speech_order)
        .first()
    )
    return (row[0] if row else "") or ""


def manual_fix_reason(house: str, old: str, title: str, first_attributed_speech) -> str | None:
    """
    Which earlier one-off script set this row's current type, or None. Uses the
    same predicates those scripts selected rows with. `first_attributed_speech`
    is a string or a zero-argument callable (only called when needed).
    """
    from hansard_archive.ingestion import _MADE_SI_RE
    if house != "Lords":
        return None
    t = (title or "").strip().lower()
    if old == "statutory_instrument" and _MADE_SI_RE.search(t):
        return "apply_lords_classify_fixes (SI title)"
    if old == "other" and any(t.startswith(p) for p in _MANUAL_PROCEDURAL_STARTS):
        return "apply_lords_classify_fixes (procedural title)"
    if old == "oral_questions":
        speech = first_attributed_speech() if callable(first_attributed_speech) else first_attributed_speech
        if (speech or "").lower().startswith("to ask his majesty"):
            return "backfill_lords_oq_classify"
    return None


def run(args) -> None:
    from flask_app import app
    from extensions import db
    from hansard_archive.ingestion import classify_session
    from hansard_archive.models import HansardContribution, HansardSession

    changes: collections.Counter = collections.Counter()
    samples: dict = collections.defaultdict(list)
    manual: collections.Counter = collections.Counter()
    manual_samples: dict = collections.defaultdict(list)
    include_manual = getattr(args, "include_manual_fixes", False)
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
                    reason = manual_fix_reason(
                        s.house, old, s.title,
                        lambda sid=s.id: _first_attributed_speech(db, HansardContribution, sid))
                    if reason:
                        mkey = (reason,) + key
                        manual[mkey] += 1
                        if len(manual_samples[mkey]) < args.samples:
                            manual_samples[mkey].append(
                                f"{s.date} {(s.title or '')[:70]!r} [{label or s.hrs_tag}]")
                        if not include_manual:
                            continue
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
    state = ("INCLUDED above" if include_manual
             else "SKIPPED — pass --include-manual-fixes to change them")
    print(f"\n[reclassify] Rows set by earlier one-off scripts ({state}):")
    if not manual:
        print("  none")
    for (reason, house, old, new), n in sorted(manual.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {house:8s} {old} -> {new}   [{reason}]")
        for line in manual_samples[(reason, house, old, new)]:
            print(f"            e.g. {line}")
    leaving_other = sum(n for (h, old, new), n in changes.items() if old == "other")
    manual_skipped = 0 if include_manual else sum(manual.values())
    print(f"\n[reclassify] checked={seen} would_change={sum(changes.values())} "
          f"leaving_other(untagged, need run_tagging)={leaving_other} fetch_errors={fetch_errors} "
          f"manual_fixes_skipped={manual_skipped} written={written}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--house", choices=("Commons", "Lords"))
    p.add_argument("--since", help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", help="YYYY-MM-DD, inclusive")
    p.add_argument("--limit", type=int, help="check at most N sessions (by id)")
    p.add_argument("--samples", type=int, default=3, help="example titles per change type")
    p.add_argument("--include-manual-fixes", action="store_true",
                   help="also change rows set by apply_lords_classify_fixes.py / "
                        "backfill_lords_oq_classify.py")
    run(p.parse_args())


if __name__ == "__main__":
    main()
