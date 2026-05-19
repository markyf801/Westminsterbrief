"""
Export suspicious-tagged bills (>4 themes) to a TSV for manual review.

Usage:
    python scripts/bills_suspicious_report.py
    python scripts/bills_suspicious_report.py --threshold 6

Output: scripts/bills_suspicious.tsv
Columns: parliament_bill_id, title, bill_type, house_of_origin, session,
         is_act, tag_count, themes (pipe-separated)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def run(threshold: int = 4) -> None:
    from flask_app import app
    from extensions import db
    from sqlalchemy import text

    out_path = Path(__file__).parent / "bills_suspicious.tsv"

    with app.app_context():
        rows = db.session.execute(text("""
            SELECT
                b.parliament_bill_id,
                b.title,
                b.bill_type,
                b.house_of_origin,
                b.session,
                b.is_act,
                COUNT(t.id)                                          AS tag_count,
                STRING_AGG(t.theme, ' | ' ORDER BY t.theme)         AS themes
            FROM ha_bill b
            JOIN ha_bill_theme t ON t.bill_id = b.id
            GROUP BY b.parliament_bill_id, b.title, b.bill_type,
                     b.house_of_origin, b.session, b.is_act
            HAVING COUNT(t.id) > :threshold
            ORDER BY COUNT(t.id) DESC, b.parliament_bill_id
        """), {"threshold": threshold}).fetchall()

    headers = [
        "parliament_bill_id", "title", "bill_type", "house_of_origin",
        "session", "is_act", "tag_count", "themes",
    ]

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(headers) + "\n")
        for r in rows:
            cells = [str(c) if c is not None else "" for c in r]
            f.write("\t".join(cells) + "\n")

    print(f"Written {len(rows)} rows to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Export suspicious-tagged bills to TSV")
    parser.add_argument("--threshold", type=int, default=4,
                        help="Tag count threshold (default 4, i.e. >4 tags)")
    args = parser.parse_args()
    run(threshold=args.threshold)


if __name__ == "__main__":
    main()
