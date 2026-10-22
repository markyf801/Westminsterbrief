import hashlib
import json
from datetime import datetime, timedelta
from sqlalchemy.orm import deferred

from extensions import db


class MemberLink(db.Model):
    """Legacy table, no longer read or written.

    It mapped Parliament member ids to TheyWorkForYou person ids. Every search
    now uses Parliament member ids against our own archive, so nothing needs it.
    The model stays only so the table and its columns are left as they are
    (no schema change); it can be dropped in a later migration.
    """
    __tablename__ = 'member_link'

    id               = db.Column(db.Integer, primary_key=True)
    parliament_id    = db.Column(db.Integer, unique=True, nullable=False, index=True)
    display_name     = db.Column(db.String(300), nullable=False)
    house            = db.Column(db.String(20), nullable=False)   # 'Commons' | 'Lords'
    twfy_person_id   = db.Column(db.String(20), nullable=True, index=True)
    twfy_name        = db.Column(db.String(300), nullable=True)   # name as TWFY returns it
    resolution_method = db.Column(db.String(50), nullable=True)
    # Values: 'twfy_name_search' | 'debate_speaker' | 'seeded' | 'failed'
    lookup_failed    = db.Column(db.Boolean, nullable=False, default=False)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at      = db.Column(db.DateTime, nullable=True)


class CachedSearch(db.Model):
    """Caches search results (Hansard / Parliament API searches, AI query expansion).
    TTL is set by the caller, typically 6-24 hours.
    The table keeps its original name, cached_twfy_search (no schema change)."""
    __tablename__ = 'cached_twfy_search'
    id = db.Column(db.Integer, primary_key=True)
    cache_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    search_query = db.Column(db.Text, nullable=False)
    source_type = db.Column(db.String(20), nullable=False)
    results_json = db.Column(db.Text, nullable=False)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def make_key(query, source_type):
        raw = f"{source_type}:{query}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @staticmethod
    def get(query, source_type, ttl_hours=24):
        key = CachedSearch.make_key(query, source_type)
        entry = CachedSearch.query.filter_by(cache_key=key).first()
        if not entry:
            return None
        age = datetime.utcnow() - entry.cached_at
        if age > timedelta(hours=ttl_hours):
            return None
        try:
            return json.loads(entry.results_json)
        except Exception:
            return None

    @staticmethod
    def store(query, source_type, results):
        key = CachedSearch.make_key(query, source_type)
        existing = CachedSearch.query.filter_by(cache_key=key).first()
        data = json.dumps(results)
        if existing:
            existing.results_json = data
            existing.cached_at = datetime.utcnow()
        else:
            db.session.add(CachedSearch(
                cache_key=key, search_query=query,
                source_type=source_type, results_json=data
            ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


class CachedTranscript(db.Model):
    """Stores scraped Hansard debate transcripts. Never expires — published debates never change."""
    __tablename__ = 'cached_transcript'
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), unique=True, nullable=False, index=True)
    title = db.Column(db.String(500))
    date = db.Column(db.String(20))
    house = db.Column(db.String(20))
    transcript_text = db.Column(db.Text, nullable=False)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def get(url):
        return CachedTranscript.query.filter_by(url=url).first()

    @staticmethod
    def store(url, title, date, house, transcript_text):
        entry = CachedTranscript(
            url=url, title=title, date=date,
            house=house, transcript_text=transcript_text
        )
        db.session.add(entry)
        db.session.commit()


class CachedQuestion(db.Model):
    """Stores Written Questions. Only questions older than 7 days are cached (fully settled)."""
    __tablename__ = 'cached_question'
    id = db.Column(db.Integer, primary_key=True)
    uin = db.Column(db.String(50), unique=True, nullable=False, index=True)
    member_name = db.Column(db.String(200))
    party = db.Column(db.String(100))
    department_id = db.Column(db.String(20))
    department_name = db.Column(db.String(200))
    question_text = db.Column(db.Text)
    answer_text = db.Column(db.Text, nullable=True)
    date_tabled = db.Column(db.String(20), index=True)
    url = db.Column(db.String(500))
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def is_cacheable(date_tabled_str):
        """Only cache questions older than 7 days — they won't change."""
        try:
            tabled = datetime.strptime(date_tabled_str, '%Y-%m-%d')
            return (datetime.utcnow() - tabled).days >= 7
        except Exception:
            return False

    @staticmethod
    def get(uin):
        return CachedQuestion.query.filter_by(uin=str(uin)).first()

    @staticmethod
    def store(uin, member_name, party, department_id, department_name,
              question_text, answer_text, date_tabled, url):
        entry = CachedQuestion(
            uin=str(uin), member_name=member_name, party=party,
            department_id=str(department_id), department_name=department_name,
            question_text=question_text, answer_text=answer_text,
            date_tabled=date_tabled, url=url
        )
        db.session.add(entry)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


class StakeholderOrg(db.Model):
    """Shared global list of external stakeholder organisations.
    Used by the Stakeholder Research tab to find what orgs are saying about topics."""
    __tablename__ = 'stakeholder_org'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), unique=True, nullable=False)
    short_name    = db.Column(db.String(50), nullable=True)
    category      = db.Column(db.String(100), nullable=False)   # e.g. "HE Mission Group"
    website       = db.Column(db.String(300), nullable=True)     # domain, e.g. "universitiesuk.ac.uk"
    description   = db.Column(db.Text, nullable=True)
    bsky_handle   = db.Column(db.String(100), nullable=True)
    rss_url       = db.Column(db.String(500), nullable=True)
    hansard_search_name = db.Column(db.String(200), nullable=True)  # override name for Hansard search
    active        = db.Column(db.Boolean, default=True, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def all_active():
        return StakeholderOrg.query.filter_by(active=True).order_by(
            StakeholderOrg.category, StakeholderOrg.name).all()

    @staticmethod
    def by_category():
        """Return active orgs grouped as {category: [orgs]} dict, sorted."""
        orgs = StakeholderOrg.all_active()
        grouped = {}
        for org in orgs:
            grouped.setdefault(org.category, []).append(org)
        return dict(sorted(grouped.items()))


class CachedMember(db.Model):
    """Stores MP/Peer details. Refreshed after 7 days (party/constituency can change)."""
    __tablename__ = 'cached_member'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    name = db.Column(db.String(200))
    party = db.Column(db.String(100))
    constituency = db.Column(db.String(200))
    house = db.Column(db.String(20))
    image_url = db.Column(db.String(500))
    ministerial_role = db.Column(db.String(500))  # current govt post, null if not a minister
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_fresh(self):
        return (datetime.utcnow() - self.cached_at) < timedelta(days=7)

    @staticmethod
    def get(member_id):
        return CachedMember.query.filter_by(member_id=int(member_id)).first()

    @staticmethod
    def store(member_id, name, party, constituency, house, image_url, ministerial_role=None):
        existing = CachedMember.query.filter_by(member_id=int(member_id)).first()
        if existing:
            existing.name = name
            existing.party = party
            existing.constituency = constituency
            existing.house = house
            existing.image_url = image_url
            if ministerial_role is not None:
                existing.ministerial_role = ministerial_role
            existing.cached_at = datetime.utcnow()
        else:
            db.session.add(CachedMember(
                member_id=int(member_id), name=name, party=party,
                constituency=constituency, house=house, image_url=image_url,
                ministerial_role=ministerial_role,
            ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


class MemberProfile(db.Model):
    """Stored profile for an MP or Peer, refreshed offline by scripts/refresh_members.py.

    /members/<id>/<slug>, /mp_search and /biography read from this table instead
    of calling the Parliament Members API on page view. On production the table
    is created by migrations/members_profile_tables.py (the web service runs
    with SKIP_MIGRATIONS=1, so db.create_all() does not run there).

    Photos are not stored — thumbnail_url points at Parliament's image.
    """
    __tablename__ = 'member_profile'

    member_id         = db.Column(db.Integer, primary_key=True, autoincrement=False)
    slug              = db.Column(db.String(300), nullable=False, index=True)
    # 'personal' (built from the member's personal name) or 'title' (fallback to Parliament's display
    # title). Stable-first: a slug only changes when a 'title' slug can be upgraded to 'personal';
    # any old slug 301s to the current one because the numeric id is the key.
    slug_source       = db.Column(db.String(20))
    name              = db.Column(db.String(300), nullable=False)               # nameDisplayAs
    name_list_as      = db.Column(db.String(300))
    full_title        = db.Column(db.String(400))
    party             = db.Column(db.String(100))
    party_abbreviation = db.Column(db.String(20))
    party_colour      = db.Column(db.String(6))
    house             = db.Column(db.String(20))                                # 'Commons' | 'Lords'
    membership_from   = db.Column(db.String(200))                               # constituency, or 'Life peer' etc.
    membership_start  = db.Column(db.Date)
    is_current        = db.Column(db.Boolean, nullable=False, default=True)
    status_description = db.Column(db.String(200))
    thumbnail_url     = db.Column(db.String(500))
    current_role      = db.Column(db.String(500))                               # current govt/opposition post(s)

    # Lists of {name, start, end, info} — dates as 'YYYY-MM-DD' strings
    government_posts  = db.Column(db.JSON)
    opposition_posts  = db.Column(db.JSON)
    other_posts       = db.Column(db.JSON)
    committees        = db.Column(db.JSON)
    contact           = db.Column(db.JSON)    # [{type, address, phone, email, website}]
    interests         = db.Column(db.JSON)    # [{category, description}]
    representations   = db.Column(db.JSON)    # constituencies held: [{name, start, end, info}]
    house_memberships = db.Column(db.JSON)    # [{name, start, end, info}] e.g. Commons then Lords
    wikipedia_url     = db.Column(db.String(500))
    wiki_status       = db.Column(db.String(20))   # found | not_found | ambiguous | error
    wiki_checked_at   = db.Column(db.DateTime)
    wikidata_id       = db.Column(db.String(20))
    wikidata_facts    = db.Column(db.JSON)    # {education: [...], occupations: [...]} — bio fallback input

    # Activity derived from our own DB (ha_pq, ha_contribution) at refresh time
    wq_12m            = db.Column(db.Integer)
    wq_total          = db.Column(db.Integer)
    sessions_12m      = db.Column(db.Integer)
    contributions_12m = db.Column(db.Integer)
    last_spoke_on     = db.Column(db.Date)
    top_policy_areas  = db.Column(db.JSON)    # [{theme, sessions}] — debates, last 12 months
    wq_departments    = db.Column(db.JSON)    # [{department, questions}] — WQs asked, last 12 months

    summary_refreshed_at  = db.Column(db.DateTime)   # Members/Search pass
    details_refreshed_at  = db.Column(db.DateTime)   # Biography / Contact / Interests pass
    activity_refreshed_at = db.Column(db.DateTime)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    # Set explicitly by the refresh only when profile content changes (sitemap lastmod) — no onupdate,
    # because every run bumps the *_refreshed_at stamps.
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def last_updated(self):
        stamps = [s for s in (self.details_refreshed_at, self.summary_refreshed_at) if s]
        return max(stamps) if stamps else None


class MemberBiography(db.Model):
    """Pre-generated AI biography for a member (one row per member).

    Generated offline by scripts/refresh_members.py --regenerate-bios with a
    Gemini Flash-Lite class model. source_hash covers the prompt version and
    every input the prompt used, so a bio is only regenerated when its inputs
    change. Free feature: Gemini only, never Claude.
    """
    __tablename__ = 'member_biography'

    member_id       = db.Column(db.Integer, primary_key=True, autoincrement=False)
    bio_text        = db.Column(db.Text)              # NULL until a generation succeeds
    model_used      = db.Column(db.String(100))
    prompt_version  = db.Column(db.String(20))
    source_hash     = db.Column(db.String(64))
    sources         = db.Column(db.JSON)              # [{label, url}]
    generated_at    = db.Column(db.DateTime)
    last_attempt_at = db.Column(db.DateTime)
    last_error      = db.Column(db.String(500))
    # {method, verify_model, checked_claims, verified_claims, dropped_claims, dropped: [...]}.
    # Deferred: page reads never SELECT it, so pages keep working if the code is deployed before
    # migrations/members_profile_tables.py --execute adds the column.
    verification    = deferred(db.Column(db.JSON))


class MemberName(db.Model):
    """Personal name for a member alongside Parliament's display name/title.

    Filled by scripts/refresh_members.py from Wikidata (matched on P10428,
    "parliament.uk member ID") with Parliament's own name fields as fallback
    (MPs: display/list name; peers: the Members API synopsis "His/Her name is …").
    Never guessed. Rows with source='manual' are owner corrections and are not
    overwritten by the refresh.
    """
    __tablename__ = 'member_name'

    member_id       = db.Column(db.Integer, primary_key=True, autoincrement=False)
    personal_name   = db.Column(db.String(300))      # e.g. "Jacqui Smith"; NULL if not found
    given_name      = db.Column(db.String(200))
    family_name     = db.Column(db.String(200))
    peerage_title   = db.Column(db.String(300))      # e.g. "Baroness Smith of Malvern"; NULL for MPs
    full_title      = db.Column(db.String(400))      # Parliament's full title incl. honorifics/post-nominals
    wikidata_id     = db.Column(db.String(20))
    wikidata_name   = db.Column(db.String(300))      # what each source said, kept for the audit
    parliament_name = db.Column(db.String(300))
    source          = db.Column(db.String(20))       # wikidata | parliament | manual
    source_url      = db.Column(db.String(500))
    confidence      = db.Column(db.String(10))       # high | medium | low
    last_audited_at = db.Column(db.DateTime)
    audit_notes     = db.Column(db.String(1000))
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow)


class MemberNameAudit(db.Model):
    """One row per name audit run (refresh_members.py --audit-names): counts + the gaps to review."""
    __tablename__ = 'member_name_audit'

    id      = db.Column(db.Integer, primary_key=True)
    run_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    counts  = db.Column(db.JSON)     # matched, missing, changed, conflicts, wiki_not_found, ...
    gaps    = db.Column(db.JSON)     # [{member_id, name, issue, detail}]


class GovMinister(db.Model):
    """One row per current minister per department (a joint post gives one row
    in each department). Rebuilt daily by scripts/refresh_ministers.py from the
    Parliament Members API (government posts, with member ids) merged with
    GOV.UK's ministers page (portfolio titles, and anyone Parliament is missing).

    The Research Tool's department/minister pickers, minister flags and PQ
    answering-minister roles read from here (ministers.py) instead of calling
    GOV.UK or Parliament on page view. On production the table is created by
    migrations/gov_minister_table.py (the web service runs with SKIP_MIGRATIONS=1).
    """
    __tablename__ = 'gov_minister'

    id               = db.Column(db.Integer, primary_key=True)
    department       = db.Column(db.String(200), nullable=False, index=True)  # Parliament's name
    govuk_department = db.Column(db.String(200))                             # GOV.UK's name, if matched
    role_title       = db.Column(db.String(500), nullable=False)  # Parliament post name, else GOV.UK role
    govuk_role_title = db.Column(db.String(500))                  # GOV.UK portfolio title(s)
    member_name      = db.Column(db.String(300), nullable=False)  # display name, honorifics stripped
    govuk_name       = db.Column(db.String(300))                  # GOV.UK person title as given
    name_norm        = db.Column(db.String(300), nullable=False, index=True)
    mnis_id          = db.Column(db.Integer, index=True)          # Parliament member id, null if unmatched
    house            = db.Column(db.String(20))                   # 'Commons' | 'Lords'
    rank             = db.Column(db.Integer, nullable=False, default=0)  # order within the department
    is_cabinet       = db.Column(db.Boolean, nullable=False, default=False)
    start_date       = db.Column(db.Date)                         # when the current appointment began
    source           = db.Column(db.String(20), nullable=False)   # 'parliament+govuk' | 'parliament' | 'govuk'
    source_url       = db.Column(db.String(500))
    fetched_at       = db.Column(db.DateTime, nullable=False)
