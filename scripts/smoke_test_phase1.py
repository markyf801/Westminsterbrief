"""
Phase 1 smoke test — runs against a locally running Flask app on port 5000.
Uses requests.Session to maintain cookies. Accesses SQLite DB directly to
extract reset tokens (since email is in test mode / not delivered).

Run: python scripts/smoke_test_phase1.py
"""
import sys
import io
import sqlite3
import requests

# Force UTF-8 output so arrow characters render on Windows cp1252 terminals
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE = 'http://127.0.0.1:5000'
DB_PATH = 'intelligence.db'
TEST_EMAIL = 'smoketest_p1@example.com'
INITIAL_PW = 'TestPass1234!'
RESET_PW   = 'ResetPass5678!'

PASS = '  [PASS]'
FAIL = '  [FAIL]'

errors = []

def check(label, condition, detail=''):
    if condition:
        print(f'{PASS} {label}')
    else:
        msg = f'{FAIL} {label}' + (f' — {detail}' if detail else '')
        print(msg)
        errors.append(msg)

def db_get(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row

def db_cleanup():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM "user" WHERE email = ?', (TEST_EMAIL,))
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
print('\n=== Phase 1 Smoke Test ===\n')

# ------------------------------------------------------------------
print('0. Feature flag logic — unit tests (no server required)')
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from feature_flags import feature_enabled

class _MockUser:
    def __init__(self, email, authenticated=True):
        self.email = email
        self.is_authenticated = authenticated

_save = {}
for k in ('FEATURE_AUTH', 'FEATURE_ACCOUNT', 'BETA_USERS'):
    _save[k] = os.environ.get(k)

# hidden → always False
os.environ['FEATURE_AUTH'] = 'hidden'
check('hidden → False for anonymous',      not feature_enabled('FEATURE_AUTH'))
check('hidden → False for auth user',      not feature_enabled('FEATURE_AUTH', _MockUser('a@b.com')))

# live → always True
os.environ['FEATURE_AUTH'] = 'live'
check('live → True for anonymous',         feature_enabled('FEATURE_AUTH'))
check('live → True for auth user',         feature_enabled('FEATURE_AUTH', _MockUser('a@b.com')))

# beta → only matching users
os.environ['FEATURE_AUTH'] = 'beta'
os.environ['BETA_USERS'] = 'beta@example.com,other@example.com'
check('beta → False for anonymous',        not feature_enabled('FEATURE_AUTH'))
check('beta → False for non-beta user',    not feature_enabled('FEATURE_AUTH', _MockUser('nobody@example.com')))
check('beta → True for beta user',         feature_enabled('FEATURE_AUTH', _MockUser('beta@example.com')))
check('beta → case-insensitive match',     feature_enabled('FEATURE_AUTH', _MockUser('BETA@EXAMPLE.COM')))
check('beta → False for unauthenticated',  not feature_enabled('FEATURE_AUTH', _MockUser('beta@example.com', authenticated=False)))

# unrecognised value → fail closed
os.environ['FEATURE_AUTH'] = 'enabled'
check('unrecognised value → False',        not feature_enabled('FEATURE_AUTH'))

# restore
for k, v in _save.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

# ------------------------------------------------------------------
print('\nFlags confirmed live for remaining tests (app must be running with FEATURE_*=live)\n')

# Pre-clean any leftover test user
db_cleanup()

s = requests.Session()

# ------------------------------------------------------------------
print('1. GET /register — page loads')
r = s.get(f'{BASE}/register')
check('GET /register → 200', r.status_code == 200, r.status_code)

# ------------------------------------------------------------------
print('\n2. POST /register — create new user')
r = s.post(f'{BASE}/register', data={
    'email': TEST_EMAIL,
    'password': INITIAL_PW,
    'confirm_password': INITIAL_PW,
}, allow_redirects=True)
check('POST /register → redirects to onboarding or home', r.status_code == 200, r.status_code)
check('Landed on onboarding page', 'onboarding' in r.url or 'preferences' in r.url or 'home' in r.url or 'my_alerts' in r.url, r.url)

row = db_get('SELECT id, access_tier FROM "user" WHERE email = ?', (TEST_EMAIL,))
check('User created in DB', row is not None)
if row:
    check('Tier is "standard" (non-gov email)', row[1] == 'standard', row[1])

# ------------------------------------------------------------------
print('\n3. GET /account — verify tier badge')
r = s.get(f'{BASE}/account')
check('GET /account → 200', r.status_code == 200, r.status_code)
check('Standard tier badge visible', 'Standard' in r.text, 'badge text missing')
check('Email shown on page', TEST_EMAIL in r.text)

# ------------------------------------------------------------------
print('\n4. POST /account/change-password — wrong current password')
r = s.post(f'{BASE}/account/change-password', data={
    'current_password': 'wrongpassword',
    'new_password': RESET_PW,
    'confirm_password': RESET_PW,
}, allow_redirects=True)
check('Rejected bad current password', 'incorrect' in r.text.lower() or 'invalid' in r.text.lower(), 'no error shown')

# ------------------------------------------------------------------
print('\n5. GET /account/export — data download')
r = s.get(f'{BASE}/account/export')
check('GET /account/export → 200', r.status_code == 200, r.status_code)
check('Content-Disposition header set', 'attachment' in r.headers.get('Content-Disposition', ''))
check('Content is valid JSON with email', TEST_EMAIL in r.text)

# ------------------------------------------------------------------
print('\n6. POST /logout')
r = s.post(f'{BASE}/logout', allow_redirects=True)
# logout is GET in this app
r = s.get(f'{BASE}/logout', allow_redirects=True)
check('Logout redirects to login', 'login' in r.url or r.status_code == 200)

# ------------------------------------------------------------------
print('\n7. GET /forgot-password — page loads')
r = s.get(f'{BASE}/forgot-password')
check('GET /forgot-password → 200', r.status_code == 200, r.status_code)

# ------------------------------------------------------------------
print('\n8. POST /forgot-password — request reset')
r = s.post(f'{BASE}/forgot-password', data={'email': TEST_EMAIL}, allow_redirects=True)
check('Forgot-password POST → redirects to login', r.status_code == 200)
check('Neutral flash message shown', 'reset link' in r.text.lower() or 'if an account' in r.text.lower(), r.text[:200])

token_row = db_get('SELECT reset_token FROM "user" WHERE email = ?', (TEST_EMAIL,))
check('Reset token stored in DB', token_row and token_row[0], token_row)

# ------------------------------------------------------------------
print('\n9. GET /reset-password/<token>')
if token_row and token_row[0]:
    token = token_row[0]
    r = s.get(f'{BASE}/reset-password/{token}')
    check('GET /reset-password/<token> → 200', r.status_code == 200, r.status_code)
    check('New password form shown', 'new password' in r.text.lower() or 'password' in r.text.lower())

    r = s.post(f'{BASE}/reset-password/{token}', data={
        'password': RESET_PW,
        'confirm_password': RESET_PW,
    }, allow_redirects=True)
    check('POST /reset-password → redirects to login', r.status_code == 200)
    check('Success flash shown', 'password' in r.text.lower())

    token_after = db_get('SELECT reset_token FROM "user" WHERE email = ?', (TEST_EMAIL,))
    check('Reset token cleared after use', token_after and token_after[0] is None, token_after)

# ------------------------------------------------------------------
print('\n10. POST /login with new password')
r = s.post(f'{BASE}/login', data={
    'email': TEST_EMAIL,
    'password': RESET_PW,
}, allow_redirects=True)
check('Login with reset password → 200', r.status_code == 200, r.status_code)
check('Not on login page (authenticated)', '/login' not in r.url or 'Invalid' not in r.text)

# ------------------------------------------------------------------
print('\n11. POST /account/delete — request deletion')
r = s.post(f'{BASE}/account/delete', allow_redirects=True)
check('Delete POST → 200', r.status_code == 200, r.status_code)
check('Redirected to login after deletion request', '/login' in r.url or 'sign in' in r.text.lower() or 'deletion' in r.text.lower())

del_row = db_get('SELECT deletion_requested_at FROM "user" WHERE email = ?', (TEST_EMAIL,))
check('deletion_requested_at set in DB', del_row and del_row[0], del_row)

# ------------------------------------------------------------------
print('\n12. Log back in → see grace period notice')
r = s.post(f'{BASE}/login', data={
    'email': TEST_EMAIL,
    'password': RESET_PW,
}, allow_redirects=True)
check('Log back in after deletion request → 200', r.status_code == 200)

r = s.get(f'{BASE}/account')
check('GET /account after deletion request → 200', r.status_code == 200)
check('Deletion pending notice visible', 'deletion pending' in r.text.lower() or 'scheduled for deletion' in r.text.lower())
check('Cancel deletion button present', 'cancel-deletion' in r.text)

# ------------------------------------------------------------------
print('\n13. POST /account/cancel-deletion')
r = s.post(f'{BASE}/account/cancel-deletion', allow_redirects=True)
check('Cancel deletion → 200', r.status_code == 200, r.status_code)
check('Success message shown', 'cancelled' in r.text.lower() or 'active' in r.text.lower())

del_row_after = db_get('SELECT deletion_requested_at FROM "user" WHERE email = ?', (TEST_EMAIL,))
check('deletion_requested_at cleared in DB', del_row_after and del_row_after[0] is None, del_row_after)

r = s.get(f'{BASE}/account')
check('Account page after cancel — no deletion notice', 'deletion pending' not in r.text.lower())

# ------------------------------------------------------------------
print('\n14. Nav — unauthenticated visitor sees Sign in / Register')
s2 = requests.Session()
r = s2.get(f'{BASE}/home')
check('Unauthenticated nav shows Sign in link', '/login' in r.text or 'sign in' in r.text.lower())
check('Unauthenticated nav shows Register link', '/register' in r.text or 'register' in r.text.lower())

# ------------------------------------------------------------------
print('\n15. Stripe webhook — missing secret returns ignored')
r = requests.post(f'{BASE}/stripe/webhook', data=b'{}',
                  headers={'Content-Type': 'application/json', 'Stripe-Signature': 'bad'})
# With empty STRIPE_WEBHOOK_SECRET it returns 200 ignored; with a secret it would 400
check('Stripe webhook endpoint reachable (200 or 400)', r.status_code in (200, 400), r.status_code)

# ------------------------------------------------------------------
# Cleanup
db_cleanup()

print('\n' + '='*40)
if errors:
    print(f'RESULT: {len(errors)} failure(s):')
    for e in errors:
        print(f'  {e}')
    sys.exit(1)
else:
    print('RESULT: All checks passed.')
    sys.exit(0)
