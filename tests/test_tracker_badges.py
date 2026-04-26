"""
Tests for tracker question-type badge derivation logic.

Run: python -m pytest tests/test_tracker_badges.py -v
"""

# ---------------------------------------------------------------------------
# The question_type derivation is embedded in morning_tracker(); we test the
# same conditional logic here as a standalone function so we don't need to
# spin up Flask or hit the Parliament API.
# ---------------------------------------------------------------------------

def derive_question_type(val: dict) -> str:
    """Mirror of the derivation in tracker.py morning_tracker()."""
    if val.get('house') == 'Lords':
        return 'LORDS'
    if val.get('isNamedDay'):
        return 'NAMED_DAY'
    return 'ORDINARY'


class TestQuestionTypeBadge:
    def test_lords_question(self):
        val = {'house': 'Lords', 'isNamedDay': False, 'uin': 'HL16701'}
        assert derive_question_type(val) == 'LORDS'

    def test_lords_question_ignores_named_day_flag(self):
        # A Lords question cannot be Named Day — Lords flag takes precedence
        val = {'house': 'Lords', 'isNamedDay': True, 'uin': 'HL99999'}
        assert derive_question_type(val) == 'LORDS'

    def test_named_day_commons(self):
        val = {'house': 'Commons', 'isNamedDay': True, 'uin': '129665'}
        assert derive_question_type(val) == 'NAMED_DAY'

    def test_ordinary_commons(self):
        val = {'house': 'Commons', 'isNamedDay': False, 'uin': '129633'}
        assert derive_question_type(val) == 'ORDINARY'

    def test_missing_house_defaults_to_ordinary(self):
        # API might omit house on some rows; should not crash
        val = {'isNamedDay': False, 'uin': '000000'}
        assert derive_question_type(val) == 'ORDINARY'

    def test_missing_is_named_day_defaults_to_ordinary(self):
        val = {'house': 'Commons'}
        assert derive_question_type(val) == 'ORDINARY'


class TestTrackerBadgeTemplate:
    """Integration test: confirm badge CSS classes render in the tracker template."""

    def _make_app(self):
        from pathlib import Path
        from flask import Flask
        from flask_login import LoginManager
        from extensions import db as _db
        import stakeholder_directory.models  # noqa: F401
        import stakeholder_directory.ingesters.staging  # noqa: F401
        from tracker import tracker_bp

        project_root = Path(__file__).parents[1]
        app = Flask(__name__, template_folder=str(project_root / 'templates'))
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret'
        app.config['LOGIN_DISABLED'] = True

        _db.init_app(app)
        lm = LoginManager()
        lm.init_app(app)

        @lm.user_loader
        def load_user(uid):
            return None

        app.register_blueprint(tracker_bp)
        with app.app_context():
            _db.create_all()
        return app

    def _render_with_results(self, app, questions):
        """Render tracker.html with seeded grouped results inside a request context."""
        from flask import render_template

        grouped = {
            '2026-04-24': {
                'display_date': '24 April 2026',
                'themes': {'Test Theme': questions},
            }
        }

        with app.test_request_context('/tracker'):
            html = render_template(
                'tracker.html',
                sorted_grouped_results=grouped,
                error_message=None,
                departments={'All': ''},
                selected_dept='',
                is_post=True,
                sitting_day_used='2026-04-24',
                api_failed=False,
            )
        return html

    def _make_question(self, question_type):
        return {
            'dept': 'Test Dept',
            'uin': '999999',
            'member': 'Test MP',
            'member_id': 1,
            'house': 'Lords' if question_type == 'LORDS' else 'Commons',
            'text': 'Test question text',
            'raw_date': '2026-04-24',
            'date_asked': '24 April 2026',
            'due_date': '2026-04-24',
            'date_for_answer': '2026-04-27',
            'question_type': question_type,
            'is_answered': False,
            'status': 'UNANSWERED',
        }

    def test_named_day_badge_class_rendered(self):
        app = self._make_app()
        html = self._render_with_results(app, [self._make_question('NAMED_DAY')])
        assert 'wq-type-named_day' in html
        assert 'NAMED DAY' in html

    def test_lords_badge_class_rendered(self):
        app = self._make_app()
        html = self._render_with_results(app, [self._make_question('LORDS')])
        assert 'wq-type-lords' in html
        assert 'LORDS' in html

    def test_ordinary_badge_class_rendered(self):
        app = self._make_app()
        html = self._render_with_results(app, [self._make_question('ORDINARY')])
        assert 'wq-type-ordinary' in html
        assert 'ORDINARY' in html
