"""
Delete duplicate copies of debates — DRY RUN BY DEFAULT; --execute deletes.

Why: the archive holds some debates two to five times (Hansard replaced a
provisional id with its final one and we kept both copies; see
hansard_archive/dedupe.py). scripts/find_duplicate_debates.py lists them; this
script removes the copies a person has reviewed, keeping one twin each.

Input: the CSV from find_duplicate_debates.py (--csv; only rows whose action
is "delete" are used, so edit an action there to override), and/or
--session-id DELETE:KEEP pairs (comma-separated, repeatable; the find script
prints them ready to paste). Every deletion names its kept twin.

Each pair is checked again just before deleting, and refused (skipped and
listed) when:
  - either session is missing, the twin is a container or has no URL slug,
    or the two are on different days or Houses;
  - the twin is itself listed for deletion (no chains);
  - the session is a container, or is dated in the last 4 days (the archive
    cron is still re-reading those days and would store a live id again);
  - the twin no longer holds at least 90% of its named contributions (small
    sessions, one short contribution: only an identical twin will do);
  - another session's contribution points at one of its contributions
    (responds_to_id);
  - a division (ha_division.ha_session_id, no foreign key) points at it,
    unless --repoint-divisions moves them to the twin;
  - a saved alert links to its page (alert.url).

--execute, one transaction per session (its row locked first; on Postgres
also the tagger's per-session advisory lock, pg_advisory_xact_lock(72616701,
id), so tags aren't deleted while the tagger writes them): copies into undo
tables, then deletes. Tags are backed up and deleted with the copy, never
moved to the twin (the twin keeps its own). Undo tables (named with the run's
UTC time):
  ha_session_dedupe_bak_<ts>          the session rows
  ha_contribution_dedupe_bak_<ts>     their contributions
  ha_session_theme_dedupe_bak_<ts>    their tags (policy areas and topics)
  ha_tag_attempt_dedupe_bak_<ts>      their tagging attempt counts (if that table exists)
  ha_session_dedupe_links_<ts>        which twin was kept, divisions re-pointed,
                                      redirects added or moved
It then writes ha_session_redirect (old slug -> twin), so the deleted copy's
URL 301s to the twin's (create the table first: migrations/
session_redirect_table.py --execute; --execute refuses to run without it),
and moves any redirect that pointed at the deleted copy to the twin. The undo
SQL is printed at the end (production SQL is Mark's call: log it in
docs/deploys.md).

Cron windows: --execute doesn't run in or just before a production cron window
(hansard_archive/cron_windows.py; weekday daytime is blocked whole): exit 4.
Override with --ignore-cron-windows. A dry run is not blocked.

Usage (Railway one-off; dry run first):
    python scripts/delete_duplicate_debates.py --csv dupes.csv
    python scripts/delete_duplicate_debates.py --session-id 123:456,789:456
    python scripts/delete_duplicate_debates.py --session-id 123:456 --execute

Exit codes: 0 done (refusals are listed but not an error), 2 bad arguments or
missing redirect table, 4 cron window.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

CRON_REFRESH_DAYS = 4          # the archive cron re-reads the last 3 days
SOFT_URL_REFS = (("alert", "url"),)
TAG_LOCK_NAMESPACE = 72616701  # pg_advisory_xact_lock(72616701, session_id): the tagger's per-session lock

UNDO_SQL = """\
-- Undo scripts/delete_duplicate_debates.py run {ts} (production SQL: Mark's call, log it).
-- 1. Remove the redirects this run added; point moved ones back.
DELETE FROM ha_session_redirect
 WHERE old_slug IN (SELECT old_slug FROM {links} WHERE kind = 'redirect_added');
UPDATE ha_session_redirect SET new_session_id = l.session_id
  FROM {links} l
 WHERE l.kind = 'redirect_moved' AND ha_session_redirect.old_slug = l.old_slug;
-- 2. Put the sessions back first, then their contributions and tags (foreign keys; same ids).
INSERT INTO ha_session ({session_cols}) SELECT {session_cols} FROM {bak_session};
INSERT INTO ha_contribution ({contrib_cols}) SELECT {contrib_cols} FROM {bak_contrib};
INSERT INTO ha_session_theme ({theme_cols}) SELECT {theme_cols} FROM {bak_theme};
{attempt_sql}-- 3. Point re-pointed divisions back.
UPDATE ha_division SET ha_session_id = l.session_id, updated_at = CURRENT_TIMESTAMP
  FROM {links} l
 WHERE l.kind = 'division' AND ha_division.id = l.division_id;
"""


class WindowStop(Exception):
    pass


def parse_pairs(values: list[str]) -> list[tuple[int, int]]:
    pairs = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            left, sep, right = part.partition(":")
            if not sep:
                raise ValueError(f"{part!r}: give DELETE:KEEP (the session to delete and its kept twin)")
            pairs.append((int(left), int(right)))
    return pairs


def read_csv(path: str) -> list[tuple[int, int]]:
    pairs = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("action") or "").strip() != "delete":
                continue
            if not (row.get("kept_session_id") or "").strip():
                raise ValueError(f"CSV row for session {row.get('session_id')} has no kept_session_id")
            pairs.append((int(row["session_id"]), int(row["kept_session_id"])))
    return pairs


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _table_columns(conn, table: str) -> list[str]:
    """Stored (non-generated) columns: Postgres has generated tsvector columns."""
    return [c["name"] for c in inspect(conn).get_columns(table) if not c.get("computed")]


def _has_table(conn, table: str) -> bool:
    return inspect(conn).has_table(table)


def refusal(pair: tuple[int, int], deleting: set, today: date, repoint: bool) -> str | None:
    """Why this pair must not be deleted now, or None. Reads the DB (call after locking)."""
    from extensions import db
    from hansard_archive import dedupe as dd
    from hansard_archive.models import HansardContribution, HansardSession

    sid, kid = pair
    if sid == kid:
        return "session and kept twin are the same"
    s, k = db.session.get(HansardSession, sid), db.session.get(HansardSession, kid)
    if s is None:
        return "session not found (already deleted?)"
    if k is None:
        return f"kept twin {kid} not found"
    if kid in deleting:
        return f"kept twin {kid} is itself listed for deletion"
    if k.is_container or not k.slug:
        return f"kept twin {kid} is a container or has no URL"
    if s.is_container:
        return "session is a container"
    if (s.date, s.house) != (k.date, k.house):
        return f"kept twin {kid} is on {k.date} {k.house}, not {s.date} {s.house}"
    if s.date >= today - timedelta(days=CRON_REFRESH_DAYS):
        return f"dated {s.date}: the archive cron is still re-reading that day"

    loaded = dd.load_sessions([sid, kid])
    a, b = loaded[sid], loaded[kid]
    if not a.named:
        return "session has no named contributions to compare"
    if a.small and a.signature != b.signature:
        return "small session and the twin is not identical"
    cov = dd.coverage(a, b)
    if cov < dd.COVERAGE_MIN:
        return f"kept twin holds only {cov:.0%} of its named contributions"

    own = db.session.query(HansardContribution.id).filter(HansardContribution.session_id == sid)
    outside = (HansardContribution.query
               .filter(HansardContribution.responds_to_id.in_(own.scalar_subquery()),
                       HansardContribution.session_id != sid).count())
    if outside:
        return f"{outside} contribution(s) in other sessions respond to its contributions"

    conn = db.session.connection()
    if not repoint and _has_table(conn, "ha_division"):
        n = db.session.execute(text("SELECT COUNT(*) FROM ha_division WHERE ha_session_id = :s"),
                               {"s": sid}).scalar()
        if n:
            return f"{n} division(s) point at it (use --repoint-divisions to move them to {kid})"
    if s.slug:
        for table, col in SOFT_URL_REFS:
            if _has_table(conn, table):
                n = db.session.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE :p"),
                    {"p": f"%/archive/debate/%/{s.slug}%"}).scalar()
                if n:
                    return f"{n} {table} row(s) link to its page"
    return None


# ---------------------------------------------------------------------------
# Undo tables
# ---------------------------------------------------------------------------

def create_undo_tables(db, ts: str) -> dict:
    conn = db.session.connection()
    names = {"session": f"ha_session_dedupe_bak_{ts}",
             "contrib": f"ha_contribution_dedupe_bak_{ts}",
             "theme": f"ha_session_theme_dedupe_bak_{ts}",
             "links": f"ha_session_dedupe_links_{ts}"}
    sources = {"session": "ha_session", "contrib": "ha_contribution", "theme": "ha_session_theme"}
    if _has_table(conn, "ha_tag_attempt"):
        names["attempt"] = f"ha_tag_attempt_dedupe_bak_{ts}"
        sources["attempt"] = "ha_tag_attempt"
    existing = set(inspect(conn).get_table_names())
    clash = [n for n in names.values() if n in existing]
    if clash:
        raise SystemExit(f"[dedupe] undo table already exists: {clash[0]} — run again in a second")
    cols = {}
    for key, src in sources.items():
        cols[key] = _table_columns(conn, src)
        col_list = ", ".join(cols[key])
        db.session.execute(text(f"CREATE TABLE {names[key]} AS SELECT {col_list} FROM {src} WHERE 1 = 0"))
    db.session.execute(text(
        f"CREATE TABLE {names['links']} (kind VARCHAR(20) NOT NULL, session_id INTEGER, "
        f"new_session_id INTEGER, division_id INTEGER, old_slug VARCHAR(200), "
        f"created_at TIMESTAMP NOT NULL)"))
    db.session.commit()
    print(f"[dedupe] undo tables: {', '.join(names.values())}", flush=True)
    return {"names": names, "cols": cols}


def undo_sql(ts: str, undo: dict) -> str:
    names, cols = undo["names"], undo["cols"]
    attempt = ""
    if "attempt" in names:
        c = ", ".join(cols["attempt"])
        attempt = f"INSERT INTO ha_tag_attempt ({c}) SELECT {c} FROM {names['attempt']};\n"
    return UNDO_SQL.format(ts=ts, links=names["links"], bak_session=names["session"],
                           bak_contrib=names["contrib"], bak_theme=names["theme"],
                           session_cols=", ".join(cols["session"]),
                           contrib_cols=", ".join(cols["contrib"]),
                           theme_cols=", ".join(cols["theme"]), attempt_sql=attempt)


def _link(db, links: str, kind: str, now: datetime, session_id=None, new_session_id=None,
          division_id=None, old_slug=None) -> None:
    db.session.execute(
        text(f"INSERT INTO {links} (kind, session_id, new_session_id, division_id, old_slug, "
             f"created_at) VALUES (:k, :s, :n, :d, :o, :t)"),
        {"k": kind, "s": session_id, "n": new_session_id, "d": division_id, "o": old_slug, "t": now})


def delete_one(db, sid: int, kid: int, undo: dict, repoint: bool, now: datetime) -> dict:
    """Back up and delete one session inside the current transaction. Caller commits."""
    from hansard_archive.models import HansardSession
    names, cols = undo["names"], undo["cols"]
    conn = db.session.connection()
    s = db.session.get(HansardSession, sid)
    slug = s.slug
    counts = {}
    if conn.dialect.name == "postgresql":
        # The tagger holds this lock while it writes a session's tags: don't delete
        # the tags under it. Released at commit / rollback. The kept twin's tags
        # are never touched (the deleted copy's are backed up and deleted, not moved).
        db.session.execute(text("SELECT pg_advisory_xact_lock(:ns, :s)"),
                           {"ns": TAG_LOCK_NAMESPACE, "s": sid})

    def backup(key: str, src: str, where: str, params: dict) -> int:
        c = ", ".join(cols[key])
        return db.session.execute(
            text(f"INSERT INTO {names[key]} ({c}) SELECT {c} FROM {src} WHERE {where}"),
            params).rowcount

    counts["session"] = backup("session", "ha_session", "id = :s", {"s": sid})
    counts["contributions"] = backup("contrib", "ha_contribution", "session_id = :s", {"s": sid})
    counts["tags"] = backup("theme", "ha_session_theme", "session_id = :s", {"s": sid})
    if "attempt" in names:
        counts["tag_attempts"] = backup("attempt", "ha_tag_attempt",
                                        "item_kind = 'session' AND item_id = :s", {"s": sid})
    _link(db, names["links"], "deleted", now, session_id=sid, new_session_id=kid, old_slug=slug)

    counts["divisions"] = 0
    if repoint and _has_table(conn, "ha_division"):
        for (div_id,) in db.session.execute(
                text("SELECT id FROM ha_division WHERE ha_session_id = :s"), {"s": sid}).fetchall():
            _link(db, names["links"], "division", now, session_id=sid, new_session_id=kid,
                  division_id=div_id)
            counts["divisions"] += 1
        db.session.execute(text("UPDATE ha_division SET ha_session_id = :k, updated_at = :t "
                                "WHERE ha_session_id = :s"), {"k": kid, "t": now, "s": sid})

    for (old_slug,) in db.session.execute(
            text("SELECT old_slug FROM ha_session_redirect WHERE new_session_id = :s"),
            {"s": sid}).fetchall():
        _link(db, names["links"], "redirect_moved", now, session_id=sid, new_session_id=kid,
              old_slug=old_slug)
    db.session.execute(text("UPDATE ha_session_redirect SET new_session_id = :k "
                            "WHERE new_session_id = :s"), {"k": kid, "s": sid})

    db.session.execute(text("DELETE FROM ha_session_theme WHERE session_id = :s"), {"s": sid})
    if "attempt" in names:
        db.session.execute(text("DELETE FROM ha_tag_attempt WHERE item_kind = 'session' "
                                "AND item_id = :s"), {"s": sid})
    db.session.execute(text("DELETE FROM ha_contribution WHERE session_id = :s"), {"s": sid})
    db.session.execute(text("DELETE FROM ha_session WHERE id = :s"), {"s": sid})

    if slug:
        prev = db.session.execute(text("SELECT new_session_id FROM ha_session_redirect "
                                       "WHERE old_slug = :o"), {"o": slug}).fetchone()
        if prev is None:
            db.session.execute(text("INSERT INTO ha_session_redirect (old_slug, new_session_id, "
                                    "created_at) VALUES (:o, :k, :t)"),
                               {"o": slug, "k": kid, "t": now})
            _link(db, names["links"], "redirect_added", now, new_session_id=kid, old_slug=slug)
        elif prev[0] != kid:
            _link(db, names["links"], "redirect_moved", now, session_id=prev[0],
                  new_session_id=kid, old_slug=slug)
            db.session.execute(text("UPDATE ha_session_redirect SET new_session_id = :k "
                                    "WHERE old_slug = :o"), {"k": kid, "o": slug})
    db.session.expunge(s)
    return counts


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def check_cron_window(args, clock) -> None:
    from hansard_archive.cron_windows import blocked_window
    if args.ignore_cron_windows:
        return
    label = blocked_window(clock())
    if label:
        raise WindowStop(label)


def run(args, pairs: list[tuple[int, int]], clock=datetime.utcnow, today: date | None = None) -> dict:
    from extensions import db
    from hansard_archive.models import HansardSession

    today = today or clock().date()
    pairs = list(dict.fromkeys(pairs))
    seen: dict[int, int] = {}
    for sid, kid in pairs:
        if sid in seen and seen[sid] != kid:
            print(f"[dedupe] session {sid} is listed with two kept twins ({seen[sid]}, {kid})",
                  file=sys.stderr)
            return {"exit_code": 2, "deleted": [], "refused": []}
        seen[sid] = kid
    deleting = set(seen)
    mode = "EXECUTE" if args.execute else "dry run — nothing is written"
    print(f"[dedupe] {len(seen)} session(s) listed for deletion ({mode})", flush=True)

    conn = db.session.connection()
    has_redirects = _has_table(conn, "ha_session_redirect")
    db.session.rollback()
    if not has_redirects:
        msg = ("ha_session_redirect table missing: run migrations/session_redirect_table.py "
               "--execute first (deleted copies' URLs must redirect)")
        if args.execute:
            print(f"[dedupe] {msg}", file=sys.stderr)
            return {"exit_code": 2, "deleted": [], "refused": []}
        print(f"[dedupe] WARNING: {msg}", flush=True)

    deleted, refused = [], []
    undo = None
    ts = clock().strftime("%Y%m%d_%H%M%S")
    stopped = None
    try:
        if args.execute and seen:
            check_cron_window(args, clock)
        for sid, kid in seen.items():
            if args.execute:
                check_cron_window(args, clock)
                (db.session.query(HansardSession.id).filter(HansardSession.id == sid)
                 .with_for_update().all())       # lock (Postgres); no-op on SQLite
            why = refusal((sid, kid), deleting, today, args.repoint_divisions)
            s = db.session.get(HansardSession, sid)
            label = (f"id={sid} {s.date} {s.house} {(s.title or '')[:60]!r} ({s.ext_id})"
                     if s is not None else f"id={sid}")
            if why:
                refused.append((sid, kid, why))
                print(f"[dedupe] REFUSED {label}: {why}", flush=True)
                db.session.rollback()
                continue
            if not args.execute:
                deleted.append((sid, kid, {}))
                print(f"[dedupe] would delete {label}; keep id={kid}", flush=True)
                db.session.rollback()
                continue
            if undo is None:
                db.session.rollback()
                undo = create_undo_tables(db, ts)
                (db.session.query(HansardSession.id).filter(HansardSession.id == sid)
                 .with_for_update().all())
                why = refusal((sid, kid), deleting, today, args.repoint_divisions)
                if why:
                    refused.append((sid, kid, why))
                    print(f"[dedupe] REFUSED {label}: {why}", flush=True)
                    db.session.rollback()
                    continue
            try:
                counts = delete_one(db, sid, kid, undo, args.repoint_divisions, clock())
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            deleted.append((sid, kid, counts))
            print(f"[dedupe] DELETED {label}; kept id={kid} — "
                  + ", ".join(f"{k}={v}" for k, v in counts.items()), flush=True)
    except WindowStop as exc:
        db.session.rollback()
        stopped = f"cron window due: {exc} — run the same command later (done ones are skipped)"

    verb = "deleted" if args.execute else "would be deleted"
    print(f"\n[dedupe] {len(deleted)} {verb}, {len(refused)} refused")
    for sid, kid, why in refused:
        print(f"  refused id={sid} (keep {kid}): {why}")
    if undo:
        print("\n[dedupe] undo SQL (production SQL is Mark's call; log it in docs/deploys.md):\n"
              + undo_sql(ts, undo))
    if stopped:
        print(f"\n[dedupe] STOPPED: {stopped}")
    return {"exit_code": 4 if stopped else 0, "deleted": deleted, "refused": refused,
            "undo": undo, "ts": ts}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--csv", help="CSV from scripts/find_duplicate_debates.py (rows with action 'delete')")
    p.add_argument("--session-id", action="append",
                   help="DELETE:KEEP pairs, comma-separated (repeatable)")
    p.add_argument("--execute", action="store_true", help="delete (default: dry run)")
    p.add_argument("--repoint-divisions", action="store_true",
                   help="move divisions pointing at a deleted copy to its kept twin")
    p.add_argument("--ignore-cron-windows", action="store_true")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    try:
        pairs = parse_pairs(args.session_id or [])
        if args.csv:
            pairs += read_csv(args.csv)
    except (OSError, ValueError, KeyError) as exc:
        print(f"delete_duplicate_debates.py: {exc}", file=sys.stderr)
        return 2
    if not pairs:
        p.print_usage()
        print("delete_duplicate_debates.py: give --csv or --session-id DELETE:KEEP", file=sys.stderr)
        return 2

    from flask_app import app
    with app.app_context():
        result = run(args, pairs)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
