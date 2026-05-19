"""
Post-ingestion structured summary for ha_bill / ha_bill_sponsor / ha_bill_stage.

Produces the quality-gate report needed before greenlighting the tagging run:
  - Totals and session/type breakdown
  - Sponsor counts + member_id FK match rate
  - Stage counts + avg per bill
  - raw_data population rate
  - Spot-check rows for 8 named bills

Usage:
    python scripts/bills_ingest_report.py
    python scripts/bills_ingest_report.py --spot-check   # show spot-check rows only
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


SPOT_CHECK_BILLS = [
    # (parliament_bill_id, label)
    (3772,  "Institute for Apprenticeships (Skills England / IfATE Transfer Act)"),
    (3316,  "Renters' Rights Bill"),
    (3323,  "Tobacco and Vapes Bill"),
    (3296,  "Border Security, Asylum and Immigration Bill"),
    (3347,  "Employment Rights Bill"),
    # PMB — Peter Bone's National Service (Armed Forces) Bill
    (3682,  "National Service (Armed Forces) Bill [PMB]"),
    # Lords-originated bill
    (3481,  "Product Regulation and Metrology Bill [Lords]"),
    # HS2 carryover
    (2505,  "High Speed Rail (Crewe - Manchester) Bill"),
]


def run_report(spot_check_only: bool = False) -> None:
    from flask_app import app
    from extensions import db
    from sqlalchemy import text

    with app.app_context():
        def q(sql, **kw):
            return db.session.execute(text(sql), kw).fetchall()

        def scalar(sql, **kw):
            row = db.session.execute(text(sql), kw).fetchone()
            return row[0] if row else None

        if not spot_check_only:
            # ── 1. Bill totals ───────────────────────────────────────────────
            total_bills     = scalar("SELECT COUNT(*) FROM ha_bill")
            raw_data_filled = scalar("SELECT COUNT(*) FROM ha_bill WHERE raw_data IS NOT NULL")
            is_act_count    = scalar("SELECT COUNT(*) FROM ha_bill WHERE is_act = TRUE")
            is_defeated     = scalar("SELECT COUNT(*) FROM ha_bill WHERE is_defeated = TRUE")

            print("\n====================================================")
            print("  Westminster Brief -- Bills Ingestion Report")
            print("====================================================\n")

            print(f"  Total ha_bill rows      : {total_bills}")
            raw_pct = (raw_data_filled / total_bills * 100) if total_bills else 0
            print(f"  raw_data populated      : {raw_data_filled} / {total_bills}  ({raw_pct:.1f}%)")
            print(f"  is_act = True           : {is_act_count}")
            print(f"  is_defeated = True      : {is_defeated}")

            # ── 2. Session breakdown ─────────────────────────────────────────
            print("\n  By session:")
            rows = q("SELECT session, COUNT(*) n FROM ha_bill GROUP BY session ORDER BY session")
            for sess, n in rows:
                print(f"    {sess or '(null)':12s}  {n:4d}")

            # ── 3. Bill type breakdown ───────────────────────────────────────
            print("\n  By bill_type:")
            rows = q(
                "SELECT bill_type, COUNT(*) n FROM ha_bill "
                "GROUP BY bill_type ORDER BY n DESC"
            )
            for btype, n in rows:
                print(f"    {(btype or '(null)')[:45]:45s}  {n:4d}")

            # ── 4. House of origin ───────────────────────────────────────────
            print("\n  By house_of_origin:")
            rows = q(
                "SELECT house_of_origin, COUNT(*) n FROM ha_bill "
                "GROUP BY house_of_origin ORDER BY n DESC"
            )
            for house, n in rows:
                print(f"    {(house or '(null)'):10s}  {n:4d}")

            # ── 5. Sponsor totals + FK match rate ────────────────────────────
            total_sponsors    = scalar("SELECT COUNT(*) FROM ha_bill_sponsor")
            sponsors_with_fk  = scalar("SELECT COUNT(*) FROM ha_bill_sponsor WHERE member_id IS NOT NULL")
            sponsors_null_fk  = total_sponsors - (sponsors_with_fk or 0) if total_sponsors else 0
            fk_pct = (sponsors_with_fk / total_sponsors * 100) if total_sponsors else 0

            print("\n  Sponsors:")
            print(f"    Total ha_bill_sponsor rows  : {total_sponsors}")
            print(f"    member_id resolved (FK set) : {sponsors_with_fk}  ({fk_pct:.1f}%)")
            print(f"    member_id NULL (unresolved) : {sponsors_null_fk}")
            print(f"    Avg sponsors / bill         : "
                  f"{total_sponsors / total_bills:.1f}" if total_bills else "    n/a")

            # ── 6. Stage totals ──────────────────────────────────────────────
            total_stages = scalar("SELECT COUNT(*) FROM ha_bill_stage")
            avg_stages   = scalar(
                "SELECT ROUND(AVG(n), 1) FROM "
                "(SELECT COUNT(*) n FROM ha_bill_stage GROUP BY bill_id) s"
            )

            print("\n  Stages:")
            print(f"    Total ha_bill_stage rows  : {total_stages}")
            print(f"    Avg stages / bill         : {avg_stages}")

            # ── 7. Bills with no stages ──────────────────────────────────────
            no_stages = scalar(
                "SELECT COUNT(*) FROM ha_bill b "
                "WHERE NOT EXISTS (SELECT 1 FROM ha_bill_stage s WHERE s.bill_id = b.id)"
            )
            print(f"    Bills with 0 stages       : {no_stages}")

            # ── 8. Introduced date coverage ──────────────────────────────────
            with_date    = scalar("SELECT COUNT(*) FROM ha_bill WHERE introduced_date IS NOT NULL")
            earliest     = scalar("SELECT MIN(introduced_date) FROM ha_bill")
            latest       = scalar("SELECT MAX(introduced_date) FROM ha_bill")
            print(f"\n  Introduced date:")
            print(f"    Populated               : {with_date} / {total_bills}")
            print(f"    Range                   : {earliest}  →  {latest}")

        # ── 9. Spot-check for 8 named bills ─────────────────────────────────
        print("\n  ----------------------------------------------------")
        print("  Spot-check: 8 named bills")
        print("  ----------------------------------------------------")

        for pid, label in SPOT_CHECK_BILLS:
            row = db.session.execute(
                text("""
                    SELECT b.id, b.title, b.session, b.bill_type,
                           b.house_of_origin, b.is_act, b.is_defeated,
                           b.introduced_date, b.current_stage,
                           b.raw_data IS NOT NULL AS has_raw,
                           (SELECT COUNT(*) FROM ha_bill_sponsor s WHERE s.bill_id = b.id) n_sponsors,
                           (SELECT COUNT(*) FROM ha_bill_stage st WHERE st.bill_id = b.id) n_stages,
                           (SELECT COUNT(*) FROM ha_bill_sponsor s
                            WHERE s.bill_id = b.id AND s.is_primary) n_primary,
                           (SELECT member_name FROM ha_bill_sponsor s
                            WHERE s.bill_id = b.id AND s.is_primary LIMIT 1) primary_name
                    FROM ha_bill b
                    WHERE b.parliament_bill_id = :pid
                """),
                {"pid": pid},
            ).fetchone()

            print(f"\n  [{pid}] {label}")
            if row is None:
                print("    *** NOT FOUND IN ha_bill ***")
                continue

            print(f"    DB id            : {row[0]}")
            print(f"    Title            : {row[1]}")
            print(f"    Session          : {row[2]}")
            print(f"    Type             : {row[3]}")
            print(f"    House of origin  : {row[4]}")
            print(f"    is_act           : {row[5]}")
            print(f"    is_defeated      : {row[6]}")
            print(f"    Introduced date  : {row[7]}")
            print(f"    Current stage    : {row[8]}")
            print(f"    raw_data present : {bool(row[9])}")
            print(f"    Sponsors         : {row[10]} (primary: {row[12]}x, name: {row[13]})")
            print(f"    Stages           : {row[11]}")

        print()


def main():
    parser = argparse.ArgumentParser(description="Bills ingestion quality report")
    parser.add_argument("--spot-check", action="store_true",
                        help="Show named-bill spot-check rows only")
    args = parser.parse_args()
    run_report(spot_check_only=args.spot_check)


if __name__ == "__main__":
    main()
