"""
SQLAlchemy models for the stakeholder directory.

Design spec: docs/stakeholder-directory-design.md, Section 4.
Enum-like columns are enforced at database level via explicit CHECK constraints
so that both SQLite (local) and PostgreSQL (Railway) reject invalid values.
"""
from datetime import datetime
from extensions import db
from stakeholder_directory.vocab import (
    ORG_TYPE_VALUES,
    SOURCE_TYPE_VALUES,
    SCOPE_VALUES,
    STATUS_VALUES,
    REGISTRATION_STATUS_VALUES,
    FLAG_TYPE_VALUES,
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
    # department and policy_area: plain String until YAML vocabs are populated
    department = db.Column(db.String(50), nullable=True)
    policy_area = db.Column(db.String(100), nullable=True)
    engagement_date = db.Column(db.Date, nullable=False, index=True)
    evidence_url = db.Column(db.String(500), nullable=True)
    inquiry_id = db.Column(db.String(200), nullable=True)
    cited_in_outcome = db.Column(db.Boolean, nullable=False, default=False)
    engagement_depth = db.Column(db.String(50), nullable=True)
    ingested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ingester_source = db.Column(db.String(100), nullable=False)

    flags = db.relationship('Flag', backref='engagement_ref', lazy=True)


class PolicyAreaTag(db.Model):
    __tablename__ = 'sd_policy_area_tag'
    __table_args__ = (
        db.UniqueConstraint('organisation_id', 'area', name='uq_policy_area_tag_org_area'),
    )

    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(
        db.Integer, db.ForeignKey('sd_organisation.id'), nullable=False, index=True
    )
    # area validates against policy_areas.yaml once that vocab is populated
    area = db.Column(db.String(100), nullable=False)


class Flag(db.Model):
    __tablename__ = 'sd_flag'
    __table_args__ = (
        db.CheckConstraint(_in_list('flag_type', FLAG_TYPE_VALUES), name='ck_sd_flag_type'),
    )

    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(
        db.Integer, db.ForeignKey('sd_organisation.id'), nullable=False, index=True
    )
    engagement_id = db.Column(
        db.Integer, db.ForeignKey('sd_engagement.id'), nullable=True
    )
    flag_type = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.Text, nullable=False)
    raised_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    raised_by = db.Column(db.String(100), nullable=False)
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    resolved_at = db.Column(db.DateTime, nullable=True)
