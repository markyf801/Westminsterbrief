from flask import Flask, session

from feature_flags import feature_enabled


def _app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test'
    return app


def test_beta_hidden_from_anonymous(monkeypatch):
    monkeypatch.setenv('FEATURE_X', 'beta')
    with _app().test_request_context('/'):
        assert feature_enabled('FEATURE_X') is False


def test_beta_visible_to_admin_session(monkeypatch):
    monkeypatch.setenv('FEATURE_X', 'beta')
    with _app().test_request_context('/'):
        session['admin_authenticated'] = True
        assert feature_enabled('FEATURE_X') is True


def test_hidden_stays_hidden_for_admin(monkeypatch):
    monkeypatch.setenv('FEATURE_X', 'hidden')
    with _app().test_request_context('/'):
        session['admin_authenticated'] = True
        assert feature_enabled('FEATURE_X') is False


def test_no_request_context(monkeypatch):
    monkeypatch.setenv('FEATURE_X', 'beta')
    assert feature_enabled('FEATURE_X') is False
