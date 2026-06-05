"""
Tests for Phase 1.9 re-discovery: pre-classify dedup + run_rediscovery.

Covers the two correctness flags hardest:
  - normalised URL dedup (trailing slash / case / query → same publication)
  - ONS date-refresh routes through the shared helper and is the ONLY
    update-existing path; GOV.UK first_published_at is NEVER overwritten.

Run: python -m pytest tests/test_rediscovery.py -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hansard_archive.discovery import (
    _normalise_pub_url,
    _ons_dataset_id,
    _lookup_existing,
    _build_existing_index,
    run_rediscovery,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_discovery_skill.py — in-memory SQLite)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models
    import cache_models
    import stakeholder_directory.models
    import stakeholder_directory.ingesters.staging

    app = Flask(__name__)
    app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope="module")
def db(test_app):
    from extensions import db as _db
    return _db


def _producer(db, slug, **kw):
    from hansard_archive.models import StatProducer
    defaults = dict(
        name=f"Producer {slug}", producer_type="central_department",
        web_root_url="https://www.gov.uk/", licence="OGL_v3",
        authorisation_status="authorised", discovery_status="completed",
        licence_evidence_raw_url="https://example.com/copyright",
    )
    defaults.update(kw)
    p = StatProducer(slug=slug, **defaults)
    db.session.add(p)
    db.session.flush()
    return p


def _pub(db, producer, slug, url, **kw):
    from hansard_archive.models import StatPublication
    p = StatPublication(producer_id=producer.id, slug=slug, name=slug, url=url,
                        authorisation_status="candidate", **kw)
    db.session.add(p)
    db.session.flush()
    return p


def _cand(url, title="Some Stat"):
    from hansard_archive.discovery.strategies import CandidateItem
    return CandidateItem(title=title, url=url)


# ---------------------------------------------------------------------------
# URL normalisation (Flag 2)
# ---------------------------------------------------------------------------

class TestNormaliseUrl:

    def test_trailing_slash_ignored(self):
        assert (_normalise_pub_url("https://www.gov.uk/government/statistics/foo/")
                == _normalise_pub_url("https://www.gov.uk/government/statistics/foo"))

    def test_scheme_and_host_case_ignored(self):
        assert (_normalise_pub_url("HTTPS://WWW.GOV.UK/government/statistics/foo")
                == _normalise_pub_url("https://www.gov.uk/government/statistics/foo"))

    def test_query_and_fragment_stripped(self):
        assert (_normalise_pub_url("https://www.gov.uk/government/statistics/foo?utm=x#sec")
                == _normalise_pub_url("https://www.gov.uk/government/statistics/foo"))


class TestOnsDatasetId:

    def test_human_url(self):
        assert _ons_dataset_id("https://www.ons.gov.uk/datasets/wellbeing-quarterly") == "wellbeing-quarterly"

    def test_api_version_url(self):
        assert _ons_dataset_id(
            "https://api.beta.ons.gov.uk/v1/datasets/wellbeing-quarterly/editions/x/versions/9"
        ) == "wellbeing-quarterly"

    def test_non_ons_returns_none(self):
        assert _ons_dataset_id("https://www.gov.uk/government/statistics/foo") is None


# ---------------------------------------------------------------------------
# Lookup / index
# ---------------------------------------------------------------------------

class TestLookup:

    def test_govuk_match_via_normalised_url(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "lk-gov")
            _pub(db, p, "foo", "https://www.gov.uk/government/statistics/foo")
            by_url, by_ds, slugs = _build_existing_index(p.id, db.session)
            # trailing-slash variant of the candidate still matches
            existing, is_ons = _lookup_existing(
                "https://www.gov.uk/government/statistics/foo/", by_url, by_ds)
            assert existing is not None and is_ons is False
            assert _lookup_existing(
                "https://www.gov.uk/government/statistics/other", by_url, by_ds)[0] is None
            db.session.rollback()

    def test_ons_match_via_dataset_id(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "lk-ons")
            _pub(db, p, "wbq", "https://www.ons.gov.uk/datasets/wbq")
            by_url, by_ds, slugs = _build_existing_index(p.id, db.session)
            existing, is_ons = _lookup_existing(
                "https://api.beta.ons.gov.uk/v1/datasets/wbq/editions/x/versions/3", by_url, by_ds)
            assert existing is not None and is_ons is True
            db.session.rollback()


# ---------------------------------------------------------------------------
# run_rediscovery
# ---------------------------------------------------------------------------

def _patch_strategy(candidates):
    return patch(
        "hansard_archive.discovery.strategies.GovUkSearchStrategy.fetch_candidates",
        return_value=candidates,
    ), patch(
        "hansard_archive.discovery.strategies.GovUkSearchStrategy.can_handle",
        return_value=True,
    ), patch(
        "hansard_archive.discovery.strategies.OnsApiStrategy.can_handle",
        return_value=False,
    )


class TestRunRediscovery:

    def test_known_url_skipped_without_classify(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "rd-known")
            _pub(db, p, "foo", "https://www.gov.uk/government/statistics/foo")
            db.session.commit()
            classify = MagicMock()
            s1, s2, s3 = _patch_strategy([_cand("https://www.gov.uk/government/statistics/foo")])
            with s1, s2, s3, patch("hansard_archive.discovery.classifier.classify_candidate", classify):
                summary = run_rediscovery(p, db.session, "key", dry_run=False)
            assert summary["skipped_known"] == 1
            assert summary["new"] == 0
            classify.assert_not_called()          # the cost guarantee
            assert p.last_rediscovered_at is not None
            assert p.discovery_status == "completed"   # untouched
            db.session.rollback()

    def test_new_url_classified_and_inserted(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "rd-new")
            db.session.commit()
            from hansard_archive.models import StatPublication
            cand = [_cand("https://www.gov.uk/government/statistics/brand-new")]
            result = {"is_publication": True, "name": "Brand New Stats",
                      "update_cadence": "annual", "policy_areas": ["Education"]}
            s1, s2, s3 = _patch_strategy(cand)
            with s1, s2, s3, \
                 patch("hansard_archive.discovery.classifier.classify_candidate", return_value=result), \
                 patch("hansard_archive.discovery.pub_dates.resolve_publication_date", return_value=date(2026, 1, 1)):
                summary = run_rediscovery(p, db.session, "key", dry_run=False)
            assert summary["new"] == 1
            pub = db.session.query(StatPublication).filter_by(producer_id=p.id).first()
            assert pub is not None and pub.name == "Brand New Stats"
            assert pub.first_published_at == date(2026, 1, 1)
            db.session.rollback()

    def test_ons_known_dataset_refreshes_date_via_helper(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "rd-ons")
            existing = _pub(db, p, "wbq", "https://www.ons.gov.uk/datasets/wbq",
                            first_published_at=date(2025, 1, 1))
            db.session.commit()
            classify = MagicMock()
            cand = [_cand("https://api.beta.ons.gov.uk/v1/datasets/wbq/editions/x/versions/9")]
            s1, s2, s3 = _patch_strategy(cand)
            with s1, s2, s3, \
                 patch("hansard_archive.discovery.classifier.classify_candidate", classify), \
                 patch("hansard_archive.discovery.pub_dates.resolve_publication_date", return_value=date(2026, 6, 1)):
                summary = run_rediscovery(p, db.session, "key", dry_run=False)
            assert summary["skipped_known"] == 1
            assert summary["ons_date_updated"] == 1
            classify.assert_not_called()
            assert existing.first_published_at == date(2026, 6, 1)   # refreshed via helper
            db.session.rollback()

    def test_govuk_known_url_first_published_not_overwritten(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "rd-gov-date")
            existing = _pub(db, p, "foo", "https://www.gov.uk/government/statistics/foo",
                            first_published_at=date(2024, 1, 1))
            db.session.commit()
            cand = [_cand("https://www.gov.uk/government/statistics/foo")]
            s1, s2, s3 = _patch_strategy(cand)
            resolve = MagicMock(return_value=date(2026, 9, 9))
            with s1, s2, s3, \
                 patch("hansard_archive.discovery.classifier.classify_candidate", MagicMock()), \
                 patch("hansard_archive.discovery.pub_dates.resolve_publication_date", resolve):
                run_rediscovery(p, db.session, "key", dry_run=False)
            # GOV.UK known URL: date NOT touched, helper NOT called for it
            assert existing.first_published_at == date(2024, 1, 1)
            resolve.assert_not_called()
            db.session.rollback()

    def test_dry_run_writes_nothing(self, test_app, db):
        with test_app.app_context():
            p = _producer(db, "rd-dry")
            db.session.commit()
            from hansard_archive.models import StatPublication
            cand = [_cand("https://www.gov.uk/government/statistics/dry-new")]
            result = {"is_publication": True, "name": "Dry New", "policy_areas": []}
            s1, s2, s3 = _patch_strategy(cand)
            with s1, s2, s3, \
                 patch("hansard_archive.discovery.classifier.classify_candidate", return_value=result), \
                 patch("hansard_archive.discovery.pub_dates.resolve_publication_date", return_value=None):
                summary = run_rediscovery(p, db.session, "key", dry_run=True)
            assert summary["new"] == 1                       # would insert
            assert db.session.query(StatPublication).filter_by(producer_id=p.id).count() == 0
            assert p.last_rediscovered_at is None            # not stamped on dry-run
            db.session.rollback()
