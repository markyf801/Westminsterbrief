"""
Smoke tests for key routes and a reserved-kwarg lint check.

Catches:
- Template rendering errors (base.html regressions, missing variables)
- Reserved Flask/Jinja2 kwarg collisions in render_template() calls
- HTTP 200 on key GET routes

Run: python -m pytest tests/test_routes.py -v
"""

import re
import pytest
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]

# ── Reserved-kwarg lint ───────────────────────────────────────────────────────

# Flask injects these into every Jinja2 context. Passing them as render_template
# kwargs silently shadows the global and causes hard-to-diagnose failures.
# See CLAUDE.md "Flask template safety" section.
_RESERVED = {
    'session', 'request', 'g', 'config',
    'url_for', 'get_flashed_messages', 'current_user',
}


def test_no_reserved_render_template_kwargs():
    """render_template() calls must not pass reserved Flask/Jinja2 global names."""
    violations = []
    skip_dirs = {'.venv', '__pycache__', 'migrations', '.claude', 'node_modules'}

    for py_file in PROJECT_ROOT.rglob('*.py'):
        if any(p in py_file.parts for p in skip_dirs):
            continue
        source = py_file.read_text(encoding='utf-8', errors='ignore')
        if 'render_template' not in source:
            continue

        lines = source.splitlines()
        in_call = False
        depth = 0

        for lineno, line in enumerate(lines, 1):
            if not in_call and 'render_template(' in line:
                in_call = True

            if in_call:
                depth += line.count('(') - line.count(')')
                for kwarg in _RESERVED:
                    if re.search(rf'\b{kwarg}\s*=', line):
                        rel = py_file.relative_to(PROJECT_ROOT)
                        violations.append(f"{rel}:{lineno}: kwarg '{kwarg}'")
                if depth <= 0:
                    in_call = False
                    depth = 0

    assert not violations, (
        "Reserved render_template kwargs found — these shadow Flask globals:\n"
        + "\n".join(violations)
    )


# ── Shared test app ───────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def app():
    """Minimal Flask app: SQLite in-memory, all blueprints, seeded test rows."""
    from flask import Flask
    from flask_login import LoginManager
    from extensions import db as _db

    # Import all models so db.create_all() registers their tables
    import hansard_archive.models  # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    from hansard_archive.models import HansardSession, HaPQ

    # Import blueprints
    from hansard import hansard_bp
    from biography import biography_bp
    from tracker import tracker_bp
    from debate_scanner import debate_scanner_bp
    from mp_search import mp_search_bp
    from stakeholder_directory.views import directory_bp
    from hansard_archive.views import archive_bp
    from hansard_archive.slugs import slugify_theme

    test_app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))
    test_app.config.update({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'TESTING': True,
        'SECRET_KEY': 'test-secret',
        'LOGIN_DISABLED': True,
        'WTF_CSRF_ENABLED': False,
    })

    _db.init_app(test_app)

    lm = LoginManager()
    lm.init_app(test_app)

    @lm.user_loader
    def load_user(uid):
        return None

    # Context processors that base.html depends on
    @test_app.context_processor
    def inject_globals():
        return {'admin_authenticated': False, 'app_version': 'test'}

    # Jinja2 filter used in archive templates
    test_app.jinja_env.filters['slugify'] = slugify_theme

    # Jinja2 global used in base.html
    from feature_flags import feature_enabled
    test_app.jinja_env.globals['feature_enabled'] = feature_enabled

    for bp in (hansard_bp, biography_bp, tracker_bp, debate_scanner_bp,
               mp_search_bp, directory_bp, archive_bp):
        test_app.register_blueprint(bp)

    with test_app.app_context():
        _db.create_all()

        # Seed a HansardSession for /archive/debate/ route
        s = HansardSession(
            ext_id='test-ext-001',
            title='Test Debate Session',
            date=date(2026, 1, 1),
            house='Commons',
            debate_type='debate',
            slug='test-session',
            contributions_ingested=True,
            is_container=False,
        )
        _db.session.add(s)

        # Seed a HaPQ for /archive/pq/ route
        pq = HaPQ(
            uin='TEST001',
            question_text='What is the government doing about test issues?',
            tabled_date=date(2026, 1, 1),
            asking_member='Test MP',
            asking_mnis_id=99999,
            answering_body='Department for Testing',
            answering_body_id=999,
            is_answered=False,
        )
        _db.session.add(pq)
        _db.session.commit()

    yield test_app

    with test_app.app_context():
        _db.drop_all()


@pytest.fixture(scope='module')
def client(app):
    return app.test_client()


# ── Tool form routes (GET, no data needed) ────────────────────────────────────

class TestToolRoutes:
    def test_questions_get(self, client):
        r = client.get('/questions')
        assert r.status_code == 200

    def test_tracker_get(self, client):
        r = client.get('/tracker')
        assert r.status_code == 200

    def test_mp_search_get(self, client):
        r = client.get('/mp_search')
        assert r.status_code == 200

    def test_biography_get(self, client):
        r = client.get('/biography')
        assert r.status_code == 200

    def test_debates_get(self, client):
        r = client.get('/debates')
        assert r.status_code == 200


# ── Archive routes ────────────────────────────────────────────────────────────

class TestArchiveRoutes:
    def test_archive_home(self, client):
        r = client.get('/archive')
        assert r.status_code == 200

    def test_archive_search_empty(self, client):
        r = client.get('/archive/search?q=test')
        assert r.status_code == 200

    def test_archive_pq_detail(self, client):
        """Seeded UIN TEST001 must render without error."""
        r = client.get('/archive/pq/TEST001')
        assert r.status_code == 200

    def test_archive_debate_detail(self, client):
        """Seeded session slug 'test-session' on 1 Jan 2026 must render without error."""
        r = client.get('/archive/debate/1-january-2026/test-session')
        assert r.status_code == 200

    def test_archive_pq_detail_missing_returns_404(self, client):
        r = client.get('/archive/pq/NOTEXIST')
        assert r.status_code == 404

    def test_archive_debate_detail_missing_returns_404(self, client):
        r = client.get('/archive/debate/1-january-2026/no-such-session')
        assert r.status_code == 404
