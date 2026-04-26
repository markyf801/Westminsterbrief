"""
Commits a staging record into the main organisation/engagement tables.

Called by the normaliser after a canonical organisation has been determined
(either an existing one matched via Tier 1/2, or a new one created via Tier 4).
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def commit_staging_record(staging_row, org, dry_run: bool = False) -> object | None:
    """Create an Engagement from a staging row linked to org.

    Also adds an Alias if the staging name differs from org.canonical_name
    and isn't already recorded. Marks the staging row as 'committed'.

    Returns the Engagement (or None if dry_run).
    """
    from extensions import db
    from stakeholder_directory.models import Engagement, Alias
    from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

    if dry_run:
        return None

    # Add alias if staging name != canonical and not already known
    raw_name = staging_row.raw_organisation_name
    if raw_name and raw_name.lower() != org.canonical_name.lower():
        exists = db.session.query(Alias).filter_by(
            organisation_id=org.id,
            alias_name=raw_name,
        ).first()
        if not exists:
            db.session.add(Alias(
                organisation_id=org.id,
                alias_name=raw_name,
                source='ministerial_meeting_normaliser',
            ))

    engagement = Engagement(
        organisation_id=org.id,
        source_type='ministerial_meeting',
        source_url=staging_row.source_url,
        department=staging_row.department,
        engagement_date=staging_row.meeting_date,
        engagement_subject=staging_row.meeting_purpose or None,
        ingested_at=datetime.utcnow(),
        ingester_source='ministerial_meeting_normaliser',
    )
    db.session.add(engagement)
    staging_row.processing_status = 'committed'

    return engagement


def commit_committee_evidence_record(staging_row, org, dry_run: bool = False) -> object | None:
    """Create an Engagement from a committee evidence staging row linked to org.

    Also adds an Alias if the staging name differs from org.canonical_name
    and isn't already recorded. Marks the staging row as 'committed'.

    Returns the Engagement (or None if dry_run).
    """
    from extensions import db
    from stakeholder_directory.models import Engagement, Alias

    if dry_run:
        return None

    raw_name = staging_row.raw_organisation_name
    if raw_name and raw_name.lower() != org.canonical_name.lower():
        exists = db.session.query(Alias).filter_by(
            organisation_id=org.id,
            alias_name=raw_name,
        ).first()
        if not exists:
            db.session.add(Alias(
                organisation_id=org.id,
                alias_name=raw_name,
                source='committee_evidence_normaliser',
            ))

    engagement = Engagement(
        organisation_id=org.id,
        source_type=staging_row.publication_type,
        source_url=staging_row.source_url,
        engagement_date=staging_row.publication_date,
        committee_id=staging_row.committee_id,
        committee_name=staging_row.committee_name,
        inquiry_id=staging_row.inquiry_id,
        engagement_subject=staging_row.inquiry_title or None,
        ingested_at=datetime.utcnow(),
        ingester_source='committee_evidence_normaliser',
    )
    db.session.add(engagement)
    staging_row.processing_status = 'committed'

    return engagement


def commit_lobbying_record(staging_row, org, dry_run: bool = False) -> object | None:
    """Create an Engagement from a lobbying register staging row linked to org.

    record_type='firm'   → source_type='lobbying_register'
    record_type='client' → source_type='lobbying_register_client'

    Also adds an Alias if the staging name differs from org.canonical_name.
    Marks the staging row as 'committed'.
    """
    from extensions import db
    from stakeholder_directory.models import Engagement, Alias

    if dry_run:
        return None

    raw_name = staging_row.raw_organisation_name
    if raw_name and raw_name.lower() != org.canonical_name.lower():
        exists = db.session.query(Alias).filter_by(
            organisation_id=org.id,
            alias_name=raw_name,
        ).first()
        if not exists:
            db.session.add(Alias(
                organisation_id=org.id,
                alias_name=raw_name,
                source='lobbying_register_normaliser',
            ))

    source_type = (
        'lobbying_register' if staging_row.record_type == 'firm'
        else 'lobbying_register_client'
    )
    if staging_row.record_type == 'client' and staging_row.firm_name:
        subject = f'Represented by {staging_row.firm_name}'
        if staging_row.subject_matters:
            subject += f' — {staging_row.subject_matters}'
    else:
        subject = staging_row.subject_matters or None
    engagement = Engagement(
        organisation_id=org.id,
        source_type=source_type,
        source_url=staging_row.source_url,
        engagement_date=staging_row.quarter_start_date,
        engagement_subject=subject,
        ingested_at=datetime.utcnow(),
        ingester_source='lobbying_register_normaliser',
    )
    db.session.add(engagement)
    staging_row.processing_status = 'committed'

    return engagement
