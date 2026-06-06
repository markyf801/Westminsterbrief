"""
Pytest session configuration.

Force an in-memory SQLite database for the whole test session BEFORE any test
module imports the real ``flask_app``.

Why this exists
---------------
``flask_app`` binds its SQLAlchemy engine at import time to whatever
``DATABASE_URL`` is set. During local dev that is often a persistent
``instance/*.db`` file left over from an import-check (the CLAUDE.md pre-push
step uses ``DATABASE_URL=sqlite:///local_check.db``). SQLAlchemy's
``create_all()`` creates *missing* tables but never ALTERs an existing one to
add a column — so once a model gains a column (e.g. ``last_rediscovered_at``),
the stale file still has the old table and tests fail with a misleading
``no such column`` until someone manually deletes the file.

That made the producer-authorisation suite show 13 spurious failures on
2026-06-06 and is exactly the "normalised red suite that hides a real failure"
trap. Forcing a fresh ``:memory:`` DB each run means the schema always reflects
the current models, so a red test means a real problem — not a stale local file.

This must run before test collection, hence module-top in ``conftest.py``.
Tests that build their own Flask app (e.g. ``test_rediscovery``,
``test_discovery_worker``) don't read this and are unaffected; only the
real-``flask_app`` tests pick it up.
"""
import os

# Force (not setdefault) so a DATABASE_URL inherited from the shell/.env — which
# would otherwise bind flask_app to a persistent file — cannot leak into tests.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SKIP_MIGRATIONS"] = "0"
