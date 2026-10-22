"""
Divisions (recorded votes) in both Houses — database models.

Filled offline by scripts/ingest_divisions.py from the Commons Votes API
(commonsvotes-api.parliament.uk) and the Lords Votes API
(lordsvotes-api.parliament.uk). The pages in divisions.py read only from these
tables.

On production the tables are created by migrations/division_tables.py (the web
service and crons run with SKIP_MIGRATIONS=1, so db.create_all() never creates
them there). Kept out of hansard_archive/models.py so this feature is
self-contained.

Votes are stored as 'aye' / 'no' for both Houses so they can be queried
together; the Lords equivalents are Content / Not content (VOTE_LABELS).
member_id is the MNIS id (members.parliament.uk/member/<id>). No FK: not every
member is in cached_member / member_profile.
"""

from datetime import datetime

from extensions import db

HOUSES = ("Commons", "Lords")

VOTE_AYE = "aye"
VOTE_NO = "no"

# What each House calls the two lobbies.
VOTE_LABELS = {
    "Commons": {VOTE_AYE: "Aye", VOTE_NO: "No"},
    "Lords": {VOTE_AYE: "Content", VOTE_NO: "Not content"},
}


class HaDivision(db.Model):
    """
    One division in the Commons or Lords.

    parliament_division_id is the Votes API id; it is unique within a House only
    (the two APIs number independently), hence the internal id.
    ayes_count / noes_count are the official totals as published: in the Commons
    they exclude the tellers, in the Lords they include them.
    hansard_* come from Hansard's /search/divisions.json, matched on
    (House, date, division number); ha_session_id is our ha_session row for that
    debate when we hold it.
    """

    __tablename__ = "ha_division"
    __table_args__ = (
        db.UniqueConstraint("house", "parliament_division_id", name="uq_ha_division_house_pid"),
        db.Index("idx_ha_division_date", "date"),
        db.Index("idx_ha_division_house_date", "house", "date"),
        db.Index("idx_ha_division_session", "ha_session_id"),
    )

    id                       = db.Column(db.Integer, primary_key=True)
    house                    = db.Column(db.String(10), nullable=False)          # Commons | Lords
    parliament_division_id   = db.Column(db.Integer, nullable=False)
    number                   = db.Column(db.Integer, nullable=True)              # Commons: per session; Lords: per day
    date                     = db.Column(db.Date, nullable=False)
    title                    = db.Column(db.Text, nullable=False)
    motion_text              = db.Column(db.Text, nullable=True)                 # question put (Hansard / Lords notes), plain text
    outcome_text             = db.Column(db.Text, nullable=True)                 # e.g. "Question accordingly negatived." (Hansard)
    ayes_count               = db.Column(db.Integer, nullable=False, default=0)
    noes_count               = db.Column(db.Integer, nullable=False, default=0)
    is_deferred              = db.Column(db.Boolean, nullable=False, default=False)
    official_url             = db.Column(db.Text, nullable=False)
    hansard_debate_ext_id    = db.Column(db.String(64), nullable=True)
    hansard_division_ext_id  = db.Column(db.String(64), nullable=True)
    hansard_debate_title     = db.Column(db.Text, nullable=True)
    ha_session_id            = db.Column(db.Integer, nullable=True)              # ha_session.id (no FK: archive rows are managed separately)
    source_updated           = db.Column(db.DateTime, nullable=True)             # Commons PublicationUpdated
    votes_hash               = db.Column(db.String(40), nullable=True)           # sha1 of the vote lists, to skip unchanged rewrites
    vote_rows                = db.Column(db.Integer, nullable=False, default=0)
    raw_data                 = db.Column(db.JSON, nullable=True)                 # API summary without the member lists
    ingested_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed           = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at               = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                         onupdate=datetime.utcnow)

    votes = db.relationship(
        "HaDivisionVote",
        backref="division",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def house_slug(self) -> str:
        return (self.house or "").lower()

    @property
    def path(self) -> str:
        return f"/divisions/{self.house_slug}/{self.parliament_division_id}"

    def __repr__(self):
        return f"<HaDivision {self.house} {self.parliament_division_id} {self.date} {self.title[:50]!r}>"


class HaDivisionVote(db.Model):
    """
    One member's vote in a division. Name, party and constituency / peerage
    type are as the Votes API gave them when we downloaded the division.
    The primary key includes the vote because an MP can (rarely) vote in both
    lobbies to record an active abstention. Tellers are rows with is_teller.
    """

    __tablename__ = "ha_division_vote"
    __table_args__ = (
        db.Index("idx_ha_division_vote_member", "member_id", "division_id"),
    )

    division_id  = db.Column(db.Integer, db.ForeignKey("ha_division.id", ondelete="CASCADE"),
                             primary_key=True, autoincrement=False)
    member_id    = db.Column(db.Integer, primary_key=True, autoincrement=False)
    vote         = db.Column(db.String(4), primary_key=True)                     # aye | no
    member_name  = db.Column(db.Text, nullable=False)
    list_as      = db.Column(db.Text, nullable=True)                             # sort name, e.g. "Corbyn, Jeremy"
    party        = db.Column(db.Text, nullable=True)
    member_from  = db.Column(db.Text, nullable=True)                             # constituency, or e.g. "Life peer"
    is_teller    = db.Column(db.Boolean, nullable=False, default=False)
    proxy_name   = db.Column(db.Text, nullable=True)                             # Commons proxy vote cast by

    def __repr__(self):
        return f"<HaDivisionVote div={self.division_id} {self.member_name!r} {self.vote}>"
