"""
Shared pytest fixtures for stakeholder_directory tests.

The `app` fixture provides a Flask application with an in-memory SQLite database.
Tests that need the database request it; tests that don't (e.g. test_scoring.py)
are unaffected.
"""
import pytest
from flask import Flask


@pytest.fixture
def app():
    """Flask app with all stakeholder_directory tables in SQLite in-memory."""
    from extensions import db as _db
    import stakeholder_directory.models  # noqa: F401 — register main models
    import stakeholder_directory.ingesters.staging  # noqa: F401 — register staging model

    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _app.config['TESTING'] = True
    _db.init_app(_app)

    with _app.app_context():
        _db.create_all()
        yield _app
        _db.drop_all()
