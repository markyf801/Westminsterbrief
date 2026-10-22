"""
PQ ingestor: ha_pq_deadline (date due for answer, named day) is filled from
the bulk WQ API records, never blanked, skipped for identity mismatches, and
a missing table never costs the question rows.

Re-uses the SQLite app, savepoint fix and fake API from test_pq_ingestor_identity.

Run: python -m pytest tests/test_pq_deadlines.py -v
"""

from datetime import date

from test_pq_ingestor_identity import _ingest, _pq, _value, app, db, fake_api  # noqa: F401


def _v(api_id, uin, due=None, named=None, **kw):
    item = _value(api_id, uin, "2026-10-20", **kw)
    item["value"]["dateForAnswer"] = f"{due}T00:00:00" if due else None
    item["value"]["isNamedDay"] = named
    return item


def _deadline(api_id):
    from hansard_archive.models import HaPQDeadline
    from extensions import db as _db
    return _db.session.get(HaPQDeadline, api_id)


def test_deadlines_stored_on_insert(db, fake_api):  # noqa: F811
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-26", named=True),
                        _v(5002, "900002", due="2026-10-29", named=False)]
    res = _ingest()
    assert res["inserted"] == 2
    d1, d2 = _deadline(5001), _deadline(5002)
    assert (d1.date_for_answer, d1.is_named_day) == (date(2026, 10, 26), True)
    assert (d2.date_for_answer, d2.is_named_day) == (date(2026, 10, 29), False)


def test_deadlines_written_for_unchanged_questions(db, fake_api):  # noqa: F811
    """First run after the migration: questions already stored still get a row."""
    fake_api["bulk"] = [_v(5001, "900001")]
    _ingest()
    assert _deadline(5001) is None                      # nothing to store yet
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-26", named=True)]
    res = _ingest()
    assert res["unchanged"] == 1
    db.session.expire_all()
    assert _deadline(5001).date_for_answer == date(2026, 10, 26)


def test_missing_values_never_blank_stored_ones(db, fake_api):  # noqa: F811
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-26", named=True)]
    _ingest()
    fake_api["bulk"] = [_v(5001, "900001", due=None, named=None)]
    _ingest()
    db.session.expire_all()
    d = _deadline(5001)
    assert (d.date_for_answer, d.is_named_day) == (date(2026, 10, 26), True)
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-27", named=False)]
    _ingest()
    db.session.expire_all()
    d = _deadline(5001)
    assert (d.date_for_answer, d.is_named_day) == (date(2026, 10, 27), False)


def test_identity_mismatch_writes_no_deadline(db, fake_api):  # noqa: F811
    db.session.add(_pq(api_id=5001, uin="OTHER", tabled_date=date(2026, 10, 20)))
    db.session.commit()
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-26", named=True)]
    res = _ingest()
    assert res["identity_mismatch"] == 1
    assert _deadline(5001) is None


def test_dry_run_writes_no_deadline(db, fake_api):  # noqa: F811
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-26", named=True)]
    _ingest(dry_run=True)
    assert _deadline(5001) is None


def test_missing_table_does_not_stop_question_ingest(db, fake_api):  # noqa: F811
    from sqlalchemy import text
    from hansard_archive.models import HaPQ
    db.session.execute(text("DROP TABLE ha_pq_deadline"))   # local in-memory SQLite only
    db.session.commit()
    fake_api["bulk"] = [_v(5001, "900001", due="2026-10-26", named=True),
                        _v(5002, "900002", due="2026-10-29", named=False)]
    res = _ingest()
    assert res["inserted"] == 2 and res["errors"] == 0
    db.session.remove()
    assert {r.uin for r in HaPQ.query.all()} == {"900001", "900002"}
