"""
Simulate slug deduplication on live DfE GOV.UK search results.
Confirms whether within-run slug collisions explain the 445 skips.
"""
import os, sys, re
from pathlib import Path

_env = Path(__file__).parents[1] / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import requests
_UA = "WestminsterBrief/2 (+https://westminsterbrief.co.uk)"
_SEARCH = "https://www.gov.uk/api/search.json"

sys.path.insert(0, str(Path(__file__).parents[1]))
from hansard_archive.slugs import slugify_theme

doc_types = ["statistics_announcement", "official_statistics", "statistical_data_set"]

# Fetch all 681 items
print("Fetching all DfE items from GOV.UK Search API...")
all_items = []
start = 0
while True:
    r = requests.get(_SEARCH, params={
        "filter_organisations[]": "department-for-education",
        "filter_content_store_document_type[]": doc_types,
        "start": start,
        "count": 100,
        "fields[]": ["title", "content_store_document_type", "link", "description"],
    }, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    batch = r.json().get("results", [])
    all_items.extend(batch)
    print(f"  fetched {len(all_items)} so far...")
    if len(batch) < 100:
        break
    start += 100

print(f"Total fetched: {len(all_items)}\n")

# Approximate the classifier's name canonicalisation:
# "canonical publication name stripped of year/release suffixes"
def canonical_name(title: str) -> str:
    t = title.strip()
    # Strip trailing year patterns
    patterns = [
        r'[,:]?\s*\d{4}\s*[-–/]\s*\d{2,4}.*$',     # 2024-25, 2024/25, 2024-2025
        r'[,:]?\s*\d{4}\s*to\s*\d{4}.*$',                # 2024 to 2025
        r'[,:]?\s*(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s*\d{4}.*$',
        r'[,:]?\s*(Q[1-4]|quarter\s*[1-4]).*$',
        r'[,:]?\s*\d{4}\s*$',                             # trailing year
        r'\s+\([^)]+\)\s*$',                              # trailing (parenthetical)
    ]
    for p in patterns:
        t2 = re.sub(p, '', t, flags=re.IGNORECASE).strip()
        if t2:
            t = t2
    return t


# Simulate the run loop
seen_slugs = {}       # slug -> (title, dtype, link) of first occurrence
written = []
skipped_list = []
failed = []

for item in all_items:
    title = item.get("title", "")
    dtype = item.get("content_store_document_type", "?")
    link  = item.get("link", "")

    if not title:
        failed.append((title, dtype, "no title"))
        continue

    name = canonical_name(title)
    slug = slugify_theme(name)
    if not slug:
        slug = slugify_theme(title)
    if not slug:
        failed.append((title, dtype, "empty slug"))
        continue

    if slug in seen_slugs:
        skipped_list.append((slug, dtype, title, seen_slugs[slug][0]))
    else:
        seen_slugs[slug] = (title, dtype, link)
        written.append((slug, dtype, title))

print("SLUG SIMULATION RESULTS")
print("=" * 60)
print(f"Total items:      {len(all_items)}")
print(f"Written (unique): {len(written)}")
print(f"Skipped (dup):    {len(skipped_list)}")
print(f"Pre-slug fail:    {len(failed)}")
skip_rate = 100 * len(skipped_list) / len(all_items) if all_items else 0
print(f"Skip rate:        {skip_rate:.1f}%")


print("\n\nTOP 20 MOST-COLLIDED SLUGS")
print("(publication series with most releases in the API)")
from collections import Counter
coll_count = Counter(s[0] for s in skipped_list)
print(f"\n{'slug':<50} {'skips'}")
print("-" * 60)
for slug, n in coll_count.most_common(20):
    first_title = seen_slugs.get(slug, ("?",))[0]
    print(f"{slug:<50} {n}  (e.g. {first_title[:50]})")


print("\n\nCOLLISION TYPE BREAKDOWN")
print("What document types do the skipped items belong to?")
dtype_counter = Counter(s[1] for s in skipped_list)
for dt, n in dtype_counter.most_common():
    print(f"  {dt}: {n} skips")


print("\n\nSAMPLE COLLISIONS (showing 15 canonical deduplication examples)")
print()
shown = set()
i = 0
for slug, dtype, new_title, first_title in skipped_list:
    if slug in shown:
        continue
    shown.add(slug)
    print(f"  Slug: {slug}")
    print(f"  Kept:    {first_title[:75]}")
    print(f"  Skipped: {new_title[:75]}")
    print()
    i += 1
    if i >= 15:
        break


print("\n\nINFERRED CLASSIFIER REJECTIONS")
print("(Items that would likely fail is_publication=false, based on title patterns)")
rejection_patterns = re.compile(
    r"(methodology|quality|guidance|framework|user guide|"
    r"frequently asked|FAQ|background|notes|concepts|definitions|"
    r"corrections|erratum|erratum|amended|revised methodology|"
    r"compliance|consultation|response to|call for evidence)",
    re.IGNORECASE
)
likely_rejected = [(t, dt) for (t, dt) in [(i.get("title",""), i.get("content_store_document_type","")) for i in all_items]
                   if rejection_patterns.search(t)]
print(f"  Items with methodology/guidance keywords: {len(likely_rejected)}")
print(f"  (These are likely is_publication=false from the LLM)")
print()
for t, dt in likely_rejected[:10]:
    print(f"  [{dt}] {t[:80]}")

print("\n\nWRITTEN PUBLICATIONS (first 20)")
print()
for slug, dtype, title in written[:20]:
    print(f"  {slug[:48]:<48}  {title[:55]}")
