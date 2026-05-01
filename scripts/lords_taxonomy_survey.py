"""
Lords Hansard taxonomy survey — run after 12-month backfill completes.

Purpose: identify Lords-specific title patterns for debate type reclassification.
The Lords uses a flat HRSTag taxonomy (almost all NewDebate), so the Commons
hrs_tag-first classifier leaves ~55% of Lords sessions as 'other'. This survey
maps the actual title/contribution patterns to inform a targeted SQL UPDATE pass.

Usage:
  python scripts/lords_taxonomy_survey.py           # full survey
  python scripts/lords_taxonomy_survey.py --show-titles  # include title samples per cluster

Output is printed to stdout. Pipe to a file for review:
  python scripts/lords_taxonomy_survey.py > data/lords_taxonomy_survey.txt
"""

import argparse
import sys

sys.path.insert(0, ".")

from flask_app import app
from flask_app import db
from sqlalchemy import text


def run_survey(show_titles: bool = False) -> None:
    with app.app_context():

        # -------------------------------------------------------------------
        # 1. Baseline: debate_type distribution across all Lords sessions
        # -------------------------------------------------------------------
        print("=" * 70)
        print("1. DEBATE TYPE DISTRIBUTION — ALL LORDS NON-CONTAINER SESSIONS")
        print("=" * 70)

        rows = db.session.execute(text("""
            SELECT debate_type,
                   COUNT(*) AS sessions,
                   ROUND(AVG(contrib_count), 1) AS avg_contribs,
                   MIN(contrib_count) AS min_contribs,
                   MAX(contrib_count) AS max_contribs
            FROM (
                SELECT s.debate_type,
                       (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count
                FROM ha_session s
                WHERE s.house = 'Lords' AND s.is_container = 0
            )
            GROUP BY debate_type
            ORDER BY sessions DESC
        """)).fetchall()

        total = sum(r.sessions for r in rows)
        print(f"\n{'Type':<25} {'Sessions':>8} {'%':>6}  {'Avg':>5}  {'Min':>5}  {'Max':>5}")
        print("-" * 65)
        for r in rows:
            print(f"{r.debate_type:<25} {r.sessions:>8} {r.sessions/total*100:>5.1f}%  "
                  f"{r.avg_contribs:>5}  {r.min_contribs:>5}  {r.max_contribs:>5}")
        print(f"{'TOTAL':<25} {total:>8}")

        # -------------------------------------------------------------------
        # 2. HRSTag survey — any tags beyond NewDebate appearing at scale?
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("2. HRSTAG DISTRIBUTION — LORDS (all sessions incl. containers)")
        print("=" * 70)

        tag_rows = db.session.execute(text("""
            SELECT COALESCE(hrs_tag, '(null)') AS tag, COUNT(*) AS n
            FROM ha_session
            WHERE house = 'Lords'
            GROUP BY hrs_tag
            ORDER BY n DESC
        """)).fetchall()

        print(f"\n{'HRSTag':<30} {'Count':>8}")
        print("-" * 42)
        for r in tag_rows:
            print(f"{r.tag:<30} {r.n:>8}")

        # -------------------------------------------------------------------
        # 3. 'other' session anatomy — contribution count buckets
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("3. 'OTHER' SESSIONS — CONTRIBUTION COUNT BUCKETS")
        print("=" * 70)

        bucket_rows = db.session.execute(text("""
            SELECT
                CASE
                    WHEN contrib_count = 0 THEN '0 (empty)'
                    WHEN contrib_count BETWEEN 1 AND 5 THEN '1-5'
                    WHEN contrib_count BETWEEN 6 AND 15 THEN '6-15'
                    WHEN contrib_count BETWEEN 16 AND 30 THEN '16-30'
                    WHEN contrib_count BETWEEN 31 AND 60 THEN '31-60'
                    WHEN contrib_count BETWEEN 61 AND 120 THEN '61-120'
                    ELSE '120+'
                END AS bucket,
                COUNT(*) AS n
            FROM (
                SELECT (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count
                FROM ha_session s
                WHERE s.house = 'Lords' AND s.is_container = 0 AND s.debate_type = 'other'
            )
            GROUP BY bucket
            ORDER BY MIN(contrib_count)
        """)).fetchall()

        print(f"\n{'Bucket':<15} {'Count':>8}")
        print("-" * 27)
        for r in bucket_rows:
            print(f"{r.bucket:<15} {r.n:>8}")

        # -------------------------------------------------------------------
        # 4. Title keyword analysis within 'other' — top trigrams/patterns
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("4. CANDIDATE SI TITLES IN 'OTHER' (Regulations/Order/Rules + year)")
        print("=" * 70)

        si_rows = db.session.execute(text("""
            SELECT title,
                   (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count,
                   date, location
            FROM ha_session s
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (
                title LIKE '% Regulations %'
                OR title LIKE '% Regulations'
                OR title LIKE '% (Regulations)%'
                OR title LIKE '% Order %'
                OR title LIKE '% Orders %'
                OR title LIKE '% Order)'
                OR title LIKE '% Rules %'
                OR title LIKE '% Rules'
            )
            AND title NOT LIKE '%Draft%'
            AND title NOT LIKE '%draft%'
            ORDER BY date DESC
            LIMIT 40
        """)).fetchall()

        print(f"\nFound {len(si_rows)} candidate SI sessions in 'other' (sample of 40):")
        print(f"\n{'Title':<60} {'Contribs':>8}  {'Location':<20}  {'Date'}")
        print("-" * 110)
        for r in si_rows:
            print(f"{r.title[:58]:<60} {r.contrib_count:>8}  {str(r.location or ''):<20}  {r.date}")

        # Count total
        si_count = db.session.execute(text("""
            SELECT COUNT(*) FROM ha_session
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (
                title LIKE '% Regulations %' OR title LIKE '% Regulations'
                OR title LIKE '% (Regulations)%'
                OR title LIKE '% Order %' OR title LIKE '% Orders %'
                OR title LIKE '% Order)' OR title LIKE '% Rules %' OR title LIKE '% Rules'
            )
            AND title NOT LIKE '%Draft%' AND title NOT LIKE '%draft%'
        """)).scalar()
        print(f"\nTotal candidate SI sessions in 'other': {si_count}")

        # Spot-check 10 random matches for annulment motions.
        # "That the X Regulations 2026 be annulled" matches the regex but is a
        # debate on an SI, not the SI consideration itself. Confirm all 10 are
        # genuine SI consideration sessions before approving the UPDATE pass.
        spot_rows = db.session.execute(text("""
            SELECT title,
                   (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count,
                   date
            FROM ha_session s
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (
                title LIKE '% Regulations %' OR title LIKE '% Regulations'
                OR title LIKE '% (Regulations)%'
                OR title LIKE '% Order %' OR title LIKE '% Orders %'
                OR title LIKE '% Order)' OR title LIKE '% Rules %' OR title LIKE '% Rules'
            )
            AND title NOT LIKE '%Draft%' AND title NOT LIKE '%draft%'
            ORDER BY RANDOM()
            LIMIT 10
        """)).fetchall()

        print(f"\n--- SPOT-CHECK: 10 random SI matches (confirm not annulment motions) ---")
        print(f"{'Title':<70} {'Contribs':>8}  {'Date'}")
        print("-" * 90)
        for r in spot_rows:
            flag = "  *** CHECK ***" if any(
                kw in r.title.lower() for kw in ("annul", "revok", "fatal", "be revoked", "be annulled")
            ) else ""
            print(f"{r.title[:68]:<70} {r.contrib_count:>8}  {r.date}{flag}")
        print(f"\nIf any annulment motion titles appear above, exclude them from the"
              f" UPDATE pass by adding: AND LOWER(title) NOT LIKE '%annul%'")

        # -------------------------------------------------------------------
        # 5. Short sessions in 'other' — likely oral questions (< 35 contribs)
        #    Group by approximate contribution count to spot clusters
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("5. SHORT 'OTHER' SESSIONS (16-35 contribs) — LIKELY ORAL QUESTIONS")
        print("=" * 70)

        oq_rows = db.session.execute(text("""
            SELECT title,
                   (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count,
                   date
            FROM ha_session s
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) BETWEEN 16 AND 35
            ORDER BY date DESC, contrib_count DESC
            LIMIT 60
        """)).fetchall()

        oq_total = db.session.execute(text("""
            SELECT COUNT(*) FROM ha_session s
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) BETWEEN 16 AND 35
        """)).scalar()

        print(f"\n{oq_total} sessions with 16-35 contributions in 'other' (sample of 60):")
        print(f"\n{'Title':<60} {'Contribs':>8}  {'Date'}")
        print("-" * 80)
        for r in oq_rows:
            print(f"{r.title[:58]:<60} {r.contrib_count:>8}  {r.date}")

        # -------------------------------------------------------------------
        # 6. Statement patterns in 'other'
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("6. CANDIDATE MINISTERIAL STATEMENTS IN 'OTHER'")
        print("=" * 70)

        stmt_rows = db.session.execute(text("""
            SELECT title,
                   (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count,
                   date, hrs_tag
            FROM ha_session s
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (
                LOWER(title) LIKE '%statement%'
                OR LOWER(title) LIKE '%update%'
                OR LOWER(title) LIKE '%written statement%'
                OR LOWER(title) LIKE '%oral statement%'
            )
            ORDER BY date DESC
            LIMIT 40
        """)).fetchall()

        print(f"\n{'Title':<60} {'Contribs':>8}  {'HRSTag':<20}  {'Date'}")
        print("-" * 105)
        for r in stmt_rows:
            print(f"{r.title[:58]:<60} {r.contrib_count:>8}  {str(r.hrs_tag or ''):<20}  {r.date}")

        # -------------------------------------------------------------------
        # 7. Procedural 'other' sessions — candidates for explicit detection
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("7. PROCEDURAL TITLE PATTERNS IN 'OTHER' (0-5 contribs)")
        print("=" * 70)

        proc_rows = db.session.execute(text("""
            SELECT title, COUNT(*) AS occurrences,
                   ROUND(AVG(contrib_count), 1) AS avg_contribs
            FROM (
                SELECT title,
                       (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count
                FROM ha_session s
                WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            )
            WHERE contrib_count BETWEEN 0 AND 5
            GROUP BY title
            ORDER BY occurrences DESC
            LIMIT 40
        """)).fetchall()

        print(f"\n{'Title':<60} {'Occurrences':>12}  {'Avg contribs':>12}")
        print("-" * 90)
        for r in proc_rows:
            print(f"{r.title[:58]:<60} {r.occurrences:>12}  {r.avg_contribs:>12}")

        # -------------------------------------------------------------------
        # 8. 'Arrangement of Business' in Grand Committee
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("8. 'ARRANGEMENT OF BUSINESS' SESSIONS — VENUE BREAKDOWN")
        print("=" * 70)

        aob_rows = db.session.execute(text("""
            SELECT COALESCE(location, '(null)') AS location,
                   debate_type,
                   COUNT(*) AS n,
                   ROUND(AVG(contrib_count), 1) AS avg_contribs
            FROM (
                SELECT location, debate_type,
                       (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count
                FROM ha_session s
                WHERE house = 'Lords' AND is_container = 0
                AND LOWER(title) = 'arrangement of business'
            )
            GROUP BY location, debate_type
            ORDER BY n DESC
        """)).fetchall()

        print(f"\n{'Location':<25} {'Type':<22} {'Count':>8}  {'Avg contribs':>12}")
        print("-" * 75)
        for r in aob_rows:
            print(f"{r.location:<25} {r.debate_type:<22} {r.n:>8}  {r.avg_contribs:>12}")

        # -------------------------------------------------------------------
        # 9. Unique title patterns by day — per-day session ordering
        #    (helps identify oral question position in day's chain)
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("9. SAMPLE SITTING DAY — FULL SESSION ORDER (most recent normal day)")
        print("=" * 70)

        # Find the most recent normal Lords sitting day (not prorogation)
        sample_date = db.session.execute(text("""
            SELECT date FROM ha_session
            WHERE house = 'Lords' AND is_container = 0
            AND date < '2026-04-29'
            GROUP BY date
            HAVING COUNT(*) > 5
            ORDER BY date DESC
            LIMIT 1
        """)).scalar()

        if sample_date:
            day_rows = db.session.execute(text("""
                SELECT title, debate_type, is_container,
                       (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count,
                       hrs_tag, location
                FROM ha_session s
                WHERE house = 'Lords' AND date = :d
                ORDER BY id
            """), {"d": sample_date}).fetchall()

            print(f"\nSample day: {sample_date}  ({len(day_rows)} sessions)")
            print(f"\n{'#':<4} {'Title':<52} {'Type':<22} {'Ctr':<4} {'n':>5}  {'HRSTag'}")
            print("-" * 115)
            for i, r in enumerate(day_rows, 1):
                ctr = "*" if r.is_container else " "
                print(f"{i:<4} {r.title[:50]:<52} {r.debate_type:<22} {ctr:<4} {r.contrib_count:>5}  {r.hrs_tag or ''}")

        # -------------------------------------------------------------------
        # 10. Summary: reclassification candidates
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("10. RECLASSIFICATION CANDIDATE COUNTS")
        print("=" * 70)

        other_total = db.session.execute(text("""
            SELECT COUNT(*) FROM ha_session
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
        """)).scalar()

        si_candidate = db.session.execute(text("""
            SELECT COUNT(*) FROM ha_session
            WHERE house = 'Lords' AND is_container = 0 AND debate_type = 'other'
            AND (
                title LIKE '% Regulations %' OR title LIKE '% Regulations'
                OR title LIKE '% (Regulations)%'
                OR title LIKE '% Order %' OR title LIKE '% Orders %'
                OR title LIKE '% Order)' OR title LIKE '% Rules %' OR title LIKE '% Rules'
            )
            AND title NOT LIKE '%Draft%' AND title NOT LIKE '%draft%'
        """)).scalar()

        aob_gc = db.session.execute(text("""
            SELECT COUNT(*) FROM ha_session
            WHERE house = 'Lords' AND is_container = 0
            AND LOWER(title) = 'arrangement of business'
            AND LOWER(COALESCE(location, '')) LIKE '%grand committee%'
        """)).scalar()

        print(f"\nTotal 'other' Lords sessions:               {other_total}")
        print(f"  Candidate SI reclassification:            {si_candidate}  ->statutory_instrument")
        print(f"  'Arrangement of Business' in GC:          {aob_gc}  ->other (from committee_stage)")
        print(f"\n  Remaining 'other' after SI fix:           {other_total - si_candidate}")
        print(f"\nNote: oral_questions and ministerial_statement classification")
        print(f"requires reviewing sections 5 and 6 above — confirm patterns")
        print(f"before writing UPDATE pass.")
        print(f"\nNote: SI regex candidate count ({si_candidate}) includes possible")
        print(f"annulment motions ('That X Regulations 2026 be annulled') — these")
        print(f"match the year-pattern but are debates, not SI consideration sessions.")
        print(f"Confirm via spot-check in section 4 before applying UPDATE.")


        # -------------------------------------------------------------------
        # 11. Random sample — 20 sessions for eyeballing
        #     Aggregate stats find patterns; random samples find weirdness
        #     that aggregates miss. Deliberately house-agnostic (containers
        #     excluded; all debate_types included).
        # -------------------------------------------------------------------
        print()
        print("=" * 70)
        print("11. RANDOM SAMPLE — 20 LORDS SESSIONS (eyeball check)")
        print("=" * 70)

        rand_rows = db.session.execute(text("""
            SELECT title, debate_type, is_container, date,
                   COALESCE(hrs_tag, '(null)') AS hrs_tag,
                   COALESCE(location, '(null)') AS location,
                   (SELECT COUNT(*) FROM ha_contribution c WHERE c.session_id = s.id) AS contrib_count
            FROM ha_session s
            WHERE house = 'Lords' AND is_container = 0
            ORDER BY RANDOM()
            LIMIT 20
        """)).fetchall()

        print(f"\n{'#':<4} {'Title':<50} {'Type':<22} {'n':>5}  {'HRSTag':<18}  {'Location':<22}  {'Date'}")
        print("-" * 145)
        for i, r in enumerate(rand_rows, 1):
            print(f"{i:<4} {r.title[:48]:<50} {r.debate_type:<22} {r.contrib_count:>5}  "
                  f"{r.hrs_tag[:16]:<18}  {r.location[:20]:<22}  {r.date}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lords taxonomy survey")
    parser.add_argument("--show-titles", action="store_true",
                        help="Include title samples in cluster output")
    args = parser.parse_args()
    run_survey(show_titles=args.show_titles)
