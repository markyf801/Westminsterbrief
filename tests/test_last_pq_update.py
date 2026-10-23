"""
"Archive updated" time on /questions: the newest ok run of any PQ cron.

Run: python -m pytest tests/test_last_pq_update.py -v
"""

from datetime import datetime

from test_routes import app  # noqa: F401  (module-scoped seeded test app)


def test_last_pq_update_reads_the_recorded_cron_names(app):  # noqa: F811
    from extensions import db
    from hansard import _last_pq_update
    from hansard_archive.models import HaCronRun
    with app.app_context():
        db.session.add_all([
            HaCronRun(service_name="pq-cron-afternoon", started_at=datetime(2026, 10, 22, 14),
                      finished_at=datetime(2026, 10, 22, 14, 41), status="ok"),
            HaCronRun(service_name="pq-cron-early", started_at=datetime(2026, 10, 23, 8, 30),
                      finished_at=None, status="running"),
            HaCronRun(service_name="morning-catchup", started_at=datetime(2026, 10, 23, 8),
                      finished_at=datetime(2026, 10, 23, 8, 5), status="ok"),
        ])
        db.session.commit()
        assert _last_pq_update() == "22 Oct 2026 14:41 UTC"
