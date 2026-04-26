import os
import ipaddress
import socket
import requests
import re
import numpy as np
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import text
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from extensions import db
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

# Force load environment variables so the API keys never get missed
load_dotenv()

# External APIs
from newsapi import NewsApiClient
from google import genai
from atproto import Client as BskyClient

_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in [
        '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
        '127.0.0.0/8', '169.254.0.0/16', '::1/128', 'fc00::/7',
    ]
]

def _validate_external_url(url: str) -> str:
    """Raise ValueError if url is not a safe external http/https URL.
    Blocks SSRF by rejecting private/loopback IP ranges."""
    if not url or len(url) > 500:
        raise ValueError('URL missing or too long')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f'URL scheme must be http/https, got {parsed.scheme!r}')
    if not parsed.hostname:
        raise ValueError('URL has no hostname')
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        if any(ip in net for net in _PRIVATE_NETS):
            raise ValueError(f'URL resolves to private IP {ip}')
    except socket.gaierror:
        raise ValueError(f'Cannot resolve hostname {parsed.hostname!r}')
    return url


# Import existing blueprints
from hansard import hansard_bp
from biography import biography_bp
from tracker import tracker_bp
from debate_scanner import debate_scanner_bp
from mp_search import mp_search_bp
from stakeholder_directory.views import directory_bp

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)

# Build version string: short git hash (Railway sets RAILWAY_GIT_COMMIT_SHA; fallback to subprocess locally)
def _get_app_version():
    sha = os.environ.get('RAILWAY_GIT_COMMIT_SHA', '')
    if sha:
        return sha[:7]
    try:
        import subprocess
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                       cwd=basedir, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return 'dev'

APP_VERSION = _get_app_version()

# ==========================================
# 1. CONFIGURATION
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-key-change-this-later')
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'intelligence.db'))
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# ==========================================
# 2. LOGIN MANAGER
# ==========================================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' 

# ==========================================
# 3. DATABASE MODELS
# ==========================================
# Access control — all configurable via env vars, no hardcoding needed.
# PAYWALL_ENABLED=false  → bypass all tier checks (useful in dev)
# APPROVED_DOMAINS=gov.uk,parliament.uk  → comma-separated; default gov.uk
# APPROVED_EMAILS=you@gmail.com,colleague@gmail.com  → individual overrides
_PAYWALL_ENABLED = os.environ.get('PAYWALL_ENABLED', 'true').lower() != 'false'
_APPROVED_DOMAINS = [d.strip().lower() for d in os.environ.get('APPROVED_DOMAINS', 'gov.uk').split(',') if d.strip()]
_APPROVED_EMAILS  = {e.strip().lower() for e in os.environ.get('APPROVED_EMAILS', '').split(',') if e.strip()}

def _is_approved_email(email: str) -> bool:
    e = email.lower().strip()
    if e in _APPROVED_EMAILS:
        return True
    return any(e.endswith('.' + d) or e.endswith('@' + d) for d in _APPROVED_DOMAINS)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    has_completed_onboarding = db.Column(db.Boolean, default=False, nullable=False)
    access_tier = db.Column(db.String(20), nullable=False, default='restricted')
    topics = db.relationship('TrackedTopic', backref='owner', lazy=True)
    stakeholders = db.relationship('TrackedStakeholder', backref='owner', lazy=True)
    preference = db.relationship('UserPreference', backref='user', uselist=False, lazy=True)

class TrackedTopic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(255), nullable=False)
    department = db.Column(db.String(100), nullable=False) 
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    alerts = db.relationship('Alert', backref='topic', lazy=True)

class TrackedStakeholder(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    bsky_handle = db.Column(db.String(100))
    website     = db.Column(db.String(300))
    rss_url     = db.Column(db.String(500))
    description = db.Column(db.Text)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    alerts      = db.relationship('Alert', backref='stakeholder', lazy=True)

class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('tracked_topic.id'), nullable=True)
    stakeholder_id = db.Column(db.Integer, db.ForeignKey('tracked_stakeholder.id'), nullable=True)
    source = db.Column(db.String(50), default='Hansard') 
    title = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(255), nullable=False)
    snippet = db.Column(db.Text, nullable=False)
    speaker = db.Column(db.String(100), nullable=True) 
    date_found = db.Column(db.DateTime, default=datetime.utcnow)

class UserPreference(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    department  = db.Column(db.String(100), default='')
    policy_area = db.Column(db.String(100), default='')
    subject     = db.Column(db.String(100), default='')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Import cache models so their tables are created
from cache_models import CachedTranscript, CachedQuestion, CachedMember, CachedTWFYSearch, MemberLink, StakeholderOrg
# Register stakeholder directory models with SQLAlchemy metadata before db.create_all()
import stakeholder_directory.models          # noqa: F401
import stakeholder_directory.ingesters.staging  # noqa: F401

# ==========================================
# 4. AUTO-BUILD DATABASE
# ==========================================
with app.app_context():
    db.create_all()
    # Add has_completed_onboarding to existing user tables that predate this column
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE "user" ADD COLUMN has_completed_onboarding BOOLEAN DEFAULT FALSE NOT NULL'))
            conn.commit()
    except Exception:
        pass  # Column already exists — nothing to do
    # Rename cached_twfy_search.query → search_query (query shadows SQLAlchemy's .query interface)
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE cached_twfy_search RENAME COLUMN "query" TO search_query'))
            conn.commit()
    except Exception:
        pass  # Already renamed or table doesn't exist yet
    # Add access_tier to user table; backfill gov.uk + owner email
    from sqlalchemy import inspect as _sa_inspect
    _user_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('user')}
    if 'access_tier' not in _user_cols:
        try:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN access_tier VARCHAR(20) NOT NULL DEFAULT 'restricted'"))
                conn.execute(text("UPDATE \"user\" SET access_tier = 'civil_servant' WHERE email LIKE '%.gov.uk' OR email = 'markjforde@gmail.com'"))
                conn.commit()
        except Exception as _e:
            app.logger.warning('user access_tier migration failed: %s', _e)
    # Add new columns to tracked_stakeholder (website, rss_url, description)
    _ts_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('tracked_stakeholder')}
    for _col, _defn in [('website', 'VARCHAR(300)'), ('rss_url', 'VARCHAR(500)'), ('description', 'TEXT')]:
        if _col not in _ts_cols:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE tracked_stakeholder ADD COLUMN {_col} {_defn}'))
                    conn.commit()
            except Exception as _e:
                app.logger.warning('tracked_stakeholder migration failed for %s: %s', _col, _e)
    # Widen raw_organisation_name to TEXT in staging tables (was VARCHAR(300), too short for multi-org rows)
    for _tbl in ('sd_staging_ministerial_meeting', 'sd_staging_committee_evidence'):
        try:
            with db.engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE {_tbl} ALTER COLUMN raw_organisation_name TYPE TEXT'))
                conn.commit()
        except Exception:
            pass  # Table doesn't exist yet or already TEXT
    # Add columns to sd_engagement added after initial Railway deployment
    _sd_eng_new_cols = [
        ('committee_id',       'INTEGER'),
        ('committee_name',     'VARCHAR(200)'),
        ('cited_in_outcome',   'BOOLEAN NOT NULL DEFAULT FALSE'),
        ('engagement_depth',   'VARCHAR(50)'),
        ('engagement_subject', 'TEXT'),
        ('inquiry_status',     'VARCHAR(20)'),
        ('ingested_at',        'TIMESTAMP DEFAULT NOW()'),
        ('ingester_source',    "VARCHAR(100) DEFAULT 'unknown'"),
    ]
    try:
        _eng_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('sd_engagement')}
        with db.engine.connect() as conn:
            for _col, _defn in _sd_eng_new_cols:
                if _col not in _eng_cols:
                    conn.execute(text(f'ALTER TABLE sd_engagement ADD COLUMN {_col} {_defn}'))
            conn.commit()
    except Exception as _e:
        app.logger.warning('sd_engagement migration failed: %s', _e)
    # Seed known hard-to-resolve ministers into MemberLink
    # These are peers whose TWFY getLords name search fails (newer Life Peers)
    # parliament_id and twfy_person_id verified from direct Hansard debate records
    _SEEDS = [
        {'parliament_id': 269, 'display_name': 'Baroness Smith of Malvern',
         'house': 'Lords', 'twfy_person_id': '10549',
         'twfy_name': 'Baroness Smith of Malvern', 'resolution_method': 'seeded'},
        {'parliament_id': 5033, 'display_name': 'Josh MacAlister',
         'house': 'Commons', 'twfy_person_id': '26321',
         'twfy_name': 'Josh MacAlister', 'resolution_method': 'seeded'},
    ]
    for s in _SEEDS:
        if not MemberLink.get_by_parliament_id(s['parliament_id']):
            MemberLink.upsert(**s)
    if not User.query.filter_by(email='joe@university.ac.uk').first():
        joe_pass = generate_password_hash('password123', method='pbkdf2:sha256')
        joe = User(email='joe@university.ac.uk', password_hash=joe_pass)
        db.session.add(joe)
        db.session.commit()

    # Seed education stakeholder orgs (run once — skipped if any orgs already exist)
    if StakeholderOrg.query.count() == 0:
        _STAKEHOLDER_SEEDS = [
            # Central Government & Regulators
            ('Department for Education (DfE)', 'DfE', 'Regulator / Government', 'gov.uk/dfe',
             'The central government department responsible for education and children\'s services in England.', None, None),
            ('Ofsted', None, 'Regulator / Government', 'gov.uk/ofsted',
             'Inspects and regulates schools, colleges, and childcare services to ensure quality standards.', None, None),
            ('Office for Students (OfS)', 'OfS', 'Regulator / Government', 'officeforstudents.org.uk',
             'The independent regulator for higher education in England.', None, 'https://www.officeforstudents.org.uk/news-blog-and-events/press-and-media/rss/'),
            # Trade Unions — Teaching
            ('National Education Union (NEU)', 'NEU', 'Trade Union', 'neu.org.uk',
             'The largest UK education union, representing teachers, lecturers, and support staff.', None, None),
            ('NASUWT', None, 'Trade Union', 'nasuwt.org.uk',
             'A major union representing teachers and headteachers across all phases of education.', None, None),
            ('UCU', None, 'Trade Union', 'ucu.org.uk',
             'The University and College Union; represents academics, lecturers, and professional staff in HE and FE.', None, 'https://www.ucu.org.uk/rss'),
            ('UNISON Education', 'UNISON', 'Trade Union', 'unison.org.uk',
             'The largest union for school support staff, including TAs, cleaners, and admin staff.', None, None),
            ('GMB Schools', 'GMB', 'Trade Union', 'gmb.org.uk',
             'Represents a wide range of school support staff, focusing on fair pay and safe staffing levels.', None, None),
            ('Unite Education', 'Unite', 'Trade Union', 'unitetheunion.org',
             'Represents technical, scientific, and estates staff within schools and universities.', None, None),
            ('Voice (Community)', None, 'Trade Union', 'voicetheunion.org.uk',
             'The education section of the Community union, representing teachers and early years workers.', None, None),
            # Student Representation
            ('National Union of Students (NUS)', 'NUS', 'Student Representation', 'nus.org.uk',
             'The collective voice of students in higher and further education.', None, None),
            # School Leadership & Governance
            ('Association of School and College Leaders (ASCL)', 'ASCL', 'School Leadership', 'ascl.org.uk',
             'A union and professional body for secondary leaders.', None, 'https://www.ascl.org.uk/rss.xml'),
            ('National Association of Head Teachers (NAHT)', 'NAHT', 'School Leadership', 'naht.org.uk',
             'Represents leaders in primary and special schools.', None, None),
            ('Confederation of School Trusts (CST)', 'CST', 'School Governance', 'cstuk.org.uk',
             'The national sector body and representative organization for school trusts (academies).', None, None),
            ('National Governance Association (NGA)', 'NGA', 'School Governance', 'nga.org.uk',
             'The national membership body for governors and trustees in state schools.', None, None),
            # HE Mission Groups
            ('Universities UK (UUK)', 'UUK', 'HE Sector Body', 'universitiesuk.ac.uk',
             'The collective voice of 140+ UK universities; leads sector advocacy on policy and funding.', None, 'https://www.universitiesuk.ac.uk/rss'),
            ('Russell Group', None, 'HE Sector Body', 'russellgroup.ac.uk',
             'Represents 24 research-intensive UK universities committed to world-class research and teaching.', None, None),
            ('University Alliance', None, 'HE Sector Body', 'unialliance.ac.uk',
             'Represents professional and technical universities focused on industry partnerships and skills.', None, None),
            ('MillionPlus', None, 'HE Sector Body', 'millionplus.ac.uk',
             'Advocates for the role of modern universities in social mobility and regional economies.', None, None),
            ('GuildHE', None, 'HE Sector Body', 'guildhe.ac.uk',
             'Represents small and specialist universities, including arts and vocational providers.', None, None),
            # FE & Skills
            ('Association of Colleges (AoC)', 'AoC', 'FE & Skills', 'aoc.co.uk',
             'The national representative body for further education, sixth form, and specialist colleges.', None, 'https://www.aoc.co.uk/news.rss'),
            # Regional & Independent
            ('London Higher', None, 'Regional Body', 'londonhigher.ac.uk',
             'The membership body for over 40 London-based higher education institutions.', None, None),
            ('Independent HE', None, 'Independent Sector', 'ihe.ac.uk',
             'The representative body for independent and specialist higher education providers.', None, None),
            # Mental Health & Wellbeing
            ('Student Minds', None, 'Wellbeing & Equality', 'studentminds.org.uk',
             'A charity focusing on student mental health; developers of the University Mental Health Charter.', None, None),
            ('Anna Freud Centre', None, 'Wellbeing & Equality', 'annafreud.org',
             'Provides research and training to schools to support children\'s mental health and wellbeing.', None, None),
            # Access & Equality
            ('TASO', None, 'Wellbeing & Equality', 'taso.org.uk',
             'An evidence hub researching effective methods to eliminate equality gaps in higher education.', None, None),
        ]
        for (name, short_name, category, website, description, bsky_handle, rss_url) in _STAKEHOLDER_SEEDS:
            db.session.add(StakeholderOrg(
                name=name, short_name=short_name, category=category,
                website=website, description=description,
                bsky_handle=bsky_handle, rss_url=rss_url,
                hansard_search_name=short_name or name,
            ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

# Kick off background minister link seeding after app context is established
from debate_scanner import seed_all_minister_links
seed_all_minister_links(app)

DEPARTMENTS_FOR_PREFS = [
    "All Departments", "Department for Education",
    "Department of Health and Social Care", "HM Treasury",
    "Home Office", "Ministry of Defence", "Ministry of Justice",
    "Department for Science, Innovation and Technology", "Cabinet Office",
]

# ==========================================
# 5. ROUTES
# ==========================================
@app.route('/')
@app.route('/home')
def home():
    return render_template('home.html')

@app.route('/health')
def health():
    import requests as _req
    from cache_models import CachedTWFYSearch
    checks = {}

    # DB
    try:
        db.session.execute(text('SELECT 1'))
        checks['database'] = 'ok'
    except Exception as e:
        checks['database'] = f'FAIL: {e}'

    # Cache table
    try:
        CachedTWFYSearch.query.count()
        checks['cache_table'] = 'ok'
    except Exception as e:
        checks['cache_table'] = f'FAIL: {e}'

    # TWFY API
    twfy_key = os.environ.get('TWFY_API_KEY')
    if not twfy_key:
        checks['twfy_api'] = 'FAIL: key not configured'
    else:
        try:
            r = _req.get('https://www.theyworkforyou.com/api/getDebates',
                         params={'key': twfy_key, 'search': 'test', 'num': 1, 'output': 'json'},
                         timeout=8)
            checks['twfy_api'] = 'ok' if r.status_code == 200 else f'FAIL: HTTP {r.status_code}'
        except Exception as e:
            checks['twfy_api'] = f'FAIL: {e}'

    # Gemini API
    gemini_key = os.environ.get('GEMINI_API_KEY')
    checks['gemini_api'] = 'ok' if gemini_key else 'FAIL: key not configured'

    # Git version
    checks['version'] = os.environ.get('RAILWAY_GIT_COMMIT_SHA', 'unknown')[:7]

    overall = 'ok' if all(v == 'ok' or k == 'version' for k, v in checks.items()) else 'degraded'
    checks['status'] = overall

    from flask import jsonify
    return jsonify(checks), 200 if overall == 'ok' else 503

@app.route('/robots.txt')
def robots():
    return app.send_static_file('robots.txt')

@app.route('/sitemap.xml')
def sitemap():
    return app.send_static_file('sitemap.xml')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

_PAYWALL_EXEMPT = {
    '/', '/home', '/login', '/register', '/logout',
    '/paywall', '/health', '/terms', '/privacy',
    '/robots.txt', '/sitemap.xml',
}

@app.before_request
def check_tier_access():
    if not _PAYWALL_ENABLED:
        return None
    if request.path.startswith('/static'):
        return None
    if request.path in _PAYWALL_EXEMPT:
        return None
    if not current_user.is_authenticated:
        return None  # @login_required on the route handles unauthenticated users
    if getattr(current_user, 'access_tier', 'civil_servant') == 'restricted':
        return redirect(url_for('paywall'))
    return None


@app.route('/paywall')
def paywall():
    return render_template('paywall.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            if not user.has_completed_onboarding:
                return redirect(url_for('onboarding'))
            return redirect(url_for('my_alerts'))
        flash('Invalid email or password')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')
        if not email or not password:
            flash('Email and password are required.')
        elif password != confirm:
            flash('Passwords do not match.')
        elif len(password) < 8:
            flash('Password must be at least 8 characters.')
        elif User.query.filter_by(email=email).first():
            flash('An account with that email already exists.')
        else:
            tier = 'civil_servant' if _is_approved_email(email) else 'restricted'
            user = User(email=email, password_hash=generate_password_hash(password, method='pbkdf2:sha256'), access_tier=tier)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            if tier == 'restricted':
                return redirect(url_for('paywall'))
            return redirect(url_for('onboarding'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/my_alerts')
@login_required
def my_alerts():
    user_topics = TrackedTopic.query.filter_by(user_id=current_user.id).all()
    user_stakeholders = TrackedStakeholder.query.filter_by(user_id=current_user.id).all()
    return render_template('my_alerts.html', topics=user_topics, stakeholders=user_stakeholders)

@app.route('/onboarding')
@login_required
def onboarding():
    return render_template('onboarding.html', departments=DEPARTMENTS_FOR_PREFS)

@app.route('/onboarding/save', methods=['POST'])
@login_required
def onboarding_save():
    dept   = request.form.get('department', '').strip()
    policy = request.form.get('policy_area', '').strip()
    subject = request.form.get('subject', '').strip()
    pref = UserPreference.query.filter_by(user_id=current_user.id).first()
    if pref is None:
        pref = UserPreference(user_id=current_user.id)
        db.session.add(pref)
    pref.department  = dept
    pref.policy_area = policy
    pref.subject     = subject
    current_user.has_completed_onboarding = True
    db.session.commit()
    flash('Preferences saved — your searches will now be pre-filled.')
    return redirect(url_for('my_alerts'))

@app.route('/onboarding/skip', methods=['POST'])
@login_required
def onboarding_skip():
    current_user.has_completed_onboarding = True
    db.session.commit()
    return redirect(url_for('my_alerts'))

@app.route('/my_preferences', methods=['GET', 'POST'])
@login_required
def my_preferences():
    pref = UserPreference.query.filter_by(user_id=current_user.id).first()
    if request.method == 'POST':
        dept   = request.form.get('department', '').strip()
        policy = request.form.get('policy_area', '').strip()
        subject = request.form.get('subject', '').strip()
        if pref is None:
            pref = UserPreference(user_id=current_user.id)
            db.session.add(pref)
        pref.department  = dept
        pref.policy_area = policy
        pref.subject     = subject
        db.session.commit()
        flash('Preferences updated.')
        return redirect(url_for('my_preferences'))
    return render_template('my_preferences.html', pref=pref, departments=DEPARTMENTS_FOR_PREFS)

@app.route('/add_topic', methods=['POST'])
@login_required
def add_topic():
    keyword = request.form.get('keyword')
    dept = request.form.get('department')
    if keyword:
        db.session.add(TrackedTopic(keyword=keyword, department=dept, user_id=current_user.id))
        db.session.commit()
    return redirect(url_for('my_alerts'))

@app.route('/remove_topic/<int:topic_id>', methods=['POST'])
@login_required
def remove_topic(topic_id):
    topic = TrackedTopic.query.filter_by(id=topic_id, user_id=current_user.id).first()
    if topic:
        Alert.query.filter_by(topic_id=topic.id).delete()
        db.session.delete(topic)
        db.session.commit()
    return redirect(url_for('my_alerts'))

MAX_STAKEHOLDERS_PER_USER = 50

@app.route('/add_stakeholder', methods=['POST'])
@login_required
def add_stakeholder():
    name = request.form.get('name', '').strip()
    if not name:
        return redirect(url_for('my_alerts'))
    if TrackedStakeholder.query.filter_by(user_id=current_user.id).count() >= MAX_STAKEHOLDERS_PER_USER:
        flash(f'Maximum {MAX_STAKEHOLDERS_PER_USER} stakeholders reached.')
        return redirect(url_for('my_alerts'))
    website = request.form.get('website', '').strip() or None
    rss_url = request.form.get('rss_url', '').strip() or None
    if website:
        try:
            _validate_external_url(website)
        except ValueError:
            website = None
    if rss_url:
        try:
            _validate_external_url(rss_url)
        except ValueError:
            rss_url = None
    db.session.add(TrackedStakeholder(
        name=name,
        bsky_handle=request.form.get('bsky_handle', '').strip() or None,
        website=website,
        rss_url=rss_url,
        description=request.form.get('description', '').strip() or None,
        user_id=current_user.id,
    ))
    db.session.commit()
    return redirect(url_for('my_alerts'))


@app.route('/delete_stakeholder/<int:sh_id>', methods=['POST'])
@login_required
def delete_stakeholder(sh_id):
    sh = TrackedStakeholder.query.get_or_404(sh_id)
    if sh.user_id == current_user.id:
        Alert.query.filter_by(stakeholder_id=sh.id).delete()
        db.session.delete(sh)
        db.session.commit()
    return redirect(url_for('my_alerts'))


# Helper for Semantic Scoring (Cosine Similarity)
def get_similarity(vec1, vec2):
    return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))

# ==========================================
# 6. SMART AI SCANNER (WITH NEW GEMINI MODEL)
# ==========================================
@app.route('/run_manual_scan', methods=['POST'])
@login_required
def run_manual_scan():
    TWFY_API_KEY = os.environ.get("TWFY_API_KEY")
    NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    BSKY_HANDLE = os.environ.get("BSKY_HANDLE")
    BSKY_PASSWORD = os.environ.get("BSKY_PASSWORD")
    
    newsapi = NewsApiClient(api_key=NEWS_API_KEY) if NEWS_API_KEY else None
    
    ai_client = None
    if GEMINI_API_KEY:
        try:
            ai_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            print(f"Failed to initialize Gemini: {e}")
    
    new_count = 0
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')

    user_topics = TrackedTopic.query.filter_by(user_id=current_user.id).all()
    
    for topic in user_topics:
        target_emb = None
        if ai_client:
            try:
                # 🎯 THE FIX: Upgraded to gemini-embedding-001
                target_emb = ai_client.models.embed_content(
                    model='gemini-embedding-001', 
                    contents=topic.keyword
                ).embeddings[0].values
            except Exception as e: 
                print(f"Gemini Target Embedding Error: {e}")
                
        if not target_emb:
            flash(f"⚠️ Warning: AI Filtering unavailable for '{topic.keyword}'. Check GEMINI_API_KEY.")

        broad_search = topic.department if topic.department and topic.department != "All Departments" else "Higher Education"

        # --- 1. MEDIA SCAN ---
        if newsapi:
            try:
                all_articles = newsapi.get_everything(q=broad_search, from_param=three_days_ago, language='en', sort_by='relevancy', page_size=50)
                for art in all_articles.get('articles', []):
                    if not Alert.query.filter_by(url=art['url']).first():
                        snippet = art['description'] if art['description'] else art['title']
                        is_relevant = False 
                        
                        if ai_client and target_emb and snippet:
                            try:
                                # 🎯 THE FIX: Upgraded to gemini-embedding-001
                                art_emb = ai_client.models.embed_content(model='gemini-embedding-001', contents=snippet).embeddings[0].values
                                score = get_similarity(target_emb, art_emb)
                                
                                if score >= 0.62:
                                    is_relevant = True
                            except: pass

                        if is_relevant:
                            db.session.add(Alert(topic_id=topic.id, source='Media', title=art['title'], url=art['url'], snippet=snippet[:200], speaker=art['source']['name']))
                            new_count += 1
            except Exception as e: print(f"News API Error: {e}")

        # --- 2. HANSARD SCAN ---
        if TWFY_API_KEY:
            try:
                resp = requests.get(f"https://www.theyworkforyou.com/api/getDebates?key={TWFY_API_KEY}&search={broad_search}&output=json&num=15", timeout=5)
                if resp.status_code == 200:
                    for row in resp.json().get('rows', []):
                        url = "https://www.theyworkforyou.com" + row.get('listurl', '')
                        if not Alert.query.filter_by(url=url).first():
                            snippet = re.sub('<[^>]+>', '', row.get('body', ''))[:300]
                            is_relevant = False
                            
                            if ai_client and target_emb and snippet:
                                try:
                                    # 🎯 THE FIX: Upgraded to gemini-embedding-001
                                    art_emb = ai_client.models.embed_content(model='gemini-embedding-001', contents=snippet).embeddings[0].values
                                    if get_similarity(target_emb, art_emb) >= 0.62:
                                        is_relevant = True
                                except: pass
                                
                            if is_relevant:
                                db.session.add(Alert(topic_id=topic.id, source='Hansard', title=row.get('parent', {}).get('body', 'Parliamentary Debate'), url=url, snippet=snippet, speaker=row.get('speaker', {}).get('name')))
                                new_count += 1
            except: pass

    # --- 3. BLUESKY SCAN ---
    if BSKY_HANDLE and BSKY_PASSWORD:
        try:
            bsky = BskyClient()
            bsky.login(BSKY_HANDLE, BSKY_PASSWORD)
            user_stakeholders = TrackedStakeholder.query.filter_by(user_id=current_user.id).all()
            for sh in user_stakeholders:
                if sh.bsky_handle:
                    handle = sh.bsky_handle.replace('@', '')
                    feed = bsky.get_author_feed(actor=handle, limit=5)
                    for feed_view in feed.feed:
                        post = feed_view.post
                        post_url = f"https://bsky.app/profile/{handle}/post/{post.uri.split('/')[-1]}"
                        if not Alert.query.filter_by(url=post_url).first():
                            db.session.add(Alert(stakeholder_id=sh.id, source='Bluesky', title=f"New Statement from {sh.name}", url=post_url, snippet=post.record.text[:250], speaker=handle))
                            new_count += 1
        except Exception as e: print(f"Bluesky Error: {e}")

    db.session.commit()
    flash(f"Smart AI Scan Complete! Discovered {new_count} highly relevant updates.")
    return redirect(url_for('my_alerts'))

# ==========================================
# 6b. ADMIN PAGE
# ==========================================
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '').strip()

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    token = request.args.get('token', '') or request.form.get('token', '')
    if not ADMIN_TOKEN:
        return render_template('admin_login.html', error="ADMIN_TOKEN is not set in environment variables.")
    if token != ADMIN_TOKEN:
        return render_template('admin_login.html', error="Invalid token." if token else None)

    from debate_scanner import MINISTER_CACHE_FILE
    import json, time

    message = None

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'clear_minister':
            try:
                if os.path.exists(MINISTER_CACHE_FILE):
                    os.remove(MINISTER_CACHE_FILE)
                message = 'Minister cache cleared — will refresh from GOV.UK on next search.'
            except Exception as e:
                message = f'Error clearing minister cache: {e}'
        elif action == 'clear_twfy_search':
            try:
                deleted = CachedTWFYSearch.query.delete()
                db.session.commit()
                message = f'TWFY search cache cleared ({deleted} entries removed).'
            except Exception as e:
                db.session.rollback()
                message = f'Error clearing TWFY search cache: {e}'
        elif action == 'clear_sessions':
            try:
                deleted = CachedTWFYSearch.query.filter(
                    CachedTWFYSearch.source_type.like('session_%')
                ).delete(synchronize_session=False)
                db.session.commit()
                message = f'Session expansion cache cleared ({deleted} entries removed).'
            except Exception as e:
                db.session.rollback()
                message = f'Error clearing session cache: {e}'
        elif action == 'clear_all':
            try:
                if os.path.exists(MINISTER_CACHE_FILE):
                    os.remove(MINISTER_CACHE_FILE)
                deleted = CachedTWFYSearch.query.delete()
                db.session.commit()
                message = f'All caches cleared ({deleted} TWFY entries removed, minister cache deleted).'
            except Exception as e:
                db.session.rollback()
                message = f'Error clearing caches: {e}'
        elif action == 'retry_failed_links':
            try:
                reset = MemberLink.query.filter_by(lookup_failed=True).all()
                for row in reset:
                    row.lookup_failed = False
                    row.resolution_method = None
                db.session.commit()
                message = f'{len(reset)} failed member link(s) reset — will retry on next search.'
            except Exception as e:
                db.session.rollback()
                message = f'Error resetting failed links: {e}'

        elif action == 'ingest_directory_csv':
            import tempfile
            csv_file = request.files.get('csv_file')
            department = request.form.get('department', '').strip()
            source_url = request.form.get('source_url', '').strip() or 'uploaded via admin panel'
            if not csv_file or not csv_file.filename:
                message = 'Error: no CSV file selected.'
            elif not department:
                message = 'Error: department is required.'
            else:
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as tmp:
                        tmp_path = tmp.name
                        csv_file.save(tmp_path)
                    from stakeholder_directory.ingesters.ministerial_meetings import ingest_ministerial_meetings
                    from stakeholder_directory.normalisation.normaliser import normalise_pending_staging
                    from stakeholder_directory.models import IngestionRun, Organisation, Engagement
                    import time as _time
                    orgs_before = Organisation.query.count()
                    eng_before = Engagement.query.count()
                    t0 = _time.monotonic()
                    ing = ingest_ministerial_meetings(tmp_path, department, source_url)
                    norm = normalise_pending_staging('staging_ministerial_meeting', batch_size=2000)
                    duration = int(_time.monotonic() - t0)
                    orgs_after = Organisation.query.count()
                    eng_after = Engagement.query.count()
                    log = IngestionRun(
                        run_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        script_invocation='admin panel upload',
                        source_files=[csv_file.filename],
                        department=department,
                        rows_ingested=ing.rows_processed,
                        rows_committed=norm.staging_records_processed,
                        organisations_created=orgs_after - orgs_before,
                        engagements_created=eng_after - eng_before,
                        errors=ing.errors + norm.errors or None,
                        duration_seconds=duration,
                    )
                    db.session.add(log)
                    db.session.commit()
                    message = (
                        f'Done in {duration}s. '
                        f'Rows processed: {ing.rows_processed} '
                        f'(staged {ing.rows_staged}, excluded {ing.rows_excluded}, '
                        f'nil-return skipped {ing.skipped_nil_return}). '
                        f'Normalised: {norm.staging_records_processed} staging rows — '
                        f'new orgs: {orgs_after - orgs_before}, '
                        f'new engagements: {eng_after - eng_before}. '
                        f'Errors: {len(ing.errors) + len(norm.errors)}.'
                    )
                except Exception as e:
                    db.session.rollback()
                    message = f'Ingestion error: {e}'
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)

    # --- Build cache status ---
    minister_status = {}
    try:
        if os.path.exists(MINISTER_CACHE_FILE):
            with open(MINISTER_CACHE_FILE) as f:
                mc = json.load(f)
            age_hours = (time.time() - mc.get('_ts', 0)) / 3600
            minister_count = sum(len(v) for v in mc.get('by_dept', {}).values())
            twfy_id_count = len(mc.get('twfy_ids', {}))
            minister_status = {
                'exists': True,
                'age_hours': round(age_hours, 1),
                'age_days': round(age_hours / 24, 1),
                'minister_count': minister_count,
                'twfy_id_count': twfy_id_count,
                'dept_count': len(mc.get('by_dept', {})),
            }
        else:
            minister_status = {'exists': False}
    except Exception:
        minister_status = {'exists': False, 'error': True}

    twfy_total = CachedTWFYSearch.query.count()
    twfy_session = CachedTWFYSearch.query.filter(
        CachedTWFYSearch.source_type.like('session_%')
    ).count()
    twfy_keyword = twfy_total - twfy_session

    member_link_stats = MemberLink.stats()

    # --- Directory stats ---
    dir_stats = {'org_count': 0, 'eng_count': 0, 'last_run': None, 'departments': {}, 'error': None}
    try:
        from stakeholder_directory.models import Organisation, Engagement, IngestionRun
        dir_stats['org_count'] = Organisation.query.count()
        dir_stats['eng_count'] = Engagement.query.count()
        dir_stats['last_run'] = IngestionRun.query.order_by(IngestionRun.run_at.desc()).first()
    except Exception as e:
        dir_stats['error'] = f'DB error: {e}'
    try:
        from stakeholder_directory.vocab import _load_yaml
        dept_yaml = _load_yaml('departments.yaml')
        dir_stats['departments'] = {k: v['name'] for k, v in dept_yaml.get('departments', {}).items()}
    except Exception as e:
        dir_stats['error'] = (dir_stats.get('error') or '') + f' | YAML error: {e}'

    return render_template('admin.html',
                           message=message,
                           minister_status=minister_status,
                           twfy_total=twfy_total,
                           twfy_session=twfy_session,
                           twfy_keyword=twfy_keyword,
                           member_link_stats=member_link_stats,
                           dir_stats=dir_stats,
                           admin_token=token)


# ==========================================
# 7. BLUEPRINTS
# ==========================================
app.register_blueprint(hansard_bp)
app.register_blueprint(biography_bp)
app.register_blueprint(tracker_bp)
app.register_blueprint(debate_scanner_bp)
app.register_blueprint(mp_search_bp)
app.register_blueprint(directory_bp)

@app.context_processor
def inject_version():
    return {'app_version': APP_VERSION}

if __name__ == '__main__':
    app.run(debug=True)