"""
Staging table models for the stakeholder directory ingesters.

Ingesters write raw source data here first. A separate normalisation pass
resolves raw_organisation_name to canonical_name and populates the main
sd_organisation / sd_engagement tables.
"""
from datetime import datetime
from extensions import db
from stakeholder_directory.vocab import STAGING_STATUS_VALUES


def _in_list(col: str, values: tuple[str, ...]) -> str:
    quoted = ', '.join(f"'{v}'" for v in values)
    return f"{col} IN ({quoted})"


class StagingMinisterialMeeting(db.Model):
    __tablename__ = 'sd_staging_ministerial_meeting'
    __table_args__ = (
        db.CheckConstraint(
            _in_list('processing_status', STAGING_STATUS_VALUES),
            name='ck_sd_staging_min_status',
        ),
        db.UniqueConstraint(
            'raw_organisation_name', 'minister_name', 'meeting_date', 'source_url',
            name='uq_sd_staging_min_meeting',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    raw_organisation_name = db.Column(db.String(300), nullable=False, index=True)
    minister_name = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(50), nullable=False)
    meeting_date = db.Column(db.Date, nullable=False, index=True)
    meeting_purpose = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(500), nullable=False)
    source_csv_row = db.Column(db.Text, nullable=False)
    ingested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    processing_status = db.Column(db.String(30), nullable=False, default='pending')
    processing_notes = db.Column(db.Text, nullable=True)
