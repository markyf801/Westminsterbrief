"""
Merge Written Questions stored more than once in ha_pq.

A duplicate group is a set of rows that share Parliament's api_id, or share
(tabled_date, lower(uin)) — Parliament's public key. Each group is reduced to
one canonical row:

  * keep: the row with the most complete data; ties go to the earliest id
  * fill: empty fields on the kept row are filled from the duplicates (a
    stored value is never overwritten; a missing or truncated answer is
    replaced by a full one)
  * themes: ha_pq_theme rows are moved onto the kept row, skipping themes it
    already has, then the duplicates' leftover theme rows are removed
  * references: every other foreign key to ha_pq.id (found from the database
    schema at run time, not a fixed list) is re-pointed to the kept row
  * delete: the duplicate rows

A group is left alone and listed for review when its rows disagree about which
question they are: different non-null api_ids, or the same api_id with a
different tabled_date / uin. Also groups larger than --max-group-size.

Set-based: duplicate keys come from two GROUP BY queries and rows are loaded
in batches, so any number of groups is fine. No Parliament API calls.

DRY RUN BY DEFAULT — prints every group and what would happen; writes nothing.
--apply writes, one transaction per group (rows locked and re-checked inside
it), and prints a summary. Safe to re-run: a merged group is no longer a
duplicate.

Usage:
  python scripts/dedupe_pq.py                 # dry run
  python scripts/dedupe_pq.py --apply
  python scripts/dedupe_pq.py --apply --limit 5

Exit codes: 0 clean; 1 a group failed to apply; 3 groups left for review.

Railway one-off: SKIP_MIGRATIONS=1 is set by default below. Not during a PQ
cron window. Take a snapshot first (docs/plans/pq-identity-phase3-sql.md).
"""

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import datetime

os.environ.setdefault("SKIP_MIGRATIONS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pq-dedupe] %(message)s",
                    datefmt="%H:%M:%S")
_log = logging.getLogger("pq-dedupe")

KEY_BATCH = 500           # keys per IN (...) query
DEFAULT_MAX_GROUP = 3

# Fields that count towards "most complete" and may be filled on the kept row.
FILL_FIELDS = (
    "api_id", "heading", "answer_date",
    "asking_member", "asking_mnis_id",
    "answering_member", "answering_mnis_id",
    "answering_body", "answering_body_id", "chamber",
)


def _empty(val) -> bool:
    return val is None or (isinstance(val, str) and not val.strip())


def _short(val, n=60) -> str:
    s = "None" if val is None else str(val)
    return s if len(s) <= n else s[:n] + "…"


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── finding groups ────────────────────────────────────────────────────────────

def find_duplicate_groups(session, HaPQ) -> list[list[int]]:
    """Row-id groups of duplicates (rows linked by api_id or (date, lower(uin)))."""
    from sqlalchemy import func, select

    uin_lc = func.lower(HaPQ.uin)
    dup_api_ids = session.execute(
        select(HaPQ.api_id).where(HaPQ.api_id.isnot(None))
        .group_by(HaPQ.api_id).having(func.count() > 1)
    ).scalars().all()
    dup_keys = {(d, u) for d, u in session.execute(
        select(HaPQ.tabled_date, uin_lc).group_by(HaPQ.tabled_date, uin_lc)
        .having(func.count() > 1)
    ).all()}

    rows = {}   # id -> (api_id, tabled_date, uin_lc)
    cols = (HaPQ.id, HaPQ.api_id, HaPQ.tabled_date, uin_lc)
    for chunk in _chunks(dup_api_ids, KEY_BATCH):
        for rid, api_id, d, u in session.execute(select(*cols).where(HaPQ.api_id.in_(chunk))):
            rows[rid] = (api_id, d, u)
    for chunk in _chunks(sorted(dup_keys), KEY_BATCH):
        dates = {d for d, _u in chunk}
        uins = {u for _d, u in chunk}
        wanted = set(chunk)
        for rid, api_id, d, u in session.execute(
                select(*cols).where(HaPQ.tabled_date.in_(dates), uin_lc.in_(uins))):
            if (d, u) in wanted:
                rows[rid] = (api_id, d, u)

    parent = {rid: rid for rid in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by_key = {}
    for rid in sorted(rows):
        api_id, d, u = rows[rid]
        keys = [("date_uin", d, u)]
        if api_id is not None:
            keys.append(("api_id", api_id))
        for key in keys:
            if key in first_by_key:
                union(rid, first_by_key[key])
            else:
                first_by_key[key] = rid

    groups: dict[int, list[int]] = {}
    for rid in rows:
        groups.setdefault(find(rid), []).append(rid)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: g[0])


# ── deciding what to do with one group ────────────────────────────────────────

def review_reason(rows, max_group: int) -> str:
    """Why this group must not be merged automatically ("" = safe to merge)."""
    api_ids = {r.api_id for r in rows if r.api_id is not None}
    if len(api_ids) > 1:
        return f"different api_ids {sorted(api_ids)} — not the same question"
    if len({r.tabled_date for r in rows}) > 1 or len({(r.uin or "").lower() for r in rows}) > 1:
        return "same api_id but different tabled_date/uin — stored identity disagrees"
    if len(rows) > max_group:
        return f"{len(rows)} rows (more than --max-group-size {max_group})"
    return ""


def _completeness(row) -> int:
    from hansard_archive.pq_ingestor import _looks_truncated
    score = sum(0 if _empty(getattr(row, f)) else 1 for f in FILL_FIELDS)
    if row.answer_text and not _looks_truncated(row.answer_text):
        score += 2
    elif row.answer_text:
        score += 1
    return score


def choose_keep(rows):
    return min(rows, key=lambda r: (-_completeness(r), r.id))


def planned_fills(keep, extras) -> dict:
    """field -> (source_row_id, value) to copy onto the kept row."""
    from hansard_archive.pq_ingestor import _looks_truncated
    fills = {}
    for name in FILL_FIELDS:
        if not _empty(getattr(keep, name)):
            continue
        for r in sorted(extras, key=lambda x: x.id):
            if not _empty(getattr(r, name)):
                fills[name] = (r.id, getattr(r, name))
                break
    if _empty(keep.answer_text) or _looks_truncated(keep.answer_text):
        full = [r for r in extras if r.answer_text and not _looks_truncated(r.answer_text)]
        if full:
            best = max(full, key=lambda r: (len(r.answer_text), -r.id))
            fills["answer_text"] = (best.id, best.answer_text)
    if not keep.is_answered and any(r.is_answered for r in extras):
        src = min((r for r in extras if r.is_answered), key=lambda r: r.id)
        fills["is_answered"] = (src.id, True)
    if _empty(keep.question_text):
        src = next((r for r in sorted(extras, key=lambda x: x.id)
                    if not _empty(r.question_text)), None)
        if src is not None:
            fills["question_text"] = (src.id, src.question_text)
    return fills


def other_fk_refs(engine) -> list[tuple[str, str]]:
    """(table, column) of every foreign key to ha_pq.id except ha_pq_theme."""
    from sqlalchemy import inspect
    insp = inspect(engine)
    refs = []
    for table in insp.get_table_names():
        for fk in insp.get_foreign_keys(table):
            if (fk.get("referred_table") == "ha_pq"
                    and fk.get("referred_columns") == ["id"]
                    and len(fk.get("constrained_columns") or []) == 1):
                col = fk["constrained_columns"][0]
                if (table, col) != ("ha_pq_theme", "pq_id"):
                    refs.append((table, col))
    return sorted(set(refs))


# ── applying one group ────────────────────────────────────────────────────────

def plan_group(session, HaPQ, HaPQTheme, ids, *, refs, max_group, lock) -> dict:
    """Load one group (row-locked when lock=True) and decide what to do.

    Returns a report: status "merge", "review" or "gone" plus the plan."""
    from sqlalchemy import column, func, select
    from sqlalchemy import table as sa_table

    query = HaPQ.query.filter(HaPQ.id.in_(ids)).order_by(HaPQ.id)
    if lock:
        query = query.with_for_update()
    rows = query.all()
    theme_counts = dict(session.execute(
        select(HaPQTheme.pq_id, func.count()).where(HaPQTheme.pq_id.in_(ids))
        .group_by(HaPQTheme.pq_id)).all())
    report = {"ids": ids, "rows": rows, "status": "", "reason": "",
              "theme_counts": theme_counts}
    if len(rows) < 2:
        report["status"] = "gone"     # already merged or deleted since the scan
        return report
    reason = review_reason(rows, max_group)
    if reason:
        report.update(status="review", reason=reason)
        return report

    keep = choose_keep(rows)
    extras = [r for r in rows if r.id != keep.id]
    extra_ids = [r.id for r in extras]
    fills = planned_fills(keep, extras)

    keep_themes = set(session.execute(
        select(HaPQTheme.theme).where(HaPQTheme.pq_id == keep.id)).scalars())
    moves = {}            # extra id -> themes to move onto keep
    seen = set(keep_themes)
    dropped = 0
    for r in extras:
        themes = list(session.execute(
            select(HaPQTheme.theme).where(HaPQTheme.pq_id == r.id)).scalars())
        move = [t for t in themes if t not in seen]
        seen.update(move)
        moves[r.id] = move
        dropped += len(themes) - len(move)

    ref_counts = {}
    for table, col in refs:
        t = sa_table(table, column(col))
        n = session.execute(select(func.count()).select_from(t)
                            .where(t.c[col].in_(extra_ids))).scalar()
        if n:
            ref_counts[(table, col)] = n

    report.update(status="merge", keep=keep, extras=extras, fills=fills, moves=moves,
                  themes_moved=sum(len(v) for v in moves.values()),
                  themes_dropped=dropped, ref_counts=ref_counts)
    return report


def apply_plan(session, HaPQ, HaPQTheme, report, *, refs) -> None:
    """Carry out a "merge" plan. The caller commits or rolls back."""
    from sqlalchemy import column, delete, update
    from sqlalchemy import table as sa_table

    keep, extras, fills, moves = (report["keep"], report["extras"],
                                  report["fills"], report["moves"])
    extra_ids = [r.id for r in extras]
    if "api_id" in fills:
        # The kept row takes over the duplicate's api_id: clear it on the
        # duplicates first so a unique api_id index (if built) is never violated.
        session.execute(update(HaPQ).where(HaPQ.id.in_(extra_ids)).values(api_id=None),
                        execution_options={"synchronize_session": False})
    for name, (_src, value) in fills.items():
        setattr(keep, name, value)
    if fills:
        keep.updated_at = datetime.utcnow()
    session.flush()
    for rid, move in moves.items():
        if move:
            session.execute(update(HaPQTheme)
                            .where(HaPQTheme.pq_id == rid, HaPQTheme.theme.in_(move))
                            .values(pq_id=keep.id))
    session.execute(delete(HaPQTheme).where(HaPQTheme.pq_id.in_(extra_ids)))
    for (table, col) in refs:
        t = sa_table(table, column(col))
        session.execute(update(t).where(t.c[col].in_(extra_ids)).values({col: keep.id}))
    for r in extras:
        session.expunge(r)
    session.execute(delete(HaPQ).where(HaPQ.id.in_(extra_ids)))
    session.flush()


def _describe(n, report) -> list[str]:
    from hansard_archive.pq_ingestor import _looks_truncated

    def row_line(tag, r):
        themes = report["theme_counts"].get(r.id, 0)
        ans = ("none" if not r.answer_text else
               f"{len(r.answer_text)}c{' TRUNCATED' if _looks_truncated(r.answer_text) else ''}")
        return (f"  {tag} id={r.id} api_id={r.api_id} {r.tabled_date} uin={r.uin} "
                f"heading={_short(r.heading, 40)!s} answer={ans} themes={themes} "
                f"ingested={r.ingested_at}")

    rows = report["rows"]
    head = rows[0] if rows else None
    lines = [f"Group {n}: ids={report['ids']}"
             + (f" api_id={head.api_id} {head.tabled_date} uin={head.uin}" if head else "")]
    if report["status"] == "gone":
        lines.append("  (rows no longer duplicated — skipped)")
        return lines
    if report["status"] == "review":
        lines += [row_line("    ", r) for r in rows]
        lines.append(f"  REVIEW — not merged: {report['reason']}")
        return lines
    lines.append(row_line("KEEP", report["keep"]))
    lines += [row_line("drop", r) for r in report["extras"]]
    for name, (src, value) in report["fills"].items():
        lines.append(f"  fill {name} <- id={src}: {_short(value)}")
    lines.append(f"  themes: move {report['themes_moved']}, drop {report['themes_dropped']} "
                 f"already on the kept row")
    for (table, col), cnt in report["ref_counts"].items():
        lines.append(f"  re-point {table}.{col}: {cnt} row(s)")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Merge duplicate Written Questions in ha_pq")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Show what would happen, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="Merge, one transaction per group")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N groups")
    parser.add_argument("--max-group-size", type=int, default=DEFAULT_MAX_GROUP,
                        help=f"Larger groups are left for review (default {DEFAULT_MAX_GROUP})")
    args = parser.parse_args(argv)
    apply = args.apply

    from flask_app import app
    from extensions import db
    from hansard_archive.models import HaPQ, HaPQTheme

    tally = Counter()
    review = []
    with app.app_context():
        refs = other_fk_refs(db.engine)
        _log.info("=== %s === other foreign keys to ha_pq.id: %s",
                  "APPLY" if apply else "DRY RUN (no writes)",
                  ", ".join(f"{t}.{c}" for t, c in refs) or "none (ha_pq_theme handled)")
        groups = find_duplicate_groups(db.session, HaPQ)
        db.session.rollback()   # end the scan's read transaction
        _log.info("%d duplicate group(s), %d row(s) in them",
                  len(groups), sum(len(g) for g in groups))
        if args.limit is not None:
            groups = groups[:args.limit]

        for n, ids in enumerate(groups, 1):
            try:
                report = plan_group(db.session, HaPQ, HaPQTheme, ids, refs=refs,
                                    max_group=args.max_group_size, lock=apply)
                for line in _describe(n, report):
                    _log.info(line)
                if apply and report["status"] == "merge":
                    apply_plan(db.session, HaPQ, HaPQTheme, report, refs=refs)
                    db.session.commit()
                else:
                    db.session.rollback()
            except Exception as exc:
                db.session.rollback()
                tally["failed"] += 1
                _log.error("Group %d ids=%s FAILED, rolled back: %s", n, ids, exc)
                continue
            tally[report["status"]] += 1
            if report["status"] == "review":
                review.append((ids, report["reason"]))
            if report["status"] == "merge":
                tally["rows_deleted"] += len(report["extras"])
                tally["themes_moved"] += report["themes_moved"]
                tally["themes_dropped"] += report["themes_dropped"]
                tally["fields_filled"] += len(report["fills"])
                for key, cnt in report["ref_counts"].items():
                    tally[f"refs {key[0]}.{key[1]}"] += cnt

        remaining = len(find_duplicate_groups(db.session, HaPQ)) if apply else None
        db.session.rollback()

    verb = "" if apply else "would be "
    _log.info("=== SUMMARY (%s) ===", "applied" if apply else "dry run — nothing written")
    _log.info("groups: %smerged=%d, left for review=%d, skipped (gone)=%d, failed=%d",
              verb, tally["merge"], tally["review"], tally["gone"], tally["failed"])
    _log.info("rows %sdeleted=%d; themes %smoved=%d, duplicates dropped=%d; fields filled=%d",
              verb, tally["rows_deleted"], verb, tally["themes_moved"],
              tally["themes_dropped"], tally["fields_filled"])
    for key in sorted(k for k in tally if k.startswith("refs ")):
        _log.info("%s re-pointed: %d", key[5:], tally[key])
    for ids, reason in review:
        _log.warning("REVIEW ids=%s: %s", ids, reason)
    if remaining is not None:
        _log.info("duplicate groups remaining after apply: %d", remaining)
    if tally["failed"]:
        return 1
    return 3 if review else 0


if __name__ == "__main__":
    sys.exit(main())
