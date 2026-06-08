"""
Tests for the per-producer stats pages (/stats/producer/<slug>) — step 4 of the
stats-catalogue launch.

Covers:
  - populated producer → 200, distinct title/H1, ≥1 publication row
  - thin-content gate: producer with 0 visible pubs → 404
  - unknown / declined / unauthorised slug → 404
  - flag off → coming-soon (the blueprint's before_request gate)
  - the catalogue's "Browse by producer" cross-link
  - two producers render distinct title/H1 (no duplicate-page regression)
  - the catalogue still renders its publications after the card-partial extraction

Run: python -m pytest tests/test_stats_producer_pages.py -v
"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(scope="module")
def app():
    # conftest.py forces sqlite:///:memory: before flask_app is imported.
    import flask_app as _fa
    _fa.app.config["TESTING"] = True
    _fa.app.config["LOGIN_DISABLED"] = True
    from extensions import db as _db
    with _fa.app.app_context():
        _db.create_all()
    return _fa.app


@pytest.fixture(scope="module")
def db(app):
    from extensions import db as _db
    return _db


def _producer(db, slug, name, ptype="central_department", short=None,
              auth="authorised", description=None):
    from hansard_archive.models import StatProducer
    p = StatProducer(
        slug=slug, name=name, short_name=short, producer_type=ptype,
        web_root_url="https://www.gov.uk/", licence="OGL_v3",
        authorisation_status=auth, discovery_status="completed",
        description=description,
    )
    db.session.add(p)
    db.session.flush()
    return p


def _pub(db, producer, slug, name, d=date(2026, 1, 1), auth="authorised"):
    from hansard_archive.models import StatPublication
    p = StatPublication(
        producer_id=producer.id, slug=slug, name=name,
        url=f"https://www.gov.uk/government/statistics/{slug}",
        authorisation_status=auth, first_published_at=d,
    )
    db.session.add(p)
    db.session.flush()
    return p


@pytest.fixture
def client_enabled(app, monkeypatch):
    # Force the catalogue flag ON for the request (route + blueprint gate read it).
    import stats_catalogue
    monkeypatch.setattr(stats_catalogue, "_ENABLED", True)
    return app.test_client()


class TestProducerPage:

    def test_populated_producer_renders(self, app, db, client_enabled):
        with app.app_context():
            p = _producer(db, "dept-alpha", "Department Alpha", short="DALPHA")
            _pub(db, p, "alpha-stats-2026", "Alpha Annual Statistics 2026")
            db.session.commit()
        r = client_enabled.get("/stats/producer/dept-alpha")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Department Alpha" in body            # name in title + H1
        assert "Alpha Annual Statistics 2026" in body  # ≥1 publication row
        assert "central government department" in body  # type-label fallback fired

    def test_description_overrides_fallback(self, app, db, client_enabled):
        with app.app_context():
            p = _producer(db, "dept-desc", "Department Desc",
                          description="A bespoke description that should render verbatim.")
            _pub(db, p, "desc-stats", "Desc Stats")
            db.session.commit()
        body = client_enabled.get("/stats/producer/dept-desc").get_data(as_text=True)
        assert "A bespoke description that should render verbatim." in body
        assert "publishes UK official statistics" not in body  # fallback suppressed

    def test_unpopulated_producer_404(self, app, db, client_enabled):
        with app.app_context():
            _producer(db, "dept-empty", "Department Empty")  # no pubs
            db.session.commit()
        assert client_enabled.get("/stats/producer/dept-empty").status_code == 404

    def test_declined_producer_404(self, app, db, client_enabled):
        with app.app_context():
            p = _producer(db, "dept-declined", "Department Declined", auth="declined")
            _pub(db, p, "declined-stats", "Declined Stats")  # has a pub, but declined
            db.session.commit()
        assert client_enabled.get("/stats/producer/dept-declined").status_code == 404

    def test_unknown_slug_404(self, client_enabled):
        assert client_enabled.get("/stats/producer/does-not-exist").status_code == 404

    def test_two_producers_distinct(self, app, db, client_enabled):
        with app.app_context():
            a = _producer(db, "dept-one", "Department One")
            b = _producer(db, "dept-two", "Department Two")
            _pub(db, a, "one-stats", "One Stats")
            _pub(db, b, "two-stats", "Two Stats")
            db.session.commit()
        b1 = client_enabled.get("/stats/producer/dept-one").get_data(as_text=True)
        b2 = client_enabled.get("/stats/producer/dept-two").get_data(as_text=True)
        assert "Department One" in b1 and "Department Two" not in b1
        assert "Department Two" in b2 and "Department One" not in b2

    def test_flag_off_serves_coming_soon(self, app, db):
        # Default _ENABLED is False (no env var in tests) → before_request gate fires.
        client = app.test_client()
        r = client.get("/stats/producer/anything")
        assert r.status_code == 200   # gate serves the coming-soon page, not the route's 404
        assert "being rebuilt" in r.get_data(as_text=True).lower()


class TestCatalogueCrossLink:

    def test_browse_by_producer_links_present(self, app, db, client_enabled):
        with app.app_context():
            p = _producer(db, "dept-browse", "Department Browse")
            _pub(db, p, "browse-stats", "Browse Stats")
            db.session.commit()
        body = client_enabled.get("/stats").get_data(as_text=True)
        assert "Browse by producer" in body
        assert "/stats/producer/dept-browse" in body

    def test_catalogue_still_lists_publications(self, app, db, client_enabled):
        # Regression: the card-partial extraction must not change the catalogue's
        # publication listing.
        with app.app_context():
            p = _producer(db, "dept-reg", "Department Reg")
            _pub(db, p, "reg-pub", "Regression Publication Title")
            db.session.commit()
        body = client_enabled.get("/stats").get_data(as_text=True)
        assert "Regression Publication Title" in body
        assert "/stats/dept-reg/reg-pub" in body   # detail link still built
