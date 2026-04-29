"""
Hansard Archive database models — Phase 2A.

Three tables (all prefixed ha_):
  ha_session          — one row per Hansard debate section
  ha_contribution     — speeches within a session (flat; responds_to_id for Q&A pairing, Week 2)
  ha_session_theme    — AI theme tags (schema now; populated in Week 2)

Table prefix ha_ keeps archive tables clearly namespaced from the existing
application tables (user, tracked_topic, cached_*, sd_*, etc.).
"""

from datetime import datetime
from extensions import db


DEBATE_TYPE_ORAL_QUESTIONS = "oral_questions"
DEBATE_TYPE_PMQS = "pmqs"
DEBATE_TYPE_WESTMINSTER_HALL = "westminster_hall"
DEBATE_TYPE_DEBATE = "debate"
DEBATE_TYPE_MINISTERIAL_STATEMENT = "ministerial_statement"
DEBATE_TYPE_STATUTORY_INSTRUMENT = "statutory_instrument"
DEBATE_TYPE_COMMITTEE_STAGE = "committee_stage"
DEBATE_TYPE_PETITION = "petition"
DEBATE_TYPE_OTHER = "other"

DEBATE_TYPES = [
    DEBATE_TYPE_ORAL_QUESTIONS,
    DEBATE_TYPE_PMQS,
    DEBATE_TYPE_WESTMINSTER_HALL,
    DEBATE_TYPE_DEBATE,
    DEBATE_TYPE_MINISTERIAL_STATEMENT,
    DEBATE_TYPE_STATUTORY_INSTRUMENT,
    DEBATE_TYPE_COMMITTEE_STAGE,
    DEBATE_TYPE_PETITION,
    DEBATE_TYPE_OTHER,
]


class HansardSession(db.Model):
    """A single Hansard debate section (question time, debate, statement, etc.)."""

    __tablename__ = "ha_session"

    id = db.Column(db.Integer, primary_key=True)
    ext_id = db.Column(db.String(200), unique=True, nullable=False, index=True)
    title = db.Column(db.String(500), nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    house = db.Column(db.String(20), nullable=False)       # Commons | Lords
    debate_type = db.Column(db.String(50), nullable=True)  # controlled vocab above
    location = db.Column(db.String(100), nullable=True)    # Overview.Location e.g. "Westminster Hall"
    hrs_tag = db.Column(db.String(100), nullable=True)     # Overview.HRSTag e.g. "hs_8Question"
    hansard_url = db.Column(db.String(500), nullable=True)
    contributions_ingested = db.Column(db.Boolean, nullable=False, default=False)
    is_container = db.Column(db.Boolean, nullable=False, default=False)  # True for structural header sessions (hs_6bDepartment, hs_3MainHdg) that recursively duplicate child contributions
    ingested_at = db.Column(db.DateTime, default=datetime.utcnow)

    contributions = db.relationship(
        "HansardContribution",
        backref="session",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    themes = db.relationship(
        "HansardSessionTheme",
        backref="session",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<HansardSession {self.date} {self.house!r} {self.title[:60]!r}>"


class HansardContribution(db.Model):
    """
    An individual speech contribution within a Hansard session.

    responds_to_id is NULL for all rows until Week 2 Q&A pairing logic populates it.
    For general debates it will remain NULL permanently (flat structure is correct).
    For oral questions sessions, it will link each minister answer back to the
    question it responds to, enabling directness analysis.
    """

    __tablename__ = "ha_contribution"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("ha_session.id"), nullable=False, index=True
    )

    member_id = db.Column(db.Integer, nullable=True, index=True)
    member_name = db.Column(db.String(300), nullable=True)
    party = db.Column(db.String(100), nullable=True)

    speech_text = db.Column(db.Text, nullable=False)
    speech_order = db.Column(db.Integer, nullable=False, default=0)

    # Q&A pairing — populated in Week 2, NULL until then.
    # For an answer contribution: points to the question contribution it responds to.
    # For question contributions and general debate contributions: NULL.
    responds_to_id = db.Column(
        db.Integer, db.ForeignKey("ha_contribution.id"), nullable=True, index=True
    )

    ingested_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        name = self.member_name or "Unknown"
        return f"<HansardContribution session={self.session_id} order={self.speech_order} {name!r}>"


class HansardSessionTheme(db.Model):
    """AI-generated theme tags for a session. Schema created in Week 1; populated in Week 2."""

    __tablename__ = "ha_session_theme"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("ha_session.id"), nullable=False, index=True
    )
    theme = db.Column(db.String(200), nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    tagged_at = db.Column(db.DateTime, default=datetime.utcnow)
    model_used = db.Column(db.String(100), nullable=True)

    def __repr__(self):
        return f"<HansardSessionTheme session={self.session_id} {self.theme!r}>"
