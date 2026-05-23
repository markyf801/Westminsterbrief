"""
DfE discovery skip rate investigation.
Reads DATABASE_URL from .env. Read-only queries only.
"""
import os, sys
from pathlib import Path

_env = Path(__file__).parents[1] / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import psycopg2, requests
DB_URL = os.environ.get("DATABASE_URL", "")
if not DB_URL:
    sys.exit("DATABASE_URL not set")

conn = psycopg2.connect(DB_URL)
_UA = "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"
_SEARCH = "https://www.gov.uk/api/search.json"

def q(sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return rows, cols

def show(title, sql, params=None):
    rows, cols = q(sql, params)
    print(f"\n--- {title} ---")
    if not rows:
        print("  (no rows)")
        return rows
    widths = [max(len(str(c)), max(len(str(r[i])) for r in rows))
              for i, c in enumerate(cols)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*cols))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(v) if v is not None else "NULL" for v in row]))
    return rows


print("=" * 65)
print("PART 1 — DB STATE CONFIRMATION")
print("=" * 65)

show("DfE publication count and breakdown", """
    SELECT
        COUNT(*) AS total_pubs,
        COUNT(*) FILTER (WHERE pub.authorisation_status='candidate') AS candidates,
        COUNT(*) FILTER (WHERE pub.authorisation_status='authorised') AS authorised,
        COUNT(*) FILTER (WHERE pub.authorisation_status='declined') AS declined
    FROM ha_stat_producer p
    JOIN ha_stat_publication pub ON pub.producer_id = p.id
    WHERE p.slug = 'department-for-education'
""")

show("DfE publications — discovered_at spread", """
    SELECT MIN(pub.discovered_at) AS first_seen,
           MAX(pub.discovered_at) AS last_seen,
           COUNT(*) AS total
    FROM ha_stat_producer p
    JOIN ha_stat_publication pub ON pub.producer_id = p.id
    WHERE p.slug = 'department-for-education'
""")

show("DfE publications per discovery date/hour", """
    SELECT DATE_TRUNC('hour', pub.discovered_at) AS disc_hour, COUNT(*) AS n
    FROM ha_stat_producer p
    JOIN ha_stat_publication pub ON pub.producer_id = p.id
    WHERE p.slug = 'department-for-education'
    GROUP BY disc_hour
    ORDER BY disc_hour
""")


print("\n\n" + "=" * 65)
print("PART 2 — GOVUK API: WHAT DOES DFE ACTUALLY RETURN?")
print("=" * 65)

# Count per document type
print("\nTotal items per document type for DfE:")
doc_types = ["statistics_announcement", "official_statistics", "statistical_data_set"]
counts = {}
for dt in doc_types:
    try:
        r = requests.get(_SEARCH, params={
            "filter_organisations[]": "department-for-education",
            "filter_content_store_document_type[]": dt,
            "count": 0,
        }, headers={"User-Agent": _UA}, timeout=20)
        r.raise_for_status()
        total = r.json().get("total", "?")
        counts[dt] = total
        print(f"  {dt}: {total}")
    except Exception as exc:
        print(f"  {dt}: FAILED — {exc}")
        counts[dt] = 0

# Combined total
try:
    r = requests.get(_SEARCH, params={
        "filter_organisations[]": "department-for-education",
        "filter_content_store_document_type[]": doc_types,
        "count": 0,
    }, headers={"User-Agent": _UA}, timeout=20)
    r.raise_for_status()
    combined = r.json().get("total", "?")
    print(f"\n  Combined (all three types): {combined}")
    type_sum = sum(v for v in counts.values() if isinstance(v, int))
    print(f"  Sum of individual counts:   {type_sum}")
    if isinstance(combined, int) and type_sum > 0:
        overlap = type_sum - combined
        print(f"  Apparent overlap:           {overlap} items appear in multiple type buckets")
except Exception as exc:
    print(f"  Combined count FAILED — {exc}")


print("\n\n" + "=" * 65)
print("PART 3 — SLUG DEDUPLICATION SIMULATION")
print("=" * 65)
print("Fetch first 200 items and simulate slugify → count collisions")

try:
    from hansard_archive.slugs import slugify_theme

    all_items = []
    for start in range(0, 300, 100):
        r = requests.get(_SEARCH, params={
            "filter_organisations[]": "department-for-education",
            "filter_content_store_document_type[]": doc_types,
            "start": start,
            "count": 100,
            "fields[]": ["title", "content_store_document_type", "link"],
        }, headers={"User-Agent": _UA}, timeout=20)
        r.raise_for_status()
        batch = r.json().get("results", [])
        all_items.extend(batch)
        if len(batch) < 100:
            break

    print(f"\nFetched {len(all_items)} items for simulation.\n")

    # Simulate what classifier does to names: strip year/release suffixes
    # (classifier prompt says "canonical publication name stripped of year/release suffixes")
    # We approximate by slugifying the full title and checking collisions
    import re
    def strip_year_suffix(title: str) -> str:
        # Remove common year patterns from end
        title = re.sub(
            r"[:,\s]*\d{4}[-–/]?\d{0,4}\s*(to\s*\d{4})?[,:]?\s*(Q[1-4]|quarter\s*[1-4])?.*$",
            "", title, flags=re.IGNORECASE
        ).strip()
        title = re.sub(r"\s+(january|february|march|april|may|june|july|august|"
                       r"september|october|november|december).*$", "", title,
                       flags=re.IGNORECASE).strip()
        return title or title  # fallback to original if stripped to empty

    seen_slugs = {}  # slug → first title
    slug_collisions = []

    for item in all_items:
        title = item.get("title", "")
        dtype = item.get("content_store_document_type", "?")
        link  = item.get("link", "")
        canonical = strip_year_suffix(title)
        slug = slugify_theme(canonical)
        if not slug:
            slug = slugify_theme(title)

        if slug in seen_slugs:
            slug_collisions.append((slug, dtype, title[:60], seen_slugs[slug]["title"][:50]))
        else:
            seen_slugs[slug] = {"title": title, "dtype": dtype}

    print(f"Unique slugs after canonicalisation: {len(seen_slugs)}")
    print(f"Slug collisions in first {len(all_items)} items: {len(slug_collisions)}")
    collision_rate = 100 * len(slug_collisions) / len(all_items) if all_items else 0
    print(f"Collision rate: {collision_rate:.1f}%\n")

    if slug_collisions:
        print("Sample collisions (slug, new_type, new_title, first_seen_title):")
        for i, (slug, dtype, title, first) in enumerate(slug_collisions[:20]):
            print(f"  [{dtype}] '{title}'")
            print(f"    → collides with: '{first}'")
            print(f"    → slug: {slug}")
            print()

except ImportError as e:
    print(f"  Could not import slugify_theme: {e}")
    print("  Run from project root: python scripts/dfe_skip_investigation.py")

print("\n\n" + "=" * 65)
print("PART 4 — DOCUMENT TYPE BREAKDOWN IN DB SAMPLE")
print("=" * 65)
print("Fetching document types for the actual 182 written DfE publications...")

# Cross-reference written publications against GOV.UK to see their doc types
dfe_rows, _ = q("""
    SELECT pub.url, pub.name, pub.slug
    FROM ha_stat_producer p
    JOIN ha_stat_publication pub ON pub.producer_id = p.id
    WHERE p.slug = 'department-for-education'
    ORDER BY pub.id
    LIMIT 30
""")

print(f"\nSample of 30 written DfE publications:")
print(f"{'slug':<45} {'name':<55}")
print("-" * 100)
for url, name, slug in dfe_rows:
    print(f"{slug:<45} {(name or '')[:55]}")


print("\n\n" + "=" * 65)
print("PART 5 — STATISTICS_ANNOUNCEMENT ANALYSIS")
print("=" * 65)
print("Are announcements a major source of skips?")
print("(Announcements are pre-publication notices — not actual data releases)")

try:
    r = requests.get(_SEARCH, params={
        "filter_organisations[]": "department-for-education",
        "filter_content_store_document_type[]": "statistics_announcement",
        "count": 20,
        "fields[]": ["title", "content_store_document_type", "description"],
    }, headers={"User-Agent": _UA}, timeout=20)
    r.raise_for_status()
    results = r.json().get("results", [])
    print(f"\nSample statistics_announcements for DfE:")
    for item in results[:15]:
        print(f"  {item.get('title', '?')[:80]}")
except Exception as exc:
    print(f"  FAILED — {exc}")

conn.close()
print("\n\nDone.")
