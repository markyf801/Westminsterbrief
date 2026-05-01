"""
migrate_sqlite_to_railway.py

One-time migration: copies ha_session, ha_contribution, ha_session_theme
from local SQLite (intelligence.db) to Railway Postgres.

Usage:
  python scripts/migrate_sqlite_to_railway.py            # dry run (default, no writes)
  python scripts/migrate_sqlite_to_railway.py --execute  # real migration

Requires:
  DATABASE_URL env var pointing to Railway Postgres.
  Run from project root (c:/Users/marky/hansard_app).

Safety:
  - Dry run connects to Railway and checks schema, but writes nothing.
  - Real run creates intelligence.db.backup before touching anything.
  - All inserts wrapped in a single transaction — any failure rolls back cleanly.
  - ON CONFLICT DO NOTHING on all tables — safe to re-run.
"""

import argparse
import os
import shutil
import sqlite3
import sys

SQLITE_PATH = "intelligence.db"
BATCH = 500

# ── Expected columns per table ─────────────────────────────────────────────

HA_SESSION_COLS = [
    "id", "ext_id", "title", "date", "house", "debate_type",
    "location", "hrs_tag", "hansard_url", "contributions_ingested",
    "is_container", "slug", "department", "ingested_at",
]

HA_CONTRIBUTION_COLS = [
    "id", "session_id", "member_id", "member_name", "party",
    "speech_text", "speech_order", "responds_to_id", "ingested_at",
]

HA_SESSION_THEME_COLS = [
    "id", "session_id", "theme", "theme_type", "confidence",
    "tagged_at", "model_used",
]

# SQLite stores these as 0/1 integers; Postgres needs Python bool
BOOL_COLS = {"contributions_ingested", "is_container"}


# ── Connection helpers ─────────────────────────────────────────────────────

def get_pg_conn():
    import psycopg2
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("ERROR: DATABASE_URL is not set.")
    # Railway issues postgres:// scheme; psycopg2 requires postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    try:
        return psycopg2.connect(url)
    except Exception as e:
        sys.exit(f"ERROR: Could not connect to Railway Postgres: {e}")


# ── Pre-flight schema check ────────────────────────────────────────────────

def preflight(pg_conn):
    cur = pg_conn.cursor()
    checks = {
        "ha_session":       HA_SESSION_COLS,
        "ha_contribution":  HA_CONTRIBUTION_COLS,
        "ha_session_theme": HA_SESSION_THEME_COLS,
    }

    failed = False
    for table, expected in checks.items():
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
        """, (table,))
        actual = {row[0] for row in cur.fetchall()}
        if not actual:
            print(f"  FAIL {table}: table not found in Railway schema")
            failed = True
            continue
        missing = sorted(set(expected) - actual)
        if missing:
            print(f"  FAIL {table}: missing columns: {missing}")
            failed = True
        else:
            print(f"  OK   {table}: all {len(expected)} columns present")

    # Confirm unique index on slug
    cur.execute("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE tablename = 'ha_session' AND indexdef ILIKE '%slug%'
    """)
    slug_idx = cur.fetchall()
    if not slug_idx:
        print("  FAIL ha_session.slug: no unique index found")
        failed = True
    else:
        print(f"  OK   ha_session.slug: unique index '{slug_idx[0][0]}' present")

    cur.close()
    if failed:
        pg_conn.close()
        sys.exit("\nPre-flight FAILED. Aborting — nothing written.")
    print("Pre-flight PASSED.\n")


# ── Type conversion ────────────────────────────────────────────────────────

def convert_row(row, cols):
    """Cast SQLite 0/1 integers to Python bool for BOOLEAN Postgres columns."""
    out = []
    for val, col in zip(row, cols):
        if col in BOOL_COLS:
            out.append(bool(val) if val is not None else False)
        else:
            out.append(val)
    return tuple(out)


# ── Batch insert helper ────────────────────────────────────────────────────

def insert_table(pg_cur, sqlite_conn, table, cols, conflict_col):
    cols_str   = ", ".join(cols)
    ph_str     = ", ".join(["%s"] * len(cols))
    sql        = (f"INSERT INTO {table} ({cols_str}) VALUES ({ph_str}) "
                  f"ON CONFLICT ({conflict_col}) DO NOTHING")

    rows = sqlite_conn.execute(
        f"SELECT {cols_str} FROM {table} ORDER BY id"
    ).fetchall()

    for i in range(0, len(rows), BATCH):
        batch = [convert_row(r, cols) for r in rows[i:i + BATCH]]
        pg_cur.executemany(sql, batch)
        done = min(i + BATCH, len(rows))
        print(f"  {table}: {done:,}/{len(rows):,} rows ...", end="\r", flush=True)

    print(f"  {table}: {len(rows):,} rows submitted{' ' * 20}")
    return len(rows)


# ── Main ───────────────────────────────────────────────────────────────────

def main(execute):
    if not os.path.exists(SQLITE_PATH):
        sys.exit(f"ERROR: Local SQLite not found at {SQLITE_PATH}. Run from project root.")

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    pg_conn     = get_pg_conn()

    print("=" * 62)
    print("  Hansard Archive: SQLite -> Railway Postgres migration")
    print(f"  Mode   : {'EXECUTE (real migration)' if execute else 'DRY RUN (no writes)'}")
    print(f"  Source : {os.path.abspath(SQLITE_PATH)}")
    print("=" * 62)
    print()

    # ── Pre-flight ─────────────────────────────────────────────────────
    print("Pre-flight schema checks:")
    preflight(pg_conn)

    # ── Count current state ────────────────────────────────────────────
    pg_cur = pg_conn.cursor()

    local  = {}
    remote = {}
    for table in ("ha_session", "ha_contribution", "ha_session_theme"):
        local[table]  = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pg_cur.execute(f"SELECT COUNT(*) FROM {table}")
        remote[table] = pg_cur.fetchone()[0]

    print("Row counts:")
    print(f"  {'Table':<22} {'Local':>8}  {'Railway':>8}  {'To insert':>10}")
    print(f"  {'-'*22} {'-'*8}  {'-'*8}  {'-'*10}")
    for t in ("ha_session", "ha_contribution", "ha_session_theme"):
        delta = local[t] - remote[t]
        print(f"  {t:<22} {local[t]:>8,}  {remote[t]:>8,}  {delta:>10,}")
    print()

    if not execute:
        print("DRY RUN complete — nothing written.")
        print("Railway connection verified. Schema checks passed.")
        print("Run with --execute when ready to migrate.")
        sqlite_conn.close()
        pg_conn.close()
        return

    # ── Real migration ─────────────────────────────────────────────────

    # Local backup first (before any remote writes)
    backup = SQLITE_PATH + ".backup"
    print(f"Creating local backup: {backup}")
    shutil.copy2(SQLITE_PATH, backup)
    print(f"Backup written: {os.path.getsize(backup):,} bytes\n")

    try:
        print("Inserting rows (FK order: sessions -> contributions -> themes):")
        insert_table(pg_cur, sqlite_conn, "ha_session",       HA_SESSION_COLS,       "ext_id")
        insert_table(pg_cur, sqlite_conn, "ha_contribution",  HA_CONTRIBUTION_COLS,  "id")
        insert_table(pg_cur, sqlite_conn, "ha_session_theme", HA_SESSION_THEME_COLS, "id")

        pg_conn.commit()
        print("\nCOMMITTED.\n")

        # ── Verify ─────────────────────────────────────────────────────
        print("Verifying Railway counts after commit:")
        all_ok = True
        for t in ("ha_session", "ha_contribution", "ha_session_theme"):
            pg_cur.execute(f"SELECT COUNT(*) FROM {t}")
            final = pg_cur.fetchone()[0]
            ok    = final == local[t]
            mark  = "OK" if ok else "MISMATCH"
            print(f"  {t:<22} {final:>8,}  (expected {local[t]:,})  [{mark}]")
            if not ok:
                all_ok = False

        print()
        if all_ok:
            print("Migration COMPLETE. All counts match.")
        else:
            print("WARNING: count mismatch — review output above.")

    except Exception as exc:
        pg_conn.rollback()
        print(f"\nERROR: {exc}")
        print("Transaction ROLLED BACK. Railway DB is unchanged.")
        sqlite_conn.close()
        pg_conn.close()
        sys.exit(1)

    sqlite_conn.close()
    pg_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Hansard Archive from SQLite to Railway Postgres")
    parser.add_argument("--execute", action="store_true",
                        help="Run the real migration (default is dry run, no writes)")
    args = parser.parse_args()
    main(execute=args.execute)
