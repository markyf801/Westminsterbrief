"""Deep probe: find the right API endpoint for committee business/inquiry status."""
import sqlite3
import urllib.request
import json

DB = 'intelligence.db'
c = sqlite3.connect(DB)
sample_ids = [r[0] for r in c.execute(
    "SELECT inquiry_id FROM sd_engagement "
    "WHERE inquiry_id IS NOT NULL GROUP BY inquiry_id LIMIT 5"
).fetchall()]
c.close()
print("Sample business IDs:", sample_ids)

BASE = 'https://committees-api.parliament.uk/api'
HDR = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}

# Try different endpoint patterns
candidate_paths = [
    '/CommitteeBusiness/{id}',
    '/Inquiry/{id}',
    '/OralEvidence?businessId={id}&take=1',
    '/OralEvidence/{id}',
    '/WrittenEvidence?businessId={id}&take=1',
]

test_id = sample_ids[0]
print(f"\nTrying endpoint patterns with ID {test_id}:")
for path in candidate_paths:
    url = BASE + path.format(id=test_id)
    try:
        req = urllib.request.Request(url, headers=HDR)
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        top_keys = list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]"
        print(f"  200  {url}")
        print(f"    keys: {top_keys}")
        if isinstance(data, dict):
            for k in ('status', 'inquiryStatus', 'Status', 'phase', 'slug', 'title', 'displayTitle'):
                if k in data:
                    print(f"    {k}: {data[k]!r}")
    except urllib.error.HTTPError as e:
        print(f"  {e.code}  {url}")
    except Exception as e:
        print(f"  ERR({type(e).__name__})  {url}")

# Also check a full oral evidence record to see what business data looks like
print(f"\nFull oral evidence record for a publication linked to business {test_id}:")
c2 = sqlite3.connect(DB)
pub_url = c2.execute(
    "SELECT source_url FROM sd_engagement WHERE inquiry_id=? LIMIT 1", (test_id,)
).fetchone()
c2.close()
if pub_url:
    raw_url = pub_url[0].replace('/html/', '').rstrip('/')
    pub_id = raw_url.split('/')[-1]
    for endpoint_url in [
        f'{BASE}/OralEvidence/{pub_id}',
        f'{BASE}/WrittenEvidence/{pub_id}',
    ]:
        try:
            req = urllib.request.Request(endpoint_url, headers=HDR)
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            print(f"  200 {endpoint_url}")
            # Show business-related keys
            for k in ('committeeBusinesses', 'committeeBusiness', 'status', 'inquiryStatus'):
                if k in data:
                    val = data[k]
                    if isinstance(val, list) and val:
                        print(f"    {k}[0]: {val[0]}")
                    else:
                        print(f"    {k}: {val}")
        except urllib.error.HTTPError as e:
            print(f"  {e.code} {endpoint_url}")
        except Exception as e:
            print(f"  ERR {endpoint_url}: {e}")
