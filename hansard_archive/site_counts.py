"""Exact archive counts for coverage lines (home page, archive pages).

Cached in-process for a few minutes so the counts follow the daily crons
without running COUNT queries on every page view.
"""
import time

from sqlalchemy import func

from extensions import db

_TTL = 300  # seconds
_cache: dict = {'counts': None, 'ts': 0.0}


def site_counts() -> dict:
    """Return {'sessions', 'pqs', 'bills', 'orgs'} as comma-formatted strings.

    A count that is missing or fails is an empty string, so templates fall
    back to their default wording.
    """
    if _cache['counts'] is not None and time.time() - _cache['ts'] < _TTL:
        return _cache['counts']
    counts = {'sessions': '', 'pqs': '', 'bills': '', 'orgs': ''}
    try:
        from hansard_archive.models import HansardSession, HaPQ, HaBill
        sc = (db.session.query(func.count(HansardSession.id))
              .filter(HansardSession.is_container.is_(False)).scalar() or 0)
        pqc = db.session.query(func.count(HaPQ.id)).scalar() or 0
        bc = db.session.query(func.count(HaBill.id)).scalar() or 0
        counts.update(sessions=f"{sc:,}" if sc else '',
                      pqs=f"{pqc:,}" if pqc else '',
                      bills=f"{bc:,}" if bc else '')
    except Exception:
        db.session.rollback()
    try:
        from stakeholder_directory.models import Organisation
        orgc = db.session.query(func.count(Organisation.id)).scalar() or 0
        counts['orgs'] = f"{orgc:,}" if orgc else ''
    except Exception:
        db.session.rollback()
    if counts['sessions'] or counts['pqs']:
        _cache.update(counts=counts, ts=time.time())
    return counts
