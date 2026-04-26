"""
Fetch inquiry_status from Parliament CommitteeBusiness API for all
distinct inquiry_ids in sd_engagement, then write back to the DB.

Status logic:
  latestReport != null  → 'reported'
  closeDate != null     → 'closed'
  else                  → 'open'

Run: python backfill_inquiry_status.py
"""
import sqlite3
import urllib.request
import json
import time

DB = 'intelligence.db'
BASE = 'https://committees-api.parliament.uk/api'
HDR = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}


def fetch_status(inquiry_id: str) -> str | None:
    url = f'{BASE}/CommitteeBusiness/{inquiry_id}'
    try:
        req = urllib.request.Request(url, headers=HDR)
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if data.get('latestReport'):
            return 'reported'
        if data.get('closeDate'):
            return 'closed'
        return 'open'
    except Exception as e:
        print(f'  WARN: {inquiry_id} → {e}')
        return None


def main():
    c = sqlite3.connect(DB)
    inquiry_ids = [r[0] for r in c.execute(
        'SELECT DISTINCT inquiry_id FROM sd_engagement WHERE inquiry_id IS NOT NULL'
    ).fetchall()]
    print(f'{len(inquiry_ids)} distinct inquiry IDs to fetch')

    statuses: dict[str, str] = {}
    for i, iid in enumerate(inquiry_ids, 1):
        status = fetch_status(iid)
        if status:
            statuses[iid] = status
        if i % 20 == 0:
            print(f'  {i}/{len(inquiry_ids)} fetched...')
        time.sleep(0.1)  # be polite to the API

    print(f'Fetched {len(statuses)} statuses')
    from collections import Counter
    print('Breakdown:', dict(Counter(statuses.values())))

    updated = 0
    for iid, status in statuses.items():
        cur = c.execute(
            'UPDATE sd_engagement SET inquiry_status=? WHERE inquiry_id=?',
            (status, iid),
        )
        updated += cur.rowcount
    c.commit()
    print(f'Updated {updated} engagement rows')

    # Spot-check
    rows = c.execute(
        'SELECT inquiry_id, inquiry_status, engagement_subject FROM sd_engagement '
        'WHERE inquiry_id IS NOT NULL LIMIT 5'
    ).fetchall()
    print('\nSpot-check:')
    for r in rows:
        print(f'  {r[0]}: {r[1]} — {r[2][:60] if r[2] else None}')
    c.close()


if __name__ == '__main__':
    main()
