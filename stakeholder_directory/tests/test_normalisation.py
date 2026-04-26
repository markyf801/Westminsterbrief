"""
Tests for the normalisation pass (Tier 1–4 dedup, commit, flag creation).

Run: python -m pytest stakeholder_directory/tests/test_normalisation.py -v
"""
import pytest
from datetime import date, datetime
from types import SimpleNamespace

from stakeholder_directory.normalisation.string_utils import (
    normalise_for_match,
    expand_aliases,
    strip_legal_suffixes,
)
from stakeholder_directory.normalisation.similarity import compute_similarity
from stakeholder_directory.normalisation.normaliser import (
    normalise_pending_staging,
    NormalisationResult,
    TIER2_THRESHOLD,
    TIER3_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEPT = 'department_for_education'
SOURCE_URL = 'https://www.gov.uk/test-normalisation'


def _make_staging_row(db, org_name, minister='Test Minister',
                      dept=DEPT, meeting_date=None, source_url=SOURCE_URL):
    """Insert a pending StagingMinisterialMeeting row and return it."""
    from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting
    import json
    row = StagingMinisterialMeeting(
        raw_organisation_name=org_name,
        minister_name=minister,
        department=dept,
        meeting_date=meeting_date or date(2025, 1, 15),
        source_url=source_url,
        source_csv_row=json.dumps({'org': org_name}),
        ingested_at=datetime.utcnow(),
        processing_status='pending',
    )
    db.session.add(row)
    db.session.commit()
    return row


def _make_org(db, canonical_name, canonical_url=None, registration_number=None,
              org_type='membership_body'):
    """Insert an Organisation and return it."""
    from stakeholder_directory.models import Organisation
    org = Organisation(
        canonical_name=canonical_name,
        type=org_type,
        scope='national',
        status='active',
        canonical_url=canonical_url,
        registration_number=registration_number,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.session.add(org)
    db.session.commit()
    return org


def _make_alias(db, org, alias_name, source='test'):
    """Insert an Alias for an org and return it."""
    from stakeholder_directory.models import Alias
    alias = Alias(organisation_id=org.id, alias_name=alias_name, source=source)
    db.session.add(alias)
    db.session.commit()
    return alias


def _add_engagement(db, org, source_url='https://different.gov.uk/1'):
    """Add an Engagement to an org (for safety-rule tests)."""
    from stakeholder_directory.models import Engagement
    eng = Engagement(
        organisation_id=org.id,
        source_type='ministerial_meeting',
        source_url=source_url,
        engagement_date=date(2024, 6, 1),
        ingested_at=datetime.utcnow(),
        ingester_source='test',
    )
    db.session.add(eng)
    db.session.commit()
    return eng


# ---------------------------------------------------------------------------
# String utilities
# ---------------------------------------------------------------------------

class TestStripLegalSuffixes:
    def test_strips_ltd(self):
        assert strip_legal_suffixes('Acme Ltd').strip() == 'Acme'

    def test_strips_limited(self):
        assert strip_legal_suffixes('Acme Limited').strip() == 'Acme'

    def test_strips_plc(self):
        assert strip_legal_suffixes('Megacorp plc').strip() == 'Megacorp'

    def test_strips_llp(self):
        assert strip_legal_suffixes('Smith & Partners LLP').strip() == 'Smith & Partners'

    def test_no_suffix_unchanged(self):
        assert strip_legal_suffixes('Universities UK') == 'Universities UK'


class TestExpandAliases:
    def test_known_alias_returns_canonical(self):
        aliases = {'Universities UK': ['UUK']}
        assert expand_aliases('UUK', aliases) == 'Universities UK'

    def test_case_insensitive(self):
        aliases = {'Universities UK': ['UUK']}
        assert expand_aliases('uuk', aliases) == 'Universities UK'

    def test_unknown_name_unchanged(self):
        aliases = {'Universities UK': ['UUK']}
        assert expand_aliases('Ofsted', aliases) == 'Ofsted'

    def test_canonical_name_returns_itself(self):
        aliases = {'Universities UK': ['UUK']}
        assert expand_aliases('Universities UK', aliases) == 'Universities UK'


class TestNormaliseForMatch:
    def test_lowercases(self):
        assert normalise_for_match('Universities UK') == 'universities uk'

    def test_strips_punctuation(self):
        result = normalise_for_match('U.U.K.')
        assert '.' not in result

    def test_collapses_whitespace(self):
        result = normalise_for_match('Russell  Group')
        assert '  ' not in result

    def test_legal_suffix_stripped(self):
        result = normalise_for_match('Universities UK Ltd')
        assert 'ltd' not in result

    def test_alias_expanded(self):
        aliases = {'Universities UK': ['UUK']}
        result = normalise_for_match('UUK', aliases)
        assert result == 'universities uk'


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

class TestComputeSimilarity:
    def test_identical_names(self):
        assert compute_similarity('Universities UK', 'Universities UK') == 1.0

    def test_similar_names(self):
        sim = compute_similarity('Russell Group', 'The Russell Group')
        assert sim > 0.80

    def test_dissimilar_names(self):
        sim = compute_similarity('Ofsted', 'British Medical Association')
        assert sim < 0.50

    def test_word_reordering(self):
        sim = compute_similarity('Royal College of Nursing', 'Nursing, Royal College of')
        assert sim > 0.90


# ---------------------------------------------------------------------------
# Tier 1 — exact match
# ---------------------------------------------------------------------------

class TestTier1ExactMatch:
    def test_exact_match_links_to_existing_org(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement

        org = _make_org(db, 'Universities UK')
        _make_staging_row(db, 'Universities UK')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier1_auto_merged == 1
        assert result.tier4_new_org == 0

        eng = db.session.query(Engagement).first()
        assert eng is not None
        assert eng.organisation_id == org.id

    def test_case_insensitive_exact_match(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement

        org = _make_org(db, 'Universities UK')
        _make_staging_row(db, 'universities uk')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier1_auto_merged == 1
        eng = db.session.query(Engagement).first()
        assert eng.organisation_id == org.id

    def test_alias_in_yaml_matches_canonical(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement

        org = _make_org(db, 'Universities UK')
        _make_staging_row(db, 'UUK')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier1_auto_merged == 1
        eng = db.session.query(Engagement).first()
        assert eng.organisation_id == org.id

    def test_stored_alias_matches(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement

        org = _make_org(db, 'Universities UK')
        _make_alias(db, org, 'U.U.K.')
        _make_staging_row(db, 'U.U.K.')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier1_auto_merged == 1
        eng = db.session.query(Engagement).first()
        assert eng.organisation_id == org.id

    def test_legal_suffix_stripped_matches(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement, Alias

        org = _make_org(db, 'Universities UK')
        _make_staging_row(db, 'Universities UK Ltd')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier1_auto_merged == 1

        # Alias "Universities UK Ltd" should be recorded
        alias = db.session.query(Alias).filter_by(
            organisation_id=org.id, alias_name='Universities UK Ltd'
        ).first()
        assert alias is not None


# ---------------------------------------------------------------------------
# Tier 2 — similarity + corroborator
# ---------------------------------------------------------------------------

class TestTier2SimilarityCorroborated:
    def test_staging_high_sim_org_has_url_falls_to_tier3(self, app):
        """Staging rows carry no URL, so Tier 2 cannot fire even if the existing
        org has a canonical_url. The match falls to Tier 3."""
        from extensions import db

        _make_org(db, 'Russell Group', canonical_url='https://russellgroup.ac.uk')
        _make_staging_row(db, 'Russel Group')  # 0.92 similarity

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier2_auto_merged == 0
        assert result.tier3_flagged == 1

    def test_staging_high_sim_org_no_url_falls_to_tier3(self, app):
        """No identifiers on either side — still falls to Tier 3 on similarity alone."""
        from extensions import db

        _make_org(db, 'Russell Group')
        _make_staging_row(db, 'Russel Group')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier2_auto_merged == 0
        assert result.tier3_flagged == 1

    def test_safety_rule_not_triggered_from_staging(self, app):
        """Safety rule lives inside the Tier 2 corroborator path. Since
        staging-to-org Tier 2 never fires, safety_blocks stays 0; the
        match still flags via Tier 3."""
        from extensions import db

        org = _make_org(db, 'Russell Group', canonical_url='https://russellgroup.ac.uk')
        _add_engagement(db, org, source_url='https://gov.uk/source-1')
        _add_engagement(db, org, source_url='https://gov.uk/source-2')
        _make_staging_row(db, 'Russel Group')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.safety_blocks == 0
        assert result.tier3_flagged == 1
        assert result.tier2_auto_merged == 0

    def test_corroborator_helper_both_orgs_same_url_returns_true(self, app):
        """The org-vs-org corroborator helper correctly identifies matching URLs.
        This is the path Tier 2 will use in a future org-vs-org merge pass."""
        from extensions import db
        from stakeholder_directory.normalisation.normaliser import _corroborator_match_between_orgs

        org_a = _make_org(db, 'Russell Group', canonical_url='https://russellgroup.ac.uk')
        org_b = _make_org(db, 'The Russell Group', canonical_url='https://russellgroup.ac.uk/about')

        assert _corroborator_match_between_orgs(org_a, org_b) is True

    def test_corroborator_helper_different_urls_returns_false(self, app):
        """Different hosts → no corroboration."""
        from extensions import db
        from stakeholder_directory.normalisation.normaliser import _corroborator_match_between_orgs

        org_a = _make_org(db, 'Russell Group', canonical_url='https://russellgroup.ac.uk')
        org_b = _make_org(db, 'The Russell Group', canonical_url='https://different.ac.uk')

        assert _corroborator_match_between_orgs(org_a, org_b) is False

    def test_corroborator_helper_matching_registration_number(self, app):
        """Shared registration number is a valid corroborator."""
        from extensions import db
        from stakeholder_directory.normalisation.normaliser import _corroborator_match_between_orgs

        org_a = _make_org(db, 'Russell Group', registration_number='RC123456')
        org_b = _make_org(db, 'The Russell Group', registration_number='RC123456')

        assert _corroborator_match_between_orgs(org_a, org_b) is True


# ---------------------------------------------------------------------------
# Tier 3 — flag for human review
# ---------------------------------------------------------------------------

class TestTier3Flagging:
    def test_high_similarity_no_corroborator_creates_flag(self, app):
        """Similarity >=0.85 with no corroborator falls to Tier 3 (flag for review)."""
        from extensions import db
        from stakeholder_directory.models import Flag

        _make_org(db, 'Russell Group')
        _make_staging_row(db, 'The Russell Group of Universities')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 1

        flag = db.session.query(Flag).first()
        assert flag is not None
        assert flag.flag_type == 'possible_duplicate'

    def test_below_tier3_threshold_creates_new_org(self, app):
        """Similarity <0.90 (e.g. 0.78) falls to Tier 4 — not flagged, treated as distinct."""
        from extensions import db
        from stakeholder_directory.models import Organisation, Flag

        _make_org(db, 'ARK Schools')
        _make_staging_row(db, 'Lift Schools')  # similarity ~0.78

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 0
        assert result.tier4_new_org == 1
        assert db.session.query(Flag).count() == 0

    def test_similarity_88_falls_to_tier4_not_tier3(self, app):
        """Similarity between 0.85 and 0.90 now falls to Tier 4 (threshold raised to 0.90).
        Previously (threshold=0.85) this would have been Tier 3; now treated as distinct."""
        from extensions import db
        from stakeholder_directory.models import Flag
        from stakeholder_directory.normalisation.similarity import compute_similarity

        existing_name = 'National Education Trust'
        staging_name = 'National Education Union'

        sim = compute_similarity(existing_name, staging_name)
        assert 0.85 <= sim < TIER3_THRESHOLD, (
            f"Test requires similarity in [0.85, {TIER3_THRESHOLD}), got {sim:.2f}. "
            "Choose org names that produce similarity in this range."
        )

        _make_org(db, existing_name)
        _make_staging_row(db, staging_name)

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 0
        assert result.tier4_new_org == 1
        assert db.session.query(Flag).count() == 0

    def test_multiple_candidates_flags_highest(self, app):
        from extensions import db
        from stakeholder_directory.models import Flag

        _make_org(db, 'Russell Group')
        _make_org(db, 'Russell Commission')
        _make_org(db, 'Russell Trust')
        _make_staging_row(db, 'The Russell Group of Universities')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 1

        flags = db.session.query(Flag).all()
        assert len(flags) == 1
        # Highest candidate should be Russell Group
        assert 'Russell Group' in flags[0].detail


# ---------------------------------------------------------------------------
# Distinct-org pairs exclusion
# ---------------------------------------------------------------------------

class TestDistinctOrgPairs:
    def test_distinct_pair_not_flagged(self, app):
        """Orgs in distinct_orgs.yaml are not flagged despite high similarity."""
        from extensions import db
        from stakeholder_directory.models import Flag

        # "NASUWT" in distinct_orgs.yaml expands via aliases.yaml to the full name.
        _make_org(db, 'National Association of Schoolmasters Union of Women Teachers')
        _make_staging_row(db, 'National Association of Head Teachers')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 0
        assert result.tier4_new_org == 1
        assert db.session.query(Flag).count() == 0

    def test_distinct_pair_symmetric(self, app):
        """Distinct pair check is symmetric — order within the YAML pair does not matter."""
        from extensions import db
        from stakeholder_directory.models import Flag

        _make_org(db, 'National Association of Head Teachers')
        _make_staging_row(db, 'National Association of Schoolmasters Union of Women Teachers')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 0
        assert result.tier4_new_org == 1
        assert db.session.query(Flag).count() == 0

    def test_non_distinct_pair_still_flags(self, app):
        """Orgs NOT in distinct_orgs.yaml are still flagged at Tier 3."""
        from extensions import db
        from stakeholder_directory.models import Flag

        _make_org(db, 'Russell Group')
        _make_staging_row(db, 'The Russell Group of Universities')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier3_flagged == 1
        assert db.session.query(Flag).count() == 1


# ---------------------------------------------------------------------------
# Tier 4 — new organisation
# ---------------------------------------------------------------------------

class TestTier4NewOrg:
    def test_new_org_created_with_correct_defaults(self, app):
        from extensions import db
        from stakeholder_directory.models import Organisation

        _make_staging_row(db, 'Completely New Organisation XYZ')

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.tier4_new_org == 1

        org = db.session.query(Organisation).first()
        assert org is not None
        assert org.canonical_name == 'Completely New Organisation XYZ'
        assert org.type == 'unknown'
        assert org.scope == 'national'
        assert org.status == 'active'

    def test_new_org_engagement_linked_correctly(self, app):
        from extensions import db
        from stakeholder_directory.models import Organisation, Engagement

        _make_staging_row(db, 'Completely New Organisation XYZ',
                          meeting_date=date(2025, 3, 10))

        normalise_pending_staging('staging_ministerial_meeting')

        org = db.session.query(Organisation).first()
        eng = db.session.query(Engagement).first()
        assert eng is not None
        assert eng.organisation_id == org.id
        assert eng.engagement_date == date(2025, 3, 10)
        assert eng.source_type == 'ministerial_meeting'


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_no_pending_rows_returns_zero_counts(self, app):
        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.staging_records_processed == 0
        assert result.tier4_new_org == 0

    def test_rerun_after_commit_produces_no_changes(self, app):
        from extensions import db
        from stakeholder_directory.models import Organisation

        _make_staging_row(db, 'Completely New Organisation XYZ')
        normalise_pending_staging('staging_ministerial_meeting')

        count_orgs_first = db.session.query(Organisation).count()

        result2 = normalise_pending_staging('staging_ministerial_meeting')
        assert result2.staging_records_processed == 0
        assert db.session.query(Organisation).count() == count_orgs_first

    def test_committed_rows_skipped(self, app):
        from extensions import db
        from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting
        import json

        # Insert a row already committed
        row = StagingMinisterialMeeting(
            raw_organisation_name='Already Done',
            minister_name='Minister',
            department=DEPT,
            meeting_date=date(2025, 1, 1),
            source_url=SOURCE_URL,
            source_csv_row=json.dumps({}),
            ingested_at=datetime.utcnow(),
            processing_status='committed',
        )
        db.session.add(row)
        db.session.commit()

        result = normalise_pending_staging('staging_ministerial_meeting')
        assert result.staging_records_processed == 0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_correct_counts(self, app):
        from extensions import db

        _make_staging_row(db, 'Brand New Org ABC')
        _make_staging_row(db, 'Another New Org DEF', source_url=SOURCE_URL + '/2')

        result = normalise_pending_staging('staging_ministerial_meeting', dry_run=True)
        assert result.tier4_new_org == 2

    def test_dry_run_writes_no_orgs(self, app):
        from extensions import db
        from stakeholder_directory.models import Organisation

        _make_staging_row(db, 'Brand New Org ABC')
        normalise_pending_staging('staging_ministerial_meeting', dry_run=True)

        assert db.session.query(Organisation).count() == 0

    def test_dry_run_writes_no_engagements(self, app):
        from extensions import db
        from stakeholder_directory.models import Engagement

        _make_staging_row(db, 'Brand New Org ABC')
        normalise_pending_staging('staging_ministerial_meeting', dry_run=True)

        assert db.session.query(Engagement).count() == 0

    def test_dry_run_writes_no_flags(self, app):
        from extensions import db
        from stakeholder_directory.models import Flag

        _make_org(db, 'Russell Group')
        _make_staging_row(db, 'The Russell Group of Universities')
        normalise_pending_staging('staging_ministerial_meeting', dry_run=True)

        assert db.session.query(Flag).count() == 0


# ---------------------------------------------------------------------------
# Flag detail format
# ---------------------------------------------------------------------------

class TestFlagDetail:
    def test_tier3_flag_detail_format(self, app):
        from extensions import db
        from stakeholder_directory.models import Flag

        existing = _make_org(db, 'Russell Group')
        _make_staging_row(db, 'The Russell Group of Universities')

        normalise_pending_staging('staging_ministerial_meeting')

        flag = db.session.query(Flag).first()
        assert flag is not None
        assert 'Possible duplicate' in flag.detail
        assert 'similarity' in flag.detail
        assert 'Russell Group' in flag.detail
        assert f'org_id={existing.id}' in flag.detail
        assert 'Corroborator' in flag.detail
