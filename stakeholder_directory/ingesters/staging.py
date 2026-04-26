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


class StagingCommitteeEvidence(db.Model):
    __tablename__ = 'sd_staging_committee_evidence'
    __table_args__ = (
        db.CheckConstraint(
            _in_list('processing_status', STAGING_STATUS_VALUES),
            name='ck_sd_staging_ce_status',
        ),
        db.UniqueConstraint(
            'publication_id', 'raw_organisation_name',
            name='uq_sd_staging_ce_pub_org',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    committee_id = db.Column(db.Integer, nullable=False, index=True)
    committee_name = db.Column(db.String(200), nullable=False)
    committee_house = db.Column(db.String(20), nullable=False)
    publication_id = db.Column(db.Integer, nullable=False, index=True)
    publication_type = db.Column(db.String(50), nullable=False)
    publication_date = db.Column(db.Date, nullable=True, index=True)
    inquiry_id = db.Column(db.String(100), nullable=True)
    inquiry_title = db.Column(db.String(500), nullable=True)
    raw_organisation_name = db.Column(db.Text, nullable=False, index=True)
    attendee_role = db.Column(db.String(500), nullable=True)
    source_url = db.Column(db.String(500), nullable=False)
    processing_status = db.Column(db.String(30), nullable=False, default='pending')
    processing_notes = db.Column(db.Text, nullable=True)
    ingested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    source_json = db.Column(db.Text, nullable=True)


class StagingLobbyingEntry(db.Model):
    """One row per (firm, client, quarter, record_type) combination.

    The ingester creates two rows per register CSV line: one with
    record_type='firm' (raw_organisation_name=firm_name) and one with
    record_type='client' (raw_organisation_name=client_name).  The
    normaliser processes each row independently via the shared dedup pipeline.
    """
    __tablename__ = 'sd_staging_lobbying_entry'
    __table_args__ = (
        db.CheckConstraint(
            _in_list('processing_status', STAGING_STATUS_VALUES),
            name='ck_sd_staging_le_status',
        ),
        db.UniqueConstraint(
            'firm_name', 'client_name', 'quarter', 'record_type',
            name='uq_sd_staging_le_firm_client_quarter_type',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    firm_name = db.Column(db.String(500), nullable=False)
    firm_registration_id = db.Column(db.String(100), nullable=True)
    client_name = db.Column(db.String(500), nullable=False)
    record_type = db.Column(db.String(10), nullable=False)   # 'firm' or 'client'
    raw_organisation_name = db.Column(db.String(500), nullable=False, index=True)
    quarter = db.Column(db.String(10), nullable=False)
    quarter_start_date = db.Column(db.Date, nullable=False)
    quarter_end_date = db.Column(db.Date, nullable=False)
    subject_matters = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(500), nullable=False)
    processing_status = db.Column(db.String(20), nullable=False, default='pending')
    processing_notes = db.Column(db.Text, nullable=True)
    ingested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


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
    raw_organisation_name = db.Column(db.Text, nullable=False, index=True)
    minister_name = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(50), nullable=False)
    meeting_date = db.Column(db.Date, nullable=True, index=True)
    meeting_purpose = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(500), nullable=False)
    source_csv_row = db.Column(db.Text, nullable=False)
    ingested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    processing_status = db.Column(db.String(30), nullable=False, default='pending')
    processing_notes = db.Column(db.Text, nullable=True)
