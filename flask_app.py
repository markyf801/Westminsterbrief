import os
import ipaddress
import secrets
import socket
import requests
import re
import numpy as np
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import text
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from extensions import db, limiter
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv
from email_service import send_template_email, send_email
from feature_flags import feature_enabled

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
from hansard_archive.views import archive_bp
from tracker import tracker_bp
from debate_scanner import debate_scanner_bp
from mp_search import mp_search_bp
from stakeholder_directory.views import directory_bp

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
# Trust one proxy hop (Cloudflare → Railway) so rate limiting reads real client IPs
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'intelligence.db'))
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# Rate limiter — init with global defaults; stricter limits on LLM/auth endpoints
# Set RATELIMIT_ENABLED=false in .env to disable locally without affecting production
if os.environ.get('RATELIMIT_ENABLED', 'true').lower() == 'false':
    app.config['RATELIMIT_ENABLED'] = False
limiter.init_app(app)
limiter.default_limits = ["200 per hour", "30 per minute"]

# Feature flag helper available in all Jinja2 templates as feature_enabled(...)
app.jinja_env.globals['feature_enabled'] = feature_enabled

# Security headers
from flask_talisman import Talisman
Talisman(
    app,
    force_https=False,
    strict_transport_security=True,
    strict_transport_security_max_age=31536000,
    content_security_policy={
        'default-src': "'self'",
        'script-src': [
            "'self'",
            "'unsafe-inline'",
            "https://cdn.jsdelivr.net",
            "https://code.jquery.com",
        ],
        'style-src': [
            "'self'",
            "'unsafe-inline'",
            "https://fonts.googleapis.com",
            "https://cdn.jsdelivr.net",
        ],
        'font-src': [
            "'self'",
            "https://fonts.gstatic.com",
        ],
        'img-src': [
            "'self'",
            "data:",
            "https:",
        ],
        'connect-src': "'self'",
    },
    referrer_policy='strict-origin-when-cross-origin',
    frame_options='DENY',
)

# ==========================================
# 2. LOGIN MANAGER
# ==========================================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' 

# ==========================================
# 3. DATABASE MODELS
# ==========================================
# Access control — approved domains/emails determine public_sector tier at registration
# APPROVED_DOMAINS=gov.uk,parliament.uk  → comma-separated; default gov.uk
# APPROVED_EMAILS=you@gmail.com  → individual overrides
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
    access_tier = db.Column(db.String(20), nullable=False, default='standard')
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    deletion_requested_at = db.Column(db.DateTime, nullable=True)
    stripe_customer_id = db.Column(db.String(64), nullable=True, index=True)
    stripe_subscription_id = db.Column(db.String(64), nullable=True)
    sector = db.Column(db.String(50), nullable=True)
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
# Register Hansard Archive models (Phase 2A)
import hansard_archive.models                # noqa: F401

# ==========================================
# 4. AUTO-BUILD DATABASE
# ==========================================
import time as _mig_time
_mig_t0 = _mig_time.monotonic()
def _mig_log(phase):
    print(f'[STARTUP] {phase} +{_mig_time.monotonic() - _mig_t0:.1f}s', flush=True)

_mig_log('begin')
with app.app_context():
    _mig_log('app_context entered')
    db.create_all()
    _mig_log('db.create_all done')
    # Add has_completed_onboarding to existing user tables that predate this column
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE "user" ADD COLUMN has_completed_onboarding BOOLEAN DEFAULT FALSE NOT NULL'))
            conn.commit()
    except Exception:
        pass  # Column already exists — nothing to do
    _mig_log('onboarding col done')
    # Rename cached_twfy_search.query → search_query (query shadows SQLAlchemy's .query interface)
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE cached_twfy_search RENAME COLUMN "query" TO search_query'))
            conn.commit()
    except Exception:
        pass  # Already renamed or table doesn't exist yet
    _mig_log('twfy rename done')
    # Add access_tier to user table; backfill gov.uk + owner email
    from sqlalchemy import inspect as _sa_inspect
    _user_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('user')}
    _mig_log('inspect user done')
    if 'access_tier' not in _user_cols:
        try:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN access_tier VARCHAR(20) NOT NULL DEFAULT 'standard'"))
                conn.execute(text("UPDATE \"user\" SET access_tier = 'public_sector' WHERE email LIKE '%.gov.uk' OR email = 'markjforde@gmail.com'"))
                conn.commit()
        except Exception as _e:
            app.logger.warning('user access_tier migration failed: %s', _e)
    # Rename tier values for existing rows (civil_servant→public_sector, restricted→standard)
    # Idempotent: subsequent runs update 0 rows
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE \"user\" SET access_tier = 'public_sector' WHERE access_tier = 'civil_servant'"))
            conn.execute(text("UPDATE \"user\" SET access_tier = 'standard' WHERE access_tier = 'restricted'"))
            conn.commit()
    except Exception as _e:
        app.logger.warning('tier rename migration failed: %s', _e)
    # Add password reset and deletion columns to user table
    _user_cols2 = {c['name'] for c in _sa_inspect(db.engine).get_columns('user')}
    for _col, _defn in [
        ('reset_token', 'VARCHAR(100)'),
        ('reset_token_expiry', 'TIMESTAMP'),
        ('deletion_requested_at', 'TIMESTAMP'),
        ('stripe_customer_id', 'VARCHAR(64)'),
        ('stripe_subscription_id', 'VARCHAR(64)'),
        ('sector', 'VARCHAR(50)'),
    ]:
        if _col not in _user_cols2:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {_col} {_defn}'))
                    conn.commit()
            except Exception as _e:
                app.logger.warning('reset token migration failed for %s: %s', _col, _e)
    # Add new columns to tracked_stakeholder (website, rss_url, description)
    _ts_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('tracked_stakeholder')}
    _mig_log('inspect tracked_stakeholder done')
    for _col, _defn in [('website', 'VARCHAR(300)'), ('rss_url', 'VARCHAR(500)'), ('description', 'TEXT')]:
        if _col not in _ts_cols:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE tracked_stakeholder ADD COLUMN {_col} {_defn}'))
                    conn.commit()
            except Exception as _e:
                app.logger.warning('tracked_stakeholder migration failed for %s: %s', _col, _e)
    # Refresh CHECK constraints and widen TEXT columns on sd_ tables.
    # Uses pg_try_advisory_lock so only one worker runs these on multi-worker startup.
    # Second worker skips entirely — constraints are already correct after first worker.
    from stakeholder_directory.vocab import (
        ORG_TYPE_VALUES, SCOPE_VALUES, STATUS_VALUES,
        REGISTRATION_STATUS_VALUES, SOURCE_TYPE_VALUES, STAGING_STATUS_VALUES,
    )
    def _ck_in(col, vals):
        quoted = ', '.join(f"'{v}'" for v in vals)
        return f"({col} IN ({quoted}))"
    _sd_constraints = [
        ('sd_organisation', 'ck_sd_org_type',    _ck_in('type', ORG_TYPE_VALUES)),
        ('sd_organisation', 'ck_sd_org_scope',   _ck_in('scope', SCOPE_VALUES)),
        ('sd_organisation', 'ck_sd_org_status',  _ck_in('status', STATUS_VALUES)),
        ('sd_organisation', 'ck_sd_org_reg_status',
            f"(registration_status IS NULL OR {_ck_in('registration_status', REGISTRATION_STATUS_VALUES)})"),
        ('sd_engagement',   'ck_sd_eng_source_type', _ck_in('source_type', SOURCE_TYPE_VALUES)),
        ('sd_staging_ministerial_meeting', 'ck_sd_staging_min_status', _ck_in('processing_status', STAGING_STATUS_VALUES)),
        ('sd_staging_committee_evidence',  'ck_sd_staging_ce_status',  _ck_in('processing_status', STAGING_STATUS_VALUES)),
        ('sd_staging_lobbying_entry',      'ck_sd_staging_le_status',  _ck_in('processing_status', STAGING_STATUS_VALUES)),
    ]
    _sd_text_widenings = [
        ('sd_organisation',              'canonical_name'),
        ('sd_alias',                     'alias_name'),
        ('sd_staging_ministerial_meeting', 'raw_organisation_name'),
        ('sd_staging_committee_evidence',  'raw_organisation_name'),
    ]
    _is_pg = 'postgresql' in app.config.get('SQLALCHEMY_DATABASE_URI', '')
    _mig_log('starting sd_ constraints block')
    try:
        with db.engine.connect() as conn:
            _run_migs = True
            if _is_pg:
                _run_migs = conn.execute(text('SELECT pg_try_advisory_lock(9876543210)')).scalar()
            _mig_log(f'advisory lock acquired={_run_migs}')
            if _run_migs:
                for _tbl, _name, _expr in _sd_constraints:
                    conn.execute(text(f'ALTER TABLE {_tbl} DROP CONSTRAINT IF EXISTS {_name}'))
                    conn.execute(text(f'ALTER TABLE {_tbl} ADD CONSTRAINT {_name} CHECK {_expr}'))
                _mig_log('sd_ CHECK constraints done')
                # Only widen columns still typed as character varying
                _varchar_cols = set(conn.execute(text("""
                    SELECT table_name || '.' || column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND data_type = 'character varying'
                      AND (table_name, column_name) IN (
                        VALUES ('sd_organisation','canonical_name'),
                               ('sd_alias','alias_name'),
                               ('sd_staging_ministerial_meeting','raw_organisation_name'),
                               ('sd_staging_committee_evidence','raw_organisation_name')
                      )
                """)).scalars()) if _is_pg else set()
                for _tbl, _col in _sd_text_widenings:
                    if not _is_pg or f'{_tbl}.{_col}' in _varchar_cols:
                        conn.execute(text(f'ALTER TABLE {_tbl} ALTER COLUMN {_col} TYPE TEXT'))
                if _is_pg:
                    conn.execute(text('SELECT pg_advisory_unlock(9876543210)'))
            conn.commit()
    except Exception as _e:
        app.logger.warning('sd_ constraint/widening migration failed: %s', _e)
    _mig_log('sd_ constraints block done')
    # Add columns to sd_organisation added after initial Railway deployment
    _sd_org_new_cols = [
        ('last_verified',   'DATE'),
        ('created_at',      'TIMESTAMP NOT NULL DEFAULT NOW()'),
        ('updated_at',      'TIMESTAMP NOT NULL DEFAULT NOW()'),
        ('canonical_url',   'VARCHAR(500)'),
        ('description',     'TEXT'),
        ('registration_status', 'VARCHAR(50)'),
        ('registration_number', 'VARCHAR(50)'),
    ]
    try:
        _org_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('sd_organisation')}
        _mig_log('inspect sd_organisation done')
        with db.engine.connect() as conn:
            for _col, _defn in _sd_org_new_cols:
                if _col not in _org_cols:
                    conn.execute(text(f'ALTER TABLE sd_organisation ADD COLUMN {_col} {_defn}'))
            conn.commit()
    except Exception as _e:
        app.logger.warning('sd_organisation migration failed: %s', _e)
    _mig_log('sd_organisation cols done')
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
        _mig_log('inspect sd_engagement done')
        with db.engine.connect() as conn:
            for _col, _defn in _sd_eng_new_cols:
                if _col not in _eng_cols:
                    conn.execute(text(f'ALTER TABLE sd_engagement ADD COLUMN {_col} {_defn}'))
            conn.commit()
    except Exception as _e:
        app.logger.warning('sd_engagement migration failed: %s', _e)
    _mig_log('sd_engagement cols done')
    # Add columns to ha_session added in Phase 2A build
    try:
        _ha_sess_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('ha_session')}
        with db.engine.connect() as conn:
            for _col, _defn in [
                ('location', 'VARCHAR(100)'),
                ('hrs_tag', 'VARCHAR(100)'),
                ('is_container', 'BOOLEAN NOT NULL DEFAULT 0'),
                ('slug', 'VARCHAR(200)'),
                ('department', 'VARCHAR(200)'),
            ]:
                if _col not in _ha_sess_cols:
                    conn.execute(text(f'ALTER TABLE ha_session ADD COLUMN {_col} {_defn}'))
            # Unique index on slug — separate from ADD COLUMN because SQLite does
            # not support ADD COLUMN ... UNIQUE. CREATE UNIQUE INDEX works on both
            # SQLite and PostgreSQL and tolerates multiple NULL values correctly.
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uix_ha_session_slug ON ha_session (slug)'
            ))
            conn.commit()
    except Exception as _e:
        app.logger.warning('ha_session migration failed: %s', _e)
    _mig_log('ha_session cols done')
    # Add theme_type column to ha_session_theme added in Phase 2A Week 2
    try:
        _ha_theme_cols = {c['name'] for c in _sa_inspect(db.engine).get_columns('ha_session_theme')}
        with db.engine.connect() as conn:
            if 'theme_type' not in _ha_theme_cols:
                conn.execute(text(
                    "ALTER TABLE ha_session_theme ADD COLUMN theme_type VARCHAR(20) NOT NULL DEFAULT 'specific'"
                ))
            conn.commit()
    except Exception as _e:
        app.logger.warning('ha_session_theme migration failed: %s', _e)
    _mig_log('ha_session_theme cols done')
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
    _mig_log('starting MemberLink seeds')
    for s in _SEEDS:
        if not MemberLink.get_by_parliament_id(s['parliament_id']):
            MemberLink.upsert(**s)
    _mig_log('MemberLink seeds done')
    if not User.query.filter_by(email='joe@university.ac.uk').first():
        joe_pass = generate_password_hash('password123', method='pbkdf2:sha256')
        joe = User(email='joe@university.ac.uk', password_hash=joe_pass)
        db.session.add(joe)
        db.session.commit()
    _mig_log('user seed done')

    # Seed education stakeholder orgs (run once — skipped if any orgs already exist)
    _mig_log('checking StakeholderOrg count')
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
    _mig_log('StakeholderOrg seed done')
    _mig_log('app_context block complete')

# Kick off background minister link seeding after app context is established
_mig_log('importing debate_scanner for seed_all_minister_links')
from debate_scanner import seed_all_minister_links
_mig_log('debate_scanner imported')
seed_all_minister_links(app)
_mig_log('seed_all_minister_links started')

DEPARTMENTS_FOR_PREFS = [
    "All Departments", "Department for Education",
    "Department of Health and Social Care", "HM Treasury",
    "Home Office", "Ministry of Defence", "Ministry of Justice",
    "Department for Science, Innovation and Technology", "Cabinet Office",
]

SECTOR_OPTIONS = [
    ('civil_service_government',  'UK Civil Service / Government'),
    ('local_government',          'Local government'),
    ('public_sector',             'Public sector (NHS, university, etc.)'),
    ('charity_ngo',               'Charity / NGO / Third sector'),
    ('trade_body',                'Trade body / Membership organisation'),
    ('public_affairs',            'Public affairs / Lobbying / PR consultancy'),
    ('think_tank',                'Think tank / Research institute'),
    ('academic_researcher',       'Academic / Researcher'),
    ('journalist_media',          'Journalist / Media'),
    ('student',                   'Student'),
    ('engaged_citizen_other',     'Engaged citizen / Other'),
]
SECTOR_LABELS = dict(SECTOR_OPTIONS)

# ==========================================
# 5. ROUTES
# ==========================================
@app.route('/ping')
def ping():
    return 'ok', 200

@app.route('/')
@app.route('/home')
def home():
    try:
        return render_template('home.html')
    except Exception as _e:
        print(f'[HOME ERROR] {_e}', flush=True)
        raise

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

_FEEDBACK_CATEGORIES = {
    'bug':     'Bug report',
    'feature': 'Feature request',
    'general': 'General feedback',
}
_FEEDBACK_EMAIL = 'hello@westminsterbrief.co.uk'

@app.route('/feedback', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def feedback():
    submitted = False
    error = None
    if request.method == 'POST':
        # Honeypot — bots fill this in, humans leave it blank
        if request.form.get('website', ''):
            return redirect(url_for('feedback'))

        name     = request.form.get('name', '').strip()[:100]
        email    = request.form.get('email', '').strip()[:200]
        category = request.form.get('category', 'general')
        message  = request.form.get('message', '').strip()[:4000]

        if not message:
            error = 'Please enter a message.'
        else:
            cat_label = _FEEDBACK_CATEGORIES.get(category, 'General feedback')
            subject   = f'[Westminster Brief] {cat_label}'
            if name:
                subject += f' from {name}'

            html_body  = f"""
<p><strong>Category:</strong> {cat_label}</p>
<p><strong>Name:</strong> {name or '(not provided)'}</p>
<p><strong>Email:</strong> {email or '(not provided)'}</p>
<hr>
<p>{message.replace(chr(10), '<br>')}</p>
"""
            text_body = f"Category: {cat_label}\nName: {name or '(not provided)'}\nEmail: {email or '(not provided)'}\n\n{message}"

            ok = send_email(_FEEDBACK_EMAIL, subject, html_body, text_body)
            if ok:
                submitted = True
            else:
                error = 'Sorry, there was a problem sending your message. Please try again or email hello@westminsterbrief.co.uk directly.'

    return render_template('feedback.html',
                           submitted=submitted,
                           error=error,
                           categories=_FEEDBACK_CATEGORIES)

@app.before_request
def _log_request():
    import time as _rt
    _rt._req_start = _rt.monotonic()
    print(f'[REQ] {request.method} {request.path}', flush=True)

@app.after_request
def _log_response(response):
    import time as _rt
    elapsed = _rt.monotonic() - getattr(_rt, '_req_start', _rt.monotonic())
    print(f'[RES] {request.method} {request.path} -> {response.status_code} ({elapsed:.2f}s)', flush=True)
    return response

@app.errorhandler(429)
def ratelimit_handler(e):
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify(error="Too many requests. Please try again in a moment."), 429
    return render_template('429.html'), 429


@app.before_request
def _enforce_feature_flags():
    """Abort 404 for hidden feature routes before @login_required can redirect."""
    auth_paths = {'/login', '/register', '/logout', '/forgot-password'}
    if request.path in auth_paths or request.path.startswith('/reset-password/'):
        if not feature_enabled('FEATURE_AUTH', current_user):
            abort(404)
    if request.path == '/account' or request.path.startswith('/account/'):
        if not feature_enabled('FEATURE_ACCOUNT', current_user):
            abort(404)


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def login():
    if not feature_enabled('FEATURE_AUTH', current_user):
        abort(404)
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
    if not feature_enabled('FEATURE_AUTH', current_user):
        abort(404)
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
            tier = 'public_sector' if _is_approved_email(email) else 'standard'
            user = User(email=email, password_hash=generate_password_hash(password, method='pbkdf2:sha256'), access_tier=tier)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for('onboarding'))
    return render_template('register.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if not feature_enabled('FEATURE_AUTH', current_user):
        abort(404)
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()
            reset_url = url_for('reset_password', token=token, _external=True)
            send_template_email(
                to=user.email,
                subject='Reset your Westminster Brief password',
                template_name='password_reset',
                recipient_email=user.email,
                reset_url=reset_url,
            )
        # Always show the same message — don't reveal whether the email exists
        flash('If an account exists for that email, a password reset link has been sent.')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if not feature_enabled('FEATURE_AUTH', current_user):
        abort(404)
    user = User.query.filter_by(reset_token=token).first()
    if not user or not user.reset_token_expiry or user.reset_token_expiry < datetime.utcnow():
        flash('This reset link has expired or is invalid. Please request a new one.')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')
        if len(password) < 8:
            flash('Password must be at least 8 characters.')
        elif password != confirm:
            flash('Passwords do not match.')
        else:
            user.password_hash = generate_password_hash(password, method='pbkdf2:sha256')
            user.reset_token = None
            user.reset_token_expiry = None
            db.session.commit()
            flash('Password updated. Please sign in with your new password.')
            return redirect(url_for('login'))
    return render_template('reset_password.html', token=token)


@app.route('/account')
@login_required
def account():
    if not feature_enabled('FEATURE_ACCOUNT', current_user):
        abort(404)
    deletion_due = None
    if current_user.deletion_requested_at:
        deletion_due = (current_user.deletion_requested_at + timedelta(days=30)).strftime('%d %B %Y').lstrip('0')
    sector_label = SECTOR_LABELS.get(current_user.sector, '') if current_user.sector else ''
    return render_template('account.html', user=current_user, deletion_due=deletion_due,
                           sector_label=sector_label, sector_options=SECTOR_OPTIONS)


@app.route('/account/sector', methods=['POST'])
@login_required
def account_sector():
    if not feature_enabled('FEATURE_ACCOUNT', current_user):
        abort(404)
    sector = request.form.get('sector', '').strip()
    if sector in SECTOR_LABELS:
        current_user.sector = sector
        db.session.commit()
        flash('Sector updated.')
    return redirect(url_for('account'))


@app.route('/account/change-password', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def account_change_password():
    if not feature_enabled('FEATURE_ACCOUNT', current_user):
        abort(404)
    current_pw = request.form.get('current_password', '')
    new_pw     = request.form.get('new_password', '')
    confirm_pw = request.form.get('confirm_password', '')
    if not check_password_hash(current_user.password_hash, current_pw):
        flash('Current password is incorrect.')
    elif len(new_pw) < 8:
        flash('New password must be at least 8 characters.')
    elif new_pw != confirm_pw:
        flash('New passwords do not match.')
    else:
        current_user.password_hash = generate_password_hash(new_pw, method='pbkdf2:sha256')
        db.session.commit()
        flash('Password updated successfully.')
    return redirect(url_for('account'))


@app.route('/account/export')
@login_required
def account_export():
    if not feature_enabled('FEATURE_ACCOUNT', current_user):
        abort(404)
    import json
    from flask import make_response
    data = {
        'email': current_user.email,
        'access_tier': current_user.access_tier,
        'sector': current_user.sector,
        'topics': [{'keyword': t.keyword, 'department': t.department} for t in current_user.topics],
        'stakeholders': [{'name': s.name, 'bsky_handle': s.bsky_handle} for s in current_user.stakeholders],
    }
    pref = current_user.preference
    if pref:
        data['preferences'] = {
            'department': pref.department,
            'policy_area': pref.policy_area,
            'subject': pref.subject,
        }
    send_template_email(
        to=current_user.email,
        subject='Westminster Brief — Your data export',
        template_name='data_export_ready',
        recipient_email=current_user.email,
        export_requested_at=datetime.utcnow().strftime('%d %B %Y at %H:%M UTC').lstrip('0'),
    )
    resp = make_response(json.dumps(data, indent=2))
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="westminsterbrief-data-{datetime.utcnow().strftime("%Y-%m-%d")}.json"'
    )
    return resp


@app.route('/account/cancel-deletion', methods=['POST'])
@login_required
def account_cancel_deletion():
    if not feature_enabled('FEATURE_ACCOUNT', current_user):
        abort(404)
    current_user.deletion_requested_at = None
    db.session.commit()
    flash('Account deletion cancelled. Your account is active.')
    return redirect(url_for('account'))


@app.route('/account/delete', methods=['POST'])
@login_required
def account_delete():
    if not feature_enabled('FEATURE_ACCOUNT', current_user):
        abort(404)
    deletion_date = (datetime.utcnow() + timedelta(days=30)).strftime('%d %B %Y').lstrip('0')
    send_template_email(
        to=current_user.email,
        subject='Westminster Brief — Account deletion requested',
        template_name='account_deletion_confirmed',
        recipient_email=current_user.email,
        deletion_date=deletion_date,
    )
    current_user.deletion_requested_at = datetime.utcnow()
    db.session.commit()
    logout_user()
    flash('Deletion request received. You will receive a confirmation email. Your data will be removed within 30 days.')
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------

def _stripe_handle_checkout_completed(obj):
    customer_id = obj.get('customer')
    sub_id = obj.get('subscription')
    details = obj.get('customer_details') or {}
    email = details.get('email') or obj.get('customer_email', '')
    if not (customer_id and email):
        return
    user = User.query.filter_by(email=email.lower()).first()
    if user:
        user.stripe_customer_id = customer_id
        if sub_id:
            user.stripe_subscription_id = sub_id
        db.session.commit()
        app.logger.info('[STRIPE] checkout completed → user %s', user.id)


def _stripe_handle_subscription_updated(obj):
    customer_id = obj.get('customer')
    sub_id = obj.get('id')
    if not customer_id:
        return
    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if user:
        user.stripe_subscription_id = sub_id
        db.session.commit()
        app.logger.info('[STRIPE] subscription updated → user %s status=%s', user.id, obj.get('status'))


def _stripe_handle_subscription_deleted(obj):
    customer_id = obj.get('customer')
    if not customer_id:
        return
    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if user:
        user.stripe_subscription_id = None
        db.session.commit()
        app.logger.info('[STRIPE] subscription deleted → user %s', user.id)


@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    try:
        import stripe as _stripe
    except ImportError:
        app.logger.error('[STRIPE] stripe package not installed')
        return jsonify({'error': 'stripe not configured'}), 500

    payload = request.data
    sig_header = request.headers.get('Stripe-Signature', '')
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    if not webhook_secret:
        app.logger.warning('[STRIPE] STRIPE_WEBHOOK_SECRET not set — webhook ignored')
        return jsonify({'status': 'ignored'}), 200

    try:
        event = _stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except _stripe.error.SignatureVerificationError:
        app.logger.warning('[STRIPE] Signature verification failed')
        return jsonify({'error': 'invalid signature'}), 400
    except Exception as _e:
        app.logger.warning('[STRIPE] Webhook parse error: %s', _e)
        return jsonify({'error': 'invalid payload'}), 400

    event_type = event['type']
    obj = event['data']['object']
    app.logger.info('[STRIPE] Event received: %s', event_type)

    if event_type == 'checkout.session.completed':
        _stripe_handle_checkout_completed(obj)
    elif event_type in ('customer.subscription.created', 'customer.subscription.updated'):
        _stripe_handle_subscription_updated(obj)
    elif event_type == 'customer.subscription.deleted':
        _stripe_handle_subscription_deleted(obj)

    return jsonify({'status': 'ok'}), 200


@app.route('/logout')
@login_required
def logout():
    if not feature_enabled('FEATURE_AUTH', current_user):
        abort(404)
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
    return render_template('onboarding.html', departments=DEPARTMENTS_FOR_PREFS, sector_options=SECTOR_OPTIONS)

_GOVT_SECTORS = {'civil_service_government', 'local_government'}

@app.route('/onboarding/save', methods=['POST'])
@login_required
def onboarding_save():
    sector = request.form.get('sector', '').strip() or 'engaged_citizen_other'
    current_user.sector = sector
    if sector in _GOVT_SECTORS:
        dept    = request.form.get('department', '').strip()
        policy  = request.form.get('policy_area', '').strip()
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
    if sector in _GOVT_SECTORS:
        flash('Preferences saved — your searches will now be pre-filled.')
    return redirect(url_for('my_alerts'))

@app.route('/onboarding/skip', methods=['POST'])
@login_required
def onboarding_skip():
    current_user.sector = 'engaged_citizen_other'
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
TOTP_SECRET = os.environ.get('TOTP_SECRET', '').strip()

# Background job status for long-running admin operations
_committee_ingest_status = {'running': False, 'message': None, 'started_at': None}

def _admin_log(action):
    app.logger.info('ADMIN | ip=%s | %s', request.remote_addr, action)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_authenticated', None)
    return redirect('/admin')


@app.route('/admin', methods=['GET', 'POST'])
@limiter.limit("15 per minute", error_message="Too many attempts — wait a minute and try again.")
def admin_panel():
    import pyotp

    # Session already authenticated and not expired
    if session.get('admin_authenticated'):
        pass  # fall through to admin page
    else:
        token = request.args.get('token', '') or request.form.get('token', '')
        totp_code = request.form.get('totp', '')

        if not ADMIN_TOKEN:
            return render_template('admin_login.html', error="ADMIN_TOKEN is not set in environment variables.")
        if token != ADMIN_TOKEN:
            return render_template('admin_login.html', error="Invalid token." if token else None)

        # Token correct — check TOTP second factor
        if TOTP_SECRET:
            if not totp_code:
                return render_template('admin_login.html', token=token, need_totp=True, error=None)
            totp = pyotp.TOTP(TOTP_SECRET)
            if not totp.verify(totp_code, valid_window=1):
                _admin_log('failed TOTP attempt')
                return render_template('admin_login.html', token=token, need_totp=True,
                                       error="Invalid authenticator code. Codes expire every 30 seconds — try again.")

        # Both factors passed — create session
        session.permanent = True
        session['admin_authenticated'] = True
        _admin_log('login successful')

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
                _admin_log('clear_minister')
            except Exception as e:
                message = f'Error clearing minister cache: {e}'
        elif action == 'clear_twfy_search':
            try:
                deleted = CachedTWFYSearch.query.delete()
                db.session.commit()
                message = f'TWFY search cache cleared ({deleted} entries removed).'
                _admin_log(f'clear_twfy_search | {deleted} entries')
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
                _admin_log(f'clear_sessions | {deleted} entries')
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
                _admin_log(f'clear_all | {deleted} TWFY entries')
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
                _admin_log(f'retry_failed_links | {len(reset)} reset')
            except Exception as e:
                db.session.rollback()
                message = f'Error resetting failed links: {e}'

        elif action == 'ingest_committee_evidence':
            import threading, requests as _requests
            from datetime import date as _date

            if _committee_ingest_status['running']:
                message = 'Ingestion already running — check back in a few minutes.'
            else:
                raw_ids = (request.form.get('committee_ids') or '').strip()
                date_from_str = (request.form.get('date_from') or '').strip()
                date_to_str = (request.form.get('date_to') or '').strip()

                try:
                    if raw_ids.lower() == 'all':
                        committee_ids = []
                        for _attempt in range(3):
                            try:
                                resp = _requests.get(
                                    'https://committees-api.parliament.uk/api/Committees',
                                    params={'status': 'Current', 'take': 300}, timeout=30,
                                )
                                committee_ids = [item['id'] for item in resp.json().get('items', [])]
                                break
                            except Exception:
                                if _attempt == 2:
                                    raise
                                import time as _time; _time.sleep(3)
                    else:
                        committee_ids = [int(x.strip()) for x in raw_ids.split(',') if x.strip()]
                except Exception as e:
                    committee_ids = []
                    message = f'Error resolving committee IDs: {e}'

                if committee_ids:
                    incremental = request.form.get('incremental') == '1'
                    fallback_start = _date(2024, 1, 1)
                    end_date = _date.fromisoformat(date_to_str) if date_to_str else _date.today()

                    if incremental:
                        from stakeholder_directory.ingesters.committee_evidence import get_incremental_start_dates
                        per_start = get_incremental_start_dates(committee_ids, fallback_start=fallback_start)
                        new_count = sum(1 for cid in committee_ids if per_start[cid] == fallback_start)
                        incremental_count = len(committee_ids) - new_count
                    else:
                        per_start = None
                        start_date = _date.fromisoformat(date_from_str) if date_from_str else fallback_start

                    def _run_ingest(cids, sd, ed, psd):
                        from stakeholder_directory.ingesters.committee_evidence import ingest_committee_evidence
                        from stakeholder_directory.normalisation.normaliser import normalise_pending_staging
                        from stakeholder_directory.models import Organisation, Engagement
                        import time as _t
                        _committee_ingest_status['running'] = True
                        _committee_ingest_status['message'] = (
                            f'Running incremental update for {len(cids)} committees → {ed}'
                            if psd else f'Running… {len(cids)} committees, {sd} → {ed}'
                        )
                        try:
                            with app.app_context():
                                orgs_before = Organisation.query.count()
                                eng_before = Engagement.query.count()
                                t0 = _t.monotonic()
                                ing = ingest_committee_evidence(cids, sd, ed, per_committee_start_dates=psd)
                                norm = normalise_pending_staging('staging_committee_evidence', batch_size=5000)
                                duration = int(_t.monotonic() - t0)
                                orgs_after = Organisation.query.count()
                                eng_after = Engagement.query.count()
                                _committee_ingest_status['message'] = (
                                    f'Done in {duration}s across {len(cids)} committees. '
                                    f'Publications: {ing.publications_fetched}, witnesses: {ing.witnesses_processed}, '
                                    f'staged: {ing.rows_staged} (skipped {ing.rows_skipped_duplicate} dup, '
                                    f'{ing.rows_skipped_internal_govt} govt). '
                                    f'Normalised: {norm.staging_records_processed} rows — '
                                    f'new orgs: {orgs_after - orgs_before}, '
                                    f'new engagements: {eng_after - eng_before}. '
                                    f'Errors: {len(ing.errors) + len(norm.errors)}.'
                                    + (f' First: {(ing.errors + norm.errors)[0]}' if ing.errors or norm.errors else '')
                                )
                        except Exception as e:
                            db.session.rollback()
                            _committee_ingest_status['message'] = f'Ingestion error: {e}'
                        finally:
                            _committee_ingest_status['running'] = False

                    threading.Thread(target=_run_ingest, args=(committee_ids, fallback_start if incremental else start_date, end_date, per_start), daemon=True).start()
                    if incremental:
                        message = (
                            f'Incremental update started for {len(committee_ids)} committees '
                            f'({incremental_count} updating since last run, {new_count} fetching from scratch). '
                            'Refresh this page in a few minutes to see results.'
                        )
                        _admin_log(f'ingest_committee_evidence incremental | {len(committee_ids)} committees')
                    else:
                        message = f'Ingestion started for {len(committee_ids)} committees ({start_date} → {end_date}). Refresh this page in a few minutes to see results.'
                        _admin_log(f'ingest_committee_evidence full | {len(committee_ids)} committees | {start_date} → {end_date}')

        elif action == 'clear_directory_data':
            try:
                from stakeholder_directory.models import Organisation, Engagement, Alias, Flag, PolicyAreaTag, IngestionRun
                from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting, StagingCommitteeEvidence, StagingLobbyingEntry
                for model in (StagingMinisterialMeeting, StagingCommitteeEvidence, StagingLobbyingEntry,
                              Flag, PolicyAreaTag, Alias, Engagement, Organisation, IngestionRun):
                    db.session.query(model).delete()
                db.session.commit()
                message = 'Directory data cleared — all orgs, engagements, staging rows and ingestion logs deleted.'
                _admin_log('clear_directory_data')
            except Exception as e:
                db.session.rollback()
                message = f'Error clearing directory data: {e}'

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
                        f'Errors: {len(ing.errors) + len(norm.errors)}. '
                        + (f'First error: {(ing.errors + norm.errors)[0]}' if ing.errors or norm.errors else '')
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
        from sqlalchemy import func as _func
        _type_rows = (
            db.session.query(Engagement.source_type, _func.count(Engagement.id))
            .group_by(Engagement.source_type)
            .all()
        )
        dir_stats['eng_by_type'] = {st: n for st, n in _type_rows}
    except Exception as e:
        dir_stats['error'] = f'DB error: {e}'
    try:
        from stakeholder_directory.vocab import _load_yaml
        dept_yaml = _load_yaml('departments.yaml')
        dir_stats['departments'] = {k: v['name'] for k, v in dept_yaml.get('departments', {}).items()}
    except Exception as e:
        dir_stats['error'] = (dir_stats.get('error') or '') + f' | YAML error: {e}'

    # Show background ingestion status as the message if no other message and job ran/is running
    if not message and _committee_ingest_status['message']:
        message = _committee_ingest_status['message']
        if _committee_ingest_status['running']:
            message = '⏳ ' + message

    return render_template('admin.html',
                           message=message,
                           minister_status=minister_status,
                           twfy_total=twfy_total,
                           twfy_session=twfy_session,
                           twfy_keyword=twfy_keyword,
                           member_link_stats=member_link_stats,
                           dir_stats=dir_stats)


# ==========================================
# 7. BLUEPRINTS
# ==========================================
app.register_blueprint(hansard_bp)
app.register_blueprint(biography_bp)
app.register_blueprint(tracker_bp)
app.register_blueprint(debate_scanner_bp)
app.register_blueprint(mp_search_bp)
app.register_blueprint(directory_bp)
app.register_blueprint(archive_bp)

@app.context_processor
def inject_version():
    return {'app_version': APP_VERSION}

if __name__ == '__main__':
    app.run(debug=True)