"""
Hansard tagging distribution analysis for Phase 1.8 taxonomy design conversation.

Tasks:
  1. Policy area distribution (per-area counts, session-level tag distribution)
  2. Specifics analysis (unique count, top 50, frequency distribution)
  3. Co-occurrence (for top 5 policy areas, top specifics that co-occur)

Usage:
    python scripts/tagging_distribution.py

Reads DATABASE_URL from .env in the project root.
"""

import os
import sys
from pathlib import Path

_env_path = Path(__file__).parents[1] / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

try:
    import psycopg2
except ImportError:
    print("psycopg2 not available — install with: pip install psycopg2-binary")
    sys.exit(1)

DB_URL = os.environ.get("DATABASE_URL", "")
if not DB_URL:
    print("DATABASE_URL not set")
    sys.exit(1)

conn = psycopg2.connect(DB_URL)

def q(sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall(), [d[0] for d in cur.description]


def print_table(rows, headers, col_widths=None):
    if not col_widths:
        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                      for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt.format(*[str(v) for v in row]))


print("=" * 70)
print("TASK 1: POLICY AREA DISTRIBUTION")
print("=" * 70)

total_sessions, _ = q("SELECT COUNT(*) FROM ha_session")
total = total_sessions[0][0]
print(f"\nTotal sessions in ha_session: {total:,}\n")

tagged_sessions, _ = q("""
    SELECT COUNT(DISTINCT session_id)
    FROM ha_session_theme
    WHERE theme_type = 'policy_area'
""")
tagged = tagged_sessions[0][0]
untagged = total - tagged
print(f"Sessions with at least one policy_area tag: {tagged:,} ({100*tagged/total:.1f}%)")
print(f"Sessions with zero policy_area tags:        {untagged:,} ({100*untagged/total:.1f}%)\n")

print("Policy area usage (sorted by count):\n")
rows, _ = q("""
    SELECT
        theme,
        COUNT(*) AS count,
        ROUND(100.0 * COUNT(*) / %(total)s, 2) AS pct_all_sessions,
        ROUND(100.0 * COUNT(*) / %(tagged)s, 2) AS pct_tagged_sessions
    FROM ha_session_theme
    WHERE theme_type = 'policy_area'
    GROUP BY theme
    ORDER BY count DESC
""", {"total": total, "tagged": tagged})
print_table(rows, ["policy_area", "count", "pct_all_sess", "pct_tagged_sess"],
            [45, 7, 13, 16])

print("\n\nTags-per-session distribution (among tagged sessions):\n")
dist_rows, _ = q("""
    SELECT tag_count, COUNT(*) AS sessions
    FROM (
        SELECT session_id, COUNT(*) AS tag_count
        FROM ha_session_theme
        WHERE theme_type = 'policy_area'
        GROUP BY session_id
    ) t
    GROUP BY tag_count
    ORDER BY tag_count
""")
print_table(dist_rows, ["tags_per_session", "session_count"])

print("\n\n" + "=" * 70)
print("TASK 2: SPECIFICS ANALYSIS")
print("=" * 70)

unique_specifics, _ = q("""
    SELECT COUNT(DISTINCT theme)
    FROM ha_session_theme
    WHERE theme_type = 'specific'
""")
total_specific_tags, _ = q("""
    SELECT COUNT(*)
    FROM ha_session_theme
    WHERE theme_type = 'specific'
""")
print(f"\nUnique specific tags: {unique_specifics[0][0]:,}")
print(f"Total specific tag instances: {total_specific_tags[0][0]:,}\n")

print("Top 50 specific tags:\n")
top50, _ = q("""
    SELECT theme, COUNT(*) AS count
    FROM ha_session_theme
    WHERE theme_type = 'specific'
    GROUP BY theme
    ORDER BY count DESC
    LIMIT 50
""")
print_table(top50, ["specific_tag", "count"], [50, 7])

print("\n\nFrequency distribution of specific tags:\n")
freq_rows, _ = q("""
    SELECT bucket, COUNT(*) AS unique_tags
    FROM (
        SELECT theme,
            COUNT(*) AS c,
            CASE
                WHEN COUNT(*) = 1 THEN 'once'
                WHEN COUNT(*) BETWEEN 2 AND 5 THEN '2-5x'
                WHEN COUNT(*) BETWEEN 6 AND 19 THEN '6-19x'
                WHEN COUNT(*) >= 20 THEN '20+ times'
            END AS bucket
        FROM ha_session_theme
        WHERE theme_type = 'specific'
        GROUP BY theme
    ) t
    GROUP BY bucket
    ORDER BY MIN(c)
""")
print_table(freq_rows, ["frequency_bucket", "unique_tags_in_bucket"])

print("\n\n" + "=" * 70)
print("TASK 3: CO-OCCURRENCE (top 5 policy areas × top specifics)")
print("=" * 70)

top5_areas, _ = q("""
    SELECT theme
    FROM ha_session_theme
    WHERE theme_type = 'policy_area'
    GROUP BY theme
    ORDER BY COUNT(*) DESC
    LIMIT 5
""")
top5 = [r[0] for r in top5_areas]

for area in top5:
    print(f"\n--- {area} ---\n")
    co_rows, _ = q("""
        SELECT s.theme AS specific, COUNT(*) AS co_count
        FROM ha_session_theme p
        JOIN ha_session_theme s ON s.session_id = p.session_id
            AND s.theme_type = 'specific'
        WHERE p.theme_type = 'policy_area'
          AND p.theme = %s
        GROUP BY s.theme
        ORDER BY co_count DESC
        LIMIT 15
    """, (area,))
    if co_rows:
        print_table(co_rows, ["specific_tag", "co_occurrences"], [45, 14])
    else:
        print("  (no specifics found)")

conn.close()
print("\n\nDone.")
