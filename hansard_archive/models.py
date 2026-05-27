"""
Hansard Archive database models — Phase 2A.

Six tables (all prefixed ha_):
  ha_session          — one row per Hansard debate section
  ha_contribution     — speeches within a session (flat; responds_to_id for Q&A pairing, Week 2)
  ha_session_theme    — AI theme tags for Hansard sessions
  ha_cron_run         — cron job run history for monitoring and incident diagnosis
  ha_pq               — Written Questions ingested from Parliament WQ API
  ha_pq_theme         — AI theme tags for Written Questions

Table prefix ha_ keeps archive tables clearly namespaced from the existing
application tables (user, tracked_topic, cached_*, sd_*, etc.).
"""

from datetime import datetime
from extensions import db
from sqlalchemy import event, inspect as sa_inspect, text


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
    slug = db.Column(db.String(200), nullable=True, unique=True, index=True)  # URL slug: {title-slug}-{short-id}, e.g. womens-pension-age-4069
    department = db.Column(db.String(200), nullable=True, index=True)  # answering dept for oral_questions/pmqs; NULL for debates/WH/etc.
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


THEME_TYPE_POLICY_AREA = "policy_area"   # controlled vocab — GOV.UK taxonomy
THEME_TYPE_SPECIFIC = "specific"         # free-text topic phrase


class HansardSessionTheme(db.Model):
    """
    AI-generated theme tags for a session. Populated in Week 2 by tagger.py.

    Two theme_type values per session (multiple rows):
      policy_area — one of the 23 controlled GOV.UK policy taxonomy terms
      specific    — free-text policy topic phrase (e.g. "student loan repayments")

    Sessions may have 1-3 policy_area rows and 1-5 specific rows.
    Container sessions (is_container=True) are excluded from tagging.
    """

    __tablename__ = "ha_session_theme"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("ha_session.id"), nullable=False, index=True
    )
    theme = db.Column(db.String(200), nullable=False)
    theme_type = db.Column(db.String(20), nullable=False, default=THEME_TYPE_SPECIFIC, index=True)
    confidence = db.Column(db.Float, nullable=True)
    tagged_at = db.Column(db.DateTime, default=datetime.utcnow)
    model_used = db.Column(db.String(100), nullable=True)

    def __repr__(self):
        return f"<HansardSessionTheme session={self.session_id} [{self.theme_type}] {self.theme!r}>"


class HaCronRun(db.Model):
    """
    Persisted log of each scheduled cron run.

    Retained for 90 days (pruned by the cron script itself on each run).
    Queryable via /admin for incident diagnosis and pattern spotting.
    """

    __tablename__ = "ha_cron_run"

    id = db.Column(db.Integer, primary_key=True)
    service_name = db.Column(db.String(50), nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=False)
    finished_at = db.Column(db.DateTime, nullable=True)
    days_window = db.Column(db.Integer, nullable=False, default=3)
    sessions_ingested = db.Column(db.Integer, nullable=False, default=0, server_default=text('0'))
    sessions_tagged = db.Column(db.Integer, nullable=False, default=0, server_default=text('0'))
    errors = db.Column(db.Integer, nullable=False, default=0, server_default=text('0'))
    status = db.Column(db.String(20), nullable=False, default="running")  # running / ok / failed
    notes = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<HaCronRun {self.service_name} {self.started_at} {self.status}>"


class HaPQ(db.Model):
    """A Written Question ingested from the Parliament WQ API."""

    __tablename__ = "ha_pq"

    id = db.Column(db.Integer, primary_key=True)
    uin = db.Column(db.String(50), unique=True, nullable=False, index=True)

    heading = db.Column(db.String(500))
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text)

    asking_member = db.Column(db.String(200))
    asking_mnis_id = db.Column(db.Integer, index=True)
    answering_member = db.Column(db.String(200))
    answering_mnis_id = db.Column(db.Integer, index=True)
    answering_body = db.Column(db.String(200), index=True)
    answering_body_id = db.Column(db.Integer, index=True)

    tabled_date  = db.Column(db.Date, nullable=False, index=True)
    answer_date  = db.Column(db.Date)
    is_answered  = db.Column(db.Boolean, default=False, nullable=False, index=True)
    is_holding   = db.Column(db.Boolean, default=False, nullable=False)
    is_withdrawn = db.Column(db.Boolean, default=False, nullable=False)
    chamber      = db.Column(db.String(20), index=True)  # Commons | Lords

    api_id      = db.Column(db.Integer, index=True)  # Parliament API numeric id for individual endpoint

    ingested_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    themes = db.relationship(
        "HaPQTheme",
        backref="pq",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<HaPQ {self.uin} {self.tabled_date} {(self.heading or '')[:60]!r}>"


class HaPQTheme(db.Model):
    """AI-generated theme tags for a Written Question."""

    __tablename__ = "ha_pq_theme"

    pq_id = db.Column(db.Integer, db.ForeignKey("ha_pq.id"), primary_key=True)
    theme = db.Column(db.String(200), primary_key=True)
    theme_type = db.Column(db.String(20), index=True, default=THEME_TYPE_SPECIFIC)
    tagged_at = db.Column(db.DateTime, default=datetime.utcnow)
    model_used = db.Column(db.String(100))

    def __repr__(self):
        return f"<HaPQTheme pq={self.pq_id} [{self.theme_type}] {self.theme!r}>"


class MpAnalytics(db.Model):
    """
    Precomputed parliamentary activity analytics for a Commons MP.

    Refreshed nightly by scripts/compute_mp_analytics.py.  Only covers
    members present in cached_member with house='Commons'.  Procedural
    chair contributions (Mr Speaker, Deputy Speakers, etc.) are excluded
    from all calculations — see analytics_constants.PROCEDURAL_NAME_PATTERNS.

    JSON columns store lists/dicts; None means the member had too little data
    to compute that metric.
    """

    __tablename__ = "ha_mp_analytics"

    member_id        = db.Column(db.Integer, primary_key=True)   # FK to cached_member.member_id (no constraint — avoids cross-file FK complexity)
    computed_at      = db.Column(db.DateTime, nullable=False)

    # Activity counts (12-month and 3-month windows)
    sessions_12m     = db.Column(db.Integer, nullable=False, default=0)
    sessions_3m      = db.Column(db.Integer, nullable=False, default=0)

    # Chamber ranking among Commons MPs with ≥ BASELINE_MIN_SESSIONS sessions
    commons_rank_12m = db.Column(db.Integer, nullable=True)   # 1 = most active
    commons_total    = db.Column(db.Integer, nullable=True)   # size of ranking pool
    commons_pct_12m  = db.Column(db.Float,   nullable=True)   # percentile 0–100

    # Policy area engagement — JSON list of dicts:
    # [{theme, sessions, rank, contributor_count, surface_rank}]
    top_policy_areas = db.Column(db.JSON, nullable=True)

    # Policy areas newly engaged with in last 3 months vs prior 9 months
    # JSON list of dicts: [{theme}]
    recent_shifts    = db.Column(db.JSON, nullable=True)

    # Debate type breakdown (12 months) — JSON dict: {debate_type: session_count}
    debate_type_dist = db.Column(db.JSON, nullable=True)

    # HHI concentration score (0–1); label is the plain-English interpretation
    specialism_score = db.Column(db.Float,       nullable=True)
    specialism_label = db.Column(db.String(200),  nullable=True)

    # True when total sessions < TIER_FULL — simplified panel renders
    thin_data        = db.Column(db.Boolean, nullable=False, default=False)

    # Proportion (0–100) of this MP's 12-month sessions that carry theme tags
    tagged_pct       = db.Column(db.Float, nullable=True)

    def __repr__(self):
        return f"<MpAnalytics member={self.member_id} computed={self.computed_at}>"


class ManifestoChunk(db.Model):
    """
    A short excerpt (2-5 sentences) from a party's 2024 general election manifesto,
    tagged to one or more GOV.UK policy areas.

    Chunks are AI-generated (Gemini Flash-Lite) and must be reviewed by Mark
    (review_status='approved') before they render on party pages.
    """

    __tablename__ = "manifesto_chunk"
    __table_args__ = (
        db.UniqueConstraint("party_slug", "manifesto_year", "chunk_text",
                            name="uq_manifesto_chunk"),
    )

    id             = db.Column(db.Integer, primary_key=True)
    party_slug     = db.Column(db.String(80),  nullable=False, index=True)
    manifesto_year = db.Column(db.Integer,     nullable=False, default=2024)
    source_section = db.Column(db.Text,        nullable=True)   # section heading
    source_url     = db.Column(db.Text,        nullable=True)   # HTML manifesto page URL
    source_page    = db.Column(db.Integer,     nullable=True)   # PDF page number (1-indexed)
    pdf_url        = db.Column(db.Text,        nullable=True)   # direct PDF URL for #page=N links
    chunk_text     = db.Column(db.Text,        nullable=False)
    ingested_at    = db.Column(db.DateTime,    nullable=False)
    review_status  = db.Column(db.String(20),  nullable=False, default="pending")
    # 'pending' | 'approved' | 'rejected'

    tags = db.relationship("ManifestoChunkTag", back_populates="chunk",
                           cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ManifestoChunk id={self.id} party={self.party_slug} status={self.review_status}>"


class ManifestoChunkTag(db.Model):
    """
    Many-to-many: one chunk tagged to one policy area.
    is_primary=True marks the best-fit area for display when multiple chunks
    share the same party+policy_area combination.
    """

    __tablename__ = "manifesto_chunk_tag"

    chunk_id    = db.Column(db.Integer, db.ForeignKey("manifesto_chunk.id", ondelete="CASCADE"),
                            primary_key=True)
    policy_area = db.Column(db.String(100), primary_key=True)
    is_primary  = db.Column(db.Boolean, nullable=False, default=False)

    chunk = db.relationship("ManifestoChunk", back_populates="tags")

    def __repr__(self):
        return f"<ManifestoChunkTag chunk={self.chunk_id} area={self.policy_area} primary={self.is_primary}>"


# ---------------------------------------------------------------------------
# Phase 2: Cross-government statistics
# ---------------------------------------------------------------------------

class HeadlineStat(db.Model):
    """
    One statistic row per theme (v1). source_id is a stable slug independent
    of URL — source_url can be updated when gov.uk moves bulletins without
    breaking row identity.

    source_type: 'ons_timeseries' | 'govuk_bulletin' | 'manual'
    display_hint: 'raw' | 'borrowing' | 'surplus_deficit'
    """

    __tablename__ = "headline_stat"
    __table_args__ = (
        db.UniqueConstraint("theme_slug", "source_id", name="uq_headline_stat"),
        db.Index("idx_headline_stat_producer", "producer_id"),
        db.Index("idx_headline_stat_publication", "publication_id"),
    )

    id               = db.Column(db.Integer,   primary_key=True)
    theme_slug       = db.Column(db.Text,      nullable=False, index=True)
    source_type      = db.Column(db.Text,      nullable=False)
    source_id        = db.Column(db.Text,      nullable=False)
    display_label    = db.Column(db.Text,      nullable=False)
    display_hint     = db.Column(db.Text,      nullable=False, default="raw")
    geography        = db.Column(db.Text,      nullable=False, default="UK")
    latest_value     = db.Column(db.Text,      nullable=True)
    unit             = db.Column(db.Text,      nullable=True)
    period_label     = db.Column(db.Text,      nullable=True)
    release_date     = db.Column(db.Date,      nullable=True)
    source_url       = db.Column(db.Text,      nullable=False)
    source_wording   = db.Column(db.Text,      nullable=True)
    plain_english    = db.Column(db.Text,      nullable=True)
    plain_english_generated_at = db.Column(db.DateTime, nullable=True)
    rewrite_model    = db.Column(db.Text,      nullable=True)
    extraction_config = db.Column(db.Text,     nullable=True)   # JSON stored as text (SQLite compat)
    last_refreshed   = db.Column(db.DateTime,  nullable=False)
    last_success     = db.Column(db.DateTime,  nullable=False)
    # Phase 1 additions
    producer_url     = db.Column(db.Text,      nullable=True)
    methodology_url  = db.Column(db.Text,      nullable=True)
    definition_id    = db.Column(db.Integer,   db.ForeignKey("ha_stat_definition.id",
                                                              ondelete="SET NULL"),
                                 nullable=True)
    release_type     = db.Column(db.Text,      nullable=True)  # headline/reference/analysis/ad_hoc
    sub_topic_slug   = db.Column(db.Text,      nullable=True)
    # Phase 1.6 — nullable FK; backfill in Phase 1.7+
    producer_id      = db.Column(db.Integer,   db.ForeignKey("ha_stat_producer.id",
                                                              ondelete="SET NULL"),
                                 nullable=True, index=True)
    publication_id   = db.Column(db.Integer,   db.ForeignKey("ha_stat_publication.id",
                                                              ondelete="SET NULL"),
                                 nullable=True, index=True)

    issue_reports = db.relationship("StatIssueReport", back_populates="stat",
                                    cascade="all, delete-orphan")
    observations  = db.relationship("StatObservation", back_populates="stat",
                                    cascade="all, delete-orphan",
                                    order_by="StatObservation.period_end.desc()")
    policy_areas  = db.relationship("StatPolicyArea", back_populates="stat",
                                    cascade="all, delete-orphan")
    definition    = db.relationship("StatDefinition", back_populates="stats",
                                    foreign_keys=[definition_id])
    producer      = db.relationship("StatProducer", foreign_keys=[producer_id])
    publication   = db.relationship("StatPublication", foreign_keys=[publication_id])

    def __repr__(self):
        return f"<HeadlineStat id={self.id} theme={self.theme_slug} source={self.source_id}>"


class StatIssueReport(db.Model):
    """User-submitted issue reports against a specific stat card."""

    __tablename__ = "stat_issue_report"

    id               = db.Column(db.Integer, primary_key=True)
    headline_stat_id = db.Column(db.Integer, db.ForeignKey("headline_stat.id", ondelete="SET NULL"),
                                 nullable=True)
    theme_slug       = db.Column(db.Text,    nullable=False)
    source_id        = db.Column(db.Text,    nullable=False)
    user_message     = db.Column(db.Text,    nullable=False)
    user_email       = db.Column(db.Text,    nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False)
    reviewed         = db.Column(db.Boolean, nullable=False, default=False)

    stat = db.relationship("HeadlineStat", back_populates="issue_reports")

    def __repr__(self):
        return f"<StatIssueReport id={self.id} theme={self.theme_slug} reviewed={self.reviewed}>"


class UpcomingRelease(db.Model):
    """
    Cached upcoming official statistics from the GOV.UK release calendar.
    Refreshed daily via the upcoming_refresh.py CLI job.

    release_date_confirmed: True = confirmed date, False = provisional.
    Cancelled releases are excluded at refresh time and never stored.
    """

    __tablename__ = "upcoming_release"
    __table_args__ = (
        db.UniqueConstraint("govuk_content_id", "theme_slug", name="uq_upcoming_release_content_theme"),
        db.Index("idx_upcoming_release_theme_date", "theme_slug", "release_date"),
    )

    id                     = db.Column(db.Integer,  primary_key=True)
    govuk_content_id       = db.Column(db.Text,     nullable=False)
    theme_slug             = db.Column(db.Text,     nullable=False)
    organisation_slug      = db.Column(db.Text,     nullable=False)
    organisation_name      = db.Column(db.Text,     nullable=False)
    title                  = db.Column(db.Text,     nullable=False)
    summary                = db.Column(db.Text,     nullable=True)
    release_date           = db.Column(db.Date,     nullable=False)
    release_date_confirmed = db.Column(db.Boolean,  nullable=False)
    publication_url        = db.Column(db.Text,     nullable=False)
    document_type          = db.Column(db.Text,     nullable=True)
    last_refreshed         = db.Column(db.DateTime, nullable=False)

    def __repr__(self):
        return f"<UpcomingRelease id={self.id} theme={self.theme_slug} title={self.title[:40]!r}>"


# ---------------------------------------------------------------------------
# Phase 2A.5: Bills ingestion
# ---------------------------------------------------------------------------

class HaBill(db.Model):
    """
    A UK Parliament bill ingested from the Parliament Bills API.

    Sessions 38 (2024-25) and 39 (2025-26) cover the full Labour government.
    is_act and is_defeated are independently settable; both FALSE = in-progress.
    slug is nullable — populate later for SEO-friendly /bill/<slug> URLs.
    """

    __tablename__ = "ha_bill"
    __table_args__ = (
        db.Index("idx_ha_bill_session", "session"),
        db.Index("idx_ha_bill_introduced", "introduced_date"),
        db.Index("idx_ha_bill_is_act", "is_act"),
        db.Index("idx_ha_bill_is_defeated", "is_defeated"),
    )

    id                 = db.Column(db.Integer, primary_key=True)
    parliament_bill_id = db.Column(db.Integer, nullable=False, unique=True)
    title              = db.Column(db.Text, nullable=False)
    short_title        = db.Column(db.Text, nullable=True)
    long_title         = db.Column(db.Text, nullable=True)
    summary            = db.Column(db.Text, nullable=True)   # often NULL from API
    house_of_origin    = db.Column(db.String(20), nullable=False)   # Commons | Lords
    session            = db.Column(db.String(20), nullable=False)   # e.g. 2024-25
    bill_type          = db.Column(db.String(100), nullable=True)
    is_act              = db.Column(db.Boolean, nullable=False, default=False)
    is_defeated         = db.Column(db.Boolean, nullable=False, default=False)
    bill_withdrawn_date = db.Column(db.Date, nullable=True)
    is_carried_over     = db.Column(db.Boolean, nullable=False, default=False)
    current_stage       = db.Column(db.Text, nullable=True)
    current_house      = db.Column(db.String(20), nullable=True)
    introduced_date    = db.Column(db.Date, nullable=True)
    last_updated_date  = db.Column(db.Date, nullable=True)
    royal_assent_date  = db.Column(db.Date, nullable=True)
    slug               = db.Column(db.Text, nullable=True)
    parliament_url     = db.Column(db.Text, nullable=False)
    govuk_url          = db.Column(db.Text, nullable=True)
    raw_data           = db.Column(db.JSON, nullable=True)
    ingested_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_refreshed     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Tagging pipeline state — set by scripts/tag_bills.py
    tagging_attempted_at   = db.Column(db.DateTime, nullable=True)
    tagging_completed_at   = db.Column(db.DateTime, nullable=True)
    tagging_failure_reason = db.Column(db.Text, nullable=True)

    sponsors = db.relationship(
        "HaBillSponsor",
        backref="bill",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    stages = db.relationship(
        "HaBillStage",
        backref="bill",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    themes = db.relationship(
        "HaBillTheme",
        backref="bill",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<HaBill {self.parliament_bill_id} {self.title[:60]!r}>"


class HaBillSponsor(db.Model):
    """
    A sponsor (primary or co-sponsor) of a bill.

    member_id is a FK to cached_member.member_id — NULL only for Lords without
    a member record or other edge cases. member_name stored as fallback.
    sortOrder=1 in the API maps to is_primary=True.
    """

    __tablename__ = "ha_bill_sponsor"
    __table_args__ = (
        db.UniqueConstraint("bill_id", "member_name", name="uq_ha_bill_sponsor"),
        db.Index("idx_ha_bill_sponsor_bill", "bill_id"),
        db.Index("idx_ha_bill_sponsor_member", "member_id"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    bill_id     = db.Column(db.Integer, db.ForeignKey("ha_bill.id", ondelete="CASCADE"), nullable=False)
    member_id   = db.Column(db.Integer, db.ForeignKey("cached_member.member_id"), nullable=True)
    member_name = db.Column(db.Text, nullable=False)
    party       = db.Column(db.Text, nullable=True)
    is_primary  = db.Column(db.Boolean, nullable=False, default=False)
    house       = db.Column(db.String(20), nullable=True)

    def __repr__(self):
        return f"<HaBillSponsor bill={self.bill_id} {self.member_name!r} primary={self.is_primary}>"


class HaBillStage(db.Model):
    """Stage history for a bill (First reading, Committee, Third reading, etc.)."""

    __tablename__ = "ha_bill_stage"
    __table_args__ = (
        db.UniqueConstraint("bill_id", "parliament_stage_id", name="uq_ha_bill_stage"),
        db.Index("idx_ha_bill_stage_bill", "bill_id"),
    )

    id                  = db.Column(db.Integer, primary_key=True)
    bill_id             = db.Column(db.Integer, db.ForeignKey("ha_bill.id", ondelete="CASCADE"), nullable=False)
    parliament_stage_id = db.Column(db.Integer, nullable=False)
    stage_name          = db.Column(db.Text, nullable=False)
    house               = db.Column(db.String(20), nullable=False)
    stage_date          = db.Column(db.Date, nullable=True)
    stage_order         = db.Column(db.Integer, nullable=False)

    def __repr__(self):
        return f"<HaBillStage bill={self.bill_id} {self.stage_name!r} {self.house}>"


class HaBillTheme(db.Model):
    """
    AI-generated policy area tag for a bill.

    theme uses display names from POLICY_AREAS in tagger.py — not slugs.
    tagged_by records the model tier: 'ai_gemini_pro', 'manual', 'api'.
    """

    __tablename__ = "ha_bill_theme"
    __table_args__ = (
        db.UniqueConstraint("bill_id", "theme", name="uq_ha_bill_theme"),
        db.Index("idx_ha_bill_theme_theme", "theme"),
        db.Index("idx_ha_bill_theme_bill", "bill_id"),
    )

    id               = db.Column(db.Integer, primary_key=True)
    bill_id          = db.Column(db.Integer, db.ForeignKey("ha_bill.id", ondelete="CASCADE"), nullable=False)
    theme            = db.Column(db.Text, nullable=False)
    tagged_by        = db.Column(db.String(50), nullable=False, default="ai_gemini_pro")
    tagged_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    confidence_score = db.Column(db.Float, nullable=True)

    def __repr__(self):
        return f"<HaBillTheme bill={self.bill_id} {self.theme!r} by={self.tagged_by}>"


class HaBillPublication(db.Model):
    """
    A document publication associated with a bill (Explanatory Notes,
    Impact Assessment, Delegated Powers Memorandum, etc.).

    publication_type uses a controlled vocabulary:
      'explanatory_notes' | 'impact_assessment' |
      'delegated_powers_memorandum' | 'human_rights_memorandum' |
      'bill_text' | 'other'

    summary_text is populated only for explanatory_notes where an HTML
    version exists and parsing succeeds.  All other types store NULL.
    No AI rewriting — verbatim extract from the source document.
    """

    __tablename__ = "ha_bill_publication"
    __table_args__ = (
        db.UniqueConstraint("bill_id", "publication_url", name="uq_ha_bill_publication"),
        db.Index("idx_ha_bill_pub_bill", "bill_id"),
        db.Index("idx_ha_bill_pub_type", "publication_type"),
    )

    id               = db.Column(db.Integer, primary_key=True)
    bill_id          = db.Column(db.Integer, db.ForeignKey("ha_bill.id", ondelete="CASCADE"), nullable=False)
    publication_type = db.Column(db.Text, nullable=False)
    source           = db.Column(db.String(20), nullable=False, default='parliament')  # parliament | govuk
    title            = db.Column(db.Text, nullable=False)
    publication_date = db.Column(db.Date, nullable=True)
    publication_url  = db.Column(db.Text, nullable=False)
    publisher        = db.Column(db.Text, nullable=True)
    summary_text     = db.Column(db.Text, nullable=True)
    raw_data         = db.Column(db.JSON, nullable=True)
    ingested_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<HaBillPublication bill={self.bill_id} {self.publication_type!r} {self.title[:40]!r}>"


# ---------------------------------------------------------------------------
# Stats index — Phase 1 schema
# ---------------------------------------------------------------------------

class StatDefinition(db.Model):
    """
    Reusable measure definitions shared across multiple HeadlineStat rows.
    Solves the 'highly skilled employment' problem: one canonical definition,
    many stats (by year, geography, provider type) that reference it.

    Forward note (Phase 1.6): source_type on HeadlineStat will be migrated
    to a FK against a StatSource table. This table is not StatSource — it
    defines *what* is being measured, not *who* produces it.
    """

    __tablename__ = "ha_stat_definition"
    __table_args__ = (
        db.UniqueConstraint("slug", name="uq_stat_definition_slug"),
    )

    id                 = db.Column(db.Integer,  primary_key=True)
    slug               = db.Column(db.Text,     nullable=False)
    name               = db.Column(db.Text,     nullable=False)
    producer           = db.Column(db.Text,     nullable=True)
    description        = db.Column(db.Text,     nullable=True)
    methodology_url    = db.Column(db.Text,     nullable=True)
    cohort_definition  = db.Column(db.Text,     nullable=True)
    denominator        = db.Column(db.Text,     nullable=True)
    caveats            = db.Column(db.Text,     nullable=True)
    created_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                   onupdate=datetime.utcnow)

    stats = db.relationship("HeadlineStat", back_populates="definition",
                            foreign_keys="HeadlineStat.definition_id")

    def __repr__(self):
        return f"<StatDefinition id={self.id} slug={self.slug!r}>"


class StatPolicyArea(db.Model):
    """
    Many-to-many junction between HeadlineStat and policy areas.
    A stat may belong to multiple policy areas; exactly one must be primary.
    The primary policy area determines the stat's canonical URL.

    HeadlineStat.theme_slug is kept in sync with the is_primary=True row
    here via application logic — do not update one without the other.
    """

    __tablename__ = "ha_stat_policy_area"
    __table_args__ = (
        db.UniqueConstraint("headline_stat_id", "theme_slug",
                            name="uq_stat_policy_area"),
    )

    id               = db.Column(db.Integer, primary_key=True)
    headline_stat_id = db.Column(db.Integer,
                                 db.ForeignKey("headline_stat.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    theme_slug       = db.Column(db.Text, nullable=False, index=True)
    is_primary       = db.Column(db.Boolean, nullable=False, default=False)

    stat = db.relationship("HeadlineStat", back_populates="policy_areas")

    def __repr__(self):
        return (f"<StatPolicyArea stat={self.headline_stat_id} "
                f"theme={self.theme_slug!r} primary={self.is_primary}>")


class StatObservation(db.Model):
    """
    Time-series storage for HeadlineStat values — one row per release/period.
    Cutoff: observations with period_end < 2024-07-01 are rejected at ingestion.

    straddles_cutoff: True when period_start < 2024-07-01 AND period_end >= 2024-07-01.
    Straddling observations are stored but excluded from 'latest value' queries
    by default.

    Latest value query (fastest path):
        SELECT * FROM ha_stat_observation
        WHERE headline_stat_id = ? AND NOT straddles_cutoff
        ORDER BY period_end DESC LIMIT 1
    """

    __tablename__ = "ha_stat_observation"
    __table_args__ = (
        db.UniqueConstraint("headline_stat_id", "period_start", "period_end",
                            name="uq_stat_observation_period"),
        db.Index("idx_stat_obs_stat_id", "headline_stat_id"),
        db.Index("idx_stat_obs_latest", "headline_stat_id", "period_end"),
    )

    id               = db.Column(db.Integer,  primary_key=True)
    headline_stat_id = db.Column(db.Integer,
                                 db.ForeignKey("headline_stat.id", ondelete="CASCADE"),
                                 nullable=False)
    period_label     = db.Column(db.Text,     nullable=True)
    period_start     = db.Column(db.Date,     nullable=True)
    period_end       = db.Column(db.Date,     nullable=True)
    value            = db.Column(db.Text,     nullable=True)
    release_date     = db.Column(db.Date,     nullable=True)
    release_url      = db.Column(db.Text,     nullable=True)
    source_wording   = db.Column(db.Text,     nullable=True)
    plain_english    = db.Column(db.Text,     nullable=True)
    plain_english_generated_at = db.Column(db.DateTime, nullable=True)
    rewrite_model    = db.Column(db.Text,     nullable=True)
    straddles_cutoff = db.Column(db.Boolean,  nullable=False, default=False)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    stat = db.relationship("HeadlineStat", back_populates="observations")

    def __repr__(self):
        return (f"<StatObservation id={self.id} stat={self.headline_stat_id} "
                f"period={self.period_label!r} value={self.value!r}>")


# ---------------------------------------------------------------------------
# Phase 1.6: Source registry
# ---------------------------------------------------------------------------

class StatProducer(db.Model):
    """
    A statistics-producing body. The primary unit of licence authorisation.

    Application-layer invariants (not enforceable via SQLite CHECK constraints):
    - If licence != 'Unverified', licence_evidence_raw_url must not be null.
    - If authorisation_status == 'authorised', reviewed_by and reviewed_at
      must not be null.

    A SQLAlchemy event listener registered at the bottom of this module
    automatically writes a StatLicenceAuditLog row whenever a StatProducer row
    is created or has its licence, licence_evidence_raw_url, or
    licence_evidence_wayback_url fields changed.
    """

    __tablename__ = "ha_stat_producer"
    __table_args__ = (
        db.UniqueConstraint("slug", name="uq_stat_producer_slug"),
        db.CheckConstraint(
            "producer_type IN ('central_department','executive_agency','ndpb',"
            "'regulator','devolved_administration','devolved_body',"
            "'gss_producer','other_public_body')",
            name="ck_stat_producer_type"),
        db.CheckConstraint(
            "authorisation_status IN ('candidate','under_review','authorised',"
            "'declined','paused')",
            name="ck_stat_producer_auth_status"),
        db.CheckConstraint(
            "licence IN ('OGL_v3','Crown_Copyright_other','HESA_Open',"
            "'HESA_Commercial','UCAS','Custom_Open','Custom_Restrictive','Unverified')",
            name="ck_stat_producer_licence"),
        db.CheckConstraint(
            "discovery_status IN ('pending','in_progress','completed','failed')",
            name="ck_stat_producer_discovery_status"),
    )

    id                           = db.Column(db.Integer, primary_key=True)
    slug                         = db.Column(db.Text, nullable=False)
    name                         = db.Column(db.Text, nullable=False)
    short_name                   = db.Column(db.Text, nullable=True)
    producer_type                = db.Column(db.Text, nullable=False)
    parent_id                    = db.Column(db.Integer,
                                             db.ForeignKey("ha_stat_producer.id",
                                                           ondelete="SET NULL"),
                                             nullable=True, index=True)
    web_root_url                 = db.Column(db.Text, nullable=False)
    contact_email                = db.Column(db.Text, nullable=True)
    description                  = db.Column(db.Text, nullable=True)
    authorisation_status         = db.Column(db.Text, nullable=False, default="candidate")
    authorisation_reason         = db.Column(db.Text, nullable=True)
    licence                      = db.Column(db.Text, nullable=False)
    licence_evidence_raw_url     = db.Column(db.Text, nullable=True)
    licence_evidence_wayback_url = db.Column(db.Text, nullable=True)
    reviewed_by                  = db.Column(db.Text, nullable=True)
    reviewed_at                  = db.Column(db.DateTime, nullable=True)
    created_at                   = db.Column(db.DateTime, nullable=False,
                                             default=datetime.utcnow)
    updated_at                   = db.Column(db.DateTime, nullable=False,
                                             default=datetime.utcnow,
                                             onupdate=datetime.utcnow)
    # Phase 1.7: discovery state
    discovery_status             = db.Column(db.Text, nullable=False, default="pending")
    discovery_completed_at       = db.Column(db.DateTime, nullable=True)
    discovery_failure_reason     = db.Column(db.Text, nullable=True)
    # Phase 1.8: batch resume — count of candidates processed so far; used to
    # skip already-classified candidates when restarting an interrupted run
    candidates_processed_count   = db.Column(db.Integer, nullable=True)

    parent       = db.relationship("StatProducer", remote_side="StatProducer.id",
                                   foreign_keys=[parent_id])
    publications = db.relationship("StatPublication", back_populates="producer",
                                   passive_deletes=True)
    audit_log    = db.relationship("StatLicenceAuditLog", back_populates="producer",
                                   cascade="all, delete-orphan")
    auth_log     = db.relationship("StatProducerAuthLog", back_populates="producer",
                                   cascade="all, delete-orphan")

    def __repr__(self):
        return f"<StatProducer id={self.id} slug={self.slug!r} licence={self.licence!r}>"


class StatPublication(db.Model):
    """
    A specific statistical output published by a StatProducer.

    Publications inherit their producer's authorisation status and licence.
    No publication-level override — if a specific publication needs different
    handling, the producer is re-reviewed.
    """

    __tablename__ = "ha_stat_publication"
    __table_args__ = (
        db.UniqueConstraint("producer_id", "slug",
                            name="uq_stat_publication_producer_slug"),
        db.Index("idx_stat_pub_producer", "producer_id"),
        db.CheckConstraint(
            "update_cadence IS NULL OR update_cadence IN "
            "('daily','weekly','monthly','quarterly','annual','biennial','ad_hoc','one_off')",
            name="ck_stat_pub_cadence"),
        db.CheckConstraint(
            "authorisation_status IN ('candidate','under_review','authorised','declined','paused')",
            name="ck_stat_pub_auth_status"),
    )

    id             = db.Column(db.Integer, primary_key=True)
    producer_id    = db.Column(db.Integer,
                               db.ForeignKey("ha_stat_producer.id", ondelete="RESTRICT"),
                               nullable=False, index=True)
    slug           = db.Column(db.Text, nullable=False, index=True)
    name           = db.Column(db.Text, nullable=False)
    url            = db.Column(db.Text, nullable=False)
    description    = db.Column(db.Text, nullable=True)
    update_cadence = db.Column(db.Text, nullable=True)
    first_seen_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                               onupdate=datetime.utcnow)
    # Phase 1.7: authorisation workflow
    authorisation_status = db.Column(db.Text, nullable=False, default="candidate")
    subject_area         = db.Column(db.Text, nullable=True)
    discovered_at        = db.Column(db.DateTime, nullable=True)
    authorised_at        = db.Column(db.DateTime, nullable=True)
    authorised_by        = db.Column(db.Text, nullable=True)
    declined_at          = db.Column(db.DateTime, nullable=True)
    declined_by          = db.Column(db.Text, nullable=True)
    decline_reason       = db.Column(db.Text, nullable=True)

    producer      = db.relationship("StatProducer", back_populates="publications")
    pub_audit_log = db.relationship("StatPublicationAuditLog", back_populates="publication",
                                    cascade="all, delete-orphan")
    themes        = db.relationship("StatPublicationTheme", back_populates="publication",
                                    lazy="dynamic", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<StatPublication id={self.id} producer={self.producer_id} slug={self.slug!r}>"


class StatLicenceAuditLog(db.Model):
    """
    Append-only audit trail for licence-related changes on a StatProducer.

    Rows are created automatically by the SQLAlchemy event listener registered
    at the bottom of this module. Application code must never UPDATE or DELETE
    rows in this table — always create new rows to record changes.
    """

    __tablename__ = "ha_stat_licence_audit_log"
    __table_args__ = (
        db.Index("idx_stat_licence_audit_producer", "producer_id"),
    )

    id                       = db.Column(db.Integer, primary_key=True)
    producer_id              = db.Column(db.Integer,
                                         db.ForeignKey("ha_stat_producer.id",
                                                       ondelete="CASCADE"),
                                         nullable=False, index=True)
    changed_at               = db.Column(db.DateTime, nullable=False,
                                         default=datetime.utcnow)
    old_licence              = db.Column(db.Text, nullable=True)
    new_licence              = db.Column(db.Text, nullable=False)
    old_evidence_raw_url     = db.Column(db.Text, nullable=True)
    new_evidence_raw_url     = db.Column(db.Text, nullable=True)
    old_evidence_wayback_url = db.Column(db.Text, nullable=True)
    new_evidence_wayback_url = db.Column(db.Text, nullable=True)
    change_reason            = db.Column(db.Text, nullable=True)
    recorded_by              = db.Column(db.Text, nullable=True)

    producer = db.relationship("StatProducer", back_populates="audit_log")

    def __repr__(self):
        return (f"<StatLicenceAuditLog id={self.id} producer={self.producer_id} "
                f"new_licence={self.new_licence!r}>")


class StatProducerAuthLog(db.Model):
    """
    Append-only audit trail for authorisation_status changes on a StatProducer.

    Written directly by the admin route handler on Authorise/Decline actions.
    Application code must never UPDATE or DELETE rows in this table.
    """

    __tablename__ = "ha_stat_producer_auth_log"
    __table_args__ = (
        db.Index("idx_stat_producer_auth_log_producer", "producer_id"),
    )

    id            = db.Column(db.Integer, primary_key=True)
    producer_id   = db.Column(db.Integer,
                               db.ForeignKey("ha_stat_producer.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    changed_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    old_status    = db.Column(db.Text, nullable=False)
    new_status    = db.Column(db.Text, nullable=False)
    change_reason = db.Column(db.Text, nullable=True)
    recorded_by   = db.Column(db.Text, nullable=False)

    producer = db.relationship("StatProducer", back_populates="auth_log")

    def __repr__(self):
        return (f"<StatProducerAuthLog id={self.id} producer={self.producer_id} "
                f"{self.old_status!r}->{self.new_status!r}>")


class StatPublicationAuditLog(db.Model):
    """
    Append-only audit trail for authorise/decline decisions on a StatPublication.

    Rows are written automatically by the before_update event listener below
    whenever authorisation_status changes. Application code must never UPDATE
    or DELETE rows in this table.

    Only fires for human decisions (admin UI). Discovery worker candidate
    creation uses INSERT (not UPDATE) so this hook does not fire for it.
    """

    __tablename__ = "ha_stat_publication_audit_log"
    __table_args__ = (
        db.Index("idx_stat_pub_audit_pub", "publication_id"),
    )

    id             = db.Column(db.Integer, primary_key=True)
    publication_id = db.Column(db.Integer,
                               db.ForeignKey("ha_stat_publication.id",
                                             ondelete="CASCADE"),
                               nullable=False, index=True)
    changed_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    old_status     = db.Column(db.Text, nullable=False)
    new_status     = db.Column(db.Text, nullable=False)
    change_reason  = db.Column(db.Text, nullable=True)
    recorded_by    = db.Column(db.Text, nullable=False)

    publication = db.relationship("StatPublication", back_populates="pub_audit_log")

    def __repr__(self):
        return (f"<StatPublicationAuditLog id={self.id} pub={self.publication_id} "
                f"new_status={self.new_status!r}>")


class StatPublicationTheme(db.Model):
    """
    Controlled policy-area tags and free-text specifics for a stat publication.
    Mirrors HansardSessionTheme. theme_type values:
      policy_area — one of the 23 HANSARD_POLICY_NAMES strings
      specific    — free-text subject phrase (retained from subject_area field)
    1-4 policy_area rows per publication. Written by run_discovery() for new
    candidates and by the Phase 1.8 backfill script for existing rows.
    """

    __tablename__ = "ha_stat_publication_theme"
    __table_args__ = (
        db.Index("idx_stat_pub_theme_pub", "publication_id"),
        db.Index("idx_stat_pub_theme_type", "theme_type"),
        db.UniqueConstraint(
            "publication_id", "theme", "theme_type",
            name="uq_stat_pub_theme_publication_theme_type",
        ),
    )

    id             = db.Column(db.Integer, primary_key=True)
    publication_id = db.Column(
        db.Integer,
        db.ForeignKey("ha_stat_publication.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    theme      = db.Column(db.String(200), nullable=False)
    theme_type = db.Column(db.String(20), nullable=False,
                           default=THEME_TYPE_SPECIFIC, index=True)
    confidence = db.Column(db.Float, nullable=True)
    tagged_at  = db.Column(db.DateTime, default=datetime.utcnow)
    model_used = db.Column(db.String(100), nullable=True)

    publication = db.relationship("StatPublication", back_populates="themes")

    def __repr__(self):
        return (f"<StatPublicationTheme pub={self.publication_id} "
                f"[{self.theme_type}] {self.theme!r}>")


# ---------------------------------------------------------------------------
# Phase 1.6: Audit log event listeners
# ---------------------------------------------------------------------------

def _write_licence_audit(connection, producer_id, *, old_licence, new_licence,
                          old_raw_url, new_raw_url, old_way_url, new_way_url,
                          change_reason):
    """Insert one ha_stat_licence_audit_log row using the mapper connection."""
    connection.execute(
        text(
            "INSERT INTO ha_stat_licence_audit_log "
            "(producer_id, changed_at, old_licence, new_licence, "
            "old_evidence_raw_url, new_evidence_raw_url, "
            "old_evidence_wayback_url, new_evidence_wayback_url, change_reason) "
            "VALUES (:pid, :at, :old_lic, :new_lic, "
            ":old_raw, :new_raw, :old_way, :new_way, :reason)"
        ),
        {
            "pid":     producer_id,
            "at":      datetime.utcnow(),
            "old_lic": old_licence,
            "new_lic": new_licence,
            "old_raw": old_raw_url,
            "new_raw": new_raw_url,
            "old_way": old_way_url,
            "new_way": new_way_url,
            "reason":  change_reason,
        },
    )


@event.listens_for(StatProducer, "after_insert")
def _stat_producer_after_insert(mapper, connection, target):
    """Write an initial audit entry when a StatProducer row is first created."""
    _write_licence_audit(
        connection,
        target.id,
        old_licence=None,
        new_licence=target.licence,
        old_raw_url=None,
        new_raw_url=target.licence_evidence_raw_url,
        old_way_url=None,
        new_way_url=target.licence_evidence_wayback_url,
        change_reason="Initial classification",
    )


@event.listens_for(StatProducer, "before_update")
def _stat_producer_before_update(mapper, connection, target):
    """Write an audit entry when any licence-related field changes on a StatProducer."""
    _TRACKED = ("licence", "licence_evidence_raw_url", "licence_evidence_wayback_url")
    attrs = sa_inspect(target).attrs
    if not any(attrs[f].history.has_changes() for f in _TRACKED):
        return  # no licence-related change — skip

    def _old_new(field):
        h = attrs[field].history
        old = h.deleted[0] if h.deleted else (h.unchanged[0] if h.unchanged else None)
        new = h.added[0]   if h.added   else (h.unchanged[0] if h.unchanged else None)
        return old, new

    old_lic, new_lic = _old_new("licence")
    old_raw, new_raw = _old_new("licence_evidence_raw_url")
    old_way, new_way = _old_new("licence_evidence_wayback_url")

    _write_licence_audit(
        connection,
        target.id,
        old_licence=old_lic,
        new_licence=new_lic,
        old_raw_url=old_raw,
        new_raw_url=new_raw,
        old_way_url=old_way,
        new_way_url=new_way,
        change_reason=None,
    )


# ---------------------------------------------------------------------------
# Phase 1.7: Publication authorisation audit hook
# ---------------------------------------------------------------------------

@event.listens_for(StatPublication, "before_update")
def _stat_publication_before_update(mapper, connection, target):
    """Write audit entry when authorisation_status changes on a StatPublication.

    Only fires for human authorise/decline decisions (admin UI actions).
    Discovery worker candidate creation uses INSERT, not UPDATE, so this
    hook does not fire during automated discovery.

    The admin route must set target._recorded_by before flushing so the
    entry records who made the decision. Falls back to 'system' if unset.
    """
    attrs = sa_inspect(target).attrs
    h = attrs["authorisation_status"].history
    if not h.has_changes():
        return
    old = h.deleted[0] if h.deleted else (h.unchanged[0] if h.unchanged else None)
    new = h.added[0]   if h.added   else (h.unchanged[0] if h.unchanged else None)
    if old == new:
        return
    recorded_by   = getattr(target, "_recorded_by",   None) or "system"
    change_reason = getattr(target, "_change_reason", None)
    connection.execute(
        text(
            "INSERT INTO ha_stat_publication_audit_log "
            "(publication_id, changed_at, old_status, new_status, change_reason, recorded_by) "
            "VALUES (:pid, :at, :old, :new, :reason, :by)"
        ),
        {
            "pid":    target.id,
            "at":     datetime.utcnow(),
            "old":    old,
            "new":    new,
            "reason": change_reason,
            "by":     recorded_by,
        },
    )
