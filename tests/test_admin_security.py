"""Admin login and the security settings around it, against the real app."""
import pyotp
import pytest

import flask_app as fa

SECRET = pyotp.random_base32()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(fa, 'ADMIN_TOKEN', 'right-token')
    monkeypatch.setattr(fa, 'TOTP_SECRET', SECRET)
    monkeypatch.setitem(fa._totp_last_used, 'counter', -1)
    monkeypatch.setattr(fa.limiter, 'enabled', False)
    return fa.app.test_client()


def _logged_in(client):
    with client.session_transaction() as s:
        return bool(s.get('admin_authenticated'))


def test_wrong_token_refused(client):
    r = client.post('/admin', data={'token': 'wrong'})
    assert 'Invalid token.' in r.get_data(as_text=True)
    assert not _logged_in(client)


def test_token_then_code_logs_in(client):
    r = client.post('/admin', data={'token': 'right-token'})
    assert 'authenticator' in r.get_data(as_text=True)
    assert not _logged_in(client)
    client.post('/admin', data={'token': 'right-token', 'totp': pyotp.TOTP(SECRET).now()})
    assert _logged_in(client)


def test_code_cannot_be_used_twice(client):
    code = pyotp.TOTP(SECRET).now()
    client.post('/admin', data={'token': 'right-token', 'totp': code})
    assert _logged_in(client)

    other = fa.app.test_client()
    r = other.post('/admin', data={'token': 'right-token', 'totp': code})
    assert 'Invalid authenticator code' in r.get_data(as_text=True)
    with other.session_transaction() as s:
        assert not s.get('admin_authenticated')


def test_wrong_code_refused(client):
    good = pyotp.TOTP(SECRET).now()
    bad = '000000' if good != '000000' else '111111'
    r = client.post('/admin', data={'token': 'right-token', 'totp': bad})
    assert 'Invalid authenticator code' in r.get_data(as_text=True)
    assert not _logged_in(client)


def test_admin_subpages_need_login(client):
    for path in ('/admin/stats', '/admin/producers'):
        r = client.get(path)
        assert r.status_code == 302 and r.headers['Location'].endswith('/admin')
    r = client.post('/admin/producers/1/authorise')
    assert r.status_code == 302


def test_session_cookie_flags(client):
    client.post('/admin', data={'token': 'right-token', 'totp': pyotp.TOTP(SECRET).now()})
    assert fa.app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert fa.app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
    assert fa.app.config['SESSION_COOKIE_SECURE'] is True


def test_edge_secret_unset_changes_nothing(client):
    assert client.get('/admin').status_code == 200


def test_edge_secret_required_when_set(client, monkeypatch):
    monkeypatch.setattr(fa, 'ADMIN_EDGE_SECRET', 'edge-value')
    assert client.get('/admin').status_code == 404
    assert client.get('/admin/stats').status_code == 404
    assert client.get('/admin', headers={'X-Admin-Edge': 'wrong'}).status_code == 404
    assert client.get('/admin', headers={'X-Admin-Edge': 'edge-value'}).status_code == 200
    # Other pages are unaffected.
    assert client.get('/ping').status_code == 200
