"""
Relevance scoring for the stakeholder directory.

Design spec: docs/stakeholder-directory-design.md, Section 8.

Pure logic — no database writes, no Flask dependencies, no AI calls.
Import from ingesters or query endpoints; pass ORM objects or any
duck-typed equivalent that has the required attributes.

WeightsConfigError is raised at module import time if weights.yaml
references a source type not present in config/source_types.yaml.
"""
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from stakeholder_directory.vocab import SOURCE_TYPE_VALUES

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / 'config'


class WeightsConfigError(Exception):
    """Raised at import time if weights.yaml references an unknown source type."""


def _load_weights_yaml() -> dict:
    path = _CONFIG_DIR / 'weights.yaml'
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _build_defaults(data: dict) -> dict:
    """Parse and validate a weights config dict. Raises WeightsConfigError on bad keys."""
    source_weights = data.get('source_type_weights') or {}
    unknown = set(source_weights) - set(SOURCE_TYPE_VALUES)
    if unknown:
        raise WeightsConfigError(
            f"weights.yaml references unknown source types: {sorted(unknown)}. "
            f"Valid values: {sorted(SOURCE_TYPE_VALUES)}"
        )
    return {
        'source_type_weights': {k: float(v) for k, v in source_weights.items()},
        'cited_in_outcome_bonus': float(data.get('cited_in_outcome_bonus', 1.5)),
        'recency_half_life_days': float(data.get('recency_half_life_days', 1825.0)),
        'policy_area_match_multiplier': float(data.get('policy_area_match_multiplier', 2.0)),
        'department_match_multiplier': float(data.get('department_match_multiplier', 1.5)),
    }


_DEFAULT_WEIGHTS: dict = _build_defaults(_load_weights_yaml())


@dataclass
class ScoringQuery:
    """Parameters for a relevance query. All fields optional."""
    policy_area: str | None = None
    department: str | None = None
    recency_window: tuple[date, date] | None = None  # (start_date, end_date), inclusive


@dataclass
class EngagementBreakdown:
    """Per-engagement score components, for transparency and debugging."""
    engagement_id: int
    source_type: str
    engagement_date: date
    score: float
    source_weight: float
    recency_factor: float
    cited_bonus: float
    policy_area_mult: float
    department_mult: float


@dataclass
class RelevanceResult:
    """Output of compute_relevance."""
    organisation_id: int
    total_score: float
    breakdown: list[EngagementBreakdown] = field(default_factory=list)


def _recency_decay(engagement_date: date, reference_date: date, half_life_days: float) -> float:
    """Exponential decay: 1.0 at day 0, 0.5 at half_life_days, approaching 0 asymptotically."""
    days = (reference_date - engagement_date).days
    if days < 0:
        days = 0  # future-dated engagements treated as today
    return 0.5 ** (days / half_life_days)


def compute_relevance(
    organisation_id: int,
    engagements: list,
    query: ScoringQuery,
    weights: dict | None = None,
    reference_date: date | None = None,
) -> RelevanceResult:
    """Compute relevance score for an organisation against a query.

    Args:
        organisation_id: The sd_organisation.id.
        engagements:     List of objects with attributes: id, source_type,
                         engagement_date (date), cited_in_outcome (bool),
                         policy_area (str|None), department (str|None).
                         Accepts ORM Engagement objects or any duck-typed equivalent.
        query:           ScoringQuery specifying filters and match parameters.
        weights:         Optional dict to override defaults. Keys must match top-level
                         keys in weights.yaml. Partial overrides are merged; missing
                         keys fall back to defaults. source_type_weights sub-dict is
                         also merged (not replaced wholesale).
        reference_date:  Date from which recency is measured. Defaults to today.

    Returns:
        RelevanceResult with total_score and per-engagement breakdown sorted by
        score descending.
    """
    if reference_date is None:
        reference_date = date.today()

    w = _DEFAULT_WEIGHTS
    if weights is not None:
        w = {**_DEFAULT_WEIGHTS, **weights}
        if 'source_type_weights' in weights:
            # Merge sub-dict so a partial source_type_weights override doesn't
            # zero out all other source types
            w = {
                **w,
                'source_type_weights': {
                    **_DEFAULT_WEIGHTS['source_type_weights'],
                    **weights['source_type_weights'],
                },
            }

    breakdown: list[EngagementBreakdown] = []
    total = 0.0

    for eng in engagements:
        if query.recency_window is not None:
            start, end = query.recency_window
            if not (start <= eng.engagement_date <= end):
                continue

        source_weight = w['source_type_weights'].get(eng.source_type, 0.0)
        recency = _recency_decay(eng.engagement_date, reference_date, w['recency_half_life_days'])
        cited = 1.0 + w['cited_in_outcome_bonus'] if eng.cited_in_outcome else 1.0
        pa_mult = (
            w['policy_area_match_multiplier']
            if query.policy_area and eng.policy_area == query.policy_area
            else 1.0
        )
        dept_mult = (
            w['department_match_multiplier']
            if query.department and eng.department == query.department
            else 1.0
        )

        score = source_weight * recency * cited * pa_mult * dept_mult
        total += score

        breakdown.append(EngagementBreakdown(
            engagement_id=eng.id,
            source_type=eng.source_type,
            engagement_date=eng.engagement_date,
            score=score,
            source_weight=source_weight,
            recency_factor=recency,
            cited_bonus=cited,
            policy_area_mult=pa_mult,
            department_mult=dept_mult,
        ))

    return RelevanceResult(
        organisation_id=organisation_id,
        total_score=round(total, 6),
        breakdown=sorted(breakdown, key=lambda b: b.score, reverse=True),
    )
