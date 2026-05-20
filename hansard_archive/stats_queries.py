"""
hansard_archive/stats_queries.py

Reusable query helpers for HeadlineStat and StatObservation data.

SWR pattern: HeadlineStat rows follow a stale-while-revalidate approach.
On a refresh failure, last_refreshed is updated but previous observation
values are preserved so pages continue rendering during transient outages.

Phase 1 note: HeadlineStat.latest_value still exists as a legacy column
(used by the admin UI and stats_refresh.py pipeline). It will be retired
once the admin UI and ingestion are updated to write StatObservation rows
directly. Until then:
- get_headline_stat() filters on latest_value.isnot(None) (legacy path)
- get_latest_observation() queries StatObservation (Phase 1+ path)
"""

from __future__ import annotations

from datetime import date

from hansard_archive.models import HeadlineStat, StatObservation


def get_headline_stat(theme_slug: str) -> HeadlineStat | None:
    """
    Return the first HeadlineStat for the given theme slug that has a
    non-null latest_value (legacy column), or None if no such row exists.

    Used on /brief/<slug> (teaser line) and /stats/<slug> (full card).
    Will be superseded by get_latest_observation() once ingestion migrates.
    """
    return (
        HeadlineStat.query
        .filter_by(theme_slug=theme_slug)
        .filter(HeadlineStat.latest_value.isnot(None))
        .first()
    )


def get_latest_observation(
    stat: HeadlineStat,
    include_straddling: bool = False,
) -> StatObservation | None:
    """
    Return the most recent StatObservation for the given HeadlineStat,
    or None if no observations exist.

    Straddling observations (period_start < 2024-07-01 and period_end >=
    2024-07-01) are excluded by default. Pass include_straddling=True to
    include them — only needed when displaying historical context.
    """
    q = (
        StatObservation.query
        .filter_by(headline_stat_id=stat.id)
        .order_by(StatObservation.period_end.desc().nullslast())
    )
    if not include_straddling:
        q = q.filter(StatObservation.straddles_cutoff.is_(False))
    return q.first()


def get_observations_for_stat(
    stat: HeadlineStat,
    include_straddling: bool = False,
    limit: int = 20,
) -> list[StatObservation]:
    """
    Return ordered StatObservation rows for a stat (newest period_end first).
    Used on the individual stat leaf page (/stats/<area>/<source-id>).
    """
    q = (
        StatObservation.query
        .filter_by(headline_stat_id=stat.id)
        .order_by(StatObservation.period_end.desc().nullslast())
    )
    if not include_straddling:
        q = q.filter(StatObservation.straddles_cutoff.is_(False))
    return q.limit(limit).all()


def get_latest_observation_for_stat(
    theme_slug: str,
    source_id: str | None = None,
    include_straddling: bool = False,
) -> StatObservation | None:
    """
    Resolve a HeadlineStat by theme_slug (and optionally source_id), then
    return its most recent StatObservation by period_end.

    Multiple stats can share a theme_slug (unique constraint is on
    (theme_slug, source_id)). When source_id is omitted, returns the
    observation for the first matching stat — use source_id to disambiguate
    when a theme has more than one stat.

    NULL period_end rows (legacy placeholder rows from the Phase 1 backfill)
    are ordered last via NULLS LAST, so a real time-series observation always
    takes precedence over a placeholder.

    Returns None if the stat doesn't exist or has no qualifying observations.
    """
    q = HeadlineStat.query.filter_by(theme_slug=theme_slug)
    if source_id is not None:
        q = q.filter_by(source_id=source_id)
    stat = q.first()
    if stat is None:
        return None
    return get_latest_observation(stat, include_straddling=include_straddling)


def cutoff_date() -> date:
    """The canonical ingestion cutoff — 1 July 2024 (start of current government)."""
    return date(2024, 7, 1)


def is_straddling(period_start: date | None, period_end: date | None) -> bool:
    """
    Return True if the observation window straddles the cutoff date.
    Used at ingestion time to set StatObservation.straddles_cutoff.
    """
    cutoff = cutoff_date()
    if period_start is None or period_end is None:
        return False
    return period_start < cutoff <= period_end
