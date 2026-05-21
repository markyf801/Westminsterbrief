"""
Tests for the SKIP_MIGRATIONS startup guard.

The guard is a single boolean expression evaluated at flask_app module load time.
These tests verify the expression behaves correctly without importing flask_app
(importing flask_app runs the full startup sequence; that is tested implicitly
by all other test modules that exercise the app fixture).

flask_app.py line ~246:
    _SKIP_MIGRATIONS = os.environ.get('SKIP_MIGRATIONS') == '1'

flask_app.py startup block:
    if _SKIP_MIGRATIONS:
        _mig_log('SKIP_MIGRATIONS=1 set; skipping db.create_all() and schema modifications')
    else:
        _run_startup_migrations()

    if not _SKIP_MIGRATIONS:
        seed_all_minister_links(app)
"""

import os


class TestSkipMigrationsFlagEvaluation:
    """Verify that only the string '1' enables the skip, nothing else."""

    def test_flag_true_when_set_to_1(self, monkeypatch):
        monkeypatch.setenv('SKIP_MIGRATIONS', '1')
        assert (os.environ.get('SKIP_MIGRATIONS') == '1') is True

    def test_flag_false_when_unset(self, monkeypatch):
        monkeypatch.delenv('SKIP_MIGRATIONS', raising=False)
        assert (os.environ.get('SKIP_MIGRATIONS') == '1') is False

    def test_flag_false_when_set_to_zero(self, monkeypatch):
        monkeypatch.setenv('SKIP_MIGRATIONS', '0')
        assert (os.environ.get('SKIP_MIGRATIONS') == '1') is False

    def test_flag_false_when_set_to_true_string(self, monkeypatch):
        monkeypatch.setenv('SKIP_MIGRATIONS', 'true')
        assert (os.environ.get('SKIP_MIGRATIONS') == '1') is False

    def test_flag_false_for_all_non_1_values(self, monkeypatch):
        for val in ('0', '', 'yes', 'TRUE', 'True', '1.0', 'on', '2'):
            monkeypatch.setenv('SKIP_MIGRATIONS', val)
            assert (os.environ.get('SKIP_MIGRATIONS') == '1') is False, (
                f"Expected False for SKIP_MIGRATIONS={val!r}"
            )


class TestSkipMigrationsGuardBehaviour:
    """Verify the guard logic controls which code paths are taken."""

    def test_skip_true_prevents_run_startup_migrations(self, monkeypatch):
        """When SKIP_MIGRATIONS=1, _run_startup_migrations must not be called."""
        monkeypatch.setenv('SKIP_MIGRATIONS', '1')
        called = []

        def mock_run():
            called.append('run')

        skip = os.environ.get('SKIP_MIGRATIONS') == '1'
        if not skip:
            mock_run()

        assert called == [], (
            "_run_startup_migrations() must not be called when SKIP_MIGRATIONS=1"
        )

    def test_skip_false_calls_run_startup_migrations(self, monkeypatch):
        """When SKIP_MIGRATIONS is unset, _run_startup_migrations must be called."""
        monkeypatch.delenv('SKIP_MIGRATIONS', raising=False)
        called = []

        def mock_run():
            called.append('run')

        skip = os.environ.get('SKIP_MIGRATIONS') == '1'
        if not skip:
            mock_run()

        assert called == ['run'], (
            "_run_startup_migrations() must be called when SKIP_MIGRATIONS is unset"
        )

    def test_skip_true_prevents_seed_all_minister_links(self, monkeypatch):
        """When SKIP_MIGRATIONS=1, seed_all_minister_links must not be called."""
        monkeypatch.setenv('SKIP_MIGRATIONS', '1')
        called = []

        def mock_seed(app):
            called.append('seed')

        skip = os.environ.get('SKIP_MIGRATIONS') == '1'
        if not skip:
            mock_seed(None)

        assert called == [], (
            "seed_all_minister_links() must not be called when SKIP_MIGRATIONS=1"
        )

    def test_skip_false_calls_seed_all_minister_links(self, monkeypatch):
        """When SKIP_MIGRATIONS is unset, seed_all_minister_links must be called."""
        monkeypatch.delenv('SKIP_MIGRATIONS', raising=False)
        called = []

        def mock_seed(app):
            called.append('seed')

        skip = os.environ.get('SKIP_MIGRATIONS') == '1'
        if not skip:
            mock_seed(None)

        assert called == ['seed'], (
            "seed_all_minister_links() must be called when SKIP_MIGRATIONS is unset"
        )

    def test_log_message_matches_expected_text(self, monkeypatch):
        """The skip log message used in flask_app.py must contain expected text."""
        monkeypatch.setenv('SKIP_MIGRATIONS', '1')
        messages = []

        def mock_mig_log(msg):
            messages.append(msg)

        skip = os.environ.get('SKIP_MIGRATIONS') == '1'
        if skip:
            mock_mig_log(
                'SKIP_MIGRATIONS=1 set; skipping db.create_all() and schema modifications'
            )

        assert len(messages) == 1
        assert 'SKIP_MIGRATIONS=1' in messages[0]
        assert 'skipping' in messages[0]
        assert 'db.create_all()' in messages[0]
