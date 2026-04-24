"""Unit tests for stakeholder_directory.scoring.

Run from project root: python -m pytest stakeholder_directory/tests/test_scoring.py -v
"""
import pytest
from datetime import date
from types import SimpleNamespace

from stakeholder_directory.scoring import (
    compute_relevance,
    ScoringQuery,
    RelevanceResult,
    EngagementBreakdown,
    WeightsConfigError,
    _DEFAULT_WEIGHTS,
    _recency_decay,
    _build_defaults,
)


def _eng(
    eid=1,
    source_type='consultation_response',
    engagement_date=date(2025, 1, 1),
    cited_in_outcome=False,
    policy_area=None,
    department=None,
):
    """Build a minimal fake Engagement object (no ORM needed)."""
    return SimpleNamespace(
        id=eid,
        source_type=source_type,
        engagement_date=engagement_date,
        cited_in_outcome=cited_in_outcome,
        policy_area=policy_area,
        department=department,
    )


REF = date(2025, 1, 1)  # fixed reference date for deterministic tests


# ---------------------------------------------------------------------------
# 1. Base case
# ---------------------------------------------------------------------------

class TestBaseCase:
    def test_single_engagement_no_multipliers(self):
        eng = _eng(source_type='consultation_response', engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(), reference_date=REF)
        # At day 0: recency=1.0, source_weight=1.0, no multipliers → score=1.0
        assert result.organisation_id == 1
        assert result.total_score == pytest.approx(1.0)
        assert len(result.breakdown) == 1
        bd = result.breakdown[0]
        assert bd.source_weight == pytest.approx(1.0)
        assert bd.recency_factor == pytest.approx(1.0)
        assert bd.cited_bonus == pytest.approx(1.0)
        assert bd.policy_area_mult == pytest.approx(1.0)
        assert bd.department_mult == pytest.approx(1.0)

    def test_result_type(self):
        result = compute_relevance(7, [_eng()], ScoringQuery(), reference_date=REF)
        assert isinstance(result, RelevanceResult)
        assert isinstance(result.breakdown[0], EngagementBreakdown)


# ---------------------------------------------------------------------------
# 2. Empty engagements
# ---------------------------------------------------------------------------

class TestEmptyEngagements:
    def test_empty_engagements_returns_zero(self):
        result = compute_relevance(42, [], ScoringQuery(), reference_date=REF)
        assert result.organisation_id == 42
        assert result.total_score == 0.0
        assert result.breakdown == []


# ---------------------------------------------------------------------------
# 3. Recency decay
# ---------------------------------------------------------------------------

class TestRecencyDecay:
    def test_decay_at_half_life(self):
        half_life = _DEFAULT_WEIGHTS['recency_half_life_days']
        eng_date = date.fromordinal(REF.toordinal() - int(half_life))
        eng = _eng(source_type='consultation_response', engagement_date=eng_date)
        result = compute_relevance(1, [eng], ScoringQuery(), reference_date=REF)
        # consultation_response weight=1.0; recency at half-life=0.5 → score≈0.5
        assert result.total_score == pytest.approx(0.5, rel=1e-4)

    def test_higher_source_weight_scales_with_decay(self):
        half_life = _DEFAULT_WEIGHTS['recency_half_life_days']
        ow = _DEFAULT_WEIGHTS['source_type_weights']['oral_evidence_committee']  # 4.0
        eng_date = date.fromordinal(REF.toordinal() - int(half_life))
        eng = _eng(source_type='oral_evidence_committee', engagement_date=eng_date)
        result = compute_relevance(1, [eng], ScoringQuery(), reference_date=REF)
        assert result.total_score == pytest.approx(ow * 0.5, rel=1e-4)

    def test_future_date_clamped_to_zero_days(self):
        future = date(2030, 1, 1)
        eng = _eng(source_type='consultation_response', engagement_date=future)
        result = compute_relevance(1, [eng], ScoringQuery(), reference_date=REF)
        # Negative days clamped to 0 → decay=1.0 → score=1.0
        assert result.total_score == pytest.approx(1.0)

    def test_recency_decay_helper_at_day_zero(self):
        assert _recency_decay(REF, REF, 1825.0) == pytest.approx(1.0)

    def test_recency_window_filter_excludes_out_of_range(self):
        eng_in = _eng(eid=1, engagement_date=date(2024, 6, 1))
        eng_out = _eng(eid=2, engagement_date=date(2023, 1, 1))
        window = (date(2024, 1, 1), date(2025, 1, 1))
        result = compute_relevance(1, [eng_in, eng_out], ScoringQuery(recency_window=window), reference_date=REF)
        assert len(result.breakdown) == 1
        assert result.breakdown[0].engagement_id == 1


# ---------------------------------------------------------------------------
# 4. Cited-in-outcome bonus
# ---------------------------------------------------------------------------

class TestCitedBonus:
    def test_cited_multiplies_by_one_plus_bonus(self):
        bonus = _DEFAULT_WEIGHTS['cited_in_outcome_bonus']
        cited = _eng(eid=1, cited_in_outcome=True, engagement_date=REF)
        uncited = _eng(eid=2, cited_in_outcome=False, engagement_date=REF)
        res_c = compute_relevance(1, [cited], ScoringQuery(), reference_date=REF)
        res_u = compute_relevance(1, [uncited], ScoringQuery(), reference_date=REF)
        assert res_c.total_score == pytest.approx(res_u.total_score * (1.0 + bonus))

    def test_cited_bonus_reflected_in_breakdown(self):
        bonus = _DEFAULT_WEIGHTS['cited_in_outcome_bonus']
        eng = _eng(cited_in_outcome=True, engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(), reference_date=REF)
        assert result.breakdown[0].cited_bonus == pytest.approx(1.0 + bonus)


# ---------------------------------------------------------------------------
# 5. Policy area match
# ---------------------------------------------------------------------------

class TestPolicyAreaMatch:
    def test_match_applies_multiplier(self):
        mult = _DEFAULT_WEIGHTS['policy_area_match_multiplier']
        eng = _eng(policy_area='education', engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(policy_area='education'), reference_date=REF)
        assert result.total_score == pytest.approx(1.0 * mult)
        assert result.breakdown[0].policy_area_mult == pytest.approx(mult)

    def test_mismatch_no_multiplier(self):
        eng = _eng(policy_area='health', engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(policy_area='education'), reference_date=REF)
        assert result.total_score == pytest.approx(1.0)

    def test_none_policy_area_on_engagement_no_multiplier(self):
        eng = _eng(policy_area=None, engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(policy_area='education'), reference_date=REF)
        assert result.total_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 6. Department match
# ---------------------------------------------------------------------------

class TestDepartmentMatch:
    def test_match_applies_multiplier(self):
        mult = _DEFAULT_WEIGHTS['department_match_multiplier']
        eng = _eng(department='department_for_education', engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(department='department_for_education'), reference_date=REF)
        assert result.total_score == pytest.approx(1.0 * mult)
        assert result.breakdown[0].department_mult == pytest.approx(mult)

    def test_mismatch_no_multiplier(self):
        eng = _eng(department='home_office', engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(department='department_for_education'), reference_date=REF)
        assert result.total_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 7. All multipliers stacked
# ---------------------------------------------------------------------------

class TestAllMultipliersStacked:
    def test_all_multipliers_combined(self):
        w = _DEFAULT_WEIGHTS
        src_w = w['source_type_weights']['oral_evidence_committee']  # 4.0
        bonus = w['cited_in_outcome_bonus']                           # 1.5
        pa_m = w['policy_area_match_multiplier']                      # 2.0
        dept_m = w['department_match_multiplier']                     # 1.5
        eng = _eng(
            source_type='oral_evidence_committee',
            engagement_date=REF,
            cited_in_outcome=True,
            policy_area='education',
            department='department_for_education',
        )
        query = ScoringQuery(policy_area='education', department='department_for_education')
        result = compute_relevance(1, [eng], query, reference_date=REF)
        expected = src_w * 1.0 * (1.0 + bonus) * pa_m * dept_m
        assert result.total_score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 8. Multiple engagements
# ---------------------------------------------------------------------------

class TestMultipleEngagements:
    def test_score_is_sum_of_all_engagement_scores(self):
        stw = _DEFAULT_WEIGHTS['source_type_weights']
        engagements = [
            _eng(eid=1, source_type='oral_evidence_committee', engagement_date=REF),
            _eng(eid=2, source_type='consultation_response', engagement_date=REF),
            _eng(eid=3, source_type='ministerial_meeting', engagement_date=REF),
        ]
        result = compute_relevance(1, engagements, ScoringQuery(), reference_date=REF)
        expected = (
            stw['oral_evidence_committee']
            + stw['consultation_response']
            + stw['ministerial_meeting']
        )
        assert result.total_score == pytest.approx(expected)
        assert len(result.breakdown) == 3

    def test_breakdown_sorted_by_score_descending(self):
        engagements = [
            _eng(eid=1, source_type='lobbying_register', engagement_date=REF),       # 0.5
            _eng(eid=2, source_type='oral_evidence_committee', engagement_date=REF),  # 4.0
            _eng(eid=3, source_type='consultation_response', engagement_date=REF),    # 1.0
        ]
        result = compute_relevance(1, engagements, ScoringQuery(), reference_date=REF)
        scores = [b.score for b in result.breakdown]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# 9. Custom weights override
# ---------------------------------------------------------------------------

class TestCustomWeights:
    def test_custom_source_weight(self):
        custom = {'source_type_weights': {'consultation_response': 99.0}}
        eng = _eng(source_type='consultation_response', engagement_date=REF)
        result = compute_relevance(1, [eng], ScoringQuery(), weights=custom, reference_date=REF)
        assert result.total_score == pytest.approx(99.0)

    def test_partial_source_weight_override_preserves_other_weights(self):
        # Override only consultation_response; oral_evidence_committee should keep default
        custom = {'source_type_weights': {'consultation_response': 50.0}}
        engagements = [
            _eng(eid=1, source_type='consultation_response', engagement_date=REF),
            _eng(eid=2, source_type='oral_evidence_committee', engagement_date=REF),
        ]
        result = compute_relevance(1, engagements, ScoringQuery(), weights=custom, reference_date=REF)
        oral_default = _DEFAULT_WEIGHTS['source_type_weights']['oral_evidence_committee']
        assert result.total_score == pytest.approx(50.0 + oral_default)

    def test_custom_half_life_affects_decay(self):
        custom = {'recency_half_life_days': 1.0}
        eng_date = date.fromordinal(REF.toordinal() - 1)  # 1 day old
        eng = _eng(source_type='consultation_response', engagement_date=eng_date)
        result = compute_relevance(1, [eng], ScoringQuery(), weights=custom, reference_date=REF)
        # half_life=1, days=1 → decay=0.5; source_weight=1.0 → score=0.5
        assert result.total_score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 10. Config validation error
# ---------------------------------------------------------------------------

class TestConfigValidation:
    def test_unknown_source_type_raises_weights_config_error(self):
        bad_data = {
            'source_type_weights': {'nonexistent_source_type_xyz': 1.0},
            'cited_in_outcome_bonus': 1.5,
            'recency_half_life_days': 1825,
            'policy_area_match_multiplier': 2.0,
            'department_match_multiplier': 1.5,
        }
        with pytest.raises(WeightsConfigError, match='nonexistent_source_type_xyz'):
            _build_defaults(bad_data)

    def test_valid_weights_config_does_not_raise(self):
        good_data = {
            'source_type_weights': {'consultation_response': 1.0},
            'cited_in_outcome_bonus': 1.5,
            'recency_half_life_days': 1825,
            'policy_area_match_multiplier': 2.0,
            'department_match_multiplier': 1.5,
        }
        result = _build_defaults(good_data)
        assert result['source_type_weights']['consultation_response'] == 1.0
