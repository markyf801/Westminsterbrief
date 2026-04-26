"""
Integration tests for the stakeholder directory views blueprint.

Run: python -m pytest stakeholder_directory/tests/test_views.py -v
"""
import pytest
from pathlib import Path
from datetime import date
from flask import Flask
from flask_login import LoginManager

_PROJECT_ROOT = Path(__file__).parents[2]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def view_app():
    """Minimal Flask app with directory blueprint and LOGIN_DISABLED=True."""
    from extensions import db as _db
    import stakeholder_directory.models       # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    from stakeholder_directory.views import directory_bp

    _app = Flask(__name__, template_folder=str(_PROJECT_ROOT / 'templates'))
    _app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _app.config['TESTING'] = True
    _app.config['SECRET_KEY'] = 'test-secret'
    _app.config['LOGIN_DISABLED'] = True

    _db.init_app(_app)
    login_mgr = LoginManager()
    login_mgr.init_app(_app)

    @login_mgr.user_loader
    def load_user(user_id):
        return None

    _app.register_blueprint(directory_bp)

    with _app.app_context():
        _db.create_all()
        _seed_data(_db)
        yield _app
        _db.drop_all()


@pytest.fixture
def view_app_auth_required():
    """Same app but with login_required enforced (LOGIN_DISABLED omitted)."""
    from extensions import db as _db
    import stakeholder_directory.models       # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401
    from stakeholder_directory.views import directory_bp

    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _app.config['TESTING'] = True
    _app.config['SECRET_KEY'] = 'test-secret'

    _db.init_app(_app)
    login_mgr = LoginManager()
    login_mgr.login_view = 'login'
    login_mgr.init_app(_app)

    @login_mgr.user_loader
    def load_user(user_id):
        return None

    @_app.route('/login')
    def login():
        return 'login', 200

    _app.register_blueprint(directory_bp)

    with _app.app_context():
        _db.create_all()
        yield _app
        _db.drop_all()


def _seed_data(db):
    from stakeholder_directory.models import Organisation, Engagement, Alias

    org = Organisation(
        canonical_name='Universities UK',
        type='trade_association',
        scope='national',
        status='active',
    )
    db.session.add(org)
    db.session.flush()

    db.session.add(Alias(organisation_id=org.id, alias_name='UUK', source='test'))
    db.session.add(Engagement(
        organisation_id=org.id,
        source_type='ministerial_meeting',
        engagement_date=date(2025, 3, 15),
        department='department_for_education',
        source_url='https://www.gov.uk/test',
        ingester_source='test',
    ))
    db.session.add(Engagement(
        organisation_id=org.id,
        source_type='oral_evidence_committee',
        engagement_date=date(2024, 11, 20),
        committee_name='Education Committee',
        source_url='https://committees.parliament.uk/oralevidence/1001/html/',
        ingester_source='test',
        engagement_subject='Higher education quality and standards inquiry',
        inquiry_id='9309',
        inquiry_status='open',
    ))
    db.session.add(Engagement(
        organisation_id=org.id,
        source_type='ministerial_meeting',
        engagement_date=date(2025, 1, 10),
        department='department_for_education',
        source_url='https://www.gov.uk/test2',
        ingester_source='test',
        engagement_subject=None,  # blank purpose
    ))
    db.session.add(Engagement(
        organisation_id=org.id,
        source_type='oral_evidence_committee',
        engagement_date=date(2024, 6, 10),
        committee_name='Education Committee',
        source_url='https://committees.parliament.uk/oralevidence/1002/html/',
        ingester_source='test',
        engagement_subject='Teacher recruitment inquiry',
        inquiry_id='7357',
        inquiry_status=None,  # status not yet fetched
    ))
    db.session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDirectoryIndex:
    def test_landing_page_returns_200(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory')
            assert resp.status_code == 200

    def test_landing_page_shows_org_count(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory')
            assert b'1' in resp.data  # 1 org seeded

    def test_anonymous_access_redirects(self, view_app_auth_required):
        with view_app_auth_required.test_client() as client:
            resp = client.get('/directory')
            assert resp.status_code == 302


class TestDirectorySearch:
    def test_search_universities_returns_result(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory/search?q=universities')
            assert resp.status_code == 200
            assert b'Universities UK' in resp.data

    def test_search_by_alias_returns_result(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory/search?q=UUK')
            assert resp.status_code == 200
            assert b'Universities UK' in resp.data

    def test_short_query_shows_error(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory/search?q=ab')
            assert resp.status_code == 200
            assert b'3 characters' in resp.data

    def test_no_results_shows_empty_state(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory/search?q=zxqwertyuiop')
            assert resp.status_code == 200
            assert b'No organisations found' in resp.data or b'Did you mean' in resp.data


class TestOrganisationDetail:
    def test_org_detail_returns_200(self, view_app):
        from extensions import db
        with view_app.app_context():
            from stakeholder_directory.models import Organisation
            org = db.session.query(Organisation).first()
            org_id = org.id
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            assert resp.status_code == 200
            assert b'Universities UK' in resp.data

    def test_org_detail_shows_engagement_count(self, view_app):
        from extensions import db
        with view_app.app_context():
            from stakeholder_directory.models import Organisation
            org = db.session.query(Organisation).first()
            org_id = org.id
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            assert b'2' in resp.data  # 2 engagements seeded

    def test_missing_org_returns_404(self, view_app):
        with view_app.test_client() as client:
            resp = client.get('/directory/org/999999')
            assert resp.status_code == 404

    def test_engagement_subject_rendered_when_present(self, view_app):
        from extensions import db
        with view_app.app_context():
            from stakeholder_directory.models import Organisation
            org = db.session.query(Organisation).first()
            org_id = org.id
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            assert b'Higher education quality and standards inquiry' in resp.data

    def test_blank_engagement_subject_shows_no_separator(self, view_app):
        from extensions import db
        with view_app.app_context():
            from stakeholder_directory.models import Organisation
            org = db.session.query(Organisation).first()
            org_id = org.id
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            html = resp.data.decode()
            # Only the 2 committee engagements have subjects; ministerial meetings without
            # a subject should not render the dir-engagement-subject element at all
            assert html.count('dir-engagement-subject') == 2


class TestInquiryStatus:
    def _get_org_id(self, app):
        from extensions import db
        with app.app_context():
            from stakeholder_directory.models import Organisation
            return db.session.query(Organisation).first().id

    def test_open_status_badge_renders(self, view_app):
        org_id = self._get_org_id(view_app)
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            assert b'badge-inquiry-open' in resp.data

    def test_null_status_no_badge(self, view_app):
        org_id = self._get_org_id(view_app)
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            html = resp.data.decode()
            # Only one badge (for inquiry_status='open'); the null-status engagement has none
            assert html.count('badge-inquiry-') == 1

    def test_inquiry_title_is_hyperlink(self, view_app):
        org_id = self._get_org_id(view_app)
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            html = resp.data.decode()
            assert 'committees.parliament.uk/work/9309/' in html
            assert 'Higher education quality and standards inquiry' in html

    def test_ministerial_subject_not_hyperlink(self, view_app):
        # The two committee evidence engagements in seed data have inquiry_id and get links.
        # The two ministerial meetings have no inquiry_id and must NOT generate extra links.
        org_id = self._get_org_id(view_app)
        with view_app.test_client() as client:
            resp = client.get(f'/directory/org/{org_id}')
            html = resp.data.decode()
            # Exactly 2 committee.parliament.uk/work/ links (one per seeded inquiry_id)
            assert html.count('committees.parliament.uk/work/') == 2
