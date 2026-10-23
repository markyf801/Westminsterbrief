"""
Delete duplicate copies of debates — DRY RUN BY DEFAULT; --execute deletes.

Why: the archive holds some debates two to five times (Hansard replaced a
provisional id with its final one and we kept both copies; see
hansard_archive/dedupe.py). scripts/find_duplicate_debates.py lists them; this
script removes the copies a person has reviewed, keeping one twin each.

Input: the CSV from find_duplicate_debates.py (--csv; only rows whose action
is "delete" are used), and/or --session-id DELETE:KEEP pairs (comma-separated,
repeatable). Every deletion names its kept twin. Turning a "review" row into
"delete" by hand is at your own risk: every check below still applies.

Hansard is asked again (at least 1 second apart) about every id involved,
before anything is locked or written: the copy to delete must be DEAD (HTTP
400/404; a 200 without a debate only if a second look-up agrees) and the twin
LIVE. A 429/401/403, or three failed look-ups in a row, stops the run (exit 3)
before anything is deleted.

Each pair is then checked again, inside its transaction, and refused (skipped
and listed) when:
  - either session is missing, the twin is a container or has no URL slug,
    or the two are on different days or Houses, or have a different title or
    location;
  - the twin is itself listed for deletion (no chains);
  - the session is a container, or is dated in the last 4 days (the archive
    cron is still re-reading those days and would store a live id again);
  - the session's id is not confirmed dead, or the twin's not live;
  - the twin holds under 90% of its named contributions, or any of its
    speeches is held neither by the twin nor by another stored session that
    day (the refusal lists them); small sessions (one short contribution)
    need an identical twin;
  - the twin is much bigger and has speeches of its own before or among the
    shared ones (it's a heading holding other debates, not a later copy), or
    Hansard now has fewer speeches for it than we hold (a swallower), or it
    holds every speech of a debate with another title;
  - another session's contribution points at one of its contributions
    (responds_to_id);
  - a division (ha_division.ha_session_id, no foreign key) points at it,
    unless --repoint-divisions moves them to the twin;
  - a saved alert links to its page (alert.url).

--execute, one transaction per session. Locks, in this order: the sitting
day's (pg_advisory_xact_lock(72616702, day and House), shared with
refresh_partial_debates.py --accept-shrink so the two never check and change
the same day at once), the tagger's
per-session advisory lock (Postgres: pg_advisory_xact_lock(72616701, id), so
tags aren't deleted while the tagger writes them), the session row FOR
UPDATE, the twin's row FOR SHARE. Then it copies into undo tables and
deletes. Tags are backed up and deleted with the copy, never moved to the
twin (the twin keeps its own). Undo tables (named with the run's UTC time):
  ha_session_dedupe_bak_<ts>          the session rows
  ha_contribution_dedupe_bak_<ts>     their contributions
  ha_session_theme_dedupe_bak_<ts>    their tags (policy areas and topics)
  ha_tag_attempt_dedupe_bak_<ts>      their tagging attempt counts (if that table exists)
  ha_session_dedupe_links_<ts>        which twin was kept, divisions re-pointed,
                                      redirects added or moved, old -> new
                                      contribution ids
It writes ha_session_redirect (old slug -> twin), so the deleted copy's URL
redirects to the twin's, and ha_contribution_redirect (old contribution id ->
the twin's matching one), so an old #contrib-<id> link lands on the same
speech; it moves any redirect that pointed at the deleted copy to the twin.
Create both tables first (migrations/session_redirect_table.py --execute);
--execute refuses to run without them.

The undo SQL (production SQL is Mark's call: log it in docs/deploys.md) is
printed as soon as the undo tables exist and again at the end, even after a
crash. For one session only, or later:
    python scripts/delete_duplicate_debates.py --print-undo <ts> [--undo-session ID ...]

Checks around a production run (Mark's SQL, via DBeaver; log it):
  - Rehearse on a Postgres copy first (docs/recovery-runbook.md restore), and
    confirm the generated columns are skipped by the backups:
      SELECT table_name, column_name, is_generated FROM information_schema.columns
       WHERE table_name IN ('ha_session', 'ha_contribution') AND is_generated = 'ALWAYS';
    (title_tsv and speech_tsv expected; neither may appear in the undo SQL.)
  - Before and after: SELECT COUNT(*) FROM ha_session WHERE date >= '2026-04-27';
    the drop must equal the number deleted; SELECT COUNT(*) FROM ha_session_redirect.
  - After: no division points at a missing session:
      SELECT d.id FROM ha_division d LEFT JOIN ha_session s ON s.id = d.ha_session_id
       WHERE d.ha_session_id IS NOT NULL AND s.id IS NULL;      -- expect no rows
  - curl -I https://westminsterbrief.co.uk/archive/debate/<date>/<old slug>
    -> 302 with Location: the kept copy; purge those URLs and the day pages
    in Cloudflare; clear the cache at /admin. Each web worker looks for the
    redirect tables at most every 10 minutes and reads ARCHIVE_REDIRECT_CODE
    itself: after the migration wait 10 minutes (or restart the web service)
    before testing with curl, or a 404 is expected.
  - Don't re-ingest the affected days afterwards (scripts/backfill_hansard.py
    and the like): a provisional id Hansard still serves would be stored again.
  - The redirects are 302 until ARCHIVE_REDIRECT_CODE=301 is set on the web
    service (after a few weeks with no problems).

Cron windows: --execute doesn't run in or just before a production cron window
(hansard_archive/cron_windows.py; weekday daytime is blocked whole): exit 4.
Override with --ignore-cron-windows. A dry run is not blocked.

Usage (Railway one-off; dry run first):
    python scripts/delete_duplicate_debates.py --csv dupes.csv
    python scripts/delete_duplicate_debates.py --session-id 123:456,789:456
    python scripts/delete_duplicate_debates.py --session-id 123:456 --execute

Exit codes: 0 done (refusals are listed but not an error), 2 bad arguments or
missing redirect tables, 3 stopped on Hansard API errors, 4 cron window.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

CRON_REFRESH_DAYS = 4          # the archive cron re-reads the last 3 days
SOFT_URL_REFS = (("alert", "url"),)
TAG_LOCK_NAMESPACE = 72616701  # pg_advisory_xact_lock(72616701, session_id): the tagger's per-session lock
REDIRECT_TABLES = ("ha_session_redirect", "ha_contribution_redirect")

UNDO_SQL = """\
-- Undo scripts/delete_duplicate_debates.py run {ts}{scope}.
-- Production SQL is Mark's call: log it in docs/deploys.md. Run it as one block.
-- If the archive cron or a re-ingest has since stored any of these debates
-- again (same ext_id or slug), delete that new row first or step 2 fails.
BEGIN;
-- 1. Remove the redirects this run added; point moved ones back.
DELETE FROM ha_session_redirect
 WHERE old_slug IN (SELECT old_slug FROM {links} WHERE kind = 'redirect_added' AND {lf});
UPDATE ha_session_redirect SET new_session_id = l.session_id
  FROM {links} l
 WHERE l.kind = 'redirect_moved' AND ha_session_redirect.old_slug = l.old_slug AND {lf_l};
DELETE FROM ha_contribution_redirect
 WHERE old_contribution_id IN (SELECT old_contribution_id FROM {links}
                                WHERE kind = 'contrib' AND {lf});
UPDATE ha_contribution_redirect
   SET new_contribution_id = l.new_contribution_id, session_id = l.session_id
  FROM {links} l
 WHERE l.kind = 'contrib_moved'
   AND ha_contribution_redirect.old_contribution_id = l.old_contribution_id AND {lf_l};
-- 2. Put the sessions back first, then their contributions and tags (foreign keys; same ids).
INSERT INTO ha_session ({session_cols}) SELECT {session_cols} FROM {bak_session} WHERE {sf_id};
INSERT INTO ha_contribution ({contrib_cols}) SELECT {contrib_cols} FROM {bak_contrib} WHERE {sf};
INSERT INTO ha_session_theme ({theme_cols}) SELECT {theme_cols} FROM {bak_theme} WHERE {sf};
{attempt_sql}-- 3. Point re-pointed divisions back.
UPDATE ha_division SET ha_session_id = l.session_id, updated_at = CURRENT_TIMESTAMP
  FROM {links} l
 WHERE l.kind = 'division' AND ha_division.id = l.division_id AND {lf_l};
COMMIT;
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


def refusal(pair: tuple[int, int], deleting: set, today: date, repoint: bool,
            statuses: dict) -> str | None:
    """Why this pair must not be deleted now, or None. Reads the DB (call after locking).
    statuses: ext_id -> (status, data, reason) from the Hansard re-check."""
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

    day = {x.id: x for x in dd.load_day(s.date, s.house)}
    a, b = day[sid], day[kid]
    if a.place_key != b.place_key:
        return f"kept twin {kid} has a different title or location ({b.title!r})"
    st_s = statuses.get(s.ext_id, (dd.UNKNOWN, None, "not checked"))
    st_k = statuses.get(k.ext_id, (dd.UNKNOWN, None, "not checked"))
    if st_s[0] != dd.DEAD:
        return f"its Hansard id is {st_s[0]} ({st_s[2]}), not confirmed dead"
    if st_k[0] != dd.LIVE:
        return f"kept twin's Hansard id is {st_k[0]} ({st_k[2]}), not live"
    if st_k[1] is not None:
        from hansard_archive.ingestion import _flatten_items
        h_named = len(dd.contribs_from_fresh(_flatten_items(st_k[1], [0])))
        if h_named < b.named:
            return (f"kept twin is a swallower: Hansard now has {h_named} named contribution(s) "
                    f"for it, we hold {b.named}")
    inside = dd.holds_other_debates(b, day.values())
    if inside:
        return "kept twin holds other debates: " + ", ".join(
            f"id {x.id} {x.title[:40]!r}" for x in inside[:3])
    if not a.named:
        return "session has no named contributions to compare"
    if a.small and a.signature != b.signature:
        return "small session and the twin is not identical"
    m = dd.align_sessions(a, b)
    cov = len(m) / a.named
    if cov < dd.COVERAGE_MIN:
        return f"kept twin holds only {cov:.0%} of its named contributions"
    ok, why = dd.twin_shape_ok(a, b, m)
    if not ok:
        return why
    others = [x for x in day.values() if x.id not in deleting]
    unheld = dd.unheld_speeches(a, b, others, m)
    if unheld:
        return f"{len(unheld)} speech(es) held nowhere else: {dd.describe(unheld)}"

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

def _undo_names(ts: str, with_attempts: bool) -> dict:
    names = {"session": f"ha_session_dedupe_bak_{ts}",
             "contrib": f"ha_contribution_dedupe_bak_{ts}",
             "theme": f"ha_session_theme_dedupe_bak_{ts}",
             "links": f"ha_session_dedupe_links_{ts}"}
    if with_attempts:
        names["attempt"] = f"ha_tag_attempt_dedupe_bak_{ts}"
    return names


def create_undo_tables(db, ts: str) -> dict:
    conn = db.session.connection()
    names = _undo_names(ts, _has_table(conn, "ha_tag_attempt"))
    sources = {"session": "ha_session", "contrib": "ha_contribution", "theme": "ha_session_theme",
               "attempt": "ha_tag_attempt"}
    existing = set(inspect(conn).get_table_names())
    clash = [n for n in names.values() if n in existing]
    if clash:
        raise SystemExit(f"[dedupe] undo table already exists: {clash[0]} — run again in a second")
    cols = {}
    for key in names:
        if key == "links":
            continue
        cols[key] = _table_columns(conn, sources[key])
        col_list = ", ".join(cols[key])
        db.session.execute(text(f"CREATE TABLE {names[key]} AS SELECT {col_list} "
                                f"FROM {sources[key]} WHERE 1 = 0"))
    db.session.execute(text(
        f"CREATE TABLE {names['links']} (kind VARCHAR(20) NOT NULL, "
        f"deleted_session_id INTEGER NOT NULL, session_id INTEGER, new_session_id INTEGER, "
        f"division_id INTEGER, old_slug VARCHAR(200), old_contribution_id INTEGER, "
        f"new_contribution_id INTEGER, created_at TIMESTAMP NOT NULL)"))
    db.session.commit()
    print(f"[dedupe] undo tables: {', '.join(names.values())}", flush=True)
    return {"names": names, "cols": cols}


def load_undo(db, ts: str) -> dict:
    """The undo tables of an earlier run, for --print-undo."""
    conn = db.session.connection()
    names = _undo_names(ts, _has_table(conn, f"ha_tag_attempt_dedupe_bak_{ts}"))
    missing = [n for n in names.values() if not _has_table(conn, n)]
    if missing:
        raise SystemExit(f"[dedupe] no undo table {missing[0]} for run {ts}")
    cols = {k: _table_columns(conn, n) for k, n in names.items() if k != "links"}
    return {"names": names, "cols": cols}


def undo_sql(ts: str, undo: dict, session_ids: list[int] | None = None) -> str:
    """The undo SQL for the whole run, or only for the given deleted session ids."""
    names, cols = undo["names"], undo["cols"]
    if session_ids:
        ids = ", ".join(str(int(i)) for i in session_ids)
        lf, lf_l = f"deleted_session_id IN ({ids})", f"l.deleted_session_id IN ({ids})"
        sf, sf_id, sf_item = f"session_id IN ({ids})", f"id IN ({ids})", f"item_id IN ({ids})"
        scope = f" (only deleted session(s) {ids})"
    else:
        lf = lf_l = sf = sf_id = sf_item = "1 = 1"
        scope = ""
    attempt = ""
    if "attempt" in names:
        c = ", ".join(cols["attempt"])
        attempt = (f"INSERT INTO ha_tag_attempt ({c}) SELECT {c} FROM {names['attempt']} "
                   f"WHERE {sf_item}\n    ON CONFLICT DO NOTHING;\n")
    return UNDO_SQL.format(ts=ts, scope=scope, links=names["links"], bak_session=names["session"],
                           bak_contrib=names["contrib"], bak_theme=names["theme"],
                           session_cols=", ".join(cols["session"]),
                           contrib_cols=", ".join(cols["contrib"]),
                           theme_cols=", ".join(cols["theme"]), attempt_sql=attempt,
                           lf=lf, lf_l=lf_l, sf=sf, sf_id=sf_id)


def _link(db, links: str, kind: str, deleted: int, now: datetime, session_id=None,
          new_session_id=None, division_id=None, old_slug=None, old_contribution_id=None,
          new_contribution_id=None) -> None:
    db.session.execute(
        text(f"INSERT INTO {links} (kind, deleted_session_id, session_id, new_session_id, "
             f"division_id, old_slug, old_contribution_id, new_contribution_id, created_at) "
             f"VALUES (:k, :ds, :s, :n, :d, :o, :oc, :nc, :t)"),
        {"k": kind, "ds": deleted, "s": session_id, "n": new_session_id, "d": division_id,
         "o": old_slug, "oc": old_contribution_id, "nc": new_contribution_id, "t": now})


def lock_pair(db, sid: int, kid: int) -> None:
    """The day's lock first (shared with refresh_partial_debates.py
    --accept-shrink, which also checks the day's other sessions), then the
    tagger's per-session lock (it takes that before writing tags, which needs a
    key-share lock on the session row), then the rows."""
    from hansard_archive.dedupe import lock_day
    from hansard_archive.models import HansardSession
    s = db.session.get(HansardSession, sid)
    if s is not None:
        lock_day(s.date, s.house)
    if db.session.connection().dialect.name == "postgresql":
        db.session.execute(text("SELECT pg_advisory_xact_lock(:ns, :s)"),
                           {"ns": TAG_LOCK_NAMESPACE, "s": sid})
    (db.session.query(HansardSession.id).filter(HansardSession.id == sid)
     .with_for_update().all())                     # no-op on SQLite
    (db.session.query(HansardSession.id).filter(HansardSession.id == kid)
     .with_for_update(read=True).all())


def delete_one(db, sid: int, kid: int, undo: dict, repoint: bool, now: datetime) -> dict:
    """Back up and delete one session inside the current (locked) transaction. Caller commits."""
    from hansard_archive import dedupe as dd
    from hansard_archive.models import HansardSession
    names, cols = undo["names"], undo["cols"]
    links = names["links"]
    conn = db.session.connection()
    s = db.session.get(HansardSession, sid)
    slug = s.slug
    counts = {}

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
    _link(db, links, "deleted", sid, now, session_id=sid, new_session_id=kid, old_slug=slug)

    # Old contribution ids -> the twin's matching ones, for #contrib-<id> links.
    loaded = dd.load_sessions([sid, kid])
    a, b = loaded[sid], loaded[kid]
    mapping = {a.contribs[i].id: b.contribs[j].id for i, j in dd.align_sessions(a, b).items()}
    # Links from an earlier run that pointed at this copy's speeches follow on to the twin.
    counts["contrib_links_moved"] = 0
    for old_c, prev_new in db.session.execute(
            text("SELECT old_contribution_id, new_contribution_id FROM ha_contribution_redirect "
                 "WHERE session_id = :s"), {"s": sid}).fetchall():
        if prev_new not in mapping:
            continue
        _link(db, links, "contrib_moved", sid, now, session_id=sid, new_session_id=kid,
              old_contribution_id=old_c, new_contribution_id=prev_new)
        db.session.execute(text("UPDATE ha_contribution_redirect SET new_contribution_id = :n, "
                                "session_id = :k WHERE old_contribution_id = :o"),
                           {"n": mapping[prev_new], "k": kid, "o": old_c})
        counts["contrib_links_moved"] += 1
    counts["contrib_links"] = 0
    for old_c, new_c in mapping.items():
        if db.session.execute(text("SELECT 1 FROM ha_contribution_redirect "
                                   "WHERE old_contribution_id = :o"), {"o": old_c}).first():
            continue
        db.session.execute(text("INSERT INTO ha_contribution_redirect (old_contribution_id, "
                                "new_contribution_id, session_id, created_at) "
                                "VALUES (:o, :n, :k, :t)"),
                           {"o": old_c, "n": new_c, "k": kid, "t": now})
        _link(db, links, "contrib", sid, now, session_id=sid, new_session_id=kid,
              old_contribution_id=old_c, new_contribution_id=new_c)
        counts["contrib_links"] += 1

    counts["divisions"] = 0
    if repoint and _has_table(conn, "ha_division"):
        for (div_id,) in db.session.execute(
                text("SELECT id FROM ha_division WHERE ha_session_id = :s"), {"s": sid}).fetchall():
            _link(db, links, "division", sid, now, session_id=sid, new_session_id=kid,
                  division_id=div_id)
            counts["divisions"] += 1
        db.session.execute(text("UPDATE ha_division SET ha_session_id = :k, updated_at = :t "
                                "WHERE ha_session_id = :s"), {"k": kid, "t": now, "s": sid})

    for (old_slug,) in db.session.execute(
            text("SELECT old_slug FROM ha_session_redirect WHERE new_session_id = :s"),
            {"s": sid}).fetchall():
        _link(db, links, "redirect_moved", sid, now, session_id=sid, new_session_id=kid,
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
            _link(db, links, "redirect_added", sid, now, new_session_id=kid, old_slug=slug)
        elif prev[0] != kid:
            _link(db, links, "redirect_moved", sid, now, session_id=prev[0],
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


def recheck_hansard(pairs: dict, sleep, pace: float) -> dict:
    """ext_id -> (status, data, reason) for every session involved, before any lock."""
    from hansard_archive import dedupe as dd
    from hansard_archive.models import HansardSession
    ids = list(dict.fromkeys([i for p in pairs.items() for i in p]))
    ext = {s.id: s.ext_id for s in HansardSession.query.filter(HansardSession.id.in_(ids)).all()}
    checker = dd.Checker(pace=pace, sleep=sleep)
    out = {}
    for i in ids:
        if i in ext:
            out[ext[i]] = checker(ext[i])
    print(f"[dedupe] Hansard re-check: {checker.calls} look-up(s)", flush=True)
    return out


def run(args, pairs: list[tuple[int, int]], clock=datetime.utcnow, today: date | None = None,
        sleep=time.sleep) -> dict:
    from extensions import db
    from hansard_archive import dedupe as dd
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
    missing = [t for t in REDIRECT_TABLES if not _has_table(conn, t)]
    db.session.rollback()
    if missing:
        msg = (f"{', '.join(missing)} missing: run migrations/session_redirect_table.py "
               f"--execute first (deleted copies' URLs must redirect)")
        if args.execute:
            print(f"[dedupe] {msg}", file=sys.stderr)
            return {"exit_code": 2, "deleted": [], "refused": []}
        print(f"[dedupe] WARNING: {msg}", flush=True)

    deleted, refused = [], []
    undo = None
    ts = clock().strftime("%Y%m%d_%H%M%S")
    stopped, exit_code = None, 0
    try:
        if args.execute and seen:
            check_cron_window(args, clock)
        try:
            statuses = recheck_hansard(seen, sleep, args.pace)
        except dd.ApiStop as exc:
            stopped, exit_code = f"{exc} — nothing deleted", 3
            seen = {}
        db.session.rollback()
        for sid, kid in seen.items():
            if args.execute:
                check_cron_window(args, clock)
                if undo is None:
                    undo = create_undo_tables(db, ts)
                    print("[dedupe] undo SQL for this run (also printed at the end):\n"
                          + undo_sql(ts, undo), flush=True)
                lock_pair(db, sid, kid)
            why = refusal((sid, kid), deleting, today, args.repoint_divisions, statuses)
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
            try:
                counts = delete_one(db, sid, kid, undo, args.repoint_divisions, clock())
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            deleted.append((sid, kid, counts))
            print(f"[dedupe] DELETED {label}; kept id={kid} — "
                  + ", ".join(f"{k}={v}" for k, v in counts.items())
                  + f" (undo just this one: --print-undo {ts} --undo-session {sid})", flush=True)
    except WindowStop as exc:
        db.session.rollback()
        stopped, exit_code = (f"cron window due: {exc} — run the same command later "
                              f"(done ones are skipped)"), 4
    finally:
        verb = "deleted" if args.execute else "would be deleted"
        print(f"\n[dedupe] {len(deleted)} {verb}, {len(refused)} refused")
        for sid, kid, why in refused:
            print(f"  refused id={sid} (keep {kid}): {why}")
        if undo:
            print("\n[dedupe] undo SQL (production SQL is Mark's call; log it in docs/deploys.md):\n"
                  + undo_sql(ts, undo), flush=True)
        if stopped:
            print(f"\n[dedupe] STOPPED: {stopped}", flush=True)
    return {"exit_code": exit_code, "deleted": deleted, "refused": refused, "undo": undo, "ts": ts}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--csv", help="CSV from scripts/find_duplicate_debates.py (rows with action 'delete')")
    p.add_argument("--session-id", action="append",
                   help="DELETE:KEEP pairs, comma-separated (repeatable)")
    p.add_argument("--execute", action="store_true", help="delete (default: dry run)")
    p.add_argument("--repoint-divisions", action="store_true",
                   help="move divisions pointing at a deleted copy to its kept twin")
    p.add_argument("--ignore-cron-windows", action="store_true")
    p.add_argument("--pace", type=float, default=1.0, help="seconds between Hansard look-ups (min 1)")
    p.add_argument("--print-undo", metavar="TS", help="print the undo SQL of an earlier run and stop")
    p.add_argument("--undo-session", type=int, action="append",
                   help="with --print-undo: only for this deleted session id (repeatable)")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if args.print_undo:
        from flask_app import app
        from extensions import db
        with app.app_context():
            print(undo_sql(args.print_undo, load_undo(db, args.print_undo), args.undo_session))
        return 0
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
