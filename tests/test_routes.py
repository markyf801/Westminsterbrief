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
from datetime import date
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
    from hansard_archive.models import (
        HansardContribution, HansardSession, HansardSessionTheme, HaPQ, HaPQTheme,
        THEME_TYPE_POLICY_AREA, THEME_TYPE_SPECIFIC,
    )

    # Import blueprints
    from hansard import hansard_bp
    from biography import biography_bp
    from tracker import tracker_bp
    from debate_scanner import debate_scanner_bp
    from mp_search import mp_search_bp
    from stakeholder_directory.views import directory_bp
    from hansard_archive.views import archive_bp, brief_bp
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
               mp_search_bp, directory_bp, archive_bp, brief_bp):
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
            hansard_url='https://hansard.parliament.uk/Commons/2026-01-01/debates/TEST-GUID/TestDebateSession',
            contributions_ingested=True,
            is_container=False,
        )
        _db.session.add(s)

        # Second session sharing only the 'rail fares' specific tag with test-session
        s2 = HansardSession(
            ext_id='test-ext-002',
            title='Rail Fares Consultation',
            date=date(2026, 1, 8),
            house='Lords',
            debate_type='debate',
            slug='rail-fares-consultation',
            contributions_ingested=True,
            is_container=False,
        )
        _db.session.add(s2)

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

        # Extra PQs for /brief/<slug> topic tests
        extra_pqs = [
            HaPQ(uin=f'TEST00{i}', heading=f'Chagos Islands question {i}',
                 question_text='What is the government doing about the Chagos Islands?',
                 tabled_date=date(2026, 2, i), asking_member='Test MP',
                 answering_body='Foreign Office', is_answered=True)
            for i in (2, 3, 4)
        ]
        _db.session.add_all(extra_pqs)

        # Related-PQ panel on test-session: policy-area-only match inside the
        # window, and one outside the ±180-day window.
        transport_pqs = [
            HaPQ(uin='TEST010', heading='Rail timetable changes',
                 question_text='What assessment has been made of rail timetables?',
                 tabled_date=date(2026, 3, 1), answering_body='Department for Transport',
                 is_answered=True),
            HaPQ(uin='TEST011', heading='Old transport question',
                 question_text='An old question about transport.',
                 tabled_date=date(2024, 1, 1), answering_body='Department for Transport',
                 is_answered=True),
        ]
        _db.session.add_all(transport_pqs)
        # Search-fix PQs: 24 match "school funding". SCH001 is the only heading
        # match (oldest); SCH002 is the newest. BEIS001 is a defunct department.
        _db.session.add_all(
            [HaPQ(uin='SCH001', heading='School funding',
                  question_text='What is the school funding settlement for rural areas?',
                  tabled_date=date(2025, 6, 1), asking_member='Test MP',
                  answering_body='Department for Education', answering_body_id=60,
                  is_answered=True),
             HaPQ(uin='SCH002', heading='Teachers',
                  question_text='What assessment has been made of the school funding formula?',
                  tabled_date=date(2026, 2, 10), asking_member='Test MP',
                  answering_body='Department for Education', answering_body_id=60,
                  is_answered=False),
             HaPQ(uin='BEIS001', heading='Energy prices',
                  question_text='What steps are being taken on energy prices?',
                  tabled_date=date(2025, 5, 1), asking_member='Test MP',
                  answering_body='Department for Business, Energy and Industrial Strategy',
                  answering_body_id=7, is_answered=True)]
            + [HaPQ(uin=f'SCHB{i:02d}', heading=f'Education budget {i}',
                    question_text='What is the school funding allocation for next year?',
                    tabled_date=date(2025, 7, 1 + i), asking_member='Test MP',
                    answering_body='Department for Education', answering_body_id=60,
                    is_answered=True)
               for i in range(22)]
        )
        _db.session.flush()

        _db.session.add(HansardContribution(
            session_id=s.id, member_name='Test Speaker (Lab)', member_id=12345,
            speech_text='I beg to move.', speech_order=1,
        ))

        # Topic tags:
        #   'Chagos Islands'      PQ-only, 3 PQs        -> 200, indexed
        #   'Workplace violence'  PQ-only, 1 PQ         -> 200, noindex (thin)
        #   "Women's pensions"    PQ-only, punctuation  -> slug womens-pensions
        #   'Bus services'        1 session + 2 PQs     -> both lists shown
        #   'Transport'           policy area (bridge slug /brief/transport)
        _db.session.add_all(
            [HaPQTheme(pq_id=p.id, theme='Chagos Islands', theme_type=THEME_TYPE_SPECIFIC)
             for p in extra_pqs]
            + [
                HaPQTheme(pq_id=pq.id, theme='Workplace violence', theme_type=THEME_TYPE_SPECIFIC),
                HaPQTheme(pq_id=pq.id, theme="Women's pensions", theme_type=THEME_TYPE_SPECIFIC),
                HaPQTheme(pq_id=pq.id, theme='Bus services', theme_type=THEME_TYPE_SPECIFIC),
                HaPQTheme(pq_id=extra_pqs[0].id, theme='Bus services', theme_type=THEME_TYPE_SPECIFIC),
                HaPQTheme(pq_id=pq.id, theme='Transport', theme_type=THEME_TYPE_POLICY_AREA),
                HansardSessionTheme(session_id=s.id, theme='Bus services',
                                    theme_type=THEME_TYPE_SPECIFIC),
                HansardSessionTheme(session_id=s.id, theme='Transport',
                                    theme_type=THEME_TYPE_POLICY_AREA),
                HansardSessionTheme(session_id=s.id, theme='rail fares',
                                    theme_type=THEME_TYPE_SPECIFIC),
                HansardSessionTheme(session_id=s2.id, theme='rail fares',
                                    theme_type=THEME_TYPE_SPECIFIC),
            ]
            + [HaPQTheme(pq_id=p.id, theme='Transport', theme_type=THEME_TYPE_POLICY_AREA)
               for p in transport_pqs]
        )
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

    @pytest.mark.parametrize('dtype', ['oral_questions', 'pmqs', 'committee_stage', 'urgent_question'])
    def test_archive_home_type_filters(self, client, dtype):
        # The grouped Oral Questions / PMQs view raised UnboundLocalError (500)
        # because a local `from collections import defaultdict` shadowed the
        # module import for the whole function.
        r = client.get(f'/archive?dtype={dtype}')
        assert r.status_code == 200
        r = client.get(f'/archive?dtype={dtype}&house=Lords')
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

    def test_debate_related_pqs_panel(self, client):
        """Tag-matched PQs: specific-tag matches first (newest first), then
        policy-area fill; headingless PQs fall back to question text."""
        html = client.get('/archive/debate/1-january-2026/test-session').get_data(as_text=True)
        assert 'Related written questions' in html
        # TEST001 has no heading: question text is the title, never the bare UIN
        assert ('<span class="rpa-title">What is the government doing about test issues?</span>'
                in html)
        assert '<span class="rpa-title">TEST001</span>' not in html
        assert 'href="/archive/pq/test001"' in html
        # Order: specific 'Bus services' (TEST002 Feb, TEST001 Jan), then policy-area TEST010
        i2 = html.index('href="/archive/pq/test002"')
        i1 = html.index('href="/archive/pq/test001"')
        i10 = html.index('href="/archive/pq/test010"')
        assert i2 < i1 < i10
        assert 'Department for Transport' in html
        # Outside the ±180-day window
        assert 'href="/archive/pq/test011"' not in html

    def test_pq_display_heading_fallback(self):
        from hansard_archive.views import _pq_display_heading
        assert _pq_display_heading('Bus services', 'x') == 'Bus services'
        long_q = 'To ask the Secretary of State for Transport ' + 'what steps are being taken ' * 5
        title = _pq_display_heading(None, long_q)
        assert title.endswith('…') and len(title) <= 81
        assert title.startswith('To ask the Secretary of State for Transport')
        assert _pq_display_heading('  ', '') == 'Written question'

    def test_debate_topic_related_sessions(self, client):
        html = client.get('/archive/debate/1-january-2026/test-session').get_data(as_text=True)
        assert 'Debates on the same topics' in html
        assert 'href="/archive/debate/8-january-2026/rail-fares-consultation"' in html

    def test_debate_cite_box(self, client):
        html = client.get('/archive/debate/1-january-2026/test-session').get_data(as_text=True)
        assert 'id="cite-this"' in html
        assert 'id="sd-cite-copy"' in html
        expected = (
            "HC Deb 1 January 2026, &#39;Test Debate Session&#39;. Westminster Brief, "
            "https://westminsterbrief.co.uk/archive/debate/1-january-2026/test-session. "
            "Official record: https://hansard.parliament.uk/Commons/2026-01-01/debates/"
            "TEST-GUID/TestDebateSession"
        )
        assert expected in html
        lords = client.get('/archive/debate/8-january-2026/rail-fares-consultation').get_data(as_text=True)
        assert "HL Deb 8 January 2026, &#39;Rail Fares Consultation&#39;." in lords
        assert 'Official record:' not in lords  # no hansard_url stored

    def test_debate_contribution_anchors(self, app, client):
        from hansard_archive.models import HansardContribution
        with app.app_context():
            cid = HansardContribution.query.filter_by(member_name='Test Speaker (Lab)').one().id
        html = client.get('/archive/debate/1-january-2026/test-session').get_data(as_text=True)
        assert f'id="contrib-{cid}"' in html
        assert f'href="#contrib-{cid}"' in html
        assert 'class="sd-contrib-permalink"' in html

    def test_archive_pq_detail_missing_returns_404(self, client):
        r = client.get('/archive/pq/NOTEXIST')
        assert r.status_code == 404

    def test_archive_debate_detail_missing_returns_404(self, client):
        r = client.get('/archive/debate/1-january-2026/no-such-session')
        assert r.status_code == 404


# ── Topic brief pages — /brief/<slug> ────────────────────────────────────────

_INDEX = '<meta name="robots" content="index, follow">'
_NOINDEX = '<meta name="robots" content="noindex, follow">'


class TestBriefRoutes:
    def test_archive_theme_redirects_to_brief(self, client):
        r = client.get('/archive/theme/chagos-islands?page=1')
        assert r.status_code == 301
        assert r.headers['Location'].endswith('/brief/chagos-islands?page=1')

    def test_pq_only_topic_lists_written_questions(self, client):
        r = client.get('/brief/chagos-islands')
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert 'Chagos Islands question 2' in html
        assert 'href="/archive/pq/test004"' in html
        assert '3 written questions on this topic' in html
        assert _INDEX in html

    def test_topic_with_debates_and_pqs_shows_both(self, client):
        html = client.get('/brief/bus-services').get_data(as_text=True)
        assert 'href="/archive/debate/1-january-2026/test-session"' in html
        assert 'href="/archive/pq/test001"' in html
        assert '1 session and 2 written questions on this topic' in html
        assert _INDEX in html

    def test_policy_area_bridge_slug_finds_sessions(self, client):
        """Bridge slugs must filter on policy_area tags, not specific topics."""
        r = client.get('/brief/transport')
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert 'test-session' in html
        assert 'href="/archive/pq/test001"' in html

    def test_slug_with_punctuation_resolves(self, client):
        r = client.get('/brief/womens-pensions')
        assert r.status_code == 200
        assert "Women&#39;s pensions" in r.get_data(as_text=True)

    def test_thin_topic_is_noindex(self, client):
        r = client.get('/brief/workplace-violence')
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert _NOINDEX in html
        assert _INDEX not in html

    def test_unknown_topic_is_404_with_friendly_page(self, client):
        r = client.get('/brief/no-such-topic-anywhere')
        html = r.get_data(as_text=True)
        assert r.status_code == 404
        assert 'No debates or written questions' in html
        assert _NOINDEX in html

    def test_page_past_last_is_404(self, client):
        assert client.get('/brief/chagos-islands?page=2').status_code == 404
        assert client.get('/brief/chagos-islands?page=999999').status_code == 404

    def test_bad_page_param_falls_back_to_first_page(self, client):
        assert client.get('/brief/chagos-islands?page=abc').status_code == 200

    def test_other_pages_default_to_index(self, client):
        assert _INDEX in client.get('/archive').get_data(as_text=True)


# ── get_related_content optimisation ─────────────────────────────────────────

class TestGetRelatedContentParams:
    """
    Verify that get_related_content calls _fts_search with skip_count=True
    and lim=limit, and _pq_fts_search with limit=limit.

    These parameters eliminate a wasted COUNT(DISTINCT) query and cap
    ts_headline generation to the number of results actually needed.
    Both callers that DO need the count (archive_search, hansard_home)
    pass skip_count=False (the default) — this test confirms only the
    sidebar call uses the optimised path.
    """

    def test_skip_count_and_lim_forwarded(self, app):
        from unittest.mock import patch
        from hansard_archive.views import get_related_content

        with app.app_context():
            with (
                patch('hansard_archive.views._is_postgres', return_value=True),
                patch('hansard_archive.views._fts_search', return_value=([], 0)) as mock_fts,
                patch('hansard_archive.views._pq_fts_search', return_value=[]),
                patch('hansard_archive.views._bill_fts_search', return_value=[]),
            ):
                get_related_content("student loan repayments", limit=8)

                mock_fts.assert_called_once()
                kwargs = mock_fts.call_args.kwargs
                assert kwargs.get('skip_count') is True, (
                    "skip_count must be True — COUNT(DISTINCT) is discarded "
                    "by the sidebar caller and runs for nothing"
                )
                assert kwargs.get('lim') == 8, (
                    "lim must equal limit — fetching _PER_PAGE=25 results "
                    "and slicing to 8 wastes ts_headline calls"
                )

    def test_pq_fts_limit_matches_limit(self, app):
        from unittest.mock import patch
        from hansard_archive.views import get_related_content

        with app.app_context():
            with (
                patch('hansard_archive.views._is_postgres', return_value=True),
                patch('hansard_archive.views._fts_search', return_value=([], 0)),
                patch('hansard_archive.views._pq_fts_search', return_value=[]) as mock_pq,
                patch('hansard_archive.views._bill_fts_search', return_value=[]),
            ):
                get_related_content("student loan repayments", limit=8)

                mock_pq.assert_called_once()
                kwargs = mock_pq.call_args.kwargs
                assert kwargs.get('limit') == 8, (
                    "_pq_fts_search must use limit=8, not the old hardcoded 50 "
                    "— 50 ts_headline calls for a sidebar showing 8 results"
                )


# ── Search fixes: GET WQ search, sort, true WQ count, page caps ───────────────

def _pq_uins(html):
    return [u.upper() for u in re.findall(r'href="/archive/pq/([a-z0-9]+)"', html)]


class TestWrittenQuestionsSearch:
    def test_get_search_returns_results(self, client):
        r = client.get('/questions?q=school+funding')
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert 'SCH001' in html and 'SCH002' in html
        assert 'value="school funding"' in html
        assert _NOINDEX in html

    def test_old_subject_param_still_works(self, client):
        assert 'SCH002' in client.get('/questions?subject=school+funding').get_data(as_text=True)

    def test_get_filters_apply(self, client):
        html = client.get('/questions?q=school+funding&status_filter=unanswered').get_data(as_text=True)
        assert 'SCH002' in html
        assert 'SCH001' not in html

    def test_blank_form_is_indexable_and_runs_no_search(self, client):
        html = client.get('/questions').get_data(as_text=True)
        assert _INDEX in html
        assert 'SCH001' not in html
        assert 'No questions found' not in html

    def test_post_search_redirects_to_get(self, client):
        r = client.post('/questions', data={
            'subject': 'school funding', 'department': '60', 'house_filter': 'All',
            'status_filter': 'all', 'start_date': '', 'end_date': '', 'action': 'search',
        })
        assert r.status_code == 303
        assert r.headers['Location'].endswith(
            '/written-questions?q=school+funding&department=60')

    def test_post_export_still_downloads(self, client):
        r = client.post('/questions', data={'q': 'school funding', 'action': 'csv'})
        assert r.status_code == 200
        assert 'attachment' in r.headers['Content-Disposition']
        assert 'SCH001' in r.get_data(as_text=True)

    def test_huge_page_redirects_to_last_page(self, client):
        r = client.get('/questions?q=school&page=99999999999999999999')
        assert r.status_code == 302
        assert r.headers['Location'].endswith('/written-questions?q=school')

    def test_department_list_drops_defunct_departments(self, app):
        import hansard
        hansard._dept_list_cache = None
        with app.app_context():
            depts = hansard._get_department_list()
        hansard._dept_list_cache = None
        assert depts['Department for Education'] == '60'
        assert 'Department for Business, Energy and Industrial Strategy' not in depts


class TestArchiveSearchFixes:
    def test_relevance_is_default(self, client):
        html = client.get('/archive/search?q=school+funding').get_data(as_text=True)
        assert _pq_uins(html)[0] == 'SCH001'   # only heading match wins on SQLite
        assert '<strong aria-current="true">Relevance</strong>' in html
        assert 'href="/archive/search?q=school+funding&sort=newest"' in html

    def test_sort_newest_orders_by_date_desc(self, client):
        from hansard_archive.models import HaPQ
        html = client.get('/archive/search?q=school+funding&sort=newest').get_data(as_text=True)
        uins = _pq_uins(html)
        assert uins[0] == 'SCH002'
        with client.application.app_context():
            dates = {p.uin: p.tabled_date for p in HaPQ.query.filter(HaPQ.uin.in_(uins))}
        shown = [dates[u] for u in uins]
        assert shown == sorted(shown, reverse=True)
        assert '<strong aria-current="true">Newest</strong>' in html
        assert 'name="sort" value="newest"' in html

    def test_unknown_sort_falls_back_to_relevance(self, client):
        html = client.get('/archive/search?q=school+funding&sort=bogus').get_data(as_text=True)
        assert _pq_uins(html)[0] == 'SCH001'

    def test_wq_count_shows_true_total(self, client):
        html = client.get('/archive/search?q=school+funding').get_data(as_text=True)
        assert len(_pq_uins(html)) == 20
        assert '<span class="as-tab-count">24</span>' in html
        assert 'href="/written-questions?q=school+funding">See all 24 written questions' in html

    def test_wq_count_is_capped_on_postgres(self, client):
        from unittest.mock import patch
        pq = {'uin': 'X1', 'heading': 'h', 'asking_member': '', 'answering_body': '',
              'is_answered': True, 'url': '/archive/pq/X1', 'human_date': '',
              'snippet': None, 'question_text': ''}
        with (
            patch('hansard_archive.views._is_postgres', return_value=True),
            patch('hansard_archive.views._fts_search', return_value=([], 0)) as mock_fts,
            patch('hansard_archive.views._pq_fts_search', return_value=[pq] * 20) as mock_pq,
            patch('hansard_archive.views._pq_fts_count', return_value=10001),
            patch('hansard_archive.views._all_policy_areas', return_value=[]),
        ):
            html = client.get('/archive/search?q=school&sort=newest').get_data(as_text=True)
        assert mock_fts.call_args.kwargs['sort'] == 'newest'
        assert mock_pq.call_args.kwargs['sort'] == 'newest'
        assert 'See all 10,000+ written questions' in html

    def test_huge_page_is_404_not_500(self, client):
        r = client.get('/archive/search?q=school+funding&page=99999999999999999999')
        assert r.status_code == 404

    def test_page_past_last_is_404(self, client):
        assert client.get('/archive/search?q=school+funding&page=2').status_code == 404
        assert client.get('/archive/search?q=school+funding&page=1').status_code == 200

    def test_postgres_sql_sort_and_recency(self, app):
        """The Postgres SQL orders by date for newest and decays rank by age for relevance."""
        from unittest.mock import MagicMock, patch
        from hansard_archive import views

        def run(fn, **kw):
            with patch.object(views.db.session, 'execute') as ex:
                ex.return_value = MagicMock(scalar=lambda: 0, fetchall=lambda: [])
                fn('school funding', **kw)
                return [str(c.args[0]) for c in ex.call_args_list]

        with app.app_context():
            rel = run(views._fts_search, page=1)[-1]
            new = run(views._fts_search, page=1, sort='newest')[-1]
            pq_rel = run(views._pq_fts_search)[0]
            pq_new = run(views._pq_fts_search, sort='newest')[0]
            count = run(views._pq_fts_count)[0]
        assert 'CURRENT_DATE - s.date' in rel
        assert 'ORDER BY final_rank DESC, s.date DESC' in rel
        assert 'ORDER BY s.date DESC, final_rank DESC' in new
        assert 'CURRENT_DATE - p.tabled_date' in pq_rel
        assert 'ORDER BY rank DESC' in pq_rel
        assert 'ORDER BY p.tabled_date DESC' in pq_new
        assert 'LIMIT :cap' in count
