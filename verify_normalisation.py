"""
End-to-end verification run: ingest 4 DfE quarterly CSVs then normalise.
Uses the pipeline module so the same code path is exercised as in production.

Run:
    python verify_normalisation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask
from extensions import db
import stakeholder_directory.models  # noqa: F401
import stakeholder_directory.ingesters.staging  # noqa: F401

_DB_PATH = Path(__file__).parent / 'instance' / 'dfe_real_run2.db'
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{_DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

DOWNLOADS = Path('downloads/dfe_meetings')
DEPT = 'department_for_education'

# Quarterly publication URLs for DfE ministerial meetings transparency data.
# Pattern: https://www.gov.uk/government/publications/dfe-ministerial-overseas-travel-and-meetings-{period}-{year}
# Verify each URL resolves before re-ingesting; gov.uk slugs vary across departments.
QUARTERLY_URLS = {
    'q1_2025': 'https://www.gov.uk/government/publications/dfe-ministerial-overseas-travel-and-meetings-january-to-march-2025',
    'q2_2025': 'https://www.gov.uk/government/publications/dfe-ministerial-overseas-travel-and-meetings-april-to-june-2025',
    'q3_2025': 'https://www.gov.uk/government/publications/dfe-ministerial-overseas-travel-and-meetings-july-to-september-2025',
    'q4_2025': 'https://www.gov.uk/government/publications/dfe-ministerial-overseas-travel-and-meetings-october-to-december-2025',
}

CSV_PAIRS = [
    (DOWNLOADS / 'dfe_meetings_q1_2025.csv', QUARTERLY_URLS['q1_2025']),
    (DOWNLOADS / 'dfe_meetings_q2_2025.csv', QUARTERLY_URLS['q2_2025']),
    (DOWNLOADS / 'dfe_meetings_q3_2025.csv', QUARTERLY_URLS['q3_2025']),
    (DOWNLOADS / 'dfe_meetings_q4_2025.csv', QUARTERLY_URLS['q4_2025']),
]


def run():
    from stakeholder_directory.migrations import run_migrations
    run_migrations(app)

    from stakeholder_directory.pipeline import run_pipeline

    log_row = run_pipeline(CSV_PAIRS, DEPT, app)

    print("\n" + "=" * 60)
    print("SD_INGESTION_RUN ROW")
    print("=" * 60)
    with app.app_context():
        from stakeholder_directory.models import IngestionRun, Organisation, Engagement, Flag

        row = db.session.query(IngestionRun).order_by(IngestionRun.id.desc()).first()
        print(f"  id:                   {row.id}")
        print(f"  run_at:               {row.run_at}")
        print(f"  department:           {row.department}")
        print(f"  rows_ingested:        {row.rows_ingested}")
        print(f"  rows_committed:       {row.rows_committed}")
        print(f"  organisations_created:{row.organisations_created}")
        print(f"  engagements_created:  {row.engagements_created}")
        print(f"  aliases_created:      {row.aliases_created}")
        print(f"  flags_created:        {row.flags_created}")
        print(f"  errors:               {row.errors}")
        print(f"  duration_seconds:     {row.duration_seconds}")
        print(f"  source_files:         {row.source_files}")

        print("\n" + "=" * 60)
        print("TOTAL INGESTION_RUN ROWS IN DB")
        print("=" * 60)
        total_runs = db.session.query(IngestionRun).count()
        print(f"  {total_runs}")


if __name__ == '__main__':
    run()
