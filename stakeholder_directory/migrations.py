"""
Idempotent migration script for the stakeholder directory tables.

Run standalone:   python stakeholder_directory/migrations.py
Call from app:    from stakeholder_directory.migrations import run_migrations; run_migrations(app)

Uses db.create_all() which is safe to call multiple times — it only creates
tables that do not already exist. No Alembic required.
"""
import os
import sys

# Ensure project root is on sys.path when running as a standalone script
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def run_migrations(app):
    """
    Create all stakeholder_directory tables within an existing Flask app context.
    Safe to call multiple times — skips tables that already exist.
    """
    from extensions import db
    import stakeholder_directory.models  # noqa: F401 — registers models with db.metadata

    with app.app_context():
        db.create_all()
        print('[stakeholder_directory] tables: OK (created or already existed)')


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()

    from flask import Flask
    from extensions import db

    _app = Flask(__name__, root_path=_PROJECT_ROOT)

    _db_url = os.environ.get(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(_PROJECT_ROOT, 'intelligence.db')
    )
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

    _app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'migrations-dev-key')

    db.init_app(_app)
    run_migrations(_app)
    print('[stakeholder_directory] migrations complete.')
