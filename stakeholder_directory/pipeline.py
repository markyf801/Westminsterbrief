"""
End-to-end pipeline runner for the stakeholder directory.

Runs ingestion + normalisation for a set of source CSVs, writes one
sd_ingestion_run audit row per invocation, and prints a brief summary.

Usage (standalone — see verify_normalisation.py for a worked example):

    from stakeholder_directory.pipeline import run_pipeline

    log_row = run_pipeline(
        csv_pairs=[
            ('downloads/dfe_meetings_q1_2025.csv', 'https://gov.uk/.../q1'),
        ],
        department='department_for_education',
        app=flask_app,
    )
    print(log_row)
"""
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def run_pipeline(
    csv_pairs: list[tuple],
    department: str,
    app,
    notes: str | None = None,
) -> object:
    """Ingest + normalise, write an sd_ingestion_run log row, return it.

    Args:
        csv_pairs:  List of (csv_path, source_url) tuples.
        department: Snake-case dept key (e.g. 'department_for_education').
        app:        Flask app with an active or push-able app context.
        notes:      Optional free-text note stored in the log row.

    Returns:
        The IngestionRun ORM object just written.
    """
    with app.app_context():
        from extensions import db
        from stakeholder_directory.models import (
            IngestionRun, Organisation, Engagement, Alias, Flag,
        )
        from stakeholder_directory.ingesters.ministerial_meetings import (
            ingest_ministerial_meetings,
        )
        from stakeholder_directory.normalisation.normaliser import (
            normalise_pending_staging,
        )

        # Print last-run summary
        last = (
            db.session.query(IngestionRun)
            .order_by(IngestionRun.run_at.desc())
            .first()
        )
        if last:
            print(
                f"Last successful run: {last.run_at.strftime('%Y-%m-%d %H:%M:%S')}; "
                f"processed {last.rows_ingested or 0} rows. Current run starting."
            )
        else:
            print("No previous runs found. Current run starting.")

        run_at = datetime.utcnow()
        start = time.monotonic()

        # Snapshot counts before normalisation
        orgs_before = db.session.query(Organisation).count()
        eng_before = db.session.query(Engagement).count()
        alias_before = db.session.query(Alias).count()
        flag_before = db.session.query(Flag).count()

        # Ingestion
        all_errors: list[str] = []
        total_ingested = 0
        source_file_list = []

        for csv_path, source_url in csv_pairs:
            result = ingest_ministerial_meetings(csv_path, department, source_url)
            total_ingested += result.rows_processed
            all_errors.extend(result.errors)
            source_file_list.append(str(csv_path))
            print(f"  {Path(csv_path).name}: {result}")

        # Normalisation
        norm = normalise_pending_staging('staging_ministerial_meeting', batch_size=2000)
        print(f"  Normalisation: {norm}")

        # Snapshot counts after
        orgs_after = db.session.query(Organisation).count()
        eng_after = db.session.query(Engagement).count()
        alias_after = db.session.query(Alias).count()
        flag_after = db.session.query(Flag).count()

        all_errors.extend(norm.errors)
        duration = int(time.monotonic() - start)

        log_row = IngestionRun(
            run_at=run_at,
            script_invocation=' '.join(sys.argv) if sys.argv else 'run_pipeline()',
            source_files=source_file_list,
            department=department,
            rows_ingested=total_ingested,
            rows_committed=norm.staging_records_processed,
            organisations_created=orgs_after - orgs_before,
            engagements_created=eng_after - eng_before,
            aliases_created=alias_after - alias_before,
            flags_created=flag_after - flag_before,
            errors=all_errors or None,
            duration_seconds=duration,
            notes=notes,
        )
        db.session.add(log_row)
        db.session.commit()

        print(
            f"\nRun complete in {duration}s. "
            f"Orgs +{orgs_after - orgs_before}, "
            f"engagements +{eng_after - eng_before}, "
            f"flags +{flag_after - flag_before}."
        )
        return log_row
