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


def test_health_hides_error_detail(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('could not connect to host secret-db.internal:5432')
    monkeypatch.setattr(fa.db.session, 'execute', boom)
    body = client.get('/health').get_data(as_text=True)
    assert 'secret-db' not in body
    assert 'FAIL: RuntimeError' in body


def test_ai_briefing_is_escaped():
    from flask import render_template
    with fa.app.test_request_context('/'):
        html = render_template('debate_briefing.html',
                               briefing='## Record\n<script>alert(1)</script>', selected_count=1)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_ai_endpoints_are_rate_limited(monkeypatch):
    monkeypatch.setattr(fa.limiter, 'enabled', True)
    c = fa.app.test_client()
    env = {'REMOTE_ADDR': '198.51.100.77'}
    codes = [c.post('/api/expand_topic', json={}, environ_base=env).status_code for _ in range(21)]
    assert codes[:20] == [400] * 20
    assert codes[20] == 429
    codes = [c.post('/stakeholder_search', data={}, environ_base=env).status_code for _ in range(11)]
    assert codes[-1] == 429


def test_feedback_email_escapes_visitor_text(client, monkeypatch):
    sent = {}

    def fake_send(to, subject, html_body, text_body):
        sent['html'] = html_body
        return True
    monkeypatch.setattr(fa, 'send_email', fake_send)
    client.post('/feedback', data={'name': '<b>x</b>', 'message': '<a href="http://evil">click</a>'})
    assert '<a href' not in sent['html'] and '<b>x</b>' not in sent['html']
    assert '&lt;a href=' in sent['html']


def test_www_redirect_uses_fixed_site(client):
    r = client.get('/about?x=1', headers={'Host': 'www.westminsterbrief.co.uk'})
    assert r.status_code == 301
    assert r.headers['Location'] == 'https://westminsterbrief.co.uk/about?x=1'
    r = client.get('/about', headers={'X-Forwarded-Host': 'www.evil.example'})
    assert 'evil.example' not in r.headers.get('Location', '')


def test_reset_link_ignores_forged_host(client, monkeypatch):
    monkeypatch.setenv('FEATURE_AUTH', 'live')
    sent = {}
    monkeypatch.setattr(fa, 'send_template_email', lambda **kw: sent.update(kw))
    with fa.app.app_context():
        if not fa.User.query.filter_by(email='reset-test@example.com').first():
            fa.db.session.add(fa.User(email='reset-test@example.com', password_hash='x'))
            fa.db.session.commit()
    client.post('/forgot-password', data={'email': 'reset-test@example.com'},
                headers={'X-Forwarded-Host': 'evil.example'})
    assert sent['reset_url'].startswith('https://westminsterbrief.co.uk/reset-password/')
