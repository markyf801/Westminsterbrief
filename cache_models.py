import hashlib
import json
from datetime import datetime, timedelta
from extensions import db


class MemberLink(db.Model):
    """Persistent cross-reference: Parliament member ID <-> TWFY person ID.

    Parliament IDs are the authoritative source (members-api.parliament.uk).
    TWFY person IDs are needed for debate search (theyworkforyou.com/api).
    Both IDs are assigned for life — a resolved row never needs updating.
    Rows are added lazily as ministers are encountered in searches.
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

    @staticmethod
    def get_by_parliament_id(parliament_id):
        return MemberLink.query.filter_by(parliament_id=int(parliament_id)).first()

    @staticmethod
    def get_by_twfy_id(twfy_person_id):
        return MemberLink.query.filter_by(twfy_person_id=str(twfy_person_id)).first()

    @staticmethod
    def upsert(parliament_id, display_name, house,
               twfy_person_id=None, twfy_name=None,
               resolution_method=None, lookup_failed=False):
        """Insert or update a MemberLink row. Safe to call multiple times."""
        existing = MemberLink.query.filter_by(parliament_id=int(parliament_id)).first()
        if existing:
            # Only update TWFY fields if we have new information
            if twfy_person_id and not existing.twfy_person_id:
                existing.twfy_person_id = str(twfy_person_id)
                existing.twfy_name = twfy_name
                existing.resolution_method = resolution_method
                existing.lookup_failed = False
                existing.resolved_at = datetime.utcnow()
            elif lookup_failed and not existing.twfy_person_id:
                existing.lookup_failed = True
                existing.resolution_method = 'failed'
        else:
            row = MemberLink(
                parliament_id=int(parliament_id),
                display_name=display_name,
                house=house,
                twfy_person_id=str(twfy_person_id) if twfy_person_id else None,
                twfy_name=twfy_name,
                resolution_method=resolution_method,
                lookup_failed=lookup_failed,
                resolved_at=datetime.utcnow() if twfy_person_id else None,
            )
            db.session.add(row)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    @staticmethod
    def stats():
        """Return dict of resolution stats for admin page."""
        total    = MemberLink.query.count()
        resolved = MemberLink.query.filter(
            MemberLink.twfy_person_id.isnot(None)).count()
        failed   = MemberLink.query.filter_by(lookup_failed=True).count()
        pending  = total - resolved - failed
        return {'total': total, 'resolved': resolved,
                'failed': failed, 'pending': pending}


class CachedTWFYSearch(db.Model):
    """Caches TWFY API search results to reduce API call usage.
    TTL: 6 hours for date-filtered searches, 24 hours for open searches."""
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
        key = CachedTWFYSearch.make_key(query, source_type)
        entry = CachedTWFYSearch.query.filter_by(cache_key=key).first()
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
        key = CachedTWFYSearch.make_key(query, source_type)
        existing = CachedTWFYSearch.query.filter_by(cache_key=key).first()
        data = json.dumps(results)
        if existing:
            existing.results_json = data
            existing.cached_at = datetime.utcnow()
        else:
            db.session.add(CachedTWFYSearch(
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
    hansard_search_name = db.Column(db.String(200), nullable=True)  # override for TWFY search
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
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_fresh(self):
        return (datetime.utcnow() - self.cached_at) < timedelta(days=7)

    @staticmethod
    def get(member_id):
        entry = CachedMember.query.filter_by(member_id=int(member_id)).first()
        if entry and entry.is_fresh():
            return entry
        return None

    @staticmethod
    def store(member_id, name, party, constituency, house, image_url):
        existing = CachedMember.query.filter_by(member_id=int(member_id)).first()
        if existing:
            existing.name = name
            existing.party = party
            existing.constituency = constituency
            existing.house = house
            existing.image_url = image_url
            existing.cached_at = datetime.utcnow()
        else:
            db.session.add(CachedMember(
                member_id=int(member_id), name=name, party=party,
                constituency=constituency, house=house, image_url=image_url
            ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
