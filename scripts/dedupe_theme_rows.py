"""
Remove topic tags stored more than once in ha_session_theme.

A duplicate is two or more ha_session_theme rows with the same
(session_id, theme, theme_type). ha_session_theme has no unique constraint, and
before the 22 Oct 2026 tagger release the tagger wrote whatever policy-area list
Gemini returned, repeats included (a JSON-schema enum doesn't forbid the same
value twice). Search copes (it uses EXISTS), but some counts and lists don't.

Per duplicate group one row is kept: the most informative one (most of
confidence / model_used / tagged_at filled in, then the highest confidence),
then the lowest id (the tagger writes the most central area first, and pages
rank areas by id). The rest are deleted.

Also checks, report only, the other tag tables, which all have a primary key or
unique constraint in the models so should hold no duplicates: ha_pq_theme
(primary key pq_id + theme), ha_bill_theme, ha_stat_publication_theme,
consultation_topic, manifesto_chunk_tag, sd_policy_area_tag. If one does have
duplicates, the production table doesn't match the model: the script exits 3
and changes nothing in it — investigate first.

DRY RUN BY DEFAULT: reports duplicate groups per table and theme_type, how many
groups look like one write (rows share tagged_at and model_used: the old
tagger) or separate writes (a race between two runs), a sample, and the rows
that would be deleted. Writes nothing.

--execute, one transaction for ha_session_theme:
  1. on Postgres, LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE (reads carry on;
     tag writers wait a moment; lock_timeout 10s), then re-plan inside it
  2. copy the rows to be deleted into ha_theme_dedupe_bak_<YYYYMMDD_HHMMSS>
  3. delete them, check none are left and the backup holds every one
  4. print the undo SQL
Refuses to run in a production cron window (hansard_archive/cron_windows.py)
unless --ignore-cron-windows. Safe to re-run: a clean table has nothing to do.

Usage (Railway one-off, SKIP_MIGRATIONS=1, Dockerfile.cron):
  python scripts/dedupe_theme_rows.py              # dry run
  python scripts/dedupe_theme_rows.py --execute

Exit codes: 0 ok; 1 error (rolled back) or cron window; 3 duplicates found in a
report-only table (nothing changed there).

Then, optionally, migrations/theme_unique_indexes.py adds a unique index so a
duplicate can't be stored again.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa

os.environ.setdefault("SKIP_MIGRATIONS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEDUPE_TABLE = "ha_session_theme"
DEDUPE_KEY = ("session_id", "theme", "theme_type")
INFO_COLS = ("confidence", "model_used", "tagged_at")
BAK_PREFIX = "ha_theme_dedupe_bak_"

# (table, key columns) checked and reported only.
REPORT_ONLY = (
    ("ha_pq_theme", ("pq_id", "theme", "theme_type")),
    ("ha_bill_theme", ("bill_id", "theme")),
    ("ha_stat_publication_theme", ("publication_id", "theme", "theme_type")),
    ("consultation_topic", ("consultation_id", "policy_area")),
    ("manifesto_chunk_tag", ("chunk_id", "policy_area")),
    ("sd_policy_area_tag", ("organisation_id", "area")),
)

_BATCH = 500
SAMPLE = 10


@dataclass
class Group:
    key: tuple
    rows: list[dict]
    keep_id: int
    delete_ids: list[int]
    same_write: bool


@dataclass
class Plan:
    total_rows: int = 0
    groups: list[Group] = field(default_factory=list)

    @property
    def delete_ids(self) -> list[int]:
        return sorted(i for g in self.groups for i in g.delete_ids)

    def by_type(self) -> Counter:
        return Counter(g.key[2] for g in self.groups)


def _table(conn, name: str) -> sa.Table:
    return sa.Table(name, sa.MetaData(), autoload_with=conn, resolve_fks=False)


def _rank(row: dict):
    """Sort key: most informative first, then highest confidence, then lowest id."""
    filled = sum(row.get(c) is not None for c in INFO_COLS)
    conf = row.get("confidence")
    return (-filled, -(conf if conf is not None else float("-inf")), row["id"])


def plan_dedupe(conn) -> Plan | None:
    """Duplicate groups in ha_session_theme and the rows to delete; None if the table is missing."""
    if not sa.inspect(conn).has_table(DEDUPE_TABLE):
        return None
    t = _table(conn, DEDUPE_TABLE)
    keys = [t.c[k] for k in DEDUPE_KEY]
    plan = Plan(total_rows=conn.execute(sa.select(sa.func.count()).select_from(t)).scalar() or 0)
    g = (sa.select(*keys).group_by(*keys).having(sa.func.count() > 1).subquery())
    rows = conn.execute(
        sa.select(t).join(g, sa.and_(*[t.c[k] == g.c[k] for k in DEDUPE_KEY]))
        .order_by(*keys, t.c.id)).mappings().all()
    grouped: dict[tuple, list[dict]] = {}
    for r in rows:
        grouped.setdefault(tuple(r[k] for k in DEDUPE_KEY), []).append(dict(r))
    for key, members in grouped.items():
        keep = min(members, key=_rank)
        writes = {(m.get("tagged_at"), m.get("model_used")) for m in members}
        plan.groups.append(Group(key=key, rows=members, keep_id=keep["id"],
                                 delete_ids=[m["id"] for m in members if m["id"] != keep["id"]],
                                 same_write=len(writes) == 1))
    return plan


def count_duplicates(conn, table: str, key: tuple) -> tuple[int, int] | None:
    """(duplicate groups, surplus rows) for a table, or None if it (or a key column) is missing."""
    if not sa.inspect(conn).has_table(table):
        return None
    t = _table(conn, table)
    if any(k not in t.c for k in key):
        return None
    cols = [t.c[k] for k in key]
    g = (sa.select(sa.func.count().label("n")).select_from(t)
         .group_by(*cols).having(sa.func.count() > 1).subquery())
    groups, rows = conn.execute(sa.select(sa.func.count(), sa.func.sum(g.c.n)).select_from(g)).one()
    return groups or 0, (rows or 0) - (groups or 0)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _backup_name(conn, stamp: str) -> str:
    insp = sa.inspect(conn)
    name, n = f"{BAK_PREFIX}{stamp}", 1
    while insp.has_table(name):
        n += 1
        name = f"{BAK_PREFIX}{stamp}_{n}"
    return name


def execute_dedupe(engine, stamp: str | None = None) -> tuple[str | None, int, list[str]]:
    """
    Delete the surplus rows in one transaction, after copying them to a backup
    table. Returns (backup table or None, rows deleted, column names).
    Raises (and rolls back) if the end state isn't what was planned.
    """
    stamp = stamp or datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            conn.execute(sa.text(f"LOCK TABLE {DEDUPE_TABLE} IN SHARE ROW EXCLUSIVE MODE"))
        plan = plan_dedupe(conn)
        if plan is None or not plan.groups:
            return None, 0, []
        ids = plan.delete_ids
        t = _table(conn, DEDUPE_TABLE)
        cols = [c.name for c in t.columns]
        bak = _backup_name(conn, stamp)
        conn.execute(sa.text(f"CREATE TABLE {bak} AS SELECT * FROM {DEDUPE_TABLE} WHERE 1 = 0"))
        ins = sa.text(f"INSERT INTO {bak} SELECT * FROM {DEDUPE_TABLE} WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True))
        deleted = 0
        for batch in _chunks(ids, _BATCH):
            conn.execute(ins, {"ids": batch})
            deleted += conn.execute(t.delete().where(t.c.id.in_(batch))).rowcount
        backed_up = conn.execute(sa.text(f"SELECT COUNT(*) FROM {bak}")).scalar()
        after = plan_dedupe(conn)
        if deleted != len(ids) or backed_up != len(ids) or after.groups:
            raise RuntimeError(f"end state check failed: planned {len(ids)}, deleted {deleted}, "
                               f"backed up {backed_up}, groups left {len(after.groups)}")
    return bak, deleted, cols


def undo_sql(bak: str, cols: list[str]) -> str:
    """Re-insert the deleted rows whose kept partner is still there (a re-tag since
    may have replaced the session's tags; those rows are not brought back)."""
    c = ", ".join(cols)
    bc = ", ".join(f"b.{x}" for x in cols)
    same_key = " AND ".join(f"k.{k} = b.{k}" for k in DEDUPE_KEY)
    return ("-- Drop uq_ha_session_theme_session_theme_type first (if created). "
            "Only valid before any re-tag of these sessions.\n"
            f"INSERT INTO {DEDUPE_TABLE} ({c})\n  SELECT {bc} FROM {bak} b\n"
            f"  WHERE EXISTS (SELECT 1 FROM {DEDUPE_TABLE} k WHERE {same_key});\n"
            f"-- once the result is confirmed good: DROP TABLE {bak};  (Mark's call)")


def _fmt(v) -> str:
    return "NULL" if v is None else str(v)


def print_plan(plan: Plan | None) -> None:
    if plan is None:
        print(f"[theme-dedupe] {DEDUPE_TABLE}: table not present, nothing to do", flush=True)
        return
    n_del = len(plan.delete_ids)
    print(f"[theme-dedupe] {DEDUPE_TABLE}: {plan.total_rows:,} rows, {len(plan.groups):,} duplicate "
          f"group(s), {n_del:,} row(s) to delete", flush=True)
    if not plan.groups:
        return
    for theme_type, n in sorted(plan.by_type().items()):
        print(f"[theme-dedupe]   theme_type={theme_type}: {n:,} group(s)", flush=True)
    same = sum(g.same_write for g in plan.groups)
    print(f"[theme-dedupe]   {same:,} group(s) from one write (same tagged_at + model_used), "
          f"{len(plan.groups) - same:,} from separate writes", flush=True)
    raced = sorted({g.key[0] for g in plan.groups if not g.same_write})
    if raced:
        ids = ",".join(str(i) for i in raced)
        print(f"[theme-dedupe]   separate-write sessions (two runs' tag sets mixed): {ids}", flush=True)
        print("[theme-dedupe]   to give them one clean tag set after the dedupe (dry run first, "
              "then add --apply --backup):", flush=True)
        print(f"[theme-dedupe]     python scripts/retag.py --what sessions --ids {ids}", flush=True)
    print(f"[theme-dedupe]   sample (first {min(SAMPLE, len(plan.groups))}):", flush=True)
    for g in plan.groups[:SAMPLE]:
        print(f"[theme-dedupe]   session {g.key[0]} [{g.key[2]}] {g.key[1]!r}: keep id {g.keep_id}, "
              f"delete {g.delete_ids}", flush=True)
        for r in g.rows:
            print(f"[theme-dedupe]       id {r['id']}: "
                  + ", ".join(f"{c}={_fmt(r.get(c))}" for c in INFO_COLS), flush=True)


def report_other_tables(conn) -> int:
    """Print duplicate counts for the report-only tables; return how many have duplicates."""
    bad = 0
    for table, key in REPORT_ONLY:
        res = count_duplicates(conn, table, key)
        if res is None:
            print(f"[theme-dedupe] {table}: not present, skipped", flush=True)
            continue
        groups, surplus = res
        flag = "" if not groups else "  <-- UNEXPECTED: the model has a key on these columns; investigate"
        print(f"[theme-dedupe] {table} ({', '.join(key)}): {groups:,} duplicate group(s), "
              f"{surplus:,} surplus row(s) (report only){flag}", flush=True)
        bad += bool(groups)
    return bad


def main(argv=None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Remove duplicate ha_session_theme rows (dry run by default).")
    ap.add_argument("--execute", action="store_true", help="delete the duplicates (default: dry run)")
    ap.add_argument("--ignore-cron-windows", action="store_true")
    args = ap.parse_args(argv)

    ctx = None
    if engine is None:
        from flask_app import app
        from extensions import db
        ctx = app.app_context()
        ctx.push()
        engine = db.engine
    try:
        print(f"[theme-dedupe] Target: {engine.url.render_as_string(hide_password=True)}", flush=True)
        print(f"[theme-dedupe] {'EXECUTE' if args.execute else 'DRY RUN — pass --execute to apply'}",
              flush=True)
        with engine.connect() as conn:
            plan = plan_dedupe(conn)
            print_plan(plan)
            bad_other = report_other_tables(conn)

        if args.execute and plan is not None and plan.groups:
            if not args.ignore_cron_windows:
                from hansard_archive.cron_windows import blocked_window
                label = blocked_window(datetime.utcnow())
                if label:
                    print(f"[theme-dedupe] Not run: cron window ({label}). Try later, or pass "
                          "--ignore-cron-windows.", flush=True)
                    return 1
            try:
                bak, deleted, cols = execute_dedupe(engine)
            except Exception as e:  # noqa: BLE001 - report and exit non-zero; the transaction rolled back
                print(f"[theme-dedupe] FAILED, rolled back, nothing changed: {type(e).__name__}: {e}",
                      flush=True)
                return 1
            if bak:
                print(f"[theme-dedupe] Deleted {deleted:,} row(s); copies in {bak}.", flush=True)
                print("[theme-dedupe] Undo:\n" + undo_sql(bak, cols), flush=True)
            else:
                print("[theme-dedupe] Nothing left to delete (cleaned meanwhile).", flush=True)

        if bad_other:
            print(f"[theme-dedupe] {bad_other} report-only table(s) hold duplicates: not changed, "
                  "investigate.", flush=True)
            return 3
        return 0
    finally:
        if ctx is not None:
            ctx.pop()


if __name__ == "__main__":
    sys.exit(main())
