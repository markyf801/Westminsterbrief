"""
SQLAlchemy models for the stakeholder directory.

Design spec: docs/stakeholder-directory-design.md, Section 4.

Enum-like columns are enforced at database level via explicit CHECK constraints
(see __table_args__ on each model). CHECK constraint values are sourced from
YAML configs at import time; to update them, edit the relevant config/*.yaml
then run: python -m stakeholder_directory.migrations --sync-vocab

Columns whose vocabulary is intentionally deferred (policy_area, department,
area) use runtime guards via @validates decorators. Attempts to write
non-None values to these columns while their YAML vocab is empty will
raise VocabularyNotReadyError — this is intentional: populate the config
file before ingesting.

NAMING NOTE: Flag.engagement_ref is the correct accessor for the engagement
relationship. The backref cannot be named 'engagement' because that would
collide with the 'engagement_id' FK column name on the same model.
Do not use flag.engagement in calling code — use flag.engagement_ref.
"""
from datetime import datetime
from extensions import db
from sqlalchemy.orm import validates
from stakeholder_directory.vocab import (
    ORG_TYPE_VALUES,
    SOURCE_TYPE_VALUES,
    SCOPE_VALUES,
    STATUS_VALUES,
    REGISTRATION_STATUS_VALUES,
    FLAG_TYPE_VALUES,
    validate_against_vocab,
)


def _in_list(col: str, values: tuple[str, ...]) -> str:
    """Generate SQL CHECK expression: col IN ('a', 'b', ...)"""
    quoted = ', '.join(f"'{v}'" for v in values)
    return f"{col} IN ({quoted})"


class Organisation(db.Model):
    __tablename__ = 'sd_organisation'
    __table_args__ = (
        db.CheckConstraint(_in_list('type', ORG_TYPE_VALUES), name='ck_sd_org_type'),
        db.CheckConstraint(_in_list('scope', SCOPE_VALUES), name='ck_sd_org_scope'),
        db.CheckConstraint(_in_list('status', STATUS_VALUES), name='ck_sd_org_status'),
        db.CheckConstraint(
            f"registration_status IS NULL OR {_in_list('registration_status', REGISTRATION_STATUS_VALUES)}",
            name='ck_sd_org_reg_status',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    canonical_name = db.Column(db.String(300), nullable=False, index=True)
    canonical_url = db.Column(db.String(500), nullable=True)
    description = db.Column(db.Text, nullable=True)
    type = db.Column(db.String(50), nullable=False)
    scope = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='active')
    registration_status = db.Column(db.String(50), nullable=True)
    registration_number = db.Column(db.String(50), nullable=True)
    last_verified = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    aliases = db.relationship('Alias', backref='organisation', lazy=True, cascade='all, delete-orphan')
    engagements = db.relationship('Engagement', backref='organisation', lazy=True, cascade='all, delete-orphan')
    policy_area_tags = db.relationship('PolicyAreaTag', backref='organisation', lazy=True, cascade='all, delete-orphan')
    flags = db.relationship('Flag', backref='organisation', lazy=True, cascade='all, delete-orphan')


class Alias(db.Model):
    __tablename__ = 'sd_alias'
    __table_args__ = (
        db.UniqueConstraint('organisation_id', 'alias_name', name='uq_alias_org_name'),
    )

    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(
        db.Integer, db.ForeignKey('sd_organisation.id'), nullable=False, index=True
    )
    alias_name = db.Column(db.String(300), nullable=False)
    source = db.Column(db.String(100), nullable=False)


class Engagement(db.Model):
    __tablename__ = 'sd_engagement'
    __table_args__ = (
        db.CheckConstraint(_in_list('source_type', SOURCE_TYPE_VALUES), name='ck_sd_eng_source_type'),
    )

    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(
        db.Integer, db.ForeignKey('sd_organisation.id'), nullable=False, index=True
    )
    source_type = db.Column(db.String(50), nullable=False)
    source_url = db.Column(db.String(500), nullable=False)
    # department and policy_area: guarded by @validates below until YAML vocabs are populated
    department = db.Column(db.String(50), nullable=True)
    policy_area = db.Column(db.String(100), nullable=True)
    engagement_date = db.Column(db.Date, nullable=False, index=True)
    evidence_url = db.Column(db.String(500), nullable=True)
    inquiry_id = db.Column(db.String(200), nullable=True)
    committee_id = db.Column(db.Integer, nullable=True)
    committee_name = db.Column(db.String(200), nullable=True)
    cited_in_outcome = db.Column(db.Boolean, nullable=False, default=False)
    engagement_depth = db.Column(db.String(50), nullable=True)
    engagement_subject = db.Column(db.Text, nullable=True)
    inquiry_status = db.Column(db.String(20), nullable=True)
    ingested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ingester_source = db.Column(db.String(100), nullable=False)

    flags = db.relationship('Flag', backref='engagement_ref', lazy=True)

    @validates('department')
    def _guard_department(self, key: str, value):
        if value is not None:
            validate_against_vocab(value, 'departments')
        return value

    @validates('policy_area')
    def _guard_policy_area(self, key: str, value):
        if value is not None:
            validate_against_vocab(value, 'policy_areas')
        return value


class PolicyAreaTag(db.Model):
    __tablename__ = 'sd_policy_area_tag'
    __table_args__ = (
        db.UniqueConstraint('organisation_id', 'area', name='uq_policy_area_tag_org_area'),
    )

    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(
        db.Integer, db.ForeignKey('sd_organisation.id'), nullable=False, index=True
    )
    # area: guarded by @validates below until policy_areas.yaml is populated
    area = db.Column(db.String(100), nullable=False)

    @validates('area')
    def _guard_area(self, key: str, value):
        if value is not None:
            validate_against_vocab(value, 'policy_areas')
        return value


class Flag(db.Model):
    __tablename__ = 'sd_flag'
    __table_args__ = (
        db.CheckConstraint(_in_list('flag_type', FLAG_TYPE_VALUES), name='ck_sd_flag_type'),
    )

    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(
        db.Integer, db.ForeignKey('sd_organisation.id'), nullable=False, index=True
    )
    # See NAMING NOTE at top of file: use flag.engagement_ref, not flag.engagement
    engagement_id = db.Column(
        db.Integer, db.ForeignKey('sd_engagement.id'), nullable=True
    )
    flag_type = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.Text, nullable=False)
    raised_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    raised_by = db.Column(db.String(100), nullable=False)
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    resolved_at = db.Column(db.DateTime, nullable=True)


class IngestionRun(db.Model):
    """Audit log — one row per end-to-end pipeline execution."""
    __tablename__ = 'sd_ingestion_run'

    id = db.Column(db.Integer, primary_key=True)
    run_at = db.Column(db.DateTime, nullable=False, index=True)
    script_invocation = db.Column(db.String(500), nullable=True)
    source_files = db.Column(db.JSON, nullable=True)
    department = db.Column(db.String(50), nullable=True)
    rows_ingested = db.Column(db.Integer, nullable=True)
    rows_committed = db.Column(db.Integer, nullable=True)
    organisations_created = db.Column(db.Integer, nullable=True)
    engagements_created = db.Column(db.Integer, nullable=True)
    aliases_created = db.Column(db.Integer, nullable=True)
    flags_created = db.Column(db.Integer, nullable=True)
    errors = db.Column(db.JSON, nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.Text, nullable=True)
