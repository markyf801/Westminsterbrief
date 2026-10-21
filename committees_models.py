"""
Committees and All-Party Parliamentary Groups (APPGs) — database models.

Filled offline by scripts/ingest_committees.py (Parliament Committees API) and
scripts/ingest_appgs.py (the Register of All-Party Parliamentary Groups). The
pages in committees.py read only from these tables.

On production the tables are created by migrations/committees_appgs_tables.py
(the web service and crons run with SKIP_MIGRATIONS=1, so db.create_all() never
creates them there). Kept out of hansard_archive/models.py so this feature is
self-contained.

member_id columns hold the MNIS id (members.parliament.uk/member/<id>). No FK:
not every member is in cached_member / member_profile.
"""

from datetime import datetime

from extensions import db


class Committee(db.Model):
    """A parliamentary committee. id is the Committees API id."""

    __tablename__ = "committee"
    __table_args__ = (
        db.Index("idx_committee_current", "is_current"),
        db.Index("idx_committee_house", "house"),
    )

    id              = db.Column(db.Integer, primary_key=True, autoincrement=False)
    name            = db.Column(db.Text, nullable=False)
    slug            = db.Column(db.Text, nullable=True)
    house           = db.Column(db.String(10), nullable=True)      # Commons | Lords | Joint
    category        = db.Column(db.String(20), nullable=True)      # Select | General | Other
    committee_types = db.Column(db.JSON, nullable=True)            # ["(HC) Public Standing Orders - Departmental", ...]
    parent_id       = db.Column(db.Integer, nullable=True)
    parent_name     = db.Column(db.Text, nullable=True)
    purpose         = db.Column(db.Text, nullable=True)            # plain text (API sends HTML)
    contact_email   = db.Column(db.Text, nullable=True)
    contact_phone   = db.Column(db.Text, nullable=True)
    contact_address = db.Column(db.Text, nullable=True)
    start_date      = db.Column(db.Date, nullable=True)
    end_date        = db.Column(db.Date, nullable=True)
    is_current      = db.Column(db.Boolean, nullable=False, default=True)
    show_on_website = db.Column(db.Boolean, nullable=False, default=True)
    topic_slugs     = db.Column(db.Text, nullable=True)            # ",education,health-and-social-care,"
    raw_data        = db.Column(db.JSON, nullable=True)
    ingested_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Committee {self.id} {self.name!r}>"


class CommitteeMembership(db.Model):
    """
    A current member of a committee (one row per person per committee).
    Replaced wholesale for a committee on every successful refresh.
    role = the most senior role held ("Chair" beats "Member"); roles = all of them.
    """

    __tablename__ = "committee_membership"
    __table_args__ = (
        db.Index("idx_committee_membership_committee", "committee_id"),
        db.Index("idx_committee_membership_member", "member_id"),
    )

    id            = db.Column(db.Integer, primary_key=True)
    committee_id  = db.Column(db.Integer, db.ForeignKey("committee.id", ondelete="CASCADE"), nullable=False)
    member_id     = db.Column(db.Integer, nullable=True)          # MNIS id; NULL for lay members
    name          = db.Column(db.Text, nullable=False)
    party         = db.Column(db.Text, nullable=True)
    member_from   = db.Column(db.Text, nullable=True)             # constituency / peerage type
    house         = db.Column(db.String(10), nullable=True)
    role          = db.Column(db.Text, nullable=False, default="Member")
    roles         = db.Column(db.JSON, nullable=True)
    is_chair      = db.Column(db.Boolean, nullable=False, default=False)
    is_lay_member = db.Column(db.Boolean, nullable=False, default=False)
    is_ex_officio = db.Column(db.Boolean, nullable=False, default=False)
    start_date    = db.Column(db.Date, nullable=True)
    end_date      = db.Column(db.Date, nullable=True)


class CommitteeInquiry(db.Model):
    """
    A piece of committee business (inquiry, non-inquiry session, legislative
    scrutiny, pre-appointment hearing ...). id is the Committees API
    CommitteeBusiness id. A joint inquiry is linked to several committees
    through committee_inquiry_link.

    evidence_open / evidence_deadline describe the open call(s) for evidence:
    evidence_deadline is the earliest end date of the open submission periods
    (NULL with evidence_open=True means "open, no deadline published").
    """

    __tablename__ = "committee_inquiry"
    __table_args__ = (
        db.Index("idx_committee_inquiry_status", "status"),
        db.Index("idx_committee_inquiry_evidence", "evidence_open", "evidence_deadline"),
    )

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=False)
    title               = db.Column(db.Text, nullable=False)
    slug                = db.Column(db.Text, nullable=True)
    business_type       = db.Column(db.String(60), nullable=True)
    is_inquiry          = db.Column(db.Boolean, nullable=False, default=False)
    status              = db.Column(db.String(10), nullable=False, default="Open")   # Open | Closed
    open_date           = db.Column(db.Date, nullable=True)
    close_date          = db.Column(db.Date, nullable=True)
    scope               = db.Column(db.Text, nullable=True)              # plain text
    evidence_open       = db.Column(db.Boolean, nullable=False, default=False)
    evidence_deadline   = db.Column(db.DateTime, nullable=True)
    submission_periods  = db.Column(db.JSON, nullable=True)              # open periods [{id, type, start, end}]
    next_oral_evidence  = db.Column(db.DateTime, nullable=True)
    latest_report       = db.Column(db.JSON, nullable=True)
    topic_slugs         = db.Column(db.Text, nullable=True)
    list_hash           = db.Column(db.String(64), nullable=True)        # change detection for detail refetch
    raw_data            = db.Column(db.JSON, nullable=True)
    detail_fetched_at   = db.Column(db.DateTime, nullable=True)
    ingested_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                    onupdate=datetime.utcnow)


class CommitteeInquiryLink(db.Model):
    """Which committees run an inquiry (joint inquiries have several rows)."""

    __tablename__ = "committee_inquiry_link"
    __table_args__ = (
        db.Index("idx_committee_inquiry_link_inquiry", "inquiry_id"),
    )

    committee_id = db.Column(db.Integer, db.ForeignKey("committee.id", ondelete="CASCADE"), primary_key=True)
    inquiry_id   = db.Column(db.Integer, db.ForeignKey("committee_inquiry.id", ondelete="CASCADE"), primary_key=True)


class CommitteePublication(db.Model):
    """A committee report, special report or government response (recent ones only)."""

    __tablename__ = "committee_publication"
    __table_args__ = (
        db.UniqueConstraint("publication_id", "committee_id", name="uq_committee_publication"),
        db.Index("idx_committee_publication_committee_date", "committee_id", "publication_date"),
    )

    id                = db.Column(db.Integer, primary_key=True)
    publication_id    = db.Column(db.Integer, nullable=False)
    committee_id      = db.Column(db.Integer, db.ForeignKey("committee.id", ondelete="CASCADE"), nullable=False)
    inquiry_id        = db.Column(db.Integer, nullable=True)
    inquiry_title     = db.Column(db.Text, nullable=True)
    publication_type  = db.Column(db.String(40), nullable=True)
    description       = db.Column(db.Text, nullable=False)
    publication_date  = db.Column(db.DateTime, nullable=True)
    paper_number      = db.Column(db.String(40), nullable=True)          # "HC 150" / "HL Paper 12"
    document_url      = db.Column(db.Text, nullable=True)
    response_deadline = db.Column(db.Date, nullable=True)                # government response due
    ingested_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                  onupdate=datetime.utcnow)


class Appg(db.Model):
    """
    An All-Party Parliamentary Group from the latest register edition.
    register_slug is the register's page name (e.g. "youth-employment"), stable
    across editions. Groups that drop out of the register keep their row with
    is_current=False.
    """

    __tablename__ = "appg"
    __table_args__ = (
        db.Index("idx_appg_current", "is_current"),
    )

    id                      = db.Column(db.Integer, primary_key=True)
    register_slug           = db.Column(db.String(200), nullable=False, unique=True)
    title                   = db.Column(db.Text, nullable=False)       # "All-Party Parliamentary Group for Youth Employment"
    short_name              = db.Column(db.Text, nullable=False)       # "Youth Employment"
    slug                    = db.Column(db.Text, nullable=True)
    category                = db.Column(db.String(40), nullable=True)  # Subject Group | Country Group
    purpose                 = db.Column(db.Text, nullable=True)
    secretariat             = db.Column(db.Text, nullable=True)
    website                 = db.Column(db.Text, nullable=True)
    registered_contact_name = db.Column(db.Text, nullable=True)
    agm_date                = db.Column(db.Date, nullable=True)
    benefits                = db.Column(db.JSON, nullable=True)        # [{kind, source, description, value, received, registered}]
    topic_slugs             = db.Column(db.Text, nullable=True)
    register_edition        = db.Column(db.Date, nullable=True)        # last edition the group appeared in
    first_seen_edition      = db.Column(db.Date, nullable=True)
    is_current              = db.Column(db.Boolean, nullable=False, default=True)
    source_url              = db.Column(db.Text, nullable=True)
    content_hash            = db.Column(db.String(64), nullable=True)
    ingested_at             = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                        onupdate=datetime.utcnow)


class AppgOfficer(db.Model):
    """
    One officer of an APPG as listed in the register, matched by name to a
    current member: high = exact name, medium = same name without titles or
    honours (or a peer's short title), low = fuzzy. Only high/medium are linked
    on pages (committees.LINKED_CONFIDENCE); low keeps the candidate member_id
    for review. Unmatched names are kept with member_id NULL, confidence 'none'.
    """

    __tablename__ = "appg_officer"
    __table_args__ = (
        db.Index("idx_appg_officer_appg", "appg_id"),
        db.Index("idx_appg_officer_member", "member_id"),
    )

    id                    = db.Column(db.Integer, primary_key=True)
    appg_id               = db.Column(db.Integer, db.ForeignKey("appg.id", ondelete="CASCADE"), nullable=False)
    role                  = db.Column(db.Text, nullable=False)        # as registered, e.g. "Chair & Registered Contact"
    is_chair              = db.Column(db.Boolean, nullable=False, default=False)
    is_registered_contact = db.Column(db.Boolean, nullable=False, default=False)
    name                  = db.Column(db.Text, nullable=False)        # as registered
    party                 = db.Column(db.Text, nullable=True)
    member_id             = db.Column(db.Integer, nullable=True)
    match_confidence      = db.Column(db.String(10), nullable=False, default="none")  # high | medium | low | none
    sort_order            = db.Column(db.Integer, nullable=False, default=0)
